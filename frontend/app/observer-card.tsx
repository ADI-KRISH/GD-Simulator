"use client";

/**
 * The Observer Agent's rolling understanding, shown live (design §14.3, §16).
 *
 * This is the background analysis that runs while the discussion is going on.
 * It holds observations only — the contract has no field for a score, so
 * nothing here can be read as a verdict.
 */

import { useEffect, useState } from "react";  // state and polling effect

import { sessions } from "@/lib/api";  // fetch the Observer state
import type { RollingState } from "@/lib/types";  // Observer state type

// The Observer folds in one closed 45-second window at a time.
const POLL_MS = 10_000;

/** Live card showing the Observer's notes, refreshed every 10 seconds. */
export function ObserverCard({ sessionId }: { sessionId: string }) {
  const [state, setState] = useState<RollingState | null>(null);

  useEffect(() => {
    /** Fetch the latest Observer state (failures are ignored: display only). */
    const load = () =>
      sessions
        .observer(sessionId)
        .then(setState)
        .catch(() => {}); // Display-only.
    void load();
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [sessionId]);

  // True until the Observer has produced anything worth showing.
  const empty =
    !state ||
    (!state.discussion_direction &&
      state.main_arguments.length === 0 &&
      Object.keys(state.candidate_positions).length === 0);

  return (
    <div className="card">
      <div className="row row--between" style={{ marginBottom: "0.5rem" }}>
        <h2 style={{ fontSize: "1.05rem", margin: 0 }}>Observer agent · live analysis</h2>
        <span className="pill pill--live">background</span>
      </div>
      {empty ? (
        <p className="empty">
          The Observer reads each 45-second window once it closes. Its first notes appear about a
          minute into the discussion.
        </p>
      ) : (
        <div className="stack" style={{ gap: "0.7rem", fontSize: "0.9rem" }}>
          {state.discussion_direction && (
            <div>
              <span className="meta">Direction</span>
              <div>{state.discussion_direction}</div>
            </div>
          )}
          {Object.keys(state.candidate_positions).length > 0 && (
            <div>
              <span className="meta">Positions</span>
              {Object.entries(state.candidate_positions).map(([label, position]) => (
                <div key={label}>
                  <strong>{label}</strong> — {position}
                </div>
              ))}
            </div>
          )}
          <Section title="Main arguments" items={state.main_arguments} />
          <Section title="Recent interactions" items={state.recent_interactions} />
          <Section title="Observations" items={state.observations ?? []} />
          <Section title="Unresolved" items={state.unresolved_questions} />
        </div>
      )}
      <p className="footnote">
        Observations only — the Observer never scores. Scoring happens once, after the discussion,
        with the full transcript.
      </p>
    </div>
  );
}

/** A titled list of the latest five items (hidden when empty). */
function Section({ title, items }: { title: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <div>
      <span className="meta">{title}</span>
      <ul style={{ margin: "0.2rem 0 0", paddingLeft: "1.1rem" }}>
        {items.slice(-5).map((item, index) => (
          <li key={index}>{item}</li>
        ))}
      </ul>
    </div>
  );
}
