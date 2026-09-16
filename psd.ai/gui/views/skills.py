"""Skills workspace screen: learned procedures the agent can reuse."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QHeaderView, QLabel, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QWidget,
)

from gui.theme import current_theme
from gui.views.base import View
from gui.widgets.dialogs import confirm, info_dialog
from gui.widgets.transcript import RichView
from gui.workers import run


class SkillsView(View):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Skills",
            "Procedures the agent extracted from your sessions — editable and reusable.",
            parent,
        )
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Skill", "Category", "Status", "Confidence"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.itemSelectionChanged.connect(self._on_selected)

        self.detail = RichView()

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.table)
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([420, 640])
        self.set_content(splitter)

        for label, handler, style in (
            ("Enable", lambda: self._set_status("active"), ""),
            ("Archive", lambda: self._set_status("archived"), ""),
            ("Details", self._details, ""),
            ("Delete", self._delete, "Danger"),
        ):
            button = QPushButton(label)
            if style:
                button.setObjectName(style)
            button.clicked.connect(handler)
            self.add_action(button)
        self.count_label = QLabel("")
        self.count_label.setObjectName("MetaLine")
        self.add_action(self.count_label)
        self._skills: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        self.run(self.api.skills, on_done=self._loaded)

    def _loaded(self, payload: Dict[str, Any]) -> None:
        self._skills = (payload or {}).get("skills") or []
        self.table.setRowCount(len(self._skills))
        for row, skill in enumerate(self._skills):
            values = [
                skill.get("name") or skill.get("title") or "",
                skill.get("category") or "",
                skill.get("status") or "active",
                str(skill.get("confidence") or ""),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.UserRole, skill.get("id"))
                self.table.setItem(row, column, item)
        self.count_label.setText(f"{len(self._skills)} skills")

    def _current(self) -> Dict[str, Any]:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._skills):
            return {}
        return self._skills[row]

    def _on_selected(self) -> None:
        skill = self._current()
        if not skill:
            return
        from gui import markdown_html as md

        sections = []
        for key in ("problem", "solution", "when_to_use", "procedure", "pitfalls",
                    "verification", "description"):
            value = skill.get(key)
            if value:
                sections.append(f"### {key.replace('_', ' ').title()}\n{value}")
        tags = skill.get("tags") or []
        if tags:
            sections.append("**tags:** " + ", ".join(tags if isinstance(tags, list) else [tags]))
        markdown_text = f"# {skill.get('name') or skill.get('title')}\n\n" + "\n\n".join(sections)
        result = md.render_markdown(markdown_text, current_theme())
        self.detail.setHtml(result.html)

    def _set_status(self, status: str) -> None:
        skill = self._current()
        if not skill:
            return
        run(self.api.put, f"/api/skills/{skill.get('id')}", json_body={"status": status},
            on_done=lambda _r: (self.toast(f"Skill {status}", "success"), self.refresh()),
            on_error=self.error)

    def _details(self) -> None:
        skill = self._current()
        if not skill:
            return
        run(self.api.skill, skill.get("id"),
            on_done=lambda payload: info_dialog(
                self, skill.get("name") or "Skill",
                str((payload or {}).get("body") or payload)[:3000]),
            on_error=self.error)

    def _delete(self) -> None:
        skill = self._current()
        if not skill:
            return
        if not confirm(self, "Delete skill?", skill.get("name") or "",
                       yes="Delete", destructive=True):
            return
        run(self.api.delete_skill, skill.get("id"),
            on_done=lambda _r: self.refresh(), on_error=self.error)
