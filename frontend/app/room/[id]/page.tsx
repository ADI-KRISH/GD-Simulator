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

import { ObserverCard } from "@/app/observer-card";
import { TranscriptCard } from "@/app/transcript-card";
import { ApiError, sessions } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { useMediaRoom } from "@/lib/use-media-room";
import { formatClock, useSessionSocket } from "@/lib/use-session-socket";
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

  // Each candidate's most recent line, shown as a caption on their tile.
  const lastLine = new Map<string, string>();
  for (const line of transcript?.lines ?? []) lastLine.set(line.candidate_label, line.text);
  const hasVideo = !observing || media.remotes.length > 0;
  const progress =
    timer && timer.duration_seconds > 0
      ? Math.max(0, Math.min(100, (timer.seconds_remaining / timer.duration_seconds) * 100))
      : 0;

  return (
    <div className="stack">
      <div className="card">
        <div className="row row--between row--wrap">
          <div style={{ flex: "1 1 20rem" }}>
            <span className={`pill ${isLive ? "pill--live" : ""}`}>
              {phase === "preparation" ? "preparation" : isLive ? "live" : phase}
            </span>
            <h1 style={{ marginTop: "0.5rem" }}>{topic}</h1>
            <p className="subtitle" style={{ marginBottom: 0 }}>
              {observing ? (
                <>You are hosting: watching, not recorded or evaluated.</>
              ) : (
                <>
                  You are candidate <strong>{socket.you ?? "—"}</strong>. Everything you say is
                  transcribed and evaluated after the discussion ends.
                </>
              )}
            </p>
          </div>
          <div className="row" style={{ gap: "1.2rem" }}>
            <div style={{ textAlign: "right" }}>
              <div className={`clock ${timer?.warning ? "clock--warn" : ""}`}>
                {formatClock(timer?.seconds_remaining ?? 0)}
              </div>
              <p className="meta" style={{ margin: 0 }}>
                {phase === "preparation" ? "until the discussion starts" : "remaining"}
              </p>
            </div>
            {isHost && (
              <button type="button" className="danger" onClick={endSession} disabled={ending}>
                {ending ? "Ending…" : "End discussion"}
              </button>
            )}
          </div>
        </div>
        <div className={`timer-bar ${timer?.warning ? "timer-bar--warn" : ""}`}>
          <span style={{ width: `${progress}%` }} />
        </div>
      </div>

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

      <div className="split">
        <div className="stack">
          <div className="card">
            <div className="row row--between" style={{ marginBottom: "0.9rem" }}>
              <h2 className="card-title">Discussion</h2>
              <span className="meta">
                {roster.length} candidate{roster.length === 1 ? "" : "s"}
                {socket.speaking.length > 0 && ` · ${socket.speaking.join(", ")} speaking`}
              </span>
            </div>
            <div className="stage">
              {roster.map((participant) => {
                const speaking =
                  socket.speaking.includes(participant.candidate_label) ||
                  media.activeSpeakers.includes(participant.candidate_label);
                const isAI = participant.display_name.endsWith("(AI)");
                const name = participant.display_name.replace(/\s*\(AI\)$/, "");
                const said = lastLine.get(participant.candidate_label);
                return (
                  <div
                    key={participant.id}
                    className={`tile ${speaking ? "tile--speaking" : ""} ${
                      participant.state === "DISCONNECTED" ? "tile--away" : ""
                    }`}
                  >
                    <div className="tile-head">
                      <span className="avatar avatar--lg">{participant.candidate_label}</span>
                      <div style={{ minWidth: 0 }}>
                        <div className="tile-name">
                          {name} {isAI && <span className="badge">AI</span>}
                        </div>
                        <div className="meta">
                          {speaking ? (
                            <span className="row" style={{ gap: "0.4rem", color: "var(--ok)" }}>
                              <span className="speaking-dots" aria-hidden>
                                <span />
                                <span />
                                <span />
                              </span>
                              speaking
                            </span>
                          ) : participant.state === "DISCONNECTED" ? (
                            "away"
                          ) : (
                            "listening"
                          )}
                        </div>
                      </div>
                    </div>
                    <div className="caption">
                      {said ? `“${said}”` : isLive ? "Has not spoken yet." : "Waiting to start."}
                    </div>
                  </div>
                );
              })}
            </div>

            {hasVideo && (
              <>
                <div
                  className="row row--wrap"
                  style={{ marginTop: "1rem", alignItems: "flex-start" }}
                >
                  {!observing && (
                    <div style={{ position: "relative", width: 220 }}>
                      <video
                        ref={media.localVideoRef}
                        className="remote-video"
                        autoPlay
                        muted
                        playsInline
                        style={{
                          transform: "scaleX(-1)",
                          opacity: media.cameraEnabled ? 1 : 0.15,
                        }}
                      />
                      <span
                        className="meta"
                        style={{
                          position: "absolute",
                          left: 8,
                          bottom: 6,
                          color: "#fff",
                        }}
                      >
                        You{media.micEnabled ? "" : " · muted"}
                      </span>
                    </div>
                  )}
                  <div id="remote-media" style={{ flex: 1 }} />
                </div>
                {!observing && (
                  <div className="row row--wrap" style={{ marginTop: "0.8rem" }}>
                    <button
                      type="button"
                      className="secondary"
                      onClick={media.toggleMic}
                      disabled={!media.connected}
                      aria-pressed={!media.micEnabled}
                    >
                      {media.micEnabled ? "🎤 Audio on" : "🔇 Audio off"}
                    </button>
                    <button
                      type="button"
                      className="secondary"
                      onClick={media.toggleCamera}
                      disabled={!media.connected}
                      aria-pressed={!media.cameraEnabled}
                    >
                      {media.cameraEnabled ? "📷 Video on" : "🚫 Video off"}
                    </button>
                    <span className="meta">
                      {media.connected
                        ? `media connected · ${media.uploadedChunks} audio window${
                            media.uploadedChunks === 1 ? "" : "s"
                          } sent`
                        : media.connecting
                          ? "connecting…"
                          : "media offline"}
                    </span>
                  </div>
                )}
              </>
            )}
          </div>

          {isLive && transcript && <TranscriptCard transcript={transcript} live />}
        </div>

        <div className="stack">{isLive && <ObserverCard sessionId={id} />}</div>
      </div>
    </div>
  );
}
