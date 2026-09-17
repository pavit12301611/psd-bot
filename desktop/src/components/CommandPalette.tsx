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
  const [webHits, setWebHits] = useState<{ title?: string; url?: string; snippet?: string }[] | null>(null);
  const [webBusy, setWebBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setQ("");
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
                  if (e.key === "Enter" && e.metaKey) runWeb();
                  if (e.key === "Enter" && !e.metaKey && chats[0]) {
                    setView("chat");
                    select(chats[0].id);
                    close();
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

function Row({ icon, label, hint, onClick }: { icon: React.ReactNode; label: string; hint?: string; onClick: () => void }) {
  return (
    <button className="flex w-full items-center gap-2.5 rounded-xl px-3 py-2 text-left text-[13px] hover:bg-[var(--accent-soft)]" onClick={onClick}>
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
