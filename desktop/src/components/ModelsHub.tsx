import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Download,
  HardDrive,
  RefreshCw,
  Search,
  Sparkles,
  Box,
  Play,
  Loader2,
  CheckCircle2,
  AlertCircle,
} from "lucide-react";
import { useApp } from "../store/app";
import {
  cookbook,
  hwfit,
  type CachedModel,
  type CookbookTaskStatus,
  type HwfitModel,
  type OllamaLibModel,
} from "../lib/api";
import { Empty, PanelHead } from "./media";

type Tab = "fit" | "huggingface" | "ollama" | "cached" | "jobs";

function shortName(id?: string) {
  return (id || "").split("/").pop() || id || "model";
}

function fitColor(level?: string) {
  if (level === "perfect") return "#34d399";
  if (level === "good") return "#61afef";
  if (level === "marginal") return "#e5c07b";
  return "#f87171";
}

function progressPct(progress?: string) {
  const m = String(progress || "").match(/(\d+)%/);
  return m ? Math.min(100, Number(m[1])) : null;
}

async function rememberCookbookTask(sessionId: string, name: string, type: "download" | "serve", payload: Record<string, unknown>) {
  let state: any = {};
  try {
    state = await cookbook.state();
  } catch {
    state = {};
  }
  const tasks = Array.isArray(state.tasks) ? [...state.tasks] : [];
  if (!tasks.some((t: any) => t?.sessionId === sessionId)) {
    tasks.push({
      id: sessionId,
      sessionId,
      name,
      type,
      status: "running",
      output: "",
      ts: Date.now(),
      payload,
      remoteHost: "",
    });
  }
  await cookbook.saveState({ ...state, tasks }).catch(() => {});
}

function ggufPath(m: CachedModel) {
  const file = m.gguf_files?.[0];
  if (file && (file.includes("/") || file.includes("\\"))) return file;
  if (file && m.path) return `${m.path.replace(/[/\\]$/, "")}/${file}`;
  return m.path || m.repo_id;
}

export default function ModelsHub() {
  const toast = useApp((s) => s.toast);
  const isAdmin = useApp((s) => s.authStatus?.is_admin);
  const loadModels = useApp((s) => s.loadModels);
  const setView = useApp((s) => s.setView);
  const [tab, setTab] = useState<Tab>("fit");
  const [q, setQ] = useState("");
  const [custom, setCustom] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [fit, setFit] = useState<HwfitModel[]>([]);
  const [sys, setSys] = useState<any>(null);
  const [hf, setHf] = useState<{ repo_id: string; est_vram_gb?: number; likes?: number }[]>([]);
  const [ollama, setOllama] = useState<OllamaLibModel[]>([]);
  const [cached, setCached] = useState<CachedModel[]>([]);
  const [jobs, setJobs] = useState<CookbookTaskStatus[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [port, setPort] = useState(8080);
  const [ggufPick, setGgufPick] = useState<{ repo: string; files: string[] } | null>(null);

  const loadCatalogs = useCallback(async () => {
    setErr(null);
    try {
      const [hw, latest, lib, cache] = await Promise.all([
        hwfit.models({ limit: 36, fit_only: true }).catch((e: any) => ({ models: [], system: null, error: e.message })),
        cookbook.hfLatest(16).catch(() => ({ models: [] })),
        cookbook.ollamaLibrary().catch(() => ({ models: [] })),
        cookbook.cached().catch(() => ({ models: [] })),
      ]);
      let fitModels = hw.models || [];
      if (!fitModels.length) {
        const all = await hwfit.models({ limit: 36, fit_only: false }).catch(() => ({ models: [] as HwfitModel[] }));
        fitModels = all.models || [];
      }
      setFit(fitModels);
      setSys(hw.system);
      if (hw.error) setErr(hw.error);
      setHf(latest.models || []);
      setOllama(lib.models || []);
      setCached(cache.models || []);
    } catch (e: any) {
      setErr(e.message || "Could not load model catalogs");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadJobs = useCallback(async () => {
    try {
      const r = await cookbook.taskStatus();
      setJobs(r.tasks || []);
    } catch {
      /* engine may be offline */
    }
  }, []);

  useEffect(() => {
    loadCatalogs();
  }, [loadCatalogs]);

  useEffect(() => {
    loadJobs();
    const t = setInterval(loadJobs, 3000);
    return () => clearInterval(t);
  }, [loadJobs]);

  const live = jobs.filter((j) => j.status === "running" || j.status === "ready" || j.status === "queued");
  const doneJobs = jobs.filter((j) => j.status === "completed" || j.status === "done");

  const doneCount = doneJobs.length;
  const prevDone = useRef(0);
  useEffect(() => {
    if (doneCount > prevDone.current) loadModels(true).catch(() => {});
    prevDone.current = doneCount;
  }, [doneCount, loadModels]);

  const startDownload = async (opts: { repo_id: string; backend?: "hf" | "ollama"; include?: string; required_gb?: number }) => {
    if (!isAdmin) {
      toast("Only an administrator can download models.", "error");
      return;
    }
    const repo = opts.repo_id.trim();
    if (!repo) return;
    const key = `${opts.backend || "hf"}:${repo}:${opts.include || ""}`;
    if (busy === key) return;
    setBusy(key);
    try {
      const payload = {
        repo_id: repo,
        backend: opts.backend || (repo.includes("/") ? "hf" : "ollama"),
        include: opts.include,
        disable_hf_transfer: (opts.required_gb || 0) >= 10 || !!opts.include,
      };
      const r = await cookbook.download(payload);
      if (!r.ok) throw new Error(r.error || "Download failed");
      if (r.session_id) await rememberCookbookTask(r.session_id, shortName(repo), "download", payload);
      toast(`Downloading ${shortName(repo)}…`, "success");
      setTab("jobs");
      await loadJobs();
    } catch (e: any) {
      toast(e.message || "Download failed", "error");
    } finally {
      setBusy(null);
    }
  };

  const serveCached = async (m: CachedModel) => {
    if (!isAdmin) {
      toast("Only an administrator can serve models.", "error");
      return;
    }
    setBusy(`serve:${m.repo_id}`);
    try {
      if (m.is_ollama) {
        await loadModels(true);
        toast(`${shortName(m.repo_id)} is on disk. Pick it from the model menu once Ollama is online.`, "success");
        setView("chat");
        return;
      }
      const path = ggufPath(m);
      const cmd = m.is_gguf
        ? `llama-server -m ${JSON.stringify(path)} --host 127.0.0.1 --port ${port} -c 8192`
        : `llama-server --host 127.0.0.1 --port ${port} -c 8192`;
      const r = await cookbook.serve({ repo_id: m.repo_id, cmd });
      if (!r.ok) throw new Error(r.error || "Serve failed");
      if (r.session_id) await rememberCookbookTask(r.session_id, shortName(m.repo_id), "serve", { repo_id: m.repo_id, _cmd: cmd });
      toast(`Serving ${shortName(m.repo_id)}…`, "success");
      setTab("jobs");
      await loadJobs();
      setTimeout(() => loadModels(true), 4000);
    } catch (e: any) {
      toast(e.message || "Serve failed", "error");
    } finally {
      setBusy(null);
    }
  };

  const fitRows = useMemo(() => {
    const n = q.trim().toLowerCase();
    return fit.filter((m) => !n || (m.name || "").toLowerCase().includes(n) || (m.provider || "").toLowerCase().includes(n));
  }, [fit, q]);

  const ollamaRows = useMemo(() => {
    const n = q.trim().toLowerCase();
    return ollama.filter((m) => !n || m.name.toLowerCase().includes(n) || (m.description || "").toLowerCase().includes(n));
  }, [ollama, q]);

  const tabs: { id: Tab; label: string }[] = [
    { id: "fit", label: "For this machine" },
    { id: "huggingface", label: "Hugging Face" },
    { id: "ollama", label: "Ollama" },
    { id: "cached", label: `On disk${cached.length ? ` (${cached.length})` : ""}` },
    { id: "jobs", label: live.length ? `Downloads (${live.length})` : "Downloads" },
  ];

  return (
    <section className="panel">
      <PanelHead icon={<HardDrive size={16} />} title="Models">
        {sys?.gpu_name && (
          <span className="pill" title="Detected hardware">
            {sys.gpu_name}
            {sys.gpu_vram_gb ? ` · ${sys.gpu_vram_gb} GB` : ""}
          </span>
        )}
        <button className="btn h-8" onClick={() => { setLoading(true); loadCatalogs(); loadJobs(); }} disabled={loading}>
          <RefreshCw size={14} className={loading ? "animate-spin" : ""} /> Refresh
        </button>
      </PanelHead>

      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex flex-col gap-3 px-5 pt-4">
          <div className="flex flex-wrap gap-2">
            <input
              className="input max-w-xl flex-1"
              placeholder="Paste a Hugging Face repo (org/name) or an Ollama tag"
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && startDownload({ repo_id: custom })}
            />
            <label className="flex items-center gap-2 text-[12px]" style={{ color: "var(--muted)" }}>
              Port
              <input className="input h-10 w-24" type="number" min={1024} max={65535} value={port} onChange={(e) => setPort(Number(e.target.value) || 8080)} />
            </label>
            <button className="btn btn-primary h-10" disabled={!custom.trim() || !!busy} onClick={() => startDownload({ repo_id: custom })}>
              {busy?.includes(custom.trim()) ? <Loader2 size={15} className="animate-spin" /> : <Download size={15} />}
              Download
            </button>
          </div>
          <div className="relative">
            <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2" style={{ color: "var(--muted)" }} />
            <input className="input h-9 pl-9 text-[13px]" placeholder="Filter catalogs" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
          <div className="flex flex-wrap gap-1">
            {tabs.map((t) => (
              <button key={t.id} className="pill" data-on={tab === t.id} onClick={() => setTab(t.id)}>
                {t.label}
              </button>
            ))}
          </div>
          {err && (
            <p className="text-[12px]" style={{ color: "#f87171" }}>
              {err}
            </p>
          )}
          {!isAdmin && (
            <p className="text-[12px]" style={{ color: "var(--muted)" }}>
              Downloads are admin-only. You can still browse catalogs and pick models that are already served.
            </p>
          )}
        </div>

        <div className="panel-body">
          {loading && fit.length === 0 && jobs.length === 0 && (
            <div className="flex flex-col gap-2">
              {[1, 2, 3, 4].map((i) => (
                <div key={i} className="shimmer h-16 rounded-2xl" style={{ background: "var(--bg-sunken)" }} />
              ))}
            </div>
          )}

          {tab === "fit" && (
            <CatalogGrid empty="No catalog matches this machine yet.">
              {fitRows.map((m) => {
                const src = m.gguf_sources?.[0];
                const repo = src?.repo || m.name || "";
                const include = src?.file || undefined;
                const key = `hf:${repo}:${include || ""}`;
                return (
                  <ModelCard
                    key={`${m.name}-${m.quant}`}
                    title={shortName(m.name)}
                    subtitle={`${m.parameter_count || (m.params_b ? `${m.params_b}B` : "")}${m.quant ? ` · ${m.quant}` : ""}${m.required_gb ? ` · ${m.required_gb} GB` : ""}`}
                    badge={m.fit_level}
                    badgeColor={fitColor(m.fit_level)}
                    hint={m.provider}
                    actionLabel={busy === key ? "Starting…" : "Download"}
                    disabled={!repo || !!busy}
                    onAction={() => startDownload({ repo_id: repo, backend: repo.includes("/") ? "hf" : "ollama", include, required_gb: m.required_gb })}
                  />
                );
              })}
            </CatalogGrid>
          )}

          {tab === "huggingface" && (
            <CatalogGrid empty="Could not load trending Hugging Face models.">
              {hf
                .filter((m) => !q || m.repo_id.toLowerCase().includes(q.toLowerCase()))
                .map((m) => (
                  <ModelCard
                    key={m.repo_id}
                    title={shortName(m.repo_id)}
                    subtitle={m.repo_id}
                    hint={m.est_vram_gb ? `~${m.est_vram_gb} GB fp16` : undefined}
                    actionLabel="Download"
                    disabled={!!busy}
                    onAction={async () => {
                      try {
                        const files = await cookbook.hfGgufFiles(m.repo_id);
                        const list = files.files || files.gguf_files || [];
                        if (list.length > 1) {
                          setGgufPick({ repo: m.repo_id, files: list });
                          return;
                        }
                      } catch { /* ignore */ }
                      startDownload({ repo_id: m.repo_id, backend: "hf", required_gb: m.est_vram_gb });
                    }}
                  />
                ))}
            </CatalogGrid>
          )}

          {tab === "ollama" && (
            <CatalogGrid empty="Ollama library unavailable.">
              {ollamaRows.map((m) => (
                <div key={m.name} className="card flex flex-col gap-2">
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <div className="text-[14px] font-semibold">{m.name}</div>
                      {m.description && (
                        <p className="mt-0.5 text-[12px] leading-snug" style={{ color: "var(--muted)" }}>
                          {m.description}
                        </p>
                      )}
                    </div>
                    <button className="btn btn-primary h-8 shrink-0" disabled={!!busy} onClick={() => startDownload({ repo_id: m.name, backend: "ollama" })}>
                      <Download size={13} /> Pull
                    </button>
                  </div>
                  {!!m.sizes?.length && (
                    <div className="flex flex-wrap gap-1">
                      {m.sizes.slice(0, 8).map((s) => (
                        <button key={s} className="pill" disabled={!!busy} onClick={() => startDownload({ repo_id: `${m.name}:${s}`, backend: "ollama" })}>
                          {s}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </CatalogGrid>
          )}

          {tab === "cached" && (
            <>
              {cached.length === 0 && !loading && (
                <Empty icon={<Box size={28} />} title="Nothing on disk yet" hint="Download a Hugging Face repo or pull an Ollama model. Finished files show up here." />
              )}
              <div className="grid grid-cols-1 gap-2 lg:grid-cols-2">
                {cached.map((m) => (
                  <div key={m.repo_id + (m.path || "")} className="card flex items-center gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-[14px] font-semibold">{shortName(m.repo_id)}</div>
                      <div className="truncate text-[12px]" style={{ color: "var(--muted)" }}>
                        {m.size || ""} {m.is_ollama ? "· Ollama" : m.is_gguf ? "· GGUF" : ""} {m.status === "downloading" ? "· still writing" : "· ready"}
                      </div>
                    </div>
                    <button className="btn h-8" disabled={!!busy || m.status === "downloading"} onClick={() => serveCached(m)} title={m.status === "downloading" ? "Still writing to disk" : "Serve"}>
                      {busy === `serve:${m.repo_id}` ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
                      Serve
                    </button>
                  </div>
                ))}
              </div>
            </>
          )}

          {tab === "jobs" && (
            <>
              {jobs.length === 0 && !loading && (
                <Empty icon={<Sparkles size={28} />} title="No downloads yet" hint="Pick a model above. Progress appears here while Hugging Face or Ollama writes files." />
              )}
              <div className="flex flex-col gap-2">
                {jobs.map((j) => {
                  const pct = progressPct(j.progress);
                  const running = j.status === "running" || j.status === "queued" || j.status === "ready";
                  const ok = j.status === "completed" || j.status === "done";
                  const bad = j.status === "error" || j.status === "crashed" || j.status === "failed";
                  return (
                    <div key={j.session_id} className="card">
                      <div className="flex items-center gap-2">
                        {running ? (
                          <Loader2 size={15} className="animate-spin" style={{ color: "var(--accent)" }} />
                        ) : ok ? (
                          <CheckCircle2 size={15} style={{ color: "#34d399" }} />
                        ) : (
                          <AlertCircle size={15} style={{ color: bad ? "#f87171" : "var(--muted)" }} />
                        )}
                        <div className="min-w-0 flex-1">
                          <div className="truncate text-[13px] font-medium">{j.model || j.session_id}</div>
                          <div className="text-[11px]" style={{ color: "var(--muted)" }}>
                            {j.type} · {j.progress || j.phase || j.status}
                          </div>
                        </div>
                        {running && (
                          <button
                            className="btn h-7 text-[11px]"
                            onClick={async () => {
                              try {
                                const state = await cookbook.state();
                                const task = (state.tasks || []).find((t: any) => t.sessionId === j.session_id || t.id === j.session_id);
                                const pid = Number(task?.pid || task?.processId || (j as any).pid || 0);
                                if (pid > 0) await cookbook.killPid(pid).catch(() => {});
                                const tasks = (state.tasks || []).filter((t: any) => t.sessionId !== j.session_id && t.id !== j.session_id);
                                await cookbook.saveState({ ...state, tasks, removedTasks: [...(state.removedTasks || []), j.session_id] });
                                toast(pid > 0 ? "Stopped the download process." : "Dismissed from the list. The download may still finish on disk.", pid > 0 ? "success" : "info");
                                loadJobs();
                              } catch (e: any) {
                                toast(e.message || "Couldn't dismiss", "error");
                              }
                            }}
                          >
                            Dismiss
                          </button>
                        )}
                      </div>
                      {pct != null && (
                        <div className="mt-2 h-1.5 overflow-hidden rounded-full" style={{ background: "var(--bg-sunken)" }}>
                          <div className="h-full rounded-full" style={{ width: `${pct}%`, background: "var(--accent)" }} />
                        </div>
                      )}
                      {j.output_tail && (
                        <pre className="selectable mt-2 max-h-28 overflow-auto rounded-xl p-2 text-[10px] leading-relaxed" style={{ background: "var(--code-bg)", color: "#c9cee0", fontFamily: "var(--font-mono)" }}>
                          {j.output_tail.slice(-800)}
                        </pre>
                      )}
                    </div>
                  );
                })}
              </div>
            </>
          )}
        </div>
      </div>
      {ggufPick && (
        <div className="fixed inset-0 z-[85] flex items-center justify-center p-6" style={{ background: "rgba(0,0,0,.45)" }} onMouseDown={(e) => e.target === e.currentTarget && setGgufPick(null)}>
          <div className="glass max-h-[70vh] w-full max-w-md overflow-auto rounded-2xl p-4">
            <h3 className="mb-2 font-semibold">Pick a GGUF file</h3>
            <div className="flex flex-col gap-1">
              {ggufPick.files.map((f) => (
                <button
                  key={f}
                  className="btn justify-start text-left"
                  onClick={() => {
                    startDownload({ repo_id: ggufPick.repo, backend: "hf", include: f });
                    setGgufPick(null);
                  }}
                >
                  {f}
                </button>
              ))}
            </div>
            <button className="btn mt-3" onClick={() => setGgufPick(null)}>Cancel</button>
          </div>
        </div>
      )}
    </section>
  );
}

function CatalogGrid({ children, empty }: { children: ReactNode; empty: string }) {
  const arr = Array.isArray(children) ? children : [children];
  if (!arr.filter(Boolean).length) {
    return <Empty icon={<Download size={28} />} title={empty} />;
  }
  return <div className="grid grid-cols-1 gap-2 lg:grid-cols-2">{children}</div>;
}

function ModelCard({
  title,
  subtitle,
  hint,
  badge,
  badgeColor,
  actionLabel,
  disabled,
  onAction,
}: {
  title: string;
  subtitle?: string;
  hint?: string;
  badge?: string;
  badgeColor?: string;
  actionLabel: string;
  disabled?: boolean;
  onAction: () => void;
}) {
  return (
    <div className="card flex items-center gap-3">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-[14px] font-semibold">{title}</span>
          {badge && (
            <span className="rounded-full px-1.5 py-0.5 text-[10px] font-semibold uppercase" style={{ color: badgeColor, background: "var(--bg-sunken)" }}>
              {badge.replace("_", " ")}
            </span>
          )}
        </div>
        {subtitle && (
          <div className="truncate text-[12px]" style={{ color: "var(--muted)" }}>
            {subtitle}
          </div>
        )}
        {hint && (
          <div className="truncate text-[11px]" style={{ color: "var(--muted)" }}>
            {hint}
          </div>
        )}
      </div>
      <button className="btn btn-primary h-8 shrink-0" disabled={disabled} onClick={onAction}>
        <Download size={13} /> {actionLabel}
      </button>
    </div>
  );
}
