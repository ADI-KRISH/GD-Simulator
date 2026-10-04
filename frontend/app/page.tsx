"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ApiError, lobbies, sessions } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import type { SessionSummary } from "@/lib/types";

/** Where a session in this state should take you. */
function destination(session: SessionSummary): string {
  switch (session.state) {
    case "STARTING":
    case "LIVE":
      return `/room/${session.id}`;
    case "ENDED":
    case "EVALUATING":
    case "COMPLETED":
      return `/report/${session.id}`;
    default:
      return `/lobby/${session.id}`;
  }
}

const FEATURES = [
  {
    icon: "🎙️",
    title: "Live group discussions",
    text: "Candidates join with a code and discuss over audio and video, with a shared timer and topic released at the same moment for everyone.",
  },
  {
    icon: "👁️",
    title: "An Observer that never scores",
    text: "While people talk, an Observer agent keeps running notes on arguments, positions and dynamics — observations only, never verdicts.",
  },
  {
    icon: "⚖️",
    title: "Evidence-backed evaluation",
    text: "Specialist agents score content, communication, leadership, collaboration and behaviour, each citing the transcript. The final number is computed in code.",
  },
];

export default function HomePage() {
  const router = useRouter();
  const { user, loading } = useAuth();
  const [code, setCode] = useState("");
  const [joining, setJoining] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mine, setMine] = useState<SessionSummary[]>([]);
  const [hosted, setHosted] = useState<SessionSummary[]>([]);

  const load = useCallback(async () => {
    if (!user) return;
    try {
      setMine(await sessions.list("participant"));
      if (user.role !== "candidate") setHosted(await sessions.list("host"));
    } catch {
      // A listing failure is not worth blocking the page for.
    }
  }, [user]);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleJoin(event: React.FormEvent) {
    event.preventDefault();
    setJoining(true);
    setError(null);
    try {
      const lobby = await lobbies.join(code);
      router.push(`/lobby/${lobby.session_id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "could not join");
      setJoining(false);
    }
  }

  if (loading) return <p className="empty">Loading…</p>;

  if (!user) {
    return (
      <div className="stack fade-in" style={{ gap: "2rem" }}>
        <section className="hero">
          <span className="eyebrow">Agentic AI evaluation</span>
          <h1>
            Group discussions, <span className="gradient-text">judged on evidence.</span>
          </h1>
          <p>
            Run real-time GDs and get every candidate evaluated on what they said and how they
            behaved in the group — with every score traceable to a transcript timestamp.
          </p>
          <div className="row" style={{ justifyContent: "center" }}>
            <Link href="/register">
              <button type="button" className="button-lg">
                Get started
              </button>
            </Link>
            <Link href="/login">
              <button type="button" className="secondary button-lg">
                Sign in
              </button>
            </Link>
          </div>
        </section>

        <section className="features">
          {FEATURES.map((feature) => (
            <div key={feature.title} className="card feature">
              <div className="feature-icon" aria-hidden>
                {feature.icon}
              </div>
              <h3>{feature.title}</h3>
              <p>{feature.text}</p>
            </div>
          ))}
        </section>
      </div>
    );
  }

  const isHostRole = user.role !== "candidate";

  return (
    <div className="stack fade-in">
      <div className="card">
        <div className="dashboard-hero">
          <div>
            <span className="eyebrow">{user.role}</span>
            <h1 style={{ marginTop: "0.7rem" }}>
              Welcome back, <span className="gradient-text">{user.full_name.split(" ")[0]}</span>
            </h1>
            <p className="subtitle" style={{ marginBottom: 0 }}>
              {isHostRole
                ? "Create a discussion, seat AI agents for a demo, or join one with a code."
                : "Join a discussion with the code your host gave you."}
            </p>
          </div>
          <form onSubmit={handleJoin}>
            <label htmlFor="join-code">Have a code?</label>
            <div className="join-box">
              <input
                id="join-code"
                placeholder="ABC123"
                value={code}
                onChange={(e) => setCode(e.target.value.toUpperCase())}
                maxLength={12}
                autoComplete="off"
                required
              />
              <button type="submit" disabled={joining || code.length < 4}>
                {joining ? "Joining…" : "Join"}
              </button>
            </div>
            {error && (
              <div className="alert alert--error" style={{ margin: "0.6rem 0 0" }}>
                {error}
              </div>
            )}
          </form>
        </div>
      </div>

      <div className="actions">
        {isHostRole && (
          <Link href="/sessions/new" className="action">
            <span className="feature-icon" aria-hidden>
              ➕
            </span>
            <span>
              <strong>New discussion</strong>
              <span className="meta" style={{ display: "block" }}>
                Topic, timer and AI agents
              </span>
            </span>
          </Link>
        )}
        <Link href="/history" className="action">
          <span className="feature-icon" aria-hidden>
            📈
          </span>
          <span>
            <strong>My performance</strong>
            <span className="meta" style={{ display: "block" }}>
              Scores across every GD
            </span>
          </span>
        </Link>
        <Link href="/profile" className="action">
          <span className="feature-icon" aria-hidden>
            👤
          </span>
          <span>
            <strong>Profile</strong>
            <span className="meta" style={{ display: "block" }}>
              Your details and goals
            </span>
          </span>
        </Link>
      </div>

      {isHostRole && <SessionList title="Discussions you host" items={hosted} hostView />}
      <SessionList title="Discussions you have joined" items={mine} />
    </div>
  );
}

function SessionList({
  title,
  items,
  hostView = false,
}: {
  title: string;
  items: SessionSummary[];
  hostView?: boolean;
}) {
  return (
    <div className="card">
      <div className="row row--between" style={{ marginBottom: "0.9rem" }}>
        <h2 className="section-title">{title}</h2>
        <span className="pill">{items.length}</span>
      </div>

      {items.length === 0 ? (
        <p className="empty">
          {hostView ? "You have not created any discussions yet." : "Nothing here yet."}
        </p>
      ) : (
        <div className="session-grid">
          {items.map((session) => (
            <Link key={session.id} href={destination(session)} className="session-card">
              <div className="row row--between">
                <span className={`pill pill--state-${session.state}`}>
                  {session.state.toLowerCase()}
                </span>
                <code>{session.code}</code>
              </div>
              <span className="session-card-topic">{session.topic}</span>
              <span className="meta">Open →</span>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
