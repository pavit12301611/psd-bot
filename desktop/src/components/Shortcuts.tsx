import { motion, AnimatePresence } from "motion/react";
import { X } from "lucide-react";
import { useApp } from "../store/app";

const ROWS: { keys: string; action: string }[] = [
  { keys: "Ctrl+K", action: "Command palette / search" },
  { keys: "Ctrl+N", action: "New chat" },
  { keys: "Ctrl+B", action: "Toggle chat sidebar" },
  { keys: "Ctrl+,", action: "Open settings" },
  { keys: "Ctrl+L", action: "Focus the composer" },
  { keys: "Ctrl+Shift+A", action: "Toggle agent / chat mode" },
  { keys: "Ctrl+Shift+W", action: "Toggle web search" },
  { keys: "Esc", action: "Close dialogs / stop generating" },
  { keys: "Enter", action: "Send message" },
  { keys: "Shift+Enter", action: "New line in composer" },
  { keys: "?", action: "This shortcuts overlay" },
];

export default function Shortcuts() {
  const open = useApp((s) => s.shortcutsOpen);
  const close = () => useApp.getState().setShortcuts(false);
  return (
    <AnimatePresence>
      {open && (
        <motion.div className="absolute inset-0 z-[70] flex items-center justify-center p-6" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} style={{ background: "rgba(0,0,0,.45)", backdropFilter: "blur(6px)" }} onMouseDown={(e) => e.target === e.currentTarget && close()}>
          <motion.div initial={{ opacity: 0, scale: 0.96 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0, scale: 0.96 }} className="w-full max-w-md overflow-hidden rounded-3xl" style={{ background: "var(--bg-elev)", border: "1px solid var(--border)", boxShadow: "var(--shadow)" }}>
            <div className="flex items-center px-5 py-4" style={{ borderBottom: "1px solid var(--border)" }}>
              <h2 className="text-[15px] font-semibold">Keyboard shortcuts</h2>
              <button className="icon-btn ml-auto" onClick={close}><X size={16} /></button>
            </div>
            <div className="flex flex-col px-2 py-2">
              {ROWS.map((r) => (
                <div key={r.keys} className="flex items-center justify-between rounded-xl px-3 py-2 text-[13px]">
                  <span>{r.action}</span>
                  <kbd className="rounded-md px-2 py-0.5 text-[11px] font-medium" style={{ background: "var(--bg-sunken)", border: "1px solid var(--border)" }}>{r.keys}</kbd>
                </div>
              ))}
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
