/** Typed wrappers around psd.ai's HTTP API, all routed through the IPC bridge. */
import {
  get,
  postJson,
  putJson,
  patchJson,
  postForm,
  patchForm,
  del,
  request,
  stream,
  fileToPart,
  qs,
  type FilePart,
  type StreamHandle,
} from "./ipc";

export { stream, fileToPart, type StreamHandle, type FilePart };

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
  endpoint_id?: string;
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
  listArchived: (opts: { search?: string; offset?: number; limit?: number; sort?: string } = {}) =>
    get<{ sessions: Session[]; total: number }>(
      `/api/sessions/archived${qs({ search: opts.search, offset: opts.offset ?? 0, limit: opts.limit ?? 40, sort: opts.sort || "recent" })}`,
    ),
  create: (opts: { name: string; model?: string; endpoint_url?: string; endpoint_id?: string }) =>
    postForm<Session>("/api/session", {
      name: opts.name,
      model: opts.model || "",
      endpoint_url: opts.endpoint_url || "",
      endpoint_id: opts.endpoint_id || "",
      skip_validation: "true",
    }),
  rename: (id: string, name: string) => patchForm(`/api/session/${id}`, { name }),
  setFolder: (id: string, folder: string) => patchForm(`/api/session/${id}`, { folder }),
  setModel: (id: string, model: string, endpoint_id: string, endpoint_url: string) =>
    patchForm(`/api/session/${id}`, { model, endpoint_id, endpoint_url }),
  remove: (id: string) => del(`/api/session/${id}`),
  bulkDelete: (ids: string[]) => postJson<{ deleted: number }>("/api/sessions/bulk-delete", { ids }),
  archive: (id: string) => postJson(`/api/session/${id}/archive`, {}),
  unarchive: (id: string) => postJson(`/api/session/${id}/unarchive`, {}),
  important: (id: string, important: boolean) =>
    postForm(`/api/session/${id}/important`, { important: String(important) }),
  export: (id: string, fmt: "md" | "json" | "txt" | "html" = "md") =>
    request({ method: "GET", path: `/api/session/${id}/export?fmt=${fmt}` }),
  compact: (id: string) => postJson(`/api/session/${id}/compact`, {}),
  inject: (id: string, messages: { role: string; content: any; metadata?: any }[]) =>
    postJson(`/api/session/${id}/inject_messages`, { messages }),
  autoSort: (skipLlm = false) => postJson<any>(`/api/sessions/auto-sort${qs({ skip_llm: skipLlm })}`, {}),
  contextInfo: (id: string) => get<{ context_length?: number | null; model?: string }>(`/api/session/${id}/context_info`),
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

// ---------- cookbook / local model download ----------
export interface CookbookTaskStatus {
  session_id: string;
  type: string;
  model: string;
  status: string;
  progress?: string;
  phase?: string;
  output_tail?: string;
  exit_code?: number | null;
  remote?: string;
}
export interface OllamaLibModel {
  name: string;
  description?: string;
  sizes?: string[];
}
export interface HwfitModel {
  name?: string;
  repo_id?: string;
  display?: string;
  provider?: string;
  parameter_count?: string;
  params_b?: number;
  size?: string;
  params?: string;
  quant?: string;
  fit?: boolean;
  fit_level?: string;
  run_mode?: string;
  score?: number;
  speed_tps?: number;
  required_gb?: number;
  backend?: string;
  gguf?: string;
  gguf_sources?: { repo?: string; file?: string }[];
  notes?: string;
  vram_gb?: number;
  ram_gb?: number;
  access?: string;
  use_case?: string;
}
export interface CachedModel {
  repo_id: string;
  size?: string;
  nb_files?: number;
  has_incomplete?: boolean;
  status?: string;
  path?: string;
  is_gguf?: boolean;
  is_ollama?: boolean;
  backend?: string;
  gguf_files?: string[];
}
export const cookbook = {
  download: (opts: { repo_id: string; backend?: "hf" | "ollama"; include?: string; disable_hf_transfer?: boolean }) =>
    postJson<{ ok: boolean; session_id?: string; error?: string; remote?: string }>("/api/model/download", {
      repo_id: opts.repo_id,
      backend: opts.backend || (opts.repo_id.includes("/") ? "hf" : "ollama"),
      include: opts.include || null,
      disable_hf_transfer: opts.disable_hf_transfer ?? false,
    }),
  serve: (opts: { repo_id: string; cmd: string }) =>
    postJson<{ ok: boolean; session_id?: string; error?: string; endpoint_id?: string }>("/api/model/serve", {
      repo_id: opts.repo_id,
      cmd: opts.cmd,
    }),
  cached: () => get<{ models: any[]; host?: string; error?: string }>("/api/model/cached"),
  state: () => get<any>("/api/cookbook/state"),
  saveState: (body: any) => postJson("/api/cookbook/state", body),
  taskStatus: () => get<{ tasks: CookbookTaskStatus[] }>("/api/cookbook/tasks/status"),
  ollamaLibrary: () => get<{ models: OllamaLibModel[]; error?: string }>("/api/cookbook/ollama/library"),
  hfLatest: (limit = 12) => get<{ models: any[]; error?: string }>(`/api/cookbook/hf-latest?limit=${limit}`),
  hfGgufFiles: (repo_id: string) =>
    get<{ files?: string[]; gguf_files?: string[]; error?: string }>(`/api/cookbook/hf-gguf-files${qs({ repo_id })}`),
  killPid: (pid: number) => postJson("/api/cookbook/kill-pid", { pid }),
};
export const hwfit = {
  system: () => get<any>("/api/hwfit/system"),
  models: (opts: { limit?: number; fit_only?: boolean; search?: string } = {}) =>
    get<{ system: any; models: HwfitModel[]; error?: string }>(
      `/api/hwfit/models${qs({ limit: opts.limit ?? 24, fit_only: opts.fit_only ? "true" : undefined, search: opts.search })}`,
    ),
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
    postForm("/api/model-endpoints", {
      name,
      base_url,
      api_key,
      skip_probe: "false",
      require_models: "false",
      model_type: "llm",
      endpoint_kind: "auto",
    }),
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

// ---------- notes ----------
export interface Note {
  id: string;
  title: string;
  content?: string | null;
  items?: { text: string; done?: boolean }[] | null;
  note_type?: string;
  color?: string | null;
  label?: string | null;
  pinned?: boolean;
  archived?: boolean;
  due_date?: string | null;
  updated_at?: string;
  created_at?: string;
}
export const notes = {
  list: (archived = false) => get<{ notes: Note[] }>(`/api/notes${qs({ archived })}`),
  create: (body: Partial<Note> & { title?: string }) => postJson<Note>("/api/notes", body),
  update: (id: string, body: Partial<Note>) => putJson<Note>(`/api/notes/${id}`, body),
  remove: (id: string) => del(`/api/notes/${id}`),
  pin: (id: string) => postJson(`/api/notes/${id}/pin`, {}),
  archive: (id: string) => postJson(`/api/notes/${id}/archive`, {}),
  toggleItem: (id: string, index: number) => postJson(`/api/notes/${id}/items/${index}/toggle`, {}),
};

// ---------- tasks ----------
export interface Task {
  id: string;
  name: string;
  prompt?: string | null;
  task_type?: string;
  action?: string | null;
  schedule?: string | null;
  scheduled_time?: string | null;
  scheduled_date?: string | null;
  status?: string;
  next_run?: string | null;
  last_run?: string | null;
  last_run_status?: string | null;
  notifications_enabled?: boolean;
  model?: string | null;
  created_at?: string;
}
export const tasks = {
  list: () => get<{ tasks: Task[] }>("/api/tasks"),
  create: (body: Record<string, unknown>) => postJson<Task>("/api/tasks", body),
  update: (id: string, body: Record<string, unknown>) => putJson<Task>(`/api/tasks/${id}`, body),
  remove: (id: string) => del(`/api/tasks/${id}`),
  pause: (id: string) => postJson(`/api/tasks/${id}/pause`, {}),
  resume: (id: string) => postJson(`/api/tasks/${id}/resume`, {}),
  run: (id: string) => postJson(`/api/tasks/${id}/run`, {}),
  stop: (id: string) => postJson(`/api/tasks/${id}/stop`, {}),
  runs: (id: string) => get<{ runs: any[] }>(`/api/tasks/${id}/runs`),
  recentRuns: () => get<{ runs: any[] }>("/api/tasks/runs/recent"),
};

// ---------- calendar ----------
export interface CalEvent {
  uid: string;
  id?: string;
  summary: string;
  dtstart: string;
  dtend?: string | null;
  all_day?: boolean;
  description?: string;
  location?: string;
  color?: string | null;
  calendar_href?: string;
  calendar_id?: string;
}
export interface CalendarCal {
  id: string;
  name: string;
  color?: string;
  href?: string;
}
export const calendar = {
  calendars: () => get<{ calendars: CalendarCal[] }>("/api/calendar/calendars"),
  events: (start: string, end: string, cal?: string) =>
    get<{ events: CalEvent[] }>(`/api/calendar/events${qs({ start, end, calendar: cal })}`),
  createEvent: (body: Partial<CalEvent> & { summary: string; dtstart: string }) =>
    postJson<CalEvent>("/api/calendar/events", body),
  updateEvent: (uid: string, body: Partial<CalEvent>) => putJson<CalEvent>(`/api/calendar/events/${uid}`, body),
  deleteEvent: (uid: string) => del(`/api/calendar/events/${uid}`),
  createCalendar: (name: string, color = "#e06c75") =>
    postForm("/api/calendar/calendars", { name, color }),
};

// ---------- memory ----------
export interface MemoryItem {
  id: string;
  text: string;
  category?: string;
  categories?: string[];
  source?: string;
  pinned?: boolean;
  timestamp?: number;
  uses?: number;
  session_id?: string;
}
export const memory = {
  list: () => get<{ memory: MemoryItem[] }>("/api/memory"),
  add: (text: string, category = "fact") => postForm("/api/memory/add", { text, category, source: "user" }),
  update: (id: string, body: { text?: string; category?: string }) => putJson(`/api/memory/${id}`, body),
  remove: (id: string) => del(`/api/memory/${id}`),
  pin: (id: string) => postJson(`/api/memory/${id}/pin`, {}),
  search: (query: string) => postForm<{ memories: MemoryItem[] }>("/api/memory/search", { query }),
};

// ---------- skills ----------
export interface Skill {
  id?: string;
  name?: string;
  title?: string;
  description?: string;
  problem?: string;
  solution?: string;
  category?: string;
  tags?: string[];
  status?: string;
  confidence?: number;
  uses?: number;
}
export const skills = {
  list: () => get<{ skills: Skill[]; count: number }>("/api/skills"),
  add: (body: { title: string; problem: string; solution: string; tags?: string }) =>
    postJson("/api/skills/add", {
      title: body.title,
      problem: body.problem,
      solution: body.solution,
      tags: body.tags ? body.tags.split(",").map((t) => t.trim()).filter(Boolean) : [],
    }),
  remove: (id: string) => del(`/api/skills/${id}`),
  importUrl: (url: string) => postJson("/api/skills/import-from-url", { url }),
};

// ---------- gallery ----------
export interface GalleryImage {
  id: string;
  filename: string;
  url: string;
  prompt?: string;
  caption?: string;
  model?: string;
  tags?: string;
  ai_tags?: string;
  favorite?: boolean;
  created_at?: string;
  width?: number;
  height?: number;
}
export const gallery = {
  library: (opts: { search?: string; favorites?: boolean; offset?: number; limit?: number } = {}) =>
    get<{ items: GalleryImage[]; total: number; tags: string[]; models: string[] }>(
      `/api/gallery/library${qs({ search: opts.search, favorites: opts.favorites ? "true" : undefined, offset: opts.offset ?? 0, limit: opts.limit ?? 48 })}`,
    ),
  favorite: (id: string) => postJson(`/api/gallery/${id}/favorite`, {}),
  remove: (id: string) => del(`/api/gallery/${id}`),
  upload: async (file: File) => {
    const part = await fileToPart("file", file);
    return postForm("/api/gallery/upload", {}, [part]);
  },
};

// ---------- documents / library ----------
export interface LibraryDoc {
  id: string;
  title: string;
  language?: string;
  preview?: string;
  current_content?: string;
  version_count?: number;
  session_id?: string | null;
  session_name?: string | null;
  updated_at?: string;
  created_at?: string;
  archived?: boolean;
}
export const documents = {
  library: (opts: { search?: string; language?: string; offset?: number; limit?: number } = {}) =>
    get<{ documents: LibraryDoc[]; total: number; languages: any }>(
      `/api/documents/library${qs({ search: opts.search, language: opts.language, offset: opts.offset ?? 0, limit: opts.limit ?? 50 })}`,
    ),
  get: (id: string) => get<LibraryDoc>(`/api/document/${id}`),
  create: (title: string, content = "", language?: string) =>
    postJson<LibraryDoc>("/api/document", { title, content, language: language || null, session_id: null }),
  save: (id: string, content: string) => putJson(`/api/document/${id}`, { content }),
  rename: (id: string, title: string) => patchJson(`/api/document/${id}`, { title }),
  remove: (id: string) => del(`/api/document/${id}`),
  archive: (id: string) => postJson(`/api/document/${id}/archive`, {}),
};

// ---------- research ----------
export interface ResearchItem {
  id: string;
  query: string;
  status?: string;
  source_count?: number;
  duration?: string;
  started_at?: number;
  completed_at?: number;
  thumbnail?: string;
  archived?: boolean;
}
export const research = {
  library: () => get<{ research: ResearchItem[]; total: number }>("/api/research/library"),
  start: (query: string, opts: { max_time?: number; model?: string } = {}) =>
    postJson<{ session_id: string; status: string; query: string }>("/api/research/start", {
      query,
      max_time: opts.max_time ?? 300,
      model: opts.model,
    }),
  status: (id: string) => get<any>(`/api/research/status/${id}`),
  detail: (id: string) => get<any>(`/api/research/detail/${id}`),
  report: (id: string) => get<any>(`/api/research/report/${id}`),
  cancel: (id: string) => postJson(`/api/research/cancel/${id}`, {}),
  remove: (id: string) => del(`/api/research/${id}`),
  active: () => get<any>("/api/research/active"),
};

// ---------- compare ----------
export const compare = {
  start: (opts: {
    prompt: string;
    model_a: string;
    model_b: string;
    endpoint_a?: string;
    endpoint_b?: string;
    endpoint_a_id?: string;
    endpoint_b_id?: string;
    is_blind?: boolean;
  }) =>
    postForm<{
      id: string;
      session_left: string;
      session_right: string;
      mapping?: any;
    }>("/api/compare/start", {
      prompt: opts.prompt,
      model_a: opts.model_a,
      model_b: opts.model_b,
      endpoint_a: opts.endpoint_a || "",
      endpoint_b: opts.endpoint_b || "",
      endpoint_a_id: opts.endpoint_a_id || "",
      endpoint_b_id: opts.endpoint_b_id || "",
      is_blind: opts.is_blind === false ? "false" : "true",
    }),
  vote: (id: string, winner: string) => postForm(`/api/compare/${id}/vote`, { winner }),
  history: () => get<{ comparisons?: any[]; history?: any[] }>("/api/compare/history"),
  remove: (id: string) => del(`/api/compare/${id}`),
};

// ---------- email ----------
export interface EmailAccount {
  id: string;
  name: string;
  from_address?: string;
  display_name?: string;
  is_default?: boolean;
  enabled?: boolean;
  imap_host?: string;
}
export interface EmailMsg {
  uid: string;
  subject?: string;
  from?: string;
  from_name?: string;
  date?: string;
  seen?: boolean;
  flagged?: boolean;
  snippet?: string;
  preview?: string;
  has_attachments?: boolean;
}
export const email = {
  accounts: () => get<{ accounts: EmailAccount[] }>("/api/email/accounts"),
  list: (opts: { folder?: string; filter?: string; limit?: number; offset?: number; account_id?: string } = {}) =>
    get<{ emails: EmailMsg[]; total: number; folder: string }>(
      `/api/email/list${qs({
        folder: opts.folder || "INBOX",
        filter: opts.filter || "all",
        limit: opts.limit ?? 40,
        offset: opts.offset ?? 0,
        account_id: opts.account_id,
      })}`,
    ),
  read: (uid: string, folder = "INBOX") => get<any>(`/api/email/read/${uid}${qs({ folder })}`),
  send: (body: { to: string; subject: string; body: string; cc?: string; account_id?: string }) =>
    postJson("/api/email/send", body),
  markRead: (uid: string, folder = "INBOX") => postForm(`/api/email/mark-read/${uid}`, { folder }),
  markUnread: (uid: string, folder = "INBOX") => postForm(`/api/email/mark-unread/${uid}`, { folder }),
  remove: (uid: string, folder = "INBOX") =>
    request({ method: "DELETE", path: `/api/email/delete/${uid}${qs({ folder })}` }),
  unread: () => get<{ unread?: number; count?: number }>("/api/email/unread-state"),
  folders: (account_id?: string) => get<{ folders?: any[] }>(`/api/email/folders${qs({ account_id })}`),
  createAccount: (body: Record<string, unknown>) => postJson("/api/email/accounts", body),
  updateAccount: (id: string, body: Record<string, unknown>) => putJson(`/api/email/accounts/${id}`, body),
  removeAccount: (id: string) => del(`/api/email/accounts/${id}`),
  testAccount: (body: Record<string, unknown>) => postJson("/api/email/accounts/test", body),
  setDefaultAccount: (id: string) => postJson(`/api/email/accounts/${id}/set-default`, {}),
  reply: (uid: string, body: { to?: string; subject?: string; body: string; folder?: string; account_id?: string }) =>
    postJson(`/api/email/send`, { ...body, in_reply_to: uid }),
};

// ---------- search ----------
export const search = {
  web: (query: string) => postForm<{ context: string; sources: any[]; error?: string }>("/api/search", { query }),
  providers: () => get<{ id: string; label: string; available: boolean }[] | { providers: any[] }>("/api/search/providers"),
  config: () => get<any>("/api/search/config"),
};

// ---------- voice ----------
export const voice = {
  transcribe: async (file: File) => {
    const part = await fileToPart("file", file);
    return postForm<{ text?: string; transcript?: string }>("/api/stt/transcribe", {}, [part]);
  },
  speak: (text: string) => postJson<{ audio?: string; base64?: string }>("/api/tts/synthesize", { text, format: "base64" }),
};

// ---------- prefs ----------
export const prefs = {
  get: (key: string) => get<any>(`/api/prefs/${key}`),
  set: (key: string, value: unknown) => putJson(`/api/prefs/${key}`, { value }),
  all: () => get<Record<string, any>>("/api/prefs"),
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
  rag?: boolean;
  incognito?: boolean;
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
  if (o.rag) form.use_rag = "true";
  if (o.incognito) form.incognito = "true";
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
