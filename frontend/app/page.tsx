"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { Icon, type IconName } from "@/app/icons";
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

const FEATURES: { icon: IconName; title: string; text: string }[] = [
  {
    icon: "mic",
    title: "Live group discussions",
    text: "Candidates join with a code and discuss over audio and video, with a shared timer and topic released at the same moment for everyone.",
  },
  {
    icon: "eye",
    title: "An Observer that never scores",
    text: "While people talk, an Observer agent keeps running notes on arguments, positions and dynamics — observations only, never verdicts.",
  },
  {
    icon: "scale",
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
                <Icon name={feature.icon} size={22} />
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
  const hour = new Date().getHours();
  const greeting = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";

  return (
    <div className="stack fade-in" style={{ gap: "1.5rem" }}>
      <header className="page-head">
        <h1>
          {greeting}, {user.full_name.split(" ")[0]}
        </h1>
        <p>Improve your communication, critical thinking and teamwork with AI-powered evaluations.</p>
      </header>

      <section className="options" aria-label="Start a discussion">
        <article className="option">
          <span className="tile-icon tile-icon--lg" aria-hidden>
            <Icon name="users" size={26} />
          </span>
          <h2>{isHostRole ? "Create a lobby" : "Find a lobby"}</h2>
          <p>
            {isHostRole
              ? "Invite your friends and start a private discussion. Each participant gets individual feedback."
              : "Join a discussion another student opened. Each participant gets individual feedback."}
          </p>
          <Link href={isHostRole ? "/sessions/new" : "/lobbies"} className="option-cta">
            <button type="button">
              {isHostRole ? "Create lobby" : "Browse lobbies"} <Icon name="arrow-right" size={16} />
            </button>
          </Link>
        </article>

        <article className="option">
          <span className="tile-icon tile-icon--lg tile-icon--teal" aria-hidden>
            <Icon name="user-plus" size={26} />
          </span>
          <h2>Join a lobby</h2>
          <p>Enter the lobby code your host shared with you.</p>
          <form onSubmit={handleJoin} className="option-cta">
            <label htmlFor="join-code" className="sr-only">
              Lobby code
            </label>
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
        </article>

        <article className="option">
          <span className="tile-icon tile-icon--lg tile-icon--violet" aria-hidden>
            <Icon name="bot" size={26} />
          </span>
          <h2>Practice with AI</h2>
          <p>Talk it out loud with AI participants that push back, interrupt and keep you honest.</p>
          <Link href="/practice/new" className="option-cta">
            <button type="button" className="secondary">
              Start practice <Icon name="arrow-right" size={16} />
            </button>
          </Link>
        </article>
      </section>

      <section className="card">
        <h2 className="section-title">How it works</h2>
        <ol className="steps">
          <li>
            <span className="step-no">1</span>
            <span>Choose a topic or get a suggestion</span>
          </li>
          <li>
            <span className="step-no">2</span>
            <span>Discuss for 10 to 15 minutes</span>
          </li>
          <li>
            <span className="step-no">3</span>
            <span>AI evaluates each participant</span>
          </li>
          <li>
            <span className="step-no">4</span>
            <span>Get detailed personal feedback</span>
          </li>
        </ol>
      </section>

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
              <span className="meta session-open">Open <Icon name="arrow-right" size={14} /></span>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
