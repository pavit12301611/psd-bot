import { useEffect, useMemo, useState } from "react";
import { ChevronLeft, ChevronRight, Plus, Trash2, CalendarDays } from "lucide-react";
import { calendar as api, type CalEvent } from "../lib/api";
import { useApp } from "../store/app";
import { Empty, PanelHead } from "./media";
import { localYmd, parseDate } from "../lib/ui";

function startOfMonth(d: Date) {
  return new Date(d.getFullYear(), d.getMonth(), 1);
}
function addMonths(d: Date, n: number) {
  return new Date(d.getFullYear(), d.getMonth() + n, 1);
}
function iso(d: Date) {
  return d.toISOString();
}

export default function CalendarView() {
  const toast = useApp((s) => s.toast);
  const [cursor, setCursor] = useState(() => startOfMonth(new Date()));
  const [events, setEvents] = useState<CalEvent[]>([]);
  const [picked, setPicked] = useState<string | null>(null);
  const [draft, setDraft] = useState({ summary: "", time: "09:00", description: "" });
  const [editing, setEditing] = useState<CalEvent | null>(null);

  const range = useMemo(() => {
    const start = new Date(cursor.getFullYear(), cursor.getMonth(), 1);
    const end = new Date(cursor.getFullYear(), cursor.getMonth() + 1, 1);
    return { start: iso(start), end: iso(end) };
  }, [cursor]);

  const load = async () => {
    try {
      const r = await api.events(range.start, range.end);
      setEvents(r.events || []);
    } catch (e: any) {
      toast(e.message || "Could not load calendar", "error");
      setEvents([]);
    }
  };
  useEffect(() => {
    load();
  }, [range.start, range.end]);

  const cells = useMemo(() => {
    const first = startOfMonth(cursor);
    const offset = (first.getDay() + 6) % 7; // Monday-first
    const days: { date: Date; inMonth: boolean }[] = [];
    for (let i = 0; i < 42; i++) {
      const d = new Date(first);
      d.setDate(1 - offset + i);
      days.push({ date: d, inMonth: d.getMonth() === cursor.getMonth() });
    }
    return days;
  }, [cursor]);

  const byDay = (d: Date) => {
    const key = localYmd(d);
    return events.filter((e) => localYmd(parseDate(e.dtstart)) === key);
  };

  const create = async () => {
    if (!picked || !draft.summary.trim()) return;
    try {
      const dtstart = new Date(`${picked}T${draft.time}:00`);
      const dtend = new Date(dtstart.getTime() + 60 * 60 * 1000);
      await api.createEvent({ summary: draft.summary.trim(), dtstart: dtstart.toISOString(), dtend: dtend.toISOString(), description: draft.description });
      setDraft({ summary: "", time: "09:00", description: "" });
      setPicked(null);
      toast("Event added", "success");
      load();
    } catch (e: any) {
      toast(e.message || "Could not create event", "error");
    }
  };

  const today = localYmd(new Date());

  return (
    <section className="panel">
      <PanelHead icon={<CalendarDays size={16} />} title="Calendar">
        <button className="icon-btn h-8 w-8" onClick={() => setCursor((c) => addMonths(c, -1))}>
          <ChevronLeft size={16} />
        </button>
        <span className="min-w-[140px] text-center text-[13px] font-semibold">
          {cursor.toLocaleString(undefined, { month: "long", year: "numeric" })}
        </span>
        <button className="icon-btn h-8 w-8" onClick={() => setCursor((c) => addMonths(c, 1))}>
          <ChevronRight size={16} />
        </button>
        <button className="btn h-8" onClick={() => setCursor(startOfMonth(new Date()))}>
          Today
        </button>
      </PanelHead>
      <div className="panel-body">
        <div className="cal-grid mb-1 text-center text-[11px] font-semibold uppercase" style={{ color: "var(--muted)" }}>
          {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => (
            <div key={d}>{d}</div>
          ))}
        </div>
        <div className="cal-grid">
          {cells.map(({ date, inMonth }) => {
            const key = localYmd(date);
            const dayEvents = byDay(date);
            return (
              <button
                key={key}
                className="cal-cell"
                data-today={key === today}
                data-muted={!inMonth}
                onClick={() => {
                  setPicked(key);
                  setEditing(null);
                }}
              >
                <div className="mb-1 text-[12px] font-semibold">{date.getDate()}</div>
                <div className="flex flex-col gap-0.5">
                  {dayEvents.slice(0, 3).map((e) => (
                    <span
                      key={e.uid || e.id}
                      className="truncate rounded-md px-1.5 py-0.5 text-[11px]"
                      style={{ background: "var(--accent-soft)", color: "var(--accent)" }}
                      onClick={(ev) => {
                        ev.stopPropagation();
                        setEditing(e);
                        setPicked(null);
                      }}
                    >
                      {e.summary}
                    </span>
                  ))}
                  {dayEvents.length > 3 && <span className="text-[10px]" style={{ color: "var(--muted)" }}>+{dayEvents.length - 3}</span>}
                </div>
              </button>
            );
          })}
        </div>

        {(picked || editing) && (
          <div className="card mt-4 max-w-lg">
            {editing ? (
              <>
                <div className="mb-2 flex items-center justify-between">
                  <h3 className="font-semibold">{editing.summary}</h3>
                  <button
                    className="icon-btn hover:!text-red-400"
                    onClick={async () => {
                      await api.deleteEvent(editing.uid || editing.id || "");
                      setEditing(null);
                      load();
                    }}
                  >
                    <Trash2 size={15} />
                  </button>
                </div>
                <p className="text-[13px]" style={{ color: "var(--muted)" }}>
                  {parseDate(editing.dtstart).toLocaleString()}
                  {editing.location ? ` · ${editing.location}` : ""}
                </p>
                {editing.description && <p className="mt-2 text-[13px]">{editing.description}</p>}
                <button className="btn mt-3" onClick={() => setEditing(null)}>
                  Close
                </button>
              </>
            ) : (
              <>
                <h3 className="mb-2 font-semibold">New event · {picked}</h3>
                <div className="flex flex-col gap-2">
                  <input className="input" placeholder="What’s happening?" value={draft.summary} onChange={(e) => setDraft({ ...draft, summary: e.target.value })} />
                  <input className="input w-36" type="time" value={draft.time} onChange={(e) => setDraft({ ...draft, time: e.target.value })} />
                  <textarea className="input min-h-[70px] resize-y" placeholder="Notes (optional)" value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
                  <div className="flex gap-2">
                    <button className="btn btn-primary" disabled={!draft.summary.trim()} onClick={create}>
                      <Plus size={14} /> Add event
                    </button>
                    <button className="btn" onClick={() => setPicked(null)}>
                      Cancel
                    </button>
                  </div>
                </div>
              </>
            )}
          </div>
        )}
        {events.length === 0 && !picked && (
          <Empty icon={<CalendarDays size={28} />} title="Nothing on the calendar this month" hint="Click a day to add an event." />
        )}
      </div>
    </section>
  );
}
