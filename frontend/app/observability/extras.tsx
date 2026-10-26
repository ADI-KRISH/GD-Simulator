"use client";

/** Alerts, the trace waterfall, and datasets/experiments for the observability page. */

import { useState } from "react";  // state

import { Skeleton } from "@/app/motion";  // loading placeholder
import { ApiError } from "@/lib/api";  // error type
import {
  observability,
  type Alert,
  type CaseRow,
  type RunResult,
  type RunSummary,
  type TreeSpan,
} from "@/lib/observability";  // dashboard client and shapes

import { clock, ms, usd, useLoad } from "./shared";

/** Problems in the last hour, as a banner. Renders nothing when all is well. */
export function Alerts({ tick }: { tick: number }) {
  const { data } = useLoad<Alert[]>(() => observability.alerts(), [tick]);
  if (!data || data.length === 0) return null;
  return (
    <div className="obs-alerts" role="status">
      {data.map((a) => (
        <div key={a.title} className={`obs-alert obs-alert--${a.level}`}>
          <strong>{a.title}</strong>
          <span>{a.detail}</span>
        </div>
      ))}
    </div>
  );
}

interface Node {
  span: TreeSpan;
  depth: number;
}

/** Depth-first order of the spans, children under their parent. */
function flatten(spans: TreeSpan[]): Node[] {
  const kids = new Map<string | null, TreeSpan[]>();
  const ids = new Set(spans.map((s) => s.id));
  for (const s of spans) {
    // A parent outside this session's rows is treated as a root.
    const key = s.parent_id && ids.has(s.parent_id) ? s.parent_id : null;
    kids.set(key, [...(kids.get(key) ?? []), s]);
  }
  const out: Node[] = [];
  const walk = (key: string | null, depth: number) => {
    for (const s of kids.get(key) ?? []) {
      out.push({ span: s, depth });
      walk(s.id, depth + 1);
    }
  };
  walk(null, 0);
  return out;
}

/** One session's calls laid out in time, nested under the turn or run that made them. */
export function Waterfall({ sessionId, onPick }: { sessionId: string; onPick: (id: string) => void }) {
  const { data, error } = useLoad<TreeSpan[]>(() => observability.trace(sessionId), [sessionId]);
  if (error) return <div className="alert alert--error">{error}</div>;
  if (!data) return <Skeleton rows={3} />;
  if (data.length === 0) return <p className="empty">No spans for this session.</p>;

  const start = Math.min(...data.map((s) => new Date(s.started_at).getTime()));
  const end = Math.max(...data.map((s) => new Date(s.started_at).getTime() + s.latency_ms));
  const total = Math.max(end - start, 1);
  return (
    <div className="card obs-table-card">
      <h3>Trace · {ms(total)} end to end</h3>
      <div className="obs-wf">
        {flatten(data).map(({ span: s, depth }) => {
          const left = ((new Date(s.started_at).getTime() - start) / total) * 100;
          const width = Math.max((s.latency_ms / total) * 100, 0.4);
          return (
            <div key={s.id} className="obs-wf-row" onClick={() => onPick(s.id)}>
              <div className="obs-wf-name" style={{ paddingLeft: `${depth * 14}px` }}>
                <span className={`obs-kind obs-kind--${s.kind}`}>{s.kind}</span>
                <span>{s.name || s.agent || s.purpose}</span>
              </div>
              <div className="obs-wf-track">
                <div
                  className={`obs-wf-bar obs-wf-bar--${s.status === "error" ? "error" : s.kind}`}
                  style={{ left: `${left}%`, width: `${Math.min(width, 100 - left)}%` }}
                  title={`${clock(s.started_at)} · ${ms(s.latency_ms)}`}
                />
              </div>
              <div className="obs-wf-meta">{ms(s.latency_ms)}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** Save the open call as a test case. */
export function SaveToDataset({ spanId }: { spanId: string }) {
  const [dataset, setDataset] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  async function save() {
    try {
      await observability.saveCase(spanId, dataset.trim());
      setMessage(`Saved to “${dataset.trim()}”.`);
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "could not save");
    }
  }
  return (
    <div className="obs-save">
      <input placeholder="Dataset name" value={dataset} onChange={(e) => setDataset(e.target.value)} />
      <button type="button" disabled={!dataset.trim()} onClick={save}>
        Save as test case
      </button>
      {message && <span className="meta">{message}</span>}
    </div>
  );
}

/** Percent, or a dash when nothing was comparable. */
const pct = (v: number | null) => (v === null ? "—" : `${(v * 100).toFixed(0)}%`);

/** Datasets: cases, run an experiment, compare runs. */
export function Datasets({ tick }: { tick: number }) {
  const [version, setVersion] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const { data, error } = useLoad(() => observability.datasets(), [tick, version]);
  if (error) return <div className="alert alert--error">{error}</div>;
  if (!data) return <Skeleton rows={3} />;
  return (
    <div className="stack" style={{ gap: "1.25rem" }}>
      <div className="card obs-table-card">
        <h3>Datasets</h3>
        {data.length === 0 ? (
          <p className="empty">
            No datasets yet. Open any model call in the Calls tab and choose “Save as test case”.
          </p>
        ) : (
          <div className="obs-scroll">
            <table className="obs-table">
              <thead>
                <tr><th>Dataset</th><th>Cases</th><th>Runs</th></tr>
              </thead>
              <tbody>
                {data.map((d) => (
                  <tr key={d.dataset} className="obs-row" onClick={() => setSelected(d.dataset)}>
                    <td>{d.dataset}</td>
                    <td>{d.cases}</td>
                    <td>{d.runs}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {selected && data.some((d) => d.dataset === selected) && (
        <DatasetView key={selected} name={selected} onChange={() => setVersion((v) => v + 1)} />
      )}
    </div>
  );
}

/** One dataset: its cases, the run form and past runs. */
function DatasetView({ name, onChange }: { name: string; onChange: () => void }) {
  const [version, setVersion] = useState(0);
  const [model, setModel] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [label, setLabel] = useState("");
  const [running, setRunning] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const cases = useLoad<CaseRow[]>(() => observability.cases(name), [name, version]);
  const runs = useLoad<RunSummary[]>(() => observability.runs(name), [name, version]);

  async function run() {
    setRunning(true);
    setMessage(null);
    try {
      await observability.runDataset(name, { model, system_prompt: systemPrompt, label });
      setVersion((v) => v + 1);
      onChange();
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "run failed");
    } finally {
      setRunning(false);
    }
  }

  async function remove(id: string) {
    await observability.deleteCase(id);
    setVersion((v) => v + 1);
    onChange();
  }

  return (
    <div className="stack" style={{ gap: "1.25rem" }}>
      <div className="card stack" style={{ gap: "0.75rem" }}>
        <h3>Run “{name}” again</h3>
        <p className="meta">
          Replays every case as one plain call. Leave the model and prompt empty to rerun as saved; fill them
          to compare. Tool lookups are not re-run.
        </p>
        <div className="obs-filters">
          <input placeholder="Label (e.g. gpt-oss-20b)" value={label} onChange={(e) => setLabel(e.target.value)} />
          <input placeholder="Model (optional)" value={model} onChange={(e) => setModel(e.target.value)} />
        </div>
        <textarea
          rows={3}
          placeholder="Replacement system prompt (optional)"
          value={systemPrompt}
          onChange={(e) => setSystemPrompt(e.target.value)}
        />
        <div>
          <button type="button" disabled={running} onClick={run}>
            {running ? "Running…" : "Run experiment"}
          </button>
        </div>
        {message && <div className="alert alert--error">{message}</div>}
      </div>

      <div className="card obs-table-card">
        <h3>Runs</h3>
        {!runs.data ? (
          <Skeleton rows={2} />
        ) : runs.data.length === 0 ? (
          <p className="empty">No runs yet.</p>
        ) : (
          <div className="obs-scroll">
            <table className="obs-table">
              <thead>
                <tr>
                  <th>Label</th><th>Model</th><th>Cases</th><th>Agreement</th><th>Errors</th>
                  <th>p50</th><th>p95</th><th>Cost</th>
                </tr>
              </thead>
              <tbody>
                {runs.data.map((r) => (
                  <tr key={r.run_id} className="obs-row" onClick={() => setOpen(r.run_id)}>
                    <td>{r.label || r.run_id.slice(0, 8)}</td>
                    <td className="obs-mono">{r.model}</td>
                    <td>{r.cases}</td>
                    <td>{pct(r.avg_match)}</td>
                    <td className={r.errors ? "obs-bad" : ""}>{r.errors}</td>
                    <td>{ms(r.p50_ms)}</td>
                    <td>{ms(r.p95_ms)}</td>
                    <td>{usd(r.cost_usd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {open && <RunDetail runId={open} />}

      <div className="card obs-table-card">
        <h3>Cases</h3>
        {!cases.data ? (
          <Skeleton rows={2} />
        ) : (
          <div className="obs-scroll">
            <table className="obs-table">
              <thead>
                <tr><th>Agent</th><th>Saved from</th><th>Input</th><th>Expected</th><th /></tr>
              </thead>
              <tbody>
                {cases.data.map((c) => (
                  <tr key={c.id}>
                    <td>{c.agent ?? c.purpose}</td>
                    <td className="obs-mono">{c.model}</td>
                    <td className="obs-preview">{c.prompt.slice(0, 120)}</td>
                    <td className="obs-preview obs-mono">{JSON.stringify(c.expected).slice(0, 120)}</td>
                    <td>
                      <button type="button" className="secondary" onClick={() => remove(c.id)}>
                        Delete
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

/** Each case in a run: what was expected against what came back. */
function RunDetail({ runId }: { runId: string }) {
  const { data, error } = useLoad<RunResult[]>(() => observability.runResults(runId), [runId]);
  if (error) return <div className="alert alert--error">{error}</div>;
  if (!data) return <Skeleton rows={2} />;
  return (
    <div className="card obs-table-card">
      <h3>Run {runId.slice(0, 8)} · case by case</h3>
      <div className="stack" style={{ gap: "0.75rem" }}>
        {data.map((r) => (
          <div key={r.case_id} className="obs-case">
            <div className="obs-case-head">
              <span className="obs-preview">{r.prompt.slice(0, 100)}</span>
              <span className={r.error ? "obs-bad" : r.match !== null && r.match < 0.7 ? "obs-bad" : "obs-ok"}>
                {r.error ? "error" : pct(r.match)} · {ms(r.latency_ms)}
              </span>
            </div>
            {r.error ? (
              <pre className="obs-pre">{r.error}</pre>
            ) : (
              <div className="obs-diff">
                <pre className="obs-pre">{JSON.stringify(r.expected, null, 2)}</pre>
                <pre className="obs-pre">{JSON.stringify(r.output, null, 2)}</pre>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
