import { useCallback, useEffect, useState } from "react";
import { motion } from "motion/react";
import { X, Server, KeyRound, Info, Plus, Trash2, RefreshCw, TerminalSquare, Search, Keyboard, HardDrive, Mail, Type, Mic, MonitorPlay, ShieldAlert, Volume2, BrainCircuit, Sparkles } from "lucide-react";
import { useApp } from "../store/app";
import {
  auth,
  endpoints as epApi,
  search as searchApi,
  email as emailApi,
  settings as settingsApi,
  psd as psdApi,
  jarvis as jarvisApi,
  type Endpoint,
  type JarvisStatus,
} from "../lib/api";
import { backendStatus, inTauri, restartBackend } from "../lib/ipc";
import Overlay from "./Overlay";
import { THEMES } from "../lib/ui";

type Tab = "models" | "psd" | "voice" | "search" | "account" | "appearance" | "email" | "engine" | "about";

export default function Settings() {
  const open = useApp((s) => s.settingsOpen);
  const close = () => useApp.getState().setSettings(false);
  const tab = (useApp((s) => s.settingsTab) || "models") as Tab;
  const setTab = (t: Tab) => useApp.getState().setSettingsTab(t);
  const isAdmin = useApp((s) => s.authStatus?.is_admin);

  const tabs: { id: Tab; label: string; icon: React.ReactNode }[] = [
    { id: "models", label: "Models", icon: <Server size={15} /> },
    { id: "psd", label: "PSD", icon: <BrainCircuit size={15} /> },
    { id: "voice", label: "Voice & PC", icon: <Mic size={15} /> },
    { id: "search", label: "Search", icon: <Search size={15} /> },
    { id: "account", label: "Account", icon: <KeyRound size={15} /> },
    { id: "appearance", label: "Appearance", icon: <Type size={15} /> },
    { id: "email", label: "Email", icon: <Mail size={15} /> },
    { id: "engine", label: "Engine", icon: <TerminalSquare size={15} /> },
    { id: "about", label: "About", icon: <Info size={15} /> },
  ];

  return (
    <Overlay open={open} onClose={close} labelledBy="settings-title">
          <div className="flex h-[min(640px,90vh)] w-full max-w-3xl overflow-hidden rounded-3xl" style={{ background: "var(--bg-elev)", border: "1px solid var(--border)", boxShadow: "var(--shadow)" }}>
            <nav className="flex w-48 shrink-0 flex-col gap-1 p-3" style={{ borderRight: "1px solid var(--border)", background: "var(--bg-sunken)" }}>
              <div id="settings-title" className="px-2 pb-3 pt-1 text-base font-semibold">Settings</div>
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
                {tab === "psd" && <PsdTab />}
                {tab === "voice" && <VoiceTab isAdmin={!!isAdmin} />}
                {tab === "search" && <SearchTab />}
                {tab === "account" && <AccountTab />}
                {tab === "appearance" && <AppearanceTab />}
                {tab === "email" && <EmailAccountsTab />}
                {tab === "engine" && <EngineTab />}
                {tab === "about" && <AboutTab />}
              </div>
            </div>
          </div>
    </Overlay>
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

function PsdTab() {
  const toast = useApp((s) => s.toast);
  const [status, setStatus] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [workspaceDraft, setWorkspaceDraft] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setStatus(await psdApi.status());
    } catch (e: any) {
      toast(e?.message || "Could not load PSD status", "error");
    }
  }, [toast]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (status?.idle?.workspace !== undefined) {
      setWorkspaceDraft(status.idle.workspace || "");
    }
  }, [status?.idle?.workspace]);

  const idle = status?.idle?.settings || {};
  const workspaceValue = workspaceDraft ?? idle.idle_workspace ?? "";
  const profile = status?.profile || {};
  const save = async (patch: Record<string, any>) => {
    setBusy(true);
    try {
      const next = await psdApi.update(patch);
      setStatus((current: any) => current ? { ...current, idle: next } : current);
    } catch (e: any) {
      toast(e?.message || "Could not save PSD settings", "error");
    } finally {
      setBusy(false);
    }
  };

  const learnNow = async () => {
    setBusy(true);
    try {
      await psdApi.learnNow();
      toast("PSD will learn from the next completed conversation", "success");
      window.setTimeout(() => void load(), 800);
    } catch (e: any) {
      toast(e?.message || "Could not queue PSD learning", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Section
        title="PSD — your local coding model"
        desc="PSD is preloaded by the local model group when hardware allows it. It uses a hardware-fit Qwen Coder base behind a stable PSD identity, with psd.ai's tools, browser, PC control, memory and skills available."
      >
        <div className="mb-3 flex flex-wrap gap-1.5 text-[12px]">
          <span className="pill" data-on={!!profile.available}><span className="h-1.5 w-1.5 rounded-full" style={{ background: profile.available ? "#34d399" : "#f87171" }} />{profile.available ? "Ready" : "Not served yet"}</span>
          <span className="pill" data-on>{profile.model || "psd"}</span>
          {profile.endpoint_name && <span className="pill">{profile.endpoint_name}</span>}
          <span className="pill"><Sparkles size={12} /> coding · tools · browser · memory</span>
        </div>
        {!profile.available && <p className="mb-3 text-[12px]" style={{ color: "var(--muted)" }}>Start the local model group or download/serve a Qwen Coder model from the Models hub. PSD will appear in the picker when its local endpoint is ready.</p>}
        <button className="btn btn-primary" disabled={busy} onClick={() => { void load(); }}>
          <RefreshCw size={14} className={busy ? "animate-spin" : ""} /> Refresh PSD status
        </button>
      </Section>

      <Section
        title="Learn while I am idle"
        desc="After no new command for the idle window, PSD reviews one completed conversation at a time and catches up on durable memories and reusable skills. Active chats always win."
      >
        <div className="flex flex-col gap-2">
          <Toggle
            on={idle.idle_learning_enabled !== false}
            onChange={(value) => void save({ idle_learning_enabled: value })}
            label="Idle learning"
            desc="Build memories and skills from completed work without changing model weights."
          />
          <div className="grid grid-cols-2 gap-2">
            <label className="text-[12px]" style={{ color: "var(--muted)" }}>
              Start after (minutes)
              <input className="input mt-1" type="number" min={1} max={1440} value={idle.idle_after_minutes ?? 10} disabled={busy} onChange={(e) => void save({ idle_after_minutes: Number(e.target.value) || 10 })} />
            </label>
            <label className="text-[12px]" style={{ color: "var(--muted)" }}>
              Repeat every (minutes)
              <input className="input mt-1" type="number" min={5} max={10080} value={idle.idle_cycle_minutes ?? 30} disabled={busy} onChange={(e) => void save({ idle_cycle_minutes: Number(e.target.value) || 30 })} />
            </label>
          </div>
          <Toggle
            on={idle.auto_memory !== false}
            onChange={(value) => void save({ auto_memory: value })}
            label="Learn durable memories"
            desc="Keeps useful preferences, identity and project facts; it is instructed not to save secrets or transient chat."
          />
          <Toggle
            on={idle.auto_skills !== false}
            onChange={(value) => void save({ auto_skills: value })}
            label="Build reusable skills"
            desc="Turns proven multi-step workflows into reusable procedures and lets the existing skill audit review them."
          />
          <button className="btn self-start" disabled={busy} onClick={() => void learnNow()}>
            <Sparkles size={14} /> Learn from the latest completed chat now
          </button>
          {status?.idle?.last_run_at && <p className="text-[12px]" style={{ color: "var(--muted)" }}>Last idle pass: {status.idle.last_run_at} · {status.idle.last_result || "reviewed"}</p>}
        </div>
      </Section>

      <Section
        title="Idle coding changes"
        desc="The normal PSD agent can edit code when you ask it. Automatic idle edits are a separate, opt-in capability and require an existing absolute workspace."
      >
        <div className="flex flex-col gap-2">
          <Toggle
            on={idle.idle_code_changes === true}
            onChange={(value) => void save({ idle_code_changes: value })}
            label="Allow PSD to make bounded code fixes while idle"
            desc="Off by default. When on, PSD only reviews coding conversations in the workspace below, inspects before editing, and runs focused verification."
          />
          <input
            className="input"
            placeholder="Absolute workspace path, e.g. C:\\Projects\\my-app"
            value={workspaceValue}
            disabled={busy}
            onChange={(e) => setWorkspaceDraft(e.target.value)}
            onBlur={() => {
              if (workspaceValue !== (idle.idle_workspace || "")) void save({ idle_workspace: workspaceValue });
            }}
          />
          {idle.idle_code_changes === true && !status?.idle?.workspace_valid && <p className="text-[12px] text-amber-300">Choose a folder that exists before enabling idle code changes.</p>}
          <div className="flex items-start gap-2 rounded-xl px-3 py-2 text-[12px]" style={{ background: "var(--bg-sunken)", border: "1px solid var(--border)", color: "var(--muted)" }}>
            <ShieldAlert size={14} className="mt-0.5 shrink-0" />
            PSD never edits outside the selected workspace in this mode, never changes dependencies/secrets/authentication, and still obeys the app's tool policy and approval limits.
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
  const [config, setConfig] = useState<any>(null);
  const toast = useApp((s) => s.toast);

  useEffect(() => {
    searchApi
      .providers()
      .then((r) => setProviders(Array.isArray(r) ? r : (r as any).providers || []))
      .catch(() => setProviders([]));
    searchApi.config().then(setConfig).catch(() => {});
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
      <Section title="Web search providers" desc="API keys are set on the engine (environment / prefs). This list shows which providers are ready.">
        {config && (
          <p className="mb-2 text-[12px]" style={{ color: "var(--muted)" }}>
            Active provider: {config.provider || config.search_provider || "auto"}
          </p>
        )}
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

function AppearanceTab() {
  const theme = useApp((s) => s.theme);
  const setTheme = useApp((s) => s.setTheme);
  const fontScale = useApp((s) => s.fontScale);
  const setFontScale = useApp((s) => s.setFontScale);
  const density = useApp((s) => s.density);
  const setDensity = useApp((s) => s.setDensity);
  return (
    <>
      <Section title="Theme">
        <div className="flex flex-wrap gap-2">
          {THEMES.map((t) => (
            <button key={t.id} className="pill" data-on={theme === t.id} onClick={() => setTheme(t.id)}>
              {t.label}
            </button>
          ))}
        </div>
      </Section>
      <Section title="Text size" desc="Applies to the whole interface.">
        <input type="range" min={13} max={20} value={fontScale} onChange={(e) => setFontScale(Number(e.target.value))} />
        <div className="mt-1 text-[12px]" style={{ color: "var(--muted)" }}>{fontScale}px</div>
      </Section>
      <Section title="Density">
        <div className="flex gap-2">
          <button className="pill" data-on={density === "comfortable"} onClick={() => setDensity("comfortable")}>Comfortable</button>
          <button className="pill" data-on={density === "compact"} onClick={() => setDensity("compact")}>Compact</button>
        </div>
      </Section>
    </>
  );
}

function EmailAccountsTab() {
  const toast = useApp((s) => s.toast);
  const [accounts, setAccounts] = useState<any[]>([]);
  const [form, setForm] = useState({ name: "", from_address: "", imap_host: "", imap_user: "", imap_password: "", smtp_host: "" });
  const refresh = () => emailApi.accounts().then((r) => setAccounts(r.accounts || [])).catch(() => setAccounts([]));
  useEffect(() => { refresh(); }, []);
  return (
    <>
      <Section title="Mailboxes" desc="IMAP/SMTP accounts used by the Email view.">
        {accounts.length === 0 && <p className="text-[13px]" style={{ color: "var(--muted)" }}>No accounts yet.</p>}
        <div className="flex flex-col gap-2">
          {accounts.map((a) => (
            <div key={a.id} className="flex items-center gap-2 rounded-xl px-3 py-2" style={{ border: "1px solid var(--border)" }}>
              <div className="min-w-0 flex-1">
                <div className="truncate text-[13px] font-medium">{a.name || a.from_address}</div>
                <div className="truncate text-[12px]" style={{ color: "var(--muted)" }}>{a.from_address} {a.is_default ? "· default" : ""}</div>
              </div>
              <button className="btn h-7 text-[11px]" onClick={() => emailApi.setDefaultAccount(a.id).then(refresh)}>Default</button>
              <button className="icon-btn hover:!text-red-400" onClick={() => emailApi.removeAccount(a.id).then(refresh)}><Trash2 size={14} /></button>
            </div>
          ))}
        </div>
      </Section>
      <Section title="Add account">
        <div className="flex max-w-md flex-col gap-2">
          <input className="input" placeholder="Display name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <input className="input" placeholder="From address" value={form.from_address} onChange={(e) => setForm({ ...form, from_address: e.target.value })} />
          <input className="input" placeholder="IMAP host" value={form.imap_host} onChange={(e) => setForm({ ...form, imap_host: e.target.value })} />
          <input className="input" placeholder="IMAP user" value={form.imap_user} onChange={(e) => setForm({ ...form, imap_user: e.target.value })} />
          <input className="input" type="password" placeholder="IMAP password" value={form.imap_password} onChange={(e) => setForm({ ...form, imap_password: e.target.value })} />
          <input className="input" placeholder="SMTP host" value={form.smtp_host} onChange={(e) => setForm({ ...form, smtp_host: e.target.value })} />
          <button
            className="btn btn-primary self-start"
            disabled={!form.from_address || !form.imap_host}
            onClick={async () => {
              try {
                await emailApi.createAccount(form);
                toast("Account added", "success");
                setForm({ name: "", from_address: "", imap_host: "", imap_user: "", imap_password: "", smtp_host: "" });
                refresh();
              } catch (e: any) {
                toast(e.message || "Could not add account", "error");
              }
            }}
          >
            <Plus size={14} /> Save account
          </button>
        </div>
      </Section>
    </>
  );
}

function AccountTab() {
  const user = useApp((s) => s.authStatus?.username);
  const toast = useApp((s) => s.toast);
  const [cur, setCur] = useState("");
  const [nw, setNw] = useState("");
  const [cf, setCf] = useState("");
  const [busy, setBusy] = useState(false);

  return (
    <>
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


function Toggle({ on, onChange, label, desc }: { on: boolean; onChange: (v: boolean) => void; label: string; desc?: string }) {
  return (
    <button
      className="flex w-full items-start gap-3 rounded-xl px-3 py-2.5 text-left transition-colors"
      style={{ background: "var(--bg-sunken)", border: "1px solid var(--border)" }}
      onClick={() => onChange(!on)}
      role="switch"
      aria-checked={on}
    >
      <span
        className="mt-0.5 flex h-5 w-9 shrink-0 items-center rounded-full p-0.5 transition-colors"
        style={{ background: on ? "var(--accent)" : "var(--border)" }}
      >
        <motion.span layout className="h-4 w-4 rounded-full bg-white" style={{ marginLeft: on ? 16 : 0 }} />
      </span>
      <span className="min-w-0">
        <span className="block text-[13px] font-medium">{label}</span>
        {desc && (
          <span className="block text-[12px]" style={{ color: "var(--muted)" }}>
            {desc}
          </span>
        )}
      </span>
    </button>
  );
}

function VoiceTab({ isAdmin }: { isAdmin: boolean }) {
  const toast = useApp((s) => s.toast);
  const [status, setStatus] = useState<JarvisStatus | null>(null);
  const [saved, setSaved] = useState<Record<string, any> | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const [s, cfg] = await Promise.all([
      jarvisApi.status().catch(() => null),
      settingsApi.get().catch(() => null),
    ]);
    setStatus(s);
    setSaved(cfg);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const put = async (patch: Record<string, any>) => {
    if (!isAdmin) {
      toast("Only an admin can change these settings", "error");
      return;
    }
    setBusy(true);
    try {
      setSaved(await settingsApi.update(patch));
      await load();
    } catch (e: any) {
      toast(e?.message || "Could not save", "error");
    } finally {
      setBusy(false);
    }
  };

  const val = (key: string, fallback: any) => (saved && key in saved ? saved[key] : fallback);
  const autonomy = String(val("jarvis_autonomy", "full"));
  const jarvisOn = val("jarvis_enabled", true) !== false;
  const pcOn = val("computer_control_enabled", true) !== false;
  const confirmRisky = val("computer_control_confirm", false) === true;
  const missing = status?.computer?.missing_packages || [];

  const testVoice = () => {
    try {
      if (!window.speechSynthesis) throw new Error("This browser has no speech synthesis");
      const u = new SpeechSynthesisUtterance("Voice mode is online, sir. I will always answer in English.");
      u.lang = "en-US";
      u.rate = 1.03;
      window.speechSynthesis.speak(u);
    } catch (e: any) {
      toast(e?.message || "Speech synthesis unavailable", "error");
    }
  };

  return (
    <>
      <Section
        title="Talk to psd.ai"
        desc="Voice mode listens in any language and always answers in English, out loud. Open it from the mic icon in the left rail or with the shortcut."
      >
        <div className="mb-3 flex flex-wrap gap-1.5 text-[12px]">
          <span className="pill" data-on={jarvisOn}>
            <Mic size={12} /> Voice {jarvisOn ? "on" : "off"}
          </span>
          <span className="pill" data-on={!!status?.stt?.available}>
            <Volume2 size={12} /> STT: {status?.stt?.provider || "browser"}
          </span>
          <span className="pill" data-on={!!status?.tts?.available}>
            TTS: {status?.tts?.provider || "browser"}
          </span>
          <span className="pill" data-on={!!status?.llm_configured}>
            Model: {status?.model || "none yet"}
          </span>
        </div>

        <div className="flex flex-col gap-2">
          <Toggle
            on={jarvisOn}
            onChange={(v) => void put({ jarvis_enabled: v })}
            label="Voice mode"
            desc="Turns the Talk screen and the /api/jarvis endpoints on or off."
          />

          <div>
            <div className="mb-1.5 text-[13px] font-medium">Autonomy</div>
            <div className="flex gap-1.5">
              {[
                { id: "full", label: "Full control", desc: "Act on my PC without asking" },
                { id: "confirm", label: "Ask first", desc: "Confirm before risky actions" },
                { id: "off", label: "Chat only", desc: "Never touch the machine" },
              ].map((opt) => (
                <button
                  key={opt.id}
                  className="pill flex-1 justify-center"
                  data-on={autonomy === opt.id}
                  title={opt.desc}
                  disabled={busy}
                  onClick={() => void put({ jarvis_autonomy: opt.id })}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </div>

          <div className="rounded-xl px-3 py-2.5 text-[12px]" style={{ background: "var(--accent-soft)", color: "var(--accent)" }}>
            Replies are pinned to <b>English</b>. You can speak to psd.ai in Hindi, Hinglish or anything else — it always
            answers in English.
          </div>

          <button className="btn" onClick={testVoice}>
            <Volume2 size={14} /> Test the voice
          </button>
        </div>
      </Section>

      <Section
        title="PC control"
        desc="Mouse, keyboard, windows, clipboard, volume and processes — the same actions the voice agent uses are also available to the agent in chat."
      >
        <div className="mb-3 flex flex-wrap gap-1.5 text-[12px]">
          <span className="pill" data-on={pcOn}>
            <MonitorPlay size={12} /> {pcOn ? "Enabled" : "Disabled"}
          </span>
          <span className="pill" data-on={!!status?.computer?.supports_input}>
            Input {status?.computer?.supports_input ? "ready" : "unavailable"}
          </span>
          <span className="pill" data-on={!!status?.computer?.supports_screenshot}>
            Screenshot {status?.computer?.supports_screenshot ? "ready" : "unavailable"}
          </span>
          {status?.computer?.os && <span className="pill">{status.computer.os}</span>}
        </div>

        <div className="flex flex-col gap-2">
          <Toggle
            on={pcOn}
            onChange={(v) => void put({ computer_control_enabled: v })}
            label="Let psd.ai drive this computer"
            desc="Off means every mouse, keyboard and window action is refused."
          />
          <Toggle
            on={confirmRisky}
            onChange={(v) => void put({ computer_control_confirm: v })}
            label="Ask before risky actions"
            desc="Prompts for approval before ending a process. Formatting, wiping and system files are always refused, whatever this says."
          />
        </div>

        {missing.length > 0 && (
          <div className="mt-3 rounded-xl p-3 text-[12px]" style={{ background: "var(--bg-sunken)", border: "1px solid var(--border)" }}>
            <div className="mb-1 font-medium">Optional packages not installed: {missing.join(", ")}</div>
            <p className="mb-2" style={{ color: "var(--muted)" }}>
              psd.ai still works without them using built-in fallbacks, but installing them makes screenshots, window
              control and process management better:
            </p>
            <pre
              className="selectable overflow-x-auto rounded-lg p-2 text-[11px]"
              style={{ background: "var(--code-bg)", color: "#c9cee0", fontFamily: "var(--font-mono)" }}
            >
              .\venv\Scripts\pip install -r requirements-jarvis.txt
            </pre>
          </div>
        )}

        <div className="mt-3 flex items-start gap-2 rounded-xl px-3 py-2 text-[12px]" style={{ background: "var(--bg-sunken)", border: "1px solid var(--border)" }}>
          <ShieldAlert size={14} className="mt-0.5 shrink-0" />
          <span style={{ color: "var(--muted)" }}>
            Hard limits, always on: no disk formatting, no system-file deletion, no boot-config changes, no shutting the
            PC down, and psd.ai can never end its own process or the desktop shell. Everything else it does is logged in
            the Talk screen.
          </span>
        </div>
      </Section>
    </>
  );
}
