"""psd_tui.py — the psd.ai terminal interface.

A full-screen Textual UI for psd.ai that replaces the localhost browser window.
It never re-implements psd.ai logic: it drives the exact same FastAPI backend
over loopback HTTP (attached mode), or drives the ``app`` object **in-process**
with no TCP server, no browser, and no listening port (embedded mode).

Run modes
---------
* ``python psd_tui.py``             — attach to a server already running on
  ``--host --port`` (default 127.0.0.1:7000) and sign in.
* ``python psd_tui.py --embedded`` — import and run the FastAPI app in-process.
  First run walks you through DB init + admin setup, exactly like ``setup.py``
  plus the web first-run flow. Nothing is ever put on a port.

Why this is safe
----------------
The TUI reuses the backend's own auth (session tokens minted by the service's
``AuthManager``), its owner-scoped session/history routes, and its streaming
``/api/chat_stream`` pipeline. Chat history, files, memory, and settings keep
living in the same DATA_DIR the web UI uses, so a chat started in a browser
keeps going in the terminal and vice-versa.

Keys
----
* ``i`` or ``/`` — jump to the prompt.
* ``e``        — toggle agent mode (tools / bash-on for the next message).
* ``n``        — new chat thread.
* ``m``        — open the model picker.
* ``R``        — refresh models.
* ``r``        — rename current thread.
* ``Ctrl+X``   — delete current thread.
* ``Ctrl+L``   — clear the rendered view (history on disk is untouched).
* ``q``        — quit.

Environment
-----------
* ``PSD_TUI_USER`` / ``PSD_TUI_PASSWORD`` — pre-supply credentials (ignored for
  embedded first-run admin setup, which reads ``PSD_AI_ADMIN_*`` or prompts).
* ``PSD_TUI_SKIP_DOCS=1``                 — skip the first-run feature notice.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from contextlib import suppress
from typing import Any, Dict, List, Optional

# Make sure the repository root (where app.py/core/src live) is importable when
# this script is invoked by absolute/relative path from anywhere.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# A bare `python psd_tui.py` on a machine where the server was never started
# will offer to boot the app in-process instead of erroring out.
AUTO_EMBED_ON_FAIL = True
_VERSION = "1.0.0"

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Container, Horizontal, Vertical, VerticalScroll
    from textual.css.query import NoMatches
    from textual.screen import ModalScreen
    from textual.widgets import (
        Button,
        Footer,
        Header,
        Input,
        Label,
        ListItem,
        ListView,
        Markdown,
        Static,
    )
    HAS_TEXTUAL = True
except Exception:  # pragma: no cover - environment guard
    HAS_TEXTUAL = False


# --------------------------------------------------------------------------- #
# Feature notice (first run)
# --------------------------------------------------------------------------- #
FEATURES_TEXT = """\
# ⛵ Welcome to **psd.ai** — terminal edition

This window **replaces the localhost website**. Nothing is lost: it talks to
the same psd.ai backend (your chats, history, files, memory, and settings stay
in one place) — it is just drawn in your terminal instead of a browser.

**How to drive it**

Type a message and press **Enter** to send. While you're typing, every key is
text. Press **Esc** to switch to *commands*, then:

| Key        | Does                            |
| ---------- | ------------------------------- |
| `q`        | quit                            |
| `i` / `/`  | back to typing                  |
| `e`        | toggle **agent mode** (tools)   |
| `n`        | new chat thread                 |
| `m`        | open the **model picker**       |
| `R`        | refresh models                  |
| `r`        | rename thread                   |
| `Ctrl+X`   | delete thread                   |
| `Ctrl+L`   | clear view                      |

Markdown, tables, and code blocks render in full colour.

> Tip: run with `--embedded` to skip the localhost server entirely and boot
> psd.ai straight inside this terminal (no HTTP, no browser, no port).
"""


def _short_id(value: str, width: int = 8) -> str:
    return (value or "")[:width]


# --------------------------------------------------------------------------- #
# Widgets
# --------------------------------------------------------------------------- #
class StatusMini(Static):
    """One-line status bar below the prompt: model, mode, thread, connection."""


class ModelPickerScreen(ModalScreen):
    """Modal model picker: flatten /api/models into a searchable list."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("up", "cursor_up", "Prev", show=False),
        Binding("down", "cursor_down", "Next", show=False),
    ]

    def __init__(self, items: List[Dict[str, Any]]) -> None:
        super().__init__()
        self._items = items
        self._matches: List[Dict[str, Any]] = []
        self._selected: Optional[Dict[str, Any]] = None

    @property
    def selected(self) -> Optional[Dict[str, Any]]:
        return self._selected

    def compose(self) -> ComposeResult:
        with Container(id="picker-root"):
            yield Static(
                "Model picker — type to search, ↑/↓ choose, Enter use, Esc close",
                classes="picker-title",
            )
            yield Input(placeholder="search models…", id="picker-search")
            yield VerticalScroll(ListView(id="picker-list", initial_index=None),
                                 id="picker-scroll")
            yield Static("", id="picker-detail")

    def on_mount(self) -> None:
        self._matches = list(self._items)
        self._render_list()

    def _render_list(self) -> None:
        lv = self.query_one("#picker-list", ListView)
        lv.clear()
        if not self._matches:
            lv.append(ListItem(Label("(no matching models)")))
            self.query_one("#picker-detail", Static).update("")
            return
        for item in self._matches:
            ep_name = item.get("endpoint_name") or item.get("endpoint_url") or "endpoint"
            model = item.get("model") or "(no model)"
            lv.append(ListItem(Label(f"{model}   ◆   {ep_name}")))
        lv.index = 0
        self._show_detail(0)

    def _highlight_index(self) -> int:
        lv = self.query_one("#picker-list", ListView)
        return lv.index if lv.index is not None else 0

    def _show_detail(self, index: int) -> None:
        detail = self.query_one("#picker-detail", Static)
        if 0 <= index < len(self._matches):
            item = self._matches[index]
            detail.update(
                f"endpoint: {item.get('endpoint_name') or item.get('endpoint_url')}\n"
                f"model:    {item.get('model')}\n"
                f"category: {item.get('category') or '-'}"
            )
        else:
            detail.update("")

    def _cursor_move(self, delta: int) -> None:
        if not self._matches:
            return
        lv = self.query_one("#picker-list", ListView)
        lo, hi = 0, len(self._matches) - 1
        idx = self._highlight_index()
        self._show_detail(idx)
        new_idx = max(lo, min(hi, idx + delta))
        lv.index = new_idx
        self._show_detail(new_idx)

    def action_cursor_up(self) -> None:
        self._cursor_move(-1)

    def action_cursor_down(self) -> None:
        self._cursor_move(1)

    def on_input_changed(self, event: Input.Changed) -> None:
        q = event.value.strip().lower()
        if not q:
            self._matches = list(self._items)
        else:
            self._matches = [
                it for it in self._items
                if q in (it.get("model") or "").lower()
                or q in (it.get("endpoint_name") or "").lower()
            ]
        self._render_list()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "picker-search":
            self.action_select()

    def action_select(self) -> None:
        if not self._matches:
            self.dismiss(None)
            return
        idx = self._highlight_index()
        if 0 <= idx < len(self._matches):
            self._selected = self._matches[idx]
            self.dismiss(self._selected)
        else:
            self.dismiss(None)

    def action_dismiss(self) -> None:
        self.dismiss(None)



class ConfirmScreen(ModalScreen):
    """Yes/no confirmation dialog."""

    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    def __init__(self, text: str, danger: bool = False) -> None:
        super().__init__()
        self.text = text
        self.danger = danger

    def compose(self) -> ComposeResult:
        with Container(id="confirm-root"):
            yield Static(self.text, id="confirm-text")
            with Horizontal(id="confirm-actions"):
                yield Button("Yes", id="confirm-yes",
                             variant="error" if self.danger else "primary")
                yield Button("No", id="confirm-no", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")


class TextModal(ModalScreen):
    """Tiny modal capturing a single line of text (used for rename)."""

    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    def __init__(self, title: str, initial: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.initial = initial

    def compose(self) -> ComposeResult:
        with Container(id="confirm-root"):
            yield Static(self.title_text, id="confirm-text")
            yield Input(value=self.initial, id="text-modal-input")
            with Horizontal(id="confirm-actions"):
                yield Button("OK", id="confirm-yes", variant="primary")
                yield Button("Cancel", id="confirm-no", variant="default")

    def on_mount(self) -> None:
        self.query_one("#text-modal-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "text-modal-input":
            self.dismiss((event.value or "").strip() or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-yes":
            self.dismiss((self.query_one("#text-modal-input", Input).value or "").strip() or None)
        else:
            self.dismiss(None)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
class PsdTuiApp(App):
    """Terminal front-end for psd.ai."""

    CSS = """
    Screen {
        background: $background;
    }
    #chat-scroll {
        height: 1fr;
        overflow-y: auto;
        padding: 1 2;
    }
    .meta {
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
    }
    .user-bubble {
        margin-bottom: 1;
    }
    .assistant-bubble {
        margin-bottom: 1;
    }
    .assistant-bubble.markdown {
        background: transparent;
    }
    #prompt-row {
        dock: bottom;
        height: 3;
        padding: 0 1 1 1;
    }
    #prompt {
        height: 3;
        border: round $primary;
    }
    #statusbar {
        dock: bottom;
        height: 1;
        color: $text-muted;
        background: $panel;
        padding: 0 1;
    }
    #picker-root {
        width: 92%;
        height: 80%;
        max-height: 44;
        align: center middle;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }
    .picker-title {
        color: $accent;
        margin-bottom: 1;
    }
    #picker-scroll {
        height: 1fr;
        margin-top: 1;
        margin-bottom: 1;
    }
    #picker-detail {
        height: 3;
        color: $text-muted;
    }
    #confirm-root {
        width: 76%;
        height: auto;
        align: center middle;
        background: $surface;
        border: thick $primary;
        padding: 2 3;
    }
    #confirm-text {
        margin-bottom: 2;
    }
    #confirm-actions {
        height: 3;
        align: right middle;
    }
    #confirm-actions Button {
        margin-left: 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("i", "focus_prompt", "Prompt", show=False),
        Binding("/", "focus_prompt", "Prompt", show=False),
        Binding("escape", "command_mode", "Commands", show=False),
        Binding("e", "toggle_agent", "Agent mode"),
        Binding("m", "picker", "Models"),
        Binding("R", "refresh_models", "Refresh", show=False),
        Binding("n", "new_thread", "New chat"),
        Binding("r", "rename_thread", "Rename"),
        Binding("ctrl+x", "delete_thread", "Delete chat"),
        Binding("ctrl+l", "clear_view", "Clear view"),
    ]
    # Two-mode input, like vim: while the prompt Input is focused every
    # printable key is text (Type mode); Escape moves focus to the chat scroll
    # (Command mode) where q/n/m/r/e/R fire.  i or / returns to Type mode.

    def __init__(
        self,
        client: Any,
        *,
        username: str = "",
        base_url: str = "",
        embedded: bool = False,
    ) -> None:
        super().__init__()
        self.client = client
        self.username = username
        self.base_url = base_url
        self.embedded = embedded
        self.sessions: List[Dict[str, Any]] = []
        self.current: Optional[Dict[str, Any]] = None
        self.models: List[Dict[str, Any]] = []  # flattened picker items
        self.agent_mode = False
        self._quitting = False
        self._docs_shown = False

    # ------------------------------------------------------------------ #
    # Compose / mount
    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield VerticalScroll(id="chat-scroll")
        with Vertical(id="prompt-row"):
            yield Input(placeholder="Message psd.ai…  (Enter to send)",
                        id="prompt")
        yield StatusMini("", id="statusbar")
        yield Footer()

    async def on_mount(self) -> None:
        self.title = "psd.ai — terminal"
        self.query_one("#prompt", Input).focus()
        self._set_mode("type")
        self._refresh_status()
        await self._load_sessions()
        if not self.sessions:
            await self._open_default_or_create()
        if not self.current and self.sessions:
            # Resume the most recently used thread (list is server-sorted).
            self.current = dict(self.sessions[0])
        if self.current:
            await self._select_thread(self.current["id"])
        else:
            await self._render_note(
                "No chat threads yet and no model endpoint is configured.\n\n"
                "Press **m** to open the model picker once a model endpoint is set "
                "up, or ask an admin to add one under Settings → Models. Model "
                "endpoints can also be configured here with **R** (refresh) once "
                "the backend has one."
            )
        if os.getenv("PSD_TUI_SKIP_DOCS", "0") not in ("1", "true"):
            await self._render_note(FEATURES_TEXT, once_key="docs")
        self._refresh_status()

    # ------------------------------------------------------------------ #
    # Data helpers
    # ------------------------------------------------------------------ #
    async def _load_sessions(self) -> None:
        try:
            self.sessions = list(await self.client.sessions())
        except Exception as exc:
            self._update_status(f"sessions failed: {exc}")

    async def _load_models(self, refresh: bool = False) -> None:
        try:
            data = await self.client.models(refresh=refresh)
            items: List[Dict[str, Any]] = []
            for host in (data or {}).get("items", []):
                endpoint_id = host.get("endpoint_id") or ""
                endpoint_name = host.get("endpoint_name") or host.get("url") or ""
                models = list(host.get("models") or []) + list(host.get("models_extra") or [])
                if not models and host.get("offline"):
                    models = ["(offline)"]
                for model in models:
                    items.append({
                        "model": model,
                        "endpoint_id": endpoint_id,
                        "endpoint_url": host.get("url") or "",
                        "endpoint_name": endpoint_name,
                        "category": host.get("category") or "",
                    })
            self.models = items
        except Exception as exc:
            self._update_status(f"models failed: {exc}")

    async def _open_default_or_create(self) -> None:
        dflt: Dict[str, Any] = {}
        try:
            dflt = await self.client.default_chat()
        except Exception:
            dflt = {}
        model = dflt.get("model") or ""
        endpoint_url = dflt.get("endpoint_url") or ""
        endpoint_id = dflt.get("endpoint_id") or ""
        if not (model and endpoint_url):
            await self._load_models()
            first = next(
                (m for m in self.models if m.get("model") and m.get("endpoint_url")),
                None,
            )
            if first:
                model, endpoint_url, endpoint_id = (
                    first["model"], first["endpoint_url"], first.get("endpoint_id") or "")
        if not (model and endpoint_url):
            return
        try:
            session = await self.client.create_session(
                name="New chat",
                model=model,
                endpoint_url=endpoint_url,
                endpoint_id=endpoint_id,
            )
            await self._load_sessions()
            if session.get("id"):
                self.current = {"id": session["id"], "name": "New chat",
                                "model": model, "endpoint_url": endpoint_url}
        except Exception as exc:
            self._update_status(f"new chat failed: {exc}")

    # ------------------------------------------------------------------ #
    # Rendering helpers
    # ------------------------------------------------------------------ #
    def _chat_scroll(self) -> VerticalScroll:
        return self.query_one("#chat-scroll", VerticalScroll)

    def _update_status(self, text: str) -> None:
        with suppress(NoMatches):
            self.query_one("#statusbar", StatusMini).update(text)

    def _status_line(self) -> str:
        if self.current:
            model = self.current.get("model") or "?"
            name = self.current.get("name") or "(untitled)"
            sid = _short_id(self.current.get("id", ""))
            mode = "agent" if self.agent_mode else "chat"
            conn = "embedded" if self.embedded else self.base_url
            who = self.username or "(anon)"
            return f"{who} · {name} · {model} · {mode} · {sid} · {conn}"
        conn = "embedded" if self.embedded else self.base_url
        return f"{self.username or '(anon)'} · no thread · {conn}"

    def _refresh_status(self) -> None:
        self._update_status(self._status_line())

    async def _render_note(self, text: str, once_key: Optional[str] = None) -> None:
        if once_key == "docs" and self._docs_shown:
            return
        if once_key == "docs":
            self._docs_shown = True
        await self._chat_scroll().mount(Markdown(text))
        self._scroll_to_bottom()

    async def _render_turn(self, role: str, content: str) -> None:
        """Render one finished history turn, appending at the bottom."""
        scroll = self._chat_scroll()
        content = content or ""
        label = "You" if role == "user" else (self.current.get("model") or "psd.ai")
        header = f"[{label} — {role}]"
        await scroll.mount(Static(header, classes="meta"))
        if content.strip():
            await scroll.mount(Markdown(content, classes="assistant-bubble" if role != "user" else "user-bubble"))
        else:
            await scroll.mount(Static("", classes="meta"))

    def _scroll_to_bottom(self) -> None:
        with suppress(NoMatches):
            self._chat_scroll().scroll_end(animate=False)

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #
    def action_focus_prompt(self) -> None:
        self._set_mode("type")
        self.query_one("#prompt", Input).focus()

    def action_command_mode(self) -> None:
        """Leave the prompt so command keys (q/n/m/… ) become active."""
        self._set_mode("command")
        self._chat_scroll().focus()

    def _set_mode(self, mode: str) -> None:
        prompt = self.query_one("#prompt", Input)
        if mode == "type":
            prompt.placeholder = "Message psd.ai…  (Enter to send; Esc for commands)"
            self.sub_title = "type"
        else:
            prompt.placeholder = "Press i or / to type…  (Esc for commands)"
            self.sub_title = "commands"
        self._refresh_status()

    def action_toggle_agent(self) -> None:
        self.agent_mode = not self.agent_mode
        self._refresh_status()
        self.notify(
            "Agent mode ON — tools enabled" if self.agent_mode else "Chat mode — tools off",
            timeout=2,
        )

    def action_clear_view(self) -> None:
        self._chat_scroll().remove_children()
        self._refresh_status()

    def action_refresh_models(self) -> None:
        async def _refresh() -> None:
            await self._load_models(refresh=True)
            self.notify(f"{len(self.models)} models loaded", timeout=2)
        self.run_worker(_refresh(), exclusive=True)

    def action_picker(self) -> None:
        async def _open() -> None:
            if not self.models:
                await self._load_models()
            self.push_screen(
                ModelPickerScreen(self.models), self._on_model_picked
            )
        self.run_worker(_open(), exclusive=True)

    async def _on_model_picked(self, item: Optional[Dict[str, Any]]) -> None:
        if not item or not item.get("model") or not item.get("endpoint_url"):
            self.notify("Nothing selected.", timeout=2)
            return
        model = item["model"]
        endpoint_url = item["endpoint_url"]
        endpoint_id = item.get("endpoint_id") or ""
        if model == "(offline)":
            self.notify("That endpoint is offline — pick another.", severity="warning")
            return
        if not self.current:
            try:
                session = await self.client.create_session(
                    name="New chat", model=model, endpoint_url=endpoint_url,
                    endpoint_id=endpoint_id,
                )
                await self._load_sessions()
                if session.get("id"):
                    self.current = {"id": session["id"], "name": "New chat",
                                    "model": model, "endpoint_url": endpoint_url}
                    self._chat_scroll().remove_children()
                    await self._render_note(f"Model set to **{model}** — say hi!")
                    self._refresh_status()
            except Exception as exc:
                self.notify(f"failed: {exc}", severity="error")
            return
        try:
            await self.client.patch_session(
                self.current["id"], model=model, endpoint_url=endpoint_url,
                endpoint_id=endpoint_id,
            )
            self.current["model"] = model
            self.current["endpoint_url"] = endpoint_url
            self._refresh_status()
            self.notify(f"Model set to {model}", timeout=2)
        except Exception as exc:
            self.notify(f"failed: {exc}", severity="error")

    def action_new_thread(self) -> None:
        async def _new() -> None:
            if not self.models:
                await self._load_models()
            if not self.current or not self.current.get("model"):
                await self._open_default_or_create()
                if self.current:
                    self._chat_scroll().remove_children()
                    await self._render_note(
                        f"New thread — model **{self.current.get('model')}**."
                    )
                    self._refresh_status()
                return
            try:
                session = await self.client.create_session(
                    name="New chat",
                    model=self.current["model"],
                    endpoint_url=self.current["endpoint_url"],
                    endpoint_id="",
                )
                await self._load_sessions()
                if session.get("id"):
                    self.current = {"id": session["id"], "name": "New chat",
                                    "model": self.current["model"],
                                    "endpoint_url": self.current["endpoint_url"]}
                    self._chat_scroll().remove_children()
                    await self._render_note(
                        f"New thread — model **{self.current.get('model')}**."
                    )
                    self._refresh_status()
            except Exception as exc:
                self.notify(f"new chat failed: {exc}", severity="error")
        self.run_worker(_new(), exclusive=True)

    def action_rename_thread(self) -> None:
        if not self.current:
            self.notify("No thread open", severity="warning")
            return

        async def _done(value: Optional[str]) -> None:
            if not value or not self.current:
                return
            try:
                await self.client.rename_session(self.current["id"], value)
                self.current["name"] = value
                await self._load_sessions()
                self._refresh_status()
            except Exception as exc:
                self.notify(f"rename failed: {exc}", severity="error")

        self.push_screen(
            TextModal("Rename thread:", initial=self.current.get("name") or ""),
            _done,
        )

    def action_delete_thread(self) -> None:
        if not self.current:
            self.notify("No thread open", severity="warning")
            return
        label = self.current.get("name") or self.current.get("id")
        self.push_screen(
            ConfirmScreen(f'Delete the thread "{label}"? This cannot be undone.',
                          danger=True),
            self._on_delete_confirmed,
        )

    async def _on_delete_confirmed(self, confirmed: Optional[bool]) -> None:
        if not confirmed or not self.current:
            return
        sid = self.current["id"]
        try:
            await self.client.delete_session(sid)
            self.notify("Thread deleted", timeout=2)
            await self._load_sessions()
            if self.sessions:
                await self._select_thread(self.sessions[0]["id"])
            else:
                self.current = None
                self._chat_scroll().remove_children()
                self._refresh_status()
        except Exception as exc:
            self.notify(f"delete failed: {exc}", severity="error")

    async def _select_thread(self, sid: str) -> None:
        session = next((s for s in self.sessions if s.get("id") == sid), None)
        self.current = session or {"id": sid}
        self._chat_scroll().remove_children()
        try:
            hist = await self.client.history(sid, limit=200)
            if hist.get("model"):
                self.current["model"] = hist["model"]
            if hist.get("name"):
                self.current["name"] = hist["name"]
            if hist.get("endpoint_url"):
                self.current["endpoint_url"] = hist["endpoint_url"]
            turns = list(hist.get("history") or [])
        except Exception as exc:
            await self._render_note(f"⚠ could not load history: {exc}")
            self._refresh_status()
            return
        if not turns:
            await self._render_note(
                f"New thread — model **{self.current.get('model') or '?'}**.\n"
                "Type a message and press Enter."
            )
        else:
            for turn in turns:
                await self._render_turn(
                    turn.get("role") or "assistant", turn.get("content") or ""
                )
        self._scroll_to_bottom()
        self._refresh_status()
        self.query_one("#prompt", Input).focus()
        self._set_mode("type")

    # ------------------------------------------------------------------ #
    # Message handling
    # ------------------------------------------------------------------ #
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "prompt":
            return
        text = (event.value or "").strip()
        event.input.value = ""
        if not text:
            return
        self.run_worker(self._handle_message(text), exclusive=True, group="chat")

    async def _handle_message(self, text: str) -> None:
        text = text.strip()
        if not self.current or not self.current.get("model"):
            self.notify("No model selected — press m to pick one.", severity="warning")
            return

        if text.lower().startswith("/agent"):
            self.agent_mode = True
            text = text[len("/agent"):].strip()
            if not text:
                self._refresh_status()
                self.notify("Agent mode on — type your request.", timeout=2)
                return

        mode = "agent" if self.agent_mode else "chat"
        await self._render_turn("user", text)
        scroll = self._chat_scroll()
        await scroll.mount(
            Static(f"[{self.current.get('model') or 'psd.ai'} — assistant]",
                   classes="meta")
        )
        self._refresh_status()

        gen = self.client.chat_stream(
            text,
            self.current["id"],
            mode=mode,
            allow_bash=self.agent_mode,
        )

        assistant_md = Markdown("", classes="assistant-bubble")
        await scroll.mount(assistant_md)
        self._scroll_to_bottom()

        buf: List[str] = []
        busy = ""
        try:
            async for chunk in gen:
                if self._quitting:
                    break
                if "_error" in chunk:
                    msg = str(chunk["_error"])
                    self.notify(msg, severity="error")
                    buf.append(f"\n\n⚠ *{msg}*")
                    assistant_md.update("".join(buf))
                    self._scroll_to_bottom()
                    continue
                if "_done" in chunk:
                    continue
                ctype = chunk.get("type")
                if "delta" in chunk:
                    buf.append(str(chunk["delta"]))
                    assistant_md.update("".join(buf))
                    self._scroll_to_bottom()
                elif ctype in ("model_info", "model_actual", "fallback"):
                    model = (chunk.get("model")
                             or chunk.get("answered_by")
                             or chunk.get("suffix"))
                    if model:
                        self.current["model"] = model
                        self._refresh_status()
                elif ctype == "tool_start":
                    busy = f"⚙ {chunk.get('tool')}"
                    self._update_status(f"{self._status_line()} — {busy}")
                elif ctype in ("tool_result", "tool_end"):
                    busy = ""
                    self._refresh_status()
                elif ctype == "research_progress":
                    busy = "🔍 research…"
                    self._update_status(f"{self._status_line()} — {busy}")
                # attachments / rag_sources / web_sources: informational only.
        finally:
            if busy:
                self._refresh_status()

    # ------------------------------------------------------------------ #
    # Misc
    # ------------------------------------------------------------------ #
    def action_quit(self) -> None:
        self._quitting = True
        self.exit()


# --------------------------------------------------------------------------- #
# Console login / setup flows
# --------------------------------------------------------------------------- #
def _console_error(msg: str) -> "None":
    print(f"\n[psd.ai] {msg}", file=sys.stderr)
    sys.exit(1)


def _console_login(username_hint: str = "") -> tuple:
    """Collect username/password (+2FA) on the console before the TUI starts.

    Reading directly from the terminal keeps credentials off the process argv
    and out of the TUI widget history.
    """
    print()
    print("  ⛵ psd.ai — terminal sign-in")
    print("  ----------------------------------------")
    print("  (Credentials go straight to psd.ai — nothing is stored here.)")
    print()
    while True:
        username = (username_hint or input("  Username: ").strip().lower())
        if username:
            break
    password = getpass.getpass("  Password: ")
    if not password:
        _console_error("Password cannot be empty.")
    return username, password, ""


def _ask_admin_password() -> str:
    while True:
        password = getpass.getpass("  Admin password: ")
        if len(password) < 8:
            print("  Password must be at least 8 characters.")
            continue
        confirm = getpass.getpass("  Confirm password: ")
        if password != confirm:
            print("  Passwords don't match — try again.")
            continue
        return password


# --------------------------------------------------------------------------- #
# Embedded helper: boot the app in-process
# --------------------------------------------------------------------------- #
def _run_first_time_setup(auth_manager: Any, app_module: Any = None) -> None:
    """Mirror setup.py: create data dirs, init the DB, create the admin account."""
    # app.py already calls init_db() (and initialize_managers) at import time,
    # so the tables exist. Directories are created by core.database too. We just
    # need the first-run admin account.
    if auth_manager.is_configured:
        print("  [ok] Admin account already exists — skipping setup.")
        return

    print()
    print("  ⛵ psd.ai — first-time setup (embedded)")
    print("  Create the admin account:")
    env_user = os.getenv("PSD_AI_ADMIN_USER", "").strip().lower()
    env_pass = os.getenv("PSD_AI_ADMIN_PASSWORD", "").strip()

    if env_user and env_pass:
        username, password = env_user, env_pass
    else:
        while True:
            username = (input("  Username [admin]: ").strip().lower()) or "admin"
            if username in ("root", "owner"):
                print("  That username is reserved — pick another.")
                continue
            break
        password = _ask_admin_password()

    created = False
    try:
        created = bool(auth_manager.setup(username, password))
    except Exception as exc:
        _console_error(f"Could not create admin account '{username}': {exc}")
    if not created:
        _console_error(
            f"Could not create admin account '{username}' "
            "(reserved username, or an account already exists)."
        )
    print(f"  [ok] Admin account created: {username}")
    print()


# --------------------------------------------------------------------------- #
# Entrypoints
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="psd_tui.py",
        description="Terminal interface for psd.ai (replaces the localhost browser).",
    )
    p.add_argument("--host", default="127.0.0.1",
                   help="server host when attaching (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=7000,
                   help="server port when attaching (default 7000)")
    p.add_argument("--username", help="login username (defaults to PSD_TUI_USER env)")
    p.add_argument("--password", help="login password (defaults to PSD_TUI_PASSWORD env)")
    p.add_argument("--embedded", action="store_true",
                   help="run psd.ai in-process — no HTTP server, no browser, no port")
    p.add_argument("--no-setup", action="store_true",
                   help="(embedded) skip first-run DB + admin setup")
    p.add_argument("--version", action="store_true", help="print version and exit")
    return p.parse_args(argv)


async def run_attached(args: argparse.Namespace) -> int:
    from tui.api_client import TuiApiClient

    base_url = f"http://{args.host}:{args.port}"
    client = TuiApiClient(base_url)

    alive = False
    try:
        await client.health()
        alive = True
    except Exception:
        alive = False

    if not alive:
        if AUTO_EMBED_ON_FAIL:
            print(f"\n  [psd.ai] No server found on {base_url}.")
            print("           Falling back to embedded mode (psd.ai runs inside the")
            print("           terminal — no HTTP, no browser, no port).")
            print("           Press Ctrl+C to cancel.\n")
            await asyncio.sleep(1.5)
            return await run_embedded(args)
        _console_error(f"No psd.ai server on {base_url}. Start it first, or use --embedded.")

    username = args.username or os.getenv("PSD_TUI_USER") or ""
    password = args.password or os.getenv("PSD_TUI_PASSWORD") or ""
    if not (username and password):
        username, password, _ = _console_login(username)

    # Interactive login loop (handles wrong password + 2FA challenge).
    async def _attempt() -> str:
        totp = ""
        for _ in range(3):
            try:
                return await client.login(username, password, totp)
            except Exception as exc:
                detail = getattr(exc, "detail", None)
                if isinstance(detail, dict) and detail.get("requires_totp"):
                    totp = input("  2FA code: ").strip()
                    continue
                raise
        raise RuntimeError("too many login attempts")

    try:
        who = await _attempt()
        user = who or username
    except Exception as exc:
        _console_error(f"login failed: {exc}")

    app = PsdTuiApp(client, username=user, base_url=base_url, embedded=False)
    try:
        await app.run_async()
    finally:
        await client.aclose()
    return 0


async def run_embedded(args: argparse.Namespace) -> int:
    from tui.api_client import TuiApiClient

    # Import app.py inside this function so a plain `--help` or the attached-mode
    # path never pays the (heavy) import cost, and errors read clearly.
    import app as psd_app

    auth_manager = psd_app.auth_manager

    if not args.no_setup:
        _run_first_time_setup(auth_manager, psd_app)
    elif not auth_manager.is_configured:
        print("  [psd.ai] --no-setup given but no admin account exists; "
              "you will only be able to browse.")
        print("            Run `python setup.py` (or drop --no-setup) to create one.")

    if not auth_manager.is_configured:
        _console_error(
            "psd.ai is not configured. Re-run without --no-setup (or run "
            "`python setup.py`) to create the admin account."
        )

    username = args.username or os.getenv("PSD_TUI_USER") or ""
    password = args.password or os.getenv("PSD_TUI_PASSWORD") or ""

    if username and password:
        if not auth_manager.verify_password(username, password):
            _console_error(f"Invalid password for '{username}'.")
    elif username:
        entered = getpass.getpass(f"  Password for {username}: ")
        if not auth_manager.verify_password(username, entered):
            _console_error("Invalid credentials.")
    else:
        print()
        while True:
            username = input("  Username: ").strip().lower()
            if not username:
                continue
            passwd = getpass.getpass("  Password: ")
            if not auth_manager.verify_password(username, passwd):
                print("  Invalid credentials.")
                continue
            if auth_manager.totp_enabled(username):
                totp = input("  2FA code: ").strip()
                if not auth_manager.totp_verify(username, totp):
                    print("  Invalid 2FA code.")
                    continue
            break

    token = auth_manager.create_session_trusted(username)
    if not token:
        _console_error("Could not create a session.")

    client = TuiApiClient("http://tui.invalid", token=token, app=psd_app.app)

    app = PsdTuiApp(client, username=username, base_url="in-process", embedded=True)
    try:
        await app.run_async()
    finally:
        await client.aclose()
    return 0


async def amain(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.version:
        print(f"psd_tui {_VERSION}")
        return 0
    if not HAS_TEXTUAL:
        _console_error(
            "The `textual` package is required.\n"
            'Install it with:  pip install "textual"   (and re-run requirements).'
        )
    if args.embedded:
        return await run_embedded(args)
    return await run_attached(args)


def main(argv: Optional[List[str]] = None) -> int:
    return asyncio.run(amain(argv))


if __name__ == "__main__":
    raise SystemExit(main())
