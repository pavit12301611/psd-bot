"""Notes workspace screen: pinned list, checklists, reminders."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from gui.views.base import View, card, muted
from gui.widgets.dialogs import confirm, form_dialog
from gui.workers import run

NOTE_COLORS = ["", "#e06c75", "#e5c07b", "#98c379", "#61afef", "#c678dd", "#56b6c2"]


class NotesView(View):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Notes",
            "Quick notes, checklists and reminders — readable and writable by the agent.",
            parent,
        )
        self.list = QListWidget()
        self.list.setMinimumWidth(240)
        self.list.currentItemChanged.connect(self._on_select)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        top = QHBoxLayout()
        self.filter_box = QComboBox()
        self.filter_box.addItems(["Active", "Archived", "All"])
        self.filter_box.currentIndexChanged.connect(lambda _i: self.refresh())
        top.addWidget(self.filter_box, 1)
        new_button = QPushButton("+ New")
        new_button.setObjectName("Primary")
        new_button.clicked.connect(self._new_note)
        top.addWidget(new_button)
        left_layout.addLayout(top)
        left_layout.addWidget(self.list, 1)

        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(8)

        title_row = QHBoxLayout()
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Note title")
        title_row.addWidget(self.title_edit, 1)
        self.type_box = QComboBox()
        self.type_box.addItems(["note", "checklist", "reminder", "goal"])
        title_row.addWidget(self.type_box)
        self.color_box = QComboBox()
        self.color_box.addItem("no colour", "")
        for color in NOTE_COLORS[1:]:
            self.color_box.addItem(color, color)
        title_row.addWidget(self.color_box)
        editor_layout.addLayout(title_row)

        self.content_edit = QPlainTextEdit()
        self.content_edit.setPlaceholderText("Note body (markdown ok)…")
        editor_layout.addWidget(self.content_edit, 1)

        self.items_frame = QFrame()
        self.items_frame.setObjectName("Card")
        self.items_layout = QVBoxLayout(self.items_frame)
        self.items_layout.setContentsMargins(10, 8, 10, 8)
        self.items_layout.setSpacing(4)
        self.items_layout.addWidget(muted("Checklist items"))
        editor_layout.addWidget(self.items_frame)

        meta_row = QHBoxLayout()
        self.due_edit = QLineEdit()
        self.due_edit.setPlaceholderText("Due / remind at (e.g. 2026-09-20 09:00)")
        meta_row.addWidget(self.due_edit, 1)
        self.pinned_box = QCheckBox("Pinned")
        meta_row.addWidget(self.pinned_box)
        editor_layout.addLayout(meta_row)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        save_button = QPushButton("Save")
        save_button.setObjectName("Primary")
        save_button.clicked.connect(self._save)
        actions.addWidget(save_button)
        archive_button = QPushButton("Archive")
        archive_button.clicked.connect(self._archive)
        actions.addWidget(archive_button)
        delete_button = QPushButton("Delete")
        delete_button.setObjectName("Danger")
        delete_button.clicked.connect(self._delete)
        actions.addWidget(delete_button)
        actions.addStretch(1)
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("MetaLine")
        actions.addWidget(self.meta_label)
        editor_layout.addLayout(actions)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(editor)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 900])
        self.set_content(splitter)

        self._notes: List[Dict[str, Any]] = []
        self._current: str = ""

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        mode = self.filter_box.currentText()
        archived = {"Active": False, "Archived": True, "All": None}[mode]
        self.run(self.api.notes, archived, on_done=self._loaded)

    def _loaded(self, notes: List[Dict[str, Any]]) -> None:
        self._notes = notes or []
        self._notes.sort(key=lambda n: (not bool(n.get("pinned")),
                                        str(n.get("updated_at") or ""), ), reverse=False)
        self._notes.sort(key=lambda n: 0 if n.get("pinned") else 1)
        current = self._current
        self.list.blockSignals(True)
        self.list.clear()
        for note in self._notes:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, note.get("id"))
            title = note.get("title") or "(untitled)"
            preview = (note.get("content") or "").replace("\n", " ")[:60]
            badge = "📌 " if note.get("pinned") else ""
            item.setText(f"{badge}{title}\n{preview}")
            self.list.addItem(item)
            if note.get("id") == current:
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)

    def _on_select(self, current: Optional[QListWidgetItem], _prev) -> None:
        if current is None:
            return
        note_id = str(current.data(Qt.UserRole) or "")
        note = next((n for n in self._notes if n.get("id") == note_id), {})
        self._current = note_id
        self.title_edit.setText(note.get("title") or "")
        self.content_edit.setPlainText(note.get("content") or "")
        self.type_box.setCurrentText(note.get("note_type") or "note")
        index = self.color_box.findData(note.get("color") or "")
        self.color_box.setCurrentIndex(max(0, index))
        self.due_edit.setText(str(note.get("due_date") or "").replace("T", " ")[:16])
        self.pinned_box.setChecked(bool(note.get("pinned")))
        self._render_items(note.get("items") or [])
        self.meta_label.setText(
            f"{note.get('note_type', 'note')} · updated {str(note.get('updated_at') or '')[:16]}")

    def _render_items(self, items: List[Dict[str, Any]]) -> None:
        while self.items_layout.count():
            item = self.items_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        if not items:
            self.items_frame.setVisible(False)
            return
        self.items_frame.setVisible(True)
        for index, entry in enumerate(items):
            box = QCheckBox(entry.get("text") or "")
            box.setChecked(bool(entry.get("done")))
            box.toggled.connect(
                lambda checked, i=index: self._toggle_item(i, checked))
            self.items_layout.addWidget(box)

    def _toggle_item(self, index: int, checked: bool) -> None:
        if not self._current:
            return
        run(self.api.toggle_note_item, self._current, index,
            on_done=lambda _r: self.refresh(), on_error=self.error)

    # -- actions ----------------------------------------------------------- #
    def _new_note(self) -> None:
        values = form_dialog(self, "New note", [
            ("title", "Title", "text", ""),
            ("note_type", "Type", "combo", ["note", "checklist", "reminder", "goal"]),
            ("content", "Body", "multiline", ""),
        ])
        if not values:
            return
        run(self.api.create_note, title=values.get("title") or "",
            note_type=values.get("note_type") or "note",
            content=values.get("content") or "",
            on_done=lambda note: (self.refresh(),
                                  self._select_first(note.get("id") if isinstance(note, dict) else "")),
            on_error=self.error)

    def _select_first(self, note_id: str) -> None:
        if note_id:
            self._current = note_id
            self.refresh()

    def _save(self) -> None:
        if not self._current:
            self._new_note()
            return
        fields = {
            "title": self.title_edit.text().strip(),
            "content": self.content_edit.toPlainText(),
            "note_type": self.type_box.currentText(),
            "color": self.color_box.currentData() or None,
            "pinned": self.pinned_box.isChecked(),
            "due_date": self.due_edit.text().strip() or None,
        }
        run(self.api.update_note, self._current, **fields,
            on_done=lambda _r: (self.toast("Note saved", "success", 1500), self.refresh()),
            on_error=self.error)

    def _archive(self) -> None:
        if not self._current:
            return
        note = next((n for n in self._notes if n.get("id") == self._current), {})
        run(self.api.archive_note, self._current, not bool(note.get("archived")),
            on_done=lambda _r: self.refresh(), on_error=self.error)

    def _delete(self) -> None:
        if not self._current:
            return
        if not confirm(self, "Delete note?", "This cannot be undone.",
                       yes="Delete", destructive=True):
            return
        run(self.api.delete_note, self._current,
            on_done=lambda _r: self.refresh(), on_error=self.error)
