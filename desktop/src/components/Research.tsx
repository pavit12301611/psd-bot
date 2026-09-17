import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Telescope, Plus, Trash2, Square, ExternalLink } from "lucide-react";
import { research as api, type ResearchItem } from "../lib/api";
import { useApp } from "../store/app";
import { Empty, PanelHead } from "./media";
import { openExternal } from "../lib/ui";

export default function Research() {
  const toast = useApp((s) => s.toast);
  const route = useApp((s) => s.route);
  const [items, setItems] = useState<ResearchItem[]>([]);
  const [query, setQuery] = useState("");
  const [maxTime, setMaxTime] = useState(300);
  const [running, setRunning] = useState<string | null>(null);
  const [status, setStatus] = useState<any>(null);
  const [detail, setDetail] = useState<any>(null);

  const load = async () => {
    try {
      const r = await api.library();
      setItems(r.research || []);
    } catch (e: any) {
      toast(e.message || "Could not load research", "error");
      setItems([]);
    }
  };
  useEffect(() => {
    load();
  }, []);

  useEffect(() => {
    if (!running) return;
    const t = setInterval(async () => {
      try {
        const s = await api.status(running);
        setStatus(s);
        const st = (s.status || s.state || "").toLowerCase();
        if (st === "done" || st === "complete" || st === "error" || st === "cancelled") {
          setRunning(null);
          load();
          if (st !== "error") toast("Research finished", "success");
        }
      } catch {
        /* still running */
      }
    }, 2000);
    return () => {
      clearInterval(t);
      api.cancel(running).catch(() => {});
    };
  }, [running]);

  const start = async () => {
    if (!query.trim()) return;
    try {
      const r = await api.start(query.trim(), { max_time: maxTime, model: route?.model });
      setRunning(r.session_id);
      setStatus({ status: "running", query: r.query });
      setQuery("");
      toast("Research started", "success");
    } catch (e: any) {
      toast(e.message || "Could not start research", "error");
    }
  };

  const open = async (id: string) => {
    try {
      const d = await api.detail(id);
      setDetail(d);
    } catch (e: any) {
      toast(e.message || "Could not open report", "error");
    }
  };

  return (
    <section className="panel">
      <PanelHead icon={<Telescope size={16} />} title="Deep Research">
        {running && (
          <button className="btn h-8" onClick={async () => { await api.cancel(running); setRunning(null); }}>
            <Square size={12} /> Cancel
          </button>
        )}
      </PanelHead>
      <div className="panel-body">
        <div className="card mb-4 flex flex-col gap-2">
          <p className="text-[13px]" style={{ color: "var(--muted)" }}>
            Multi-round web research. The agent searches, reads sources, and writes a report.
          </p>
          <div className="flex gap-2">
            <input className="input flex-1" placeholder="What should I research?" value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => e.key === "Enter" && start()} disabled={!!running} />
            <label className="flex items-center gap-2 text-[12px]" style={{ color: "var(--muted)" }}>
              {Math.round(maxTime / 60)} min
              <input type="range" min={60} max={900} step={30} value={maxTime} onChange={(e) => setMaxTime(Number(e.target.value))} />
            </label>
            <button className="btn btn-primary" disabled={!query.trim() || !!running} onClick={start}>
              <Plus size={14} /> Start
            </button>
          </div>
          {running && (
            <div className="mt-1 text-[13px]" style={{ color: "var(--accent)" }}>
              <span className="dots mr-2 inline-flex"><span /><span /><span /></span>
              {status?.status || "running"} {status?.query ? `· ${status.query}` : ""}
            </div>
          )}
        </div>
        {items.length === 0 && !running && <Empty icon={<Telescope size={36} />} title="No research yet" hint="Ask a question above to kick off a deep research run." />}
        <div className="flex flex-col gap-2">
          {items.map((it) => (
            <div key={it.id} className="card flex cursor-pointer items-start gap-3" onClick={() => open(it.id)}>
              <div className="min-w-0 flex-1">
                <div className="font-medium">{it.query}</div>
                <div className="mt-0.5 text-[12px]" style={{ color: "var(--muted)" }}>
                  {it.status || "done"} · {it.source_count ?? 0} sources {it.duration ? `· ${it.duration}` : ""}
                </div>
              </div>
              <button className="icon-btn hover:!text-red-400" onClick={async (e) => { e.stopPropagation(); await api.remove(it.id); load(); }}>
                <Trash2 size={14} />
              </button>
            </div>
          ))}
        </div>
        {detail && (
          <div className="card mt-4">
            <div className="mb-2 flex items-center justify-between">
              <h3 className="font-semibold">{detail.query || "Report"}</h3>
              <button className="btn h-8" onClick={() => setDetail(null)}>Close</button>
            </div>
            <div className="md text-[13.5px] leading-relaxed">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{detail.summary || detail.report || JSON.stringify(detail.stats || {}, null, 2)}</ReactMarkdown>
            </div>
            {Array.isArray(detail.sources) && detail.sources.length > 0 && (
              <ul className="mt-3 flex flex-col gap-1">
                {detail.sources.slice(0, 12).map((s: any, i: number) => (
                  <li key={i} className="flex items-center gap-1.5 text-[13px]">
                    <ExternalLink size={12} style={{ color: "var(--muted)" }} />
                    <a className="truncate hover:underline" style={{ color: "var(--accent)" }} href={s.url} onClick={(e) => { e.preventDefault(); if (s.url) openExternal(s.url); }}>
                      {s.title || s.url}
                    </a>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
