import { useEffect } from "react";
import { motion, AnimatePresence } from "motion/react";
import { X } from "lucide-react";
import { useApp } from "./store/app";
import TitleBar from "./components/TitleBar";
import Boot from "./components/Boot";
import Auth from "./components/Auth";
import Sidebar from "./components/Sidebar";
import Chat from "./components/Chat";
import Settings from "./components/Settings";
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

  // If the engine dies (or is restarted) mid-session, drop back to the boot
  // screen; <Boot/> re-runs the auth check once the sidecar is ready again.
  useEffect(
    () =>
      onBackendStatus((s) => {
        if (s === "error" || s === "starting") useApp.setState({ screen: "boot", streaming: false, streamHandle: null });
      }),
    [],
  );

  // Disable the WebView's default context menu / drag-drop navigation.
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
        <Toasts />
      </main>
    </div>
  );
}

function Workspace() {
  const sidebarOpen = useApp((s) => s.sidebarOpen);
  return (
    <>
      <AnimatePresence initial={false}>
        {sidebarOpen && (
          <motion.div key="sb" className="relative z-10 h-full overflow-hidden" initial={{ width: 0, opacity: 0 }} animate={{ width: 272, opacity: 1 }} exit={{ width: 0, opacity: 0 }} transition={{ type: "spring", stiffness: 380, damping: 36 }}>
            <Sidebar />
          </motion.div>
        )}
      </AnimatePresence>
      <Chat />
    </>
  );
}
