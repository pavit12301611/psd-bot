import { useEffect, useState } from "react";
import { Minus, Square, X, Copy, Sun, Moon, PanelLeft } from "lucide-react";
import { inTauri } from "../lib/ipc";
import { useApp } from "../store/app";

export default function TitleBar({ showSidebarToggle = false }: { showSidebarToggle?: boolean }) {
  const [max, setMax] = useState(false);
  const theme = useApp((s) => s.theme);
  const toggleTheme = useApp((s) => s.toggleTheme);
  const sidebarOpen = useApp((s) => s.sidebarOpen);
  const setSidebar = useApp((s) => s.setSidebar);

  useEffect(() => {
    if (!inTauri) return;
    let un: (() => void) | undefined;
    import("@tauri-apps/api/window").then(({ getCurrentWindow }) => {
      const w = getCurrentWindow();
      w.isMaximized().then(setMax);
      w.onResized(() => w.isMaximized().then(setMax)).then((u) => (un = u));
    });
    return () => un?.();
  }, []);

  const win = async () => (await import("@tauri-apps/api/window")).getCurrentWindow();

  return (
    <header data-tauri-drag-region className="drag relative z-20 flex h-11 shrink-0 items-center gap-2 px-3 select-none" style={{ borderBottom: "1px solid var(--border)" }}>
      {showSidebarToggle && (
        <button className="icon-btn no-drag h-8 w-8" title="Toggle sidebar" onClick={() => setSidebar(!sidebarOpen)}>
          <PanelLeft size={16} />
        </button>
      )}
      <div data-tauri-drag-region className="flex items-center gap-2 pl-1">
        <img src="/icon.png" alt="" className="h-5 w-5 rounded-md" draggable={false} />
        <span className="text-[13px] font-semibold tracking-tight">psd.ai</span>
      </div>
      <div data-tauri-drag-region className="flex-1" />
      <button className="icon-btn no-drag h-8 w-8" title="Toggle theme" onClick={toggleTheme}>
        {theme === "dark" ? <Sun size={15} /> : <Moon size={15} />}
      </button>
      {inTauri && (
        <div className="no-drag ml-1 flex items-center">
          <button className="icon-btn h-8 w-10 rounded-lg" title="Minimize" onClick={async () => (await win()).minimize()}>
            <Minus size={15} />
          </button>
          <button className="icon-btn h-8 w-10 rounded-lg" title={max ? "Restore" : "Maximize"} onClick={async () => (await win()).toggleMaximize()}>
            {max ? <Copy size={13} /> : <Square size={13} />}
          </button>
          <button
            className="icon-btn h-8 w-10 rounded-lg hover:!bg-red-500 hover:!text-white"
            title="Close"
            onClick={async () => (await win()).close()}
          >
            <X size={16} />
          </button>
        </div>
      )}
    </header>
  );
}
