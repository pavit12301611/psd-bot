import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { motion, AnimatePresence } from "motion/react";
import { ArrowUp, Square, Paperclip, Globe, Terminal, MessageSquare, Bot, X, Mic, Database, Slash } from "lucide-react";
import { useApp } from "../store/app";
import { uploads, voice } from "../lib/api";
import { MAX_UPLOAD_BYTES, metaLabel } from "../lib/ui";

const SLASH: { token: string; label: string; run: (app: ReturnType<typeof useApp.getState>) => void }[] = [
  { token: "/models", label: "Open Models", run: (a) => a.setView("models") },
  { token: "/notes", label: "Open Notes", run: (a) => a.setView("notes") },
  { token: "/tasks", label: "Open Tasks", run: (a) => a.setView("tasks") },
  { token: "/calendar", label: "Open Calendar", run: (a) => a.setView("calendar") },
  { token: "/memory", label: "Open Brain", run: (a) => a.setView("memory") },
  { token: "/research", label: "Open Research", run: (a) => a.setView("research") },
  { token: "/gallery", label: "Open Gallery", run: (a) => a.setView("gallery") },
  { token: "/library", label: "Open Library", run: (a) => a.setView("library") },
  { token: "/email", label: "Open Email", run: (a) => a.setView("email") },
  { token: "/compare", label: "Open Compare", run: (a) => a.setView("compare") },
  { token: "/settings", label: "Open Settings", run: (a) => a.setSettings(true) },
  { token: "/new", label: "New chat", run: (a) => a.newChat() },
  { token: "/web", label: "Toggle web search", run: (a) => a.toggleWeb() },
  { token: "/agent", label: "Switch to agent mode", run: (a) => a.setMode("agent") },
  { token: "/chat", label: "Switch to chat mode", run: (a) => a.setMode("chat") },
];

export default function Composer() {
  const [text, setText] = useState("");
  const [files, setFiles] = useState<{ id: string; name: string }[]>([]);
  const [uploading, setUploading] = useState(false);
  const [recording, setRecording] = useState(false);
  const [slashOpen, setSlashOpen] = useState(false);
  const ta = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const recRef = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);
  const send = useApp((s) => s.send);
  const stop = useApp((s) => s.stop);
  const streaming = useApp((s) => s.streaming);
  const mode = useApp((s) => s.mode);
  const setMode = useApp((s) => s.setMode);
  const web = useApp((s) => s.web);
  const toggleWeb = useApp((s) => s.toggleWeb);
  const bash = useApp((s) => s.bash);
  const toggleBash = useApp((s) => s.toggleBash);
  const rag = useApp((s) => s.rag);
  const toggleRag = useApp((s) => s.toggleRag);
  const activeSessionId = useApp((s) => s.activeSessionId);
  const toast = useApp((s) => s.toast);

  useEffect(() => {
    const el = ta.current;
    if (!el) return;
    el.style.height = "0px";
    el.style.height = Math.min(el.scrollHeight, 220) + "px";
  }, [text]);

  useEffect(() => {
    ta.current?.focus();
  }, [activeSessionId]);

  useEffect(() => {
    const onAttach = (e: Event) => {
      const files = (e as CustomEvent<{ id: string; name: string }[]>).detail || [];
      if (files.length) setFiles((p) => [...p, ...files]);
    };
    const onFocus = () => ta.current?.focus();
    window.addEventListener("psd-attach", onAttach as EventListener);
    window.addEventListener("psd-focus-composer", onFocus);
    return () => {
      window.removeEventListener("psd-attach", onAttach as EventListener);
      window.removeEventListener("psd-focus-composer", onFocus);
    };
  }, []);

  const slashHits = text.startsWith("/")
    ? SLASH.filter((s) => s.token.startsWith(text.split(/\s/)[0].toLowerCase()))
    : [];

  useEffect(() => {
    setSlashOpen(slashHits.length > 0 && !text.includes(" "));
  }, [text, slashHits.length]);

  const submit = () => {
    if (streaming) return;
    const t = text.trim();
    if (t.startsWith("/") && !t.includes(" ")) {
      const hit = SLASH.find((s) => s.token === t.toLowerCase());
      if (hit) {
        hit.run(useApp.getState());
        setText("");
        return;
      }
    }
    if (!t && !files.length) return;
    send(t, files);
    setText("");
    setFiles([]);
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (slashOpen && (e.key === "Tab" || e.key === "Enter") && slashHits[0]) {
      e.preventDefault();
      slashHits[0].run(useApp.getState());
      setText("");
      setSlashOpen(false);
      return;
    }
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };

  const pick = async (list: FileList | null) => {
    if (!list?.length) return;
    setUploading(true);
    try {
      for (const f of Array.from(list)) {
        if (f.size > MAX_UPLOAD_BYTES) {
          toast(`${f.name} is larger than 25 MB`, "error");
          continue;
        }
        const r = await uploads.send(f, activeSessionId || undefined);
        for (const x of r.files || []) setFiles((p) => [...p, { id: x.id, name: x.name || x.filename || f.name }]);
      }
    } catch (e: any) {
      toast(e?.message || "Upload failed", "error");
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  const toggleMic = async () => {
    if (recording) {
      recRef.current?.stop();
      setRecording(false);
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const rec = new MediaRecorder(stream);
      chunks.current = [];
      rec.ondataavailable = (e) => e.data.size && chunks.current.push(e.data);
      rec.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(chunks.current, { type: rec.mimeType || "audio/webm" });
        const file = new File([blob], "voice.webm", { type: blob.type });
        try {
          const r = await voice.transcribe(file);
          const spoken = (r.text || r.transcript || "").trim();
          if (spoken) setText((t) => (t ? t + " " + spoken : spoken));
          else toast("Nothing transcribed", "info");
        } catch (e: any) {
          toast(e.message || "Voice input failed", "error");
        }
      };
      recRef.current = rec;
      rec.start();
      setRecording(true);
    } catch (e: any) {
      toast(e.message || "Microphone unavailable", "error");
    }
  };

  return (
    <div className="relative z-10 px-4 pb-4 pt-2">
      <motion.div layout className="glass mx-auto max-w-3xl rounded-[26px] p-2" style={{ boxShadow: "var(--shadow)" }} onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); pick(e.dataTransfer.files); }}>
        <AnimatePresence>
          {files.length > 0 && (
            <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }} className="flex flex-wrap gap-1.5 px-2 pt-1">
              {files.map((f) => (
                <span key={f.id} className="pill" data-on="true">
                  <Paperclip size={11} /> {f.name}
                  <button className="ml-0.5 opacity-70 hover:opacity-100" onClick={() => setFiles((p) => p.filter((x) => x.id !== f.id))}>
                    <X size={11} />
                  </button>
                </span>
              ))}
            </motion.div>
          )}
        </AnimatePresence>

        {slashOpen && (
          <div className="mb-1 overflow-hidden rounded-2xl" style={{ background: "var(--bg-sunken)", border: "1px solid var(--border)" }}>
            {slashHits.slice(0, 6).map((s) => (
              <button
                key={s.token}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[13px] hover:bg-[var(--accent-soft)]"
                onMouseDown={(e) => {
                  e.preventDefault();
                  s.run(useApp.getState());
                  setText("");
                  setSlashOpen(false);
                }}
              >
                <Slash size={12} style={{ color: "var(--muted)" }} />
                <span className="font-medium">{s.token}</span>
                <span style={{ color: "var(--muted)" }}>{s.label}</span>
              </button>
            ))}
          </div>
        )}

        <textarea
          ref={ta}
          rows={1}
          className="selectable block w-full resize-none bg-transparent px-3 py-2.5 text-[15px] leading-relaxed outline-none placeholder:text-[var(--muted)]"
          placeholder={mode === "agent" ? "Give the agent a task…  (/ for commands)" : "Message psd.ai…  (/ for commands)"}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
        />

        <div className="flex items-center gap-1 overflow-x-auto px-1 pb-0.5">
          <input ref={fileInput} type="file" multiple hidden onChange={(e) => pick(e.target.files)} />
          <button className="icon-btn" title="Attach files" onClick={() => fileInput.current?.click()} disabled={uploading}>
            <Paperclip size={16} className={uploading ? "animate-pulse" : ""} />
          </button>
          <button className="icon-btn" title={recording ? "Stop recording" : "Voice input"} onClick={toggleMic}>
            <Mic size={16} className={recording ? "animate-pulse" : ""} style={{ color: recording ? "var(--accent)" : undefined }} />
          </button>

          <div className="ml-1 flex rounded-full p-0.5" style={{ background: "var(--bg-sunken)", border: "1px solid var(--border)" }}>
            {(["chat", "agent"] as const).map((m) => (
              <button key={m} className="relative flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium transition-colors" style={{ color: mode === m ? "#fff" : "var(--muted)" }} onClick={() => setMode(m)}>
                {mode === m && <motion.span layoutId="chat-mode-pill" className="absolute inset-0 rounded-full" style={{ background: "linear-gradient(135deg, var(--accent), #c9505b)" }} transition={{ type: "spring", stiffness: 500, damping: 40 }} />}
                <span className="relative flex items-center gap-1.5">
                  {m === "chat" ? <MessageSquare size={12} /> : <Bot size={12} />}
                  {m === "chat" ? "Chat" : "Agent"}
                </span>
              </button>
            ))}
          </div>

          <button className="pill ml-1" data-on={web} onClick={toggleWeb} title="Search the web">
            <Globe size={12} /> Web
          </button>
          <button className="pill" data-on={bash} onClick={toggleBash} title="Allow shell commands (agent)">
            <Terminal size={12} /> Shell
          </button>
          <button className="pill" data-on={rag} onClick={toggleRag} title="Retrieve from your documents">
            <Database size={12} /> RAG
          </button>

          <div className="flex-1" />

          <AnimatePresence mode="wait" initial={false}>
            {streaming ? (
              <motion.button key="stop" initial={{ scale: 0.8, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} exit={{ scale: 0.8, opacity: 0 }} className="flex h-9 w-9 items-center justify-center rounded-full text-white" style={{ background: "var(--text)" }} title="Stop" onClick={stop}>
                <Square size={13} fill="currentColor" />
              </motion.button>
            ) : (
              <motion.button
                key="send"
                initial={{ scale: 0.8, opacity: 0 }}
                animate={{ scale: 1, opacity: 1 }}
                exit={{ scale: 0.8, opacity: 0 }}
                whileTap={{ scale: 0.92 }}
                disabled={!text.trim() && !files.length}
                className="flex h-9 w-9 items-center justify-center rounded-full text-white transition-all disabled:opacity-30"
                style={{ background: "linear-gradient(135deg, var(--accent), #c9505b)", boxShadow: "0 6px 18px -6px var(--glow)" }}
                title="Send (Enter)"
                onClick={submit}
              >
                <ArrowUp size={17} strokeWidth={2.5} />
              </motion.button>
            )}
          </AnimatePresence>
        </div>
      </motion.div>
      <p className="mt-2 text-center text-[11px]" style={{ color: "var(--muted)" }}>
        Enter to send · / for commands · Shift+Enter for a new line · {metaLabel}+L to focus · Runs on your device
      </p>
    </div>
  );
}
