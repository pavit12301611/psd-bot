import { useEffect, useState } from "react";
import { Mail, RefreshCw, Send, Trash2, MailOpen } from "lucide-react";
import { email as api, type EmailAccount, type EmailMsg } from "../lib/api";
import { useApp } from "../store/app";
import { Empty, PanelHead } from "./media";

export default function Email() {
  const toast = useApp((s) => s.toast);
  const setSettings = useApp((s) => s.setSettings);
  const [accounts, setAccounts] = useState<EmailAccount[]>([]);
  const [list, setList] = useState<EmailMsg[]>([]);
  const [active, setActive] = useState<any>(null);
  const [filter, setFilter] = useState("all");
  const [folder, setFolder] = useState("INBOX");
  const [folders, setFolders] = useState<string[]>(["INBOX"]);
  const [compose, setCompose] = useState(false);
  const [draft, setDraft] = useState({ to: "", subject: "", body: "" });

  const loadAccounts = async () => {
    try {
      const r = await api.accounts();
      setAccounts(r.accounts || []);
    } catch {
      setAccounts([]);
    }
  };
  const loadList = async () => {
    try {
      const r = await api.list({ filter, folder, limit: 50 });
      setList(r.emails || []);
    } catch (e: any) {
      toast(e.message || "Could not load inbox", "error");
      setList([]);
    }
  };
  useEffect(() => {
    loadAccounts();
  }, []);
  useEffect(() => {
    if (accounts.length) {
      loadList();
      api.folders().then((r) => {
        const names = (r.folders || []).map((f: any) => (typeof f === "string" ? f : f.name || f.id)).filter(Boolean);
        if (names.length) setFolders(names);
      }).catch(() => {});
    }
  }, [filter, folder, accounts.length]);

  const open = async (m: EmailMsg) => {
    try {
      const full = await api.read(m.uid);
      setActive(full);
      setCompose(false);
      if (!m.seen) api.markRead(m.uid).catch(() => {});
    } catch (e: any) {
      toast(e.message || "Could not open message", "error");
    }
  };

  const send = async () => {
    if (!draft.to.trim() || !draft.subject.trim()) return;
    try {
      await api.send(draft);
      toast("Queued for send", "success");
      setDraft({ to: "", subject: "", body: "" });
      setCompose(false);
    } catch (e: any) {
      toast(e.message || "Send failed", "error");
    }
  };

  return (
    <section className="panel">
      <PanelHead icon={<Mail size={16} />} title="Email">
        <div className="flex rounded-full p-0.5" style={{ background: "var(--bg-sunken)", border: "1px solid var(--border)" }}>
          {["all", "unread"].map((f) => (
            <button key={f} className="rounded-full px-3 py-1 text-xs font-medium capitalize" style={{ background: filter === f ? "var(--accent-soft)" : "transparent", color: filter === f ? "var(--accent)" : "var(--muted)" }} onClick={() => setFilter(f)}>
              {f}
            </button>
          ))}
        </div>
        <select className="input h-8 w-36 text-[12px]" value={folder} onChange={(e) => setFolder(e.target.value)}>
          {folders.map((f) => (
            <option key={f} value={f}>{f}</option>
          ))}
        </select>
        <button className="icon-btn h-8 w-8" onClick={loadList}>
          <RefreshCw size={14} />
        </button>
        <button className="btn btn-primary h-8" onClick={() => { setCompose(true); setActive(null); }}>
          <Send size={14} /> Compose
        </button>
      </PanelHead>
      {accounts.length === 0 ? (
        <Empty
          icon={<Mail size={36} />}
          title="No email account"
          hint="Connect IMAP/SMTP under Settings → Email. Once connected, your inbox shows up here."
          action={<button className="btn btn-primary mt-2" onClick={() => setSettings(true, "email")}>Open settings</button>}
        />
      ) : (
        <div className="flex min-h-0 flex-1">
          <div className="split-side overflow-y-auto" style={{ borderRight: "1px solid var(--border)" }}>
            {list.length === 0 && <Empty icon={<MailOpen size={28} />} title="Inbox empty" />}
            {list.map((m) => (
              <button key={m.uid} className="flex w-full flex-col gap-0.5 px-4 py-3 text-left" style={{ background: active?.uid === m.uid ? "var(--accent-soft)" : "transparent", borderBottom: "1px solid var(--border)" }} onClick={() => open(m)}>
                <div className="flex items-center gap-2">
                  {!m.seen && <span className="h-1.5 w-1.5 rounded-full" style={{ background: "var(--accent)" }} />}
                  <span className="truncate text-[13px] font-medium">{m.from_name || m.from || "Unknown"}</span>
                  <span className="ml-auto shrink-0 text-[11px]" style={{ color: "var(--muted)" }}>{m.date ? new Date(m.date).toLocaleDateString() : ""}</span>
                </div>
                <span className="truncate text-[13px]">{m.subject || "(no subject)"}</span>
                <span className="truncate text-[12px]" style={{ color: "var(--muted)" }}>{m.snippet || m.preview || ""}</span>
              </button>
            ))}
          </div>
          <div className="flex min-w-0 flex-1 flex-col p-5">
            {compose ? (
              <div className="flex flex-col gap-2">
                <input className="input" placeholder="To" value={draft.to} onChange={(e) => setDraft({ ...draft, to: e.target.value })} />
                <input className="input" placeholder="Subject" value={draft.subject} onChange={(e) => setDraft({ ...draft, subject: e.target.value })} />
                <textarea className="input min-h-[240px] resize-y" placeholder="Write your message…" value={draft.body} onChange={(e) => setDraft({ ...draft, body: e.target.value })} />
                <div className="flex gap-2">
                  <button className="btn btn-primary" disabled={!draft.to.trim() || !draft.subject.trim()} onClick={send}>
                    <Send size={14} /> Send
                  </button>
                  <button className="btn" onClick={() => setCompose(false)}>Cancel</button>
                </div>
              </div>
            ) : !active ? (
              <Empty icon={<MailOpen size={36} />} title="Select a message" />
            ) : (
              <>
                <div className="mb-3 flex items-start gap-2">
                  <div className="min-w-0 flex-1">
                    <h3 className="text-[16px] font-semibold">{active.subject || "(no subject)"}</h3>
                    <p className="text-[13px]" style={{ color: "var(--muted)" }}>{active.from || active.from_name} · {active.date ? new Date(active.date).toLocaleString() : ""}</p>
                  </div>
                  <button className="btn h-8" onClick={() => {
                    setCompose(true);
                    setDraft({
                      to: active.from || "",
                      subject: (active.subject || "").startsWith("Re:") ? active.subject : `Re: ${active.subject || ""}`,
                      body: `\n\n---\n${active.body || active.text || ""}`,
                    });
                  }}>Reply</button>
                  <button className="icon-btn hover:!text-red-400" title="Delete" onClick={async () => { if (!window.confirm("Delete this message?")) return; await api.remove(active.uid, folder); setActive(null); loadList(); }}>
                    <Trash2 size={15} />
                  </button>
                </div>
                {Array.isArray(active.attachments) && active.attachments.length > 0 && (
                  <div className="mb-2 flex flex-wrap gap-1 text-[12px]" style={{ color: "var(--muted)" }}>
                    {active.attachments.map((a: any, i: number) => (
                      <span key={i} className="pill">{a.filename || a.name || `file ${i + 1}`}</span>
                    ))}
                  </div>
                )}
                {active.html ? (
                  <iframe title="email" className="min-h-[240px] w-full flex-1 rounded-2xl" sandbox="allow-same-origin" srcDoc={active.html} />
                ) : (
                  <div className="selectable flex-1 overflow-auto whitespace-pre-wrap rounded-2xl p-4 text-[14px] leading-relaxed" style={{ background: "var(--bg-sunken)" }}>
                    {active.body || active.text || active.preview || ""}
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
