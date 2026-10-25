"use client";

/**
 * LiveKit media plane (FR-08, design §10).
 *
 * This hook owns two things that look similar but are not:
 *
 * 1. **The live media room.** WebRTC audio/video between participants, via
 *    LiveKit. Nothing about evaluation depends on it.
 * 2. **The recording tap.** A separate `MediaRecorder` on the local microphone
 *    that cuts fixed windows and POSTs them for transcription (FR-12).
 *
 * They are deliberately independent. If the upload path stalls, the discussion
 * carries on; if LiveKit drops, the recording still lands. That separation is
 * claude.md invariant 1 expressed in the browser.
 *
 * Speaker attribution comes for free here: each browser records only its own
 * microphone, so the server knows whose audio it received without diarizing a
 * mixed stream.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { uploadAudio } from "./api";  // upload each recorded window
import type { MediaToken } from "./types";  // LiveKit credentials shape

// Matches the server's processing window (CHUNK_SECONDS in chunking.py) so an
// uploaded chunk lines up with the window the Observer will read.
const RECORD_WINDOW_MS = 45_000;

/** Another participant in the room, as shown in the video grid. */
export interface RemoteParticipant {
  identity: string;
  name: string;
  speaking: boolean;
  audioEnabled: boolean;
  videoEnabled: boolean;
}

/** Connection, device and upload status exposed by the hook. */
export interface MediaState {
  connected: boolean;
  connecting: boolean;
  error: string | null;
  micEnabled: boolean;
  cameraEnabled: boolean;
  remotes: RemoteParticipant[];
  activeSpeakers: string[];
  uploadedChunks: number;
  uploadError: string | null;
}

/** Join the LiveKit room and record the local microphone in windows for transcription. */
export function useMediaRoom(
  sessionId: string | null,
  credentials: MediaToken | null,
  /** `publish: false` is a watching host — subscribe-only, and nothing recorded. */
  options: { video: boolean; startedAt: number | null; publish?: boolean },
) {
  const [state, setState] = useState<MediaState>({
    connected: false,
    connecting: false,
    error: null,
    micEnabled: true,
    cameraEnabled: options.video,
    remotes: [],
    activeSpeakers: [],
    uploadedChunks: 0,
    uploadError: null,
  });

  const roomRef = useRef<import("livekit-client").Room | null>(null);
  const localVideoRef = useRef<HTMLVideoElement | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  // The recording tap's own mic stream, and whether the user has muted. Muting
  // silences the tap too: what nobody heard must not land in the transcript.
  const recordStreamRef = useRef<MediaStream | null>(null);
  const micOnRef = useRef(true);
  const sequenceRef = useRef(0);
  const attachedRef = useRef(new Map<string, HTMLMediaElement>());

  /** Move already-attached remote audio/video elements into the given container. */
  const attachRemoteMedia = useCallback((container: HTMLElement | null) => {
    if (!container) return;
    for (const [, element] of attachedRef.current) {
      if (element.parentElement !== container) container.appendChild(element);
    }
  }, []);

  const publish = options.publish !== false;

  useEffect(() => {
    if (!sessionId || !credentials) return;
    let cancelled = false;

    const connect = async () => {
      setState((prev) => ({ ...prev, connecting: true, error: null }));
      // Imported lazily so the SDK is not pulled into pages that never join a
      // room, and so it never runs during server rendering.
      const { Room, RoomEvent, Track } = await import("livekit-client");

      const room = new Room({ adaptiveStream: true, dynacast: true });
      roomRef.current = room;

      /** Refresh the remote-participant list from LiveKit's current state. */
      const syncRemotes = () => {
        if (cancelled) return;
        setState((prev) => ({
          ...prev,
          remotes: Array.from(room.remoteParticipants.values()).map((p) => ({
            identity: p.identity,
            name: p.name || p.identity,
            speaking: p.isSpeaking,
            audioEnabled: p.isMicrophoneEnabled,
            videoEnabled: p.isCameraEnabled,
          })),
        }));
      };

      room
        .on(RoomEvent.ParticipantConnected, syncRemotes)
        .on(RoomEvent.ParticipantDisconnected, (participant) => {
          const element = attachedRef.current.get(participant.identity);
          element?.remove();
          attachedRef.current.delete(participant.identity);
          syncRemotes();
        })
        .on(RoomEvent.TrackMuted, syncRemotes)
        .on(RoomEvent.TrackUnmuted, syncRemotes)
        .on(RoomEvent.ActiveSpeakersChanged, (speakers) => {
          if (cancelled) return;
          setState((prev) => ({
            ...prev,
            activeSpeakers: speakers.map((s) => s.identity),
          }));
          syncRemotes();
        })
        .on(RoomEvent.TrackSubscribed, (track, _publication, participant) => {
          if (track.kind === Track.Kind.Audio || track.kind === Track.Kind.Video) {
            const element = track.attach();
            element.setAttribute("data-identity", participant.identity);
            if (track.kind === Track.Kind.Video) {
              element.classList.add("remote-video");
            }
            attachedRef.current.set(`${participant.identity}:${track.kind}`, element);
            document.getElementById("remote-media")?.appendChild(element);
          }
          syncRemotes();
        })
        .on(RoomEvent.TrackUnsubscribed, (track, _publication, participant) => {
          track.detach().forEach((element) => element.remove());
          attachedRef.current.delete(`${participant.identity}:${track.kind}`);
          syncRemotes();
        })
        .on(RoomEvent.LocalTrackPublished, (publication) => {
          // Self-preview. Turning the camera off mutes this same track rather
          // than unpublishing it, so attaching once is enough.
          if (publication.track?.kind === Track.Kind.Video && localVideoRef.current) {
            publication.track.attach(localVideoRef.current);
          }
        })
        .on(RoomEvent.Disconnected, () => {
          if (!cancelled) setState((prev) => ({ ...prev, connected: false }));
        });

      try {
        await room.connect(credentials.url, credentials.token);
        if (publish) {
          await room.localParticipant.setMicrophoneEnabled(true);
          if (options.video) await room.localParticipant.setCameraEnabled(true);
        }
        if (cancelled) return;
        setState((prev) => ({ ...prev, connected: true, connecting: false }));
        syncRemotes();
      } catch (error) {
        if (cancelled) return;
        setState((prev) => ({
          ...prev,
          connecting: false,
          connected: false,
          error: error instanceof Error ? error.message : "could not join the media room",
        }));
      }
    };

    void connect();
    return () => {
      cancelled = true;
      void roomRef.current?.disconnect();
      roomRef.current = null;
      for (const [, element] of attachedRef.current) element.remove();
      attachedRef.current.clear();
    };
  }, [sessionId, credentials, options.video, publish]);

  /**
   * Recording tap (FR-12).
   *
   * Runs off its own `getUserMedia` stream rather than borrowing LiveKit's
   * track: LiveKit applies noise suppression and gating tuned for listening,
   * and the transcript wants the rawer signal. It also means a LiveKit
   * reconnect does not interrupt recording.
   */
  useEffect(() => {
    if (!sessionId || options.startedAt === null || !publish) return;
    let stream: MediaStream | null = null;
    let stopped = false;
    let windowTimer: ReturnType<typeof setTimeout> | undefined;

    /** Open the microphone and start recording consecutive windows. */
    const start = async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      } catch (error) {
        setState((prev) => ({
          ...prev,
          uploadError: error instanceof Error ? error.message : "microphone unavailable",
        }));
        return;
      }
      if (stopped) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      recordStreamRef.current = stream;
      // Respect a mute made before the discussion clock started.
      stream.getAudioTracks().forEach((track) => (track.enabled = micOnRef.current));

      /** Upload one finished window; failures are reported but never fatal. */
      const upload = async (event: BlobEvent) => {
        if (!event.data.size || options.startedAt === null) return;
        const sequence = sequenceRef.current++;
        // Window bounds on the session clock, which is what the server stores
        // segments against.
        const start = (sequence * RECORD_WINDOW_MS) / 1000;
        const end = start + RECORD_WINDOW_MS / 1000;
        try {
          await uploadAudio(sessionId, sequence, start, end, event.data);
          setState((prev) => ({
            ...prev,
            uploadedChunks: prev.uploadedChunks + 1,
            uploadError: null,
          }));
        } catch (error) {
          // A failed upload is reported, never fatal: the discussion continues
          // and the chunk can be re-sent or reprocessed server-side.
          setState((prev) => ({
            ...prev,
            uploadError: error instanceof Error ? error.message : "upload failed",
          }));
        }
      };

      // One fresh recorder per window. `start(timeslice)` would cut a single
      // WebM stream into pieces, and only the first piece carries the header —
      // every later window would be undecodable on the server.
      const recordWindow = () => {
        if (stopped || !stream) return;
        const recorder = new MediaRecorder(stream, { mimeType: pickMimeType() });
        recorderRef.current = recorder;
        recorder.ondataavailable = upload;
        recorder.start();
        windowTimer = setTimeout(() => {
          if (recorder.state === "recording") recorder.stop();
          recordWindow();
        }, RECORD_WINDOW_MS);
      };
      recordWindow();
    };

    void start();
    return () => {
      stopped = true;
      if (windowTimer) clearTimeout(windowTimer);
      if (recorderRef.current?.state === "recording") recorderRef.current.stop();
      recorderRef.current = null;
      recordStreamRef.current = null;
      stream?.getTracks().forEach((track) => track.stop());
    };
  }, [sessionId, options.startedAt, publish]);

  /** Mute/unmute the microphone in the room and in the recording tap. */
  const toggleMic = useCallback(async () => {
    const room = roomRef.current;
    if (!room) return;
    const next = !room.localParticipant.isMicrophoneEnabled;
    await room.localParticipant.setMicrophoneEnabled(next);
    micOnRef.current = next;
    // A disabled track records silence, so chunk timing stays aligned.
    recordStreamRef.current?.getAudioTracks().forEach((track) => (track.enabled = next));
    setState((prev) => ({ ...prev, micEnabled: next }));
  }, []);

  /** Turn the camera on or off. */
  const toggleCamera = useCallback(async () => {
    const room = roomRef.current;
    if (!room) return;
    const next = !room.localParticipant.isCameraEnabled;
    await room.localParticipant.setCameraEnabled(next);
    setState((prev) => ({ ...prev, cameraEnabled: next }));
  }, []);

  return { ...state, toggleMic, toggleCamera, attachRemoteMedia, localVideoRef };
}

/** The first audio container/codec this browser's MediaRecorder supports. */
function pickMimeType(): string {
  const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"];
  for (const type of candidates) {
    if (typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported(type)) return type;
  }
  return "";
}
