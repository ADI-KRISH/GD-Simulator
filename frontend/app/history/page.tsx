"use client";

/**
 * FR-31 — historical analysis.
 *
 * Per-dimension averages across every completed discussion, which is the input
 * the personalization layer in design §53 would build on. That layer itself is
 * out of MVP scope, so this shows the data rather than pretending to coach.
 */

import Link from "next/link";  // client-side navigation link
import { useEffect, useState } from "react";  // state and effects

import { Icon } from "@/app/icons";  // icon set
import { AnimatedBar, CountUp, Skeleton } from "@/app/motion";  // animated bars and counters
import { ApiError, evaluation } from "@/lib/api";  // history call and error type
import { useAuth } from "@/lib/auth-context";  // require sign-in
import type { HistoryOut } from "@/lib/types";  // history response type

/** Display names for the scoring dimensions. */
const DIMENSION_LABELS: Record<string, string> = {
  content: "Content",
  communication: "Communication",
  leadership: "Leadership",
  collaboration: "Collaboration",
  critical_thinking: "Critical thinking",
  behavior: "Behaviour",
};

/** Past sessions: totals, average by skill and a link to each report. */
export default function HistoryPage() {
  const { user, loading } = useAuth();
  const [history, setHistory] = useState<HistoryOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!user) return;
    evaluation
      .history()
      .then(setHistory)
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "could not load your history"),
      );
  }, [user]);

  if (loading) return <p className="empty">Loading…</p>;
  if (!user) return <p className="empty">Sign in to see your performance.</p>;
  if (error) return <div className="alert alert--error">{error}</div>;
  if (!history) return <Skeleton rows={3} tall />;

  const dimensions = Object.entries(history.trend).sort(([, a], [, b]) => a - b);
  const weakest = dimensions[0];
  // Colour band for a 0-100 score.
  const tone = (score: number) => (score >= 70 ? "good" : score >= 50 ? "mid" : "low");

  return (
    <div className="stack" style={{ gap: "1.25rem" }}>
      <header className="page-head">
        <h1>Past sessions</h1>
        <p>View your previous group discussions and feedback.</p>
      </header>

      <div className="stat-row">
        <div className="card stat">
          <span className="tile-icon" aria-hidden>
            <Icon name="users" size={22} />
          </span>
          <div>
            <small>Discussions completed</small>
            <strong>
              <CountUp value={history.sessions_completed} />
            </strong>
          </div>
        </div>
        <div className="card stat">
          <span className="tile-icon tile-icon--teal" aria-hidden>
            <Icon name="chart" size={22} />
          </span>
          <div>
            <small>Average overall</small>
            <strong>
              {history.average_overall !== null ? (
                <CountUp value={history.average_overall} />
              ) : (
                "–"
              )}
            </strong>
          </div>
        </div>
        {weakest && (
          <div className="card stat">
            <span className="tile-icon tile-icon--amber" aria-hidden>
              <Icon name="target" size={22} />
            </span>
            <div>
              <small>Weakest skill</small>
              <strong>{DIMENSION_LABELS[weakest[0]] ?? weakest[0]}</strong>
            </div>
          </div>
        )}
      </div>

      {dimensions.length > 0 && (
        <section className="card">
          <h2 className="section-title">Average by skill</h2>
          {dimensions.map(([dimension, value]) => (
            <div key={dimension} className="score-row">
              <span>{DIMENSION_LABELS[dimension] ?? dimension}</span>
              <AnimatedBar value={value * 10} tone={tone(value * 10)} />
              <strong>{Math.round(value * 10)}</strong>
            </div>
          ))}
        </section>
      )}

      <section>
        <h2 className="section-title">Sessions</h2>
        {history.entries.length === 0 ? (
          <div className="card">
            <p className="empty">
              You have not completed any discussions yet. Start one from Group Discussion.
            </p>
          </div>
        ) : (
          <ul className="past-list">
            {history.entries.map((entry) => (
              <li key={entry.session_id} className="past-item">
                <span className="tile-icon" aria-hidden>
                  <Icon name="chat" size={20} />
                </span>
                <div className="past-main">
                  <strong>{entry.topic}</strong>
                  <small>
                    <Icon name="calendar" size={14} />{" "}
                    {new Date(entry.created_at).toLocaleDateString(undefined, {
                      day: "numeric",
                      month: "short",
                      year: "numeric",
                    })}
                  </small>
                </div>
                <span className={`score-pill score--${tone(entry.overall)}`}>
                  {Math.round(entry.overall)}
                </span>
                <Link href={`/report/${entry.session_id}`} className="view-link">
                  View report <Icon name="arrow-right" size={14} />
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
