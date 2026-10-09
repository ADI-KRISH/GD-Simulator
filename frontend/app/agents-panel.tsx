"use client";

/**
 * Demo affordance: seat LLM-played candidates in the lobby.
 *
 * Each agent joins as an ordinary, ready participant. Once the host starts the
 * discussion they take turns speaking until the clock runs out; the Observer,
 * metrics and final evaluation then treat them exactly like people.
 */

import { useState } from "react";

import { Select } from "@/app/select";

import { ApiError, sessions } from "@/lib/api";
import type { AgentSpec } from "@/lib/types";

const DEFAULT_AGENTS: AgentSpec[] = [
  {
    name: "Arjun",
    talkativeness: "high",
    persona:
      "Dominates the discussion. Speaks in long, confident monologues, repeats his own " +
      "position, makes sweeping claims without evidence, and rarely acknowledges others.",
  },
  {
    name: "Bhavana",
    talkativeness: "medium",
    persona:
      "A facilitator. Builds on others' points by name, summarises where the group " +
      "stands, weighs trade-offs, and invites quieter people in with direct questions.",
  },
  {
    name: "Chitra",
    talkativeness: "low",
    persona:
      "Quiet but knowledgeable. Speaks rarely and briefly — one or two sentences — but " +
      "each time adds a specific, well-reasoned point nobody else has made.",
  },
];

const BLANK: AgentSpec = { name: "", persona: "", talkativeness: "medium" };

export function AgentsPanel({
  sessionId,
  seatsLeft,
  onAdded,
}: {
  sessionId: string;
  seatsLeft: number;
  onAdded: () => void;
}) {
  const [agents, setAgents] = useState<AgentSpec[]>(DEFAULT_AGENTS);
  const [extraPrompt, setExtraPrompt] = useState(
    "Keep each turn under 60 words. Speak like a final-year engineering student in India.",
  );
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<{ ok: boolean; text: string } | null>(null);

  const update = (index: number, patch: Partial<AgentSpec>) =>
    setAgents((prev) => prev.map((agent, i) => (i === index ? { ...agent, ...patch } : agent)));

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setStatus(null);
    try {
      await sessions.addAgents(sessionId, {
        agents,
        extra_prompt: extraPrompt,
      });
      setStatus({
        ok: true,
        text: `${agents.length} agent${agents.length === 1 ? "" : "s"} joined and ready. Start the discussion when you are.`,
      });
      setAgents([]);
      onAdded();
    } catch (err) {
      const detail = err instanceof ApiError || err instanceof Error ? err.message : String(err);
      setStatus({ ok: false, text: `Could not add agents: ${detail}` });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <h2 style={{ fontSize: "1.05rem", margin: "0 0 0.5rem" }}>AI agents (demo)</h2>
      <p className="subtitle">
        Seat AI-played candidates with their own personas. They discuss the topic live until the
        timer ends; the Observer analyses them in the background and the full evaluation runs
        afterwards — exactly as for real candidates.
      </p>
      {status && (
        <div className={`alert ${status.ok ? "alert--ok" : "alert--error"}`}>{status.text}</div>
      )}

      {agents.length > 0 && (
        <form onSubmit={submit} className="stack" style={{ gap: "0.8rem" }}>
          {agents.map((agent, index) => (
            <div key={index} className="participant stack" style={{ gap: "0.5rem" }}>
              <div className="row row--wrap">
                <input
                  aria-label={`Agent ${index + 1} name`}
                  placeholder="Name"
                  value={agent.name}
                  onChange={(e) => update(index, { name: e.target.value })}
                  style={{ maxWidth: "12rem" }}
                  required
                />
                <div style={{ width: "11.5rem" }}>
                  <Select<AgentSpec["talkativeness"]>
                    aria-label={`Agent ${index + 1} talkativeness`}
                    value={agent.talkativeness}
                    onChange={(talkativeness) => update(index, { talkativeness })}
                    options={[
                      { value: "low", label: "Speaks rarely" },
                      { value: "medium", label: "Speaks sometimes" },
                      { value: "high", label: "Speaks a lot" },
                    ]}
                  />
                </div>
                <button
                  type="button"
                  className="secondary"
                  onClick={() => setAgents((prev) => prev.filter((_, i) => i !== index))}
                >
                  Remove
                </button>
              </div>
              <textarea
                aria-label={`Agent ${index + 1} persona`}
                placeholder="Persona — how this candidate behaves in a discussion"
                value={agent.persona}
                onChange={(e) => update(index, { persona: e.target.value })}
                rows={2}
                required
              />
            </div>
          ))}

          <label className="stack" style={{ gap: "0.3rem" }}>
            <span className="meta">Extra instructions for every agent</span>
            <textarea
              value={extraPrompt}
              onChange={(e) => setExtraPrompt(e.target.value)}
              rows={2}
              placeholder="e.g. Keep turns short. Disagree with at least one person."
            />
          </label>

          <div className="row row--wrap">
            <button
              type="button"
              className="secondary"
              onClick={() => setAgents((prev) => [...prev, { ...BLANK }])}
              disabled={agents.length >= seatsLeft}
            >
              Add another agent
            </button>
            <button type="submit" disabled={busy || agents.length > seatsLeft}>
              {busy
                ? "Seating agents…"
                : `Seat ${agents.length} agent${agents.length === 1 ? "" : "s"}`}
            </button>
          </div>
          {agents.length > seatsLeft && (
            <p className="footnote">
              Only {seatsLeft} seat{seatsLeft === 1 ? "" : "s"} left — remove an agent or raise the
              session capacity.
            </p>
          )}
        </form>
      )}
    </div>
  );
}
