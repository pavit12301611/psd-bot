"""The Chat workspace screen: sessions, streaming transcript, composer."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSplitter, QVBoxLayout,
    QWidget,
)

from gui.api import ChatTurn
from gui.backend import BackendError, SseStream
from gui.theme import current_theme
from gui.views.base import View
from gui.widgets.composer import Composer
from gui.widgets.session_list import SessionList
from gui.widgets.transcript import Approval, MessageState, ToolCall, Transcript
from gui.widgets.dialogs import confirm, prompt, save_file
from gui.workers import run

logger = logging.getLogger("psd.gui.chat")


class _PumpSignals(QObject):
    """Cross-thread signals carrying stream events to the GUI thread."""

    event = Signal(object)
    started = Signal()
    finished = Signal(bool)
    failed = Signal(str)


class ChatView(View):
    """Chat + sessions, wired to ``/api/chat_stream`` over the in-process bridge."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Chat",
            "Talk to your models and agents. Streams render live; tools, "
            "approvals and documents are handled inline.",
            parent,
        )
        self.sessions_widget = SessionList()
        self.transcript = Transcript()
        self.composer = Composer()

        # header extras
        self.session_title = QLineEdit()
        self.session_title.setObjectName("Ghost")
        self.session_title.setStyleSheet("border: none; background: transparent; font-weight: 700;")
        self.session_title.editingFinished.connect(self._rename_current)
        self.add_action(self.session_title)
        self.context_label = QLabel("")
        self.context_label.setObjectName("StatusPill")
        self.add_action(self.context_label)
        refresh = QPushButton("Reload")
        refresh.setObjectName("Ghost")
        refresh.clicked.connect(lambda: self.refresh())
        self.add_action(refresh)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)
        right_layout.addWidget(self.transcript, 1)
        right_layout.addWidget(self.composer)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.sessions_widget)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 900])
        self.set_content(splitter)

        self._sessions: List[Dict[str, Any]] = []
        self._current_session: str = ""
        self._stream: Optional[SseStream] = None
        self._assistant_card = None
        self._assistant_state: Optional[MessageState] = None
        self._pump = _PumpSignals(self)
        self._pump.event.connect(self._on_event)
        self._pump.started.connect(self._on_stream_started)
        self._pump.finished.connect(self._on_stream_finished)
        self._pump.failed.connect(self._on_stream_failed)
        self._streaming = False
        self._default_route: Dict[str, Any] = {}

        # wire widgets
        self.sessions_widget.selected.connect(self.open_session)
        self.sessions_widget.new_requested.connect(self.new_chat)
        self.sessions_widget.rename_requested.connect(self._rename_session)
        self.sessions_widget.delete_requested.connect(self._delete_session)
        self.sessions_widget.archive_requested.connect(self._archive_session)
        self.sessions_widget.export_requested.connect(self._export_session)
        self.sessions_widget.important_requested.connect(self._star_session)
        self.composer.send_requested.connect(self.send_message)
        self.composer.stop_requested.connect(self.stop_stream)
        self.composer.attach_requested.connect(self._attach_files)
        self.transcript.link_activated.connect(self._open_link)
        self.transcript.approval_decided.connect(self._on_approval)

        self.welcome()

    # ------------------------------------------------------------------ #
    # loading
    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        self.run(self._load_sessions, on_done=self._sessions_loaded)
        self.run(self._load_models, on_done=self._models_loaded)

    def _load_sessions(self) -> List[Dict[str, Any]]:
        return self.api.sessions()

    def _sessions_loaded(self, sessions: List[Dict[str, Any]]) -> None:
        self._sessions = sessions or []
        self.sessions_widget.set_sessions(self._sessions)
        if self._current_session:
            self.sessions_widget.select_session(self._current_session)

    def _load_models(self) -> Dict[str, Any]:
        models = self.api.models()
        default = self.api.default_chat()
        return {"models": models, "default": default}

    def _models_loaded(self, payload: Dict[str, Any]) -> None:
        self._default_route = payload.get("default") or {}
        self.composer.set_models(payload.get("models") or {}, self._default_route)

    # ------------------------------------------------------------------ #
    # sessions
    # ------------------------------------------------------------------ #
    def welcome(self) -> None:
        self.transcript.clear()
        state = MessageState(
            role="assistant",
            content=(
                "**Welcome to psd.ai on your desktop.**\n\n"
                "Pick a chat on the left or start a new one. The composer chips control "
                "what the model may do this turn:\n\n"
                "- **Agent** — tools: files, notes, calendar, email, shell\n"
                "- **Web** — live web search and page reading\n"
                "- **RAG** — search your indexed personal documents\n"
                "- **Deep research** — multi-step report with sources\n\n"
                "Models are managed under **Models** and **Local Models** in the sidebar."
            ),
        )
        self.transcript.add_message(state)

    def new_chat(self) -> None:
        self._current_session = ""
        self.session_title.setText("")
        self.context_label.setText("")
        self.welcome()
        self.composer.clear_input()
        self.composer.focus_input()

    def open_session(self, session_id: str) -> None:
        if not session_id or session_id == self._current_session:
            return
        if self._streaming:
            if not confirm(self, "Switch chat?",
                           "A response is still streaming. Switching stops it.",
                           yes="Stop & switch"):
                return
            self.stop_stream()
        self._current_session = session_id
        self.sessions_widget.select_session(session_id)
        session = next((s for s in self._sessions if s.get("id") == session_id), {})
        self.session_title.setText(session.get("name") or "Untitled")
        self.transcript.clear()
        self.run(self.api.history, session_id, on_done=self._history_loaded)
        self.run(self.api.context_info, session_id,
                 on_done=lambda info: self.context_label.setText(
                     f"context {info.get('context_length') or '?'}"
                 ), on_error=lambda _e: self.context_label.setText(""))

    def _history_loaded(self, payload: Dict[str, Any]) -> None:
        self.transcript.clear()
        history = (payload or {}).get("history", []) or []
        if not history:
            self.welcome()
            return
        for message in history:
            role = message.get("role") or "user"
            meta = message.get("metadata") or {}
            state = MessageState(
                role="assistant" if role == "assistant" else ("user" if role == "user" else "system"),
                content=message.get("content") or "",
                raw=message.get("content") or "",
                model=message.get("model") or "",
            )
            if meta.get("timestamp"):
                try:
                    from datetime import datetime

                    state.timestamp = datetime.fromisoformat(
                        str(meta["timestamp"]).replace("Z", "+00:00")).timestamp()
                except Exception:  # noqa: BLE001
                    pass
            tools = meta.get("tool_calls") or meta.get("tools") or []
            for tool in tools:
                state.tools.append(ToolCall(
                    tool=tool.get("tool") or tool.get("name") or "tool",
                    command=tool.get("command") or "",
                    output=tool.get("output") or "",
                    exit_code=tool.get("exit_code"),
                    running=False,
                ))
            card = self.transcript.add_message(state)
            card.load_images(self._fetch_image)
        self.transcript.scroll_to_bottom()

    def _rename_current(self) -> None:
        if not self._current_session:
            return
        name = self.session_title.text().strip()
        if not name:
            return
        self.run(self.api.rename_session, self._current_session, name,
                 on_done=lambda _r: self.refresh())

    def _rename_session(self, session_id: str) -> None:
        session = next((s for s in self._sessions if s.get("id") == session_id), {})
        name = prompt(self, "Rename chat", "New name:", session.get("name") or "")
        if name:
            self.run(self.api.rename_session, session_id, name,
                     on_done=lambda _r: self.refresh())

    def _delete_session(self, session_id: str) -> None:
        if not confirm(self, "Delete chat?",
                       "This permanently deletes the conversation.",
                       yes="Delete", destructive=True):
            return
        self.run(self.api.delete_session, session_id, on_done=self._after_mutation)
        if session_id == self._current_session:
            self.new_chat()

    def _archive_session(self, session_id: str, archived: bool) -> None:
        fn = self.api.archive_session if archived else self.api.unarchive_session
        self.run(fn, session_id, on_done=self._after_mutation)

    def _star_session(self, session_id: str, important: bool) -> None:
        self.run(self.api.mark_important, session_id, important,
                 on_done=self._after_mutation)

    def _export_session(self, session_id: str) -> None:
        path = save_file(self, "Export chat", "chat-export.json", "JSON files (*.json)")
        if not path:
            return

        def _do() -> None:
            data = self.api.export_session(session_id)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)

        run(_do, on_done=lambda _r: self.toast(f"Saved to {path}", "success"),
            on_error=self.error)

    def _after_mutation(self, _result: Any = None) -> None:
        self.refresh()

    # ------------------------------------------------------------------ #
    # attachments
    # ------------------------------------------------------------------ #
    def _attach_files(self, paths: List[str]) -> None:
        if not paths:
            return
        self.run(self.api.upload_files, paths, self._current_session or "",
                 on_done=self._uploads_done)

    def _uploads_done(self, payload: Dict[str, Any]) -> None:
        files = (payload or {}).get("files") or []
        existing = self.composer.attachments
        merged = existing + [
            {"file_id": f.get("file_id") or f.get("id"), "name": f.get("name") or f.get("filename")}
            for f in files if (f.get("file_id") or f.get("id"))
        ]
        self.composer.set_attachments(merged)
        self.toast(f"Attached {len(files)} file(s)", "success")

    # ------------------------------------------------------------------ #
    # sending
    # ------------------------------------------------------------------ #
    def send_message(self, text: str) -> None:
        if self._streaming:
            return
        route = self.composer.current_route()
        if not route or route.get("offline") and not route.get("model"):
            self.toast("No model available — add one under Models first.", "warning", 6000)
            return
        toggles = self.composer.toggles()

        def _prepare_and_send() -> Dict[str, Any]:
            session_id = self._current_session
            if not session_id:
                first_line = text.strip().splitlines()[0][:60] if text.strip() else "New chat"
                created = self.api.create_session(
                    name=first_line, model=route.get("model") or "",
                    endpoint_id=route.get("endpoint_id") or "")
                session_id = created.get("id") or ""
            turn = ChatTurn(
                message=text,
                session_id=session_id,
                mode="agent" if (toggles["agent"] or toggles["plan"] or toggles["research"]) else "chat",
                model=route.get("model") or "",
                endpoint_id=route.get("endpoint_id") or "",
                endpoint_url=route.get("endpoint_url") or "",
                attachments=[a["file_id"] for a in self.composer.attachments if a.get("file_id")],
                use_web=toggles["web"],
                allow_web_search=toggles["web"],
                allow_bash=toggles["bash"],
                use_rag=toggles["rag"],
                use_research=toggles["research"],
                incognito=toggles["incognito"],
                plan_mode=toggles["plan"],
            )
            stream = self.api.stream_chat(turn)
            return {"session_id": session_id, "stream": stream, "turn": turn}

        # user card immediately
        user_state = MessageState(role="user", content=text, raw=text)
        self.transcript.add_message(user_state)
        self.composer.clear_input()

        run(_prepare_and_send, on_done=self._stream_prepared, on_error=self._send_failed)

    def _stream_prepared(self, payload: Dict[str, Any]) -> None:
        session_id = payload.get("session_id") or ""
        stream = payload["stream"]
        self._current_session = session_id
        if not self.session_title.text().strip():
            session = next((s for s in self._sessions if s.get("id") == session_id), {})
            self.session_title.setText(session.get("name") or "")
        self._stream = stream
        self._assistant_state = MessageState(role="assistant", content="", streaming=True)
        self._assistant_card = self.transcript.add_message(self._assistant_state)
        self._streaming = True
        self.composer.set_busy(True)
        self._pump.started.emit()
        run(self._pump_stream, stream, on_done=None, on_error=self._send_failed)

    def _pump_stream(self, stream: SseStream) -> None:
        try:
            for event in stream:
                self._pump.event.emit(event)
            self._pump.finished.emit(False)
        except BackendError as exc:
            self._pump.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            self._pump.failed.emit(f"{type(exc).__name__}: {exc}")

    def stop_stream(self) -> None:
        stream = self._stream
        if stream is not None:
            stream.cancel()
        if self._current_session:
            try:
                self.api.stop_chat(self._current_session)
            except BackendError:
                pass
        if self._assistant_state:
            self._assistant_state.stopped = True

    def _send_failed(self, exc: BaseException) -> None:
        self._streaming = False
        self.composer.set_busy(False)
        if self._assistant_state is not None and not self._assistant_state.content:
            self._assistant_state.content = f"⚠️ {exc}"
            if self._assistant_card:
                self._assistant_card.finalize()
        self.error(exc)

    # -- stream lifecycle -------------------------------------------------- #
    def _on_stream_started(self) -> None:
        self.transcript.scroll_to_bottom()

    def _on_stream_finished(self, stopped: bool) -> None:
        self._streaming = False
        self._stream = None
        self.composer.set_busy(False)
        self.composer.clear_attachments()
        if self._assistant_state is not None:
            self._assistant_state.streaming = False
            if not self._assistant_state.content and not self._assistant_state.tools:
                self._assistant_state.content = "_(empty response)_"
        if self._assistant_card:
            self._assistant_card.finalize(stopped=stopped)
        self._assistant_card = None
        self._assistant_state = None
        self.refresh_sessions_only()

    def _on_stream_failed(self, message: str) -> None:
        self._streaming = False
        self._stream = None
        self.composer.set_busy(False)
        if self._assistant_state is not None:
            self._assistant_state.content += f"\n\n⚠️ **Error:** {message}"
            self._assistant_state.streaming = False
        if self._assistant_card:
            self._assistant_card.finalize()
        self._assistant_card = None
        self._assistant_state = None
        self.toast(message, "error", 7000)

    def refresh_sessions_only(self) -> None:
        self.run(self._load_sessions, on_done=self._sessions_loaded)

    # -- events ------------------------------------------------------------- #
    def _on_event(self, event: Any) -> None:
        state = self._assistant_state
        card = self._assistant_card
        if state is None or card is None:
            return
        payload = event.json if isinstance(event.json, dict) else {}
        kind = payload.get("type") or event.event or ""

        if event.is_delta:
            state.content += payload.get("delta") or ""
            card.schedule_render()
            return
        if kind == "tool_start":
            card.add_tool(ToolCall(
                tool=payload.get("tool") or "tool",
                command=payload.get("command") or payload.get("full_command") or "",
                running=True,
                round=payload.get("round"),
            ))
            self.transcript.scroll_to_bottom()
            return
        if kind == "tool_output":
            card.update_last_tool(
                output=str(payload.get("output") or ""),
                exit_code=payload.get("exit_code"),
                running=False,
                command=payload.get("command") or "",
            )
            return
        if kind == "tool_progress":
            card.update_last_tool(output=str(payload.get("output") or payload.get("message") or ""))
            return
        if kind == "ask_user":
            data = payload.get("data") or {}
            approval = Approval(
                approval_id=data.get("approval_id") or "",
                question=data.get("question") or "psd.ai is asking you something",
                description=data.get("description") or "",
                options=data.get("options") or [],
                kind=str(data.get("kind") or "ask_user"),
            )
            state.approvals.append(approval)
            card.approval_card(approval)
            self.transcript.scroll_to_bottom()
            return
        if kind == "tool_approval_resolved":
            for approval in state.approvals:
                if not approval.resolved:
                    approval.resolved = str(payload.get("decision") or "")
            card._render_approvals()  # noqa: SLF001 - internal refresh
            return
        if kind == "model_info":
            state.model = str(payload.get("model") or state.model)
            card._render_meta()  # noqa: SLF001
            return
        if kind == "metrics":
            state.metrics = payload.get("data") or {}
            card._render_meta()  # noqa: SLF001
            return
        if kind == "message_saved":
            return
        if kind == "doc_update":
            doc_id = str(payload.get("doc_id") or "")
            title = payload.get("title") or "document"
            self.toast(f"Document updated: {title}", "info")
            if doc_id:
                self.open_document.emit(doc_id)
            return
        if kind == "generated_image":
            filename = payload.get("filename") or payload.get("url") or ""
            state.content += f"\n\n![generated image]({filename})\n"
            card.schedule_render()
            return
        if kind in ("workspace_rejected", "budget_exceeded", "rounds_exhausted",
                    "escalation_failed", "skill_save_failed", "loop_breaker_triggered"):
            self.toast(f"{kind.replace('_', ' ')}: {json.dumps(payload)[:160]}", "warning", 6000)
            return
        if kind in ("rag_sources", "web_sources", "research_sources", "memories_used"):
            data = payload.get("data") or []
            if data:
                state.sources = data if isinstance(data, list) else [data]
            return
        if kind == "research_progress":
            data = payload.get("data") or {}
            note = data.get("message") or data.get("stage") or "research running"
            self.context_label.setText(str(note)[:60])
            return
        if kind == "research_done":
            self.context_label.setText("")
            self.toast("Deep research finished — see Research view", "success")
            return
        if kind == "compacted" or kind == "context_trimmed":
            self.context_label.setText("context compacted")
            return
        if kind == "fallback":
            self.toast(f"Fallback model in use: {payload.get('model', '')}", "warning")
            return
        # unknown event: surface quietly in the meta line so nothing is lost
        logger.debug("unhandled SSE event: %s", kind or event.data[:80])

    # -- approvals ----------------------------------------------------------- #
    def _on_approval(self, approval: Approval, decision: str) -> None:
        approval.resolved = decision
        if approval.kind == "tool_approval" and approval.approval_id:
            # Resolving a sealed approval is a control-plane submit: same chat,
            # empty message, with the approval id + decision (mirrors the web UI).
            self._send_approval(approval, decision)
        else:
            self.send_message(decision)

    def _send_approval(self, approval: Approval, decision: str) -> None:
        if self._streaming:
            return
        route = self.composer.current_route()
        toggles = self.composer.toggles()
        turn = ChatTurn(
            message="",
            session_id=self._current_session,
            mode="agent",
            model=route.get("model") or "",
            endpoint_id=route.get("endpoint_id") or "",
            endpoint_url=route.get("endpoint_url") or "",
            tool_approval_id=approval.approval_id,
            tool_approval_decision=decision,
            allow_bash=toggles["bash"],
            use_web=toggles["web"],
        )
        state = MessageState(role="assistant", content="", streaming=True)
        state.model = route.get("model") or ""
        self._assistant_state = state
        self._assistant_card = self.transcript.add_message(state)
        self._streaming = True
        self.composer.set_busy(True)

        def _open() -> SseStream:
            return self.api.stream_chat(turn)

        run(_open, on_done=lambda stream: self._stream_prepared(
            {"session_id": self._current_session, "stream": stream}),
            on_error=self._send_failed)

    # -- misc ----------------------------------------------------------------- #
    def _open_link(self, href: str) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        url = QUrl(href)
        if url.scheme() in ("http", "https", "mailto", "file"):
            QDesktopServices.openUrl(url)
        else:
            self.toast(f"Cannot open: {href}", "warning")

    def _fetch_image(self, src: str) -> bytes:
        """Resolve an in-message image without any network: backend or disk."""
        if src.startswith("/api/generated-image/"):
            return self.api.generated_image_bytes(src.rsplit("/", 1)[-1])
        if src.startswith("/api/") or src.startswith("/static/"):
            return self.api.gallery_file_bytes(src)
        if os.path.isabs(src) and os.path.isfile(src):
            with open(src, "rb") as handle:
                return handle.read()
        raise BackendError(f"Unsupported image source: {src}")

    def deactivate(self) -> None:
        if self._streaming:
            self.stop_stream()
