import { useEffect, useMemo, useState } from "react";
import {
  Search,
  MessageSquare,
  HardDrive,
  StickyNote,
  ListTodo,
  CalendarDays,
  Brain,
  Images,
  Library,
  Telescope,
  Columns2,
  Mail,
  Plus,
  Globe,
} from "lucide-react";
import { useApp, type AppView } from "../store/app";
import { search as searchApi } from "../lib/api";
import Overlay from "./Overlay";
import { openExternal } from "../lib/ui";

const JUMP: { id: AppView; label: string; icon: typeof MessageSquare; keys: string }[] = [
  { id: "chat", label: "Chat", icon: MessageSquare, keys: "chat conversations" },
  { id: "models", label: "Models", icon: HardDrive, keys: "models download huggingface ollama gguf" },
  { id: "notes", label: "Notes", icon: StickyNote, keys: "notes memo" },
  { id: "tasks", label: "Tasks", icon: ListTodo, keys: "tasks schedule cron" },
  { id: "calendar", label: "Calendar", icon: CalendarDays, keys: "calendar events" },
  { id: "memory", label: "Brain", icon: Brain, keys: "memory brain facts" },
  { id: "gallery", label: "Gallery", icon: Images, keys: "gallery images photos" },
  { id: "library", label: "Library", icon: Library, keys: "documents files library" },
  { id: "research", label: "Deep Research", icon: Telescope, keys: "research deep web" },
  { id: "compare", label: "Compare", icon: Columns2, keys: "compare models" },
  { id: "email", label: "Email", icon: Mail, keys: "email inbox mail" },
];

export default function CommandPalette() {
  const open = useApp((s) => s.paletteOpen);
  const close = () => useApp.getState().setPalette(false);
  const sessions = useApp((s) => s.sessions);
  const select = useApp((s) => s.selectSession);
  const setView = useApp((s) => s.setView);
  const newChat = useApp((s) => s.newChat);
  const send = useApp((s) => s.send);
  const [q, setQ] = useState("");
  const [hi, setHi] = useState(0);
  const [webHits, setWebHits] = useState<{ title?: string; url?: string; snippet?: string }[] | null>(null);
  const [webBusy, setWebBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setQ("");
    setHi(0);
    setWebHits(null);
    const h = (e: KeyboardEvent) => e.key === "Escape" && close();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open]);

  const chats = useMemo(() => {
    const n = q.trim().toLowerCase();
    return sessions
      .filter((s) => !s.archived && (!n || s.name.toLowerCase().includes(n)))
      .slice(0, 8);
  }, [sessions, q]);

  const jumps = useMemo(() => {
    const n = q.trim().toLowerCase();
    return JUMP.filter((j) => !n || j.label.toLowerCase().includes(n) || j.keys.includes(n)).slice(0, 6);
  }, [q]);

  const runWeb = async () => {
    const query = q.trim();
    if (!query) return;
    setWebBusy(true);
    try {
      const r = await searchApi.web(query);
      setWebHits(Array.isArray(r.sources) ? r.sources : []);
    } catch {
      setWebHits([]);
    } finally {
      setWebBusy(false);
    }
  };

  const rows = useMemo(() => {
    const out: { id: string; run: () => void }[] = [{ id: "new", run: () => { newChat(); close(); } }];
    for (const j of jumps) out.push({ id: `jump-${j.id}`, run: () => { setView(j.id); close(); } });
    for (const s of chats) out.push({ id: s.id, run: () => { setView("chat"); select(s.id); close(); } });
    if (q.trim()) {
      out.push({ id: "ask", run: () => { setView("chat"); close(); send(q.trim()); } });
      out.push({ id: "web", run: () => { void runWeb(); } });
    }
    return out;
  }, [jumps, chats, q, newChat, close, setView, select, send]);

  useEffect(() => {
    setHi(0);
  }, [q]);

  return (
    <Overlay open={open} onClose={close} labelledBy="palette-title">
          <div className="glass w-full max-w-xl overflow-hidden rounded-2xl" style={{ boxShadow: "var(--shadow)" }}>
            <div className="flex items-center gap-2 px-3 py-2" style={{ borderBottom: "1px solid var(--border)" }}>
              <Search size={16} style={{ color: "var(--muted)" }} />
              <input
                autoFocus
                className="h-10 flex-1 bg-transparent text-[15px] outline-none"
                id="palette-title"
                placeholder="Search chats, jump to a tool, or search the web…"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "ArrowDown") {
                    e.preventDefault();
                    setHi((v) => Math.min(rows.length - 1, v + 1));
                  } else if (e.key === "ArrowUp") {
                    e.preventDefault();
                    setHi((v) => Math.max(0, v - 1));
                  } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                    e.preventDefault();
                    runWeb();
                  } else if (e.key === "Enter") {
                    e.preventDefault();
                    rows[hi]?.run();
                  }
                }}
              />
              <kbd className="rounded-md px-1.5 py-0.5 text-[10px]" style={{ background: "var(--bg-sunken)", color: "var(--muted)" }}>
                Esc
              </kbd>
            </div>
            <div className="max-h-[50vh] overflow-y-auto p-1.5">
              <Row
                icon={<Plus size={14} />}
                label="New chat"
                active={rows[hi]?.id === "new"}
                onClick={() => {
                  newChat();
                  close();
                }}
              />
              {jumps.length > 0 && (
                <Group label="Go to">
                  {jumps.map((j) => {
                    const Icon = j.icon;
                    return (
                      <Row
                        key={j.id}
                        icon={<Icon size={14} />}
                        label={j.label}
                        active={rows[hi]?.id === `jump-${j.id}`}
                        onClick={() => {
                          setView(j.id);
                          close();
                        }}
                      />
                    );
                  })}
                </Group>
              )}
              {chats.length > 0 && (
                <Group label="Chats">
                  {chats.map((s) => (
                    <Row
                      key={s.id}
                      icon={<MessageSquare size={14} />}
                      label={s.name}
                      hint={s.model?.split("/").pop()}
                      active={rows[hi]?.id === s.id}
                      onClick={() => {
                        setView("chat");
                        select(s.id);
                        close();
                      }}
                    />
                  ))}
                </Group>
              )}
              {q.trim() && (
                <Group label="Actions">
                  <Row
                    icon={<MessageSquare size={14} />}
                    label={`Ask psd.ai: “${q.trim()}”`}
                    onClick={() => {
                      setView("chat");
                      close();
                      send(q.trim());
                    }}
                  />
                  <Row
                    icon={<Globe size={14} />}
                    label={webBusy ? "Searching the web…" : `Web search: “${q.trim()}”`}
                    onClick={runWeb}
                  />
                </Group>
              )}
              {webHits && (
                <Group label="Web results">
                  {webHits.length === 0 && <div className="px-3 py-2 text-[12px]" style={{ color: "var(--muted)" }}>No results</div>}
                  {webHits.slice(0, 6).map((s, i) => (
                    <Row key={i} icon={<Globe size={14} />} label={s.title || s.url || "Result"} hint={s.snippet} onClick={() => s.url && openExternal(s.url)} />
                  ))}
                </Group>
              )}
            </div>
          </div>
    </Overlay>
  );
}

function Group({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mt-1">
      <div className="px-3 py-1 text-[10px] font-semibold uppercase tracking-wider" style={{ color: "var(--muted)" }}>
        {label}
      </div>
      {children}
    </div>
  );
}

function Row({ icon, label, hint, onClick, active }: { icon: React.ReactNode; label: string; hint?: string; onClick: () => void; active?: boolean }) {
  return (
    <button className="flex w-full items-center gap-2.5 rounded-xl px-3 py-2 text-left text-[13px] hover:bg-[var(--accent-soft)]" style={active ? { background: "var(--accent-soft)" } : undefined} onClick={onClick}>
      <span style={{ color: "var(--muted)" }}>{icon}</span>
      <span className="min-w-0 flex-1 truncate">{label}</span>
      {hint && (
        <span className="max-w-[40%] truncate text-[11px]" style={{ color: "var(--muted)" }}>
          {hint}
        </span>
      )}
    </button>
  );
}
