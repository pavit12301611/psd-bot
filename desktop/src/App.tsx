import { useEffect } from "react";
import { motion, AnimatePresence } from "motion/react";
import { X } from "lucide-react";
import { useApp, type AppView } from "./store/app";
import TitleBar from "./components/TitleBar";
import Boot from "./components/Boot";
import Auth from "./components/Auth";
import Sidebar from "./components/Sidebar";
import Chat from "./components/Chat";
import Settings from "./components/Settings";
import NavRail from "./components/NavRail";
import CommandPalette from "./components/CommandPalette";
import Shortcuts from "./components/Shortcuts";
import Notes from "./components/Notes";
import Tasks from "./components/Tasks";
import CalendarView from "./components/CalendarView";
import Memory from "./components/Memory";
import Gallery from "./components/Gallery";
import Library from "./components/Library";
import Research from "./components/Research";
import Compare from "./components/Compare";
import Email from "./components/Email";
import ModelsHub from "./components/ModelsHub";
import { inTauri, onBackendStatus } from "./lib/ipc";

function Toasts() {
  const toasts = useApp((s) => s.toasts);
  const dismiss = useApp((s) => s.dismissToast);
  return (
    <div className="pointer-events-none absolute bottom-5 right-5 z-[60] flex flex-col gap-2">
      <AnimatePresence>
        {toasts.map((t) => (
          <motion.div key={t.id} layout initial={{ opacity: 0, x: 24 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: 24 }} className="glass pointer-events-auto flex max-w-sm items-center gap-3 rounded-2xl px-4 py-3 text-[13px]" style={{ borderLeft: `3px solid ${t.kind === "error" ? "#f87171" : t.kind === "success" ? "#34d399" : "var(--accent)"}`, boxShadow: "var(--shadow)" }}>
            <span className="flex-1">{t.text}</span>
            <button className="icon-btn h-6 w-6" onClick={() => dismiss(t.id)}>
              <X size={13} />
            </button>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}

export default function App() {
  const screen = useApp((s) => s.screen);

  useEffect(
    () =>
      onBackendStatus((s) => {
        if (s === "error" || s === "starting") useApp.setState({ screen: "boot", streaming: false, streamHandle: null });
      }),
    [],
  );

  useEffect(() => {
    if (!inTauri) return;
    const stop = (e: Event) => {
      const t = e.target as HTMLElement;
      if (e.type === "contextmenu" && (t.closest(".selectable") || t.closest("input,textarea"))) return;
      e.preventDefault();
    };
    document.addEventListener("contextmenu", stop);
    document.addEventListener("dragover", stop);
    document.addEventListener("drop", stop);
    return () => {
      document.removeEventListener("contextmenu", stop);
      document.removeEventListener("dragover", stop);
      document.removeEventListener("drop", stop);
    };
  }, []);

  useEffect(() => {
    const isTyping = (el: EventTarget | null) => {
      const t = el as HTMLElement | null;
      if (!t) return false;
      const tag = t.tagName;
      return tag === "INPUT" || tag === "TEXTAREA" || t.isContentEditable;
    };
    const onKey = (e: KeyboardEvent) => {
      const meta = e.metaKey || e.ctrlKey;
      const st = useApp.getState();
      if (meta && e.key.toLowerCase() === "k") {
        e.preventDefault();
        st.setPalette(!st.paletteOpen);
        return;
      }
      if (meta && e.key.toLowerCase() === "n") {
        e.preventDefault();
        st.newChat();
        return;
      }
      if (meta && e.key.toLowerCase() === "b") {
        e.preventDefault();
        st.setSidebar(!st.sidebarOpen);
        return;
      }
      if (meta && e.key === ",") {
        e.preventDefault();
        st.setSettings(!st.settingsOpen);
        return;
      }
      if (meta && e.shiftKey && e.key.toLowerCase() === "a") {
        e.preventDefault();
        st.setMode(st.mode === "agent" ? "chat" : "agent");
        return;
      }
      if (meta && e.shiftKey && e.key.toLowerCase() === "w") {
        e.preventDefault();
        st.toggleWeb();
        return;
      }
      if (e.key === "Escape") {
        if (st.shortcutsOpen) return st.setShortcuts(false);
        if (st.paletteOpen) return st.setPalette(false);
        if (st.settingsOpen) return st.setSettings(false);
        if (st.streaming) return st.stop();
      }
      if (e.key === "?" && !isTyping(e.target) && !meta) {
        e.preventDefault();
        st.setShortcuts(!st.shortcutsOpen);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="relative flex h-full flex-col overflow-hidden rounded-none" style={{ background: "var(--bg)" }}>
      <div className="ambient" />
      <TitleBar showSidebarToggle={screen === "app"} />
      <main className="relative flex min-h-0 flex-1">
        <AnimatePresence mode="wait">
          {screen === "boot" && (
            <motion.div key="boot" className="h-full w-full" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0, scale: 0.98 }} transition={{ duration: 0.25 }}>
              <Boot />
            </motion.div>
          )}
          {(screen === "setup" || screen === "login") && (
            <motion.div key={screen} className="h-full w-full" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0, scale: 0.98 }} transition={{ duration: 0.25 }}>
              <Auth mode={screen} />
            </motion.div>
          )}
          {screen === "app" && (
            <motion.div key="app" className="flex h-full w-full" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.3 }}>
              <Workspace />
            </motion.div>
          )}
        </AnimatePresence>
        <Settings />
        <CommandPalette />
        <Shortcuts />
        <Toasts />
      </main>
    </div>
  );
}

function Workspace() {
  const sidebarOpen = useApp((s) => s.sidebarOpen);
  const view = useApp((s) => s.view);
  const offline = useApp((s) => s.engineOffline);
  return (
    <>
      <NavRail />
      {view === "chat" && (
        <AnimatePresence initial={false}>
          {sidebarOpen && (
            <motion.div key="sb" className="relative z-10 h-full overflow-hidden" initial={{ width: 0, opacity: 0 }} animate={{ width: 272, opacity: 1 }} exit={{ width: 0, opacity: 0 }} transition={{ type: "spring", stiffness: 380, damping: 36 }}>
              <Sidebar />
            </motion.div>
          )}
        </AnimatePresence>
      )}
      <div className="relative flex min-w-0 flex-1 flex-col">
        {offline && (
          <div className="z-20 px-4 py-1.5 text-center text-[12px]" style={{ background: "var(--accent-soft)", color: "var(--accent)", borderBottom: "1px solid var(--border)" }}>
            Engine isn’t connected — the full interface is still here. Start the local backend to load your data.
          </div>
        )}
        <Feature view={view} />
      </div>
    </>
  );
}

function Feature({ view }: { view: AppView }) {
  switch (view) {
    case "models":
      return <ModelsHub />;
    case "notes":
      return <Notes />;
    case "tasks":
      return <Tasks />;
    case "calendar":
      return <CalendarView />;
    case "memory":
      return <Memory />;
    case "gallery":
      return <Gallery />;
    case "library":
      return <Library />;
    case "research":
      return <Research />;
    case "compare":
      return <Compare />;
    case "email":
      return <Email />;
    default:
      return <Chat />;
  }
}
