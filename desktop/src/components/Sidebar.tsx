import { useMemo, useState } from "react";
import { motion, AnimatePresence } from "motion/react";
import { Plus, Search, MessageSquare, Trash2, Pencil, Settings, LogOut, Check, X, Star, Archive, Folder } from "lucide-react";
import { useApp } from "../store/app";
import { metaLabel } from "../lib/ui";

function groupLabel(ts?: string) {
  if (!ts) return "Older";
  const d = new Date(ts);
  const now = new Date();
  const days = Math.floor((now.setHours(0, 0, 0, 0) - new Date(d).setHours(0, 0, 0, 0)) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return "This week";
  if (days < 30) return "This month";
  return "Older";
}

export default function Sidebar() {
  const sessions = useApp((s) => s.sessions);
  const active = useApp((s) => s.activeSessionId);
  const select = useApp((s) => s.selectSession);
  const newChat = useApp((s) => s.newChat);
  const remove = useApp((s) => s.deleteSession);
  const rename = useApp((s) => s.renameSession);
  const setSettings = useApp((s) => s.setSettings);
  const setPalette = useApp((s) => s.setPalette);
  const logout = useApp((s) => s.logout);
  const user = useApp((s) => s.authStatus?.username);
  const streaming = useApp((s) => s.streaming);
  const archiveSession = useApp((s) => s.archiveSession);
  const bulkDelete = useApp((s) => s.bulkDeleteSessions);
  const setFolder = useApp((s) => s.setSessionFolder);
  // Filter in useMemo, not the zustand selector — a new array every snapshot
  // makes useSyncExternalStore loop ("Maximum update depth exceeded").
  const important = useMemo(() => sessions.filter((x) => x.is_important && !x.archived), [sessions]);
  const [q, setQ] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [confirmDel, setConfirmDel] = useState<string | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  const groups = useMemo(() => {
    const filtered = sessions.filter((s) => !s.archived && (!q || s.name.toLowerCase().includes(q.toLowerCase())));
    const sorted = filtered.slice().sort((a, b) => Date.parse(b.last_message_at || b.updated_at || b.created_at || "0") - Date.parse(a.last_message_at || a.updated_at || a.created_at || "0"));
    const out: { label: string; items: typeof sessions }[] = [];
    for (const s of sorted) {
      const label = groupLabel(s.last_message_at || s.updated_at || s.created_at);
      let g = out.find((x) => x.label === label);
      if (!g) out.push((g = { label, items: [] }));
      g.items.push(s);
    }
    return out;
  }, [sessions, q]);

  const folders = useMemo(() => {
    const map = new Map<string, typeof sessions>();
    for (const s of sessions) {
      if (s.archived || (q && !s.name.toLowerCase().includes(q.toLowerCase()))) continue;
      const f = s.folder?.trim();
      if (!f) continue;
      if (!map.has(f)) map.set(f, []);
      map.get(f)!.push(s);
    }
    return [...map.entries()];
  }, [sessions, q]);

  return (
    <aside className="flex h-full w-[272px] shrink-0 flex-col" style={{ borderRight: "1px solid var(--border)", background: "color-mix(in oklab, var(--bg-elev) 55%, transparent)" }}>
      <div className="flex flex-col gap-2 p-3">
        <button className="btn btn-primary h-10 w-full justify-start" onClick={newChat} disabled={streaming}>
          <Plus size={16} /> New chat
        </button>
        <button className="btn h-9 w-full justify-start text-[13px]" onClick={() => setPalette(true)}>
          <Search size={14} /> Search
          <kbd className="ml-auto rounded px-1.5 py-0.5 text-[10px]" style={{ background: "var(--bg-sunken)", color: "var(--muted)" }}>{metaLabel}+K</kbd>
        </button>
        <div className="relative">
          <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2" style={{ color: "var(--muted)" }} />
          <input className="input h-9 pl-9 text-[13px]" placeholder="Filter chats" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
      </div>

      {picked.length > 0 && (
        <div className="flex items-center gap-1 px-3 pb-2">
          <span className="text-[11px]" style={{ color: "var(--muted)" }}>{picked.length} selected</span>
          <button className="btn h-7 px-2 text-[11px]" onClick={() => { picked.forEach((id) => archiveSession(id)); setPicked([]); }}>Archive</button>
          <button className="btn h-7 px-2 text-[11px] text-red-400" onClick={() => { if (window.confirm(`Delete ${picked.length} chats?`)) bulkDelete(picked); setPicked([]); }}>Delete</button>
          <button className="icon-btn h-7 w-7" onClick={() => setPicked([])}><X size={12} /></button>
        </div>
      )}

      <div className="flex-1 overflow-y-auto px-2 pb-2">
        {folders.map(([name, items]) => (
          <div key={name} className="mb-2">
            <button className="flex w-full items-center gap-1.5 px-3 pb-1 pt-2 text-left text-[11px] font-semibold uppercase tracking-wider" style={{ color: "var(--muted)" }} onClick={() => setCollapsed((c) => ({ ...c, [name]: !c[name] }))}>
              <Folder size={11} /> {name}
              <span className="ml-auto">{items.length}</span>
            </button>
            {!collapsed[name] && items.map((s) => (
              <button key={s.id} className="flex w-full items-center gap-2 rounded-xl px-3 py-1.5 text-left text-[13px] hover:bg-[var(--accent-soft)]" onClick={() => select(s.id)} onContextMenu={(e) => { e.preventDefault(); setFolder(s.id, ""); }}>
                <span className="truncate">{s.name}</span>
              </button>
            ))}
          </div>
        ))}
        {important.length > 0 && !q && (
          <div className="mb-2">
            <div className="px-3 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-wider" style={{ color: "var(--muted)" }}>
              Starred
            </div>
            {important.map((s) => (
              <button key={s.id} className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left text-[13px] hover:bg-[var(--accent-soft)]" onClick={() => select(s.id)}>
                <Star size={12} style={{ color: "var(--accent)" }} fill="currentColor" />
                <span className="truncate">{s.name}</span>
              </button>
            ))}
          </div>
        )}
        {groups.length === 0 && (
          <div className="mt-10 flex flex-col items-center gap-2 text-center text-sm" style={{ color: "var(--muted)" }}>
            <MessageSquare size={26} strokeWidth={1.5} />
            {q ? "No chats match" : "No chats yet — start one!"}
          </div>
        )}
        {groups.map((g) => (
          <div key={g.label} className="mb-2">
            <div className="px-3 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-wider" style={{ color: "var(--muted)" }}>
              {g.label}
            </div>
            <AnimatePresence initial={false}>
              {g.items.map((s) => {
                const isActive = s.id === active;
                const isEditing = editing === s.id;
                return (
                  <motion.div
                    key={s.id}
                    layout
                    initial={{ opacity: 0, y: -4 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, x: -12 }}
                    transition={{ duration: 0.18 }}
                    className="group relative"
                  >
                    {isActive && <motion.div layoutId="active-pill" className="absolute inset-0 rounded-xl" style={{ background: "var(--accent-soft)" }} transition={{ type: "spring", stiffness: 500, damping: 40 }} />}
                    <div
                      className="relative flex cursor-pointer items-center gap-2 rounded-xl px-3 py-2 text-[13px] transition-colors hover:bg-[var(--accent-soft)]/50"
                      onClick={(e) => {
                        if (isEditing) return;
                        if (e.metaKey || e.ctrlKey || e.shiftKey) {
                          setPicked((p) => (p.includes(s.id) ? p.filter((id) => id !== s.id) : [...p, s.id]));
                          return;
                        }
                        select(s.id);
                      }}
                      onDoubleClick={() => {
                        setEditing(s.id);
                        setDraft(s.name);
                      }}
                    >
                      {isEditing ? (
                        <>
                          <input
                            autoFocus
                            className="input h-7 flex-1 px-2 py-0 text-[13px]"
                            value={draft}
                            onChange={(e) => setDraft(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") {
                                rename(s.id, draft.trim() || s.name);
                                setEditing(null);
                              }
                              if (e.key === "Escape") setEditing(null);
                            }}
                            onClick={(e) => e.stopPropagation()}
                          />
                          <button className="icon-btn h-7 w-7" onClick={(e) => { e.stopPropagation(); rename(s.id, draft.trim() || s.name); setEditing(null); }}>
                            <Check size={14} />
                          </button>
                          <button className="icon-btn h-7 w-7" onClick={(e) => { e.stopPropagation(); setEditing(null); }}>
                            <X size={14} />
                          </button>
                        </>
                      ) : confirmDel === s.id ? (
                        <>
                          <span className="flex-1 truncate text-red-400">Delete this chat?</span>
                          <button className="icon-btn h-7 w-7 text-red-400" title="Confirm" onClick={(e) => { e.stopPropagation(); remove(s.id); setConfirmDel(null); }}>
                            <Check size={14} />
                          </button>
                          <button className="icon-btn h-7 w-7" title="Cancel" onClick={(e) => { e.stopPropagation(); setConfirmDel(null); }}>
                            <X size={14} />
                          </button>
                        </>
                      ) : (
                        <>
                          <span className="flex-1 truncate" style={{ color: isActive ? "var(--text)" : "var(--text)" }}>
                            {s.name}
                          </span>
                          <span className="flex shrink-0 items-center opacity-0 transition-opacity group-hover:opacity-100">
                            <button className="icon-btn h-7 w-7" title="Rename" onClick={(e) => { e.stopPropagation(); setEditing(s.id); setDraft(s.name); }}>
                              <Pencil size={13} />
                            </button>
                            <button className="icon-btn h-7 w-7" title="Archive" onClick={(e) => { e.stopPropagation(); archiveSession(s.id); }}>
                              <Archive size={13} />
                            </button>
                            <button className="icon-btn h-7 w-7 hover:!text-red-400" title="Delete" onClick={(e) => { e.stopPropagation(); setConfirmDel(s.id); }}>
                              <Trash2 size={13} />
                            </button>
                          </span>
                        </>
                      )}
                    </div>
                  </motion.div>
                );
              })}
            </AnimatePresence>
          </div>
        ))}
      </div>

      <div className="flex items-center gap-1 p-2" style={{ borderTop: "1px solid var(--border)" }}>
        <div className="flex h-8 w-8 items-center justify-center rounded-full text-xs font-bold text-white" style={{ background: "linear-gradient(135deg, var(--accent), #6c8cff)" }}>
          {(user || "?").slice(0, 1).toUpperCase()}
        </div>
        <div className="min-w-0 flex-1 px-1">
          <div className="truncate text-[13px] font-medium">{user}</div>
          <div className="text-[11px]" style={{ color: "var(--muted)" }}>
            Local · private
          </div>
        </div>
        <button className="icon-btn" title="Settings" onClick={() => setSettings(true)}>
          <Settings size={16} />
        </button>
        <button className="icon-btn" title="Sign out" onClick={logout}>
          <LogOut size={16} />
        </button>
      </div>
    </aside>
  );
}
