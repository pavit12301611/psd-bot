import { useEffect, useMemo, useState } from "react";
import { Brain, Plus, Trash2, Pin, Search, Zap } from "lucide-react";
import { memory as memApi, skills as skillApi, type MemoryItem, type Skill } from "../lib/api";
import { useApp } from "../store/app";
import { Empty, PanelHead } from "./media";

export default function Memory() {
  const toast = useApp((s) => s.toast);
  const [tab, setTab] = useState<"memories" | "skills">("memories");
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [q, setQ] = useState("");
  const [text, setText] = useState("");
  const [cat, setCat] = useState("fact");
  const [skillDraft, setSkillDraft] = useState({ title: "", problem: "", solution: "" });
  const [skillUrl, setSkillUrl] = useState("");
  const [editId, setEditId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");

  const loadMem = async () => {
    try {
      const r = await memApi.list();
      setItems(r.memory || []);
    } catch (e: any) {
      toast(e.message || "Could not load memories", "error");
      setItems([]);
    }
  };
  const loadSkills = async () => {
    try {
      const r = await skillApi.list();
      setSkills(r.skills || []);
    } catch (e: any) {
      toast(e.message || "Could not load skills", "error");
      setSkills([]);
    }
  };
  useEffect(() => {
    loadMem();
    loadSkills();
  }, []);

  const filtered = useMemo(() => {
    const n = q.trim().toLowerCase();
    return items.filter((m) => !n || (m.text || "").toLowerCase().includes(n) || (m.category || "").includes(n));
  }, [items, q]);

  const addMem = async () => {
    if (!text.trim()) return;
    try {
      await memApi.add(text.trim(), cat);
      setText("");
      toast("Memory saved", "success");
      loadMem();
    } catch (e: any) {
      toast(e.message || "Could not add", "error");
    }
  };

  const addSkill = async () => {
    if (!skillDraft.title.trim() || !skillDraft.solution.trim()) return;
    try {
      await skillApi.add(skillDraft);
      setSkillDraft({ title: "", problem: "", solution: "" });
      toast("Skill added", "success");
      loadSkills();
    } catch (e: any) {
      toast(e.message || "Could not add skill", "error");
    }
  };

  return (
    <section className="panel">
      <PanelHead icon={<Brain size={16} />} title="Brain">
        <div className="flex rounded-full p-0.5" style={{ background: "var(--bg-sunken)", border: "1px solid var(--border)" }}>
          {(["memories", "skills"] as const).map((t) => (
            <button key={t} className="rounded-full px-3 py-1 text-xs font-medium" style={{ background: tab === t ? "var(--accent-soft)" : "transparent", color: tab === t ? "var(--accent)" : "var(--muted)" }} onClick={() => setTab(t)}>
              {t === "memories" ? `Memories (${items.length})` : `Skills (${skills.length})`}
            </button>
          ))}
        </div>
      </PanelHead>
      <div className="panel-body">
        {tab === "memories" ? (
          <>
            <div className="mb-4 flex flex-col gap-2 sm:flex-row">
              <input className="input flex-1" placeholder="Add a memory — e.g. I prefer concise replies" value={text} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && addMem()} />
              <select className="input w-32" value={cat} onChange={(e) => setCat(e.target.value)}>
                <option value="fact">Fact</option>
                <option value="preference">Preference</option>
                <option value="person">Person</option>
                <option value="project">Project</option>
              </select>
              <button className="btn btn-primary" disabled={!text.trim()} onClick={addMem}>
                <Plus size={14} /> Add
              </button>
            </div>
            <div className="relative mb-3">
              <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2" style={{ color: "var(--muted)" }} />
              <input className="input h-9 pl-9" placeholder="Search memories" value={q} onChange={(e) => setQ(e.target.value)} />
            </div>
            {filtered.length === 0 && <Empty icon={<Brain size={32} />} title="No memories yet" hint="Facts you add here are injected into chat so the model remembers you." />}
            <div className="flex flex-col gap-2">
              {filtered.map((m) => (
                <div key={m.id} className="card flex items-start gap-3">
                  <div className="min-w-0 flex-1">
                    {editId === m.id ? (
                      <textarea
                        autoFocus
                        className="input min-h-[64px] text-[13.5px]"
                        value={editText}
                        onChange={(e) => setEditText(e.target.value)}
                        onBlur={async () => {
                          const next = editText.trim();
                          setEditId(null);
                          if (!next || next === m.text) return;
                          try {
                            await memApi.update(m.id, { text: next });
                            loadMem();
                          } catch (e: any) {
                            toast(e.message || "Could not update", "error");
                          }
                        }}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" && !e.shiftKey) (e.target as HTMLTextAreaElement).blur();
                          if (e.key === "Escape") setEditId(null);
                        }}
                      />
                    ) : (
                      <p className="cursor-text text-[13.5px] leading-relaxed" title="Click to edit" onClick={() => { setEditId(m.id); setEditText(m.text); }}>{m.text}</p>
                    )}
                    <div className="mt-1 flex gap-2 text-[11px]" style={{ color: "var(--muted)" }}>
                      <span className="pill" data-on="true">{m.category || "fact"}</span>
                      {m.source && <span>{m.source}</span>}
                    </div>
                  </div>
                  <button className="icon-btn h-8 w-8" title="Pin" onClick={async () => { await memApi.pin(m.id); loadMem(); }}>
                    <Pin size={14} style={{ color: m.pinned ? "var(--accent)" : undefined }} />
                  </button>
                  <button className="icon-btn h-8 w-8 hover:!text-red-400" title="Delete" onClick={async () => { if (!window.confirm("Forget this memory?")) return; await memApi.remove(m.id); loadMem(); }}>
                    <Trash2 size={14} />
                  </button>
                </div>
              ))}
            </div>
          </>
        ) : (
          <>
            <div className="card mb-4 flex flex-col gap-2">
              <h3 className="text-[13px] font-semibold">New skill</h3>
              <input className="input" placeholder="Title — e.g. rename-files" value={skillDraft.title} onChange={(e) => setSkillDraft({ ...skillDraft, title: e.target.value })} />
              <input className="input" placeholder="When to use" value={skillDraft.problem} onChange={(e) => setSkillDraft({ ...skillDraft, problem: e.target.value })} />
              <textarea className="input min-h-[80px] resize-y" placeholder="How — steps, commands, rules" value={skillDraft.solution} onChange={(e) => setSkillDraft({ ...skillDraft, solution: e.target.value })} />
              <button className="btn btn-primary self-start" disabled={!skillDraft.title.trim() || !skillDraft.solution.trim()} onClick={addSkill}>
                <Zap size={14} /> Add skill
              </button>
              <div className="flex gap-2">
                <input className="input flex-1" placeholder="Import skill from URL" value={skillUrl} onChange={(e) => setSkillUrl(e.target.value)} />
                <button className="btn" disabled={!skillUrl.trim()} onClick={async () => {
                  try {
                    await skillApi.importUrl(skillUrl.trim());
                    setSkillUrl("");
                    toast("Skill imported", "success");
                    loadSkills();
                  } catch (e: any) {
                    toast(e.message || "Import failed", "error");
                  }
                }}>Import</button>
              </div>
            </div>
            {skills.length === 0 && <Empty icon={<Zap size={32} />} title="No skills yet" hint="Reusable procedures the agent can call with /skill-name." />}
            <div className="flex flex-col gap-2">
              {skills.map((s) => {
                const id = s.id || s.name || s.title || "";
                return (
                  <div key={id} className="card flex items-start gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="font-medium">{s.title || s.name}</div>
                      <p className="mt-0.5 text-[13px]" style={{ color: "var(--muted)" }}>
                        {s.description || s.problem || ""}
                      </p>
                      <div className="mt-1 text-[11px]" style={{ color: "var(--muted)" }}>
                        {s.status || "draft"}
                        {s.confidence != null ? ` · ${Math.round(s.confidence * 100)}%` : ""}
                      </div>
                    </div>
                    {id && (
                      <button className="icon-btn hover:!text-red-400" onClick={async () => { await skillApi.remove(id); loadSkills(); }}>
                        <Trash2 size={14} />
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          </>
        )}
      </div>
    </section>
  );
}
