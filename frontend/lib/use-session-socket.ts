"use client";

/**
 * WebSocket control plane client (FR-09, design §10).
 *
 * Control events only — audio and video go over LiveKit, not through here.
 *
 * The server is authoritative for everything this hook exposes. It never
 * counts the clock down locally or guesses at a state change: it renders the
 * last frame the server sent. That is what keeps every participant's screen
 * agreeing with every other's.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { playAgentAudio } from "./agent-audio";  // play simulated participants' spoken lines
import { API_URL, tokenStore } from "./api";  // backend URL and the stored access token
import type { LobbyView, TimerView, TopicView } from "./types";  // lobby, timer and topic shapes

/** The server → client event names this client understands. */
export type ServerEventType =
  | "USER_JOINED"
  | "USER_LEFT"
  | "USER_READY"
  | "SPEECH_STARTED"
  | "SPEECH_STOPPED"
  | "TIMER_STARTED"
  | "TIMER_UPDATED"
  | "SESSION_STARTED"
  | "SESSION_ENDED"
  | "DISCONNECTED"
  | "RECONNECTED"
  | "STATE_SNAPSHOT"
  | "TOPIC_RELEASED"
  | "TRANSCRIPT_APPENDED"
  | "TURN_RECORDED"
  | "METRICS_UPDATED"
  | "EVALUATION_COMPLETED"
  | "ERROR";

/** Envelope for every WebSocket message from the server. */
export interface Frame {
  // Practice mode adds its own frame names on top of the shared ones.
  type: ServerEventType | (string & {});
  session_id: string;
  payload: Record<string, unknown>;
  at: string;
}

/** A tracked turn worth telling the room about: an interruption or a direct reply. */
export interface TurnNotice {
  id: number;
  kind: "interruption" | "reply";
  by: string;
  target: string;
  penalty: number;
}

/** Everything the UI shows that comes from the control-plane socket. */
export interface SocketState {
  connected: boolean;
  lobby: LobbyView | null;
  timer: TimerView | null;
  topic: TopicView | null;
  sessionState: string | null;
  you: string | null;
  speaking: string[];
  events: Frame[];
  error: string | null;
  evaluationReady: boolean;
  /** Running interruption count and score penalty per candidate label. */
  penalties: Record<string, { interruptions: number; penalty: number }>;
  /** Most recent notices first; the UI shows them briefly as toasts. */
  notices: TurnNotice[];
  /** Seconds each participant has held the floor, from recorded turns (M14 FR-14.11). */
  floor: Record<string, number>;
  /** Turns already counted into `floor`, so a re-sent turn is not counted twice. */
  counted: Record<string, true>;
  /** The latest ideas on the table, from the Observer (M14 FR-14.13). */
  ideas: { ideas: string[]; open_questions: string[] } | null;
}

/** Counter giving each turn notice a unique id. */
let noticeSeq = 0;

/** State before the first frame arrives. */
export const INITIAL: SocketState = {
  connected: false,
  lobby: null,
  timer: null,
  topic: null,
  sessionState: null,
  you: null,
  speaking: [],
  events: [],
  error: null,
  evaluationReady: false,
  penalties: {},
  notices: [],
  floor: {},
  counted: {},
  ideas: null,
};

// Keep the visible log short — it is a debug aid, not a transcript.
const MAX_EVENTS = 50;

// Reconnect backoff, capped. A dropped socket must not end the session, so the
// client keeps trying rather than giving up (design §9).
const RECONNECT_BASE_MS = 500;
/** Upper bound on the reconnect delay. */
const RECONNECT_MAX_MS = 10_000;

/**
 * Connect to a session's control socket, reconnect on drops, and expose server-driven state and
 * actions.
 */
export function useSessionSocket(sessionId: string | null) {
  const [state, setState] = useState<SocketState>(INITIAL);
  // The live socket, how many reconnects have failed in a row, and whether we closed it on purpose.
  const socketRef = useRef<WebSocket | null>(null);
  const attemptRef = useRef(0);
  const closedByUs = useRef(false);

  /** Send a typed message to the server if the socket is open. */
  const send = useCallback((type: string, payload: Record<string, unknown> = {}) => {
    const socket = socketRef.current;
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type, payload }));
    }
  }, []);

  useEffect(() => {
    if (!sessionId) return;
    closedByUs.current = false;
    let timeout: ReturnType<typeof setTimeout> | undefined;

    /** Open the socket; on an unexpected close, retry with exponential backoff. */
    const connect = () => {
      const token = tokenStore.access;
      if (!token) return;

      // The browser WebSocket API cannot set an Authorization header, so the
      // short-lived access token rides in the query string.
      const url = `${API_URL.replace(/^http/, "ws")}/ws/sessions/${sessionId}?token=${token}`;
      const socket = new WebSocket(url);
      socketRef.current = socket;

      socket.onopen = () => {
        attemptRef.current = 0;
        setState((prev) => ({ ...prev, connected: true, error: null }));
      };

      socket.onmessage = (message) => {
        let frame: Frame;
        try {
          frame = JSON.parse(message.data);
        } catch {
          return;
        }
        if (frame.type === "AGENT_AUDIO") {
          // Played, not stored: the payload is a whole spoken line.
          playAgentAudio(String(frame.payload.audio_b64 ?? ""));
          return;
        }
        setState((prev) => apply(prev, frame));
      };

      socket.onclose = (event) => {
        setState((prev) => ({ ...prev, connected: false }));
        if (closedByUs.current) return;
        // 4401/4403/4404 are our own rejections — retrying will not help.
        if (event.code >= 4400 && event.code < 4500) {
          setState((prev) => ({ ...prev, error: event.reason || "connection refused" }));
          return;
        }
        const delay = Math.min(
          RECONNECT_BASE_MS * 2 ** attemptRef.current,
          RECONNECT_MAX_MS,
        );
        attemptRef.current += 1;
        timeout = setTimeout(connect, delay);
      };
    };

    connect();
    return () => {
      closedByUs.current = true;
      if (timeout) clearTimeout(timeout);
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, [sessionId]);

  // Convenience senders for lobby readiness and speech activity.
  const setReady = useCallback((ready: boolean) => send(ready ? "READY" : "NOT_READY"), [send]);
  const speechStarted = useCallback(() => send("SPEECH_STARTED"), [send]);
  const speechStopped = useCallback(() => send("SPEECH_STOPPED"), [send]);

  return { ...state, setReady, speechStarted, speechStopped, send };
}

/** Pure reducer: fold one server frame into the socket state. */
export function apply(prev: SocketState, frame: Frame): SocketState {
  const events = [frame, ...prev.events].slice(0, MAX_EVENTS);
  const label = frame.payload.candidate_label as string | undefined;

  switch (frame.type) {
    case "STATE_SNAPSHOT": {
      // A ping response reuses this frame type but carries no snapshot.
      if (frame.payload.pong) return { ...prev, events };
      return {
        ...prev,
        events,
        you: (frame.payload.you as string) ?? prev.you,
        sessionState: (frame.payload.session_state as string) ?? prev.sessionState,
        lobby: (frame.payload.lobby as LobbyView) ?? prev.lobby,
        timer: (frame.payload.timer as TimerView) ?? prev.timer,
        topic: (frame.payload.topic as TopicView) ?? prev.topic,
      };
    }
    case "TIMER_UPDATED":
    case "TIMER_STARTED":
      return { ...prev, events, timer: frame.payload as unknown as TimerView };
    case "TOPIC_RELEASED":
      return { ...prev, events, topic: frame.payload as unknown as TopicView };
    case "SESSION_STARTED":
      return {
        ...prev,
        events,
        sessionState: frame.payload.phase === "discussion" ? "LIVE" : "STARTING",
      };
    case "SESSION_ENDED":
      return { ...prev, events, sessionState: "ENDED" };
    case "EVALUATION_COMPLETED":
      return { ...prev, events, evaluationReady: true, sessionState: "COMPLETED" };
    case "SPEECH_STARTED":
      return {
        ...prev,
        events,
        speaking: label && !prev.speaking.includes(label) ? [...prev.speaking, label] : prev.speaking,
      };
    case "SPEECH_STOPPED":
      return { ...prev, events, speaking: prev.speaking.filter((s) => s !== label) };
    case "IDEAS_UPDATED": {
      const p = frame.payload as { ideas?: string[]; open_questions?: string[] };
      return { ...prev, events, ideas: { ideas: p.ideas ?? [], open_questions: p.open_questions ?? [] } };
    }
    case "TURN_RECORDED": {
      const p = frame.payload as {
        candidate_label: string;
        interrupted: string | null;
        reply_to: string | null;
        interruptions: number;
        penalty: number;
        start?: number;
        end?: number;
      };
      const penalties = {
        ...prev.penalties,
        [p.candidate_label]: { interruptions: p.interruptions, penalty: p.penalty },
      };
      // A turn can arrive twice (an older one re-marked as interrupted): count it once.
      const key = `${p.candidate_label}|${p.start}|${p.end}`;
      const fresh = typeof p.start === "number" && typeof p.end === "number" && !prev.counted[key];
      const held = fresh ? Math.max(0, p.end! - p.start!) : 0;
      const floor = fresh
        ? { ...prev.floor, [p.candidate_label]: (prev.floor[p.candidate_label] ?? 0) + held }
        : prev.floor;
      const counted = fresh ? { ...prev.counted, [key]: true as const } : prev.counted;
      const notice: TurnNotice | null = p.interrupted
        ? { id: ++noticeSeq, kind: "interruption", by: p.candidate_label, target: p.interrupted, penalty: p.penalty }
        : p.reply_to
          ? { id: ++noticeSeq, kind: "reply", by: p.candidate_label, target: p.reply_to, penalty: p.penalty }
          : null;
      return {
        ...prev,
        events,
        penalties,
        floor,
        counted,
        notices: notice ? [notice, ...prev.notices].slice(0, 6) : prev.notices,
      };
    }
    case "ERROR":
      return { ...prev, events, error: (frame.payload.message as string) ?? "error" };
    default:
      // USER_JOINED / USER_LEFT / USER_READY / DISCONNECTED / RECONNECTED all
      // change the roster; the lobby view is refetched by the page rather than
      // patched here, so one source of truth stays in charge of it.
      return { ...prev, events };
  }
}

/** Format seconds as mm:ss. */
export function formatClock(seconds: number): string {
  const safe = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(safe / 60)).padStart(2, "0")}:${String(safe % 60).padStart(2, "0")}`;
}
