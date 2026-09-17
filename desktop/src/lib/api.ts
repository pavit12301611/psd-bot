/** Typed wrappers around psd.ai's HTTP API, all routed through the IPC bridge. */
import { get, postJson, postForm, patchForm, del, request, stream, fileToPart, type FilePart, type StreamHandle } from "./ipc";

// ---------- auth ----------
export interface AuthStatus {
  configured: boolean;
  authenticated: boolean;
  username: string | null;
  is_admin: boolean;
  signup_enabled?: boolean;
  privileges?: Record<string, boolean>;
}
export interface AuthPolicy {
  password_min_length: number;
  reserved_usernames: string[];
  signup_enabled: boolean;
  session_days: number;
}
export const auth = {
  status: () => get<AuthStatus>("/api/auth/status"),
  policy: () => get<AuthPolicy>("/api/auth/policy"),
  setup: (username: string, password: string) => postJson("/api/auth/setup", { username, password }),
  signup: (username: string, password: string) => postJson("/api/auth/signup", { username, password }),
  login: (username: string, password: string, totp_code?: string) =>
    postJson<{ ok: boolean; requires_totp?: boolean; username?: string }>("/api/auth/login", {
      username,
      password,
      remember: true,
      totp_code: totp_code || null,
    }),
  logout: () => postJson("/api/auth/logout", {}),
  changePassword: (current_password: string, new_password: string) =>
    postJson("/api/auth/change-password", { current_password, new_password }),
};

// ---------- sessions ----------
export interface Session {
  id: string;
  name: string;
  model: string;
  endpoint_url?: string;
  rag?: boolean;
  archived?: boolean;
  folder?: string | null;
  created_at?: string;
  updated_at?: string;
  last_message_at?: string;
  message_count?: number;
  is_important?: boolean;
  mode?: string | null;
}
export const sessions = {
  list: () => get<Session[]>("/api/sessions"),
  create: (opts: { name: string; model?: string; endpoint_url?: string; endpoint_id?: string }) =>
    postForm<Session>("/api/session", {
      name: opts.name,
      model: opts.model || "",
      endpoint_url: opts.endpoint_url || "",
      endpoint_id: opts.endpoint_id || "",
      skip_validation: "true",
    }),
  rename: (id: string, name: string) => patchForm(`/api/session/${id}`, { name }),
  setModel: (id: string, model: string, endpoint_id: string, endpoint_url: string) =>
    patchForm(`/api/session/${id}`, { model, endpoint_id, endpoint_url }),
  remove: (id: string) => del(`/api/session/${id}`),
};

// ---------- history ----------
export interface HistoryEntry {
  role: "user" | "assistant" | "system" | string;
  content: any;
  metadata?: Record<string, any>;
}
export const history = {
  get: (sessionId: string) =>
    get<{ history: HistoryEntry[]; model: string; name: string }>(`/api/history/${sessionId}`),
};

// ---------- models ----------
export interface ModelItem {
  host: string;
  port: number;
  url: string;
  models: string[];
  models_display?: string[];
  models_extra?: string[];
  models_extra_display?: string[];
  endpoint_id?: string;
  endpoint_name?: string;
  category?: string;
  endpoint_kind?: string;
  model_type?: string;
  offline?: boolean;
}
export interface DefaultChat {
  endpoint_id: string;
  endpoint_url: string;
  model: string;
}
export const models = {
  list: (refresh = false) => get<{ hosts: any[]; items: ModelItem[] }>(`/api/models?refresh=${refresh}&background=${refresh}`),
  defaultChat: () => get<DefaultChat>("/api/default-chat"),
};

// ---------- endpoints (admin) ----------
export interface Endpoint {
  id: string;
  name: string;
  base_url: string;
  has_key: boolean;
  is_enabled: boolean;
  models: string[];
  model_count?: number;
  status: "online" | "offline" | "empty" | string;
  ping_error?: string | null;
  model_type?: string;
}
export const endpoints = {
  list: () => get<Endpoint[]>("/api/model-endpoints"),
  create: (name: string, base_url: string, api_key: string) =>
    postForm("/api/model-endpoints", { name, base_url, api_key, skip_probe: "false", require_models: "false", model_type: "llm", endpoint_kind: "auto" }),
  remove: (id: string) => del(`/api/model-endpoints/${id}`),
};

// ---------- uploads ----------
export const uploads = {
  send: async (file: File, sessionId?: string) => {
    
    const part: FilePart = await fileToPart("files", file);
    const form: Record<string, string> = {};
    if (sessionId) form.session_id = sessionId;
    return postForm<{ files: { id: string; filename?: string; name?: string }[] }>("/api/upload", form, [part]);
  },
};

// ---------- misc ----------
export const misc = {
  version: () => get<{ version: string }>("/api/version"),
  health: () => request({ method: "GET", path: "/api/health" }),
  stop: (sessionId: string, runId: string) =>
    request({ method: "POST", path: `/api/chat/stop/${sessionId}`, headers: runId ? { "X-psd.ai-Run-Id": runId } : {} }),
};

// ---------- chat streaming ----------
export interface ChatSendOptions {
  sessionId: string;
  message: string;
  mode: "chat" | "agent";
  web: boolean;
  bash: boolean;
  model?: string;
  endpointId?: string;
  endpointUrl?: string;
  attachments?: string[];
  toolApproval?: { id: string; decision: string };
}
export interface ChatStreamHandlers {
  onOpen?: (runId: string) => void;
  onEvent: (ev: any) => void;
  onError: (msg: string) => void;
  onDone: () => void;
}
export function sendChat(o: ChatSendOptions, h: ChatStreamHandlers): StreamHandle {
  const form: Record<string, string> = {
    message: o.message,
    session: o.sessionId,
    mode: o.mode,
    plan_mode: "false",
    allow_bash: o.bash ? "true" : "false",
  };
  if (o.mode === "agent") form.allow_web_search = o.web ? "true" : "false";
  else if (o.web) form.use_web = "true";
  if (o.model) form.selected_model = o.model;
  if (o.endpointId) form.selected_endpoint_id = o.endpointId;
  if (o.endpointUrl) form.selected_endpoint_url = o.endpointUrl;
  if (o.attachments?.length) form.attachments = JSON.stringify(o.attachments);
  if (o.toolApproval) {
    form.tool_approval_id = o.toolApproval.id;
    form.tool_approval_decision = o.toolApproval.decision;
  }
  const tz = -new Date().getTimezoneOffset();
  return stream(
    { method: "POST", path: "/api/chat_stream", form, headers: { "X-TZ-Offset": String(tz) } },
    {
      onOpen: (i) => h.onOpen?.(i.runId),
      onData: (raw) => {
        if (raw === "[DONE]") return;
        try {
          h.onEvent(JSON.parse(raw));
        } catch {
          /* ignore non-json frames */
        }
      },
      onError: (m) => {
        let msg = m;
        try {
          const j = JSON.parse(m);
          msg = j.error || j.detail || m;
        } catch {
          /* raw */
        }
        h.onError(msg);
      },
      onDone: h.onDone,
    },
  );
}
