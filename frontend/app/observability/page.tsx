"use client";

/**
 * Observability: every model call, speech call and reply latency the system made,
 * with its prompt, output, tokens, cost and timing. Hosts and admins only.
 */

import { useCallback, useEffect, useState } from "react";  // state and effects

import { Skeleton } from "@/app/motion";  // loading placeholder
import { ApiError } from "@/lib/api";  // error type
import { useAuth } from "@/lib/auth-context";  // require sign-in
import {
  observability,
  type Group,
  type Quality,
  type SeriesPoint,
  type SpanDetail,
  type SpanFilters,
  type SpanRow,
  type Summary,
  type TraceRow,
} from "@/lib/observability";  // dashboard client and shapes

type Tab = "overview" | "calls" | "sessions" | "quality";

const WINDOWS: { hours: number; label: string }[] = [
  { hours: 1, label: "1h" },
  { hours: 6, label: "6h" },
  { hours: 24, label: "24h" },
  { hours: 168, label: "7d" },
  { hours: 720, label: "30d" },
];

/** 1234 -> "1.2 s", 80 -> "80 ms". */
const ms = (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`);
/** Dollars with enough digits for tiny per-call costs. */
const usd = (v: number) => (v === 0 ? "$0" : v < 0.01 ? `$${v.toFixed(5)}` : `$${v.toFixed(3)}`);
/** 12345 -> "12.3k". */
const num = (v: number) => (v >= 10_000 ? `${(v / 1000).toFixed(1)}k` : String(v));
/** Local time of day for an ISO timestamp. */
const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour12: false });

/** The dashboard. */
export default function ObservabilityPage() {
  const { user, loading } = useAuth();
  const [tab, setTab] = useState<Tab>("overview");
  const [hours, setHours] = useState(24);
  const [live, setLive] = useState(false);
  const [tick, setTick] = useState(0);
  // Jumping from a session to its calls.
  const [sessionFilter, setSessionFilter] = useState<string | undefined>();

  // Auto-refresh every 10 s while live.
  useEffect(() => {
    if (!live) return;
    const timer = setInterval(() => setTick((t) => t + 1), 10_000);
    return () => clearInterval(timer);
  }, [live]);

  if (loading) return <p className="empty">Loading…</p>;
  if (!user) return <p className="empty">Sign in to see the traces.</p>;
  if (user.role === "candidate")
    return <div className="alert alert--error">Only hosts and admins can see traces.</div>;

  const openCalls = (session_id: string) => {
    setSessionFilter(session_id);
    setTab("calls");
  };

  return (
    <div className="stack obs" style={{ gap: "1.25rem" }}>
      <header className="page-head obs-head">
        <div>
          <h1>Observability</h1>
          <p>Every model call, speech call and reply: what it said, cost and how long it took.</p>
        </div>
        <div className="obs-controls">
          <div className="seg" role="group" aria-label="Time window">
            {WINDOWS.map((w) => (
              <button
                key={w.hours}
                type="button"
                className={`seg-btn ${hours === w.hours ? "seg-btn--on" : ""}`}
                onClick={() => setHours(w.hours)}
              >
                {w.label}
              </button>
            ))}
          </div>
          <button type="button" className="secondary" onClick={() => setTick((t) => t + 1)}>
            Refresh
          </button>
          <label className="obs-live">
            <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} />
            Live
          </label>
        </div>
      </header>

      <nav className="seg obs-tabs" aria-label="Sections">
        {(["overview", "calls", "sessions", "quality"] as Tab[]).map((t) => (
          <button
            key={t}
            type="button"
            className={`seg-btn ${tab === t ? "seg-btn--on" : ""}`}
            onClick={() => setTab(t)}
          >
            {t[0].toUpperCase() + t.slice(1)}
          </button>
        ))}
      </nav>

      {tab === "overview" && <Overview hours={hours} tick={tick} />}
      {tab === "calls" && (
        <Calls
          hours={hours}
          tick={tick}
          session={sessionFilter}
          clearSession={() => setSessionFilter(undefined)}
        />
      )}
      {tab === "sessions" && <Sessions hours={hours} tick={tick} onOpen={openCalls} />}
      {tab === "quality" && <QualityTab hours={hours} tick={tick} />}
    </div>
  );
}

/** Load data with the dashboard's shared loading/error handling. */
function useLoad<T>(load: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(load, deps);
  useEffect(() => {
    let live = true;
    run()
      .then((d) => live && (setData(d), setError(null)))
      .catch((e) => live && setError(e instanceof ApiError ? e.message : "could not load"));
    return () => {
      live = false;
    };
  }, [run]);
  return { data, error };
}

/** One headline number. */
function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: string }) {
  return (
    <div className={`card obs-stat ${tone ? `obs-stat--${tone}` : ""}`}>
      <span className="obs-stat-label">{label}</span>
      <strong className="obs-stat-value">{value}</strong>
      {sub && <span className="meta">{sub}</span>}
    </div>
  );
}

/** A small bar chart of one series value over time. */
function Chart({
  title,
  points,
  value,
  format,
  tone = "accent",
}: {
  title: string;
  points: SeriesPoint[];
  value: (p: SeriesPoint) => number;
  format: (v: number) => string;
  tone?: "accent" | "danger";
}) {
  const values = points.map(value);
  const max = Math.max(...values, 1e-9);
  const w = 100 / Math.max(points.length, 1);
  return (
    <div className="card obs-chart">
      <div className="obs-chart-head">
        <h3>{title}</h3>
        <span className="meta">peak {format(max === 1e-9 ? 0 : max)}</span>
      </div>
      {points.length === 0 ? (
        <p className="empty">No calls in this window.</p>
      ) : (
        <svg viewBox="0 0 100 40" preserveAspectRatio="none" className={`obs-svg obs-svg--${tone}`} role="img" aria-label={title}>
          {values.map((v, i) => (
            <rect key={i} x={i * w + w * 0.12} y={40 - (v / max) * 38} width={w * 0.76} height={(v / max) * 38} rx="0.6">
              <title>{`${clock(points[i].t)} · ${format(v)}`}</title>
            </rect>
          ))}
        </svg>
      )}
    </div>
  );
}

/** A breakdown table (by purpose, agent or model). */
function Breakdown({ title, rows, onPick }: { title: string; rows: Group[]; onPick?: (key: string) => void }) {
  return (
    <div className="card obs-table-card">
      <h3>{title}</h3>
      {rows.length === 0 ? (
        <p className="empty">Nothing yet.</p>
      ) : (
        <div className="obs-scroll">
          <table className="obs-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Calls</th>
                <th>Errors</th>
                <th>Tokens in / out</th>
                <th>Cost</th>
                <th>p50</th>
                <th>p95</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.key} className={onPick ? "obs-row" : ""} onClick={() => onPick?.(r.key)}>
                  <td>{r.key}</td>
                  <td>{r.calls}</td>
                  <td className={r.errors ? "obs-bad" : ""}>{r.errors}</td>
                  <td>
                    {num(r.input_tokens)} / {num(r.output_tokens)}
                  </td>
                  <td>{usd(r.cost_usd)}</td>
                  <td>{ms(r.p50_ms)}</td>
                  <td>{ms(r.p95_ms)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/** Totals, charts and breakdowns. */
function Overview({ hours, tick }: { hours: number; tick: number }) {
  const { data, error } = useLoad<Summary>(() => observability.summary(hours), [hours, tick]);
  if (error) return <div className="alert alert--error">{error}</div>;
  if (!data) return <Skeleton rows={3} tall />;
  const t = data.totals;
  const reply = data.reply_latency;
  return (
    <div className="stack" style={{ gap: "1.25rem" }}>
      {t.calls === 0 && reply.count === 0 && (
        <div className="card">
          <h3>No traces yet</h3>
          <p className="meta">
            Run a practice session or an evaluation and every model call will appear here. With the offline stub model
            the calls are recorded too (cost stays $0).
          </p>
        </div>
      )}
      <section className="obs-grid">
        <Stat label="Model calls" value={num(t.calls)} sub={`${num(t.input_tokens)} in · ${num(t.output_tokens)} out tokens`} />
        <Stat label="Error rate" value={`${(t.error_rate * 100).toFixed(1)}%`} sub={`${t.errors} failed`} tone={t.errors ? "bad" : undefined} />
        <Stat label="Estimated cost" value={usd(t.cost_usd)} sub={t.unpriced_calls ? `${t.unpriced_calls} calls on unpriced models` : "all calls priced"} />
        <Stat label="Model latency" value={ms(t.p50_ms)} sub={`p95 ${ms(t.p95_ms)} · p99 ${ms(t.p99_ms)}`} />
        <Stat label="Reply latency" value={reply.count ? ms(reply.p50_ms) : "—"} sub={reply.count ? `p95 ${ms(reply.p95_ms)} · worst ${ms(reply.max_ms)}` : "student stops → persona audible"} />
        <Stat label="Speech-to-text" value={data.speech.stt.calls ? ms(data.speech.stt.p50_ms) : "—"} sub={`${data.speech.stt.calls} calls · p95 ${ms(data.speech.stt.p95_ms)}`} />
        <Stat label="Text-to-speech" value={data.speech.tts.calls ? ms(data.speech.tts.ttfb_p50_ms) : "—"} sub={`first byte · p95 ${ms(data.speech.tts.ttfb_p95_ms)}`} />
      </section>
      <section className="obs-charts">
        <Chart title="Calls" points={data.series} value={(p) => p.calls} format={(v) => String(Math.round(v))} />
        <Chart title="Errors" points={data.series} value={(p) => p.errors} format={(v) => String(Math.round(v))} tone="danger" />
        <Chart title="p95 latency" points={data.series} value={(p) => p.p95_ms} format={ms} />
        <Chart title="Cost" points={data.series} value={(p) => p.cost_usd} format={usd} />
      </section>
      <Breakdown title="By purpose" rows={data.by_purpose} />
      <Breakdown title="By agent" rows={data.by_agent} />
      <Breakdown title="By model" rows={data.by_model} />
      {data.truncated && <p className="meta">Showing the most recent 50,000 calls in this window.</p>}
    </div>
  );
}

/** Filterable call list with a detail panel. */
function Calls({ hours, tick, session, clearSession }: { hours: number; tick: number; session?: string; clearSession: () => void }) {
  const [filters, setFilters] = useState<SpanFilters>({});
  const [openId, setOpenId] = useState<string | null>(null);
  const merged = { ...filters, session_id: session };
  const { data, error } = useLoad<SpanRow[]>(
    () => observability.spans(hours, merged),
    [hours, tick, JSON.stringify(merged)],
  );
  const set = (key: keyof SpanFilters) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setFilters((f) => ({ ...f, [key]: e.target.value || undefined }));

  return (
    <div className="stack" style={{ gap: "1rem" }}>
      <div className="card obs-filters">
        <select value={filters.kind ?? ""} onChange={set("kind")} aria-label="Kind">
          <option value="">All kinds</option>
          <option value="llm">Model calls</option>
          <option value="stt">Speech-to-text</option>
          <option value="tts">Text-to-speech</option>
          <option value="latency">Reply latency</option>
        </select>
        <select value={filters.status ?? ""} onChange={set("status")} aria-label="Status">
          <option value="">Any status</option>
          <option value="ok">OK</option>
          <option value="error">Errors</option>
        </select>
        <input placeholder="Purpose (persona, evaluation…)" value={filters.purpose ?? ""} onChange={set("purpose")} />
        <input placeholder="Agent" value={filters.agent ?? ""} onChange={set("agent")} />
        <input placeholder="Search prompt or error" value={filters.q ?? ""} onChange={set("q")} />
        {session && (
          <button type="button" className="secondary" onClick={clearSession}>
            Session {session.slice(0, 8)} ✕
          </button>
        )}
      </div>
      {error && <div className="alert alert--error">{error}</div>}
      {!data ? (
        <Skeleton rows={4} />
      ) : data.length === 0 ? (
        <p className="empty">No calls match.</p>
      ) : (
        <div className="card obs-table-card">
          <div className="obs-scroll">
            <table className="obs-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Kind</th>
                  <th>Purpose</th>
                  <th>Agent</th>
                  <th>Model</th>
                  <th>Latency</th>
                  <th>Tokens</th>
                  <th>Cost</th>
                  <th>Status</th>
                  <th>Input</th>
                </tr>
              </thead>
              <tbody>
                {data.map((s) => (
                  <tr key={s.id} className="obs-row" onClick={() => setOpenId(s.id)}>
                    <td>{clock(s.started_at)}</td>
                    <td>{s.kind}</td>
                    <td>{s.purpose}</td>
                    <td>{s.agent ?? s.name}</td>
                    <td className="obs-mono">{s.model || "—"}</td>
                    <td>{ms(s.latency_ms)}</td>
                    <td>{s.input_tokens + s.output_tokens || "—"}</td>
                    <td>{s.kind === "llm" ? usd(s.cost_usd) : "—"}</td>
                    <td className={s.status === "error" ? "obs-bad" : "obs-ok"}>{s.status}</td>
                    <td className="obs-preview">{s.preview ?? s.error ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {openId && <Detail id={openId} onClose={() => setOpenId(null)} />}
    </div>
  );
}

/** One call in full, in a side panel. */
function Detail({ id, onClose }: { id: string; onClose: () => void }) {
  const { data, error } = useLoad<SpanDetail>(() => observability.span(id), [id]);
  const [section, setSection] = useState<"prompt" | "system" | "output" | "tools" | "meta">("prompt");
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const body = (() => {
    if (!data) return "";
    if (section === "system") return data.system_prompt ?? "(not stored)";
    if (section === "prompt") return data.prompt ?? "(not stored)";
    if (section === "output") return data.output ? JSON.stringify(data.output, null, 2) : "(none)";
    if (section === "tools")
      return data.tool_calls.length ? JSON.stringify(data.tool_calls, null, 2) : "(no tool calls)";
    return JSON.stringify(data.meta, null, 2);
  })();

  return (
    <div className="obs-drawer-wrap" onClick={onClose}>
      <aside className="obs-drawer card" onClick={(e) => e.stopPropagation()} aria-label="Call detail">
        <div className="obs-drawer-head">
          <h3>{data ? `${data.kind} · ${data.agent ?? data.name}` : "Call"}</h3>
          <button type="button" className="secondary" onClick={onClose}>
            Close
          </button>
        </div>
        {error && <div className="alert alert--error">{error}</div>}
        {!data ? (
          <Skeleton rows={3} />
        ) : (
          <>
            <dl className="obs-facts">
              <div><dt>Model</dt><dd className="obs-mono">{data.model || "—"}</dd></div>
              <div><dt>Latency</dt><dd>{ms(data.latency_ms)}{data.ttfb_ms != null ? ` · first byte ${ms(data.ttfb_ms)}` : ""}</dd></div>
              <div><dt>Tokens</dt><dd>{data.input_tokens} in · {data.output_tokens} out</dd></div>
              <div><dt>Cost</dt><dd>{data.priced ? usd(data.cost_usd) : "unpriced model"}</dd></div>
              <div><dt>Purpose</dt><dd>{data.purpose}{data.dimension ? ` · ${data.dimension}` : ""}{data.candidate ? ` · candidate ${data.candidate}` : ""}</dd></div>
              <div><dt>Session</dt><dd className="obs-mono">{data.session_id ?? "—"}{data.run ? ` · ${data.run}` : ""}</dd></div>
              <div><dt>Status</dt><dd className={data.status === "error" ? "obs-bad" : "obs-ok"}>{data.status}</dd></div>
            </dl>
            {data.error && <div className="alert alert--error">{data.error}</div>}
            <nav className="seg" aria-label="Detail section">
              {(["prompt", "system", "output", "tools", "meta"] as const).map((s) => (
                <button key={s} type="button" className={`seg-btn ${section === s ? "seg-btn--on" : ""}`} onClick={() => setSection(s)}>
                  {s === "tools" ? `Tool calls (${data.tool_calls.length})` : s[0].toUpperCase() + s.slice(1)}
                </button>
              ))}
            </nav>
            <pre className="obs-pre">{body}</pre>
            <button type="button" className="secondary" onClick={() => void navigator.clipboard?.writeText(body)}>
              Copy
            </button>
          </>
        )}
      </aside>
    </div>
  );
}

/** One row per session. */
function Sessions({ hours, tick, onOpen }: { hours: number; tick: number; onOpen: (id: string) => void }) {
  const { data, error } = useLoad<TraceRow[]>(() => observability.traces(hours), [hours, tick]);
  if (error) return <div className="alert alert--error">{error}</div>;
  if (!data) return <Skeleton rows={3} />;
  if (data.length === 0) return <p className="empty">No sessions traced in this window.</p>;
  return (
    <div className="card obs-table-card">
      <div className="obs-scroll">
        <table className="obs-table">
          <thead>
            <tr>
              <th>Session</th>
              <th>Last call</th>
              <th>Purposes</th>
              <th>Calls</th>
              <th>Errors</th>
              <th>Tokens</th>
              <th>Cost</th>
              <th>Model p95</th>
              <th>Reply p50 / p95</th>
            </tr>
          </thead>
          <tbody>
            {data.map((r) => (
              <tr key={r.session_id} className="obs-row" onClick={() => onOpen(r.session_id)}>
                <td className="obs-mono">{r.session_id.slice(0, 8)}</td>
                <td>{new Date(r.last_at).toLocaleString()}</td>
                <td>{r.purposes.join(", ")}</td>
                <td>{r.calls}</td>
                <td className={r.errors ? "obs-bad" : ""}>{r.errors}</td>
                <td>{num(r.input_tokens + r.output_tokens)}</td>
                <td>{usd(r.cost_usd)}</td>
                <td>{ms(r.p95_ms)}</td>
                <td>{r.reply_p50_ms ? `${ms(r.reply_p50_ms)} / ${ms(r.reply_p95_ms)}` : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** How trustworthy the evaluators' work looks. */
function QualityTab({ hours, tick }: { hours: number; tick: number }) {
  const { data, error } = useLoad<Quality>(() => observability.quality(hours), [hours, tick]);
  if (error) return <div className="alert alert--error">{error}</div>;
  if (!data) return <Skeleton rows={3} />;
  return (
    <div className="stack" style={{ gap: "1.25rem" }}>
      <section className="obs-grid">
        <Stat label="Candidates scored" value={String(data.candidates_scored)} />
        <Stat label="Sent to human review" value={`${(data.human_review_rate * 100).toFixed(0)}%`} sub="low confidence, unresolved critic findings or agent conflicts" />
      </section>
      <div className="card obs-table-card">
        <h3>Evaluators</h3>
        {data.agents.length === 0 ? (
          <p className="empty">No evaluations in this window.</p>
        ) : (
          <div className="obs-scroll">
            <table className="obs-table">
              <thead>
                <tr>
                  <th>Agent</th>
                  <th>Verdicts</th>
                  <th>Avg score</th>
                  <th>Avg confidence</th>
                  <th>Critic flagged</th>
                  <th>Tool calls / verdict</th>
                  <th>Evidence / verdict</th>
                </tr>
              </thead>
              <tbody>
                {data.agents.map((a) => (
                  <tr key={a.agent}>
                    <td>{a.agent}</td>
                    <td>{a.verdicts}</td>
                    <td>{a.avg_score.toFixed(1)}</td>
                    <td>{a.avg_confidence.toFixed(2)}</td>
                    <td className={a.critic_issue_rate > 0.3 ? "obs-bad" : ""}>{(a.critic_issue_rate * 100).toFixed(0)}%</td>
                    <td>{a.avg_tool_calls.toFixed(1)}</td>
                    <td>{a.avg_evidence.toFixed(1)}</td>
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
