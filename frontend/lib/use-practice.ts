"use client";

/**
 * Solo practice client: one WebSocket carrying the whole conversation.
 *
 * Up: the student's voice. We listen to the microphone, detect where each
 * utterance starts and ends (simple energy VAD with a short pre-roll so the first
 * syllable survives), tell the server when speech starts so it can cut off a
 * persona who is mid-sentence, and send each utterance as a small WAV.
 *
 * Down: persona audio (mp3 chunks, played once the line has fully arrived - the
 * server streams faster than real time) or, when no ElevenLabs key is
 * configured, text that the browser speaks itself. Timer, captions and tracked
 * turns arrive as ordinary JSON frames.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { API_URL, tokenStore } from "./api";
import { apply, INITIAL, type Frame, type SocketState } from "./use-session-socket";
import { encodeWav } from "./wav";

export interface Caption {
  id: number;
  label: string;
  name: string;
  text: string;
}

export interface PracticePersona {
  label: string;
  name: string;
  eagerness: number;
}

interface Snapshot {
  you: string;
  personas: PracticePersona[];
  voice: "elevenlabs" | "browser";
  duration_seconds: number;
}

// Voice-activity detection. Frames are ~43 ms at 48 kHz.
const VOICE_FRAMES_TO_START = 3;
const SILENCE_MS_TO_END = 700;
const MIN_UTTERANCE_MS = 450;
const MAX_UTTERANCE_MS = 30_000;
const PRE_ROLL_FRAMES = 8;
// While a persona is audible the speakers leak into the mic, so demand more.
const THRESHOLD_QUIET = 0.022;
const THRESHOLD_WHILE_PLAYING = 0.06;

let captionSeq = 0;

const FEMALE_VOICE = /zira|heera|hazel|susan|samantha|aria|jenny|female|kalpana|neerja/i;
const MALE_VOICE = /david|ravi|mark|george|guy|male|prabhat/i;

/** Browser voice for the fallback: Indian English first, then by gender. */
function pickBrowserVoice(gender?: string): SpeechSynthesisVoice | undefined {
  const voices = speechSynthesis.getVoices().filter((v) => v.lang.startsWith("en"));
  const wanted = gender === "m" ? MALE_VOICE : FEMALE_VOICE;
  const matches = voices.filter((v) => wanted.test(v.name));
  return (
    matches.find((v) => v.lang === "en-IN") ?? matches[0] ?? voices.find((v) => v.lang === "en-IN")
  );
}

export function usePractice(sessionId: string | null) {
  const [core, setCore] = useState<SocketState>(INITIAL);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [captions, setCaptions] = useState<Caption[]>([]);
  const [thinking, setThinking] = useState<string | null>(null);
  const [personaLevel, setPersonaLevel] = useState(0);
  const [micLevel, setMicLevel] = useState(0);
  const [micError, setMicError] = useState<string | null>(null);
  const [listening, setListening] = useState(false);
  const [muted, setMuted] = useState(false);
  // Text GD mode (M14 FR-14.19): the student types; the personas still speak.
  const [typing, setTyping] = useState(false);

  const socketRef = useRef<WebSocket | null>(null);
  const liveRef = useRef(false);
  const mutedRef = useRef(false);
  const playingRef = useRef(false);

  // Persona playback.
  const pendingRef = useRef<{ text: string; audio: boolean; gender?: string } | null>(null);
  const chunksRef = useRef<ArrayBuffer[]>([]);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const playCtxRef = useRef<AudioContext | null>(null);
  const synthRef = useRef(false);

  // Microphone.
  const micRef = useRef<{ stream: MediaStream; ctx: AudioContext; proc: ScriptProcessorNode } | null>(
    null,
  );

  const stopPlayback = useCallback(() => {
    playingRef.current = false;
    synthRef.current = false;
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.src = "";
      audioRef.current = null;
    }
    if (typeof speechSynthesis !== "undefined") speechSynthesis.cancel();
    setPersonaLevel(0);
  }, []);

  /** Tell the server the line has finished playing so the next speaker can start. */
  const sendEnded = useCallback(() => {
    const socket = socketRef.current;
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: "PLAYBACK_ENDED" }));
    }
  }, []);

  const playPending = useCallback(() => {
    const pending = pendingRef.current;
    pendingRef.current = null;
    if (!pending) return;
    playingRef.current = true;

    if (pending.audio && chunksRef.current.length > 0) {
      const blob = new Blob(chunksRef.current, { type: "audio/mpeg" });
      chunksRef.current = [];
      const audio = new Audio(URL.createObjectURL(blob));
      audioRef.current = audio;
      try {
        const ctx = (playCtxRef.current ??= new AudioContext());
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 256;
        ctx.createMediaElementSource(audio).connect(analyser);
        analyser.connect(ctx.destination);
        analyserRef.current = analyser;
      } catch {
        analyserRef.current = null; // Playback still works without the meter.
      }
      audio.onended = () => {
        playingRef.current = false;
        setPersonaLevel(0);
        sendEnded();
      };
      void audio.play().catch(() => {
        playingRef.current = false;
        sendEnded();
      });
      return;
    }

    // No audio from the server: the browser speaks the line.
    chunksRef.current = [];
    analyserRef.current = null;
    synthRef.current = true;
    const utterance = new SpeechSynthesisUtterance(pending.text);
    const voice = pickBrowserVoice(pending.gender);
    if (voice) utterance.voice = voice;
    utterance.onend = () => {
      synthRef.current = false;
      playingRef.current = false;
      setPersonaLevel(0);
      sendEnded();
    };
    utterance.onerror = utterance.onend;
    speechSynthesis.cancel();
    speechSynthesis.speak(utterance);
  }, [sendEnded]);

  // Persona orb level: real analyser data for ElevenLabs audio, a gentle
  // pulse for browser speech.
  useEffect(() => {
    let frame = 0;
    let last = 0;
    const data = new Uint8Array(128);
    const tick = (now: number) => {
      frame = requestAnimationFrame(tick);
      if (now - last < 50 || !playingRef.current) return;
      last = now;
      const analyser = analyserRef.current;
      if (analyser && audioRef.current) {
        analyser.getByteTimeDomainData(data);
        let peak = 0;
        for (const v of data) peak = Math.max(peak, Math.abs(v - 128));
        setPersonaLevel(Math.min(1, peak / 70));
      } else if (synthRef.current) {
        setPersonaLevel(0.35 + 0.3 * Math.abs(Math.sin(now / 140)));
      }
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, []);

  const handleFrame = useCallback(
    (frame: Frame) => {
      switch (frame.type) {
        case "PRACTICE_READY": {
          const p = frame.payload as unknown as Snapshot & {
            session_state: string;
            timer: SocketState["timer"];
            topic: SocketState["topic"];
          };
          setSnapshot(p);
          setCore((prev) =>
            apply(prev, {
              ...frame,
              type: "STATE_SNAPSHOT",
              payload: {
                you: p.you,
                session_state: p.session_state,
                timer: p.timer,
                topic: p.topic,
              },
            }),
          );
          liveRef.current = p.session_state === "LIVE";
          return;
        }
        case "PERSONA_AUDIO_START": {
          const p = frame.payload as {
            audio: boolean;
            text: string;
            candidate_label: string;
            gender?: string;
          };
          // A new line starting means the old one must not still be playing.
          stopPlayback();
          pendingRef.current = { text: p.text, audio: p.audio, gender: p.gender };
          chunksRef.current = [];
          setThinking(p.candidate_label);
          return;
        }
        case "PERSONA_AUDIO_FALLBACK":
          if (pendingRef.current) pendingRef.current.audio = false;
          return;
        case "PERSONA_AUDIO_READY":
          setThinking(null);
          playPending();
          return;
        case "STOP_AUDIO":
          pendingRef.current = null;
          chunksRef.current = [];
          setThinking(null);
          stopPlayback();
          return;
        case "PERSONA_AUDIO_END":
          setThinking(null);
          return;
        case "TRANSCRIPT_APPENDED": {
          const p = frame.payload as { candidate_label: string; name?: string; text?: string };
          if (p.text) {
            setCaptions((prev) =>
              [
                ...prev,
                { id: ++captionSeq, label: p.candidate_label, name: p.name ?? p.candidate_label, text: p.text! },
              ].slice(-40),
            );
          }
          return;
        }
        case "SESSION_STARTED":
          liveRef.current = frame.payload.phase === "discussion";
          break;
        case "TIMER_STARTED":
          liveRef.current = true;
          break;
        case "SESSION_ENDED":
          liveRef.current = false;
          stopPlayback();
          break;
        default:
          break;
      }
      setCore((prev) => apply(prev, frame));
    },
    [playPending, stopPlayback],
  );

  // Socket lifecycle.
  useEffect(() => {
    if (!sessionId) return;
    const token = tokenStore.access;
    if (!token) return;
    const socket = new WebSocket(
      `${API_URL.replace(/^http/, "ws")}/ws/practice/${sessionId}?token=${token}`,
    );
    socket.binaryType = "arraybuffer";
    socketRef.current = socket;
    socket.onopen = () => setCore((prev) => ({ ...prev, connected: true, error: null }));
    socket.onclose = (event) =>
      setCore((prev) => ({
        ...prev,
        connected: false,
        error: event.code >= 4400 ? event.reason || "connection refused" : prev.error,
      }));
    socket.onmessage = (message) => {
      if (typeof message.data === "string") {
        try {
          handleFrame(JSON.parse(message.data));
        } catch {
          // Ignore a malformed frame rather than dropping the room.
        }
      } else {
        chunksRef.current.push(message.data as ArrayBuffer);
      }
    };
    return () => {
      socket.close();
      socketRef.current = null;
      stopPlayback();
    };
  }, [sessionId, handleFrame, stopPlayback]);

  const sendJson = useCallback((type: string) => {
    const socket = socketRef.current;
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type }));
  }, []);

  /** Begin: unlocks audio (needs a click), opens the mic and starts the session. */
  const start = useCallback(async () => {
    setMicError(null);
    // Playing audio and capturing the mic both need a user gesture; do both now.
    playCtxRef.current ??= new AudioContext();
    void playCtxRef.current.resume();
    if (typeof speechSynthesis !== "undefined") speechSynthesis.cancel();

    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      const ctx = new AudioContext();
      const source = ctx.createMediaStreamSource(stream);
      const proc = ctx.createScriptProcessor(2048, 1, 1);
      const sink = ctx.createGain();
      sink.gain.value = 0;
      source.connect(proc);
      proc.connect(sink);
      sink.connect(ctx.destination);
      micRef.current = { stream, ctx, proc };

      const frameMs = (2048 / ctx.sampleRate) * 1000;
      const preRoll: Float32Array[] = [];
      let utterance: Float32Array[] = [];
      let speaking = false;
      let voiced = 0;
      let silenceMs = 0;
      let utteranceMs = 0;
      let levelTick = 0;

      const finish = () => {
        const socket = socketRef.current;
        const longEnough = utteranceMs - silenceMs >= MIN_UTTERANCE_MS;
        sendJson("UTTERANCE_END");
        // An empty frame tells the server to drop the utterance (noise).
        if (socket?.readyState === WebSocket.OPEN) {
          socket.send(longEnough ? encodeWav(utterance, ctx.sampleRate) : new ArrayBuffer(0));
        }
        utterance = [];
        speaking = false;
        voiced = 0;
        silenceMs = 0;
        utteranceMs = 0;
      };

      proc.onaudioprocess = (event) => {
        const data = new Float32Array(event.inputBuffer.getChannelData(0));
        let sum = 0;
        for (const v of data) sum += v * v;
        const rms = mutedRef.current ? 0 : Math.sqrt(sum / data.length);

        if (++levelTick % 2 === 0) setMicLevel(Math.min(1, rms * 9));

        const threshold = playingRef.current ? THRESHOLD_WHILE_PLAYING : THRESHOLD_QUIET;
        const loud = rms > threshold && liveRef.current;

        if (!speaking) {
          preRoll.push(data);
          if (preRoll.length > PRE_ROLL_FRAMES) preRoll.shift();
          voiced = loud ? voiced + 1 : 0;
          if (voiced >= VOICE_FRAMES_TO_START) {
            speaking = true;
            utterance = [...preRoll];
            utteranceMs = utterance.length * frameMs;
            silenceMs = 0;
            sendJson("UTTERANCE_START");
          }
          return;
        }

        utterance.push(data);
        utteranceMs += frameMs;
        silenceMs = loud ? 0 : silenceMs + frameMs;
        if (silenceMs >= SILENCE_MS_TO_END || utteranceMs >= MAX_UTTERANCE_MS) finish();
      };
      setListening(true);
    } catch (err) {
      setMicError(
        err instanceof Error
          ? `Microphone unavailable: ${err.message}. Allow microphone access and try again.`
          : "Microphone unavailable.",
      );
      return;
    }
    sendJson("START");
  }, [sendJson]);

  /** Begin without a microphone: type your turns (FR-14.19). Audio still needs the click. */
  const startTyped = useCallback(() => {
    setMicError(null);
    playCtxRef.current ??= new AudioContext();
    void playCtxRef.current.resume();
    if (typeof speechSynthesis !== "undefined") speechSynthesis.cancel();
    setTyping(true);
    sendJson("START");
  }, [sendJson]);

  /** A typed turn. The server waits for a persona's line to finish: typing never cuts in. */
  const sendText = useCallback((text: string): boolean => {
    const socket = socketRef.current;
    const clean = text.trim();
    if (!clean || socket?.readyState !== WebSocket.OPEN) return false;
    socket.send(JSON.stringify({ type: "TEXT", text: clean }));
    return true;
  }, []);

  /** A raised hand (FR-14.12): the moderator invites you in at the next gap. */
  const raiseHand = useCallback(() => sendJson("RAISE_HAND"), [sendJson]);

  const toggleMute = useCallback(() => {
    mutedRef.current = !mutedRef.current;
    setMuted(mutedRef.current);
  }, []);

  // Release the microphone when leaving.
  useEffect(
    () => () => {
      const mic = micRef.current;
      if (mic) {
        mic.proc.disconnect();
        mic.stream.getTracks().forEach((t) => t.stop());
        void mic.ctx.close();
        micRef.current = null;
      }
    },
    [],
  );

  return {
    ...core,
    snapshot,
    captions,
    thinking,
    personaLevel,
    micLevel,
    micError,
    listening,
    muted,
    typing,
    start,
    startTyped,
    sendText,
    raiseHand,
    toggleMute,
  };
}
