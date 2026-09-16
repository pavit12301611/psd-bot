"""Settings workspace screen: appearance, account, assistant, data, about."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFormLayout, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QPushButton,
    QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from gui import theme as theme_module
from gui.theme import Theme, THEMES, theme_swatch
from gui.views.base import View, card, muted
from gui.widgets.dialogs import choose_directory, confirm, form_dialog, save_file
from gui.workers import run

PREF_THEME = "gui_theme"
PREF_FONT_FAMILY = "gui_font_family"
PREF_FONT_SIZE = "gui_font_size"
PREF_DENSITY = "gui_density"


class SettingsView(View):
    theme_change_requested = Signal(object)      # Theme

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Settings",
            "Appearance, account, assistant behaviour and data — stored per user.",
            parent,
        )
        self.tabs = QTabWidget()
        self.set_content(self.tabs)

        self._build_appearance()
        self._build_account()
        self._build_assistant()
        self._build_data()
        self._build_about()
        self._users: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # appearance
    # ------------------------------------------------------------------ #
    def _build_appearance(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 10, 8, 10)
        layout.setSpacing(14)

        themes_card = card("Theme")
        themes_layout = themes_card.layout_  # type: ignore[attr-defined]
        grid = QGridLayout()
        grid.setSpacing(10)
        self.theme_buttons: Dict[str, QPushButton] = {}
        for index, name in enumerate(THEMES):
            button = QPushButton(name)
            button.setCheckable(True)
            button.setIcon(theme_module.dot_icon(THEMES[name]["red"], 14))
            button.setMinimumWidth(120)
            button.clicked.connect(lambda _=False, theme_name=name: self._pick_theme(theme_name))
            self.theme_buttons[name] = button
            grid.addWidget(button, index // 4, index % 4)
        themes_layout.addLayout(grid)
        themes_layout.addWidget(muted(
            "Themes match the browser app exactly — the same five seed colours, "
            "rendered natively."))
        layout.addWidget(themes_card)

        type_card = card("Type & density")
        type_layout = QFormLayout()
        type_layout.setSpacing(8)
        self.font_family_box = QComboBox()
        self.font_family_box.addItems(["sans", "mono", "serif"])
        type_layout.addRow("Font family", self.font_family_box)
        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(8, 16)
        self.font_size_spin.setValue(10)
        type_layout.addRow("Font size", self.font_size_spin)
        self.density_box = QComboBox()
        self.density_box.addItems(["comfortable", "compact"])
        type_layout.addRow("Density", self.density_box)
        type_card.layout_.addLayout(type_layout)  # type: ignore[attr-defined]
        apply_button = QPushButton("Apply & save")
        apply_button.setObjectName("Primary")
        apply_button.clicked.connect(self._apply_type)
        type_card.layout_.addWidget(apply_button)  # type: ignore[attr-defined]
        layout.addWidget(type_card)
        layout.addStretch(1)
        self.tabs.addTab(page, "Appearance")

    def _pick_theme(self, name: str) -> None:
        for key, button in self.theme_buttons.items():
            button.setChecked(key == name)
        seed = THEMES.get(name, THEMES[theme_module.DEFAULT_THEME])
        theme = Theme.from_seed(
            name, seed,
            font_family=self.font_family_box.currentText(),
            font_size=self.font_size_spin.value(),
            density=self.density_box.currentText(),
        )
        self.theme_change_requested.emit(theme)
        run(self.api.set_pref, PREF_THEME, name, on_done=lambda _r: None,
            on_error=lambda _e: None)

    def _apply_type(self) -> None:
        current = theme_module.current_theme()
        theme = Theme.from_seed(
            current.name, current.seed_dict(),
            font_family=self.font_family_box.currentText(),
            font_size=self.font_size_spin.value(),
            density=self.density_box.currentText(),
        )
        self.theme_change_requested.emit(theme)
        run(self.api.set_pref, PREF_FONT_FAMILY, self.font_family_box.currentText(),
            on_done=lambda _r: None, on_error=lambda _e: None)
        run(self.api.set_pref, PREF_FONT_SIZE, self.font_size_spin.value(),
            on_done=lambda _r: None, on_error=lambda _e: None)
        run(self.api.set_pref, PREF_DENSITY, self.density_box.currentText(),
            on_done=lambda _r: None, on_error=lambda _e: None)
        self.toast("Appearance updated", "success")

    def sync_theme_ui(self, theme: Theme) -> None:
        button = self.theme_buttons.get(theme.name)
        if button:
            button.setChecked(True)
        self.font_family_box.setCurrentText(theme.font_family or "sans")
        self.font_size_spin.setValue(theme.font_size or 10)
        self.density_box.setCurrentText(theme.density or "comfortable")

    # ------------------------------------------------------------------ #
    # account
    # ------------------------------------------------------------------ #
    def _build_account(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 10, 8, 10)
        layout.setSpacing(14)

        password_card = card("Password")
        password_layout = password_card.layout_  # type: ignore[attr-defined]
        form = QFormLayout()
        form.setSpacing(8)
        self.current_password = QLineEdit()
        self.current_password.setEchoMode(QLineEdit.Password)
        self.new_password = QLineEdit()
        self.new_password.setEchoMode(QLineEdit.Password)
        self.confirm_password = QLineEdit()
        self.confirm_password.setEchoMode(QLineEdit.Password)
        form.addRow("Current", self.current_password)
        form.addRow("New", self.new_password)
        form.addRow("Repeat", self.confirm_password)
        password_layout.addLayout(form)
        change_button = QPushButton("Change password")
        change_button.setObjectName("Primary")
        change_button.clicked.connect(self._change_password)
        password_layout.addWidget(change_button)
        layout.addWidget(password_card)

        self.security_card = card("Security")
        security_layout = self.security_card.layout_  # type: ignore[attr-defined]
        self.two_fa_label = QLabel("2FA: checking…")
        self.two_fa_label.setObjectName("Muted")
        security_layout.addWidget(self.two_fa_label)
        layout.addWidget(self.security_card)

        self.users_card = card("Users (admin)")
        users_layout = self.users_card.layout_  # type: ignore[attr-defined]
        self.users_table = QTableWidget(0, 3)
        self.users_table.setHorizontalHeaderLabels(["Username", "Admin", "Privileges"])
        self.users_table.verticalHeader().setVisible(False)
        self.users_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.users_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        users_layout.addWidget(self.users_table)
        users_buttons = QHBoxLayout()
        add_user = QPushButton("+ Add user")
        add_user.clicked.connect(self._add_user)
        users_buttons.addWidget(add_user)
        toggle_admin = QPushButton("Toggle admin")
        toggle_admin.clicked.connect(self._toggle_admin)
        users_buttons.addWidget(toggle_admin)
        delete_user = QPushButton("Delete")
        delete_user.setObjectName("Danger")
        delete_user.clicked.connect(self._delete_user)
        users_buttons.addWidget(delete_user)
        users_buttons.addStretch(1)
        users_layout.addLayout(users_buttons)
        layout.addWidget(self.users_card)
        layout.addStretch(1)
        self.tabs.addTab(page, "Account")

    def _change_password(self) -> None:
        current = self.current_password.text()
        new = self.new_password.text()
        if new != self.confirm_password.text():
            self.toast("New passwords do not match", "error")
            return
        if len(new) < 8:
            self.toast("Password must be at least 8 characters", "error")
            return
        run(self.api.change_password, current, new,
            on_done=lambda _r: (self.toast("Password changed", "success"),
                                self.current_password.clear(), self.new_password.clear(),
                                self.confirm_password.clear()),
            on_error=self.error)

    def _add_user(self) -> None:
        values = form_dialog(self, "Add user", [
            ("username", "Username", "text", ""),
            ("password", "Password", "password", ""),
            ("is_admin", "Administrator", "check", False),
        ])
        if not values or not values.get("username") or not values.get("password"):
            return
        run(self.api.create_user, values["username"], values["password"],
            bool(values.get("is_admin")),
            on_done=lambda _r: (self.toast("User created", "success"), self._load_users()),
            on_error=self.error)

    def _current_username(self) -> str:
        row = self.users_table.currentRow()
        if row < 0:
            return ""
        item = self.users_table.item(row, 0)
        return item.text() if item else ""

    def _toggle_admin(self) -> None:
        username = self._current_username()
        if not username:
            return
        user = next((u for u in self._users if u.get("username") == username), {})
        run(self.api.set_admin, username, not bool(user.get("is_admin")),
            on_done=lambda _r: self._load_users(), on_error=self.error)

    def _delete_user(self) -> None:
        username = self._current_username()
        if not username:
            return
        if not confirm(self, "Delete user?", username, yes="Delete", destructive=True):
            return
        run(self.api.delete_user, username,
            on_done=lambda _r: self._load_users(), on_error=self.error)

    def _load_users(self) -> None:
        run(self._users_payload, on_done=self._users_loaded, on_error=lambda _e: None)

    def _users_payload(self) -> Dict[str, Any]:
        users = self.api.users()
        two_fa = self.api.two_fa_status()
        return {"users": users, "two_fa": two_fa}

    def _users_loaded(self, payload: Dict[str, Any]) -> None:
        self._users = payload.get("users") or []
        two_fa = payload.get("two_fa") or {}
        self.two_fa_label.setText(
            "Two-factor authentication: " + ("enabled ✅" if two_fa.get("enabled") else "disabled"))
        self.users_table.setRowCount(len(self._users))
        for row, user in enumerate(self._users):
            privileges = user.get("privileges") or {}
            restricted = "restricted" if privileges.get("allowed_models_restricted") else "full"
            values = [user.get("username") or "",
                      "yes" if user.get("is_admin") else "no",
                      restricted]
            for column, value in enumerate(values):
                self.users_table.setItem(row, column, QTableWidgetItem(value))

    # ------------------------------------------------------------------ #
    # assistant
    # ------------------------------------------------------------------ #
    def _build_assistant(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 10, 8, 10)
        layout.setSpacing(14)
        self.assistant_card = card("Assistant features")
        assistant_layout = self.assistant_card.layout_  # type: ignore[attr-defined]
        form = QFormLayout()
        form.setSpacing(8)
        self.image_gen_box = QCheckBox("Image generation")
        self.vision_box = QCheckBox("Vision (image understanding)")
        self.tts_box = QCheckBox("Text to speech")
        self.stt_box = QCheckBox("Speech to text")
        self.tts_provider_box = QComboBox()
        self.tts_provider_box.addItems(["disabled", "openai", "edge", "local"])
        self.search_provider_box = QComboBox()
        self.search_provider_box.addItems(["searxng", "duckduckgo", "brave", "tavily", "serper", "disabled"])
        self.search_count_spin = QSpinBox()
        self.search_count_spin.setRange(1, 20)
        form.addRow(self.image_gen_box)
        form.addRow(self.vision_box)
        form.addRow(self.tts_box)
        form.addRow("TTS provider", self.tts_provider_box)
        form.addRow(self.stt_box)
        form.addRow("Web search provider", self.search_provider_box)
        form.addRow("Search results", self.search_count_spin)
        assistant_layout.addLayout(form)
        save_button = QPushButton("Save assistant settings")
        save_button.setObjectName("Primary")
        save_button.clicked.connect(self._save_assistant)
        assistant_layout.addWidget(save_button)
        layout.addWidget(self.assistant_card)
        layout.addStretch(1)
        self.tabs.addTab(page, "Assistant")

    def _save_assistant(self) -> None:
        payload = {
            "image_gen_enabled": self.image_gen_box.isChecked(),
            "vision_enabled": self.vision_box.isChecked(),
            "tts_enabled": self.tts_box.isChecked(),
            "tts_provider": self.tts_provider_box.currentText(),
            "stt_enabled": self.stt_box.isChecked(),
            "search_provider": self.search_provider_box.currentText(),
            "search_result_count": self.search_count_spin.value(),
        }
        run(self.api.save_auth_settings, payload,
            on_done=lambda _r: self.toast("Assistant settings saved", "success"),
            on_error=self.error)

    def _load_assistant(self) -> None:
        run(self.api.auth_settings, on_done=self._assistant_loaded, on_error=lambda _e: None)

    def _assistant_loaded(self, settings: Dict[str, Any]) -> None:
        self.image_gen_box.setChecked(bool(settings.get("image_gen_enabled")))
        self.vision_box.setChecked(bool(settings.get("vision_enabled")))
        self.tts_box.setChecked(bool(settings.get("tts_enabled")))
        self.stt_box.setChecked(bool(settings.get("stt_enabled")))
        for box, key in ((self.tts_provider_box, "tts_provider"),
                         (self.search_provider_box, "search_provider")):
            index = box.findText(str(settings.get(key) or ""))
            if index >= 0:
                box.setCurrentIndex(index)
        self.search_count_spin.setValue(int(settings.get("search_result_count") or 5))

    # ------------------------------------------------------------------ #
    # data
    # ------------------------------------------------------------------ #
    def _build_data(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 10, 8, 10)
        layout.setSpacing(14)

        stats_card = card("Storage")
        stats_layout = stats_card.layout_  # type: ignore[attr-defined]
        self.stats_label = QLabel("Loading…")
        self.stats_label.setObjectName("Muted")
        self.stats_label.setWordWrap(True)
        stats_layout.addWidget(self.stats_label)
        layout.addWidget(stats_card)

        docs_card = card("Personal documents (RAG index)")
        docs_layout = docs_card.layout_  # type: ignore[attr-defined]
        self.docs_list = QListWidget()
        self.docs_list.setMaximumHeight(140)
        docs_layout.addWidget(self.docs_list)
        docs_buttons = QHBoxLayout()
        add_dir = QPushButton("+ Add folder")
        add_dir.clicked.connect(self._add_directory)
        docs_buttons.addWidget(add_dir)
        remove_dir = QPushButton("Remove selected")
        remove_dir.setObjectName("Danger")
        remove_dir.clicked.connect(self._remove_directory)
        docs_buttons.addWidget(remove_dir)
        docs_buttons.addStretch(1)
        docs_layout.addLayout(docs_buttons)
        layout.addWidget(docs_card)

        backup_card = card("Backup")
        backup_layout = backup_card.layout_  # type: ignore[attr-defined]
        backup_buttons = QHBoxLayout()
        export_button = QPushButton("Export everything…")
        export_button.clicked.connect(self._export_all)
        backup_buttons.addWidget(export_button)
        import_button = QPushButton("Import…")
        import_button.clicked.connect(self._import_all)
        backup_buttons.addWidget(import_button)
        backup_buttons.addStretch(1)
        backup_layout.addLayout(backup_buttons)
        layout.addWidget(backup_card)
        layout.addStretch(1)
        self.tabs.addTab(page, "Data")

    def _load_data(self) -> None:
        run(self._data_payload, on_done=self._data_loaded, on_error=lambda _e: None)

    def _data_payload(self) -> Dict[str, Any]:
        db = self.api.db_stats()
        uploads = self.api.upload_stats()
        personal = self.api.personal_docs()
        return {"db": db, "uploads": uploads, "personal": personal}

    def _data_loaded(self, payload: Dict[str, Any]) -> None:
        db = payload.get("db") or {}
        uploads = payload.get("uploads") or {}
        personal = payload.get("personal") or {}
        self.stats_label.setText(
            f"Sessions {db.get('total_sessions', 0)} · messages {db.get('total_messages', 0)} · "
            f"memories {db.get('total_memories', 0)} · database {db.get('database_size_mb', 0)} MB\n"
            f"Uploads {uploads.get('total_files', 0)} files · {uploads.get('total_size_mb', 0)} MB "
            f"(auto-clean after {uploads.get('cleanup_days', 30)} days)")
        self.docs_list.clear()
        for directory in personal.get("directories") or []:
            path = directory.get("path") if isinstance(directory, dict) else str(directory)
            self.docs_list.addItem(str(path))

    def _add_directory(self) -> None:
        path = choose_directory(self, "Index a folder for RAG")
        if not path:
            return
        run(self.api.add_personal_directory, path,
            on_done=lambda _r: (self.toast("Folder indexed", "success"), self._load_data()),
            on_error=self.error)

    def _remove_directory(self) -> None:
        item = self.docs_list.currentItem()
        if not item:
            return
        run(self.api.remove_personal_directory, item.text(),
            on_done=lambda _r: self._load_data(), on_error=self.error)

    def _export_all(self) -> None:
        path = save_file(self, "Export psd.ai data", "psd-ai-backup.json", "JSON files (*.json)")
        if not path:
            return

        def _do() -> None:
            data = self.api.get("/api/export")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)

        run(_do, on_done=lambda _r: self.toast(f"Exported to {path}", "success"),
            on_error=self.error)

    def _import_all(self) -> None:
        from gui.widgets.dialogs import choose_files

        paths = choose_files(self, "Import backup", "JSON files (*.json)")
        if not paths:
            return
        if not confirm(self, "Import backup?", "Existing data may be merged/overwritten.",
                       yes="Import"):
            return

        def _do() -> Any:
            with open(paths[0], "rb") as handle:
                payload = json.load(handle)
            return self.api.post("/api/import", json_body=payload)

        run(_do, on_done=lambda _r: self.toast("Import complete", "success"),
            on_error=self.error)

    # ------------------------------------------------------------------ #
    # about
    # ------------------------------------------------------------------ #
    def _build_about(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 10, 8, 10)
        layout.setSpacing(12)
        about_card = card("About")
        about_layout = about_card.layout_  # type: ignore[attr-defined]
        self.about_label = QLabel("")
        self.about_label.setObjectName("Muted")
        self.about_label.setWordWrap(True)
        self.about_label.setTextFormat(Qt.RichText)
        about_layout.addWidget(self.about_label)
        layout.addWidget(about_card)
        layout.addStretch(1)
        self.tabs.addTab(page, "About")

    def _load_about(self) -> None:
        from gui import __version__ as gui_version

        status = self.api.be.status()
        version = self.api.version()
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.about_label.setText(
            f"<b>psd.ai desktop</b> v{gui_version} · backend v{version or '?'}<br/>"
            f"App folder: {base_dir}<br/>"
            f"Backend: in-process ASGI (no port, no localhost, no browser)<br/>"
            f"Import {status.import_seconds:.1f}s · startup {status.startup_seconds:.1f}s · "
            f"{status.requests} requests · {status.streams} streams<br/>"
            f"License: AGPL-3.0-or-later")

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        self._load_users()
        self._load_assistant()
        self._load_data()
        self._load_about()
        self.sync_theme_ui(theme_module.current_theme())
