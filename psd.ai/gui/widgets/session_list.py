"""The chat session sidebar: search, list, context actions."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMenu, QPushButton, QVBoxLayout, QWidget,
)

from gui.theme import current_theme


def _relative_time(value: Any) -> str:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        text = str(value or "")
        if not text:
            return ""
        try:
            from datetime import datetime

            timestamp = datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except Exception:  # noqa: BLE001
            return text[:16]
    delta = time.time() - timestamp
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    if delta < 7 * 86400:
        return f"{int(delta // 86400)}d ago"
    return time.strftime("%d %b", time.localtime(timestamp))


class SessionList(QFrame):
    """Left-hand session browser used by the chat view."""

    selected = Signal(str)
    new_requested = Signal()
    rename_requested = Signal(str)
    delete_requested = Signal(str)
    archive_requested = Signal(str, bool)
    export_requested = Signal(str)
    important_requested = Signal(str, bool)
    clear_all_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(230)
        self.setMaximumWidth(340)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(6)
        self.new_button = QPushButton("+ New chat")
        self.new_button.setObjectName("Primary")
        self.new_button.clicked.connect(self.new_requested)
        top.addWidget(self.new_button, 1)
        self.archived_button = QPushButton("Archive")
        self.archived_button.setObjectName("Ghost")
        self.archived_button.setCheckable(True)
        self.archived_button.setToolTip("Show archived sessions")
        top.addWidget(self.archived_button)
        layout.addLayout(top)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search chats…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search)

        self.list = QListWidget()
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        self.list.currentItemChanged.connect(self._on_current)
        self.list.setWordWrap(False)
        layout.addWidget(self.list, 1)

        self.count_label = QLabel("")
        self.count_label.setObjectName("MetaLine")
        layout.addWidget(self.count_label)

        self._sessions: List[Dict[str, Any]] = []
        self._loading = False

    # -- data ------------------------------------------------------------- #
    def set_sessions(self, sessions: List[Dict[str, Any]]) -> None:
        self._sessions = sessions or []
        current_id = self.current_session_id()
        query = self.search.text().strip().lower()
        self.list.blockSignals(True)
        self.list.clear()
        shown = 0
        for session in self._sessions:
            name = session.get("name") or "Untitled"
            if query and query not in name.lower():
                continue
            item = QListWidgetItem()
            item.setData(Qt.UserRole, session.get("id"))
            meta_bits = [_relative_time(session.get("last_message_at") or session.get("updated_at"))]
            if session.get("message_count"):
                meta_bits.append(f"{session['message_count']} msgs")
            if session.get("model"):
                meta_bits.append(str(session["model"])[:22])
            if session.get("is_important"):
                meta_bits.append("★")
            label = f"{name}\n{' · '.join(bit for bit in meta_bits if bit)}"
            item.setText(label)
            item.setToolTip(name)
            if session.get("archived"):
                item.setForeground(current_theme().text_faint)
            self.list.addItem(item)
            shown += 1
            if session.get("id") == current_id:
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)
        self.count_label.setText(f"{shown} of {len(self._sessions)} chats")

    def _apply_filter(self, _text: str) -> None:
        self.set_sessions(self._sessions)

    def current_session_id(self) -> str:
        item = self.list.currentItem()
        return str(item.data(Qt.UserRole)) if item else ""

    def select_session(self, session_id: str) -> None:
        for index in range(self.list.count()):
            item = self.list.item(index)
            if item.data(Qt.UserRole) == session_id:
                self.list.blockSignals(True)
                self.list.setCurrentItem(item)
                self.list.blockSignals(False)
                return

    # -- events ----------------------------------------------------------- #
    def _on_current(self, current: Optional[QListWidgetItem], _prev) -> None:
        if current is None or self._loading:
            return
        self.selected.emit(str(current.data(Qt.UserRole) or ""))

    def _context_menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if item is None:
            return
        session_id = str(item.data(Qt.UserRole) or "")
        session = next((s for s in self._sessions if s.get("id") == session_id), {})
        menu = QMenu(self)
        rename = menu.addAction("Rename…")
        star_text = "Remove star" if session.get("is_important") else "Star"
        star = menu.addAction(star_text)
        archive_text = "Unarchive" if session.get("archived") else "Archive"
        archive = menu.addAction(archive_text)
        export = menu.addAction("Export as JSON…")
        menu.addSeparator()
        delete = menu.addAction("Delete chat")
        chosen = menu.exec(self.list.mapToGlobal(pos))
        if chosen == rename:
            self.rename_requested.emit(session_id)
        elif chosen == star:
            self.important_requested.emit(session_id, not bool(session.get("is_important")))
        elif chosen == archive:
            self.archive_requested.emit(session_id, not bool(session.get("archived")))
        elif chosen == export:
            self.export_requested.emit(session_id)
        elif chosen == delete:
            self.delete_requested.emit(session_id)
