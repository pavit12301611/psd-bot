import { useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "motion/react";
import {
  ArrowLeft,
  ArrowRight,
  RotateCw,
  Home,
  Lock,
  Search,
  ExternalLink,
  Copy,
  Check,
  ChevronDown,
  Terminal,
  Activity,
  Maximize2,
  Minimize2,
  X,
  Sparkles,
} from "lucide-react";
import { useApp } from "../store/app";
import { openExternal } from "../lib/ui";

interface EngineOption {
  id: string;
  name: string;
  icon: string;
  color: string;
}

const ENGINES: EngineOption[] = [
  { id: "duckduckgo", name: "DuckDuckGo", icon: "🦆", color: "#de5833" },
  { id: "google", name: "Google", icon: "🌐", color: "#4285F4" },
  { id: "bing", name: "Bing", icon: "🔍", color: "#008373" },
  { id: "brave", name: "Brave", icon: "🦁", color: "#FB542B" },
  { id: "ecosia", name: "Ecosia", icon: "🌱", color: "#2BA143" },
  { id: "searxng", name: "SearXNG", icon: "⚡", color: "#3B82F6" },
  { id: "yahoo", name: "Yahoo", icon: "🟣", color: "#6001D2" },
];

export default function EmbeddedBrowser({
  onClose,
  isSplit = false,
}: {
  onClose?: () => void;
  isSplit?: boolean;
}) {
  const url = useApp((s) => s.browserUrl);
  const title = useApp((s) => s.browserTitle);
  const engine = useApp((s) => s.browserEngine);
  const status = useApp((s) => s.browserStatus);
  const statusMessage = useApp((s) => s.browserStatusMessage);
  const logs = useApp((s) => s.browserLogs);
  const canBack = useApp((s) => s.browserCanBack);
  const canForward = useApp((s) => s.browserCanForward);
  const refreshTick = useApp((s) => s.browserRefreshTick);
  const html = useApp((s) => s.browserHtml);
  const navigate = useApp((s) => s.navigateBrowser);
  const search = useApp((s) => s.searchBrowser);
  const goBack = useApp((s) => s.browserBack);
  const goForward = useApp((s) => s.browserForward);
  const reload = useApp((s) => s.browserReload);
  const loadState = useApp((s) => s.loadBrowserState);
  const toast = useApp((s) => s.toast);
  const setView = useApp((s) => s.setView);

  const [inputUrl, setInputUrl] = useState(url);
  const [engineMenuOpen, setEngineMenuOpen] = useState(false);
  const [logsOpen, setLogsOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Sync address bar input with active URL
  useEffect(() => {
    setInputUrl(url);
  }, [url]);

  // Initial load
  useEffect(() => {
    loadState();
  }, []);

  // Keyboard shortcut Ctrl+L / Cmd+L to focus address bar
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "l") {
        e.preventDefault();
        inputRef.current?.focus();
        inputRef.current?.select();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Listen to postMessage from iframe for internal link navigation
  useEffect(() => {
    const handleMsg = (e: MessageEvent) => {
      if (e.data && e.data.type === "browser_navigate" && e.data.url) {
        navigate(e.data.url);
      }
    };
    window.addEventListener("message", handleMsg);
    return () => window.removeEventListener("message", handleMsg);
  }, [navigate]);

  const handleSubmit = (e?: React.FormEvent) => {
    e?.preventDefault();
    const target = inputUrl.trim();
    if (!target) return;
    if (target.startsWith("http://") || target.startsWith("https://") || target.startsWith("about:") || (target.includes(".") && !target.includes(" "))) {
      navigate(target, engine);
    } else {
      search(target, engine);
    }
  };

  const handleCopyUrl = async () => {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
      toast("URL copied to clipboard", "success");
    } catch {
      toast("Could not copy URL", "error");
    }
  };

  const currentEngineObj = ENGINES.find((e) => e.id === engine) || ENGINES[0];
  const isBusy = status === "navigating" || status === "searching" || status === "busy" || status === "clicking";

  return (
    <div
      className="relative flex h-full w-full flex-col overflow-hidden select-text"
      style={{
        background: "var(--bg-elev)",
        borderLeft: isSplit ? "1px solid var(--border)" : "none",
      }}
    >
      {/* ── Top Browser Navigation Chrome ── */}
      <div
        className="flex shrink-0 flex-col gap-1.5 px-3 py-2"
        style={{
          background: "var(--bg)",
          borderBottom: "1px solid var(--border)",
        }}
      >
        {/* Row 1: Nav buttons, Address bar, Engine, Actions */}
        <div className="flex items-center gap-1.5">
          {/* Back / Forward / Reload / Home */}
          <div className="flex items-center gap-0.5">
            <button
              className="icon-btn h-7 w-7"
              disabled={!canBack}
              onClick={() => goBack()}
              title="Back"
              aria-label="Back"
            >
              <ArrowLeft size={14} />
            </button>
            <button
              className="icon-btn h-7 w-7"
              disabled={!canForward}
              onClick={() => goForward()}
              title="Forward"
              aria-label="Forward"
            >
              <ArrowRight size={14} />
            </button>
            <button
              className="icon-btn h-7 w-7"
              onClick={() => reload()}
              title="Reload"
              aria-label="Reload"
            >
              <RotateCw size={13} className={isBusy ? "animate-spin" : ""} />
            </button>
            <button
              className="icon-btn h-7 w-7"
              onClick={() => navigate("about:home")}
              title="Browser Home"
              aria-label="Browser Home"
            >
              <Home size={14} />
            </button>
          </div>

          {/* Search Engine Pill (Model's Choice) */}
          <div className="relative">
            <button
              className="flex h-7 items-center gap-1.5 rounded-lg px-2 text-[12px] font-medium transition-all"
              style={{
                background: "var(--bg-elev)",
                border: "1px solid var(--border)",
                color: "var(--text)",
              }}
              onClick={() => setEngineMenuOpen(!engineMenuOpen)}
              title="Search Engine (Model's choice)"
            >
              <span>{currentEngineObj.icon}</span>
              <span className="hidden sm:inline">{currentEngineObj.name}</span>
              <span
                className="hidden items-center gap-0.5 rounded px-1 text-[10px] md:flex"
                style={{
                  background: "var(--accent-soft)",
                  color: "var(--accent)",
                }}
              >
                <Sparkles size={9} /> Model's Choice
              </span>
              <ChevronDown size={11} style={{ opacity: 0.6 }} />
            </button>

            {/* Engine Selection Dropdown */}
            {engineMenuOpen && (
              <div
                className="glass absolute left-0 top-8 z-50 flex w-48 flex-col rounded-xl p-1 shadow-xl"
                style={{
                  background: "var(--bg-elev)",
                  border: "1px solid var(--border)",
                }}
              >
                <div
                  className="px-2.5 py-1 text-[10px] font-medium uppercase tracking-wider"
                  style={{ color: "var(--muted)" }}
                >
                  Search Engine Choice
                </div>
                {ENGINES.map((eng) => (
                  <button
                    key={eng.id}
                    className="flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[12px] transition-colors hover:bg-black/5 dark:hover:bg-white/5"
                    style={{
                      fontWeight: engine === eng.id ? 600 : 400,
                      color: engine === eng.id ? "var(--accent)" : "var(--text)",
                    }}
                    onClick={() => {
                      useApp.setState({ browserEngine: eng.id });
                      setEngineMenuOpen(false);
                      toast(`Search engine set to ${eng.name}`, "info");
                    }}
                  >
                    <span>{eng.icon}</span>
                    <span className="flex-1">{eng.name}</span>
                    {engine === eng.id && <Check size={12} />}
                  </button>
                ))}
                <div
                  className="border-t px-2.5 py-1 text-[10px] italic"
                  style={{
                    borderColor: "var(--border)",
                    color: "var(--muted)",
                  }}
                >
                  ✨ AI model chooses automatically based on prompt
                </div>
              </div>
            )}
          </div>

          {/* Address Bar */}
          <form
            onSubmit={handleSubmit}
            className="flex min-w-0 flex-1 items-center gap-1.5 rounded-lg px-2.5 py-1 transition-all"
            style={{
              background: "var(--bg-elev)",
              border: "1px solid var(--border)",
              boxShadow: "inset 0 1px 2px rgba(0,0,0,0.05)",
            }}
          >
            {url.startsWith("https") ? (
              <Lock size={12} className="shrink-0 text-emerald-500" />
            ) : (
              <Search size={12} className="shrink-0 text-muted" style={{ opacity: 0.6 }} />
            )}
            <input
              ref={inputRef}
              type="text"
              value={inputUrl}
              onChange={(e) => setInputUrl(e.target.value)}
              placeholder="Search or enter web address (Ctrl+L)..."
              className="min-w-0 flex-1 bg-transparent text-[12px] outline-none"
              style={{ color: "var(--text)" }}
            />
            {isBusy ? (
              <span className="shrink-0 text-[10px] font-medium text-amber-500">
                Loading...
              </span>
            ) : (
              <button
                type="submit"
                className="shrink-0 text-[11px] font-semibold transition-colors hover:opacity-80"
                style={{ color: "var(--accent)" }}
              >
                Go
              </button>
            )}
          </form>

          {/* Quick Actions */}
          <div className="flex items-center gap-0.5">
            <button
              className="icon-btn h-7 w-7"
              onClick={handleCopyUrl}
              title="Copy URL"
              aria-label="Copy URL"
            >
              {copied ? <Check size={13} className="text-emerald-500" /> : <Copy size={13} />}
            </button>
            {url && !url.startsWith("about:") && (
              <button
                className="icon-btn h-7 w-7"
                onClick={() => openExternal(url)}
                title="Open in external browser"
                aria-label="Open in external browser"
              >
                <ExternalLink size={13} />
              </button>
            )}
            {isSplit ? (
              <button
                className="icon-btn h-7 w-7"
                onClick={() => setView("browser")}
                title="Full View"
                aria-label="Full View"
              >
                <Maximize2 size={13} />
              </button>
            ) : (
              <button
                className="icon-btn h-7 w-7"
                onClick={() => setView("chat")}
                title="Chat Split View"
                aria-label="Chat Split View"
              >
                <Minimize2 size={13} />
              </button>
            )}
            {onClose && (
              <button
                className="icon-btn h-7 w-7"
                onClick={onClose}
                title="Close Browser View"
                aria-label="Close Browser View"
              >
                <X size={14} />
              </button>
            )}
          </div>
        </div>

        {/* Row 2: Live Status Banner & Title */}
        <div className="flex items-center justify-between text-[11px]">
          <div className="flex min-w-0 items-center gap-2 truncate" style={{ color: "var(--muted)" }}>
            <span
              className="flex h-2 w-2 shrink-0 rounded-full"
              style={{
                background: isBusy ? "#f59e0b" : "#10b981",
                boxShadow: isBusy ? "0 0 8px #f59e0b" : "0 0 8px #10b981",
              }}
            />
            <span className="font-semibold text-[11px]" style={{ color: "var(--text)" }}>
              {isBusy ? statusMessage || "Model active..." : "⚡ Fast Engine Ready"}
            </span>
            <span className="truncate opacity-75">— {title || url}</span>
          </div>

          {/* Action Log Drawer Toggle */}
          <button
            className="flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium transition-colors hover:bg-black/5 dark:hover:bg-white/5"
            style={{ color: logsOpen ? "var(--accent)" : "var(--muted)" }}
            onClick={() => setLogsOpen(!logsOpen)}
          >
            <Activity size={12} />
            <span>AI Actions ({logs.length})</span>
          </button>
        </div>

        {/* Row 3: Instant Quick Access & Search Chips */}
        <div className="flex items-center gap-1.5 overflow-x-auto py-0.5 text-[11px]">
          <span className="shrink-0 font-semibold text-[10px]" style={{ color: "var(--accent)" }}>
            ⚡ Fast AI Links:
          </span>
          <button
            className="shrink-0 rounded-md px-2 py-0.5 transition-colors hover:bg-black/5 dark:hover:bg-white/5"
            style={{ background: "var(--bg-elev)", border: "1px solid var(--border)", color: "var(--text)" }}
            onClick={() => search("Latest AI models benchmarks 2026", engine)}
          >
            🤖 Top AI Models
          </button>
          <button
            className="shrink-0 rounded-md px-2 py-0.5 transition-colors hover:bg-black/5 dark:hover:bg-white/5"
            style={{ background: "var(--bg-elev)", border: "1px solid var(--border)", color: "var(--text)" }}
            onClick={() => navigate("https://github.com/trending")}
          >
            🐙 GitHub Trending
          </button>
          <button
            className="shrink-0 rounded-md px-2 py-0.5 transition-colors hover:bg-black/5 dark:hover:bg-white/5"
            style={{ background: "var(--bg-elev)", border: "1px solid var(--border)", color: "var(--text)" }}
            onClick={() => search("Breaking Tech and Science News today", engine)}
          >
            🚀 Tech News
          </button>
          <button
            className="shrink-0 rounded-md px-2 py-0.5 transition-colors hover:bg-black/5 dark:hover:bg-white/5"
            style={{ background: "var(--bg-elev)", border: "1px solid var(--border)", color: "var(--text)" }}
            onClick={() => navigate("https://docs.python.org/3/")}
          >
            🐍 Python Docs
          </button>
          <button
            className="shrink-0 rounded-md px-2 py-0.5 transition-colors hover:bg-black/5 dark:hover:bg-white/5"
            style={{ background: "var(--bg-elev)", border: "1px solid var(--border)", color: "var(--text)" }}
            onClick={() => navigate("https://huggingface.co/models")}
          >
            🤗 Hugging Face
          </button>
        </div>
      </div>

      {/* ── Main Browser Viewport (iframe) ── */}
      <div className="relative flex-1 overflow-hidden" style={{ background: "var(--bg-sunken)" }}>
        {/* Live Loading Bar */}
        {isBusy && (
          <div
            className="absolute left-0 top-0 z-20 h-0.5 w-full overflow-hidden"
            style={{ background: "var(--accent-soft)" }}
          >
            <div
              className="h-full w-1/3 animate-pulse"
              style={{
                background: "var(--accent)",
                animation: "pulse 1s infinite alternate",
              }}
            />
          </div>
        )}

        <iframe
          ref={iframeRef}
          key={`browser-frame-${refreshTick}`}
          srcDoc={html || "<!DOCTYPE html><html><body style='background:#0f1117;color:#e7e9f2;display:flex;align-items:center;justify-content:center;height:95vh;font-family:sans-serif;'>Loading fast browser...</body></html>"}
          title="Embedded Browser Content"
          sandbox="allow-same-origin allow-scripts allow-forms allow-popups"
          className="h-full w-full border-none"
          style={{
            background: "transparent",
          }}
        />

        {/* Collapsible Action Logs Drawer */}
        <AnimatePresence>
          {logsOpen && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 220, opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              className="absolute bottom-0 left-0 right-0 z-30 flex flex-col border-t shadow-2xl"
              style={{
                background: "var(--bg-elev)",
                borderColor: "var(--border)",
              }}
            >
              <div
                className="flex items-center justify-between border-b px-3 py-1.5 text-[11px] font-semibold"
                style={{ borderColor: "var(--border)", color: "var(--muted)" }}
              >
                <div className="flex items-center gap-1.5">
                  <Terminal size={12} />
                  <span>Real-time AI Browser Actions Log</span>
                </div>
                <button
                  className="icon-btn h-5 w-5"
                  onClick={() => setLogsOpen(false)}
                >
                  <X size={11} />
                </button>
              </div>

              <div className="flex-1 overflow-y-auto p-2 font-mono text-[11px]">
                {logs.length === 0 ? (
                  <div className="p-4 text-center text-xs" style={{ color: "var(--muted)" }}>
                    No browser actions recorded yet. Tell the model to search or browse!
                  </div>
                ) : (
                  <div className="flex flex-col gap-1">
                    {logs.map((log) => (
                      <div
                        key={log.id}
                        className="flex items-start gap-2 rounded px-2 py-1 transition-colors hover:bg-black/5 dark:hover:bg-white/5"
                      >
                        <span style={{ color: "var(--muted)" }}>[{log.timestamp}]</span>
                        <span
                          className="rounded px-1 text-[10px] font-semibold uppercase"
                          style={{
                            background:
                              log.action === "search"
                                ? "rgba(59, 130, 246, 0.15)"
                                : log.action === "navigate"
                                ? "rgba(16, 185, 129, 0.15)"
                                : log.action === "click"
                                ? "rgba(245, 158, 11, 0.15)"
                                : "rgba(224, 108, 117, 0.15)",
                            color:
                              log.action === "search"
                                ? "#60a5fa"
                                : log.action === "navigate"
                                ? "#34d399"
                                : log.action === "click"
                                ? "#fbbf24"
                                : "var(--accent)",
                          }}
                        >
                          {log.action}
                        </span>
                        <span className="flex-1 break-all" style={{ color: "var(--text)" }}>
                          {log.target}
                          {log.engine && (
                            <span className="ml-1 opacity-70">
                              (via {log.engine})
                            </span>
                          )}
                        </span>
                        <span
                          className="shrink-0 text-[10px]"
                          style={{
                            color: log.outcome === "done" || log.outcome === "ok" ? "#10b981" : "#f59e0b",
                          }}
                        >
                          {log.outcome}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}
