import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { motion, AnimatePresence } from "motion/react";
import { ChevronDown, Cpu, Cloud, RefreshCw, Check, Search, HardDrive } from "lucide-react";
import { useApp } from "../store/app";
import { displayModel, isLocalUrl, urlsMatch } from "../lib/ui";

export default function ModelPicker() {
  const items = useApp((s) => s.modelItems);
  const route = useApp((s) => s.route);
  const selectModel = useApp((s) => s.selectModel);
  const setView = useApp((s) => s.setView);
  const loadModels = useApp((s) => s.loadModels);
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [hi, setHi] = useState(0);
  const [pos, setPos] = useState({ top: 0, left: 0 });
  const ref = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  const place = () => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const width = 360;
    const left = Math.min(Math.max(8, r.left), window.innerWidth - width - 8);
    const top = Math.min(r.bottom + 6, window.innerHeight - 80);
    setPos({ top, left });
  };

  useEffect(() => {
    if (!open) return;
    place();
    const h = (e: MouseEvent) => {
      const t = e.target as Node;
      if (ref.current?.contains(t)) return;
      if ((t as HTMLElement).closest?.("[data-model-menu]")) return;
      setOpen(false);
    };
    const onResize = () => place();
    window.addEventListener("mousedown", h);
    window.addEventListener("resize", onResize);
    window.addEventListener("scroll", onResize, true);
    return () => {
      window.removeEventListener("mousedown", h);
      window.removeEventListener("resize", onResize);
      window.removeEventListener("scroll", onResize, true);
    };
  }, [open]);

  const groups = useMemo(
    () =>
      items
        .map((i) => {
          const models = [...i.models, ...(i.models_extra || [])];
          const displays = [...(i.models_display || []), ...(i.models_extra_display || [])];
          const rows = models
            .map((m, idx) => ({ id: m, label: displayModel(m, displays[idx]) }))
            .filter((m) => !q || m.id.toLowerCase().includes(q.toLowerCase()) || m.label.toLowerCase().includes(q.toLowerCase()));
          return { item: i, models: rows };
        })
        .filter((g) => g.models.length || (g.item.offline && !q)),
    [items, q],
  );

  const flat = useMemo(
    () => groups.flatMap((g) => g.models.map((m) => ({ ...m, item: g.item }))),
    [groups],
  );

  useEffect(() => setHi(0), [q, open]);

  const pick = (m: string, item: (typeof items)[number]) => {
    if (item.offline) return;
    selectModel({ model: m, endpoint_id: item.endpoint_id || "", endpoint_url: item.url, endpoint_name: item.endpoint_name });
    setOpen(false);
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (!open) return;
    if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setHi((v) => Math.min(flat.length - 1, v + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHi((v) => Math.max(0, v - 1));
    } else if (e.key === "Enter" && flat[hi]) {
      e.preventDefault();
      pick(flat[hi].id, flat[hi].item);
    }
  };

  const selected = (m: string, item: (typeof items)[number]) =>
    !!route &&
    route.model === m &&
    (route.endpoint_id && item.endpoint_id ? route.endpoint_id === item.endpoint_id : urlsMatch(route.endpoint_url, item.url));

  const label = route ? displayModel(route.model) : "No model";

  return (
    <div ref={ref} className="relative">
      <button
        className="btn h-9 max-w-[280px] gap-2 rounded-full px-3 text-[13px]"
        onClick={() => setOpen((v) => !v)}
        title={route ? `${route.model}\n${route.endpoint_name || route.endpoint_url}` : "Choose a model"}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: route ? "var(--accent)" : "var(--muted)", boxShadow: route ? "0 0 8px var(--glow)" : "none" }} />
        <span className="truncate">{label}</span>
        <ChevronDown size={14} className="shrink-0 transition-transform" style={{ transform: open ? "rotate(180deg)" : "none", color: "var(--muted)" }} />
      </button>
      {createPortal(
        <AnimatePresence>
          {open && (
            <motion.div
              data-model-menu
              initial={{ opacity: 0, y: 6, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 6, scale: 0.98 }}
              transition={{ duration: 0.16 }}
              className="glass fixed z-[85] w-[360px] overflow-hidden rounded-2xl"
              style={{ top: pos.top, left: pos.left, boxShadow: "var(--shadow)" }}
              onKeyDown={onKey}
            >
              <div className="flex items-center gap-2 p-2" style={{ borderBottom: "1px solid var(--border)" }}>
                <div className="relative flex-1">
                  <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2" style={{ color: "var(--muted)" }} />
                  <input
                    ref={searchRef}
                    autoFocus
                    className="input h-8 pl-8 text-[13px]"
                    placeholder="Search models"
                    value={q}
                    onChange={(e) => setQ(e.target.value)}
                    onKeyDown={onKey}
                  />
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
              <div className="max-h-[360px] overflow-y-auto p-1.5" role="listbox">
                {groups.length === 0 && (
                  <div className="px-3 py-6 text-center text-sm" style={{ color: "var(--muted)" }}>
                    No models found. Download one from Models, or add an endpoint in Settings.
                  </div>
                )}
                {groups.map(({ item, models }) => (
                  <div key={item.endpoint_id || item.url} className="mb-1">
                    <div className="flex items-center gap-1.5 px-2.5 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-wider" style={{ color: "var(--muted)" }}>
                      {isLocalUrl(item.url, item.category) ? <Cpu size={12} /> : <Cloud size={12} />}
                      <span className="truncate">{item.endpoint_name || item.url}</span>
                      {item.model_type && item.model_type !== "llm" && (
                        <span className="rounded-full px-1.5 py-0.5 text-[9px] font-bold" style={{ background: "var(--bg-sunken)" }}>
                          {item.model_type}
                        </span>
                      )}
                      {item.offline && (
                        <span className="ml-auto rounded-full px-1.5 py-0.5 text-[9px] font-bold text-red-400" style={{ background: "rgba(239,68,68,.12)" }}>
                          OFFLINE
                        </span>
                      )}
                    </div>
                    {models.map((m) => {
                      const on = selected(m.id, item);
                      const idx = flat.findIndex((f) => f.id === m.id && f.item === item);
                      return (
                        <button
                          key={m.id}
                          role="option"
                          aria-selected={on}
                          disabled={item.offline}
                          className="flex w-full items-center gap-2 rounded-xl px-2.5 py-2 text-left text-[13px] transition-colors hover:bg-[var(--accent-soft)] disabled:cursor-not-allowed"
                          style={{ background: on || idx === hi ? "var(--accent-soft)" : undefined, opacity: item.offline ? 0.5 : 1 }}
                          onMouseEnter={() => setHi(idx)}
                          onClick={() => pick(m.id, item)}
                        >
                          <span className="flex-1 truncate" title={m.id}>
                            {m.label}
                          </span>
                          {on && <Check size={14} style={{ color: "var(--accent)" }} />}
                        </button>
                      );
                    })}
                  </div>
                ))}
              </div>
              <div className="p-1.5" style={{ borderTop: "1px solid var(--border)" }}>
                <button
                  className="flex w-full items-center gap-2 rounded-xl px-2.5 py-2 text-left text-[13px] transition-colors hover:bg-[var(--accent-soft)]"
                  onClick={() => {
                    setView("models");
                    setOpen(false);
                  }}
                >
                  <HardDrive size={14} style={{ color: "var(--accent)" }} />
                  Download or serve a model
                </button>
              </div>
            </motion.div>
          )}
        </AnimatePresence>,
        document.body,
      )}
    </div>
  );
}
