import { useEffect, useState } from "react";
import { Plus, Play, Pause, Trash2, ListTodo, RefreshCw, Square } from "lucide-react";
import { tasks as api, type Task } from "../lib/api";
import { useApp } from "../store/app";
import { Empty, PanelHead } from "./media";

export default function Tasks() {
  const toast = useApp((s) => s.toast);
  const [list, setList] = useState<Task[]>([]);
  const [draft, setDraft] = useState({ name: "", prompt: "", schedule: "daily", scheduled_time: "09:00" });
  const [showNew, setShowNew] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const load = async () => {
    try {
      const r = await api.list();
      setList(r.tasks || []);
    } catch (e: any) {
      toast(e.message || "Could not load tasks", "error");
      setList([]);
    }
  };
  useEffect(() => {
    load();
  }, []);

  const act = async (id: string, fn: () => Promise<any>, ok?: string) => {
    setBusy(id);
    try {
      await fn();
      if (ok) toast(ok, "success");
      await load();
    } catch (e: any) {
      toast(e.message || "Failed", "error");
    } finally {
      setBusy(null);
    }
  };

  const create = async () => {
    if (!draft.name.trim()) return;
    setBusy("new");
    try {
      await api.create({
        name: draft.name.trim(),
        prompt: draft.prompt,
        task_type: "llm",
        schedule: draft.schedule,
        scheduled_time: draft.scheduled_time,
      });
      setDraft({ name: "", prompt: "", schedule: "daily", scheduled_time: "09:00" });
      setShowNew(false);
      toast("Task created", "success");
      await load();
    } catch (e: any) {
      toast(e.message || "Create failed", "error");
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="panel">
      <PanelHead icon={<ListTodo size={16} />} title="Tasks">
        <button className="icon-btn h-8 w-8" title="Refresh" onClick={load}>
          <RefreshCw size={14} />
        </button>
        <button className="btn btn-primary h-8" onClick={() => setShowNew((v) => !v)}>
          <Plus size={14} /> New task
        </button>
      </PanelHead>
      <div className="panel-body">
        {showNew && (
          <div className="card mb-4 flex flex-col gap-2">
            <input className="input" placeholder="Task name" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
            <textarea className="input min-h-[80px] resize-y" placeholder="Prompt the agent should run…" value={draft.prompt} onChange={(e) => setDraft({ ...draft, prompt: e.target.value })} />
            <div className="flex flex-wrap gap-2">
              <select className="input w-auto" value={draft.schedule} onChange={(e) => setDraft({ ...draft, schedule: e.target.value })}>
                <option value="once">Once</option>
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
                <option value="monthly">Monthly</option>
              </select>
              <input className="input w-32" type="time" value={draft.scheduled_time} onChange={(e) => setDraft({ ...draft, scheduled_time: e.target.value })} />
              <button className="btn btn-primary" disabled={!draft.name.trim() || busy === "new"} onClick={create}>
                Create
              </button>
              <button className="btn" onClick={() => setShowNew(false)}>
                Cancel
              </button>
            </div>
          </div>
        )}
        {list.length === 0 && !showNew && (
          <Empty icon={<ListTodo size={36} />} title="No scheduled tasks" hint="Create a task to run a prompt on a schedule, or trigger an action." action={<button className="btn btn-primary mt-2" onClick={() => setShowNew(true)}>New task</button>} />
        )}
        <div className="flex flex-col gap-2">
          {list.map((t) => {
            const paused = t.status === "paused" || t.status === "disabled";
            return (
              <div key={t.id} className="card flex items-start gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-medium">{t.name}</span>
                    <span className="pill text-[10px]" data-on={!paused}>
                      {t.status || "active"}
                    </span>
                    {t.schedule && (
                      <span className="text-[11px]" style={{ color: "var(--muted)" }}>
                        {t.schedule}
                        {t.scheduled_time ? ` · ${t.scheduled_time}` : ""}
                      </span>
                    )}
                  </div>
                  {t.prompt && (
                    <p className="mt-1 line-clamp-2 text-[13px]" style={{ color: "var(--muted)" }}>
                      {t.prompt}
                    </p>
                  )}
                  <div className="mt-1 text-[11px]" style={{ color: "var(--muted)" }}>
                    {t.next_run ? `Next: ${fmt(t.next_run)}` : ""}
                    {t.last_run ? ` · Last: ${fmt(t.last_run)} ${t.last_run_status || ""}` : ""}
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-0.5">
                  <button className="icon-btn" title="Run now" disabled={busy === t.id} onClick={() => act(t.id, () => api.run(t.id), "Started")}>
                    <Play size={14} />
                  </button>
                  {paused ? (
                    <button className="icon-btn" title="Resume" onClick={() => act(t.id, () => api.resume(t.id))}>
                      <RefreshCw size={14} />
                    </button>
                  ) : (
                    <button className="icon-btn" title="Pause" onClick={() => act(t.id, () => api.pause(t.id))}>
                      <Pause size={14} />
                    </button>
                  )}
                  <button className="icon-btn" title="Stop" onClick={() => act(t.id, () => api.stop(t.id))}>
                    <Square size={13} />
                  </button>
                  <button className="icon-btn hover:!text-red-400" title="Delete" onClick={() => { if (window.confirm(`Delete task “${t.name}”?`)) act(t.id, () => api.remove(t.id), "Deleted"); }}>
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}

function fmt(s: string) {
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
