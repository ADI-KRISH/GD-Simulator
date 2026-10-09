"use client";

/**
 * The lobby (FR-04 – FR-07).
 *
 * The roster is refetched whenever the control socket reports a membership
 * change, rather than being patched from the event payload. One source of truth
 * for who is in the room means the screen cannot drift out of step with the
 * server, which is the whole point of the server owning session state.
 */

import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { AgentsPanel } from "@/app/agents-panel";
import { Icon } from "@/app/icons";
import { Skeleton } from "@/app/motion";
import { hueFor } from "@/app/immersive/parts";
import { ApiError, lobbies, sessions } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { useSessionSocket } from "@/lib/use-session-socket";
import type { GDSession, LobbyView } from "@/lib/types";

const ROSTER_EVENTS = new Set([
  "USER_JOINED",
  "USER_LEFT",
  "USER_READY",
  "DISCONNECTED",
  "RECONNECTED",
]);

export default function LobbyPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { user } = useAuth();

  const [lobby, setLobby] = useState<LobbyView | null>(null);
  const [session, setSession] = useState<GDSession | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteStatus, setInviteStatus] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const socket = useSessionSocket(id ?? null);
  const latest = socket.events[0];

  const load = useCallback(async () => {
    try {
      setLobby(await lobbies.get(id));
      setSession(await sessions.get(id));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "could not load the lobby");
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  // Refetch on any roster-changing event.
  useEffect(() => {
    if (latest && ROSTER_EVENTS.has(latest.type)) void load();
  }, [latest, load]);

  // The host starting the session moves everyone into the room together.
  useEffect(() => {
    const state = socket.sessionState ?? session?.state;
    if (state === "STARTING" || state === "LIVE") router.push(`/room/${id}`);
  }, [socket.sessionState, session?.state, id, router]);

  const me = lobby?.participants.find((p) => p.user_id === user?.id);
  const isHost = session != null && user != null && session.host_id === user.id;

  // The host is not a candidate by default (FR-05: candidates join with the
  // code). A host who wants to take part joins the same way anyone else does.
  async function joinAsCandidate() {
    setBusy(true);
    setError(null);
    try {
      await lobbies.join(lobby!.code);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "could not join");
    } finally {
      setBusy(false);
    }
  }

  async function toggleReady() {
    if (!me) return;
    setBusy(true);
    try {
      await lobbies.setReady(id, !me.is_ready);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "could not update readiness");
    } finally {
      setBusy(false);
    }
  }

  async function startSession() {
    setBusy(true);
    setError(null);
    try {
      await sessions.start(id);
      router.push(`/room/${id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "could not start");
      setBusy(false);
    }
  }

  async function sendInvite(event: React.FormEvent) {
    event.preventDefault();
    setInviteStatus(null);
    try {
      await lobbies.invite(id, inviteEmail);
      setInviteStatus(`Invited ${inviteEmail}.`);
      setInviteEmail("");
      await load();
    } catch (err) {
      setInviteStatus(err instanceof ApiError ? err.message : "could not send the invitation");
    }
  }

  async function copyCode() {
    try {
      await navigator.clipboard.writeText(lobby?.code ?? "");
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      // Clipboard access can be refused; the code is on screen to read out.
    }
  }

  async function leave() {
    try {
      await lobbies.leave(id);
    } finally {
      router.push("/");
    }
  }

  if (!lobby || !session) {
    return error ? <p className="empty">{error}</p> : <Skeleton rows={2} tall />;
  }

  const minutes = Math.round(session.duration_seconds / 60);
  const statusOf = (p: (typeof lobby.participants)[number]) =>
    p.state === "DISCONNECTED"
      ? { text: "Disconnected", cls: "dot--away" }
      : p.is_ready
        ? { text: "Ready", cls: "dot--ok" }
        : { text: "Joined", cls: "dot--info" };
  const empty = Math.max(0, Math.min(lobby.capacity, 12) - lobby.participants.length);

  return (
    <div className="stack" style={{ gap: "1.25rem" }}>
      <header className="page-head page-head--back">
        <button type="button" className="back" onClick={leave} aria-label="Leave the lobby">
          <Icon name="arrow-left" />
        </button>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h1>
            Lobby: <span className="lobby-code">{lobby.code}</span>
          </h1>
          <p>Share this code with your friends to join.</p>
        </div>
        <button type="button" className="secondary" onClick={copyCode}>
          <Icon name={copied ? "check" : "copy"} size={16} /> {copied ? "Copied" : "Copy code"}
        </button>
      </header>

      {error && <div className="alert alert--error">{error}</div>}
      {socket.error && <div className="alert alert--error">Control channel: {socket.error}</div>}

      <section className="card">
        <p className="topic-box topic-box--lg">{session.topic}</p>
        <ul className="chips-row">
          <li>
            <Icon name="clock" size={16} /> {minutes} minutes
            {session.preparation_seconds > 0 &&
              `, plus ${Math.round(session.preparation_seconds / 60)} to prepare`}
          </li>
          <li>
            <Icon name="users" size={16} /> {lobby.occupancy} of {lobby.capacity} joined
          </li>
          <li>
            <Icon name="lock" size={16} /> Friends join with a code
          </li>
          <li className="cap">
            <Icon name="target" size={16} /> {session.gd_type.replace("_", " ")}, {session.difficulty}
          </li>
        </ul>
      </section>

      <section>
        <div className="row row--between" style={{ marginBottom: "0.7rem" }}>
          <h2 className="section-title" style={{ margin: 0 }}>
            Participants
          </h2>
          <span className={`pill ${socket.connected ? "pill--live" : ""}`}>
            {socket.connected ? "connected" : "reconnecting"}
          </span>
        </div>

        <div className="seat-grid">
          {lobby.participants.map((participant) => {
            const status = statusOf(participant);
            const isAI = participant.display_name.endsWith("(AI)");
            const name = participant.display_name.replace(/\s*\(AI\)$/, "");
            return (
              <div
                key={participant.id}
                className={`seat-card ${participant.state === "DISCONNECTED" ? "seat-card--away" : ""}`}
              >
                <span
                  className="seat-avatar"
                  style={{ "--h": hueFor(participant.candidate_label) } as React.CSSProperties}
                >
                  {isAI ? <Icon name="bot" size={26} /> : name.slice(0, 1).toUpperCase()}
                </span>
                <strong>
                  {name}
                  {participant.user_id === user?.id && <span className="you"> (you)</span>}
                </strong>
                <span className="seat-status">
                  <i className={`dot ${status.cls}`} />
                  {status.text}
                  {isAI && <span className="badge">AI</span>}
                </span>
              </div>
            );
          })}
          {Array.from({ length: empty }, (_, i) => (
            <div key={`empty-${i}`} className="seat-card seat-card--empty">
              <span className="seat-avatar seat-avatar--empty">
                <Icon name="user-plus" size={24} />
              </span>
              <span className="meta">Waiting for participant</span>
            </div>
          ))}
        </div>

        <p className="footnote" style={{ marginTop: "0.9rem" }}>
          {lobby.ready_count} of {lobby.occupancy} ready. Needs at least {lobby.min_participants}{" "}
          participants, all ready, before it can start. An invited candidate who has not arrived
          does not block the start.
        </p>
      </section>

      <section className="card">
        <div className="row row--wrap">
          {me && (
            <button type="button" className="secondary" onClick={toggleReady} disabled={busy}>
              {me.is_ready ? "Not ready" : "I'm ready"}
            </button>
          )}
          {!me && isHost && (
            <button type="button" className="secondary" onClick={joinAsCandidate} disabled={busy}>
              Join as a candidate
            </button>
          )}
          {me && (
            <button type="button" className="secondary" onClick={leave}>
              Leave
            </button>
          )}
        </div>
        {isHost && (
          <button
            type="button"
            className="button-lg start-btn"
            onClick={startSession}
            disabled={busy || !lobby.start_condition_met}
            title={lobby.start_condition_met ? "Start the discussion" : "Everyone must be ready first"}
          >
            Start discussion
          </button>
        )}
        {isHost && !me && (
          <p className="footnote">
            You are hosting: you can start and end this discussion and watch it, but you are not a
            candidate in it, so you are not recorded or evaluated. Join as a candidate to take
            part.
          </p>
        )}
        {isHost && !lobby.start_condition_met && (
          <p className="footnote">
            Waiting for everyone to be ready. Share the code <code>{lobby.code}</code> with the
            candidates who have not joined.
          </p>
        )}
      </section>

      {isHost && (
        <div className="lobby-extras">
          <section className="card">
            <h2 className="section-title">Invite by email</h2>
            <p className="meta" style={{ margin: "0 0 0.7rem" }}>
              Reserves a seat and notifies them. They still join with the code; the invitation is
              a reservation, not a separate way in.
            </p>
            {inviteStatus && <div className="alert alert--ok">{inviteStatus}</div>}
            <form onSubmit={sendInvite} className="row row--wrap">
              <input
                type="email"
                aria-label="Candidate email"
                placeholder="friend@example.com"
                value={inviteEmail}
                onChange={(e) => setInviteEmail(e.target.value)}
                style={{ maxWidth: "20rem" }}
                required
              />
              <button type="submit" className="secondary">
                Send invitation
              </button>
            </form>
          </section>

          <AgentsPanel
            sessionId={id}
            seatsLeft={lobby.capacity - lobby.occupancy}
            onAdded={() => void load()}
          />
        </div>
      )}
    </div>
  );
}
