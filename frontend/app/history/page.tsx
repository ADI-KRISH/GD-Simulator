"use client";

/**
 * FR-31 — historical analysis.
 *
 * Per-dimension averages across every completed discussion, which is the input
 * the personalization layer in design §53 would build on. That layer itself is
 * out of MVP scope, so this shows the data rather than pretending to coach.
 */

import Link from "next/link";
import { useEffect, useState } from "react";

import { ApiError, evaluation } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import type { HistoryOut } from "@/lib/types";

const DIMENSION_LABELS: Record<string, string> = {
  content: "Content",
  communication: "Communication",
  leadership: "Leadership",
  collaboration: "Collaboration",
  critical_thinking: "Critical thinking",
  behavior: "Behaviour",
};

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
  if (!history) return <p className="empty">Loading your history…</p>;

  const dimensions = Object.entries(history.trend).sort(([, a], [, b]) => a - b);
  const weakest = dimensions[0];

  return (
    <div className="stack">
      <div className="card">
        <h1>Your performance</h1>
        <p className="subtitle">Across every discussion you have completed.</p>

        <div className="grid">
          <div>
            <dt>Discussions completed</dt>
            <dd style={{ fontSize: "1.6rem" }}>{history.sessions_completed}</dd>
          </div>
          <div>
            <dt>Average overall</dt>
            <dd style={{ fontSize: "1.6rem" }}>
              {history.average_overall !== null ? history.average_overall.toFixed(1) : "—"}
            </dd>
          </div>
          {weakest && (
            <div>
              <dt>Weakest dimension</dt>
              <dd style={{ fontSize: "1.6rem" }}>
                {DIMENSION_LABELS[weakest[0]] ?? weakest[0]}
              </dd>
            </div>
          )}
        </div>
      </div>

      {dimensions.length > 0 && (
        <div className="card">
          <h2 style={{ fontSize: "1.05rem", margin: "0 0 0.9rem" }}>Average by dimension</h2>
          {dimensions.map(([dimension, value]) => (
            <div key={dimension} style={{ marginBottom: "0.8rem" }}>
              <div className="row row--between">
                <span style={{ fontSize: "0.9rem" }}>
                  {DIMENSION_LABELS[dimension] ?? dimension}
                </span>
                <span className="meta">{value.toFixed(1)}/10</span>
              </div>
              <div className="bar" style={{ marginTop: "0.3rem" }}>
                <span style={{ width: `${Math.min(100, value * 10)}%` }} />
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="card">
        <h2 style={{ fontSize: "1.05rem", margin: "0 0 0.9rem" }}>Sessions</h2>
        {history.entries.length === 0 ? (
          <p className="empty">You have not completed any discussions yet.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Topic</th>
                <th>Score</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {history.entries.map((entry) => (
                <tr key={entry.session_id}>
                  <td>{entry.topic}</td>
                  <td>{entry.overall.toFixed(1)}</td>
                  <td style={{ textAlign: "right" }}>
                    <Link href={`/report/${entry.session_id}`}>Report</Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
