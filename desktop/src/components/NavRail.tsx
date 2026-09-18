import {
  MessageSquare,
  HardDrive,
  StickyNote,
  ListTodo,
  CalendarDays,
  Brain,
  Images,
  Library,
  Telescope,
  Columns2,
  Mail,
  Mic,
  Settings,
  Plus,
  Search,
  Keyboard,
  Globe,
} from "lucide-react";
import { useApp, type AppView } from "../store/app";

const ITEMS: { id: AppView; icon: typeof MessageSquare; label: string; hint?: string }[] = [
  { id: "chat", icon: MessageSquare, label: "Chat" },
  { id: "browser", icon: Globe, label: "Live Browser" },
  { id: "talk", icon: Mic, label: "Talk to psd.ai" },
  { id: "models", icon: HardDrive, label: "Models" },
  { id: "notes", icon: StickyNote, label: "Notes" },
  { id: "tasks", icon: ListTodo, label: "Tasks" },
  { id: "calendar", icon: CalendarDays, label: "Calendar" },
  { id: "memory", icon: Brain, label: "Brain" },
  { id: "gallery", icon: Images, label: "Gallery" },
  { id: "library", icon: Library, label: "Library" },
  { id: "research", icon: Telescope, label: "Research" },
  { id: "compare", icon: Columns2, label: "Compare" },
  { id: "email", icon: Mail, label: "Email" },
];

export default function NavRail() {
  const view = useApp((s) => s.view);
  const setView = useApp((s) => s.setView);
  const newChat = useApp((s) => s.newChat);
  const setSettings = useApp((s) => s.setSettings);
  const setPalette = useApp((s) => s.setPalette);
  const setShortcuts = useApp((s) => s.setShortcuts);
  const streaming = useApp((s) => s.streaming);

  return (
    <nav
      className="relative z-20 flex h-full w-[56px] shrink-0 flex-col items-center gap-0.5 py-2"
      style={{ borderRight: "1px solid var(--border)", background: "color-mix(in oklab, var(--bg-elev) 70%, transparent)" }}
    >
      <button className="rail-btn mb-1" title="New chat" onClick={newChat} disabled={streaming} aria-label="New chat">
        <Plus size={18} />
      </button>
      <button className="rail-btn mb-2" title="Search (Ctrl+K)" onClick={() => setPalette(true)}>
        <Search size={16} />
      </button>
      <div className="mb-1 h-px w-7" style={{ background: "var(--border)" }} />
      {ITEMS.map((it) => {
        const Icon = it.icon;
        return (
          <button
            key={it.id}
            className="rail-btn"
            data-on={view === it.id}
            title={it.label}
            aria-label={it.label}
            aria-current={view === it.id ? "page" : undefined}
            onClick={() => setView(it.id)}
          >
            <Icon size={17} />
          </button>
        );
      })}
      <div className="flex-1" />
      <button className="rail-btn" title="Keyboard shortcuts (?)" onClick={() => setShortcuts(true)}>
        <Keyboard size={16} />
      </button>
      <button className="rail-btn" title="Settings" onClick={() => setSettings(true)}>
        <Settings size={17} />
      </button>
    </nav>
  );
}
