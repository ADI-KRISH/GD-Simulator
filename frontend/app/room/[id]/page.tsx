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
import { useCallback, useEffect, useRef, useState } from "react";

import {
  ArenaHeader,
  hueFor,
  Notices,
  RoundTable,
  type SeatInfo,
} from "@/app/immersive/parts";
import { Icon, type IconName } from "@/app/icons";
import { ObserverCard } from "@/app/observer-card";
import { ApiError, sessions } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { useMediaRoom } from "@/lib/use-media-room";
import { useAgentVoicesMuted } from "@/lib/agent-audio";
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
  const [tab, setTab] = useState<"transcript" | "people" | "observer">("transcript");
  const [notes, setNotes] = useNotes(id);
  const feedRef = useRef<HTMLDivElement | null>(null);

  const socket = useSessionSocket(id ?? null);
  const [voicesMuted, setVoicesMuted] = useAgentVoicesMuted();

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

  // Keep the feed pinned to the newest line.
  const lineCount = transcript?.lines.length ?? 0;
  useEffect(() => {
    if (feedRef.current) feedRef.current.scrollTop = feedRef.current.scrollHeight;
  }, [lineCount, tab]);

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

      <div className="room-grid">
        <div className="room-main">
          <RoundTable
            seats={seats}
            centre={
              latest ? (
                <>
                  <span className="caption-who">{nameOf(latest.candidate_label)}</span>
                  <span className="caption-now">{latest.text}</span>
                </>
              ) : (
                <span>
                  {observing
                    ? "You are hosting: watching, not recorded or evaluated."
                    : isLive
                      ? "Nobody has spoken yet."
                      : "The discussion opens when preparation ends."}
                </span>
              )
            }
          />
          <Notices notices={socket.notices} nameOf={nameOf} />

          {error && <div className="alert alert--error">{error}</div>}
          {/* A watching host publishes nothing, so a missing media server (e.g. no
              LiveKit when running without Docker) is not their problem to see. */}
          {media.error && !observing && <div className="alert alert--error">Media: {media.error}</div>}
          {media.uploadError && !observing && (
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

          <div className="controls">
            {!observing && hasVideo && (
              <>
                <ControlButton
                  icon={media.micEnabled ? "mic" : "mic-off"}
                  label={media.micEnabled ? "Mute" : "Unmute"}
                  pressed={!media.micEnabled}
                  disabled={!media.connected}
                  onClick={media.toggleMic}
                />
                <ControlButton
                  icon={media.cameraEnabled ? "video" : "video-off"}
                  label={media.cameraEnabled ? "Stop video" : "Start video"}
                  pressed={!media.cameraEnabled}
                  disabled={!media.connected}
                  onClick={media.toggleCamera}
                />
              </>
            )}
            <ControlButton
              icon="users"
              label="Participants"
              pressed={tab === "people"}
              onClick={() => setTab("people")}
            />
            <ControlButton
              icon="chat"
              label="Transcript"
              pressed={tab === "transcript"}
              onClick={() => setTab("transcript")}
            />
            <ControlButton
              icon={voicesMuted ? "volume-off" : "volume"}
              label="AI voices"
              pressed={voicesMuted}
              title="Spoken lines from AI participants"
              onClick={() => setVoicesMuted(!voicesMuted)}
            />
          </div>
          {!observing && hasVideo && (
            <p className="arena-hint" style={{ margin: 0, textAlign: "center" }}>
              {media.connected
                ? `Connected. ${media.uploadedChunks} audio window${media.uploadedChunks === 1 ? "" : "s"} sent for transcription.`
                : media.connecting
                  ? "Connecting…"
                  : "Media offline."}
            </p>
          )}
        </div>

        <aside className="side-panel">
          <div className="tabs" role="tablist">
            {(
              [
                ["transcript", "Live transcript"],
                ["people", "Participants"],
                ["observer", "Observer"],
              ] as const
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={tab === key}
                className={`tab ${tab === key ? "tab--on" : ""}`}
                onClick={() => setTab(key)}
              >
                {label}
              </button>
            ))}
          </div>

          <div className="side-body" ref={feedRef}>
            {tab === "transcript" &&
              (lines.length === 0 ? (
                <p className="empty">
                  {isLive ? "Lines appear here as people speak." : "The transcript starts when the discussion does."}
                </p>
              ) : (
                <ul className="feed">
                  {lines.map((line, index) => {
                    const speaker = seats.find((x) => x.label === line.candidate_label);
                    return (
                      <li key={index}>
                        <span
                          className="feed-avatar"
                          style={{ "--h": hueFor(line.candidate_label) } as React.CSSProperties}
                        >
                          {nameOf(line.candidate_label).slice(0, 1).toUpperCase()}
                        </span>
                        <div>
                          <div className="feed-meta">
                            <strong>
                              {nameOf(line.candidate_label)}
                              {speaker?.you ? " (you)" : ""}
                            </strong>
                            <time>{line.timestamp}</time>
                          </div>
                          <p>{line.text}</p>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              ))}

            {tab === "people" && (
              <ul className="people">
                {seats.map((seat) => (
                  <li key={seat.label} className={seat.away ? "person--away" : ""}>
                    <span
                      className="feed-avatar"
                      style={{ "--h": hueFor(seat.label) } as React.CSSProperties}
                    >
                      {seat.name.slice(0, 1).toUpperCase()}
                    </span>
                    <span className="person-name">
                      {seat.name}
                      {seat.you && " (you)"}
                      {seat.badge && <span className="badge">{seat.badge}</span>}
                    </span>
                    <span className={`chip ${seat.speaking ? "chip--ok" : seat.away ? "chip--away" : "chip--info"}`}>
                      {seat.speaking ? "Speaking" : seat.away ? "Away" : "Listening"}
                    </span>
                  </li>
                ))}
              </ul>
            )}

            {tab === "observer" && <ObserverCard sessionId={id} />}
          </div>

          <label className="notes">
            <span className="meta">Your notes (only on this device)</span>
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="Jot down points to build on…"
              rows={3}
            />
          </label>
        </aside>
      </div>
    </div>
  );
}

function ControlButton({
  icon,
  label,
  pressed,
  disabled,
  title,
  onClick,
}: {
  icon: IconName;
  label: string;
  pressed?: boolean;
  disabled?: boolean;
  title?: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className="ctl-round"
      aria-pressed={pressed}
      disabled={disabled}
      title={title}
      onClick={onClick}
    >
      <span className="ctl-ico">
        <Icon name={icon} size={22} />
      </span>
      <span className="ctl-label">{label}</span>
    </button>
  );
}

/** Private notes for this discussion, kept in the browser and never sent anywhere. */
function useNotes(sessionId: string): [string, (value: string) => void] {
  const key = `gd-notes-${sessionId}`;
  const [value, setValue] = useState("");
  useEffect(() => {
    try {
      setValue(window.localStorage.getItem(key) ?? "");
    } catch {
      // Storage can be unavailable; notes then last only while the page is open.
    }
  }, [key]);
  const update = (next: string) => {
    setValue(next);
    try {
      window.localStorage.setItem(key, next);
    } catch {
      // Ignored for the same reason.
    }
  };
  return [value, update];
}
