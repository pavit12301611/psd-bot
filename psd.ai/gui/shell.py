"""The psd.ai desktop main window: navigation rail, view stack, status bar, tray."""

from __future__ import annotations

import logging
import os
import sys
from typing import Dict, Optional

from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QAction, QCloseEvent, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow, QMenu, QPushButton,
    QSizePolicy, QStackedWidget, QStatusBar, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from gui import __version__
from gui import theme as theme_module
from gui.api import get_api
from gui.theme import Theme, brand_icon, current_theme
from gui.widgets.dialogs import confirm
from gui.workers import run

logger = logging.getLogger("psd.gui.shell")

NAV_SECTIONS = [
    ("WORKSPACE", [
        ("chat", "💬", "Chat"),
        ("documents", "📄", "Documents"),
        ("notes", "🗒", "Notes"),
        ("tasks", "⏱", "Tasks"),
        ("calendar", "📅", "Calendar"),
        ("email", "✉", "Email"),
        ("gallery", "🖼", "Gallery"),
        ("research", "🔬", "Research"),
    ]),
    ("AI & MODELS", [
        ("models", "🧠", "Models"),
        ("localmodels", "⬇", "Local Models"),
        ("memory", "💡", "Memory"),
        ("skills", "🎯", "Skills"),
        ("mcp", "🔌", "MCP Servers"),
    ]),
    ("SYSTEM", [
        ("settings", "⚙", "Settings"),
        ("logs", "🩺", "Diagnostics"),
    ]),
]

VIEW_FACTORIES = {
    "chat": ("gui.views.chat", "ChatView"),
    "documents": ("gui.views.documents", "DocumentsView"),
    "notes": ("gui.views.notes", "NotesView"),
    "tasks": ("gui.views.tasks", "TasksView"),
    "calendar": ("gui.views.calendar", "CalendarView"),
    "email": ("gui.views.email", "EmailView"),
    "gallery": ("gui.views.gallery", "GalleryView"),
    "research": ("gui.views.research", "ResearchView"),
    "models": ("gui.views.models", "ModelsView"),
    "localmodels": ("gui.views.localmodels", "LocalModelsView"),
    "memory": ("gui.views.memory", "MemoryView"),
    "skills": ("gui.views.skills", "SkillsView"),
    "mcp": ("gui.views.mcp", "McpView"),
    "settings": ("gui.views.settings", "SettingsView"),
    "logs": ("gui.views.logs", "LogsView"),
}

HEARTBEAT_MS = 20_000


class MainWindow(QMainWindow):
    """The desktop shell."""

    def __init__(self, username: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.api = get_api()
        self.username = username
        self.setWindowTitle("psd.ai")
        self.setWindowIcon(brand_icon(64, current_theme().accent))
        self.resize(1360, 860)
        self.setMinimumSize(980, 640)
        self._views: Dict[str, QWidget] = {}
        self._nav_buttons: Dict[str, QPushButton] = {}
        self._current_key = ""
        self._closing = False

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_nav())

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self._build_status_bar()
        self._build_menu()
        self._build_tray()

        self.heartbeat = QTimer(self)
        self.heartbeat.setInterval(HEARTBEAT_MS)
        self.heartbeat.timeout.connect(self._beat)
        self.heartbeat.start()

        self.status_timer = QTimer(self)
        self.status_timer.setInterval(5000)
        self.status_timer.timeout.connect(self._update_status)
        self.status_timer.start()

        self.switch("chat")

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #
    def _build_nav(self) -> QFrame:
        rail = QFrame()
        rail.setObjectName("NavRail")
        rail.setFixedWidth(208)
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        brand = QLabel(" psd.ai")
        brand.setObjectName("AppBrand")
        layout.addWidget(brand)

        scroll_area_wrapper = QWidget()
        scroll_layout = QVBoxLayout(scroll_area_wrapper)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(0)
        for section, entries in NAV_SECTIONS:
            label = QLabel(section)
            label.setObjectName("NavSection")
            scroll_layout.addWidget(label)
            for key, glyph, title in entries:
                button = QPushButton(f"  {glyph}   {title}")
                button.setObjectName("NavButton")
                button.setCheckable(True)
                button.setCursor(Qt.PointingHandCursor)
                button.clicked.connect(lambda _=False, view_key=key: self.switch(view_key))
                self._nav_buttons[key] = button
                scroll_layout.addWidget(button)
        scroll_layout.addStretch(1)
        layout.addWidget(scroll_area_wrapper, 1)

        footer = QWidget()
        footer.setObjectName("NavFooter")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(12, 8, 12, 10)
        footer_layout.setSpacing(2)
        self.user_label = QLabel(self.username or "signed in")
        self.user_label.setObjectName("Muted")
        footer_layout.addWidget(self.user_label)
        self.version_label = QLabel(f"desktop {__version__}")
        self.version_label.setObjectName("Faint")
        footer_layout.addWidget(self.version_label)
        layout.addWidget(footer)
        return rail

    def _build_status_bar(self) -> None:
        bar = QStatusBar()
        self.setStatusBar(bar)
        self.engine_pill = QLabel("engine: starting")
        self.engine_pill.setObjectName("StatusPill")
        bar.addWidget(self.engine_pill)
        self.jobs_pill = QLabel("idle")
        self.jobs_pill.setObjectName("StatusPill")
        bar.addWidget(self.jobs_pill)
        bar.addPermanentWidget(QLabel(""))
        self.model_pill = QLabel("")
        self.model_pill.setObjectName("StatusPill")
        bar.addPermanentWidget(self.model_pill)
        self._update_status()

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("&File")
        new_chat = QAction("New chat", self)
        new_chat.setShortcut(QKeySequence("Ctrl+T"))
        new_chat.triggered.connect(self._new_chat)
        file_menu.addAction(new_chat)
        file_menu.addSeparator()
        quit_action = QAction("Quit psd.ai", self)
        quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        quit_action.triggered.connect(self._quit)
        file_menu.addAction(quit_action)

        view_menu = menu_bar.addMenu("&View")
        for key, _glyph, title in NAV_SECTIONS[0][1] + NAV_SECTIONS[1][1] + NAV_SECTIONS[2][1]:
            action = QAction(title, self)
            action.triggered.connect(lambda _=False, view_key=key: self.switch(view_key))
            view_menu.addAction(action)
        view_menu.addSeparator()
        theme_menu = view_menu.addMenu("Theme")
        for name in theme_module.THEMES:
            action = QAction(name, self)
            action.triggered.connect(lambda _=False, theme_name=name: self._apply_theme_name(theme_name))
            theme_menu.addAction(action)

        help_menu = menu_bar.addMenu("&Help")
        about_action = QAction("About psd.ai", self)
        about_action.triggered.connect(self._about)
        help_menu.addAction(about_action)

    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return
        self.tray = QSystemTrayIcon(brand_icon(64, current_theme().accent), self)
        menu = QMenu()
        show_action = menu.addAction("Show psd.ai")
        show_action.triggered.connect(self._show_from_tray)
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self._quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self._show_from_tray()
            if reason == QSystemTrayIcon.Trigger else None)
        self.tray.setToolTip("psd.ai")
        self.tray.show()

    # ------------------------------------------------------------------ #
    # views
    # ------------------------------------------------------------------ #
    def view(self, key: str) -> QWidget:
        existing = self._views.get(key)
        if existing is not None:
            return existing
        module_name, class_name = VIEW_FACTORIES[key]
        import importlib

        module = importlib.import_module(module_name)
        instance = getattr(module, class_name)(self)
        instance.navigate.connect(self.switch)
        if hasattr(instance, "open_document"):
            instance.open_document.connect(self._open_document)
        if hasattr(instance, "theme_change_requested"):
            instance.theme_change_requested.connect(self.apply_theme)
        self._views[key] = instance
        self.stack.addWidget(instance)
        return instance

    def switch(self, key: str) -> None:
        if key not in VIEW_FACTORIES:
            return
        previous = self._views.get(self._current_key)
        if previous is not None and hasattr(previous, "deactivate"):
            previous.deactivate()
        widget = self.view(key)
        self.stack.setCurrentWidget(widget)
        self._current_key = key
        for nav_key, button in self._nav_buttons.items():
            button.setChecked(nav_key == key)
        if hasattr(widget, "activate"):
            widget.activate()

    def _new_chat(self) -> None:
        self.switch("chat")
        chat_view = self._views.get("chat")
        if chat_view is not None and hasattr(chat_view, "new_chat"):
            chat_view.new_chat()

    def _open_document(self, doc_id: str) -> None:
        self.switch("documents")
        documents = self._views.get("documents")
        if documents is not None:
            documents._current = doc_id  # noqa: SLF001 - direct handoff
            run(documents.api.document, doc_id, on_done=documents._document_loaded,  # noqa: SLF001
                on_error=documents.error)

    # ------------------------------------------------------------------ #
    # theme
    # ------------------------------------------------------------------ #
    def _apply_theme_name(self, name: str) -> None:
        seed = theme_module.THEMES.get(name, theme_module.THEMES[theme_module.DEFAULT_THEME])
        current = current_theme()
        theme = Theme.from_seed(name, seed, font_family=current.font_family,
                                font_size=current.font_size, density=current.density)
        self.apply_theme(theme)
        settings_view = self._views.get("settings")
        if settings_view is not None:
            run(settings_view.api.set_pref, "gui_theme", name,
                on_done=lambda _r: None, on_error=lambda _e: None)

    def apply_theme(self, theme: Theme) -> None:
        theme_module.set_current(theme)
        app = QApplication.instance()
        if app is not None:
            theme.apply_to_app(app)
        self.setWindowIcon(brand_icon(64, theme.accent))
        if self.tray is not None:
            self.tray.setIcon(brand_icon(64, theme.accent))
        settings_view = self._views.get("settings")
        if settings_view is not None and hasattr(settings_view, "sync_theme_ui"):
            settings_view.sync_theme_ui(theme)

    # ------------------------------------------------------------------ #
    # status / heartbeat
    # ------------------------------------------------------------------ #
    def _beat(self) -> None:
        run(self.api.heartbeat, on_done=lambda _r: None, on_error=lambda _e: None)

    def _update_status(self) -> None:
        status = self.api.be.status()
        if status.ready:
            self.engine_pill.setText(
                f"engine ready · {status.import_seconds + status.startup_seconds:.1f}s")
            self.engine_pill.setProperty("ok", "true")
            self.engine_pill.setProperty("bad", "false")
        else:
            self.engine_pill.setText("engine starting…")
            self.engine_pill.setProperty("busy", "true")
        from gui.workers import get_runner

        active = get_runner().active_count
        self.jobs_pill.setText(f"{active} job(s)" if active else "idle")
        self.jobs_pill.setProperty("busy", "true" if active else "false")
        self.user_label.setText(self.username or status.user or "signed in")
        for pill in (self.engine_pill, self.jobs_pill):
            pill.style().unpolish(pill)
            pill.style().polish(pill)
        run(self._default_model_text, on_done=self.model_pill.setText,
            on_error=lambda _e: None)

    def _default_model_text(self) -> str:
        default = self.api.default_chat()
        model = default.get("model") or ""
        endpoint = default.get("endpoint_url") or ""
        if not endpoint:
            return "no model configured"
        return f"{model or 'default model'} @ {endpoint.split('//')[-1][:34]}"

    # ------------------------------------------------------------------ #
    # window lifecycle
    # ------------------------------------------------------------------ #
    def _show_from_tray(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _about(self) -> None:
        from gui.widgets.dialogs import info_dialog

        status = self.api.be.status()
        info_dialog(
            self, "About psd.ai",
            f"psd.ai desktop {__version__}\n\n"
            "A native workspace for chat, agents, documents, notes, tasks, calendar, "
            "email, gallery, research and local models.\n\n"
            "The backend runs in-process: no port is opened, no localhost URL, "
            "no browser window.",
            details=f"engine import {status.import_seconds:.2f}s, "
                    f"startup {status.startup_seconds:.2f}s, "
                    f"{status.requests} requests, {status.streams} streams")

    def _quit(self) -> None:
        self._closing = True
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if os.environ.get("QT_QPA_PLATFORM") in ("offscreen", "minimal"):
            self._closing = True          # headless tests: never block on modals
        if not self._closing:
            if self.tray is not None and self.tray.isVisible():
                self.hide()
                self.tray.showMessage(
                    "psd.ai", "Still running in the background. Click the tray icon to open.",
                    brand_icon(64, current_theme().accent), 2500)
                event.ignore()
                return
            if not confirm(self, "Quit psd.ai?",
                           "Running model groups and streams will stop.",
                           yes="Quit"):
                event.ignore()
                return
            self._closing = True
        self._shutdown_children()
        super().closeEvent(event)
        if self._closing:
            app = QApplication.instance()
            if app is not None:
                app.quit()

    def _shutdown_children(self) -> None:
        local_view = self._views.get("localmodels")
        if local_view is not None and hasattr(local_view, "close_group"):
            try:
                local_view.close_group()
            except Exception:  # noqa: BLE001
                logger.debug("local model group shutdown failed", exc_info=True)
        for key, widget in self._views.items():
            if hasattr(widget, "deactivate"):
                try:
                    widget.deactivate()
                except Exception:  # noqa: BLE001
                    logger.debug("view deactivate failed: %s", key, exc_info=True)

    def _shutdown_children(self) -> None:
        """Stop in-flight work (chat turns, model servers) before exit."""
        try:
            self._chat.shutdown()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        try:
            self.close_group()
        except Exception:  # noqa: BLE001
            pass
