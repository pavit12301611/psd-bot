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
      const r = await api.list({ filter, limit: 50 });
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
    if (accounts.length) loadList();
  }, [filter, accounts.length]);

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
          hint="Connect IMAP/SMTP under Settings → Account, or add an account via the API. Once connected, your inbox shows up here."
          action={<button className="btn btn-primary mt-2" onClick={() => setSettings(true)}>Open settings</button>}
        />
      ) : (
        <div className="flex min-h-0 flex-1">
          <div className="w-[320px] shrink-0 overflow-y-auto" style={{ borderRight: "1px solid var(--border)" }}>
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
                  <button className="icon-btn hover:!text-red-400" title="Delete" onClick={async () => { await api.remove(active.uid); setActive(null); loadList(); }}>
                    <Trash2 size={15} />
                  </button>
                </div>
                <div className="selectable flex-1 overflow-auto whitespace-pre-wrap rounded-2xl p-4 text-[14px] leading-relaxed" style={{ background: "var(--bg-sunken)" }}>
                  {active.body || active.text || active.html || active.preview || ""}
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
