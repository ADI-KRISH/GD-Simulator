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
} from "./types";

// PORTS.txt: M14's API is 8114 (8000 is the platform gateway's).
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8114";

const ACCESS_KEY = "gd.access_token";
const REFRESH_KEY = "gd.refresh_token";

export type Role = "candidate" | "host" | "admin";
export type ExperienceLevel = "student" | "entry" | "mid" | "senior";

export interface Profile {
  skills: string[];
  experience_level: ExperienceLevel;
  strengths: string[];
  weaknesses: string[];
  improvement_areas: string[];
  sessions_completed: number;
}

export interface User {
  id: string;
  email: string;
  full_name: string;
  role: Role;
  is_active: boolean;
  created_at: string;
  profile: Profile | null;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_at: string;
}

export class ApiError extends Error {
  status: number;
  code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

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

interface RequestOptions {
  method?: string;
  body?: unknown;
  auth?: boolean;
  /** Internal: stops a failed refresh from recursing. */
  retryOnAuthFailure?: boolean;
}

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

export const api = {
  register: (body: { email: string; full_name: string; password: string }) =>
    request<User>("/api/v1/auth/register", { method: "POST", body }),

  login: async (body: { email: string; password: string }) => {
    const pair = await request<TokenPair>("/api/v1/auth/login", { method: "POST", body });
    tokenStore.save(pair);
    return pair;
  },

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

  me: () => request<User>("/api/v1/auth/me", { auth: true }),

  updateProfile: (body: {
    full_name?: string;
    skills?: string[];
    experience_level?: ExperienceLevel;
  }) => request<User>("/api/v1/auth/me", { method: "PATCH", body, auth: true }),

  requestPasswordReset: (email: string) =>
    request<{ detail: string; reset_token: string | null }>("/api/v1/auth/password-reset/request", {
      method: "POST",
      body: { email },
    }),

  confirmPasswordReset: (token: string, new_password: string) =>
    request<void>("/api/v1/auth/password-reset/confirm", {
      method: "POST",
      body: { token, new_password },
    }),

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

export const sessions = {
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

  list: (role: "host" | "participant" = "participant") =>
    request<SessionSummary[]>(`/api/v1/sessions?role=${role}`, { auth: true }),

  get: (id: string) => request<GDSession>(`/api/v1/sessions/${id}`, { auth: true }),

  start: (id: string) =>
    request<GDSession>(`/api/v1/sessions/${id}/start`, { method: "POST", auth: true }),

  end: (id: string) =>
    request<GDSession>(`/api/v1/sessions/${id}/end`, { method: "POST", auth: true }),

  timer: (id: string) => request<TimerView>(`/api/v1/sessions/${id}/timer`, { auth: true }),

  topic: (id: string) => request<TopicView>(`/api/v1/sessions/${id}/topic`, { auth: true }),

  mediaToken: (id: string) =>
    request<MediaToken>(`/api/v1/sessions/${id}/media-token`, { auth: true }),

  transcript: (id: string) =>
    request<TranscriptOut>(`/api/v1/sessions/${id}/transcript`, { auth: true }),

  metrics: (id: string) => request<MetricsOut>(`/api/v1/sessions/${id}/metrics`, { auth: true }),

  observer: (id: string) =>
    request<RollingState>(`/api/v1/sessions/${id}/observer`, { auth: true }),

  /** Local-only demo: seat LLM-played candidates in the lobby. */
  addAgents: (id: string, body: { agents: AgentSpec[]; extra_prompt: string }) =>
    request<LobbyView>(`/api/v1/sessions/${id}/agents`, { method: "POST", body, auth: true }),
};

export const practice = {
  personas: () =>
    request<{ personas: PersonaPreset[]; voice: "elevenlabs" | "browser" }>(
      "/api/v1/practice/personas",
      { auth: true },
    ),

  create: (body: {
    topic: string;
    gd_type?: GDType;
    difficulty?: string;
    duration_seconds?: number;
    preparation_seconds?: number;
    personas: PersonaChoice[];
  }) => request<GDSession>("/api/v1/practice", { method: "POST", body, auth: true }),
};

export const lobbies = {
  browse: () => request<LobbyListItem[]>("/api/v1/lobbies", { auth: true }),

  join: (code: string) =>
    request<LobbyView>("/api/v1/lobbies/join", {
      method: "POST",
      body: { code: code.trim().toUpperCase() },
      auth: true,
    }),

  get: (sessionId: string) => request<LobbyView>(`/api/v1/lobbies/${sessionId}`, { auth: true }),

  setReady: (sessionId: string, ready: boolean) =>
    request<Participant>(`/api/v1/lobbies/${sessionId}/ready`, {
      method: "POST",
      body: { ready },
      auth: true,
    }),

  leave: (sessionId: string) =>
    request<void>(`/api/v1/lobbies/${sessionId}/leave`, { method: "POST", auth: true }),

  invite: (sessionId: string, email: string) =>
    request<Participant>(`/api/v1/lobbies/${sessionId}/invite`, {
      method: "POST",
      body: { email },
      auth: true,
    }),
};

export const notifications = {
  list: (unreadOnly = false) =>
    request<NotificationList>(`/api/v1/me/notifications?unread_only=${unreadOnly}`, {
      auth: true,
    }),

  markRead: (id: string) =>
    request<NotificationItem>(`/api/v1/me/notifications/${id}/read`, {
      method: "POST",
      auth: true,
    }),

  markAllRead: () =>
    request<{ marked_read: number }>("/api/v1/me/notifications/read-all", {
      method: "POST",
      auth: true,
    }),
};

export const evaluation = {
  request: (sessionId: string, runNow = false) =>
    request<EvaluationStatus>(
      `/api/v1/sessions/${sessionId}/evaluate?run_now=${runNow}`,
      { method: "POST", auth: true },
    ),

  status: (sessionId: string) =>
    request<EvaluationStatus>(`/api/v1/sessions/${sessionId}/evaluation`, { auth: true }),

  scores: (sessionId: string) =>
    request<ScoreOut[]>(`/api/v1/sessions/${sessionId}/scores`, { auth: true }),

  myReport: (sessionId: string) =>
    request<ReportOut<CandidateReportBody>>(`/api/v1/sessions/${sessionId}/report`, {
      auth: true,
    }),

  groupReport: (sessionId: string) =>
    request<ReportOut<GroupReportBody>>(`/api/v1/sessions/${sessionId}/report/group`, {
      auth: true,
    }),

  candidateReport: (sessionId: string, label: string) =>
    request<ReportOut<CandidateReportBody>>(
      `/api/v1/sessions/${sessionId}/report/${label}`,
      { auth: true },
    ),

  history: () => request<HistoryOut>("/api/v1/me/history", { auth: true }),
};
