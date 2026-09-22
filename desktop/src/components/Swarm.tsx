import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { motion, AnimatePresence } from "motion/react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Network,
  Crown,
  Globe,
  RefreshCw,
  Send,
  Settings2,
  Brain,
  Trash2,
  Link2,
  ChevronDown,
  Check,
  AlertCircle,
  Loader2,
  Zap,
  User,
  Plus,
  Eye,
  Code2,
  Lightbulb,
  Search,
  Gauge,
  ShieldCheck,
  Cloud,
} from "lucide-react";
import { useApp } from "../store/app";
import {
  swarm as swarmApi,
  type SwarmStatus,
  type SwarmWorkerInfo,
  type SwarmStepDone,
} from "../lib/api";
import { Empty, PanelHead } from "./media";
import { openExternal } from "../lib/ui";

const KIND_ICON: Record<string, ReactNode> = {
  coding: <Code2 size={12} />,
  reasoning: <Lightbulb size={12} />,
  research: <Search size={12} />,
  vision: <Eye size={12} />,
  fast: <Zap size={12} />,
  general: <Network size={12} />,
};

const KIND_COLOR: Record<string, string> = {
  coding: "#7ee787",
  reasoning: "#c792ea",
  research: "#61afef",
  vision: "#e5c07b",
  fast: "#ffcb6b",
  general: "#89ddff",
};

interface WorkerReport {
  index: number;
  kind: string;
  task: string;
  worker: string;
  ok: boolean;
  text: string;
  elapsed_s: number;
  tps: number;
  error: string;
}

interface SwarmTurn {
  id: string;
  question: string;
  plan: { kind: string; task: string; worker: string; kind_label: string }[];
  reports: WorkerReport[];
  live: Record<number, string>;
  switched: Record<number, string>;
  critic: string;
  answer: string;
  sources: { url: string; title?: string }[];
  phase: string;
  error: string;
  ts: number;
}

interface KnowledgeEntry {
  id: string;
  text: string;
  source: string;
  url?: string;
  ts: number;
}

function shortUrl(url: string) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url.slice(0, 40);
  }
}

function tpsLabel(w: SwarmWorkerInfo) {
  const measured = w.measured_tps > 0;
  return (
    <span title={measured ? "Measured on this machine" : "Estimated from model size"}>
      <Gauge size={11} className="inline" /> {w.speed_tps.toFixed(0)} tok/s{!measured ? "*" : ""}
    </span>
  );
}

export default function Swarm() {
  const toast = useApp((s) => s.toast);
  const [status, setStatus] = useState<SwarmStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [turns, setTurns] = useState<SwarmTurn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showKnowledge, setShowKnowledge] = useState(false);
  const [knowledge, setKnowledge] = useState<KnowledgeEntry[]>([]);
  const [learnUrl, setLearnUrl] = useState("");
  const [learning, setLearning] = useState(false);
  const [openReports, setOpenReports] = useState<Record<string, boolean>>({});
  const handleRef = useRef<{ cancel: () => void } | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const refresh = useCallback(async () => {
    try {
      const st = await swarmApi.status();
      setStatus(st);
    } catch (e: any) {
      setStatus((s) => s || null);
      toast(e?.message || "Swarm engine not reachable", "error");
    } finally {
      setLoading(false);
    }
  }, [toast]);

  const loadKnowledge = useCallback(async () => {
    try {
      const r = await swarmApi.knowledge();
      setKnowledge(r.entries || []);
    } catch { /* engine offline */ }
  }, []);

  useEffect(() => {
    refresh();
    loadKnowledge();
  }, [refresh, loadKnowledge]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [turns.length, turns[turns.length - 1]?.answer.length]);

  const patchTurn = (id: string, fn: (t: SwarmTurn) => void) =>
    setTurns((list) => {
      const idx = list.findIndex((t) => t.id === id);
      if (idx < 0) return list;
      const copy = { ...list[idx] };
      fn(copy);
      const next = list.slice();
      next[idx] = copy;
      return next;
    });

  const send = async () => {
    const text = input.trim();
    if (!text || busy) return;
    setBusy(true);
    setInput("");
    const turnId = `${Date.now()}`;
    const history = turns.slice(-4).flatMap((t) => [
      { role: "user", content: t.question },
      { role: "assistant", content: t.answer || t.error || "" },
    ]).filter((m) => m.content);

    setTurns((list) => [
      ...list,
      { id: turnId, question: text, plan: [], reports: [], live: {}, switched: {}, critic: "", answer: "", sources: [], phase: "plan", error: "", ts: Date.now() },
    ]);

    handleRef.current = swarmApi.chat(text, history, {
      onPhase: (phase, _manager, critic) => patchTurn(turnId, (t) => { t.phase = phase; if (critic) t.critic = critic; }),
      onPlan: (steps) => patchTurn(turnId, (t) => { t.plan = steps; }),
      onStepDelta: (index, delta) => patchTurn(turnId, (t) => { t.live = { ...t.live, [index]: (t.live[index] || "") + delta }; }),
      onStepRetry: (index, fallbackWorker) => patchTurn(turnId, (t) => { t.switched = { ...t.switched, [index]: fallbackWorker }; }),
      onStepDone: (step: SwarmStepDone) => patchTurn(turnId, (t) => {
        t.reports = [...t.reports.map((r) => (r.index === step.index ? { ...r, ...step } : r))];
        if (!t.reports.some((r) => r.index === step.index)) t.reports = [...t.reports, { ...step }];
      }),
      onSynthDelta: (delta) => patchTurn(turnId, (t) => { t.answer += delta; }),
      onFinal: (payload) => patchTurn(turnId, (t) => {
        t.answer = payload.text || t.answer;
        t.sources = payload.sources || [];
        t.phase = "done";
      }),
      onError: (message) => patchTurn(turnId, (t) => { t.error = message; t.phase = "error"; }),
      onDone: () => {
        setBusy(false);
        refresh();
        loadKnowledge();
      },
    });
  };

  const stop = () => {
    handleRef.current?.cancel();
    setBusy(false);
  };

  const updateSetting = async (update: Record<string, unknown>) => {
    try {
      const s = await swarmApi.settings(update as any);
      setStatus((prev) => (prev ? { ...prev, settings: s } : prev));
      toast("Swarm settings saved", "success");
    } catch (e: any) {
      toast(e?.message || "Only an admin can change swarm settings", "error");
    }
  };

  const toggleWorker = async (specId: string, enabled: boolean) => {
    const disabled = (status?.settings.disabled_workers || []).filter((s) => s !== specId);
    if (!enabled) disabled.push(specId);
    await updateSetting({ disabled_workers: disabled });
    refresh();
  };

  const learn = async () => {
    const url = learnUrl.trim();
    if (!url || learning) return;
    setLearning(true);
    try {
      const r = await swarmApi.learnUrl(url);
      toast(`Learned ${r.learned} fact${r.learned === 1 ? "" : "s"} from ${shortUrl(url)}`, r.learned ? "success" : "info");
      setLearnUrl("");
      loadKnowledge();
    } catch (e: any) {
      toast(e?.message || "Could not learn from that page", "error");
    } finally {
      setLearning(false);
    }
  };

  const workers = status?.workers || [];
  const manager = status?.manager;
  const settings = status?.settings;

  return (
    <section className="panel">
      <PanelHead icon={<Network size={16} />} title="Swarm">
        {status?.group?.profile && (
          <span className="pill" title="Resident group profile">
            {status.group.count || workers.length} models · {status.group.profile}
          </span>
        )}
        {settings && (
          <button
            className="pill"
            data-on={settings.internet}
            title={settings.internet ? "Full internet access is ON" : "Internet access is OFF"}
            onClick={() => updateSetting({ internet: !settings.internet })}
          >
            <Globe size={12} className="inline" /> Internet {settings.internet ? "on" : "off"}
          </button>
        )}
        <button className="btn h-8" onClick={() => setShowSettings((v) => !v)} title="Swarm settings">
          <Settings2 size={14} />
        </button>
        <button className="btn h-8" onClick={() => { setLoading(true); refresh(); loadKnowledge(); }} disabled={loading} title="Refresh">
          <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
        </button>
      </PanelHead>

      <AnimatePresence>
        {showSettings && settings && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="overflow-hidden border-b px-5 py-3"
            style={{ borderColor: "var(--border)" }}
          >
            <div className="flex flex-wrap items-end gap-4">
              <button className="pill" data-on={settings.parallel} onClick={() => updateSetting({ parallel: !settings.parallel })}>
                Parallel workers {settings.parallel ? "on" : "off"}
              </button>
              <button className="pill" data-on={settings.auto_learn} onClick={() => updateSetting({ auto_learn: !settings.auto_learn })}>
                Auto-learn facts {settings.auto_learn ? "on" : "off"}
              </button>
              <button
                className="pill"
                data-on={settings.verify}
                title="A critic model attacks every draft — factual errors fixed, invented details removed — before you see it"
                onClick={() => updateSetting({ verify: !settings.verify })}
              >
                <ShieldCheck size={12} className="inline" /> Verify answers {settings.verify ? "on" : "off"}
              </button>
              <button
                className="pill"
                data-on={settings.cloud_workers}
                title="Let remote endpoints you configured in Settings › Models (OpenAI, Anthropic, OpenRouter…) join as specialist workers. Local models always manage; off = fully offline."
                onClick={() => updateSetting({ cloud_workers: !settings.cloud_workers })}
              >
                <Cloud size={12} className="inline" /> Cloud assist {settings.cloud_workers ? "on" : "off"}
              </button>
              <label className="flex items-center gap-2 text-[12px]" style={{ color: "var(--muted)" }}>
                Plan steps
                <input
                  className="input h-8 w-16"
                  type="number"
                  min={1}
                  max={6}
                  value={settings.max_steps}
                  onChange={(e) => updateSetting({ max_steps: Number(e.target.value) || 4 })}
                />
              </label>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <div className="flex min-h-0 flex-1">
        {/* ── Team column ── */}
        <aside className="hidden w-[290px] shrink-0 flex-col gap-2.5 overflow-y-auto border-r p-4 lg:flex" style={{ borderColor: "var(--border)" }}>
          {loading && <div className="shimmer h-24 rounded-2xl" style={{ background: "var(--bg-sunken)" }} />}
          {!loading && !workers.length && (
            <Empty icon={<Network size={26} />} title="No resident models" hint="Start the local model group, then the swarm forms itself around the strongest model." />
          )}
          {workers.map((w) => (
            <WorkerCard key={w.spec_id} worker={w} managerCard={w.is_manager} onToggle={(on) => toggleWorker(w.spec_id, on)} />
          ))}

          {manager && (
            <div className="card swarm-manager p-3" style={{ borderColor: "var(--accent)", background: "color-mix(in oklab, var(--accent) 8%, transparent)" }}>
              <div className="flex items-center gap-2 text-[12px] font-semibold">
                <Crown size={13} style={{ color: "var(--accent)" }} /> Manager picks the specialists
              </div>
              <p className="mt-1 text-[11.5px] leading-snug" style={{ color: "var(--muted)" }}>
                Every request is split into steps; each step goes to the model that is best at it —
                coding, reasoning, quick lookups or live web — then the manager writes one answer.
              </p>
            </div>
          )}

          {/* Learn from the internet */}
          <div className="card p-3">
            <div className="mb-2 flex items-center gap-2 text-[12px] font-semibold">
              <Link2 size={13} style={{ color: "var(--accent)" }} /> Learn from a page
            </div>
            <div className="flex gap-1.5">
              <input
                className="input h-8 flex-1 text-[12px]"
                placeholder="https://…"
                value={learnUrl}
                onChange={(e) => setLearnUrl(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && learn()}
              />
              <button className="btn btn-primary h-8 px-2.5" disabled={!learnUrl.trim() || learning} onClick={learn}>
                {learning ? <Loader2 size={13} className="animate-spin" /> : <Plus size={13} />}
              </button>
            </div>
            <button className="mt-2 flex w-full items-center justify-between text-[12px]" style={{ color: "var(--muted)" }} onClick={() => { setShowKnowledge((v) => !v); loadKnowledge(); }}>
              <span className="flex items-center gap-1.5"><Brain size={12} /> Swarm memory</span>
              <span className="flex items-center gap-1">
                {status?.knowledge_count ?? 0}
                <ChevronDown size={12} className={showKnowledge ? "rotate-180 transition-transform" : "transition-transform"} />
              </span>
            </button>
            <AnimatePresence>
              {showKnowledge && (
                <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} className="overflow-hidden">
                  <div className="mt-2 flex max-h-56 flex-col gap-1.5 overflow-y-auto">
                    {knowledge.length === 0 && (
                      <p className="text-[11px]" style={{ color: "var(--muted)" }}>Nothing yet — facts from swarm turns and pages land here.</p>
                    )}
                    {knowledge.map((k) => (
                      <div key={k.id} className="group flex items-start gap-1.5 rounded-lg p-1.5 text-[11px]" style={{ background: "var(--bg-sunken)" }}>
                        <span className="min-w-0 flex-1 leading-snug">
                          {k.text}
                          {k.url && <span className="block truncate text-[10px]" style={{ color: "var(--muted)" }}>{shortUrl(k.url)}</span>}
                        </span>
                        <button
                          className="icon-btn h-5 w-5 shrink-0 opacity-0 transition-opacity group-hover:opacity-100"
                          aria-label="Forget"
                          onClick={async () => { try { await swarmApi.removeKnowledge(k.id); loadKnowledge(); } catch { /* */ } }}
                        >
                          <Trash2 size={11} />
                        </button>
                      </div>
                    ))}
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        </aside>

        {/* ── Conversation ── */}
        <div className="flex min-w-0 flex-1 flex-col">
          <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto p-5">
            {turns.length === 0 && (
              <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
                <motion.div animate={{ rotate: [0, 8, -8, 0] }} transition={{ repeat: Infinity, duration: 7, ease: "easeInOut" }}>
                  <Network size={40} style={{ color: "var(--accent)" }} />
                </motion.div>
                <h2 className="text-[17px] font-semibold">One request, every model you have</h2>
                <p className="max-w-md text-[13px] leading-relaxed" style={{ color: "var(--muted)" }}>
                  The strongest local model manages; the others work as specialists.
                  {status?.group?.count ? ` ${status.group.count} models are resident right now.` : ""}{" "}
                  Internet research is {settings?.internet ? "on" : "off"} — the manager cites what the scout finds.
                </p>
                <div className="mt-1 flex flex-wrap justify-center gap-1.5">
                  {["Refactor this function and explain the trade-offs", "What happened in AI today? Summarise with sources", "Plan my week and draft the emails"].map((example) => (
                    <button key={example} className="pill" onClick={() => setInput(example)}>{example}</button>
                  ))}
                </div>
              </div>
            )}

            {turns.map((t) => (
              <div key={t.id} className="mb-6 flex flex-col gap-2.5">
                <div className="self-end rounded-2xl rounded-br-md px-4 py-2.5 text-[13.5px]" style={{ background: "var(--accent-soft)", color: "var(--text)", maxWidth: "78%" }}>
                  {t.question}
                </div>

                {t.phase === "plan" && !t.plan.length && (
                  <div className="flex items-center gap-2 text-[12px]" style={{ color: "var(--muted)" }}>
                    <Loader2 size={13} className="animate-spin" /> {manager?.label || "Manager"} is splitting the request…
                  </div>
                )}

                {!!t.plan.length && (
                  <div className="flex flex-wrap gap-1.5">
                    {t.plan.map((step, i) => (
                      <span key={i} className="pill gap-1.5" title={step.task} style={{ borderColor: KIND_COLOR[step.kind] || "var(--border)", color: "var(--text)" }}>
                        {KIND_ICON[step.kind]} {step.kind_label} → <b>{step.worker}</b>
                      </span>
                    ))}
                  </div>
                )}

                {t.phase === "verify" && (
                  <div className="flex items-center gap-2 text-[12px]" style={{ color: "var(--muted)" }}>
                    <ShieldCheck size={13} style={{ color: "var(--accent)" }} />
                    {t.critic ? `${t.critic} (critic)` : "Critic"} double-checks the draft — facts, missing pieces, structure…
                  </div>
                )}

                {/* One lane per planned step — pending lanes spin, finished ones become reports */}
                {!!t.plan.length && (
                  <div className="grid grid-cols-1 gap-2 xl:grid-cols-2">
                    {t.plan.map((step, i) => {
                      const done = t.reports.find((r) => r.index === i);
                      if (done) {
                        return <ReportCard key={`${t.id}-lane-${i}`} report={done} open={!!openReports[`${t.id}-${i}`]} onToggle={() => setOpenReports((o) => ({ ...o, [`${t.id}-${i}`]: !o[`${t.id}-${i}`] }))} />;
                      }
                      const liveTail = (t.live[i] || "").trim();
                      return (
                        <div key={`${t.id}-lane-${i}`} className="card swarm-step-running flex flex-col gap-1.5 p-3">
                          <div className="flex items-center gap-2">
                            <Loader2 size={13} className="animate-spin" style={{ color: KIND_COLOR[step.kind] || "var(--accent)" }} />
                            <span className="flex items-center gap-1.5 text-[12px] font-semibold" style={{ color: KIND_COLOR[step.kind] }}>
                              {KIND_ICON[step.kind]} {step.worker}
                            </span>
                            <span className="min-w-0 flex-1 truncate text-[11.5px]" style={{ color: "var(--muted)" }}>{step.task}</span>
                            <span className="shrink-0 text-[10.5px]" style={{ color: "var(--muted)" }}>
                              {t.switched[i] ? `switched to ${t.switched[i]}` : "working…"}
                            </span>
                          </div>
                          {liveTail && (
                            <p className="selectable max-h-20 overflow-hidden text-[11.5px] leading-snug" style={{ color: "var(--muted)" }}>
                              {liveTail.length > 220 ? `…${liveTail.slice(-220)}` : liveTail}
                            </p>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}

                {(t.answer || t.phase === "synthesize") && (
                  <div className="card selectable max-w-none p-4">
                    <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide" style={{ color: "var(--accent)" }}>
                      <Crown size={12} /> {manager?.label || "Manager"} · final answer
                    </div>
                    <div className={`md text-[14px] leading-relaxed ${t.phase === "synthesize" && !t.error ? "caret" : ""}`}>
                      <ReactMarkdown
                        remarkPlugins={[remarkGfm]}
                        components={{ a: ({ href, children }) => <a href={href} onClick={(e) => { e.preventDefault(); if (href) openExternal(href); }}>{children}</a> }}
                      >
                        {t.answer}
                      </ReactMarkdown>
                    </div>
                    {!!t.sources.length && (
                      <div className="mt-3 flex flex-wrap gap-1.5 border-t pt-2.5" style={{ borderColor: "var(--border)" }}>
                        {t.sources.map((s, i) => (
                          <a key={s.url + i} className="pill gap-1" href={s.url} onClick={(e) => { e.preventDefault(); openExternal(s.url); }} title={s.url}>
                            <Globe size={11} /> {i + 1}. {s.title || shortUrl(s.url)}
                          </a>
                        ))}
                      </div>
                    )}
                  </div>
                )}

                {t.error && (
                  <div className="flex items-center gap-2 rounded-xl px-3 py-2 text-[12.5px]" style={{ background: "color-mix(in oklab, #f87171 12%, transparent)", color: "#f87171" }}>
                    <AlertCircle size={14} /> {t.error}
                  </div>
                )}
              </div>
            ))}
          </div>

          {/* Composer */}
          <div className="border-t p-4" style={{ borderColor: "var(--border)" }}>
            <div className="flex items-end gap-2">
              <textarea
                className="input max-h-40 min-h-[46px] flex-1 resize-none py-3"
                placeholder={status?.available ? "Ask the swarm — the manager will split it across specialists…" : "No resident models — start the local model group first"}
                value={input}
                rows={1}
                disabled={!status?.available}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    send();
                  }
                }}
              />
              {busy ? (
                <button className="btn btn-primary h-[46px] px-4" onClick={stop}>Stop</button>
              ) : (
                <button className="btn btn-primary h-[46px] px-4" disabled={!input.trim() || !status?.available} onClick={send} aria-label="Send to the swarm">
                  <Send size={15} />
                </button>
              )}
            </div>
            <p className="mt-1.5 flex items-center gap-1.5 text-[11px]" style={{ color: "var(--muted)" }}>
              <User size={11} /> Enter to send · Shift+Enter for a new line · every specialist runs in parallel and the manager answers once
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}

function WorkerCard({ worker: w, managerCard, onToggle }: { worker: SwarmWorkerInfo; managerCard?: boolean; onToggle: (on: boolean) => void }) {
  const roleColor = w.is_manager ? "var(--accent)" : w.remote ? "#61afef" : w.tier === "coding" ? KIND_COLOR.coding : w.thinking ? KIND_COLOR.reasoning : "var(--muted)";
  return (
    <motion.div layout className={`card p-3${managerCard && w.enabled ? " swarm-manager" : ""}`} style={{ opacity: w.enabled ? 1 : 0.45, borderColor: w.is_manager ? "var(--accent)" : "var(--border)" }}>
      <div className="flex items-center gap-2">
        {w.is_manager ? <Crown size={13} style={{ color: "var(--accent)" }} /> : <Network size={13} style={{ color: roleColor }} />}
        <span className="min-w-0 flex-1 truncate text-[12.5px] font-semibold">{w.label}</span>
        <button
          className="flex h-4 w-7 shrink-0 items-center rounded-full p-0.5 transition-colors"
          style={{ background: w.enabled ? "var(--accent)" : "var(--border)" }}
          onClick={() => onToggle(!w.enabled)}
          role="switch"
          aria-checked={w.enabled}
          title={w.enabled ? "Remove from the swarm" : "Bring back into the swarm"}
        >
          <motion.span layout className="h-3 w-3 rounded-full bg-white" style={{ marginLeft: w.enabled ? 12 : 0 }} />
        </button>
      </div>
      <div className="mt-1.5 flex flex-wrap gap-1 text-[10.5px]" style={{ color: "var(--muted)" }}>
        <span className="rounded-full px-1.5 py-0.5 font-semibold uppercase" style={{ color: roleColor, background: "var(--bg-sunken)" }}>{w.role}</span>
        <span className="rounded-full px-1.5 py-0.5" style={{ background: "var(--bg-sunken)" }}>{w.params_b ? `${w.params_b}B` : "?"}{w.moe ? ` · A${w.active_params_b}B` : ""}</span>
        {w.quant && <span className="rounded-full px-1.5 py-0.5" style={{ background: "var(--bg-sunken)" }}>{w.quant}</span>}
        <span className="rounded-full px-1.5 py-0.5" style={{ background: "var(--bg-sunken)" }}>{tpsLabel(w)}</span>
        {w.vision && <span className="rounded-full px-1.5 py-0.5" style={{ background: "var(--bg-sunken)" }}><Eye size={10} className="inline" /> vision</span>}
      </div>
    </motion.div>
  );
}

function ReportCard({ report: r, open, onToggle }: { report: WorkerReport; open: boolean; onToggle: () => void }) {
  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      className="card p-3"
      style={{ borderColor: r.ok ? "color-mix(in oklab, " + (KIND_COLOR[r.kind] || "#89ddff") + " 45%, var(--border))" : "#f87171" }}
    >
      <button className="flex w-full items-center gap-2 text-left" onClick={onToggle}>
        {r.ok ? (
          <Check size={13} style={{ color: "#34d399" }} />
        ) : (
          <AlertCircle size={13} style={{ color: "#f87171" }} />
        )}
        <span className="flex items-center gap-1.5 text-[12px] font-semibold" style={{ color: KIND_COLOR[r.kind] }}>
          {KIND_ICON[r.kind]} {r.worker}
        </span>
        <span className="min-w-0 flex-1 truncate text-[11.5px]" style={{ color: "var(--muted)" }}>{r.task}</span>
        <span className="shrink-0 text-[10.5px]" style={{ color: "var(--muted)" }}>
          {r.ok ? `${r.elapsed_s}s${r.tps ? ` · ${r.tps} tok/s` : ""}` : "failed"}
        </span>
        <ChevronDown size={12} className={open ? "rotate-180 transition-transform" : "transition-transform"} style={{ color: "var(--muted)" }} />
      </button>
      <AnimatePresence>
        {open && (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} className="overflow-hidden">
            <p className="mt-2 whitespace-pre-wrap text-[12px] leading-relaxed" style={{ color: "var(--text)" }}>
              {r.ok ? r.text : r.error}
            </p>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}
