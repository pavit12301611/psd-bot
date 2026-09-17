import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { motion, AnimatePresence } from "motion/react";
import { ArrowUp, Square, Paperclip, Globe, Terminal, MessageSquare, Bot, X } from "lucide-react";
import { useApp } from "../store/app";
import { uploads } from "../lib/api";

export default function Composer() {
  const [text, setText] = useState("");
  const [files, setFiles] = useState<{ id: string; name: string }[]>([]);
  const [uploading, setUploading] = useState(false);
  const ta = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const { send, stop, streaming, mode, setMode, web, toggleWeb, bash, toggleBash, activeSessionId, toast } = useApp();

  useEffect(() => {
    const el = ta.current;
    if (!el) return;
    el.style.height = "0px";
    el.style.height = Math.min(el.scrollHeight, 220) + "px";
  }, [text]);

  useEffect(() => {
    ta.current?.focus();
  }, [activeSessionId]);

  const submit = () => {
    if (streaming) return;
    const t = text.trim();
    if (!t && !files.length) return;
    send(t, files);
    setText("");
    setFiles([]);
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
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

        <textarea
          ref={ta}
          rows={1}
          className="selectable block w-full resize-none bg-transparent px-3 py-2.5 text-[15px] leading-relaxed outline-none placeholder:text-[var(--muted)]"
          placeholder={mode === "agent" ? "Give the agent a task…" : "Message psd.ai…"}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
        />

        <div className="flex items-center gap-1 px-1 pb-0.5">
          <input ref={fileInput} type="file" multiple hidden onChange={(e) => pick(e.target.files)} />
          <button className="icon-btn" title="Attach files" onClick={() => fileInput.current?.click()} disabled={uploading}>
            <Paperclip size={16} className={uploading ? "animate-pulse" : ""} />
          </button>

          <div className="ml-1 flex rounded-full p-0.5" style={{ background: "var(--bg-sunken)", border: "1px solid var(--border)" }}>
            {(["chat", "agent"] as const).map((m) => (
              <button key={m} className="relative flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium transition-colors" style={{ color: mode === m ? "#fff" : "var(--muted)" }} onClick={() => setMode(m)}>
                {mode === m && <motion.span layoutId="mode-pill" className="absolute inset-0 rounded-full" style={{ background: "linear-gradient(135deg, var(--accent), #c9505b)" }} transition={{ type: "spring", stiffness: 500, damping: 40 }} />}
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
        Enter to send · Shift+Enter for a new line · Runs entirely on your device
      </p>
    </div>
  );
}
