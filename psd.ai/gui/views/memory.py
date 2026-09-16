"""Memory workspace screen: long-term facts the agent remembers."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QWidget,
)

from gui.views.base import View
from gui.widgets.dialogs import confirm, form_dialog
from gui.workers import run


class MemoryView(View):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Memory",
            "Persistent facts and preferences the agent recalls across chats.",
            parent,
        )
        top = QHBoxLayout()
        top.setSpacing(8)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search memories…")
        self.search_edit.returnPressed.connect(self._search)
        top.addWidget(self.search_edit, 1)
        search_button = QPushButton("Search")
        search_button.clicked.connect(self._search)
        top.addWidget(search_button)
        add_button = QPushButton("+ Add memory")
        add_button.setObjectName("Primary")
        add_button.clicked.connect(self._add)
        top.addWidget(add_button)
        delete_button = QPushButton("Delete")
        delete_button.setObjectName("Danger")
        delete_button.clicked.connect(self._delete)
        top.addWidget(delete_button)
        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self.refresh)
        top.addWidget(refresh_button)
        top_wrap = QWidget()
        top_wrap.setLayout(top)
        self.content_layout.addWidget(top_wrap)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Memory", "Category", "Created"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(True)
        self.set_content(self.table)

        self.status_label = QLabel("")
        self.status_label.setObjectName("MetaLine")
        self.content_layout.addWidget(self.status_label)
        self._memories: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        self.run(self.api.memories, on_done=self._loaded)

    def _loaded(self, memories: List[Dict[str, Any]]) -> None:
        self._memories = memories or []
        self.table.setRowCount(len(self._memories))
        for row, memory in enumerate(self._memories):
            values = [
                memory.get("content") or "",
                memory.get("category") or memory.get("kind") or "",
                str(memory.get("created_at") or "")[:16],
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.UserRole, memory.get("id"))
                self.table.setItem(row, column, item)
        self.status_label.setText(f"{len(self._memories)} memories stored")

    def _search(self) -> None:
        query = self.search_edit.text().strip()
        if not query:
            self.refresh()
            return
        run(self.api.search_memory, query, on_done=self._search_loaded, on_error=self.error)

    def _search_loaded(self, payload: Any) -> None:
        results = payload if isinstance(payload, list) else (payload or {}).get("results") or []
        self._loaded(results)
        self.status_label.setText(f"{len(results)} matches")

    def _add(self) -> None:
        values = form_dialog(self, "Add memory", [
            ("content", "What should psd.ai remember?", "multiline", ""),
            ("category", "Category", "combo", ["fact", "preference", "person", "project", "instruction"]),
        ])
        if not values or not values.get("content"):
            return
        run(self.api.add_memory, values["content"], values.get("category") or "fact",
            on_done=lambda _r: (self.toast("Memory saved", "success"), self.refresh()),
            on_error=self.error)

    def _delete(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        item = self.table.item(row, 0)
        memory_id = item.data(Qt.UserRole) if item else None
        if not memory_id:
            return
        if not confirm(self, "Forget memory?", self.table.item(row, 0).text()[:120],
                       yes="Forget", destructive=True):
            return
        run(self.api.delete_memory, memory_id,
            on_done=lambda _r: self.refresh(), on_error=self.error)
