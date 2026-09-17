import { useEffect, useState } from "react";
import { motion, AnimatePresence } from "motion/react";
import { X, Server, KeyRound, Info, Plus, Trash2, RefreshCw, Sun, Moon, TerminalSquare, Search, Keyboard, HardDrive } from "lucide-react";
import { useApp } from "../store/app";
import { auth, endpoints as epApi, search as searchApi, type Endpoint } from "../lib/api";
import { backendStatus, inTauri, restartBackend } from "../lib/ipc";

type Tab = "models" | "search" | "account" | "engine" | "about";

export default function Settings() {
  const open = useApp((s) => s.settingsOpen);
  const close = () => useApp.getState().setSettings(false);
  const [tab, setTab] = useState<Tab>("models");
  const isAdmin = useApp((s) => s.authStatus?.is_admin);

  useEffect(() => {
    if (!open) return;
    const h = (e: globalThis.KeyboardEvent) => e.key === "Escape" && close();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open]);

  const tabs: { id: Tab; label: string; icon: React.ReactNode }[] = [
    { id: "models", label: "Models", icon: <Server size={15} /> },
    { id: "search", label: "Search", icon: <Search size={15} /> },
    { id: "account", label: "Account", icon: <KeyRound size={15} /> },
    { id: "engine", label: "Engine", icon: <TerminalSquare size={15} /> },
    { id: "about", label: "About", icon: <Info size={15} /> },
  ];

  return (
    <AnimatePresence>
      {open && (
        <motion.div className="absolute inset-0 z-50 flex items-center justify-center p-6" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} style={{ background: "rgba(0,0,0,.45)", backdropFilter: "blur(6px)" }} onMouseDown={(e) => e.target === e.currentTarget && close()}>
          <motion.div initial={{ opacity: 0, scale: 0.96, y: 10 }} animate={{ opacity: 1, scale: 1, y: 0 }} exit={{ opacity: 0, scale: 0.96, y: 10 }} transition={{ type: "spring", stiffness: 420, damping: 34 }} className="flex h-[min(640px,90vh)] w-full max-w-3xl overflow-hidden rounded-3xl" style={{ background: "var(--bg-elev)", border: "1px solid var(--border)", boxShadow: "var(--shadow)" }}>
            <nav className="flex w-48 shrink-0 flex-col gap-1 p-3" style={{ borderRight: "1px solid var(--border)", background: "var(--bg-sunken)" }}>
              <div className="px-2 pb-3 pt-1 text-base font-semibold">Settings</div>
              {tabs.map((t) => (
                <button key={t.id} className="relative flex items-center gap-2 rounded-xl px-3 py-2 text-left text-[13px] font-medium transition-colors" style={{ color: tab === t.id ? "var(--text)" : "var(--muted)" }} onClick={() => setTab(t.id)}>
                  {tab === t.id && <motion.span layoutId="settings-tab" className="absolute inset-0 rounded-xl" style={{ background: "var(--accent-soft)" }} />}
                  <span className="relative flex items-center gap-2">
                    {t.icon} {t.label}
                  </span>
                </button>
              ))}
            </nav>
            <div className="relative flex min-w-0 flex-1 flex-col">
              <button className="icon-btn absolute right-3 top-3" onClick={close}>
                <X size={16} />
              </button>
              <div className="flex-1 overflow-y-auto p-6">
                {tab === "models" && <ModelsTab isAdmin={!!isAdmin} />}
                {tab === "search" && <SearchTab />}
                {tab === "account" && <AccountTab />}
                {tab === "engine" && <EngineTab />}
                {tab === "about" && <AboutTab />}
              </div>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

function Section({ title, desc, children }: { title: string; desc?: string; children: React.ReactNode }) {
  return (
    <section className="mb-7">
      <h3 className="text-[15px] font-semibold">{title}</h3>
      {desc && (
        <p className="mb-3 mt-0.5 text-[13px]" style={{ color: "var(--muted)" }}>
          {desc}
        </p>
      )}
      {children}
    </section>
  );
}

function ModelsTab({ isAdmin }: { isAdmin: boolean }) {
  const [list, setList] = useState<Endpoint[] | null>(null);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const loadModels = useApp((s) => s.loadModels);
  const toast = useApp((s) => s.toast);
  const openHub = () => {
    useApp.getState().setSettings(false);
    useApp.getState().setView("models");
  };

  const refresh = () => {
    if (!isAdmin) return;
    epApi.list().then(setList).catch((e) => setErr(e.message));
  };
  useEffect(refresh, [isAdmin]);

  const add = async () => {
    setErr(null);
    setBusy(true);
    try {
      await epApi.create(name.trim() || url.trim(), url.trim(), key.trim());
      setName("");
      setUrl("");
      setKey("");
      refresh();
      await loadModels(true);
      toast("Endpoint added", "success");
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  if (!isAdmin)
    return (
      <Section title="Models" desc="Only an administrator can manage model endpoints. Ask your admin to add one, then pick it from the model menu.">
        <button className="btn" onClick={openHub}>
          <HardDrive size={15} /> Browse the Models hub
        </button>
      </Section>
    );

  return (
    <>
      <Section title="Download models" desc="Pull a Hugging Face repo or an Ollama tag, then serve it so it appears in the chat picker.">
        <button className="btn btn-primary" onClick={openHub}>
          <HardDrive size={15} /> Open Models hub
        </button>
      </Section>
      <Section title="Model endpoints" desc="Any OpenAI-compatible server: llama.cpp, Ollama, vLLM, LM Studio, or a cloud API key.">
        <div className="flex flex-col gap-2">
          {list === null && <div className="shimmer h-12 rounded-xl" style={{ background: "var(--bg-sunken)" }} />}
          {list?.length === 0 && (
            <p className="text-[13px]" style={{ color: "var(--muted)" }}>
              No endpoints yet. The local model group registers itself here once it has finished downloading.
            </p>
          )}
          {list?.map((e) => (
            <div key={e.id} className="flex items-center gap-3 rounded-xl px-3 py-2.5" style={{ border: "1px solid var(--border)" }}>
              <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: e.status === "online" ? "#34d399" : e.status === "empty" ? "#fbbf24" : "#f87171" }} />
              <div className="min-w-0 flex-1">
                <div className="truncate text-[13px] font-medium">{e.name}</div>
                <div className="truncate text-[12px]" style={{ color: "var(--muted)", fontFamily: "var(--font-mono)" }}>
                  {e.base_url} · {e.model_count ?? e.models.length} models{e.has_key ? " · key set" : ""}
                </div>
                {e.ping_error && <div className="truncate text-[11px] text-red-400">{e.ping_error}</div>}
              </div>
              <button
                className="icon-btn hover:!text-red-400"
                title="Remove"
                onClick={async () => {
                  await epApi.remove(e.id).catch((x) => toast(x.message, "error"));
                  refresh();
                  loadModels(true);
                }}
              >
                <Trash2 size={15} />
              </button>
            </div>
          ))}
        </div>
      </Section>
      <Section title="Add endpoint">
        <div className="flex flex-col gap-2">
          <input className="input" placeholder="Name (optional)" value={name} onChange={(e) => setName(e.target.value)} />
          <input className="input" placeholder="Base URL, e.g. http://127.0.0.1:8080/v1 or https://api.openai.com/v1" value={url} onChange={(e) => setUrl(e.target.value)} />
          <input className="input" type="password" placeholder="API key (if needed)" value={key} onChange={(e) => setKey(e.target.value)} />
          {err && <p className="text-[13px] text-red-400">{err}</p>}
          <div className="flex gap-2">
            <button className="btn btn-primary" disabled={!url.trim() || busy} onClick={add}>
              <Plus size={15} /> Add
            </button>
            <button className="btn" onClick={() => { refresh(); loadModels(true); }}>
              <RefreshCw size={15} /> Refresh
            </button>
          </div>
        </div>
      </Section>
    </>
  );
}

function SearchTab() {
  const [providers, setProviders] = useState<{ id: string; label: string; available: boolean }[]>([]);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<any[] | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useApp((s) => s.toast);

  useEffect(() => {
    searchApi
      .providers()
      .then((r) => setProviders(Array.isArray(r) ? r : (r as any).providers || []))
      .catch(() => setProviders([]));
  }, []);

  const run = async () => {
    if (!query.trim()) return;
    setBusy(true);
    try {
      const r = await searchApi.web(query.trim());
      setHits(r.sources || []);
      if (r.error) toast(r.error, "error");
    } catch (e: any) {
      toast(e.message || "Search failed", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Section title="Web search providers" desc="Used by the Web toggle in chat and by Deep Research.">
        <div className="flex flex-col gap-1.5">
          {providers.length === 0 && <p className="text-[13px]" style={{ color: "var(--muted)" }}>No provider list yet. DuckDuckGo works without a key.</p>}
          {providers.map((p) => (
            <div key={p.id} className="flex items-center gap-2 rounded-xl px-3 py-2" style={{ border: "1px solid var(--border)" }}>
              <span className="h-2 w-2 rounded-full" style={{ background: p.available ? "#34d399" : "#f87171" }} />
              <span className="text-[13px]">{p.label || p.id}</span>
              <span className="ml-auto text-[11px]" style={{ color: "var(--muted)" }}>{p.available ? "ready" : "needs setup"}</span>
            </div>
          ))}
        </div>
      </Section>
      <Section title="Test a query">
        <div className="flex gap-2">
          <input className="input" placeholder="Search the web…" value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => e.key === "Enter" && run()} />
          <button className="btn btn-primary" disabled={!query.trim() || busy} onClick={run}>Search</button>
        </div>
        {hits && (
          <ul className="mt-3 flex flex-col gap-1.5">
            {hits.length === 0 && <li className="text-[13px]" style={{ color: "var(--muted)" }}>No results</li>}
            {hits.slice(0, 8).map((h, i) => (
              <li key={i} className="truncate text-[13px]">
                <a className="hover:underline" style={{ color: "var(--accent)" }} href={h.url} onClick={(e) => { e.preventDefault(); if (h.url) window.open(h.url, "_blank"); }}>{h.title || h.url}</a>
              </li>
            ))}
          </ul>
        )}
      </Section>
    </>
  );
}

function AccountTab() {
  const user = useApp((s) => s.authStatus?.username);
  const theme = useApp((s) => s.theme);
  const toggleTheme = useApp((s) => s.toggleTheme);
  const toast = useApp((s) => s.toast);
  const [cur, setCur] = useState("");
  const [nw, setNw] = useState("");
  const [cf, setCf] = useState("");
  const [busy, setBusy] = useState(false);

  return (
    <>
      <Section title="Appearance">
        <button className="btn" onClick={toggleTheme}>
          {theme === "dark" ? <Sun size={15} /> : <Moon size={15} />} Switch to {theme === "dark" ? "light" : "dark"} theme
        </button>
      </Section>
      <Section title="Change password" desc={`Signed in as ${user}`}>
        <div className="flex max-w-sm flex-col gap-2">
          <input className="input" type="password" placeholder="Current password" value={cur} onChange={(e) => setCur(e.target.value)} />
          <input className="input" type="password" placeholder="New password" value={nw} onChange={(e) => setNw(e.target.value)} />
          <input className="input" type="password" placeholder="Confirm new password" value={cf} onChange={(e) => setCf(e.target.value)} />
          <button
            className="btn btn-primary self-start"
            disabled={busy || !cur || !nw || nw !== cf}
            onClick={async () => {
              setBusy(true);
              try {
                await auth.changePassword(cur, nw);
                toast("Password updated", "success");
                setCur("");
                setNw("");
                setCf("");
              } catch (e: any) {
                toast(e.message, "error");
              } finally {
                setBusy(false);
              }
            }}
          >
            Update password
          </button>
        </div>
      </Section>
    </>
  );
}

function EngineTab() {
  const [log, setLog] = useState<string[]>([]);
  const [status, setStatus] = useState("");
  const refresh = () =>
    backendStatus().then((b) => {
      setLog(b.log);
      setStatus(b.status);
    });
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 2000);
    return () => clearInterval(t);
  }, []);
  return (
    <Section title="Local engine" desc="The Python backend runs as a private process inside this app. It is not reachable from a browser.">
      <div className="mb-3 flex items-center gap-2 text-[13px]">
        <span className="h-2.5 w-2.5 rounded-full" style={{ background: status === "ready" ? "#34d399" : status === "error" ? "#f87171" : "#fbbf24" }} />
        Status: <b>{status || "unknown"}</b>
        {inTauri && (
          <button className="btn ml-auto h-8" onClick={() => restartBackend().then(() => useApp.setState({ screen: "boot" }))}>
            <RefreshCw size={14} /> Restart engine
          </button>
        )}
      </div>
      <pre className="selectable max-h-[380px] overflow-auto rounded-xl p-3 text-[11px] leading-relaxed" style={{ background: "var(--code-bg)", color: "#c9cee0", fontFamily: "var(--font-mono)" }}>
        {log.join("\n") || "(no log yet)"}
      </pre>
    </Section>
  );
}

function AboutTab() {
  const version = useApp((s) => s.version);
  const setShortcuts = useApp((s) => s.setShortcuts);
  const close = () => useApp.getState().setSettings(false);
  return (
    <Section title="psd.ai" desc="A private, local-first AI assistant.">
      <div className="flex flex-col gap-1 text-[13px]" style={{ color: "var(--muted)" }}>
        <div>Backend version: {version || "—"}</div>
        <div>Shell: Tauri 2 · React · Vite · Tailwind</div>
        <div>All data and models stay on this computer.</div>
      </div>
      <button className="btn mt-4" onClick={() => { close(); setShortcuts(true); }}>
        <Keyboard size={15} /> Keyboard shortcuts
      </button>
    </Section>
  );
}
