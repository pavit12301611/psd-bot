import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { motion, AnimatePresence } from "motion/react";
import {
  ArrowDown, Sparkles, Code2, Lightbulb, PenLine, HardDrive, StickyNote, ListTodo, CalendarDays, Telescope,
  Copy, Trash2, Download, Star, MoreHorizontal, EyeOff, GitFork, Minimize2, Archive, Folder,
} from "lucide-react";
import { useApp, type Message } from "../store/app";
import { sessions as sessionsApi, uploads } from "../lib/api";
import { downloadText, MAX_UPLOAD_BYTES } from "../lib/ui";
import MessageView from "./Message";
import Composer from "./Composer";
import ModelPicker from "./ModelPicker";

const NO_MESSAGES: Message[] = [];
const PAGE = 80;

const SUGGESTIONS = [
  { icon: <Sparkles size={15} />, title: "Explain something", text: "Explain how large language models generate text, in simple terms." },
  { icon: <Code2 size={15} />, title: "Write code", text: "Write a Python script that renames all files in a folder to lowercase." },
  { icon: <Lightbulb size={15} />, title: "Brainstorm", text: "Give me 10 creative names for a personal productivity app." },
  { icon: <PenLine size={15} />, title: "Draft an email", text: "Draft a polite email asking my professor for a deadline extension." },
];

const QUICK: { icon: React.ReactNode; title: string; view: "notes" | "tasks" | "calendar" | "research" | "models" }[] = [
  { icon: <HardDrive size={15} />, title: "Models", view: "models" },
  { icon: <StickyNote size={15} />, title: "Notes", view: "notes" },
  { icon: <ListTodo size={15} />, title: "Tasks", view: "tasks" },
  { icon: <CalendarDays size={15} />, title: "Calendar", view: "calendar" },
  { icon: <Telescope size={15} />, title: "Research", view: "research" },
];

export default function Chat() {
  const sid = useApp((s) => s.activeSessionId);
  const messages = useApp((s) => (s.activeSessionId ? s.messages[s.activeSessionId] || NO_MESSAGES : NO_MESSAGES));
  const loading = useApp((s) => s.loadingHistory);
  const send = useApp((s) => s.send);
  const session = useApp((s) => s.sessions.find((x) => x.id === s.activeSessionId));
  const user = useApp((s) => s.authStatus?.username);
  const setView = useApp((s) => s.setView);
  const deleteSession = useApp((s) => s.deleteSession);
  const toast = useApp((s) => s.toast);
  const incognito = useApp((s) => s.incognito);
  const toggleIncognito = useApp((s) => s.toggleIncognito);
  const forkChat = useApp((s) => s.forkChat);
  const compactChat = useApp((s) => s.compactChat);
  const archiveSession = useApp((s) => s.archiveSession);
  const starSession = useApp((s) => s.starSession);
  const setSessionFolder = useApp((s) => s.setSessionFolder);
  const continueReply = useApp((s) => s.continueReply);
  const streaming = useApp((s) => s.streaming);
  const contextLimit = useApp((s) => s.contextLimit);
  const route = useApp((s) => s.route);
  const scroller = useRef<HTMLDivElement>(null);
  const [stuck, setStuck] = useState(true);
  const [menu, setMenu] = useState(false);
  const [folderDraft, setFolderDraft] = useState("");
  const [showAll, setShowAll] = useState(false);
  const menuBtn = useRef<HTMLButtonElement>(null);
  const [menuPos, setMenuPos] = useState({ top: 0, right: 0 });

  useEffect(() => {
    if (stuck) scroller.current?.scrollTo({ top: scroller.current.scrollHeight });
  }, [messages, stuck]);

  useEffect(() => {
    setStuck(true);
    setShowAll(false);
    requestAnimationFrame(() => scroller.current?.scrollTo({ top: scroller.current.scrollHeight }));
  }, [sid]);

  useEffect(() => {
    if (!menu) return;
    const h = (e: MouseEvent) => {
      const t = e.target as HTMLElement;
      if (t.closest("[data-chat-menu]") || menuBtn.current?.contains(t)) return;
      setMenu(false);
    };
    window.addEventListener("mousedown", h);
    return () => window.removeEventListener("mousedown", h);
  }, [menu]);

  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    setStuck(el.scrollHeight - el.scrollTop - el.clientHeight < 80);
  };

  const usedChars = useMemo(() => messages.reduce((n, m) => n + (m.content?.length || 0) + (m.thinking?.length || 0), 0), [messages]);
  const ctxPct = contextLimit ? Math.min(100, Math.round((usedChars / 4 / contextLimit) * 100)) : null;

  const visible = showAll || messages.length <= PAGE ? messages : messages.slice(-PAGE);
  const hidden = messages.length - visible.length;

  const hour = new Date().getHours();
  const greet = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  const last = messages[messages.length - 1];
  const canContinue = !!sid && !streaming && last?.role === "assistant" && !last.streaming && !last.error;

  const dropFiles = async (list: FileList | null) => {
    if (!list?.length) return;
    const files: { id: string; name: string }[] = [];
    for (const f of Array.from(list)) {
      if (f.size > MAX_UPLOAD_BYTES) {
        toast(`${f.name} is larger than 25 MB`, "error");
        continue;
      }
      try {
        const r = await uploads.send(f, sid || undefined);
        for (const x of r.files || []) files.push({ id: x.id, name: x.name || x.filename || f.name });
      } catch (e: any) {
        toast(e?.message || "Upload failed", "error");
      }
    }
    if (files.length) window.dispatchEvent(new CustomEvent("psd-attach", { detail: files }));
  };

  const exportChat = async (fmt: "md" | "json" | "txt" | "html") => {
    if (!session) return;
    try {
      const r = await sessionsApi.export(session.id, fmt);
      const name = `${session.name || "chat"}.${fmt}`;
      downloadText(name, r.text || "", fmt === "json" ? "application/json" : fmt === "html" ? "text/html" : "text/plain");
      toast("Exported", "success");
    } catch (e: any) {
      toast(e.message || "Export failed", "error");
    }
    setMenu(false);
  };

  return (
    <section
      className="relative flex h-full min-w-0 flex-1 flex-col overflow-hidden"
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        dropFiles(e.dataTransfer.files);
      }}
    >
      <div className="relative z-10 flex h-12 shrink-0 items-center gap-3 overflow-hidden px-4" style={{ borderBottom: "1px solid transparent" }}>
        <ModelPicker />
        {session && (
          <span className="min-w-0 truncate text-[13px]" style={{ color: "var(--muted)" }}>
            {session.name}
          </span>
        )}
        {route?.model && (
          <span className="hidden truncate text-[11px] sm:inline" style={{ color: "var(--muted)" }} title={route.endpoint_url}>
            {route.model.split("/").pop()}
          </span>
        )}
        {ctxPct != null && (
          <span className="hidden items-center gap-1 text-[11px] md:flex" style={{ color: ctxPct > 85 ? "#f87171" : "var(--muted)" }} title="Estimated context use">
            {ctxPct}% context
          </span>
        )}
        <div className="ml-auto flex shrink-0 items-center gap-0.5">
          <button className="pill h-8" data-on={incognito} title="Nobody mode — nothing is saved" onClick={toggleIncognito}>
            <EyeOff size={12} /> Nobody
          </button>
          {session && (
            <div className="relative">
              <button
                ref={menuBtn}
                className="icon-btn h-8 w-8"
                title="Chat actions"
                aria-haspopup="menu"
                aria-expanded={menu}
                onClick={() => {
                  const r = menuBtn.current?.getBoundingClientRect();
                  if (r) setMenuPos({ top: r.bottom + 6, right: window.innerWidth - r.right });
                  setMenu((v) => !v);
                }}
              >
                <MoreHorizontal size={16} />
              </button>
              {menu &&
                createPortal(
                  <div
                    data-chat-menu
                    className="glass fixed z-[85] w-52 overflow-hidden rounded-xl py-1"
                    style={{ top: menuPos.top, right: menuPos.right, boxShadow: "var(--shadow)" }}
                  >
                    <MenuItem icon={<Star size={13} />} label={session.is_important ? "Unstar" : "Star"} onClick={() => { starSession(session.id, !session.is_important); setMenu(false); }} />
                    <MenuItem icon={<GitFork size={13} />} label="Fork chat" onClick={() => { forkChat(); setMenu(false); }} />
                    <MenuItem icon={<Minimize2 size={13} />} label="Compact history" onClick={() => { compactChat(); setMenu(false); }} />
                    <MenuItem icon={<Archive size={13} />} label="Archive" onClick={() => { archiveSession(session.id); setMenu(false); }} />
                    <MenuItem icon={<Copy size={13} />} label="Copy chat" onClick={() => { const t = messages.map((m) => `**${m.role}:** ${m.content}`).join("\n\n"); navigator.clipboard.writeText(t); toast("Copied", "success"); setMenu(false); }} />
                    <MenuItem icon={<Download size={13} />} label="Export markdown" onClick={() => exportChat("md")} />
                    <MenuItem icon={<Download size={13} />} label="Export JSON" onClick={() => exportChat("json")} />
                    <div className="px-3 py-1.5">
                      <div className="mb-1 flex items-center gap-1 text-[11px]" style={{ color: "var(--muted)" }}>
                        <Folder size={11} /> Folder
                      </div>
                      <input
                        className="input h-7 text-[12px]"
                        placeholder="Move to folder"
                        defaultValue={session.folder || ""}
                        onChange={(e) => setFolderDraft(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            setSessionFolder(session.id, folderDraft);
                            setMenu(false);
                          }
                        }}
                      />
                    </div>
                    <MenuItem
                      icon={<Trash2 size={13} />}
                      label="Delete chat"
                      danger
                      onClick={() => {
                        if (session.is_important) {
                          toast("Unstar this chat before deleting it", "error");
                          return;
                        }
                        if (window.confirm(`Delete “${session.name}”? This cannot be undone.`)) deleteSession(session.id);
                        setMenu(false);
                      }}
                    />
                  </div>,
                  document.body,
                )}
            </div>
          )}
        </div>
      </div>

      {incognito && (
        <div className="px-4 py-1 text-center text-[11px]" style={{ background: "var(--accent-soft)", color: "var(--accent)" }}>
          Nobody mode — this turn is not kept in your chat list.
        </div>
      )}

      <div ref={scroller} onScroll={onScroll} className="relative z-10 flex-1 overflow-y-auto px-4">
        <div className="mx-auto flex max-w-3xl flex-col gap-6 py-4">
          <AnimatePresence mode="wait">
            {!sid && (
              <motion.div key="empty" initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }} className="flex min-h-[55vh] flex-col items-center justify-center gap-8">
                <div className="text-center">
                  <motion.img src="/icon.png" alt="" className="mx-auto mb-4 h-16 w-16 rounded-2xl" draggable={false} initial={{ scale: 0.8, rotate: -6 }} animate={{ scale: 1, rotate: 0 }} transition={{ type: "spring", stiffness: 260, damping: 18 }} />
                  <h1 className="text-[26px] font-semibold tracking-tight">
                    {greet}
                    {user ? `, ${user}` : ""}.
                  </h1>
                  <p className="mt-1 text-[15px]" style={{ color: "var(--muted)" }}>
                    What can I help you with today?
                  </p>
                </div>
                <div className="flex w-full max-w-2xl flex-wrap justify-center gap-2">
                  {QUICK.map((s) => (
                    <button key={s.view} className="pill" onClick={() => setView(s.view)}>
                      {s.icon} {s.title}
                    </button>
                  ))}
                </div>
                <div className="grid w-full max-w-2xl grid-cols-1 gap-2.5 sm:grid-cols-2">
                  {SUGGESTIONS.map((s, i) => (
                    <motion.button key={s.title} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.06 * i }} whileHover={{ y: -2 }} className="glass flex flex-col items-start gap-1.5 rounded-2xl p-4 text-left transition-shadow hover:shadow-lg" onClick={() => send(s.text)}>
                      <span className="flex items-center gap-2 text-[13px] font-semibold" style={{ color: "var(--accent)" }}>
                        {s.icon} {s.title}
                      </span>
                      <span className="text-[13px] leading-snug" style={{ color: "var(--muted)" }}>
                        {s.text}
                      </span>
                    </motion.button>
                  ))}
                </div>
              </motion.div>
            )}
          </AnimatePresence>

          {sid && loading && messages.length === 0 && (
            <div className="flex flex-col gap-4 pt-6">
              {[80, 55, 70].map((w, i) => (
                <div key={i} className={`shimmer h-5 rounded-lg ${i % 2 ? "self-end" : ""}`} style={{ width: `${w}%`, background: "var(--bg-sunken)" }} />
              ))}
            </div>
          )}

          {hidden > 0 && (
            <button className="btn mx-auto h-8 text-xs" onClick={() => setShowAll(true)}>
              Show earlier messages ({hidden})
            </button>
          )}

          {visible.map((m, i) => (
            <MessageView key={m.id} msg={m} isLast={i === visible.length - 1} />
          ))}
          {canContinue && (
            <button className="btn mx-auto h-8 text-xs" onClick={() => continueReply()}>
              Continue reply
            </button>
          )}
          <div className="h-2" />
        </div>
      </div>

      <AnimatePresence>
        {!stuck && messages.length > 0 && (
          <motion.button
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 8 }}
            className="glass absolute bottom-[148px] left-1/2 z-20 flex h-9 w-9 -translate-x-1/2 items-center justify-center rounded-full"
            onClick={() => {
              setStuck(true);
              scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
            }}
          >
            <ArrowDown size={15} />
          </motion.button>
        )}
      </AnimatePresence>

      <Composer />
    </section>
  );
}

function MenuItem({ icon, label, onClick, danger }: { icon: React.ReactNode; label: string; onClick: () => void; danger?: boolean }) {
  return (
    <button className="flex w-full items-center gap-2 px-3 py-2 text-left text-[13px] hover:bg-[var(--accent-soft)]" style={{ color: danger ? "#f87171" : undefined }} onClick={onClick}>
      {icon} {label}
    </button>
  );
}
