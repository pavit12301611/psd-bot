"""Diagnostics workspace screen: service health + live application logs."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from gui import logbus
from gui.theme import SEMANTIC, current_theme
from gui.views.base import View, card
from gui.widgets.dialogs import info_dialog, save_file
from gui.workers import run

LEVEL_COLORS = {
    "DEBUG": "#828997",
    "INFO": "#9cdef2",
    "WARNING": "#f0ad4e",
    "ERROR": "#ff4444",
    "CRITICAL": "#ff4444",
}


class LogsView(View):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Diagnostics",
            "Service health and the live application log — no console window needed.",
            parent,
        )
        self.health_card = card("Services")
        health_layout = self.health_card.layout_  # type: ignore[attr-defined]
        self.health_label = QLabel("Checking…")
        self.health_label.setObjectName("Muted")
        self.health_label.setWordWrap(True)
        self.health_label.setTextFormat(Qt.RichText)
        health_layout.addWidget(self.health_label)
        self.content_layout.addWidget(self.health_card)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(QLabel("Level:"))
        self.level_box = QComboBox()
        self.level_box.addItems(["INFO", "WARNING", "ERROR", "DEBUG"])
        self.level_box.setCurrentText("INFO")
        self.level_box.currentTextChanged.connect(self._refilter)
        controls.addWidget(self.level_box)
        self.follow_box = QComboBox()
        self.follow_box.addItems(["follow", "paused"])
        controls.addWidget(self.follow_box)
        refresh = QPushButton("Refresh services")
        refresh.clicked.connect(self.refresh)
        controls.addWidget(refresh)
        backend_logs = QPushButton("Backend log file")
        backend_logs.clicked.connect(self._show_backend_logs)
        controls.addWidget(backend_logs)
        save_button = QPushButton("Save log…")
        save_button.clicked.connect(self._save_log)
        controls.addWidget(save_button)
        clear_button = QPushButton("Clear view")
        clear_button.clicked.connect(self._clear)
        controls.addWidget(clear_button)
        controls.addStretch(1)
        controls_wrap = QWidget()
        controls_wrap.setLayout(controls)
        self.content_layout.addWidget(controls_wrap)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Time", "Level", "Logger", "Message"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.set_content(self.table)

        self._records: List[logbus.LogRecord] = []
        bus = logbus.get_bus()
        bus.record.connect(self._on_record)
        for record in bus.history(600):
            self._append(record)

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        self.run(self.api.diagnostics_services, on_done=self._health_loaded)

    def _health_loaded(self, payload: Dict[str, Any]) -> None:
        services = (payload or {}).get("services") or []
        overall = (payload or {}).get("overall") or "?"
        color = {"up": SEMANTIC["success"], "degraded": SEMANTIC["warning"],
                 "down": SEMANTIC["error"]}.get(overall, SEMANTIC["info"])
        lines = [f"<b style='color:{color};'>overall: {overall}</b>"]
        for service in services:
            status = service.get("status") or "?"
            status_color = {"up": SEMANTIC["success"], "ok": SEMANTIC["success"],
                            "degraded": SEMANTIC["warning"],
                            "down": SEMANTIC["error"]}.get(status, SEMANTIC["info"])
            lines.append(
                f"• <b>{service.get('name')}</b> — "
                f"<span style='color:{status_color};'>{status}</span>: "
                f"{service.get('detail') or ''}")
        self.health_label.setText("<br/>".join(lines))

    # -- live log ------------------------------------------------------------ #
    def _on_record(self, record: Any) -> None:
        self._append(record)

    def _append(self, record: logbus.LogRecord) -> None:
        minimum = self.level_box.currentText()
        order = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if record.level < getattr(logging, minimum, logging.INFO):
            return
        self._records.append(record)
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = [record.time_text, record.level_name, record.logger_name, record.message]
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value)[:1000])
            if column == 1:
                item.setForeground(QColor(LEVEL_COLORS.get(record.level_name, current_theme().text)))
            self.table.setItem(row, column, item)
        if self.table.rowCount() > 4000:
            self.table.removeRow(0)
            self._records = self._records[-4000:]
        if self.follow_box.currentText() == "follow":
            self.table.scrollToBottom()

    def _refilter(self, _level: str) -> None:
        self.table.setRowCount(0)
        records, self._records = self._records, []
        for record in records:
            self._append(record)

    def _clear(self) -> None:
        self.table.setRowCount(0)
        self._records = []

    def _save_log(self) -> None:
        path = save_file(self, "Save log", "psd-ai-gui.log", "Log files (*.log *.txt)")
        if not path:
            return
        lines = [f"{r.time_text} {r.level_name:8s} {r.logger_name:24s} {r.message}"
                 for r in self._records]
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
        self.toast(f"Log saved to {path}", "success")

    def _show_backend_logs(self) -> None:
        run(self.api.diagnostics_logs, 400,
            on_done=lambda text: info_dialog(
                self, "Backend log file (tail)", str(text or "(empty)")[-6000:]),
            on_error=self.error)

    def deactivate(self) -> None:
        pass
