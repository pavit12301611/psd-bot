"""Deep Research workspace screen."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QProgressBar,
    QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from gui.theme import current_theme
from gui.views.base import View, card
from gui.widgets.transcript import RichView
from gui.workers import run


class ResearchView(View):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Deep Research",
            "Multi-step research: the agent searches, reads sources and writes a report.",
            parent,
        )
        start_row = QHBoxLayout()
        start_row.setSpacing(8)
        self.query_edit = QLineEdit()
        self.query_edit.setPlaceholderText(
            "What should psd.ai research? e.g. “Compare local 8B models for code tasks”")
        self.query_edit.returnPressed.connect(self._start)
        start_row.addWidget(self.query_edit, 1)
        self.start_button = QPushButton("Start research")
        self.start_button.setObjectName("Primary")
        self.start_button.clicked.connect(self._start)
        start_row.addWidget(self.start_button)
        start_wrap = QWidget()
        start_wrap.setLayout(start_row)
        self.content_layout.addWidget(start_wrap)

        self.active_card = card("Running now")
        active_layout = self.active_card.layout_  # type: ignore[attr-defined]
        self.active_list = QListWidget()
        self.active_list.setMaximumHeight(150)
        active_layout.addWidget(self.active_list)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        active_layout.addWidget(self.progress)
        self.status_label = QLabel("")
        self.status_label.setObjectName("MetaLine")
        active_layout.addWidget(self.status_label)
        cancel_button = QPushButton("Cancel run")
        cancel_button.setObjectName("Danger")
        cancel_button.clicked.connect(self._cancel_active)
        active_layout.addWidget(cancel_button)
        self.content_layout.addWidget(self.active_card)

        splitter = QSplitter(Qt.Horizontal)

        library_widget = QWidget()
        library_layout = QVBoxLayout(library_widget)
        library_layout.setContentsMargins(0, 0, 0, 0)
        library_layout.setSpacing(8)
        library_label = QLabel("Report library")
        library_label.setObjectName("Accent")
        library_layout.addWidget(library_label)
        self.library_list = QListWidget()
        self.library_list.setMinimumWidth(250)
        self.library_list.currentItemChanged.connect(self._open_report)
        library_layout.addWidget(self.library_list, 1)
        splitter.addWidget(library_widget)

        self.report_view = RichView()
        splitter.addWidget(self.report_view)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 800])
        self.set_content(splitter)

        self._timer = QTimer(self)
        self._timer.setInterval(4000)
        self._timer.timeout.connect(self._poll_active)
        self._timer.start()
        self._active: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        self.run(self.api.research_library, on_done=self._library_loaded)
        self._poll_active()

    def _library_loaded(self, payload: Dict[str, Any]) -> None:
        items = (payload or {}).get("research") or []
        current = self.library_list.currentItem()
        current_id = current.data(Qt.UserRole) if current else None
        self.library_list.clear()
        for entry in items:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, entry.get("session_id") or entry.get("id"))
            item.setText(f"{entry.get('query') or entry.get('title') or 'research'}\n"
                         f"{str(entry.get('created_at') or '')[:16]}")
            self.library_list.addItem(item)
            if entry.get("session_id") == current_id:
                self.library_list.setCurrentItem(item)

    def _poll_active(self) -> None:
        run(self.api.research_active, on_done=self._active_loaded,
            on_error=lambda _e: None)

    def _active_loaded(self, payload: Any) -> None:
        items = payload if isinstance(payload, list) else (payload or {}).get("active") or []
        self._active = items
        self.active_list.clear()
        for entry in items:
            text = (entry.get("query") or entry.get("session_id") or "run")
            stage = entry.get("stage") or entry.get("status") or ""
            self.active_list.addItem(f"{text}  ·  {stage}")
        busy = bool(items)
        self.progress.setVisible(busy)
        self.status_label.setText(f"{len(items)} run(s) active" if items else "idle")

    def _cancel_active(self) -> None:
        item = self.active_list.currentItem()
        if not item or not self._active:
            return
        index = self.active_list.row(item)
        entry = self._active[index] if index < len(self._active) else {}
        session_id = entry.get("session_id") or ""
        if session_id:
            run(self.api.research_cancel, session_id,
                on_done=lambda _r: self._poll_active(), on_error=self.error)

    # -- actions ----------------------------------------------------------- #
    def _start(self) -> None:
        query = self.query_edit.text().strip()
        if not query:
            return
        self.start_button.setEnabled(False)
        run(self.api.research_start, query,
            on_done=self._started, on_error=self._start_failed)

    def _started(self, payload: Dict[str, Any]) -> None:
        self.start_button.setEnabled(True)
        self.query_edit.clear()
        session_id = (payload or {}).get("session_id") or ""
        self.toast("Research started — progress appears below", "success")
        self._poll_active()
        if session_id:
            self._follow(session_id)

    def _start_failed(self, exc: BaseException) -> None:
        self.start_button.setEnabled(True)
        self.error(exc)

    def _follow(self, session_id: str) -> None:
        stream = None
        try:
            stream = self.api.research_stream(session_id)
        except Exception as exc:  # noqa: BLE001
            self.error(exc)
            return

        def _consume() -> List[str]:
            notes = []
            for event in stream:
                payload = event.json if isinstance(event.json, dict) else {}
                data = payload.get("data") or {}
                note = data.get("message") or data.get("stage") or payload.get("type") or ""
                if note:
                    notes.append(str(note))
            return notes

        run(_consume, on_done=lambda notes: (
            self.status_label.setText(" · ".join(notes[-3:]) or "done"),
            self._poll_active(), self.refresh()),
            on_error=lambda _e: self._poll_active())

    def _open_report(self, current: Optional[QListWidgetItem], _prev) -> None:
        if current is None:
            return
        session_id = str(current.data(Qt.UserRole) or "")
        if not session_id:
            return
        run(self.api.research_report, session_id,
            on_done=self._report_loaded, on_error=self.error)

    def _report_loaded(self, payload: Any) -> None:
        if isinstance(payload, dict):
            markdown_text = (payload.get("markdown") or payload.get("report")
                             or payload.get("content") or "")
            if not markdown_text and payload.get("html"):
                self.report_view.setHtml(str(payload["html"]))
                return
        else:
            markdown_text = str(payload or "")
        from gui import markdown_html as md

        result = md.render_markdown(markdown_text or "_no report yet_", current_theme())
        self.report_view.setHtml(result.html)

    def deactivate(self) -> None:
        self._timer.stop()

    def activate(self, force: bool = False) -> None:
        self._timer.start()
        super().activate(force)
