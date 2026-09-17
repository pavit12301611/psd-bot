import { useEffect, useMemo, useRef, useState } from "react";
import { motion, AnimatePresence } from "motion/react";
import { ChevronDown, Cpu, Cloud, RefreshCw, Check, Search } from "lucide-react";
import { useApp } from "../store/app";

export default function ModelPicker() {
  const items = useApp((s) => s.modelItems);
  const route = useApp((s) => s.route);
  const setRoute = useApp((s) => s.setRoute);
  const loadModels = useApp((s) => s.loadModels);
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const h = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", h);
    return () => window.removeEventListener("mousedown", h);
  }, [open]);

  const groups = useMemo(
    () =>
      items
        .map((i) => ({
          item: i,
          models: [...i.models, ...(i.models_extra || [])].filter((m) => !q || m.toLowerCase().includes(q.toLowerCase())),
        }))
        .filter((g) => g.models.length || (g.item.offline && !q)),
    [items, q],
  );

  const isLocal = (i: { url: string; category?: string }) => /127\.0\.0\.1|localhost|0\.0\.0\.0|\.local\b|192\.168\.|10\.\d+\./.test(i.url) || i.category === "local";
  const label = route ? route.model.split("/").pop() : "No model";

  return (
    <div ref={ref} className="relative">
      <button className="btn h-9 max-w-[280px] gap-2 rounded-full px-3 text-[13px]" onClick={() => setOpen((v) => !v)} title={route ? `${route.model}\n${route.endpoint_name || route.endpoint_url}` : "Choose a model"}>
        <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: route ? "var(--accent)" : "var(--muted)", boxShadow: route ? "0 0 8px var(--glow)" : "none" }} />
        <span className="truncate">{label}</span>
        <ChevronDown size={14} className="shrink-0 transition-transform" style={{ transform: open ? "rotate(180deg)" : "none", color: "var(--muted)" }} />
      </button>
      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: 6, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 6, scale: 0.98 }}
            transition={{ duration: 0.16 }}
            className="glass absolute left-0 top-11 z-40 w-[360px] overflow-hidden rounded-2xl"
            style={{ boxShadow: "var(--shadow)" }}
          >
            <div className="flex items-center gap-2 p-2" style={{ borderBottom: "1px solid var(--border)" }}>
              <div className="relative flex-1">
                <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2" style={{ color: "var(--muted)" }} />
                <input autoFocus className="input h-8 pl-8 text-[13px]" placeholder="Search models" value={q} onChange={(e) => setQ(e.target.value)} />
              </div>
              <button
                className="icon-btn h-8 w-8"
                title="Refresh"
                onClick={async () => {
                  setBusy(true);
                  await loadModels(true);
                  setBusy(false);
                }}
              >
                <RefreshCw size={14} className={busy ? "animate-spin" : ""} />
              </button>
            </div>
            <div className="max-h-[360px] overflow-y-auto p-1.5">
              {groups.length === 0 && (
                <div className="px-3 py-6 text-center text-sm" style={{ color: "var(--muted)" }}>
                  No models found. Add an endpoint in Settings, or wait for the local model group to finish downloading.
                </div>
              )}
              {groups.map(({ item, models }) => (
                <div key={item.endpoint_id || item.url} className="mb-1">
                  <div className="flex items-center gap-1.5 px-2.5 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-wider" style={{ color: "var(--muted)" }}>
                    {isLocal(item) ? <Cpu size={12} /> : <Cloud size={12} />}
                    <span className="truncate">{item.endpoint_name || item.url}</span>
                    {item.offline && <span className="ml-auto rounded-full px-1.5 py-0.5 text-[9px] font-bold text-red-400" style={{ background: "rgba(239,68,68,.12)" }}>OFFLINE</span>}
                  </div>
                  {models.map((m) => {
                    const on = route?.model === m && (route.endpoint_id === item.endpoint_id || route.endpoint_url === item.url);
                    return (
                      <button
                        key={m}
                        className="flex w-full items-center gap-2 rounded-xl px-2.5 py-2 text-left text-[13px] transition-colors hover:bg-[var(--accent-soft)]"
                        style={{ background: on ? "var(--accent-soft)" : undefined, opacity: item.offline ? 0.5 : 1 }}
                        onClick={() => {
                          setRoute({ model: m, endpoint_id: item.endpoint_id || "", endpoint_url: item.url, endpoint_name: item.endpoint_name });
                          setOpen(false);
                        }}
                      >
                        <span className="flex-1 truncate">{m}</span>
                        {on && <Check size={14} style={{ color: "var(--accent)" }} />}
                      </button>
                    );
                  })}
                </div>
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
