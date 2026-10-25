import { request } from "./api";  // authenticated JSON client

/** Stats for a group of calls. */
export interface Stats {
  calls: number;
  errors: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  p50_ms: number;
  p95_ms: number;
  p99_ms: number;
  mean_ms: number;
}

/** Stats for one value of a breakdown (a purpose, a model, an agent). */
export interface Group extends Stats {
  key: string;
}

/** One point of the time series. */
export interface SeriesPoint {
  t: string;
  calls: number;
  errors: number;
  cost_usd: number;
  p95_ms: number;
  tokens: number;
}

/** The overview tab's data. */
export interface Summary {
  window_hours: number;
  totals: Stats & { unpriced_calls: number; error_rate: number };
  by_purpose: Group[];
  by_model: Group[];
  by_agent: Group[];
  speech: {
    stt: Stats;
    tts: Stats & { ttfb_p50_ms: number; ttfb_p95_ms: number };
  };
  reply_latency: { count: number; p50_ms: number; p95_ms: number; max_ms: number };
  series: SeriesPoint[];
  truncated: boolean;
}

/** One session's roll-up. */
export interface TraceRow extends Stats {
  session_id: string;
  started_at: string;
  last_at: string;
  purposes: string[];
  reply_p50_ms: number;
  reply_p95_ms: number;
}

/** A row of the call list. */
export interface SpanRow {
  id: string;
  started_at: string;
  kind: string;
  purpose: string;
  name: string;
  model: string;
  agent: string | null;
  session_id: string | null;
  latency_ms: number;
  ttfb_ms: number | null;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  priced: boolean;
  status: string;
  error: string | null;
  preview: string | null;
}

/** One call in full. */
export interface SpanDetail extends Omit<SpanRow, "preview"> {
  provider: string;
  run: string | null;
  dimension: string | null;
  candidate: string | null;
  system_prompt: string | null;
  prompt: string | null;
  output: unknown;
  tool_calls: { name: string; arguments: unknown; result: string }[];
  meta: Record<string, unknown>;
}

/** Evaluator quality signals. */
export interface Quality {
  window_hours: number;
  candidates_scored: number;
  human_review_rate: number;
  agents: {
    agent: string;
    verdicts: number;
    avg_score: number;
    avg_confidence: number;
    critic_issue_rate: number;
    avg_tool_calls: number;
    avg_evidence: number;
  }[];
}

/** Filters of the call list. */
export interface SpanFilters {
  kind?: string;
  purpose?: string;
  status?: string;
  agent?: string;
  model?: string;
  session_id?: string;
  q?: string;
}

/** Build a query string from the defined values. */
function query(params: Record<string, string | number | undefined>): string {
  const pairs = Object.entries(params).filter(([, v]) => v !== undefined && v !== "");
  return pairs.length
    ? "?" + pairs.map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join("&")
    : "";
}

/** Tracing dashboard endpoints (hosts and admins). */
export const observability = {
  summary: (hours: number, session_id?: string) =>
    request<Summary>(`/api/v1/observability/summary${query({ hours, session_id })}`, { auth: true }),
  traces: (hours: number) =>
    request<TraceRow[]>(`/api/v1/observability/traces${query({ hours })}`, { auth: true }),
  spans: (hours: number, filters: SpanFilters, offset = 0) =>
    request<SpanRow[]>(`/api/v1/observability/spans${query({ hours, ...filters, offset })}`, {
      auth: true,
    }),
  span: (id: string) => request<SpanDetail>(`/api/v1/observability/spans/${id}`, { auth: true }),
  quality: (hours: number) =>
    request<Quality>(`/api/v1/observability/quality${query({ hours })}`, { auth: true }),
};
