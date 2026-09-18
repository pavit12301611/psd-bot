import { X } from "lucide-react";
import { useApp } from "../store/app";
import Overlay from "./Overlay";
import { metaLabel } from "../lib/ui";

export default function Shortcuts() {
  const open = useApp((s) => s.shortcutsOpen);
  const close = () => useApp.getState().setShortcuts(false);
  const rows = [
    { keys: `${metaLabel}+K`, action: "Command palette / search" },
    { keys: `${metaLabel}+N`, action: "New chat" },
    { keys: `${metaLabel}+B`, action: "Toggle chat sidebar" },
    { keys: `${metaLabel}+,`, action: "Open settings" },
    { keys: `${metaLabel}+L`, action: "Focus the composer" },
    { keys: `${metaLabel}+Shift+A`, action: "Toggle agent / chat mode" },
    { keys: `${metaLabel}+Shift+W`, action: "Toggle web search" },
    { keys: `${metaLabel}+Shift+T`, action: "Talk to psd.ai (voice mode)" },
    { keys: "Space (hold)", action: "Push to talk, on the Talk screen" },
    { keys: "Esc", action: "Close dialogs / stop generating" },
    { keys: "Enter", action: "Send message" },
    { keys: "Shift+Enter", action: "New line in composer" },
    { keys: "?", action: "This shortcuts overlay" },
  ];
  return (
    <Overlay open={open} onClose={close} labelledBy="shortcuts-title">
      <div className="w-full max-w-md overflow-hidden rounded-3xl" style={{ background: "var(--bg-elev)", border: "1px solid var(--border)", boxShadow: "var(--shadow)" }}>
        <div className="flex items-center px-5 py-4" style={{ borderBottom: "1px solid var(--border)" }}>
          <h2 id="shortcuts-title" className="text-[15px] font-semibold">Keyboard shortcuts</h2>
          <button className="icon-btn ml-auto" onClick={close} aria-label="Close"><X size={16} /></button>
        </div>
        <div className="flex flex-col px-2 py-2">
          {rows.map((r) => (
            <div key={r.keys} className="flex items-center justify-between rounded-xl px-3 py-2 text-[13px]">
              <span>{r.action}</span>
              <kbd className="rounded-md px-2 py-0.5 text-[11px] font-medium" style={{ background: "var(--bg-sunken)", border: "1px solid var(--border)" }}>{r.keys}</kbd>
            </div>
          ))}
        </div>
      </div>
    </Overlay>
  );
}
