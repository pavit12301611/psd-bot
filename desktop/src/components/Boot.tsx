import { useEffect, useState } from "react";
import { motion, AnimatePresence } from "motion/react";
import { RefreshCw, TerminalSquare } from "lucide-react";
import { backendStatus, onBackendLog, onBackendStatus, restartBackend, type BackendStatus } from "../lib/ipc";
import { useApp } from "../store/app";

const STEPS = ["Starting local engine", "Loading models", "Opening workspace"];

export default function Boot() {
  const [status, setStatus] = useState<BackendStatus>("starting");
  const [log, setLog] = useState<string[]>([]);
  const [showLog, setShowLog] = useState(false);
  const [step, setStep] = useState(0);
  const boot = useApp((s) => s.boot);
  const bootError = useApp((s) => s.bootError);

  useEffect(() => {
    backendStatus().then((b) => {
      setStatus(b.status);
      setLog(b.log);
    });
    const u1 = onBackendStatus((s) => setStatus(s));
    const u2 = onBackendLog((l) => setLog((x) => [...x.slice(-300), l]));
    return () => {
      u1();
      u2();
    };
  }, []);

  useEffect(() => {
    if (status !== "ready") return;
    setStep(1);
    const t = setTimeout(() => {
      setStep(2);
      boot();
    }, 350);
    return () => clearTimeout(t);
  }, [status, boot]);

  const failed = status === "error" || !!bootError;

  return (
    <div className="relative z-10 flex h-full flex-col items-center justify-center gap-8 px-8">
      <motion.div initial={{ opacity: 0, scale: 0.9, y: 8 }} animate={{ opacity: 1, scale: 1, y: 0 }} transition={{ duration: 0.5, ease: "easeOut" }} className="flex flex-col items-center gap-5">
        <div className="relative">
          <motion.div
            className="absolute inset-0 rounded-3xl"
            style={{ boxShadow: "0 0 0 0 var(--glow)" }}
            animate={failed ? {} : { boxShadow: ["0 0 0 0px var(--glow)", "0 0 0 22px rgba(224,108,117,0)"] }}
            transition={{ duration: 1.8, repeat: Infinity, ease: "easeOut" }}
          />
          <img src="/icon.png" alt="psd.ai" className="relative h-24 w-24 rounded-3xl" draggable={false} />
        </div>
        <div className="text-center">
          <h1 className="text-2xl font-semibold tracking-tight">psd.ai</h1>
          <p className="mt-1 text-sm" style={{ color: "var(--muted)" }}>
            Your private AI, running on this device.
          </p>
        </div>
      </motion.div>

      <div className="w-full max-w-sm">
        <AnimatePresence mode="wait">
          {!failed ? (
            <motion.div key="steps" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="flex flex-col gap-2">
              {STEPS.map((s, i) => {
                const done = i < step;
                const active = i === step;
                return (
                  <motion.div key={s} initial={{ opacity: 0, x: -8 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: i * 0.08 }} className="flex items-center gap-3 rounded-xl px-3 py-2 glass">
                    <span
                      className="flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-bold transition-colors"
                      style={{ background: done ? "var(--accent)" : active ? "var(--accent-soft)" : "var(--bg-sunken)", color: done ? "#fff" : "var(--muted)" }}
                    >
                      {done ? "✓" : i + 1}
                    </span>
                    <span className="text-sm" style={{ color: done || active ? "var(--text)" : "var(--muted)" }}>
                      {s}
                    </span>
                    {active && (
                      <span className="dots ml-auto">
                        <span />
                        <span />
                        <span />
                      </span>
                    )}
                  </motion.div>
                );
              })}
              <p className="mt-2 text-center text-xs" style={{ color: "var(--muted)" }}>
                First launch can take a minute while the engine warms up.
              </p>
            </motion.div>
          ) : (
            <motion.div key="err" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} className="glass rounded-2xl p-5">
              <h2 className="font-semibold text-red-400">The local engine could not start</h2>
              <p className="mt-1 text-sm" style={{ color: "var(--muted)" }}>
                {bootError || "Python backend exited before it became ready. Check the log below, then try again."}
              </p>
              <div className="mt-4 flex gap-2">
                <button
                  className="btn btn-primary"
                  onClick={() => {
                    useApp.setState({ bootError: null });
                    setStep(0);
                    setStatus("starting");
                    restartBackend().then(() => backendStatus().then((b) => setStatus(b.status)));
                  }}
                >
                  <RefreshCw size={15} /> Retry
                </button>
                <button className="btn" onClick={() => setShowLog((v) => !v)}>
                  <TerminalSquare size={15} /> {showLog ? "Hide" : "Show"} log
                </button>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      <AnimatePresence>
        {(showLog || (!failed && log.length > 0 && step < 2)) && (
          <motion.pre
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: showLog ? 260 : 96 }}
            exit={{ opacity: 0, height: 0 }}
            className="selectable w-full max-w-xl overflow-auto rounded-xl p-3 text-[11px] leading-relaxed glass"
            style={{ color: "var(--muted)", fontFamily: "var(--font-mono)" }}
          >
            {log.slice(showLog ? -200 : -6).join("\n")}
          </motion.pre>
        )}
      </AnimatePresence>
    </div>
  );
}
