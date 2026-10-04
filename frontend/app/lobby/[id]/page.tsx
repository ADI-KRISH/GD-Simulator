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

  async function leave() {
    try {
      await lobbies.leave(id);
    } finally {
      router.push("/");
    }
  }

  if (!lobby || !session) {
    return <p className="empty">{error ?? "Loading the lobby…"}</p>;
  }

  return (
    <div className="stack">
      <div className="card">
        <div className="row row--between row--wrap">
          <div>
            <h1>{session.topic}</h1>
            <p className="subtitle" style={{ marginBottom: 0 }}>
              {session.topic_category} · {session.difficulty} · {session.gd_type.replace("_", " ")}{" "}
              · {Math.round(session.duration_seconds / 60)} min
              {session.preparation_seconds > 0 &&
                ` (+${Math.round(session.preparation_seconds / 60)} min preparation)`}
            </p>
          </div>
          <div style={{ textAlign: "right" }}>
            <p className="meta" style={{ margin: "0 0 0.3rem" }}>
              Session code
            </p>
            <div className="code-badge">{lobby.code}</div>
          </div>
        </div>
      </div>

      {error && <div className="alert alert--error">{error}</div>}
      {socket.error && <div className="alert alert--error">Control channel: {socket.error}</div>}

      <div className="card">
        <div className="row row--between" style={{ marginBottom: "0.9rem" }}>
          <h2 style={{ fontSize: "1.05rem", margin: 0 }}>
            Participants {lobby.occupancy}/{lobby.capacity}
          </h2>
          <span className={`pill ${socket.connected ? "pill--live" : ""}`}>
            {socket.connected ? "connected" : "reconnecting"}
          </span>
        </div>

        <div className="participants">
          {lobby.participants.map((participant) => (
            <div
              key={participant.id}
              className={`participant ${
                participant.state === "DISCONNECTED" ? "participant--away" : ""
              }`}
            >
              <div className="row" style={{ marginBottom: "0.35rem" }}>
                <span className="avatar">{participant.candidate_label}</span>
                <strong style={{ fontSize: "0.92rem" }}>
                  {participant.display_name}
                  {participant.user_id === user?.id && " (you)"}
                </strong>
              </div>
              <span className={`pill ${participant.is_ready ? "pill--live" : ""}`}>
                {participant.state === "DISCONNECTED"
                  ? "disconnected"
                  : participant.is_ready
                    ? "ready"
                    : "waiting"}
              </span>
            </div>
          ))}
        </div>

        <p className="footnote">
          {lobby.ready_count} of {lobby.occupancy} ready. Needs at least {lobby.min_participants}{" "}
          participants, all ready, before it can start. An invited candidate who has not arrived
          does not block the start.
        </p>
      </div>

      {isHost && (
        <AgentsPanel
          sessionId={id}
          seatsLeft={lobby.capacity - lobby.occupancy}
          onAdded={() => void load()}
        />
      )}

      {isHost && (
        <div className="card">
          <h2 style={{ fontSize: "1.05rem", margin: "0 0 0.5rem" }}>Invite a candidate</h2>
          <p className="subtitle">
            Reserves a seat and notifies them. They still join with the code — the invitation is a
            reservation, not a separate way in.
          </p>
          {inviteStatus && <div className="alert alert--ok">{inviteStatus}</div>}
          <form onSubmit={sendInvite} className="row row--wrap">
            <input
              type="email"
              aria-label="Candidate email"
              placeholder="candidate@example.com"
              value={inviteEmail}
              onChange={(e) => setInviteEmail(e.target.value)}
              style={{ maxWidth: "20rem" }}
              required
            />
            <button type="submit" className="secondary">
              Send invitation
            </button>
          </form>
        </div>
      )}

      <div className="card">
        <div className="row row--wrap">
          {me && (
            <button type="button" onClick={toggleReady} disabled={busy}>
              {me.is_ready ? "Not ready" : "I'm ready"}
            </button>
          )}
          {!me && isHost && (
            <button type="button" onClick={joinAsCandidate} disabled={busy}>
              Join as a candidate
            </button>
          )}
          {isHost && (
            <button
              type="button"
              onClick={startSession}
              disabled={busy || !lobby.start_condition_met}
              title={
                lobby.start_condition_met
                  ? "Start the discussion"
                  : "Everyone must be ready first"
              }
            >
              Start discussion
            </button>
          )}
          {me && (
            <button type="button" className="secondary" onClick={leave}>
              Leave
            </button>
          )}
        </div>
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
      </div>
    </div>
  );
}
