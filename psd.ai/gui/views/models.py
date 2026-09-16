"""Models & endpoints workspace screen."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QWidget,
)

from gui.theme import current_theme
from gui.views.base import View, card
from gui.widgets.dialogs import confirm, form_dialog, info_dialog
from gui.workers import run


class ModelsView(View):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Models",
            "Endpoints (Ollama, llama.cpp, LM Studio, OpenAI, Anthropic…) and the default chat model.",
            parent,
        )
        default_card = card("Default chat model")
        default_layout = default_card.layout_  # type: ignore[attr-defined]
        picker = QHBoxLayout()
        picker.setSpacing(8)
        self.endpoint_box = QComboBox()
        self.endpoint_box.setMinimumWidth(240)
        self.endpoint_box.currentIndexChanged.connect(self._endpoint_changed)
        picker.addWidget(self.endpoint_box, 2)
        self.model_box = QComboBox()
        self.model_box.setMinimumWidth(240)
        picker.addWidget(self.model_box, 2)
        apply_button = QPushButton("Set as default")
        apply_button.setObjectName("Primary")
        apply_button.clicked.connect(self._set_default)
        picker.addWidget(apply_button)
        picker.addStretch(1)
        default_layout.addLayout(picker)
        self.default_label = QLabel("")
        self.default_label.setObjectName("MetaLine")
        default_layout.addWidget(self.default_label)
        self.content_layout.addWidget(default_card)

        endpoints_card = card("Endpoints")
        endpoints_layout = endpoints_card.layout_  # type: ignore[attr-defined]
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Name", "Base URL", "Kind", "Status", "Models", "Enabled"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        endpoints_layout.addWidget(self.table)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        for label, handler, style in (
            ("+ Add endpoint", self._add_endpoint, "Primary"),
            ("Edit", self._edit_endpoint, ""),
            ("Enable/disable", self._toggle_endpoint, ""),
            ("Probe", self._probe_endpoint, ""),
            ("Test connection", self._test_endpoint, ""),
            ("Delete", self._delete_endpoint, "Danger"),
        ):
            button = QPushButton(label)
            if style:
                button.setObjectName(style)
            button.clicked.connect(handler)
            buttons.addWidget(button)
        buttons.addStretch(1)
        endpoints_layout.addLayout(buttons)
        self.content_layout.addWidget(endpoints_card, 1)

        self._endpoints: List[Dict[str, Any]] = []
        self._models_payload: Dict[str, Any] = {}
        self._default: Dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        self.run(self._load_all, on_done=self._loaded)

    def _load_all(self) -> Dict[str, Any]:
        endpoints = self.api.endpoints()
        models = self.api.models()
        default = self.api.default_chat()
        return {"endpoints": endpoints, "models": models, "default": default}

    def _loaded(self, payload: Dict[str, Any]) -> None:
        self._endpoints = payload.get("endpoints") or []
        self._models_payload = payload.get("models") or {}
        self._default = payload.get("default") or {}
        theme = current_theme()

        self.table.setRowCount(len(self._endpoints))
        for row, endpoint in enumerate(self._endpoints):
            status = endpoint.get("status") or ("online" if endpoint.get("online") else "offline")
            values = [
                endpoint.get("name") or "",
                endpoint.get("base_url") or "",
                endpoint.get("endpoint_kind") or endpoint.get("category") or "",
                status,
                str(endpoint.get("model_count") or len(endpoint.get("models") or [])),
                "yes" if endpoint.get("is_enabled") else "no",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, endpoint.get("id"))
                if column == 3:
                    color = "#4caf50" if status in ("online", "empty") else "#ff4444"
                    item.setForeground(QColor(color))
                self.table.setItem(row, column, item)

        # default picker
        self.endpoint_box.blockSignals(True)
        self.endpoint_box.clear()
        for item in (self._models_payload.get("items") or []):
            self.endpoint_box.addItem(
                f"{item.get('endpoint_name') or item.get('host')}",
                {"endpoint_id": item.get("endpoint_id"),
                 "endpoint_url": item.get("url"),
                 "models": item.get("models_display") or item.get("models") or []})
        self.endpoint_box.blockSignals(False)
        wanted = self._default.get("endpoint_id") or ""
        for index in range(self.endpoint_box.count()):
            data = self.endpoint_box.itemData(index) or {}
            if data.get("endpoint_id") == wanted:
                self.endpoint_box.setCurrentIndex(index)
                break
        self._endpoint_changed(self.endpoint_box.currentIndex())
        self.default_label.setText(
            f"Current default: {self._default.get('model') or '(first model)'} @ "
            f"{self._default.get('endpoint_url') or '—'}")

    def _endpoint_changed(self, index: int) -> None:
        data = self.endpoint_box.itemData(index) or {}
        models = data.get("models") or []
        self.model_box.clear()
        self.model_box.addItems(models or ["(no models listed)"])
        wanted = self._default.get("model") or ""
        position = self.model_box.findText(wanted)
        if position >= 0:
            self.model_box.setCurrentIndex(position)

    # -- default ------------------------------------------------------------ #
    def _set_default(self) -> None:
        data = self.endpoint_box.currentData() or {}
        endpoint_id = data.get("endpoint_id") or ""
        model = self.model_box.currentText() or ""
        if not endpoint_id:
            self.toast("Pick an endpoint first", "warning")
            return
        run(self.api.set_default_chat, endpoint_id, model,
            on_done=lambda _r: (self.toast("Default model updated", "success"), self.refresh()),
            on_error=self.error)

    # -- endpoint CRUD ------------------------------------------------------- #
    def _current_endpoint(self) -> Dict[str, Any]:
        row = self.table.currentRow()
        if row < 0:
            return {}
        item = self.table.item(row, 0)
        endpoint_id = item.data(Qt.UserRole) if item else None
        return next((e for e in self._endpoints if e.get("id") == endpoint_id), {})

    def _fields(self, endpoint: Optional[Dict[str, Any]] = None) -> Optional[dict]:
        endpoint = endpoint or {}
        values = form_dialog(self, "Edit endpoint" if endpoint else "Add endpoint", [
            ("name", "Name", "text", endpoint.get("name") or ""),
            ("base_url", "Base URL", "text", endpoint.get("base_url") or ""),
            ("api_key", "API key (optional)", "password", ""),
            ("endpoint_kind", "Kind", "combo", ["auto", "openai", "anthropic", "ollama",
                                                 "llamacpp", "vllm", "lmstudio", "custom"]),
            ("model_type", "Model type", "combo", ["llm", "image", "embedding", "stt", "tts"]),
            ("pinned_models", "Pinned model IDs (comma separated)", "text",
             ", ".join(endpoint.get("pinned_models") or [])),
            ("shared", "Share with all users", "check", True),
        ], intro=("Base URL examples: http://127.0.0.1:11434/v1 (Ollama), "
                  "http://127.0.0.1:8080/v1 (llama.cpp), https://api.openai.com/v1"))
        return values

    def _add_endpoint(self) -> None:
        values = self._fields()
        if not values or not values.get("base_url"):
            return
        run(self.api.add_endpoint, values["name"] or values["base_url"],
            values["base_url"], values.get("api_key") or "",
            model_type=values.get("model_type") or "llm",
            endpoint_kind=values.get("endpoint_kind") or "auto",
            pinned_models=values.get("pinned_models") or "",
            shared=bool(values.get("shared")),
            on_done=lambda _r: (self.toast("Endpoint added", "success"), self.refresh()),
            on_error=self.error)

    def _edit_endpoint(self) -> None:
        endpoint = self._current_endpoint()
        if not endpoint:
            return
        values = self._fields(endpoint)
        if not values:
            return
        fields = {
            "name": values.get("name"),
            "base_url": values.get("base_url"),
            "endpoint_kind": values.get("endpoint_kind"),
            "model_type": values.get("model_type"),
            "pinned_models": values.get("pinned_models"),
        }
        if values.get("api_key"):
            fields["api_key"] = values["api_key"]
        run(self.api.update_endpoint, endpoint["id"], fields,
            on_done=lambda _r: (self.toast("Endpoint updated", "success"), self.refresh()),
            on_error=self.error)

    def _toggle_endpoint(self) -> None:
        endpoint = self._current_endpoint()
        if endpoint:
            run(self.api.toggle_endpoint, endpoint["id"],
                on_done=lambda _r: self.refresh(), on_error=self.error)

    def _probe_endpoint(self) -> None:
        endpoint = self._current_endpoint()
        if not endpoint:
            return

        def _probe() -> Dict[str, Any]:
            return self.api.probe_endpoint(endpoint["id"])

        run(_probe, on_done=lambda payload: info_dialog(
            self, "Probe result",
            f"{endpoint.get('name')}: {payload.get('status') or payload.get('online')}\n"
            f"models: {', '.join(payload.get('models') or [])[:400] or '—'}"),
            on_error=self.error)

    def _test_endpoint(self) -> None:
        endpoint = self._current_endpoint()
        if not endpoint:
            return
        run(self.api.test_endpoint, endpoint.get("base_url") or "",
            on_done=lambda payload: info_dialog(
                self, "Connection test", str(payload)[:800]),
            on_error=self.error)

    def _delete_endpoint(self) -> None:
        endpoint = self._current_endpoint()
        if not endpoint:
            return
        if not confirm(self, "Delete endpoint?",
                       f"“{endpoint.get('name')}” will be removed.",
                       yes="Delete", destructive=True):
            return
        run(self.api.delete_endpoint, endpoint["id"],
            on_done=lambda _r: self.refresh(), on_error=self.error)
