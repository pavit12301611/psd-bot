import { useEffect, useRef, useState } from "react";
import { Plus, Pin, Trash2, Archive, StickyNote, CheckSquare, Square } from "lucide-react";
import { notes as api, type Note } from "../lib/api";
import { useApp } from "../store/app";
import { Empty, PanelHead } from "./media";

const COLORS = ["#e06c75", "#61afef", "#98c379", "#e5c07b", "#c678dd", "#56b6c2", ""];

export default function Notes() {
  const toast = useApp((s) => s.toast);
  const [list, setList] = useState<Note[]>([]);
  const [archived, setArchived] = useState(false);
  const [active, setActive] = useState<Note | null>(null);
  const [busy, setBusy] = useState(false);

  const load = async (arch = archived) => {
    try {
      const r = await api.list(arch);
      setList(r.notes || []);
    } catch (e: any) {
      toast(e.message || "Could not load notes", "error");
      setList([]);
    }
  };
  useEffect(() => {
    load();
  }, [archived]);

  const create = async () => {
    setBusy(true);
    try {
      const n = await api.create({ title: "Untitled", content: "", note_type: "note" });
      setActive(n);
      await load();
    } catch (e: any) {
      toast(e.message || "Create failed", "error");
    } finally {
      setBusy(false);
    }
  };

  const saveTimer = useRef<number | null>(null);
  const save = async (patch: Partial<Note>, immediate = false) => {
    if (!active) return;
    const run = async () => {
      try {
        const n = await api.update(active.id, patch);
        setActive((cur) => (cur && cur.id === n.id ? { ...cur, ...n } : cur));
        setList((xs) => xs.map((x) => (x.id === n.id ? n : x)));
      } catch (e: any) {
        toast(e.message || "Save failed", "error");
      }
    };
    if (immediate) return run();
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(run, 400);
  };

  return (
    <section className="panel">
      <PanelHead icon={<StickyNote size={16} />} title="Notes">
        <button className={`pill ${archived ? "" : ""}`} data-on={archived} onClick={() => { setArchived((v) => !v); setActive(null); }}>
          <Archive size={12} /> Archived
        </button>
        <button className="btn btn-primary h-8" onClick={create} disabled={busy}>
          <Plus size={14} /> New note
        </button>
      </PanelHead>
      <div className="flex min-h-0 flex-1">
        <div className="split-side overflow-y-auto p-3" style={{ borderRight: "1px solid var(--border)" }}>
          {list.length === 0 && (
            <Empty icon={<StickyNote size={28} />} title={archived ? "No archived notes" : "No notes yet"} hint="Capture thoughts, checklists, and reminders." action={!archived ? <button className="btn btn-primary mt-2" onClick={create}>Create one</button> : undefined} />
          )}
          <div className="flex flex-col gap-1.5">
            {list.map((n) => (
              <button
                key={n.id}
                className="rounded-xl px-3 py-2.5 text-left"
                style={{
                  background: active?.id === n.id ? "var(--accent-soft)" : "transparent",
                  borderLeft: n.color ? `3px solid ${n.color}` : "3px solid transparent",
                }}
                onClick={() => setActive(n)}
              >
                <div className="flex items-center gap-1.5">
                  {n.pinned && <Pin size={11} style={{ color: "var(--accent)" }} />}
                  <span className="truncate text-[13px] font-medium">{n.title || "Untitled"}</span>
                </div>
                <div className="mt-0.5 truncate text-[12px]" style={{ color: "var(--muted)" }}>
                  {n.note_type === "checklist" ? `${(n.items || []).length} items` : (n.content || "").slice(0, 80) || "Empty"}
                </div>
              </button>
            ))}
          </div>
        </div>
        <div className="flex min-w-0 flex-1 flex-col p-5">
          {!active ? (
            <Empty icon={<StickyNote size={36} />} title="Select a note" hint="Or create a new one to start writing." />
          ) : (
            <>
              <div className="mb-3 flex items-center gap-2">
                <input className="input flex-1 text-[16px] font-semibold" value={active.title} onChange={(e) => { const title = e.target.value; setActive({ ...active, title }); save({ title }); }} onBlur={() => save({ title: active.title }, true)} placeholder="Title" />
                <button className="icon-btn" title="Pin" onClick={async () => { await api.pin(active.id); load(); }}>
                  <Pin size={15} style={{ color: active.pinned ? "var(--accent)" : undefined }} />
                </button>
                <button className="icon-btn" title="Archive" onClick={async () => { await api.archive(active.id); setActive(null); load(); }}>
                  <Archive size={15} />
                </button>
                <button className="icon-btn hover:!text-red-400" title="Delete" onClick={async () => { if (!window.confirm("Delete this note?")) return; await api.remove(active.id); setActive(null); load(); }}>
                  <Trash2 size={15} />
                </button>
              </div>
              <div className="mb-3 flex items-center gap-2">
                {COLORS.map((c) => (
                  <button key={c || "none"} className="note-swatch" data-on={active.color === c || (!active.color && !c)} style={{ background: c || "var(--bg-sunken)" }} onClick={() => save({ color: c })} />
                ))}
                <button className="pill ml-2" data-on={active.note_type === "checklist"} onClick={() => save({ note_type: active.note_type === "checklist" ? "note" : "checklist", items: active.items || [] })}>
                  <CheckSquare size={12} /> Checklist
                </button>
              </div>
              {active.note_type === "checklist" ? (
                <Checklist note={active} onChange={(items) => { setActive({ ...active, items }); save({ items }); }} />
              ) : (
                <textarea
                  className="input min-h-0 flex-1 resize-none py-3"
                  value={active.content || ""}
                  onChange={(e) => { const content = e.target.value; setActive({ ...active, content }); save({ content }); }}
                  onBlur={() => save({ content: active.content || "" }, true)}
                  placeholder="Write something…"
                />
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}

function Checklist({ note, onChange }: { note: Note; onChange: (items: { text: string; done?: boolean }[]) => void }) {
  const items = note.items || [];
  return (
    <div className="flex flex-col gap-1.5">
      {items.map((it, i) => (
        <div key={i} className="flex items-center gap-2">
          <button className="icon-btn h-7 w-7" onClick={() => onChange(items.map((x, j) => (j === i ? { ...x, done: !x.done } : x)))}>
            {it.done ? <CheckSquare size={15} style={{ color: "var(--accent)" }} /> : <Square size={15} />}
          </button>
          <input
            className="input h-9 flex-1"
            value={it.text}
            style={{ textDecoration: it.done ? "line-through" : undefined, opacity: it.done ? 0.6 : 1 }}
            onChange={(e) => onChange(items.map((x, j) => (j === i ? { ...x, text: e.target.value } : x)))}
          />
          <button className="icon-btn h-7 w-7 hover:!text-red-400" onClick={() => onChange(items.filter((_, j) => j !== i))}>
            <Trash2 size={13} />
          </button>
        </div>
      ))}
      <button className="btn self-start" onClick={() => onChange([...items, { text: "", done: false }])}>
        <Plus size={14} /> Add item
      </button>
    </div>
  );
}
