import { useCallback, useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "motion/react";
import {
  Mic,
  MicOff,
  Square,
  Settings as SettingsIcon,
  MonitorPlay,
  ShieldAlert,
  Trash2,
  Loader2,
  Keyboard,
  Cpu,
  Volume2,
  Radio,
} from "lucide-react";
import { useApp } from "../store/app";
import { jarvis, type JarvisStatus, type JarvisTurn } from "../lib/api";

/**
 * "Talk to psd.ai" — the Jarvis screen.
 *
 * Speak in any language; psd.ai answers in English, aloud, and does the work
 * on the PC when you ask it to. Three ways to understand you, in order of
 * preference:
 *   1. local Whisper on the engine (``/api/jarvis/turn`` with the audio clip)
 *   2. the browser's Web Speech API, if the WebView ships one
 *   3. the text box, typed
 * and two ways to speak: the engine's TTS, or the browser's speechSynthesis.
 */

type Phase = "idle" | "listening" | "working" | "speaking" | "error";

interface Entry {
  id: string;
  kind: "user" | "jarvis" | "action" | "error";
  text: string;
  at: number;
}

const PHASE_LABEL: Record<Phase, string> = {
  idle: "Tap the mic and talk to me",
  listening: "Listening…",
  working: "Thinking…",
  speaking: "Speaking…",
  error: "Something went wrong",
};

const SILENCE_MS = 1500;      // stop after this much quiet
const MIN_SPEECH_MS = 700;    // ...but only once we actually heard something
const MAX_RECORD_MS = 45_000; // hard cap on one clip

let seq = 0;
const nextId = () => `${Date.now().toString(36)}-${++seq}`;

// ---------------------------------------------------------------------------
// speech plumbing (declared once; WebView2 may or may not ship these)
// ---------------------------------------------------------------------------

type SpeechRecognitionLike = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  maxAlternatives: number;
  start: () => void;
  stop: () => void;
  abort: () => void;
  onresult: ((e: any) => void) | null;
  onerror: ((e: any) => void) | null;
  onend: (() => void) | null;
};

function webSpeechCtor(): (new () => SpeechRecognitionLike) | null {
  if (typeof window === "undefined") return null;
  const w = window as any;
  return w.SpeechRecognition || w.webkitSpeechRecognition || w.mozSpeechRecognition || null;
}

function pickEnglishVoice(): SpeechSynthesisVoice | null {
  if (typeof window === "undefined" || !window.speechSynthesis) return null;
  const voices = window.speechSynthesis.getVoices?.() || [];
  if (!voices.length) return null;
  const en = voices.filter((v) => (v.lang || "").toLowerCase().startsWith("en"));
  if (!en.length) return null;
  // A British voice is the classic assistant timbre; fall back to any English.
  return (
    en.find((v) => v.lang.toLowerCase() === "en-gb") ||
    en.find((v) => v.lang.toLowerCase().startsWith("en-gb")) ||
    en.find((v) => v.lang.toLowerCase() === "en-us") ||
    en[0]
  );
}

function audioMime(b64: string): string {
  try {
    const head = atob(b64.slice(0, 8));
    if (head.startsWith("ID3")) return "audio/mpeg";
    if (head.charCodeAt(0) === 0xff) return "audio/mpeg";
    if (head.startsWith("RIFF")) return "audio/wav";
    if (head.startsWith("OggS")) return "audio/ogg";
  } catch {
    /* fall through */
  }
  return "audio/mpeg";
}

// ---------------------------------------------------------------------------

export default function Talk() {
  const toast = useApp((s) => s.toast);
  const setSettings = useApp((s) => s.setSettings);
  const sessionId = useApp((s) => s.activeSessionId);

  const [status, setStatus] = useState<JarvisStatus | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [log, setLog] = useState<Entry[]>([]);
  const [continuous, setContinuous] = useState(() => {
    try {
      return localStorage.getItem("psd.talk.continuous") === "1";
    } catch {
      return false;
    }
  });
  const [allowActions, setAllowActions] = useState(() => {
    try {
      return localStorage.getItem("psd.talk.actions") !== "0";
    } catch {
      return true;
    }
  });
  const [draft, setDraft] = useState("");
  const [level, setLevel] = useState(0);

  const mediaRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunks = useRef<Blob[]>([]);
  const analysis = useRef<{ ctx: AudioContext; raf: number } | null>(null);
  const silenceSince = useRef<number | null>(null);
  const firstSoundAt = useRef<number | null>(null);
  const startedAt = useRef(0);
  const audioEl = useRef<HTMLAudioElement | null>(null);
  const recogRef = useRef<SpeechRecognitionLike | null>(null);
  const stopRequested = useRef(false);
  const busy = phase === "working" || phase === "listening";

  const push = useCallback((kind: Entry["kind"], text: string) => {
    setLog((prev) => [...prev.slice(-60), { id: nextId(), kind, text, at: Date.now() }]);
  }, []);

  // ------------------------------------------------------------------ status

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await jarvis.status());
    } catch (e: any) {
      setStatus(null);
    }
  }, []);

  useEffect(() => {
    void loadStatus();
    // Voices arrive asynchronously in Chromium.
    window.speechSynthesis?.getVoices?.();
    const t = window.setTimeout(() => window.speechSynthesis?.getVoices?.(), 800);
    return () => window.clearTimeout(t);
  }, [loadStatus]);

  useEffect(() => {
    try {
      localStorage.setItem("psd.talk.continuous", continuous ? "1" : "0");
      localStorage.setItem("psd.talk.actions", allowActions ? "1" : "0");
    } catch {
      /* ignore */
    }
  }, [continuous, allowActions]);

  // ----------------------------------------------------------------- speaking

  const stopSpeaking = useCallback(() => {
    try {
      window.speechSynthesis?.cancel();
    } catch {
      /* ignore */
    }
    if (audioEl.current) {
      audioEl.current.pause();
      audioEl.current.src = "";
      audioEl.current = null;
    }
  }, []);

  const speak = useCallback(
    (text: string, audioB64: string | null): Promise<void> =>
      new Promise((resolve) => {
        if (!text) return resolve();
        setPhase("speaking");
        if (audioB64) {
          try {
            const el = new Audio(`data:${audioMime(audioB64)};base64,${audioB64}`);
            audioEl.current = el;
            el.onended = () => {
              audioEl.current = null;
              resolve();
            };
            el.onerror = () => {
              audioEl.current = null;
              // Fall back to the browser voice rather than staying silent.
              void speakBrowser(text).then(resolve);
            };
            void el.play().catch(() => void speakBrowser(text).then(resolve));
            return;
          } catch {
            /* fall through to the browser voice */
          }
        }
        void speakBrowser(text).then(resolve);
      }),
    [],
  );

  const speakBrowser = useCallback((text: string): Promise<void> => {
    return new Promise((resolve) => {
      try {
        if (!window.speechSynthesis) return resolve();
        const u = new SpeechSynthesisUtterance(text);
        const voice = pickEnglishVoice();
        if (voice) {
          u.voice = voice;
          u.lang = voice.lang;
        } else {
          u.lang = "en-US";
        }
        u.rate = 1.03;
        u.pitch = 0.95;
        u.onend = () => resolve();
        u.onerror = () => resolve();
        window.speechSynthesis.speak(u);
      } catch {
        resolve();
      }
    });
  }, []);

  // ------------------------------------------------------------------- turns

  const runTurn = useCallback(
    async (payload: { audio?: Blob | null; text?: string }) => {
      stopRequested.current = false;
      setPhase("working");
      try {
        const turn: JarvisTurn = await jarvis.turn({
          audio: payload.audio || null,
          text: payload.text,
          sessionId,
          allowActions,
          speak: true,
        });
        if (turn.transcript) push("user", turn.transcript);
        for (const a of turn.actions || []) {
          const label = a.ok
            ? `${a.action} → done`
            : `${a.action} → ${a.needs_confirmation ? a.confirm_hint || "needs approval" : a.error || "failed"}`;
          push(a.ok ? "action" : "error", label);
        }
        if (turn.reply) {
          push("jarvis", turn.reply);
          await speak(turn.reply, turn.audio || null);
        }
      } catch (e: any) {
        push("error", e?.message || "Voice turn failed");
        setPhase("error");
        toast(e?.message || "Voice turn failed", "error");
        return;
      }
      if (stopRequested.current) return;
      setPhase("idle");
      if (continuous && !stopRequested.current) {
        // Small gap so the mic does not pick up its own playback.
        window.setTimeout(() => {
          if (!stopRequested.current) void startListening();
        }, 600);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [allowActions, continuous, push, sessionId, speak, toast],
  );

  // ------------------------------------------------------------- microphone

  const teardownAnalysis = useCallback(() => {
    if (analysis.current) {
      cancelAnimationFrame(analysis.current.raf);
      void analysis.current.ctx.close().catch(() => {});
      analysis.current = null;
    }
  }, []);

  const finishRecording = useCallback(() => {
    const rec = mediaRef.current;
    if (rec && rec.state !== "inactive") {
      try {
        rec.stop();
      } catch {
        /* already stopped */
      }
    }
    teardownAnalysis();
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
  }, [teardownAnalysis]);

  const startMicRecording = useCallback(async () => {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true },
    });
    streamRef.current = stream;
    chunks.current = [];
    silenceSince.current = null;
    firstSoundAt.current = null;
    startedAt.current = Date.now();

    const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"].find(
      (m) => typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported?.(m),
    );
    const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    mediaRef.current = rec;

    rec.ondataavailable = (e) => e.data.size && chunks.current.push(e.data);
    rec.onstop = () => {
      const blob = new Blob(chunks.current, { type: rec.mimeType || mime || "audio/webm" });
      chunks.current = [];
      setLevel(0);
      if (blob.size > 1000 && !stopRequested.current) {
        void runTurn({ audio: blob });
      } else if (!stopRequested.current) {
        setPhase("idle");
        toast("I did not hear anything", "info");
      }
    };

    // Silence detection: RMS over time, so a pause ends the turn.
    try {
      const ctx = new AudioContext();
      const src = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 1024;
      src.connect(analyser);
      const buf = new Uint8Array(analyser.fftSize);
      let raf = 0;
      const tick = () => {
        analyser.getByteTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) {
          const v = (buf[i] - 128) / 128;
          sum += v * v;
        }
        const rms = Math.sqrt(sum / buf.length);
        setLevel(Math.min(1, rms * 4));
        const now = Date.now();
        if (rms > 0.035) {
          firstSoundAt.current = firstSoundAt.current ?? now;
          silenceSince.current = null;
        } else if (firstSoundAt.current && silenceSince.current === null) {
          silenceSince.current = now;
        }
        if (
          silenceSince.current &&
          now - silenceSince.current > SILENCE_MS &&
          firstSoundAt.current &&
          now - firstSoundAt.current > MIN_SPEECH_MS
        ) {
          finishRecording();
          return;
        }
        if (now - startedAt.current > MAX_RECORD_MS) {
          finishRecording();
          return;
        }
        raf = requestAnimationFrame(tick);
        if (analysis.current) analysis.current.raf = raf;
      };
      raf = requestAnimationFrame(tick);
      analysis.current = { ctx, raf };
    } catch {
      /* no analyser — the stop button ends the turn */
    }

    rec.start(250);
    setPhase("listening");
  }, [finishRecording, runTurn, teardownAnalysis, toast]);

  const startWebSpeech = useCallback(() => {
    const Ctor = webSpeechCtor();
    if (!Ctor) return false;
    const rec = new Ctor();
    recogRef.current = rec;
    rec.lang = "en-US";
    rec.continuous = false;
    rec.interimResults = false;
    rec.maxAlternatives = 1;
    rec.onresult = (e: any) => {
      const text = String(e?.results?.[0]?.[0]?.transcript || "").trim();
      if (text) void runTurn({ text });
      else setPhase("idle");
    };
    rec.onerror = (e: any) => {
      const code = e?.error || "error";
      setPhase("error");
      push("error", `Microphone error: ${code}`);
      toast(`Microphone error: ${code}`, "error");
    };
    rec.onend = () => {
      if (phase === "listening") setPhase("idle");
    };
    try {
      rec.start();
      setPhase("listening");
      return true;
    } catch {
      return false;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase, push, runTurn, toast]);

  const startListening = useCallback(async () => {
    stopRequested.current = false;
    stopSpeaking();
    setPhase("listening");
    try {
      // Local Whisper on the engine is the reliable path; the Web Speech API
      // is the fallback when no server STT is configured.
      if (status?.stt?.available) {
        await startMicRecording();
        return;
      }
      if (startWebSpeech()) return;
      throw new Error(
        "No speech recognition available. Set up a local Whisper model in Settings → Voice & PC, or type your message below.",
      );
    } catch (e: any) {
      setPhase("error");
      push("error", e?.message || "Could not open the microphone");
      toast(e?.message || "Could not open the microphone", "error");
    }
  }, [push, startMicRecording, startWebSpeech, status, stopSpeaking, toast]);

  const stopListening = useCallback(() => {
    stopRequested.current = false;
    if (recogRef.current) {
      try {
        recogRef.current.stop();
      } catch {
        /* ignore */
      }
      recogRef.current = null;
    }
    if (mediaRef.current && mediaRef.current.state !== "inactive") finishRecording();
    else setPhase("idle");
  }, [finishRecording]);

  const abort = useCallback(() => {
    stopRequested.current = true;
    stopSpeaking();
    if (recogRef.current) {
      try {
        recogRef.current.abort();
      } catch {
        /* ignore */
      }
      recogRef.current = null;
    }
    if (mediaRef.current && mediaRef.current.state !== "inactive") {
      try {
        mediaRef.current.stop();
      } catch {
        /* ignore */
      }
    }
    teardownAnalysis();
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    setLevel(0);
    setPhase("idle");
  }, [stopSpeaking, teardownAnalysis]);

  useEffect(() => () => abort(), [abort]);

  // Space bar = push to talk (hold), Enter sends the typed draft.
  useEffect(() => {
    const isTyping = (el: EventTarget | null) => {
      const t = el as HTMLElement | null;
      return !!t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
    };
    const down = (e: KeyboardEvent) => {
      if (e.code !== "Space" || isTyping(e.target) || e.metaKey || e.ctrlKey) return;
      e.preventDefault();
      if (phase === "idle") void startListening();
    };
    const up = (e: KeyboardEvent) => {
      if (e.code !== "Space" || isTyping(e.target)) return;
      if (phase === "listening" && mediaRef.current) stopListening();
    };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
    };
  }, [phase, startListening, stopListening]);

  const sendDraft = () => {
    const text = draft.trim();
    if (!text || busy) return;
    setDraft("");
    void runTurn({ text });
  };

  const quickAction = async (action: string, params: Record<string, any> = {}) => {
    setPhase("working");
    try {
      const r = await jarvis.act(action, params);
      push(r.ok ? "action" : "error", r.ok ? `${action} → done` : `${action} → ${r.error || "failed"}`);
      if (!r.ok) toast(r.error || "Action failed", "error");
    } catch (e: any) {
      push("error", e?.message || "Action failed");
      toast(e?.message || "Action failed", "error");
    }
    setPhase("idle");
  };

  // ------------------------------------------------------------------ render

  const serverStt = !!status?.stt?.available;
  const computerOn = !!status?.computer?.enabled;

  return (
    <div className="relative flex h-full min-h-0 w-full flex-col">
      <div className="ambient" />
      <header
        className="relative z-10 flex items-center gap-3 px-5 py-3"
        style={{ borderBottom: "1px solid var(--border)" }}
      >
        <div>
          <h1 className="text-[15px] font-semibold">Talk to psd.ai</h1>
          <p className="text-[12px]" style={{ color: "var(--muted)" }}>
            Speak in any language — I answer in English, and I can drive this PC.
          </p>
        </div>
        <div className="ml-auto flex items-center gap-1.5">
          <span className="pill" title="Speech-to-text provider">
            <Radio size={12} /> {serverStt ? `STT: ${status?.stt?.provider || "local"}` : "STT: browser"}
          </span>
          <span className="pill" data-on={computerOn} title="Desktop control">
            <MonitorPlay size={12} /> PC {computerOn ? "on" : "off"}
          </span>
          <button className="icon-btn" title="Voice settings" onClick={() => setSettings(true, "voice")}>
            <SettingsIcon size={16} />
          </button>
        </div>
      </header>

      <div className="relative z-10 flex min-h-0 flex-1 flex-col items-center justify-center gap-6 px-6 py-6">
        <Orb phase={phase} level={level} onTap={() => (phase === "listening" ? stopListening() : void startListening())} />

        <div className="text-center">
          <motion.p
            key={phase}
            initial={{ opacity: 0, y: 4 }}
            animate={{ opacity: 1, y: 0 }}
            className="text-[15px] font-medium"
          >
            {PHASE_LABEL[phase]}
          </motion.p>
          <p className="mt-1 text-[12px]" style={{ color: "var(--muted)" }}>
            {phase === "idle"
              ? "Hold Space, or tap the orb. Silence ends the turn."
              : phase === "listening"
                ? "Tap again when you are done."
                : null}
          </p>
        </div>

        <div className="flex flex-wrap items-center justify-center gap-1.5">
          <button className="pill" data-on={continuous} onClick={() => setContinuous((v) => !v)}>
            <Cpu size={12} /> Continuous
          </button>
          <button className="pill" data-on={allowActions} onClick={() => setAllowActions((v) => !v)}>
            <ShieldAlert size={12} /> PC actions
          </button>
          <button className="pill" onClick={() => void quickAction("info")} disabled={busy}>
            System info
          </button>
          <button className="pill" onClick={() => void quickAction("windows")} disabled={busy}>
            Open windows
          </button>
        </div>
      </div>

      {/* transcript */}
      <div className="relative z-10 mx-5 mb-3 min-h-[92px] max-h-[220px] overflow-y-auto rounded-2xl p-3" style={{ background: "var(--bg-sunken)", border: "1px solid var(--border)" }}>
        {log.length === 0 ? (
          <p className="px-1 text-[12px]" style={{ color: "var(--muted)" }}>
            Nothing yet. Try: “open notepad”, “what’s my battery”, “turn the volume up”, or just ask me something.
          </p>
        ) : (
          <div className="flex flex-col gap-1.5">
            <AnimatePresence initial={false}>
              {log.slice(-20).map((e) => (
                <motion.div
                  key={e.id}
                  initial={{ opacity: 0, y: 6 }}
                  animate={{ opacity: 1, y: 0 }}
                  className="flex gap-2 text-[13px] leading-relaxed"
                >
                  <span
                    className="shrink-0 text-[11px] font-semibold uppercase tracking-wide"
                    style={{
                      width: 52,
                      color:
                        e.kind === "user"
                          ? "var(--text)"
                          : e.kind === "jarvis"
                            ? "var(--accent)"
                            : e.kind === "action"
                              ? "#34d399"
                              : "#f87171",
                    }}
                  >
                    {e.kind === "user" ? "You" : e.kind === "jarvis" ? "psd.ai" : e.kind === "action" ? "did" : "err"}
                  </span>
                  <span className="selectable min-w-0 flex-1" style={{ color: e.kind === "user" ? "var(--muted)" : "var(--text)" }}>
                    {e.text}
                  </span>
                </motion.div>
              ))}
            </AnimatePresence>
          </div>
        )}
      </div>

      {/* controls */}
      <div className="relative z-10 flex items-center gap-2 px-5 pb-5">
        <button
          className="btn btn-primary h-10 w-10 !px-0"
          title={phase === "listening" ? "Stop listening" : "Start listening (hold Space)"}
          onClick={() => (phase === "listening" ? stopListening() : void startListening())}
          disabled={phase === "working"}
        >
          {phase === "listening" ? <MicOff size={17} /> : <Mic size={17} />}
        </button>
        {busy && (
          <button className="btn h-10" onClick={abort} title="Stop everything">
            <Square size={14} /> Stop
          </button>
        )}
        <input
          className="min-w-0 flex-1 rounded-xl px-3 py-2 text-[13px] outline-none"
          style={{ background: "var(--bg-sunken)", border: "1px solid var(--border)" }}
          placeholder="Or type it here — I still answer out loud…"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              sendDraft();
            }
          }}
        />
        <button className="btn h-10" onClick={sendDraft} disabled={!draft.trim() || busy} title="Send">
          Send
        </button>
        <button
          className="icon-btn"
          title="Clear this conversation"
          onClick={() => {
            abort();
            setLog([]);
            void jarvis.reset(sessionId).catch(() => {});
          }}
        >
          <Trash2 size={16} />
        </button>
      </div>

      {status && !status.llm_configured && (
        <div
          className="mx-5 mb-4 flex items-start gap-2 rounded-xl px-3 py-2 text-[12px]"
          style={{ background: "var(--accent-soft)", color: "var(--accent)" }}
        >
          <ShieldAlert size={14} className="mt-0.5 shrink-0" />
          <span>
            No chat model is configured yet, so I can only run the built-in PC commands. Add a model in Settings →
            Models for real conversation.
          </span>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------

function Orb({ phase, level, onTap }: { phase: Phase; level: number; onTap: () => void }) {
  const active = phase === "listening";
  const working = phase === "working";
  const speaking = phase === "speaking";
  const size = 168;

  return (
    <motion.button
      onClick={onTap}
      aria-label={active ? "Stop listening" : "Start listening"}
      className="relative flex items-center justify-center rounded-full"
      style={{ width: size, height: size }}
      animate={{ scale: speaking ? 1.04 : 1 }}
      transition={{ duration: 0.6, repeat: speaking ? Infinity : 0, repeatType: "reverse" }}
    >
      {/* halo rings */}
      {(active || working) &&
        [0, 1, 2].map((i) => (
          <motion.span
            key={i}
            className="absolute inset-0 rounded-full"
            style={{ border: "1px solid var(--accent)" }}
            initial={{ opacity: 0.35, scale: 0.9 }}
            animate={{ opacity: 0, scale: 1.45 }}
            transition={{ duration: 2.1, repeat: Infinity, delay: i * 0.7, ease: "easeOut" }}
          />
        ))}

      <motion.span
        className="absolute rounded-full"
        style={{
          background: "radial-gradient(circle at 30% 30%, color-mix(in oklab, var(--accent) 85%, #fff), var(--accent))",
          boxShadow: "0 18px 60px -18px var(--glow)",
        }}
        animate={{
          width: size * (0.62 + level * 0.34),
          height: size * (0.62 + level * 0.34),
          opacity: active ? 1 : working ? 0.75 : 0.9,
        }}
        transition={{ type: "spring", stiffness: 220, damping: 22 }}
      />

      <span
        className="absolute rounded-full"
        style={{
          inset: 6,
          border: "1px solid color-mix(in oklab, var(--accent) 35%, transparent)",
        }}
      />

      <span className="relative z-10 text-white/95">
        {working ? (
          <Loader2 size={30} className="animate-spin" />
        ) : speaking ? (
          <Volume2 size={30} />
        ) : active ? (
          <Mic size={30} />
        ) : (
          <Mic size={30} />
        )}
      </span>
      {phase === "idle" && (
        <span
          className="absolute -bottom-7 left-1/2 -translate-x-1/2 whitespace-nowrap text-[11px]"
          style={{ color: "var(--muted)" }}
        >
          <Keyboard size={11} className="mr-1 inline" />
          Space to talk
        </span>
      )}
    </motion.button>
  );
}
