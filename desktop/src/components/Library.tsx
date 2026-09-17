import { useEffect, useState } from "react";
import { Library as LibIcon, Plus, Trash2, Save, FileText } from "lucide-react";
import { documents as api, type LibraryDoc } from "../lib/api";
import { useApp } from "../store/app";
import { Empty, PanelHead } from "./media";

export default function Library() {
  const toast = useApp((s) => s.toast);
  const setLibraryDirty = useApp((s) => s.setLibraryDirty);
  const [docs, setDocs] = useState<LibraryDoc[]>([]);
  const [active, setActive] = useState<LibraryDoc | null>(null);
  const [content, setContent] = useState("");
  const [q, setQ] = useState("");
  const [dirty, setDirty] = useState(false);

  const load = async () => {
    try {
      const r = await api.library({ search: q || undefined });
      setDocs(r.documents || []);
    } catch (e: any) {
      toast(e.message || "Could not load documents", "error");
      setDocs([]);
    }
  };
  useEffect(() => {
    load();
  }, []);

  const open = async (d: LibraryDoc) => {
    try {
      const full = await api.get(d.id);
      setActive(full);
      setContent(full.current_content || d.preview || "");
      setDirty(false);
      setLibraryDirty(false);
    } catch (e: any) {
      toast(e.message || "Could not open", "error");
    }
  };

  const create = async () => {
    try {
      const d = await api.create("Untitled");
      await load();
      setActive(d);
      setContent(d.current_content || "");
    } catch (e: any) {
      toast(e.message || "Create failed", "error");
    }
  };

  const save = async () => {
    if (!active) return;
    try {
      await api.save(active.id, content);
      if (active.title) await api.rename(active.id, active.title);
      setDirty(false);
      setLibraryDirty(false);
      toast("Saved", "success");
      load();
    } catch (e: any) {
      toast(e.message || "Save failed", "error");
    }
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        if (active && dirty) void save();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  return (
    <section className="panel">
      <PanelHead icon={<LibIcon size={16} />} title="Library">
        <input className="input h-8 w-48 text-[13px]" placeholder="Search documents" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && load()} />
        <button className="btn btn-primary h-8" onClick={create}>
          <Plus size={14} /> New document
        </button>
      </PanelHead>
      <div className="flex min-h-0 flex-1">
        <div className="split-side overflow-y-auto p-3" style={{ borderRight: "1px solid var(--border)" }}>
          {docs.length === 0 && <Empty icon={<FileText size={28} />} title="No documents" hint="Drafts, exports, and files you save live here." action={<button className="btn btn-primary mt-2" onClick={create}>New document</button>} />}
          <div className="flex flex-col gap-1">
            {docs.map((d) => (
              <button key={d.id} className="rounded-xl px-3 py-2.5 text-left" style={{ background: active?.id === d.id ? "var(--accent-soft)" : "transparent" }} onClick={() => { if (dirty && !window.confirm("Discard unsaved changes?")) return; open(d); }}>
                <div className="truncate text-[13px] font-medium">{d.title || "Untitled"}</div>
                <div className="truncate text-[11px]" style={{ color: "var(--muted)" }}>
                  {d.language || "text"} · {(d.preview || "").slice(0, 60)}
                </div>
              </button>
            ))}
          </div>
        </div>
        <div className="flex min-w-0 flex-1 flex-col p-4">
          {!active ? (
            <Empty icon={<FileText size={36} />} title="Pick a document" hint="Or create a new one." />
          ) : (
            <>
              <div className="mb-2 flex items-center gap-2">
                <input className="input flex-1 font-semibold" value={active.title} onChange={(e) => { setActive({ ...active, title: e.target.value }); setDirty(true); setLibraryDirty(true); }} />
                <button className="btn btn-primary h-9" disabled={!dirty} onClick={save}>
                  <Save size={14} /> Save
                </button>
                <button className="icon-btn hover:!text-red-400" onClick={async () => { if (!window.confirm("Delete this document?")) return; await api.remove(active.id); setActive(null); setLibraryDirty(false); load(); }}>
                  <Trash2 size={15} />
                </button>
              </div>
              <textarea className="input min-h-0 flex-1 resize-none font-mono text-[13px] leading-relaxed" value={content} onChange={(e) => { setContent(e.target.value); setDirty(true); setLibraryDirty(true); }} placeholder="Write markdown…" />
            </>
          )}
        </div>
      </div>
    </section>
  );
}
