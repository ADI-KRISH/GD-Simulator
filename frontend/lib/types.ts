/**
 * Mirrors of the backend response shapes.
 *
 * These follow the JSON Schemas in `claude/docs/contracts/` and the FastAPI
 * response models. When a contract changes, the schema file changes first, then
 * the Pydantic model, then this file — never the other way round.
 */

export type GDType = "technical" | "leadership" | "hr_social" | "general";
export type Difficulty = "beginner" | "intermediate" | "advanced";

export type SessionState =
  | "CREATED"
  | "WAITING"
  | "READY"
  | "STARTING"
  | "LIVE"
  | "ENDING"
  | "ENDED"
  | "EVALUATING"
  | "COMPLETED"
  | "FAILED"
  | "RETRYING";

export type ParticipantState =
  | "INVITED"
  | "JOINED"
  | "READY"
  | "ACTIVE"
  | "DISCONNECTED"
  | "RECONNECTED"
  | "LEFT";

export type EvaluationState =
  | "QUEUED"
  | "PREPARING"
  | "RUNNING"
  | "WAITING_FOR_AGENT"
  | "AGGREGATING"
  | "REPORT_GENERATING"
  | "COMPLETED"
  | "FAILED";

export interface Participant {
  id: string;
  user_id: string;
  candidate_label: string;
  display_name: string;
  state: ParticipantState;
  is_ready: boolean;
  joined_at: string;
}

export interface GDSession {
  mode?: "group" | "solo";
  id: string;
  code: string;
  host_id: string;
  topic: string;
  topic_category: string;
  gd_type: GDType;
  difficulty: Difficulty;
  min_participants: number;
  max_participants: number;
  duration_seconds: number;
  preparation_seconds: number;
  video_required: boolean;
  recording_enabled: boolean;
  rubric_weights: Record<string, number>;
  rubric_version: string;
  state: SessionState;
  started_at: string | null;
  ended_at: string | null;
  created_at: string;
  participants: Participant[];
}

export interface SessionSummary {
  id: string;
  code: string;
  topic: string;
  gd_type: GDType;
  state: SessionState;
  duration_seconds: number;
  created_at: string;
  started_at: string | null;
}

/** The Observer's running notes. Observations only — never scores. */
export interface RollingState {
  session_id: string;
  main_arguments: string[];
  candidate_positions: Record<string, string>;
  recent_interactions: string[];
  discussion_direction: string;
  unresolved_questions: string[];
  observations?: string[];
}

export interface AgentSpec {
  name: string;
  persona: string;
  talkativeness: "low" | "medium" | "high";
}

export interface LobbyView {
  lobby_id: string;
  session_id: string;
  code: string;
  topic: string;
  session_state: SessionState;
  capacity: number;
  min_participants: number;
  occupancy: number;
  ready_count: number;
  start_condition_met: boolean;
  participants: Participant[];
}

/** FR-10. The server owns the clock; this is only what it reported. */
export interface TimerView {
  session_id: string;
  phase: "waiting" | "preparation" | "discussion" | "ended";
  seconds_remaining: number;
  duration_seconds: number;
  warning: boolean;
}

export interface TopicView {
  session_id: string;
  topic: string;
  topic_category: string;
  difficulty: Difficulty;
  released: boolean;
}

export interface MediaToken {
  url: string;
  token: string;
  room: string;
  identity: string;
}

export interface TranscriptLine {
  timestamp: string;
  candidate_label: string;
  text: string;
  start: number;
  end: number;
  confidence: number;
}

export interface TranscriptOut {
  session_id: string;
  segment_count: number;
  chunk_seconds: number;
  chunk_count: number;
  lines: TranscriptLine[];
}

/** Every field here was computed in code, never estimated (design §17). */
export interface CandidateMetrics {
  session_id: string;
  candidate_id: string;
  speaking_seconds: number;
  participation_ratio: number;
  turns: number;
  interruptions: number;
  responses: number;
  questions: number;
  arguments_introduced: number;
  arguments_supported: number;
  arguments_challenged: number;
  response_latency_seconds: number;
}

export interface MetricsOut {
  session_id: string;
  total_speaking_seconds: number;
  candidates: Record<string, CandidateMetrics>;
  interactions: { source: string; target: string; count: number }[];
  dominant: string[];
  under_participating: string[];
}

export interface EvaluationStatus {
  job_id: string;
  session_id: string;
  evaluation_version: number;
  state: EvaluationState;
  gd_type: string;
  rubric: Record<string, number>;
  rubric_version: string;
  prompt_version: string;
  agent_version: string;
  selected_agents: string[];
  attempts: number;
  error: string | null;
  queued_at: string;
  completed_at: string | null;
}

export interface EvidenceItem {
  timestamp: string;
  summary: string;
  dimension?: string;
  confidence?: number;
}

export interface DimensionDetail {
  dimension: string;
  score_10: number;
  score_100: number;
  confidence: number;
  weight: number;
  strengths: string[];
  weaknesses: string[];
  evidence: EvidenceItem[];
}

/** Design §20 "Final Evaluator" — narrative only; it never sets the score. */
export interface SynthesisBody {
  summary: string;
  pattern: string;
  priority_improvement: string;
  caveat: string;
  /** Absent on reports stored before personalised feedback existed. */
  strengths?: { point: string; evidence: string; why_it_helped: string }[];
  focus_areas?: {
    area: string;
    what_happened: string;
    why_it_matters?: string;
    what_to_do: string;
    try_saying: string;
    practice?: string;
  }[];
  dimension_notes?: { dimension: string; what_it_measures: string; how_you_did: string }[];
  tool_calls: { tool: string; arguments: Record<string, unknown>; result: unknown }[];
  model: string;
  error: string | null;
}

/** Design §33. */
export interface CandidateReportBody {
  candidate_label: string;
  display_name: string;
  overall_score: number;
  dimensions: DimensionDetail[];
  strengths: string[];
  weaknesses: string[];
  evidence: EvidenceItem[];
  metrics: CandidateMetrics;
  feedback: string;
  needs_human_review: boolean;
  review_reasons: string[];
  // Absent on reports stored before the synthesis agent existed, so this is
  // optional in the type as well as in practice.
  synthesis?: SynthesisBody | Record<string, never>;
  /** Points deducted for interrupting, and when each interruption happened (s). */
  penalty?: number;
  interruption_times?: number[];
}

/** One open lobby in the public browser. */
export interface LobbyListItem {
  session_id: string;
  code: string;
  topic: string;
  topic_category: string;
  gd_type: GDType;
  difficulty: Difficulty;
  host_name: string;
  occupancy: number;
  capacity: number;
  duration_seconds: number;
  preparation_seconds: number;
}

export interface PersonaPreset {
  key: string;
  name: string;
  tagline: string;
  brief: string;
  voice_id: string;
  eagerness: number;
}

export interface PersonaChoice {
  preset?: string;
  name?: string;
  persona?: string;
  voice_id?: string;
}

/** Design §34. */
export interface GroupReportBody {
  session_id: string;
  topic: string;
  discussion_quality: string;
  main_themes: string[];
  consensus: string;
  dominant_participants: string[];
  under_participating: string[];
  turning_point: string;
  unresolved_questions: string[];
  participation: Record<string, number>;
  agent_conflicts?: string[];
  agent_failures?: string[];
}

export interface ReportOut<T> {
  session_id: string;
  candidate_label: string | null;
  body: T;
  created_at: string;
}

export interface ScoreOut {
  candidate_label: string;
  overall: number;
  dimensions: Record<string, number>;
  weights: Record<string, number>;
  confidence: number;
  needs_human_review: boolean;
}

/** FR-31. */
export interface HistoryOut {
  sessions_completed: number;
  average_overall: number | null;
  trend: Record<string, number>;
  entries: {
    session_id: string;
    topic: string;
    overall: number;
    dimensions: Record<string, number>;
    created_at: string;
  }[];
}

/** FR-32. */
export type NotificationKind =
  | "lobby_created"
  | "session_invitation"
  | "session_starting"
  | "session_ended"
  | "evaluation_completed"
  | "report_available";

export interface NotificationItem {
  id: string;
  session_id: string | null;
  kind: NotificationKind;
  title: string;
  body: string;
  link: string | null;
  payload: Record<string, unknown>;
  read_at: string | null;
  created_at: string;
}

export interface NotificationList {
  unread: number;
  items: NotificationItem[];
}
