import type {
  AgentSpec,
  EvaluationStatus,
  GDSession,
  GDType,
  HistoryOut,
  LobbyListItem,
  LobbyView,
  MediaToken,
  MetricsOut,
  Participant,
  ReportOut,
  RollingState,
  ScoreOut,
  SessionSummary,
  TimerView,
  TopicView,
  TranscriptOut,
  CandidateReportBody,
  GroupReportBody,
  NotificationItem,
  NotificationList,
  PersonaChoice,
  PersonaPreset,
} from "./types";  // response/request shapes shared with the backend

// PORTS.txt: M14's API is 8114 (8000 is the platform gateway's).
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8114";

/** localStorage key holding the access token. */
const ACCESS_KEY = "gd.access_token";
/** localStorage key holding the refresh token. */
const REFRESH_KEY = "gd.refresh_token";

/** Account role as returned by the API. */
export type Role = "candidate" | "host" | "admin";
/** Self-declared career stage on the profile. */
export type ExperienceLevel = "student" | "entry" | "mid" | "senior";

/** A user's profile fields. */
export interface Profile {
  skills: string[];
  experience_level: ExperienceLevel;
  strengths: string[];
  weaknesses: string[];
  improvement_areas: string[];
  sessions_completed: number;
}

/** A signed-in account as returned by /auth/me. */
export interface User {
  id: string;
  email: string;
  full_name: string;
  role: Role;
  is_active: boolean;
  created_at: string;
  profile: Profile | null;
}

/** Access + refresh tokens returned by login and refresh. */
export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_at: string;
}

/** Error thrown for any non-2xx API response, carrying the HTTP status and machine code. */
export class ApiError extends Error {
  status: number;
  code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

/** Reads, saves and clears the auth tokens in localStorage (server-safe: null on the server). */
export const tokenStore = {
  get access() {
    return typeof window === "undefined" ? null : localStorage.getItem(ACCESS_KEY);
  },
  get refresh() {
    return typeof window === "undefined" ? null : localStorage.getItem(REFRESH_KEY);
  },
  save(pair: TokenPair) {
    localStorage.setItem(ACCESS_KEY, pair.access_token);
    localStorage.setItem(REFRESH_KEY, pair.refresh_token);
  },
  clear() {
    localStorage.removeItem(ACCESS_KEY);
    localStorage.removeItem(REFRESH_KEY);
  },
};

/**
 * Sign in as the student the AI Interviewer platform says this is (M14 Day 4).
 * The headers are the platform's development identity, which GD's backend
 * honours only with PLATFORM_BRIDGE=1. The name is URI-encoded: a header is
 * Latin-1, and a student's name need not be.
 */
export async function platformSignIn(candidate: string, name: string): Promise<TokenPair> {
  const response = await fetch(`${API_URL}/api/v1/auth/platform`, {
    method: "POST",
    headers: {
      "X-Candidate-Id": candidate,
      "X-Role": "student",
      "X-Display-Name": encodeURIComponent(name),
    },
  });
  if (!response.ok) throw await toError(response);
  return (await response.json()) as TokenPair;
}

/** Turn a failed fetch Response into an ApiError, reading the backend's error shapes. */
async function toError(response: Response): Promise<ApiError> {
  let code = "error";
  let message = response.statusText;
  try {
    const body = await response.json();
    if (body?.error) {
      code = body.error.code ?? code;
      message = body.error.message ?? message;
    } else if (Array.isArray(body?.detail)) {
      code = "validation_error";
      message = body.detail.map((d: { msg: string }) => d.msg).join(", ");
    } else if (typeof body?.detail === "string") {
      message = body.detail;
    }
  } catch {
    // response had no JSON body; the status text stands.
  }
  return new ApiError(response.status, code, message);
}

/** Options for `request`: method, JSON body, whether to send the bearer token. */
interface RequestOptions {
  method?: string;
  body?: unknown;
  auth?: boolean;
  /** Internal: stops a failed refresh from recursing. */
  retryOnAuthFailure?: boolean;
}

/** Call the API with JSON in/out; on a 401 it refreshes the token once and retries. */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, auth = false, retryOnAuthFailure = true } = options;

  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (auth && tokenStore.access) headers["Authorization"] = `Bearer ${tokenStore.access}`;

  const response = await fetch(`${API_URL}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (response.status === 401 && auth && retryOnAuthFailure && tokenStore.refresh) {
    const refreshed = await tryRefresh();
    if (refreshed) {
      return request<T>(path, { ...options, retryOnAuthFailure: false });
    }
  }

  if (!response.ok) throw await toError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** Exchange the refresh token for a new pair; clears tokens and returns false on failure. */
async function tryRefresh(): Promise<boolean> {
  const refresh_token = tokenStore.refresh;
  if (!refresh_token) return false;
  try {
    const pair = await request<TokenPair>("/api/v1/auth/refresh", {
      method: "POST",
      body: { refresh_token },
    });
    tokenStore.save(pair);
    return true;
  } catch {
    tokenStore.clear();
    return false;
  }
}

/** Auth and account endpoints. */
export const api = {
  /** Create an account. */
  register: (body: { email: string; full_name: string; password: string }) =>
    request<User>("/api/v1/auth/register", { method: "POST", body }),

  /** Sign in and store the returned tokens. */
  login: async (body: { email: string; password: string }) => {
    const pair = await request<TokenPair>("/api/v1/auth/login", { method: "POST", body });
    tokenStore.save(pair);
    return pair;
  },

  /** Revoke the refresh token on the server and clear local tokens. */
  logout: async () => {
    const refresh_token = tokenStore.refresh;
    if (refresh_token) {
      await request<void>("/api/v1/auth/logout", {
        method: "POST",
        body: { refresh_token },
      }).catch(() => undefined);
    }
    tokenStore.clear();
  },

  /** Fetch the signed-in user. */
  me: () => request<User>("/api/v1/auth/me", { auth: true }),

  /** Update name, skills or experience level. */
  updateProfile: (body: {
    full_name?: string;
    skills?: string[];
    experience_level?: ExperienceLevel;
  }) => request<User>("/api/v1/auth/me", { method: "PATCH", body, auth: true }),

  /** Ask for a password-reset token (returned directly in local mode). */
  requestPasswordReset: (email: string) =>
    request<{ detail: string; reset_token: string | null }>("/api/v1/auth/password-reset/request", {
      method: "POST",
      body: { email },
    }),

  /** Set a new password using a reset token. */
  confirmPasswordReset: (token: string, new_password: string) =>
    request<void>("/api/v1/auth/password-reset/confirm", {
      method: "POST",
      body: { token, new_password },
    }),

  /** Backend liveness and dependency status. */
  health: () =>
    request<{ status: string; env: string; checks: Record<string, boolean> }>("/health"),
};

/**
 * Upload one recording window (FR-12).
 *
 * Multipart, so it bypasses `request` — but it reuses the same token handling
 * and error mapping so a 401 here behaves like a 401 anywhere else.
 */
export async function uploadAudio(
  sessionId: string,
  sequence: number,
  start: number,
  end: number,
  audio: Blob,
): Promise<{ status: string; segments_created: number }> {
  const form = new FormData();
  form.append("sequence", String(sequence));
  form.append("start", String(start));
  form.append("end", String(end));
  form.append("audio", audio, `chunk-${sequence}.webm`);

  const response = await fetch(`${API_URL}/api/v1/sessions/${sessionId}/audio`, {
    method: "POST",
    headers: tokenStore.access ? { Authorization: `Bearer ${tokenStore.access}` } : {},
    body: form,
  });
  if (!response.ok) throw await toError(response);
  return response.json();
}

/** Session lifecycle, timer, topic, transcript, metrics and observer endpoints. */
export const sessions = {
  /** Create a group discussion lobby. */
  create: (body: {
    topic: string;
    topic_category?: string;
    gd_type?: GDType;
    difficulty?: string;
    min_participants?: number;
    max_participants?: number;
    duration_seconds?: number;
    preparation_seconds?: number;
    video_required?: boolean;
    recording_enabled?: boolean;
  }) => request<GDSession>("/api/v1/sessions", { method: "POST", body, auth: true }),

  /** List sessions the user hosts or takes part in. */
  list: (role: "host" | "participant" = "participant") =>
    request<SessionSummary[]>(`/api/v1/sessions?role=${role}`, { auth: true }),

  /** Fetch one session. */
  get: (id: string) => request<GDSession>(`/api/v1/sessions/${id}`, { auth: true }),

  /** Host: begin preparation (the discussion follows automatically). */
  start: (id: string) =>
    request<GDSession>(`/api/v1/sessions/${id}/start`, { method: "POST", auth: true }),

  /** Host: end the discussion early. */
  end: (id: string) =>
    request<GDSession>(`/api/v1/sessions/${id}/end`, { method: "POST", auth: true }),

  /** Server clock (polling fallback for the WebSocket). */
  timer: (id: string) => request<TimerView>(`/api/v1/sessions/${id}/timer`, { auth: true }),

  /** The discussion topic (withheld until preparation begins). */
  topic: (id: string) => request<TopicView>(`/api/v1/sessions/${id}/topic`, { auth: true }),

  /** LiveKit credentials for joining the media room. */
  mediaToken: (id: string) =>
    request<MediaToken>(`/api/v1/sessions/${id}/media-token`, { auth: true }),

  /** The raw transcript. */
  transcript: (id: string) =>
    request<TranscriptOut>(`/api/v1/sessions/${id}/transcript`, { auth: true }),

  /** Deterministic metrics for the session. */
  metrics: (id: string) => request<MetricsOut>(`/api/v1/sessions/${id}/metrics`, { auth: true }),

  /** The Observer's rolling state (observations only). */
  observer: (id: string) =>
    request<RollingState>(`/api/v1/sessions/${id}/observer`, { auth: true }),

  /** Local-only demo: seat LLM-played candidates in the lobby. */
  addAgents: (id: string, body: { agents: AgentSpec[]; extra_prompt: string }) =>
    request<LobbyView>(`/api/v1/sessions/${id}/agents`, { method: "POST", body, auth: true }),
};

/** Solo practice endpoints: list personas and open a practice session. */
export const practice = {
  /** Built-in personas and how they will be voiced. */
  personas: () =>
    request<{ personas: PersonaPreset[]; voice: "elevenlabs" | "browser" }>(
      "/api/v1/practice/personas",
      { auth: true },
    ),

  /** Open a solo practice session with chosen personas. */
  create: (body: {
    topic: string;
    gd_type?: GDType;
    difficulty?: string;
    duration_seconds?: number;
    preparation_seconds?: number;
    personas: PersonaChoice[];
  }) => request<GDSession>("/api/v1/practice", { method: "POST", body, auth: true }),
};

/** Lobby endpoints: browse, join, ready, leave and invite. */
export const lobbies = {
  /** Open group lobbies anyone can join. */
  browse: () => request<LobbyListItem[]>("/api/v1/lobbies", { auth: true }),

  /** Join a lobby by its code. */
  join: (code: string) =>
    request<LobbyView>("/api/v1/lobbies/join", {
      method: "POST",
      body: { code: code.trim().toUpperCase() },
      auth: true,
    }),

  /** Current lobby view. */
  get: (sessionId: string) => request<LobbyView>(`/api/v1/lobbies/${sessionId}`, { auth: true }),

  /** Mark yourself ready / not ready. */
  setReady: (sessionId: string, ready: boolean) =>
    request<Participant>(`/api/v1/lobbies/${sessionId}/ready`, {
      method: "POST",
      body: { ready },
      auth: true,
    }),

  /** Leave the lobby. */
  leave: (sessionId: string) =>
    request<void>(`/api/v1/lobbies/${sessionId}/leave`, { method: "POST", auth: true }),

  /** Host: invite a registered user by email. */
  invite: (sessionId: string, email: string) =>
    request<Participant>(`/api/v1/lobbies/${sessionId}/invite`, {
      method: "POST",
      body: { email },
      auth: true,
    }),
};

/** The user's in-app notification inbox. */
export const notifications = {
  /** Fetch notifications (optionally only unread). */
  list: (unreadOnly = false) =>
    request<NotificationList>(`/api/v1/me/notifications?unread_only=${unreadOnly}`, {
      auth: true,
    }),

  /** Mark one notification read. */
  markRead: (id: string) =>
    request<NotificationItem>(`/api/v1/me/notifications/${id}/read`, {
      method: "POST",
      auth: true,
    }),

  /** Mark every notification read. */
  markAllRead: () =>
    request<{ marked_read: number }>("/api/v1/me/notifications/read-all", {
      method: "POST",
      auth: true,
    }),
};

/** Evaluation endpoints: request a run, check status, scores, reports and history. */
export const evaluation = {
  /** Queue (or run now) an evaluation for the session. */
  request: (sessionId: string, runNow = false) =>
    request<EvaluationStatus>(
      `/api/v1/sessions/${sessionId}/evaluate?run_now=${runNow}`,
      { method: "POST", auth: true },
    ),

  /** Status of the session's latest evaluation job. */
  status: (sessionId: string) =>
    request<EvaluationStatus>(`/api/v1/sessions/${sessionId}/evaluation`, { auth: true }),

  /** All candidates' final scores. */
  scores: (sessionId: string) =>
    request<ScoreOut[]>(`/api/v1/sessions/${sessionId}/scores`, { auth: true }),

  /** Your own report. */
  myReport: (sessionId: string) =>
    request<ReportOut<CandidateReportBody>>(`/api/v1/sessions/${sessionId}/report`, {
      auth: true,
    }),

  /** The group-level report. */
  groupReport: (sessionId: string) =>
    request<ReportOut<GroupReportBody>>(`/api/v1/sessions/${sessionId}/report/group`, {
      auth: true,
    }),

  /** A specific candidate's report. */
  candidateReport: (sessionId: string, label: string) =>
    request<ReportOut<CandidateReportBody>>(
      `/api/v1/sessions/${sessionId}/report/${label}`,
      { auth: true },
    ),

  /** Your past sessions and score trend. */
  history: () => request<HistoryOut>("/api/v1/me/history", { auth: true }),
};
