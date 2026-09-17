import { useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "motion/react";
import { ArrowDown, Sparkles, Code2, Lightbulb, PenLine } from "lucide-react";
import { useApp } from "../store/app";
import MessageView from "./Message";
import Composer from "./Composer";
import ModelPicker from "./ModelPicker";

const SUGGESTIONS = [
  { icon: <Sparkles size={15} />, title: "Explain something", text: "Explain how large language models generate text, in simple terms." },
  { icon: <Code2 size={15} />, title: "Write code", text: "Write a Python script that renames all files in a folder to lowercase." },
  { icon: <Lightbulb size={15} />, title: "Brainstorm", text: "Give me 10 creative names for a personal productivity app." },
  { icon: <PenLine size={15} />, title: "Draft an email", text: "Draft a polite email asking my professor for a deadline extension." },
];

export default function Chat() {
  const sid = useApp((s) => s.activeSessionId);
  const messages = useApp((s) => (s.activeSessionId ? s.messages[s.activeSessionId] || [] : []));
  const loading = useApp((s) => s.loadingHistory);
  const send = useApp((s) => s.send);
  const session = useApp((s) => s.sessions.find((x) => x.id === s.activeSessionId));
  const user = useApp((s) => s.authStatus?.username);
  const scroller = useRef<HTMLDivElement>(null);
  const [stuck, setStuck] = useState(true);

  useEffect(() => {
    if (stuck) scroller.current?.scrollTo({ top: scroller.current.scrollHeight });
  }, [messages, stuck]);

  useEffect(() => {
    setStuck(true);
    requestAnimationFrame(() => scroller.current?.scrollTo({ top: scroller.current.scrollHeight }));
  }, [sid]);

  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    setStuck(el.scrollHeight - el.scrollTop - el.clientHeight < 80);
  };

  const hour = new Date().getHours();
  const greet = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";

  return (
    <section className="relative flex h-full min-w-0 flex-1 flex-col">
      <div className="relative z-10 flex h-12 shrink-0 items-center gap-3 px-4">
        <ModelPicker />
        {session && (
          <span className="truncate text-[13px]" style={{ color: "var(--muted)" }}>
            {session.name}
          </span>
        )}
      </div>

      <div ref={scroller} onScroll={onScroll} className="relative z-10 flex-1 overflow-y-auto px-4">
        <div className="mx-auto flex max-w-3xl flex-col gap-6 py-4">
          <AnimatePresence mode="wait">
            {!sid && (
              <motion.div key="empty" initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }} className="flex min-h-[55vh] flex-col items-center justify-center gap-8">
                <div className="text-center">
                  <motion.img src="/icon.png" alt="" className="mx-auto mb-4 h-16 w-16 rounded-2xl" draggable={false} initial={{ scale: 0.8, rotate: -6 }} animate={{ scale: 1, rotate: 0 }} transition={{ type: "spring", stiffness: 260, damping: 18 }} />
                  <h1 className="text-[26px] font-semibold tracking-tight">
                    {greet}
                    {user ? `, ${user}` : ""}.
                  </h1>
                  <p className="mt-1 text-[15px]" style={{ color: "var(--muted)" }}>
                    What can I help you with today?
                  </p>
                </div>
                <div className="grid w-full max-w-2xl grid-cols-2 gap-2.5">
                  {SUGGESTIONS.map((s, i) => (
                    <motion.button key={s.title} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.06 * i }} whileHover={{ y: -2 }} className="glass flex flex-col items-start gap-1.5 rounded-2xl p-4 text-left transition-shadow hover:shadow-lg" onClick={() => send(s.text)}>
                      <span className="flex items-center gap-2 text-[13px] font-semibold" style={{ color: "var(--accent)" }}>
                        {s.icon} {s.title}
                      </span>
                      <span className="text-[13px] leading-snug" style={{ color: "var(--muted)" }}>
                        {s.text}
                      </span>
                    </motion.button>
                  ))}
                </div>
              </motion.div>
            )}
          </AnimatePresence>

          {sid && loading && messages.length === 0 && (
            <div className="flex flex-col gap-4 pt-6">
              {[80, 55, 70].map((w, i) => (
                <div key={i} className={`shimmer h-5 rounded-lg ${i % 2 ? "self-end" : ""}`} style={{ width: `${w}%`, background: "var(--bg-sunken)" }} />
              ))}
            </div>
          )}

          {messages.map((m, i) => (
            <MessageView key={m.id} msg={m} isLast={i === messages.length - 1} />
          ))}
          <div className="h-2" />
        </div>
      </div>

      <AnimatePresence>
        {!stuck && messages.length > 0 && (
          <motion.button
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 8 }}
            className="glass absolute bottom-[132px] left-1/2 z-20 flex h-9 w-9 -translate-x-1/2 items-center justify-center rounded-full"
            onClick={() => {
              setStuck(true);
              scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
            }}
          >
            <ArrowDown size={15} />
          </motion.button>
        )}
      </AnimatePresence>

      <Composer />
    </section>
  );
}
