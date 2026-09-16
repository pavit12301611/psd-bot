"""The chat composer: message input, mode/toggles, model picker, send/stop."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QMenu, QPlainTextEdit, QPushButton,
    QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from gui.theme import current_theme
from gui.widgets.dialogs import choose_files


class Composer(QFrame):
    """Bottom input bar for the chat view."""

    send_requested = Signal(str)
    stop_requested = Signal()
    attach_requested = Signal(list)          # list of file paths
    model_changed = Signal(str, str, str)    # endpoint_id, endpoint_url, model

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Composer")
        self._busy = False
        self._routes: List[Dict[str, Any]] = []
        self._attachments: List[Dict[str, Any]] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(8)

        # -- input --------------------------------------------------------- #
        self.input = QPlainTextEdit()
        self.input.setObjectName("ComposerInput")
        self.input.setPlaceholderText(
            "Message psd.ai…   (Enter to send, Shift+Enter for a new line)")
        self.input.setFixedHeight(74)
        self.input.setTabChangesFocus(False)
        outer.addWidget(self.input)

        # -- toggle chips --------------------------------------------------- #
        chip_row = QHBoxLayout()
        chip_row.setSpacing(6)
        self.mode_agent = self._chip("Agent", "Let the model use tools (files, notes, calendar…)")
        self.chip_web = self._chip("Web", "Allow web search / browsing this turn")
        self.chip_bash = self._chip("Bash", "Allow shell commands (ask first)")
        self.chip_rag = self._chip("RAG", "Search your personal documents")
        self.chip_research = self._chip("Deep research", "Multi-step research report")
        self.chip_incognito = self._chip("Incognito", "Do not save this conversation")
        self.chip_plan = self._chip("Plan", "Draft a plan before executing")
        for chip in (self.mode_agent, self.chip_web, self.chip_bash, self.chip_rag,
                     self.chip_research, self.chip_incognito, self.chip_plan):
            chip_row.addWidget(chip)
        chip_row.addStretch(1)

        self.attach_button = QPushButton("⎘ attach")
        self.attach_button.setObjectName("Ghost")
        self.attach_button.setToolTip("Attach files to this message")
        self.attach_button.clicked.connect(self._pick_files)
        chip_row.addWidget(self.attach_button)

        self.attach_label = QLabel("")
        self.attach_label.setObjectName("MetaLine")
        chip_row.addWidget(self.attach_label)
        outer.addLayout(chip_row)

        # -- bottom row ------------------------------------------------------ #
        bottom = QHBoxLayout()
        bottom.setSpacing(8)

        self.model_box = QComboBox()
        self.model_box.setMinimumWidth(220)
        self.model_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.model_box.setToolTip("Model used for this chat")
        self.model_box.currentIndexChanged.connect(self._on_model_changed)
        bottom.addWidget(self.model_box, 1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("MetaLine")
        bottom.addWidget(self.status_label)

        self.send_button = QPushButton("Send  ⏎")
        self.send_button.setObjectName("Primary")
        self.send_button.setMinimumWidth(110)
        self.send_button.clicked.connect(self._on_send_clicked)
        bottom.addWidget(self.send_button)
        outer.addLayout(bottom)

        self.input.installEventFilter(self)

    # -- helpers ------------------------------------------------------------ #
    def _chip(self, text: str, tooltip: str) -> QPushButton:
        chip = QPushButton(text)
        chip.setObjectName("Chip")
        chip.setCheckable(True)
        chip.setToolTip(tooltip)
        return chip

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt API
        from PySide6.QtCore import QEvent

        if obj is self.input and event.type() == QEvent.KeyPress:
            key = event.key()
            modifiers = event.modifiers()
            if key in (Qt.Key_Return, Qt.Key_Enter):
                if modifiers & Qt.ShiftModifier:
                    return False
                self._on_send_clicked()
                return True
            if key == Qt.Key_Escape and self._busy:
                self.stop_requested.emit()
                return True
        return super().eventFilter(obj, event)

    # -- model picker -------------------------------------------------------- #
    def set_models(self, payload: Dict[str, Any], default: Dict[str, Any]) -> None:
        """Populate from ``GET /api/models`` keeping the current selection."""
        items: List[Dict[str, Any]] = (payload or {}).get("items", []) or []
        previous = self.current_route()
        self._routes = []
        self.model_box.blockSignals(True)
        self.model_box.clear()

        seen = set()
        for item in items:
            endpoint_id = item.get("endpoint_id") or ""
            endpoint_name = item.get("endpoint_name") or item.get("host") or "endpoint"
            url = item.get("url") or ""
            models = item.get("models_display") or item.get("models") or []
            if item.get("offline"):
                label = f"{endpoint_name} — offline"
                self.model_box.addItem(label)
                self._routes.append({"endpoint_id": endpoint_id, "endpoint_url": url,
                                     "model": "", "offline": True})
                continue
            if not models:
                label = f"{endpoint_name} — (no models listed)"
                if label not in seen:
                    seen.add(label)
                    self.model_box.addItem(label)
                    self._routes.append({"endpoint_id": endpoint_id, "endpoint_url": url,
                                         "model": "", "offline": False})
                continue
            for model in models:
                label = f"{model}  ·  {endpoint_name}"
                if label in seen:
                    continue
                seen.add(label)
                self.model_box.addItem(label)
                self._routes.append({"endpoint_id": endpoint_id, "endpoint_url": url,
                                     "model": model, "offline": False})
        if not self._routes:
            self.model_box.addItem("No models configured")
            self._routes.append({"endpoint_id": "", "endpoint_url": "", "model": "",
                                 "offline": True})
        self.model_box.blockSignals(False)

        target = default or {}
        wanted_model = target.get("model") or ""
        wanted_ep = target.get("endpoint_id") or ""
        index = -1
        if wanted_model:
            for i, route in enumerate(self._routes):
                if route["model"] == wanted_model and (
                        not wanted_ep or route["endpoint_id"] == wanted_ep):
                    index = i
                    break
        if index < 0 and wanted_ep:
            for i, route in enumerate(self._routes):
                if route["endpoint_id"] == wanted_ep:
                    index = i
                    break
        if index < 0 and previous:
            for i, route in enumerate(self._routes):
                if (route["model"], route["endpoint_id"]) == (
                        previous.get("model"), previous.get("endpoint_id")):
                    index = i
                    break
        self.model_box.setCurrentIndex(max(0, index))
        self._on_model_changed(self.model_box.currentIndex())

    def current_route(self) -> Dict[str, Any]:
        index = self.model_box.currentIndex()
        if 0 <= index < len(self._routes):
            return self._routes[index]
        return {}

    def _on_model_changed(self, index: int) -> None:
        route = self.current_route()
        self.model_changed.emit(
            route.get("endpoint_id", ""), route.get("endpoint_url", ""),
            route.get("model", ""))
        offline = bool(route.get("offline"))
        self.send_button.setEnabled(not offline or self._busy)
        if offline and not self._busy:
            self.status_label.setText("no model selected")
        else:
            self.status_label.setText("")

    # -- attachments ---------------------------------------------------------- #
    def _pick_files(self) -> None:
        paths = choose_files(self, "Attach files")
        if paths:
            self.attach_requested.emit(paths)

    def set_attachments(self, attachments: List[Dict[str, Any]]) -> None:
        self._attachments = attachments
        names = [a.get("name") or a.get("file_id", "")[:8] for a in attachments]
        self.attach_label.setText(", ".join(names) if names else "")

    def clear_attachments(self) -> None:
        self._attachments = []
        self.attach_label.setText("")

    @property
    def attachments(self) -> List[Dict[str, Any]]:
        return self._attachments

    # -- toggles --------------------------------------------------------------- #
    def toggles(self) -> Dict[str, bool]:
        return {
            "agent": self.mode_agent.isChecked(),
            "web": self.chip_web.isChecked(),
            "bash": self.chip_bash.isChecked(),
            "rag": self.chip_rag.isChecked(),
            "research": self.chip_research.isChecked(),
            "incognito": self.chip_incognito.isChecked(),
            "plan": self.chip_plan.isChecked(),
        }

    def set_toggles(self, toggles: Dict[str, bool]) -> None:
        mapping = {
            "agent": self.mode_agent, "web": self.chip_web, "bash": self.chip_bash,
            "rag": self.chip_rag, "research": self.chip_research,
            "incognito": self.chip_incognito, "plan": self.chip_plan,
        }
        for key, value in (toggles or {}).items():
            widget = mapping.get(key)
            if widget is not None:
                widget.setChecked(bool(value))

    # -- send / stop ------------------------------------------------------------- #
    def message(self) -> str:
        return self.input.toPlainText()

    def clear_input(self) -> None:
        self.input.clear()

    def focus_input(self) -> None:
        self.input.setFocus()
        cursor = self.input.textCursor()
        cursor.movePosition(cursor.End)
        self.input.setTextCursor(cursor)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        theme = current_theme()
        if busy:
            self.send_button.setText("Stop  ⎋")
            self.send_button.setObjectName("Danger")
            self.send_button.setEnabled(True)
        else:
            self.send_button.setText("Send  ⏎")
            self.send_button.setObjectName("Primary")
            self._on_model_changed(self.model_box.currentIndex())
        self.send_button.style().unpolish(self.send_button)
        self.send_button.style().polish(self.send_button)

    def _on_send_clicked(self) -> None:
        if self._busy:
            self.stop_requested.emit()
            return
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.send_requested.emit(text)
