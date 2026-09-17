import { memo, useEffect, useState } from "react";
import { motion, AnimatePresence } from "motion/react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { Brain, ChevronDown, Copy, Check, Terminal, Globe, Paperclip, AlertTriangle, Clock, Volume2 } from "lucide-react";
import type { Message as Msg, ToolCall } from "../store/app";
import { useApp } from "../store/app";
import { inTauri, request } from "../lib/ipc";
import { voice } from "../lib/api";

/** Images served by the backend (/api/...) must be fetched over IPC inside Tauri. */
function ApiImage({ src, alt }: { src?: string; alt?: string }) {
  const [url, setUrl] = useState<string | undefined>(src && (!inTauri || !src.startsWith("/")) ? src : undefined);
  useEffect(() => {
    if (!src || !inTauri || !src.startsWith("/")) return;
    let alive = true;
    request({ method: "GET", path: src }).then((r) => {
      if (!alive || !r.base64) return;
      setUrl(`data:${r.headers["content-type"] || "image/png"};base64,${r.base64}`);
    });
    return () => {
      alive = false;
    };
  }, [src]);
  if (!url) return <span className="shimmer inline-block h-40 w-64 rounded-xl" style={{ background: "var(--bg-sunken)" }} />;
  return <img src={url} alt={alt || ""} />;
}

function CopyBtn({ text }: { text: string }) {
  const [ok, setOk] = useState(false);
  return (
    <button
      className="icon-btn h-7 w-7"
      title="Copy"
      onClick={() => {
        navigator.clipboard.writeText(text).then(() => {
          setOk(true);
          setTimeout(() => setOk(false), 1200);
        });
      }}
    >
      {ok ? <Check size={13} /> : <Copy size={13} />}
    </button>
  );
}

function SpeakBtn({ text }: { text: string }) {
  const [busy, setBusy] = useState(false);
  const toast = useApp((s) => s.toast);
  return (
    <button
      className="icon-btn h-7 w-7"
      title="Read aloud"
      disabled={busy || !text.trim()}
      onClick={async () => {
        setBusy(true);
        try {
          const r = await voice.speak(text.slice(0, 4000));
          const b64 = r.audio || r.base64;
          if (b64) {
            const audio = new Audio(`data:audio/wav;base64,${b64}`);
            await audio.play();
          } else if ("speechSynthesis" in window) {
            const u = new SpeechSynthesisUtterance(text.slice(0, 4000));
            window.speechSynthesis.speak(u);
          } else toast("No TTS available", "info");
        } catch {
          if ("speechSynthesis" in window) {
            const u = new SpeechSynthesisUtterance(text.slice(0, 4000));
            window.speechSynthesis.speak(u);
          } else toast("Read-aloud failed", "error");
        } finally {
          setBusy(false);
        }
      }}
    >
      <Volume2 size={13} className={busy ? "animate-pulse" : ""} />
    </button>
  );
}

function Collapsible({ icon, title, children, defaultOpen = false, tone }: { icon: React.ReactNode; title: React.ReactNode; children: React.ReactNode; defaultOpen?: boolean; tone?: string }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="my-1.5 overflow-hidden rounded-xl text-[13px]" style={{ border: "1px solid var(--border)", background: "var(--bg-sunken)" }}>
      <button className="flex w-full items-center gap-2 px-3 py-2 text-left" onClick={() => setOpen((v) => !v)} style={{ color: tone || "var(--muted)" }}>
        {icon}
        <span className="flex-1 truncate">{title}</span>
        <ChevronDown size={14} className="transition-transform" style={{ transform: open ? "rotate(180deg)" : "none" }} />
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.18 }} className="overflow-hidden">
            <div className="px-3 pb-3">{children}</div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function Tool({ t }: { t: ToolCall }) {
  return (
    <Collapsible
      icon={<Terminal size={14} />}
      tone={t.running ? "var(--accent)" : t.exit_code && t.exit_code !== 0 ? "#f87171" : undefined}
      title={
        <span className="flex items-center gap-2">
          <span className="font-medium">{t.tool}</span>
          {t.command && <code className="truncate opacity-80" style={{ fontFamily: "var(--font-mono)", fontSize: "12px" }}>{t.command}</code>}
          {t.running && (
            <span className="ml-auto flex items-center gap-1 text-[11px]">
              <Clock size={11} /> {t.elapsed ? `${Math.round(t.elapsed)}s` : "running"}
            </span>
          )}
        </span>
      }
    >
      <pre className="selectable max-h-72 overflow-auto whitespace-pre-wrap rounded-lg p-2.5 text-[12px] leading-relaxed" style={{ background: "var(--code-bg)", color: "#dfe3ee", fontFamily: "var(--font-mono)" }}>
        {t.running ? t.tail || "…" : t.output || "(no output)"}
      </pre>
    </Collapsible>
  );
}

function AskUser({ msg }: { msg: Msg }) {
  const aq = msg.askUser;
  const send = useApp((s) => s.send);
  const streaming = useApp((s) => s.streaming);
  const [done, setDone] = useState<string | null>(null);
  if (!aq) return null;
  const isApproval = aq.kind === "tool_approval" && aq.approval_id;
  const opts: any[] = aq.options || [];
  return (
    <motion.div initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} className="mt-3 rounded-2xl p-4" style={{ border: "1px solid color-mix(in oklab, var(--accent) 40%, var(--border))", background: "var(--accent-soft)" }}>
      <div className="mb-1 flex items-center gap-2 text-[13px] font-semibold">
        {isApproval ? <AlertTriangle size={15} style={{ color: "var(--accent)" }} /> : <Brain size={15} style={{ color: "var(--accent)" }} />}
        {aq.question}
      </div>
      {aq.description && (
        <p className="mb-3 text-[13px]" style={{ color: "var(--muted)" }}>
          {aq.description}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        {opts.map((o: any, i: number) => {
          const label = typeof o === "string" ? o : o.label;
          const value = typeof o === "string" ? o : o.value ?? o.label;
          const picked = done === String(value);
          return (
            <button
              key={i}
              className={`btn ${i === 0 ? "btn-primary" : ""}`}
              disabled={streaming || !!done}
              title={o.description}
              style={picked ? { outline: "2px solid var(--accent)" } : undefined}
              onClick={() => {
                setDone(String(value));
                if (isApproval) send("", [], { id: aq.approval_id, decision: String(value) });
                else send(label);
              }}
            >
              {label}
            </button>
          );
        })}
        {isApproval && (
          <button className="btn btn-ghost" disabled={streaming || !!done} onClick={() => { setDone("deny"); send("", [], { id: aq.approval_id, decision: "deny" }); }}>
            Deny
          </button>
        )}
      </div>
    </motion.div>
  );
}

function MessageView({ msg, isLast }: { msg: Msg; isLast: boolean }) {
  const isUser = msg.role === "user";
  const openLink = async (href: string) => {
    if (inTauri) {
      const { openUrl } = await import("@tauri-apps/plugin-opener");
      openUrl(href).catch(() => {});
    } else window.open(href, "_blank", "noopener");
  };

  if (isUser) {
    return (
      <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.22 }} className="group flex justify-end">
        <div className="max-w-[78%]">
          {msg.attachments && msg.attachments.length > 0 && (
            <div className="mb-1.5 flex flex-wrap justify-end gap-1.5">
              {msg.attachments.map((a) => (
                <span key={a.id} className="pill" data-on="true">
                  <Paperclip size={11} /> {a.name}
                </span>
              ))}
            </div>
          )}
          <div className="selectable whitespace-pre-wrap rounded-3xl rounded-br-lg px-4 py-2.5 text-[14.5px] leading-relaxed" style={{ background: "var(--user-bubble)", color: "var(--user-text)" }}>
            {msg.content}
          </div>
          <div className="mt-1 flex justify-end opacity-0 transition-opacity group-hover:opacity-100">
            <CopyBtn text={msg.content} />
          </div>
        </div>
      </motion.div>
    );
  }

  const empty = !msg.content && !msg.thinking && !(msg.tools && msg.tools.length) && !msg.error;

  return (
    <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.22 }} className="group flex gap-3">
      <div className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center overflow-hidden rounded-xl" style={{ background: "var(--accent-soft)" }}>
        <img src="/icon.png" alt="" className="h-5 w-5" draggable={false} />
      </div>
      <div className="min-w-0 flex-1">
        {msg.model && (
          <div className="mb-1 text-[11px] font-medium" style={{ color: "var(--muted)" }}>
            {msg.model.split("/").pop()}
          </div>
        )}

        {msg.thinking && (
          <Collapsible icon={<Brain size={14} />} title={msg.streaming && !msg.content ? "Thinking…" : "Reasoning"} defaultOpen={false}>
            <div className="md whitespace-pre-wrap text-[13px] opacity-80">{msg.thinking}</div>
          </Collapsible>
        )}

        {msg.tools?.map((t) => (
          <Tool key={t.id} t={t} />
        ))}

        {empty && msg.streaming && (
          <div className="dots py-2">
            <span />
            <span />
            <span />
          </div>
        )}

        {msg.content && (
          <div className={`md text-[14.5px] leading-relaxed ${msg.streaming ? "caret" : ""}`}>
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              rehypePlugins={[rehypeHighlight]}
              components={{
                a: ({ href, children }) => (
                  <a
                    href={href}
                    onClick={(e) => {
                      e.preventDefault();
                      if (href) openLink(href);
                    }}
                  >
                    {children}
                  </a>
                ),
                img: ({ src, alt }) => <ApiImage src={typeof src === "string" ? src : undefined} alt={alt} />,
                pre: ({ children }) => {
                  const text = extractText(children);
                  return (
                    <div className="relative">
                      <div className="absolute right-2 top-2 opacity-0 transition-opacity group-hover:opacity-100">
                        <CopyBtn text={text} />
                      </div>
                      <pre>{children}</pre>
                    </div>
                  );
                },
              }}
            >
              {msg.content}
            </ReactMarkdown>
          </div>
        )}

        {msg.error && (
          <div className="mt-2 flex items-start gap-2 rounded-xl px-3 py-2 text-[13px] text-red-400" style={{ background: "rgba(239,68,68,.1)" }}>
            <AlertTriangle size={15} className="mt-0.5 shrink-0" />
            <span className="selectable">{msg.error}</span>
          </div>
        )}

        {msg.sources && msg.sources.length > 0 && (
          <Collapsible icon={<Globe size={14} />} title={`${msg.sources.length} source${msg.sources.length > 1 ? "s" : ""}`}>
            <ul className="flex flex-col gap-1.5">
              {msg.sources.map((s, i) => (
                <li key={i} className="truncate">
                  <a className="cursor-pointer hover:underline" style={{ color: "var(--accent)" }} onClick={() => s.url && openLink(s.url)}>
                    {s.title || s.url}
                  </a>
                </li>
              ))}
            </ul>
          </Collapsible>
        )}

        <AskUser msg={msg} />

        {!msg.streaming && msg.content && (
          <div className={`mt-1 flex items-center gap-1 transition-opacity ${isLast ? "opacity-100" : "opacity-0 group-hover:opacity-100"}`}>
            <CopyBtn text={msg.content} />
            <SpeakBtn text={msg.content} />
            {msg.metrics?.tokens_per_second && (
              <span className="text-[11px]" style={{ color: "var(--muted)" }}>
                {Number(msg.metrics.tokens_per_second).toFixed(1)} tok/s
              </span>
            )}
          </div>
        )}
      </div>
    </motion.div>
  );
}

function extractText(node: any): string {
  if (node == null) return "";
  if (typeof node === "string") return node;
  if (Array.isArray(node)) return node.map(extractText).join("");
  if (node.props?.children) return extractText(node.props.children);
  return "";
}

export default memo(MessageView);
