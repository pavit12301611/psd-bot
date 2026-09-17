import { useMemo, useState } from "react";
import { Columns2, Play, Trophy } from "lucide-react";
import { compare as api, sendChat } from "../lib/api";
import { useApp } from "../store/app";
import { Empty, PanelHead } from "./media";

interface Pane {
  label: string;
  model: string;
  content: string;
  error?: string;
  streaming: boolean;
}

export default function Compare() {
  const toast = useApp((s) => s.toast);
  const items = useApp((s) => s.modelItems);
  const setCompareBusy = useApp((s) => s.setCompareBusy);
  const streaming = useApp((s) => s.streaming);
  const models = useMemo(
    () =>
      items.flatMap((i) =>
        [...i.models, ...(i.models_extra || [])].map((m) => ({
          model: m,
          endpoint_id: i.endpoint_id || "",
          endpoint_url: i.url,
          name: `${m} · ${i.endpoint_name || i.url}`,
        })),
      ),
    [items],
  );
  const [a, setA] = useState(0);
  const [b, setB] = useState(Math.min(1, models.length - 1));
  const [prompt, setPrompt] = useState("");
  const [panes, setPanes] = useState<[Pane, Pane] | null>(null);
  const [compId, setCompId] = useState<string | null>(null);
  const [voted, setVoted] = useState<string | null>(null);

  const run = async () => {
    if (!prompt.trim() || models.length < 2) return;
    if (streaming) {
      toast("Wait for the chat reply to finish", "info");
      return;
    }
    const left = models[a];
    const right = models[b];
    if (!left || !right) return;
    if (left.model === right.model && left.endpoint_url === right.endpoint_url) {
      toast("Pick two different models", "info");
      return;
    }
    setCompareBusy(true);
    try {
      const r = await api.start({
        prompt: prompt.trim(),
        model_a: left.model,
        model_b: right.model,
        endpoint_a: left.endpoint_url,
        endpoint_b: right.endpoint_url,
        endpoint_a_id: left.endpoint_id,
        endpoint_b_id: right.endpoint_id,
        is_blind: false,
      });
      setCompId(r.id);
      setVoted(null);
      const p: [Pane, Pane] = [
        { label: left.model.split("/").pop() || left.model, model: left.model, content: "", streaming: true },
        { label: right.model.split("/").pop() || right.model, model: right.model, content: "", streaming: true },
      ];
      setPanes(p);
      const sessions = [r.session_left, r.session_right];
      const routes = [left, right];
      sessions.forEach((sid, idx) => {
        if (!sid) return;
        sendChat(
          {
            sessionId: sid,
            message: prompt.trim(),
            mode: "chat",
            web: false,
            bash: false,
            model: routes[idx].model,
            endpointId: routes[idx].endpoint_id,
            endpointUrl: routes[idx].endpoint_url,
          },
          {
            onEvent: (ev) => {
              if (ev.delta && !ev.thinking) {
                setPanes((cur) => {
                  if (!cur) return cur;
                  const next = [...cur] as [Pane, Pane];
                  next[idx] = { ...next[idx], content: next[idx].content + ev.delta };
                  return next;
                });
              }
              if (ev.error) {
                setPanes((cur) => {
                  if (!cur) return cur;
                  const next = [...cur] as [Pane, Pane];
                  next[idx] = { ...next[idx], error: String(ev.error), streaming: false };
                  return next;
                });
              }
            },
            onError: (msg) => {
              setPanes((cur) => {
                if (!cur) return cur;
                const next = [...cur] as [Pane, Pane];
                next[idx] = { ...next[idx], error: msg, streaming: false };
                return next;
              });
            },
            onDone: () => {
              setPanes((cur) => {
                if (!cur) return cur;
                const next = [...cur] as [Pane, Pane];
                next[idx] = { ...next[idx], streaming: false };
                if (!next[0].streaming && !next[1].streaming) useApp.getState().setCompareBusy(false);
                return next;
              });
            },
          },
        );
      });
    } catch (e: any) {
      setCompareBusy(false);
      toast(e.message || "Could not start comparison", "error");
    }
  };

  const vote = async (winner: "left" | "right" | "tie") => {
    if (!compId) return;
    try {
      await api.vote(compId, winner);
      setVoted(winner);
      toast("Vote recorded", "success");
    } catch (e: any) {
      toast(e.message || "Vote failed", "error");
    }
  };

  return (
    <section className="panel">
      <PanelHead icon={<Columns2 size={16} />} title="Compare models">
        <button className="btn btn-primary h-8" disabled={!prompt.trim() || models.length < 2} onClick={run}>
          <Play size={14} /> Run
        </button>
      </PanelHead>
      <div className="panel-body flex flex-col gap-3">
        {models.length < 2 ? (
          <Empty icon={<Columns2 size={36} />} title="Need at least two models" hint="Add another endpoint in Settings, then pick two models to compare." />
        ) : (
          <>
            <div className="grid grid-cols-2 gap-3">
              <select className="input" value={a} onChange={(e) => setA(Number(e.target.value))}>
                {models.map((m, i) => (
                  <option key={m.name + i} value={i}>{m.name}</option>
                ))}
              </select>
              <select className="input" value={b} onChange={(e) => setB(Number(e.target.value))}>
                {models.map((m, i) => (
                  <option key={m.name + i} value={i}>{m.name}</option>
                ))}
              </select>
            </div>
            <textarea className="input min-h-[80px] resize-y" placeholder="Prompt both models with the same question…" value={prompt} onChange={(e) => setPrompt(e.target.value)} />
            {!panes && <Empty icon={<Columns2 size={28} />} title="Ready to compare" hint="Pick two models, write a prompt, and hit Run." />}
            {panes && (
              <>
                <div className="grid min-h-0 flex-1 grid-cols-2 gap-3">
                  {panes.map((p, i) => (
                    <div key={i} className="card flex min-h-[240px] flex-col">
                      <div className="mb-2 flex items-center justify-between text-[12px] font-semibold" style={{ color: "var(--muted)" }}>
                        {p.label}
                        {p.streaming && <span className="dots"><span /><span /><span /></span>}
                      </div>
                      <div className="selectable flex-1 overflow-auto whitespace-pre-wrap text-[13.5px] leading-relaxed">
                        {p.content || (p.streaming ? "" : "—")}
                      </div>
                      {p.error && <div className="mt-2 text-[12px] text-red-400">{p.error}</div>}
                    </div>
                  ))}
                </div>
                {!panes[0].streaming && !panes[1].streaming && (
                  <div className="flex items-center justify-center gap-2">
                    <button className="btn" disabled={!!voted} onClick={() => vote("left")}>
                      <Trophy size={14} /> Left wins
                    </button>
                    <button className="btn" disabled={!!voted} onClick={() => vote("tie")}>
                      Tie
                    </button>
                    <button className="btn" disabled={!!voted} onClick={() => vote("right")}>
                      Right wins <Trophy size={14} />
                    </button>
                    {voted && <span className="text-[12px]" style={{ color: "var(--muted)" }}>Voted {voted}</span>}
                  </div>
                )}
              </>
            )}
          </>
        )}
      </div>
    </section>
  );
}
