import { create } from "zustand";
import {
  auth,
  sessions as sessionsApi,
  history as historyApi,
  models as modelsApi,
  sendChat,
  misc,
  browser as browserApi,
  type AuthStatus,
  type Session,
  type ModelItem,
  type BrowserActionLog,
} from "../lib/api";
import type { StreamHandle } from "../lib/ipc";
import { onUnauthorized } from "../lib/ipc";
import { applyDensity, applyFontScale, applyTheme, urlsMatch } from "../lib/ui";

export type Screen = "boot" | "setup" | "login" | "app";
export type AppView =
  | "chat"
  | "browser"
  | "talk"
  | "swarm"
  | "models"
  | "notes"
  | "tasks"
  | "calendar"
  | "memory"
  | "gallery"
  | "library"
  | "research"
  | "compare"
  | "email";
export type Density = "comfortable" | "compact";

const VIEWS: AppView[] = [
  "chat", "browser", "talk", "swarm", "models", "notes", "tasks", "calendar", "memory",
  "gallery", "library", "research", "compare", "email",
];

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
  sticky?: boolean;
}

interface AppState {
  screen: Screen;
  authStatus: AuthStatus | null;
  bootError: string | null;
  version: string;
  engineOffline: boolean;

  sessions: Session[];
  activeSessionId: string | null;
  messages: Record<string, Message[]>;
  loadingHistory: boolean;

  modelItems: ModelItem[];
  route: ModelRoute | null;

  mode: "chat" | "agent";
  web: boolean;
  bash: boolean;
  rag: boolean;
  incognito: boolean;

  streaming: boolean;
  streamHandle: StreamHandle | null;
  runId: string;
  compareBusy: boolean;

  view: AppView;
  sidebarOpen: boolean;
  settingsOpen: boolean;
  settingsTab: string;
  paletteOpen: boolean;
  shortcutsOpen: boolean;
  theme: string;
  fontScale: number;
  density: Density;
  toasts: Toast[];
  contextLimit: number | null;
  libraryDirty: boolean;

  // Embedded Browser State
  browserOpen: boolean;
  browserUrl: string;
  browserTitle: string;
  browserEngine: string;
  browserStatus: string;
  browserStatusMessage: string;
  browserLogs: BrowserActionLog[];
  browserCanBack: boolean;
  browserCanForward: boolean;
  browserSplitRatio: number;
  browserRefreshTick: number;
  browserHtml: string;

  setBrowserOpen: (v: boolean) => void;
  toggleBrowser: () => void;
  setBrowserSplitRatio: (ratio: number) => void;
  loadBrowserState: () => Promise<void>;
  navigateBrowser: (url: string, engine?: string) => Promise<void>;
  searchBrowser: (query: string, engine?: string) => Promise<void>;
  clickBrowser: (target: string) => Promise<void>;
  browserBack: () => Promise<void>;
  browserForward: () => Promise<void>;
  browserReload: () => Promise<void>;

  boot: () => Promise<void>;
  refreshAuth: () => Promise<AuthStatus>;
  logout: () => Promise<void>;
  loadSessions: () => Promise<void>;
  selectSession: (id: string | null) => Promise<void>;
  newChat: () => void;
  deleteSession: (id: string) => Promise<void>;
  bulkDeleteSessions: (ids: string[]) => Promise<void>;
  archiveSession: (id: string) => Promise<void>;
  unarchiveSession: (id: string) => Promise<void>;
  starSession: (id: string, on: boolean) => Promise<void>;
  setSessionFolder: (id: string, folder: string) => Promise<void>;
  renameSession: (id: string, name: string) => Promise<void>;
  loadModels: (refresh?: boolean) => Promise<void>;
  setRoute: (r: ModelRoute) => void;
  selectModel: (r: ModelRoute) => Promise<void>;
  setMode: (m: "chat" | "agent") => void;
  toggleWeb: () => void;
  toggleBash: () => void;
  toggleRag: () => void;
  toggleIncognito: () => void;
  send: (text: string, attachments?: { id: string; name: string }[], approval?: { id: string; decision: string }) => Promise<void>;
  stop: () => Promise<void>;
  continueReply: () => Promise<void>;
  regenerate: () => Promise<void>;
  editAndResend: (msgId: string, text: string) => Promise<void>;
  forkChat: () => Promise<void>;
  compactChat: () => Promise<void>;
  refreshContext: () => Promise<void>;
  setView: (v: AppView) => void;
  setSidebar: (v: boolean) => void;
  setSettings: (v: boolean, tab?: string) => void;
  setSettingsTab: (tab: string) => void;
  setPalette: (v: boolean) => void;
  setShortcuts: (v: boolean) => void;
  closeOverlays: () => void;
  toggleTheme: () => void;
  setTheme: (t: string) => void;
  setFontScale: (n: number) => void;
  setDensity: (d: Density) => void;
  setCompareBusy: (v: boolean) => void;
  setLibraryDirty: (v: boolean) => void;
  toast: (text: string, kind?: Toast["kind"], sticky?: boolean) => void;
  dismissToast: (id: number) => void;
  pauseToast: (id: number) => void;
}

let msgSeq = 0;
const mid = () => {
  try {
    return crypto.randomUUID();
  } catch {
    return `m${Date.now().toString(36)}${(++msgSeq).toString(36)}`;
  }
};
let toastSeq = 0;
const toastTimers = new Map<number, number>();
let bootInflight: Promise<void> | null = null;

const savedTheme = (() => {
  try {
    return localStorage.getItem("psd.theme") || "dark";
  } catch {
    return "dark";
  }
})();
applyTheme(savedTheme);

function lsGet(key: string, fallback: string) {
  try {
    return localStorage.getItem(key) || fallback;
  } catch {
    return fallback;
  }
}
function lsSet(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
}

function chatCapable(item: ModelItem) {
  const t = (item.model_type || "llm").toLowerCase();
  if (t === "embedding" || t === "tts" || t === "stt" || t === "whisper" || t === "moderation" || t === "rerank") return false;
  return t === "llm" || t === "image" || t === "vision" || t === "multimodal" || t === "vlm" || t === "";
}

function routeValid(r: ModelRoute | null | undefined, items: ModelItem[]) {
  if (!r?.model || !r.endpoint_url) return false;
  return items.some((i) => {
    const models = [...(i.models || []), ...(i.models_extra || [])];
    if (!models.includes(r.model)) return false;
    if (r.endpoint_id && i.endpoint_id) return r.endpoint_id === i.endpoint_id;
    return urlsMatch(i.url, r.endpoint_url);
  });
}

function overlays(open: "settings" | "palette" | "shortcuts" | "none") {
  return {
    settingsOpen: open === "settings",
    paletteOpen: open === "palette",
    shortcutsOpen: open === "shortcuts",
  };
}

export const useApp = create<AppState>((set, getState) => ({
  screen: "boot",
  authStatus: null,
  bootError: null,
  version: "",
  engineOffline: false,

  sessions: [],
  activeSessionId: null,
  messages: {},
  loadingHistory: false,

  modelItems: [],
  route: null,

  mode: (lsGet("psd.mode", "chat") as "chat" | "agent") || "chat",
  web: lsGet("psd.web", "0") === "1",
  bash: lsGet("psd.bash", "0") === "1",
  rag: lsGet("psd.rag", "0") === "1",
  incognito: false,

  streaming: false,
  streamHandle: null,
  runId: "",
  compareBusy: false,

  view: ((): AppView => {
    const v = lsGet("psd.view", "chat") as AppView;
    return VIEWS.includes(v) ? v : "chat";
  })(),
  sidebarOpen: lsGet("psd.sidebar", "1") !== "0",
  settingsOpen: false,
  settingsTab: lsGet("psd.settingsTab", "models"),
  paletteOpen: false,
  shortcutsOpen: false,
  theme: savedTheme,
  fontScale: Number(lsGet("psd.font", "16")) || 16,
  density: (lsGet("psd.density", "comfortable") as Density) || "comfortable",
  toasts: [],
  contextLimit: null,
  libraryDirty: false,

  // Embedded Browser initial state
  browserOpen: false,
  browserUrl: "https://duckduckgo.com",
  browserTitle: "DuckDuckGo — Fast Search (Model's Choice)",
  browserEngine: "duckduckgo",
  browserStatus: "idle",
  browserStatusMessage: "Search Ready",
  browserLogs: [],
  browserCanBack: false,
  browserCanForward: false,
  browserSplitRatio: 50,
  browserRefreshTick: 0,
  browserHtml: "",

  setBrowserOpen: (v) => set({ browserOpen: v }),
  toggleBrowser: () => set((s) => ({ browserOpen: !s.browserOpen })),
  setBrowserSplitRatio: (ratio) => set({ browserSplitRatio: Math.max(25, Math.min(75, ratio)) }),

  loadBrowserState: async () => {
    try {
      const st = await browserApi.getState();
      set((s) => ({
        browserUrl: st.url || s.browserUrl,
        browserTitle: st.title || s.browserTitle,
        browserEngine: st.engine || s.browserEngine,
        browserStatus: st.status || s.browserStatus,
        browserStatusMessage: st.status_message || s.browserStatusMessage,
        browserCanBack: st.can_back,
        browserCanForward: st.can_forward,
        browserLogs: st.action_logs || s.browserLogs,
        browserHtml: st.html || s.browserHtml,
      }));
    } catch {
      /* ignore */
    }
  },

  navigateBrowser: async (url, engine) => {
    set({ browserStatus: "navigating", browserStatusMessage: `Navigating to ${url}...` });
    try {
      const res = await browserApi.navigate(url, engine);
      if (res?.state) {
        set((s) => ({
          browserUrl: res.state.url,
          browserTitle: res.state.title,
          browserEngine: res.state.engine,
          browserStatus: res.state.status,
          browserStatusMessage: res.state.status_message,
          browserCanBack: res.state.can_back,
          browserCanForward: res.state.can_forward,
          browserLogs: res.state.action_logs,
          browserHtml: res.state.html || s.browserHtml,
          browserRefreshTick: s.browserRefreshTick + 1,
        }));
      }
    } catch (e: any) {
      set({ browserStatus: "error", browserStatusMessage: e?.message || "Navigation failed" });
    }
  },

  searchBrowser: async (query, engine) => {
    set({ browserStatus: "searching", browserStatusMessage: `Searching on ${engine || "engine"}...` });
    try {
      const res = await browserApi.search(query, engine);
      if (res?.state) {
        set((s) => ({
          browserUrl: res.state.url,
          browserTitle: res.state.title,
          browserEngine: res.state.engine,
          browserStatus: res.state.status,
          browserStatusMessage: res.state.status_message,
          browserCanBack: res.state.can_back,
          browserCanForward: res.state.can_forward,
          browserLogs: res.state.action_logs,
          browserHtml: res.state.html || s.browserHtml,
          browserRefreshTick: s.browserRefreshTick + 1,
        }));
      }
    } catch (e: any) {
      set({ browserStatus: "error", browserStatusMessage: e?.message || "Search failed" });
    }
  },

  clickBrowser: async (target) => {
    set({ browserStatus: "clicking", browserStatusMessage: `Clicking ${target}...` });
    try {
      const res = await browserApi.click(target);
      if (res?.state) {
        set((s) => ({
          browserUrl: res.state.url,
          browserTitle: res.state.title,
          browserEngine: res.state.engine,
          browserStatus: res.state.status,
          browserStatusMessage: res.state.status_message,
          browserCanBack: res.state.can_back,
          browserCanForward: res.state.can_forward,
          browserLogs: res.state.action_logs,
          browserHtml: res.state.html || s.browserHtml,
          browserRefreshTick: s.browserRefreshTick + 1,
        }));
      }
    } catch (e: any) {
      set({ browserStatus: "error", browserStatusMessage: e?.message || "Click failed" });
    }
  },

  browserBack: async () => {
    try {
      const res = await browserApi.back();
      if (res?.state) {
        set((s) => ({
          browserUrl: res.state.url,
          browserTitle: res.state.title,
          browserCanBack: res.state.can_back,
          browserCanForward: res.state.can_forward,
          browserHtml: res.state.html || s.browserHtml,
          browserRefreshTick: s.browserRefreshTick + 1,
        }));
      }
    } catch {
      /* ignore */
    }
  },

  browserForward: async () => {
    try {
      const res = await browserApi.forward();
      if (res?.state) {
        set((s) => ({
          browserUrl: res.state.url,
          browserTitle: res.state.title,
          browserCanBack: res.state.can_back,
          browserCanForward: res.state.can_forward,
          browserHtml: res.state.html || s.browserHtml,
          browserRefreshTick: s.browserRefreshTick + 1,
        }));
      }
    } catch {
      /* ignore */
    }
  },

  browserReload: async () => {
    try {
      const res = await browserApi.reload();
      if (res?.state) {
        set((s) => ({
          browserHtml: res.state.html || s.browserHtml,
          browserRefreshTick: s.browserRefreshTick + 1,
        }));
      } else {
        set((s) => ({ browserRefreshTick: s.browserRefreshTick + 1 }));
      }
    } catch {
      /* ignore */
    }
  },

  toast: (text, kind = "info", sticky = false) => {
    const id = ++toastSeq;
    set((s) => ({ toasts: [...s.toasts, { id, kind, text, sticky }] }));
    if (!sticky) {
      const t = window.setTimeout(() => getState().dismissToast(id), 4500);
      toastTimers.set(id, t);
    }
  },
  dismissToast: (id) => {
    const t = toastTimers.get(id);
    if (t) {
      clearTimeout(t);
      toastTimers.delete(id);
    }
    set((s) => ({ toasts: s.toasts.filter((x) => x.id !== id) }));
  },
  pauseToast: (id) => {
    const t = toastTimers.get(id);
    if (t) {
      clearTimeout(t);
      toastTimers.delete(id);
    }
  },

  boot: async () => {
    if (bootInflight) return bootInflight;
    bootInflight = (async () => {
      applyFontScale(getState().fontScale);
      applyDensity(getState().density);
      onUnauthorized(() => {
        const st = getState();
        if (st.screen === "app") {
          st.streamHandle?.cancel();
          set({
            screen: "login",
            streaming: false,
            streamHandle: null,
            settingsOpen: false,
            paletteOpen: false,
          });
          st.toast("Session expired — please sign in again", "error");
        }
      });
      try {
        const [st, ver] = await Promise.all([auth.status(), misc.version().catch(() => ({ version: "" }))]);
        set({ authStatus: st, version: ver.version || "", bootError: null, engineOffline: false });
        if (!st.configured) set({ screen: "setup" });
        else if (!st.authenticated) set({ screen: "login" });
        else {
          set({ screen: "app" });
          await Promise.all([getState().loadSessions(), getState().loadModels()]);
        }
      } catch (e: any) {
        set({ bootError: e?.message || String(e), engineOffline: true });
      }
    })();
    try {
      await bootInflight;
    } finally {
      bootInflight = null;
    }
  },

  refreshAuth: async () => {
    const st = await auth.status();
    set({ authStatus: st, engineOffline: false });
    if (st.authenticated) {
      set({ screen: "app" });
      await Promise.all([getState().loadSessions(), getState().loadModels()]);
    } else set({ screen: st.configured ? "login" : "setup" });
    return st;
  },

  logout: async () => {
    getState().streamHandle?.cancel();
    await auth.logout().catch(() => {});
    set({
      screen: "login",
      sessions: [],
      messages: {},
      activeSessionId: null,
      streaming: false,
      streamHandle: null,
      ...overlays("none"),
    });
  },

  loadSessions: async () => {
    try {
      const list = await sessionsApi.list();
      set({ sessions: Array.isArray(list) ? list : [] });
    } catch (e: any) {
      if (e?.status !== 401) getState().toast(e?.message || "Could not load chats", "error");
    }
  },

  selectSession: async (id) => {
    if (id && id === getState().activeSessionId) {
      set({ view: "chat", ...overlays("none") });
      return;
    }
    if (getState().streaming) await getState().stop();
    set({
      activeSessionId: id,
      view: "chat",
      sidebarOpen: window.innerWidth > 900 ? getState().sidebarOpen : false,
    });
    if (!id) return;
    const sess = getState().sessions.find((s) => s.id === id);
    if (sess?.model && sess.endpoint_url) {
      const next: ModelRoute = {
        model: sess.model,
        endpoint_id: sess.endpoint_id || "",
        endpoint_url: sess.endpoint_url,
      };
      const cur = getState().route;
      if (!cur || cur.model !== next.model || !urlsMatch(cur.endpoint_url, next.endpoint_url)) {
        getState().setRoute(next);
      }
    }
    if (getState().messages[id]) {
      getState().refreshContext();
      return;
    }
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
      getState().refreshContext();
    } catch (e: any) {
      getState().toast(e?.message || "Could not load history", "error");
    } finally {
      set({ loadingHistory: false });
    }
  },

  newChat: () => {
    void (async () => {
      if (getState().streaming) await getState().stop();
      set({ activeSessionId: null, view: "chat", sidebarOpen: window.innerWidth > 900 ? getState().sidebarOpen : false });
    })();
  },

  deleteSession: async (id) => {
    const sess = getState().sessions.find((s) => s.id === id);
    if (sess?.is_important) {
      getState().toast("Unstar this chat before deleting it", "error");
      return;
    }
    try {
      await sessionsApi.remove(id);
      set((s) => {
        const messages = { ...s.messages };
        delete messages[id];
        return {
          sessions: s.sessions.filter((x) => x.id !== id),
          messages,
          activeSessionId: s.activeSessionId === id ? null : s.activeSessionId,
        };
      });
    } catch (e: any) {
      getState().toast(e?.message || "Delete failed", "error");
    }
  },

  bulkDeleteSessions: async (ids) => {
    const starred = getState().sessions.filter((s) => ids.includes(s.id) && s.is_important);
    if (starred.length) getState().toast(`${starred.length} starred chat(s) skipped`, "info");
    const drop = ids.filter((id) => !getState().sessions.find((s) => s.id === id)?.is_important);
    try {
      await sessionsApi.bulkDelete(drop);
      set((s) => {
        const messages = { ...s.messages };
        for (const id of drop) delete messages[id];
        return {
          sessions: s.sessions.filter((x) => !drop.includes(x.id)),
          messages,
          activeSessionId: drop.includes(s.activeSessionId || "") ? null : s.activeSessionId,
        };
      });
    } catch (e: any) {
      getState().toast(e?.message || "Bulk delete failed", "error");
      getState().loadSessions();
    }
  },

  archiveSession: async (id) => {
    try {
      await sessionsApi.archive(id);
      set((s) => ({
        sessions: s.sessions.filter((x) => x.id !== id),
        activeSessionId: s.activeSessionId === id ? null : s.activeSessionId,
      }));
    } catch (e: any) {
      getState().toast(e?.message || "Archive failed", "error");
    }
  },

  unarchiveSession: async (id) => {
    try {
      await sessionsApi.unarchive(id);
      await getState().loadSessions();
      await getState().selectSession(id);
    } catch (e: any) {
      getState().toast(e?.message || "Restore failed", "error");
    }
  },

  starSession: async (id, on) => {
    set((s) => ({ sessions: s.sessions.map((x) => (x.id === id ? { ...x, is_important: on } : x)) }));
    try {
      await sessionsApi.important(id, on);
    } catch (e: any) {
      getState().toast(e?.message || "Could not star chat", "error");
      getState().loadSessions();
    }
  },

  setSessionFolder: async (id, folder) => {
    set((s) => ({ sessions: s.sessions.map((x) => (x.id === id ? { ...x, folder } : x)) }));
    try {
      await sessionsApi.setFolder(id, folder);
    } catch (e: any) {
      getState().toast(e?.message || "Couldn't move chat", "error");
      getState().loadSessions();
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
      const items = (list?.items || []).filter(chatCapable);
      set({ modelItems: items });
      const current = getState().route;
      if (routeValid(current, items)) {
        lsSet("psd.route", JSON.stringify(current));
        return;
      }
      const saved = safeJson<ModelRoute>(lsGet("psd.route", ""));
      if (routeValid(saved, items)) {
        set({ route: saved });
        return;
      }
      if (def?.model) {
        const next: ModelRoute = { model: def.model, endpoint_id: def.endpoint_id || "", endpoint_url: def.endpoint_url };
        if (routeValid(next, items) || def.model) {
          set({ route: next });
          lsSet("psd.route", JSON.stringify(next));
          return;
        }
      }
      const first = items.find((i) => i.models.length && !i.offline);
      if (first) {
        const next: ModelRoute = {
          model: first.models[0],
          endpoint_id: first.endpoint_id || "",
          endpoint_url: first.url,
          endpoint_name: first.endpoint_name,
        };
        set({ route: next });
        lsSet("psd.route", JSON.stringify(next));
      } else if (!getState().route) set({ route: null });
    } catch (e: any) {
      if (e?.status !== 401) getState().toast(e?.message || "Could not load models", "error");
    }
  },

  setRoute: (r) => {
    lsSet("psd.route", JSON.stringify(r));
    set({ route: r });
  },

  selectModel: async (r) => {
    const prev = getState().route;
    const prevSessions = getState().sessions;
    getState().setRoute(r);
    const sid = getState().activeSessionId;
    if (!sid) {
      getState().toast(`Using ${r.model.split("/").pop()}`, "success");
      return;
    }
    set((s) => ({
      sessions: s.sessions.map((x) =>
        x.id === sid ? { ...x, model: r.model, endpoint_url: r.endpoint_url, endpoint_id: r.endpoint_id } : x,
      ),
    }));
    try {
      await sessionsApi.setModel(sid, r.model, r.endpoint_id || "", r.endpoint_url);
      getState().refreshContext();
      getState().toast(`Switched to ${r.model.split("/").pop()}`, "success");
    } catch (e: any) {
      if (prev) lsSet("psd.route", JSON.stringify(prev));
      set({ route: prev, sessions: prevSessions });
      getState().toast(e?.message || "Couldn't switch model", "error");
    }
  },

  setMode: (m) => {
    lsSet("psd.mode", m);
    set({ mode: m });
  },
  toggleWeb: () => set((s) => (lsSet("psd.web", s.web ? "0" : "1"), { web: !s.web })),
  toggleBash: () => set((s) => (lsSet("psd.bash", s.bash ? "0" : "1"), { bash: !s.bash })),
  toggleRag: () => set((s) => (lsSet("psd.rag", s.rag ? "0" : "1"), { rag: !s.rag })),
  toggleIncognito: () => set((s) => ({ incognito: !s.incognito })),

  send: async (text, attachments = [], approval) => {
    const st = getState();
    if (st.streaming) {
      st.toast("Already generating a reply", "info");
      return;
    }
    if (st.compareBusy) {
      st.toast("A comparison is still running", "info");
      return;
    }
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
        const name = st.incognito ? "Nobody" : `${base} · ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
        const s = await sessionsApi.create({
          name,
          model: route.model,
          endpoint_url: route.endpoint_url,
          endpoint_id: route.endpoint_id,
        });
        sid = s.id;
        set((x) => ({
          sessions: st.incognito ? x.sessions : [{ ...s, message_count: 0, model: route.model, endpoint_url: route.endpoint_url, endpoint_id: route.endpoint_id }, ...x.sessions],
          activeSessionId: sid,
          messages: { ...x.messages, [sid!]: [] },
          view: "chat",
        }));
      } catch (e: any) {
        st.toast(e?.message || "Could not create chat", "error");
        return;
      }
    }
    const sessionId = sid!;
    const userMsg: Message = { id: mid(), role: "user", content: text, attachments, ts: Date.now() };
    const botMsg: Message = { id: mid(), role: "assistant", content: "", tools: [], streaming: true, ts: Date.now(), model: route.model };
    const add = approval ? [botMsg] : [userMsg, botMsg];
    set((x) => ({ messages: { ...x.messages, [sessionId]: [...(x.messages[sessionId] || []), ...add] }, streaming: true, view: "chat" }));

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
      {
        sessionId,
        message: text,
        mode: st.mode,
        web: st.web,
        bash: st.bash,
        rag: st.rag,
        incognito: st.incognito,
        model: route.model,
        endpointId: route.endpoint_id,
        endpointUrl: route.endpoint_url,
        attachments: attachments.map((a) => a.id),
        toolApproval: approval,
      },
      {
        onOpen: (runId) => set({ runId }),
        onEvent: (ev) => {
          if (ev.delta) {
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
              if (ev.tool?.startsWith("browser_") || ev.tool?.includes("builtin_browser") || ev.tool === "web_search") {
                set({ browserOpen: true, browserStatus: "busy", browserStatusMessage: `Running ${ev.tool}...` });
              }
              break;
            case "browser_action":
            case "browser_state":
              if (ev.browser_state) {
                const bs = ev.browser_state;
                set((s) => ({
                  browserOpen: true,
                  browserUrl: bs.url || s.browserUrl,
                  browserTitle: bs.title || s.browserTitle,
                  browserEngine: bs.engine || s.browserEngine,
                  browserStatus: bs.status || s.browserStatus,
                  browserStatusMessage: bs.status_message || s.browserStatusMessage,
                  browserCanBack: bs.can_back ?? s.browserCanBack,
                  browserCanForward: bs.can_forward ?? s.browserCanForward,
                  browserLogs: bs.action_logs || s.browserLogs,
                  browserHtml: bs.html || s.browserHtml,
                  browserRefreshTick: s.browserRefreshTick + 1,
                }));
              } else if (ev.browser_url) {
                set((s) => ({
                  browserOpen: true,
                  browserUrl: ev.browser_url,
                  browserRefreshTick: s.browserRefreshTick + 1,
                }));
              }
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
          if (!getState().incognito) getState().loadSessions();
          getState().refreshContext();
        },
      },
    );
    set({ streamHandle: handle });
  },

  stop: async () => {
    const { activeSessionId, runId, streamHandle } = getState();
    streamHandle?.cancel();
    if (activeSessionId) await misc.stop(activeSessionId, runId).catch(() => {});
    set((x) => {
      const sid = x.activeSessionId;
      if (!sid) return { streaming: false, streamHandle: null };
      const list = (x.messages[sid] || []).map((m) =>
        m.streaming ? { ...m, streaming: false, tools: (m.tools || []).map((t) => ({ ...t, running: false })) } : m,
      );
      return { streaming: false, streamHandle: null, messages: { ...x.messages, [sid]: list } };
    });
  },

  continueReply: async () => {
    if (getState().streaming) return;
    await getState().send("Please continue from where you left off.");
  },

  regenerate: async () => {
    if (getState().streaming) await getState().stop();
    const sid = getState().activeSessionId;
    if (!sid) return;
    const list = getState().messages[sid] || [];
    const lastUser = [...list].reverse().find((m) => m.role === "user");
    if (!lastUser?.content) {
      getState().toast("Nothing to regenerate", "info");
      return;
    }
    const withoutTail = list[list.length - 1]?.role === "assistant" ? list.slice(0, -1) : list;
    const withoutUser = withoutTail.filter((m) => m.id !== lastUser.id);
    set((s) => ({ messages: { ...s.messages, [sid]: withoutUser } }));
    await getState().send(lastUser.content, lastUser.attachments);
  },

  editAndResend: async (msgId, text) => {
    if (getState().streaming) await getState().stop();
    const sid = getState().activeSessionId;
    if (!sid) return;
    const list = getState().messages[sid] || [];
    const idx = list.findIndex((m) => m.id === msgId);
    if (idx < 0) return;
    const prefix = list.slice(0, idx);
    const route = getState().route;
    try {
      const s = await sessionsApi.create({
        name: (text || "Edited chat").slice(0, 40),
        model: route?.model,
        endpoint_url: route?.endpoint_url,
        endpoint_id: route?.endpoint_id,
      });
      if (prefix.length) {
        await sessionsApi.inject(
          s.id,
          prefix.map((m) => ({ role: m.role, content: m.content })),
        );
      }
      set((x) => ({
        activeSessionId: s.id,
        sessions: [{ ...s, model: route?.model || s.model }, ...x.sessions],
        messages: { ...x.messages, [s.id]: prefix },
        view: "chat",
      }));
      await getState().send(text);
    } catch (e: any) {
      getState().toast(e?.message || "Couldn't edit message", "error");
    }
  },

  forkChat: async () => {
    const sid = getState().activeSessionId;
    const route = getState().route;
    const msgs = sid ? getState().messages[sid] || [] : [];
    try {
      const s = await sessionsApi.create({
        name: "Fork",
        model: route?.model,
        endpoint_url: route?.endpoint_url,
        endpoint_id: route?.endpoint_id,
      });
      if (msgs.length) {
        await sessionsApi.inject(
          s.id,
          msgs.map((m) => ({ role: m.role, content: m.content })),
        );
      }
      set((x) => ({
        activeSessionId: s.id,
        sessions: [{ ...s, name: "Fork", model: route?.model || s.model }, ...x.sessions],
        messages: { ...x.messages, [s.id]: msgs.map((m) => ({ ...m, id: mid(), streaming: false })) },
        view: "chat",
      }));
      getState().toast("Forked into a new chat", "success");
    } catch (e: any) {
      getState().toast(e?.message || "Couldn't fork chat", "error");
    }
  },

  compactChat: async () => {
    const sid = getState().activeSessionId;
    if (!sid) return;
    if (getState().streaming) {
      getState().toast("Stop the reply before compacting", "info");
      return;
    }
    try {
      await sessionsApi.compact(sid);
      set((s) => {
        const messages = { ...s.messages };
        delete messages[sid];
        return { messages };
      });
      await getState().selectSession(sid);
      getState().toast("Older messages summarized", "success");
    } catch (e: any) {
      getState().toast(e?.message || "Couldn't compact this chat", "error");
    }
  },

  refreshContext: async () => {
    const sid = getState().activeSessionId;
    if (!sid) {
      set({ contextLimit: null });
      return;
    }
    try {
      const info = await sessionsApi.contextInfo(sid);
      set({ contextLimit: info.context_length ?? null });
    } catch {
      /* ignore */
    }
  },

  setView: (v) => {
    if (getState().libraryDirty && v !== "library") {
      if (!window.confirm("Leave the library? Unsaved document changes will be lost.")) return;
      set({ libraryDirty: false });
    }
    lsSet("psd.view", v);
    set({ view: v, ...overlays("none") });
  },
  setSidebar: (v) => {
    lsSet("psd.sidebar", v ? "1" : "0");
    set({ sidebarOpen: v });
  },
  setSettings: (v, tab) => {
    if (tab) {
      lsSet("psd.settingsTab", tab);
      set({ settingsTab: tab, ...overlays(v ? "settings" : "none") });
      return;
    }
    set(overlays(v ? "settings" : "none"));
  },
  setSettingsTab: (tab) => {
    lsSet("psd.settingsTab", tab);
    set({ settingsTab: tab });
  },
  setPalette: (v) => set(overlays(v ? "palette" : "none")),
  setShortcuts: (v) => set(overlays(v ? "shortcuts" : "none")),
  closeOverlays: () => set(overlays("none")),
  toggleTheme: () => {
    const next = getState().theme === "dark" || getState().theme === "midnight" || getState().theme === "forest" || getState().theme === "ocean" ? "light" : "dark";
    getState().setTheme(next);
  },
  setTheme: (t) => {
    applyTheme(t);
    lsSet("psd.theme", t);
    set({ theme: t });
  },
  setFontScale: (n) => {
    applyFontScale(n);
    lsSet("psd.font", String(n));
    set({ fontScale: n });
  },
  setDensity: (d) => {
    applyDensity(d);
    lsSet("psd.density", d);
    set({ density: d });
  },
  setCompareBusy: (v) => set({ compareBusy: v }),
  setLibraryDirty: (v) => set({ libraryDirty: v }),
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
