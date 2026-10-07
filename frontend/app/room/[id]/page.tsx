"use client";

/**
 * The live discussion room (FR-08 – FR-12).
 *
 * Three independent things happen on this page, and they are deliberately not
 * coupled:
 *
 * 1. **Media** — WebRTC audio/video through LiveKit.
 * 2. **Control** — the WebSocket: timer, roster, readiness, session lifecycle.
 * 3. **Recording** — microphone windows uploaded for transcription.
 *
 * If the upload path breaks, the discussion continues and the failure is shown
 * without interrupting anyone. If the control socket drops, it reconnects. That
 * is claude.md invariant 1 as the user experiences it.
 */

import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ArenaHeader, Notices, RoundTable, type SeatInfo } from "@/app/immersive/parts";
import { ObserverCard } from "@/app/observer-card";
import { TranscriptCard } from "@/app/transcript-card";
import { ApiError, sessions } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { useMediaRoom } from "@/lib/use-media-room";
import { useSessionSocket } from "@/lib/use-session-socket";
import type { GDSession, MediaToken, TranscriptOut } from "@/lib/types";

export default function RoomPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { user } = useAuth();

  const [session, setSession] = useState<GDSession | null>(null);
  const [credentials, setCredentials] = useState<MediaToken | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ending, setEnding] = useState(false);

  const socket = useSessionSocket(id ?? null);

  const load = useCallback(async () => {
    try {
      const loaded = await sessions.get(id);
      setSession(loaded);
      return loaded;
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "could not load the session");
      return null;
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  // The media room only opens once the session is past the lobby.
  useEffect(() => {
    if (!session) return;
    if (credentials) return;
    if (session.state !== "STARTING" && session.state !== "LIVE") return;
    sessions
      .mediaToken(id)
      .then(setCredentials)
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "could not join the media room"),
      );
  }, [session, credentials, id]);

  const phase = socket.timer?.phase ?? "waiting";
  const isLive = phase === "discussion";
  // Recording starts only once the discussion clock is running: preparation is
  // not part of the transcript.
  const startedAt = isLive ? 0 : null;

  // A host who never joined as a candidate watches the discussion: they are
  // not in the transcript, so they publish nothing and record nothing.
  const observing = credentials?.identity === "HOST";

  const media = useMediaRoom(id ?? null, credentials, {
    video: session?.video_required ?? true,
    startedAt: observing ? null : startedAt,
    publish: !observing,
  });

  // Live transcript: fetched once the discussion is running, then refetched
  // whenever the server announces a newly transcribed window.
  const [transcript, setTranscript] = useState<TranscriptOut | null>(null);
  const latestFrame = socket.events[0];
  useEffect(() => {
    if (!isLive) return;
    if (transcript && latestFrame?.type !== "TRANSCRIPT_APPENDED") return;
    sessions
      .transcript(id)
      .then(setTranscript)
      .catch(() => {}); // Display-only; the report shows the full transcript later.
  }, [isLive, latestFrame, id]);

  // When the session ends, everyone moves to the report together.
  useEffect(() => {
    const state = socket.sessionState ?? session?.state;
    if (state === "ENDED" || state === "EVALUATING" || state === "COMPLETED") {
      router.push(`/report/${id}`);
    }
  }, [socket.sessionState, session?.state, id, router]);

  const isHost = session != null && user != null && session.host_id === user.id;

  async function endSession() {
    setEnding(true);
    try {
      await sessions.end(id);
      router.push(`/report/${id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "could not end the session");
      setEnding(false);
    }
  }

  if (!session) return <p className="empty">{error ?? "Joining…"}</p>;

  const timer = socket.timer;
  const topic = socket.topic?.released ? socket.topic.topic : session.topic;
  const roster = socket.lobby?.participants ?? session.participants;

  const hasVideo = !observing || media.remotes.length > 0;
  const seats: SeatInfo[] = roster.map((participant) => {
    const speaking =
      socket.speaking.includes(participant.candidate_label) ||
      media.activeSpeakers.includes(participant.candidate_label);
    const isAI = participant.display_name.endsWith("(AI)");
    return {
      label: participant.candidate_label,
      name: participant.display_name.replace(/\s*\(AI\)$/, ""),
      you: participant.candidate_label === socket.you,
      badge: isAI ? "AI" : undefined,
      speaking,
      strikes: socket.penalties[participant.candidate_label]?.interruptions,
      away: participant.state === "DISCONNECTED",
      sub: participant.state === "DISCONNECTED" ? "away" : "listening",
    };
  });
  const names = new Map(seats.map((s) => [s.label, s.name]));
  const nameOf = (label: string) => names.get(label) ?? label;

  const lines = transcript?.lines ?? [];
  const latest = lines[lines.length - 1];
  const myPenalty = socket.you ? socket.penalties[socket.you]?.penalty : undefined;

  return (
    <div className="arena">
      <ArenaHeader
        topic={topic}
        phaseLabel={phase === "preparation" ? "Preparation" : isLive ? "Live" : phase}
        live={isLive}
        seconds={timer?.seconds_remaining ?? 0}
        duration={timer?.duration_seconds ?? 0}
        warn={timer?.warning}
        right={
          <>
            {!observing && isLive && (myPenalty ?? 0) > 0 && (
              <span className="arena-phase" title="Interruption penalty so far">
                <i style={{ background: "var(--hot)" }} />
                Interruptions: −{myPenalty}
              </span>
            )}
            {isHost && (
              <button type="button" className="arena-btn arena-btn--danger" onClick={endSession} disabled={ending}>
                {ending ? "Ending…" : "End discussion"}
              </button>
            )}
          </>
        }
      />

      <div>
        <RoundTable
          seats={seats}
          centre={
            latest ? (
              <>
                <p className="caption-who">{nameOf(latest.candidate_label)}</p>
                <p className="caption-now">{latest.text}</p>
              </>
            ) : (
              <p>
                {observing
                  ? "You are hosting: watching, not recorded or evaluated."
                  : isLive
                    ? "Nobody has spoken yet."
                    : "The discussion opens when preparation ends."}
              </p>
            )
          }
        />
        <Notices notices={socket.notices} nameOf={nameOf} />
      </div>

      <div className="stack" style={{ gap: "0.7rem" }}>
        {error && <div className="alert alert--error">{error}</div>}
        {/* A watching host publishes nothing, so a missing media server (e.g. no
            LiveKit when running without Docker) is not their problem to see. */}
        {media.error && !observing && <div className="alert alert--error">Media: {media.error}</div>}
        {media.uploadError && (
          <div className="alert alert--error">
            Transcription upload failed ({media.uploadError}). The discussion is unaffected — the
            audio is retried server-side.
          </div>
        )}

        {hasVideo && (
          <div className="video-strip">
            {!observing && (
              <div className="video-self">
                <video
                  ref={media.localVideoRef}
                  className="remote-video"
                  autoPlay
                  muted
                  playsInline
                  style={{ transform: "scaleX(-1)", opacity: media.cameraEnabled ? 1 : 0.15 }}
                />
                <span className="meta">You{media.micEnabled ? "" : " (muted)"}</span>
              </div>
            )}
            <div id="remote-media" className="video-remote" />
          </div>
        )}

        {!observing && hasVideo && (
          <div className="arena-foot">
            <button
              type="button"
              className="arena-btn"
              onClick={media.toggleMic}
              disabled={!media.connected}
              aria-pressed={!media.micEnabled}
            >
              {media.micEnabled ? "Mute" : "Unmute"}
            </button>
            <button
              type="button"
              className="arena-btn"
              onClick={media.toggleCamera}
              disabled={!media.connected}
              aria-pressed={!media.cameraEnabled}
            >
              {media.cameraEnabled ? "Turn camera off" : "Turn camera on"}
            </button>
            <span className="arena-hint">
              {media.connected
                ? `Connected. ${media.uploadedChunks} audio window${media.uploadedChunks === 1 ? "" : "s"} sent for transcription.`
                : media.connecting
                  ? "Connecting…"
                  : "Media offline."}
            </span>
          </div>
        )}

        {isLive && (
          <details className="arena-drawer">
            <summary>Transcript and live observations</summary>
            <div className="split">
              {transcript && <TranscriptCard transcript={transcript} live />}
              <ObserverCard sessionId={id} />
            </div>
          </details>
        )}
      </div>
    </div>
  );
}
