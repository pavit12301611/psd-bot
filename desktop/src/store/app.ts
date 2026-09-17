import { create } from "zustand";
import {
  auth,
  sessions as sessionsApi,
  history as historyApi,
  models as modelsApi,
  sendChat,
  misc,
  type AuthStatus,
  type Session,
  type ModelItem,
} from "../lib/api";
import type { StreamHandle } from "../lib/ipc";

export type Screen = "boot" | "setup" | "login" | "app";

export interface ToolCall {
  id: string;
  tool: string;
  command?: string;
  output?: string;
  exit_code?: number | null;
  running: boolean;
  elapsed?: number;
  tail?: string;
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  thinking?: string;
  tools?: ToolCall[];
  sources?: { title?: string; url?: string; snippet?: string }[];
  attachments?: { id: string; name: string }[];
  model?: string;
  streaming?: boolean;
  error?: string;
  askUser?: any;
  metrics?: any;
  ts: number;
}

export interface ModelRoute {
  model: string;
  endpoint_id: string;
  endpoint_url: string;
  endpoint_name?: string;
}

export interface Toast {
  id: number;
  kind: "info" | "error" | "success";
  text: string;
}

interface AppState {
  screen: Screen;
  authStatus: AuthStatus | null;
  bootError: string | null;
  version: string;

  sessions: Session[];
  activeSessionId: string | null;
  messages: Record<string, Message[]>;
  loadingHistory: boolean;

  modelItems: ModelItem[];
  route: ModelRoute | null;

  mode: "chat" | "agent";
  web: boolean;
  bash: boolean;

  streaming: boolean;
  streamHandle: StreamHandle | null;
  runId: string;

  sidebarOpen: boolean;
  settingsOpen: boolean;
  theme: "dark" | "light";
  toasts: Toast[];

  // actions
  boot: () => Promise<void>;
  refreshAuth: () => Promise<AuthStatus>;
  logout: () => Promise<void>;
  loadSessions: () => Promise<void>;
  selectSession: (id: string | null) => Promise<void>;
  newChat: () => void;
  deleteSession: (id: string) => Promise<void>;
  renameSession: (id: string, name: string) => Promise<void>;
  loadModels: (refresh?: boolean) => Promise<void>;
  setRoute: (r: ModelRoute) => void;
  setMode: (m: "chat" | "agent") => void;
  toggleWeb: () => void;
  toggleBash: () => void;
  send: (text: string, attachments?: { id: string; name: string }[], approval?: { id: string; decision: string }) => Promise<void>;
  stop: () => Promise<void>;
  setSidebar: (v: boolean) => void;
  setSettings: (v: boolean) => void;
  toggleTheme: () => void;
  toast: (text: string, kind?: Toast["kind"]) => void;
  dismissToast: (id: number) => void;
}

let msgSeq = 0;
const mid = () => `m${Date.now().toString(36)}${(++msgSeq).toString(36)}`;
let toastSeq = 0;

const savedTheme = (localStorage.getItem("psd.theme") as "dark" | "light") || "dark";
document.documentElement.dataset.theme = savedTheme;

export const useApp = create<AppState>((set, getState) => ({
  screen: "boot",
  authStatus: null,
  bootError: null,
  version: "",

  sessions: [],
  activeSessionId: null,
  messages: {},
  loadingHistory: false,

  modelItems: [],
  route: null,

  mode: (localStorage.getItem("psd.mode") as "chat" | "agent") || "chat",
  web: localStorage.getItem("psd.web") === "1",
  bash: localStorage.getItem("psd.bash") === "1",

  streaming: false,
  streamHandle: null,
  runId: "",

  sidebarOpen: true,
  settingsOpen: false,
  theme: savedTheme,
  toasts: [],

  toast: (text, kind = "info") => {
    const id = ++toastSeq;
    set((s) => ({ toasts: [...s.toasts, { id, kind, text }] }));
    setTimeout(() => getState().dismissToast(id), 4500);
  },
  dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),

  boot: async () => {
    try {
      const [st, ver] = await Promise.all([auth.status(), misc.version().catch(() => ({ version: "" }))]);
      set({ authStatus: st, version: ver.version || "" });
      if (!st.configured) set({ screen: "setup" });
      else if (!st.authenticated) set({ screen: "login" });
      else {
        set({ screen: "app" });
        await Promise.all([getState().loadSessions(), getState().loadModels()]);
      }
    } catch (e: any) {
      set({ bootError: e?.message || String(e) });
    }
  },

  refreshAuth: async () => {
    const st = await auth.status();
    set({ authStatus: st });
    if (st.authenticated) {
      set({ screen: "app" });
      await Promise.all([getState().loadSessions(), getState().loadModels()]);
    } else set({ screen: st.configured ? "login" : "setup" });
    return st;
  },

  logout: async () => {
    getState().streamHandle?.cancel();
    await auth.logout().catch(() => {});
    set({ screen: "login", sessions: [], messages: {}, activeSessionId: null, streaming: false, streamHandle: null, settingsOpen: false });
  },

  loadSessions: async () => {
    try {
      const list = await sessionsApi.list();
      set({ sessions: Array.isArray(list) ? list : [] });
    } catch (e: any) {
      getState().toast(e?.message || "Could not load chats", "error");
    }
  },

  selectSession: async (id) => {
    if (getState().streaming) return;
    set({ activeSessionId: id, sidebarOpen: window.innerWidth > 900 ? getState().sidebarOpen : false });
    if (!id || getState().messages[id]) return;
    set({ loadingHistory: true });
    try {
      const h = await historyApi.get(id);
      const msgs: Message[] = [];
      for (const e of h.history || []) {
        if (e.role !== "user" && e.role !== "assistant") continue;
        let content = "";
        if (typeof e.content === "string") content = e.content;
        else if (Array.isArray(e.content)) content = e.content.map((p: any) => (p?.type === "text" ? p.text : "")).join("");
        const { text, thinking } = splitThinking(content);
        msgs.push({
          id: mid(),
          role: e.role,
          content: text,
          thinking,
          model: e.metadata?.model,
          ts: e.metadata?.timestamp ? Date.parse(e.metadata.timestamp) : Date.now(),
          attachments: e.metadata?.attachments,
        });
      }
      set((s) => ({ messages: { ...s.messages, [id]: msgs } }));
    } catch (e: any) {
      getState().toast(e?.message || "Could not load history", "error");
    } finally {
      set({ loadingHistory: false });
    }
  },

  newChat: () => {
    if (getState().streaming) return;
    set({ activeSessionId: null, sidebarOpen: window.innerWidth > 900 ? getState().sidebarOpen : false });
  },

  deleteSession: async (id) => {
    try {
      await sessionsApi.remove(id);
      set((s) => {
        const messages = { ...s.messages };
        delete messages[id];
        return { sessions: s.sessions.filter((x) => x.id !== id), messages, activeSessionId: s.activeSessionId === id ? null : s.activeSessionId };
      });
    } catch (e: any) {
      getState().toast(e?.message || "Delete failed", "error");
    }
  },

  renameSession: async (id, name) => {
    try {
      await sessionsApi.rename(id, name);
      set((s) => ({ sessions: s.sessions.map((x) => (x.id === id ? { ...x, name } : x)) }));
    } catch (e: any) {
      getState().toast(e?.message || "Rename failed", "error");
    }
  },

  loadModels: async (refresh = false) => {
    try {
      const [list, def] = await Promise.all([modelsApi.list(refresh), modelsApi.defaultChat().catch(() => null)]);
      const items = (list?.items || []).filter((i) => (i.model_type || "llm") === "llm");
      set({ modelItems: items });
      const saved = safeJson<ModelRoute>(localStorage.getItem("psd.route"));
      const valid = (r: ModelRoute | null) =>
        !!r && items.some((i) => (i.endpoint_id === r.endpoint_id || i.url === r.endpoint_url) && [...i.models, ...(i.models_extra || [])].includes(r.model));
      if (valid(saved)) set({ route: saved });
      else if (def && def.model && valid({ model: def.model, endpoint_id: def.endpoint_id, endpoint_url: def.endpoint_url }))
        set({ route: { model: def.model, endpoint_id: def.endpoint_id, endpoint_url: def.endpoint_url } });
      else {
        const first = items.find((i) => i.models.length && !i.offline);
        if (first) set({ route: { model: first.models[0], endpoint_id: first.endpoint_id || "", endpoint_url: first.url, endpoint_name: first.endpoint_name } });
        else if (!getState().route) set({ route: null });
      }
    } catch (e: any) {
      if (e?.status !== 401) getState().toast(e?.message || "Could not load models", "error");
    }
  },

  setRoute: (r) => {
    localStorage.setItem("psd.route", JSON.stringify(r));
    set({ route: r });
  },
  setMode: (m) => {
    localStorage.setItem("psd.mode", m);
    set({ mode: m });
  },
  toggleWeb: () => set((s) => (localStorage.setItem("psd.web", s.web ? "0" : "1"), { web: !s.web })),
  toggleBash: () => set((s) => (localStorage.setItem("psd.bash", s.bash ? "0" : "1"), { bash: !s.bash })),

  send: async (text, attachments = [], approval) => {
    const st = getState();
    if (st.streaming) return;
    if (!text.trim() && !attachments.length && !approval) return;
    const route = st.route;
    if (!route) {
      st.toast("No model available yet. Add a model endpoint in Settings.", "error");
      return;
    }

    let sid = st.activeSessionId;
    if (!sid) {
      try {
        const base = route.model.split("/").pop() || "chat";
        const s = await sessionsApi.create({ name: `${base} · ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`, model: route.model, endpoint_url: route.endpoint_url, endpoint_id: route.endpoint_id });
        sid = s.id;
        set((x) => ({ sessions: [{ ...s, message_count: 0 }, ...x.sessions], activeSessionId: sid, messages: { ...x.messages, [sid!]: [] } }));
      } catch (e: any) {
        st.toast(e?.message || "Could not create chat", "error");
        return;
      }
    }
    const sessionId = sid!;
    const userMsg: Message = { id: mid(), role: "user", content: text, attachments, ts: Date.now() };
    const botMsg: Message = { id: mid(), role: "assistant", content: "", tools: [], streaming: true, ts: Date.now(), model: route.model };
    const add = approval ? [botMsg] : [userMsg, botMsg];
    set((x) => ({ messages: { ...x.messages, [sessionId]: [...(x.messages[sessionId] || []), ...add] }, streaming: true }));

    const patch = (fn: (m: Message) => void) =>
      set((x) => {
        const list = x.messages[sessionId] || [];
        const idx = list.findIndex((m) => m.id === botMsg.id);
        if (idx < 0) return {};
        const copy = { ...list[idx] };
        fn(copy);
        const nl = list.slice();
        nl[idx] = copy;
        return { messages: { ...x.messages, [sessionId]: nl } };
      });

    let raw = "";
    let rawThinking = "";
    const handle = sendChat(
      { sessionId, message: text, mode: st.mode, web: st.web, bash: st.bash, model: route.model, endpointId: route.endpoint_id, endpointUrl: route.endpoint_url, attachments: attachments.map((a) => a.id), toolApproval: approval },
      {
        onOpen: (runId) => set({ runId }),
        onEvent: (ev) => {
          if (ev.delta) {
            // The backend tags reasoning tokens with `thinking: true`; some
            // models still emit literal <think> tags inline, so handle both.
            if (ev.thinking) rawThinking += ev.delta;
            else raw += ev.delta;
            const { text: t, thinking } = splitThinking(raw);
            const think = (rawThinking + (thinking ? "\n" + thinking : "")).trim();
            patch((m) => {
              m.content = t;
              m.thinking = think || undefined;
            });
            return;
          }
          if (ev.error) {
            patch((m) => (m.error = String(ev.error)));
            return;
          }
          switch (ev.type) {
            case "model_info":
            case "model_actual":
              if (ev.model) patch((m) => (m.model = ev.model));
              break;
            case "tool_start":
              patch((m) => (m.tools = [...(m.tools || []), { id: mid(), tool: ev.tool, command: ev.full_command || ev.command, running: true }]));
              break;
            case "tool_progress":
              patch((m) => {
                const t = (m.tools || []).slice();
                const i = findLast(t, (x) => x.tool === ev.tool && x.running);
                if (i >= 0) t[i] = { ...t[i], elapsed: ev.elapsed_s, tail: ev.tail };
                m.tools = t;
              });
              break;
            case "tool_output":
              patch((m) => {
                const t = (m.tools || []).slice();
                const i = findLast(t, (x) => x.tool === ev.tool && x.running);
                const done = { tool: ev.tool, command: ev.command, output: ev.output, exit_code: ev.exit_code, running: false };
                if (i >= 0) t[i] = { ...t[i], ...done };
                else t.push({ id: mid(), ...done });
                m.tools = t;
                if (ev.ask_user) m.askUser = ev.ask_user;
              });
              break;
            case "ask_user":
              patch((m) => (m.askUser = ev.data));
              break;
            case "web_sources":
            case "rag_sources":
            case "research_sources":
              patch((m) => (m.sources = [...(m.sources || []), ...(Array.isArray(ev.data) ? ev.data : [])]));
              break;
            case "metrics":
              patch((m) => (m.metrics = ev.data));
              break;
            case "generated_image":
              if (ev.image_url || ev.data?.image_url) {
                const u = ev.image_url || ev.data.image_url;
                raw += `\n\n![generated image](${u})\n`;
                patch((m) => (m.content = splitThinking(raw).text));
              }
              break;
            default:
              break;
          }
        },
        onError: (msg) => {
          patch((m) => {
            m.error = msg;
            m.streaming = false;
          });
          set({ streaming: false, streamHandle: null });
        },
        onDone: () => {
          patch((m) => {
            m.streaming = false;
            m.tools = (m.tools || []).map((t) => ({ ...t, running: false }));
          });
          set({ streaming: false, streamHandle: null, runId: "" });
          getState().loadSessions();
        },
      },
    );
    set({ streamHandle: handle });
  },

  stop: async () => {
    const { activeSessionId, runId, streamHandle } = getState();
    if (activeSessionId) await misc.stop(activeSessionId, runId).catch(() => {});
    streamHandle?.cancel();
    set((x) => {
      const sid = x.activeSessionId;
      if (!sid) return { streaming: false, streamHandle: null };
      const list = (x.messages[sid] || []).map((m) => (m.streaming ? { ...m, streaming: false, tools: (m.tools || []).map((t) => ({ ...t, running: false })) } : m));
      return { streaming: false, streamHandle: null, messages: { ...x.messages, [sid]: list } };
    });
  },

  setSidebar: (v) => set({ sidebarOpen: v }),
  setSettings: (v) => set({ settingsOpen: v }),
  toggleTheme: () =>
    set((s) => {
      const theme = s.theme === "dark" ? "light" : "dark";
      localStorage.setItem("psd.theme", theme);
      document.documentElement.dataset.theme = theme;
      return { theme };
    }),
}));

function safeJson<T>(s: string | null): T | null {
  if (!s) return null;
  try {
    return JSON.parse(s);
  } catch {
    return null;
  }
}

function findLast<T>(arr: T[], pred: (t: T) => boolean): number {
  for (let i = arr.length - 1; i >= 0; i--) if (pred(arr[i])) return i;
  return -1;
}

/** Pull `<think>…</think>` (open or closed) out of the model output. */
export function splitThinking(raw: string): { text: string; thinking?: string } {
  if (!raw.includes("<think")) return { text: raw };
  let thinking = "";
  const text = raw
    .replace(/<think(?:ing)?>([\s\S]*?)<\/think(?:ing)?>/g, (_, t) => {
      thinking += t;
      return "";
    })
    .replace(/<think(?:ing)?>([\s\S]*)$/, (_, t) => {
      thinking += t;
      return "";
    });
  return { text: text.replace(/^\s+/, ""), thinking: thinking.trim() || undefined };
}
