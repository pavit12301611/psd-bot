"""Application bootstrap for the psd.ai desktop.

Flow: single-instance check → splash → in-process backend start (no port!) →
auth status → first-run setup / login window → main shell. The theme is loaded
from the user's prefs (falling back to a local QSettings value for the login
screen) and applied to the whole app.
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import (
    QApplication, QDialog, QLabel, QMessageBox, QProgressBar,
    QVBoxLayout, QWidget,
)

from gui import __version__, logbus, theme as theme_module
from gui.api import Api, get_api
from gui.backend import BackendError, get_backend
from gui.shell import MainWindow
from gui.theme import Theme, brand_icon, current_theme
from gui.views.login import LoginView
from gui.views.settings import PREF_DENSITY, PREF_FONT_FAMILY, PREF_FONT_SIZE, PREF_THEME
from gui.widgets.dialogs import error_dialog

logger = logging.getLogger("psd.gui.app")

SINGLE_INSTANCE_KEY = "psd-ai-desktop-single-instance"


class Splash(QDialog):
    """A frameless startup card shown while the engine imports."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setModal(False)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setFixedSize(430, 210)
        theme = current_theme()
        self.setStyleSheet(
            f"Splash {{ background-color: {theme.surface_alt};"
            f" border: 1px solid {theme.accent}; border-radius: 12px; }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(10)

        header = QLabel("⛵  psd.ai")
        header.setStyleSheet(
            f"color: {theme.accent}; font-size: 24pt; font-weight: 700; background: transparent;")
        layout.addWidget(header)
        self.message = QLabel("Starting your workspace…")
        self.message.setStyleSheet(f"color: {theme.text}; background: transparent;")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.detail = QLabel("")
        self.detail.setStyleSheet(f"color: {theme.text_faint}; background: transparent;")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        layout.addWidget(self.progress)
        layout.addStretch(1)

    def set_message(self, message: str, detail: str = "") -> None:
        self.message.setText(message)
        if detail:
            self.detail.setText(detail)


def _load_saved_theme(api: Api) -> Theme:
    """Theme = per-user pref → local settings → default."""
    name = theme_module.DEFAULT_THEME
    font_family = "sans"
    font_size = 10
    density = "comfortable"
    try:
        from PySide6.QtCore import QSettings

        settings = QSettings()
        name = str(settings.value(PREF_THEME, name) or name)
        font_family = str(settings.value(PREF_FONT_FAMILY, font_family) or font_family)
        font_size = int(settings.value(PREF_FONT_SIZE, font_size) or font_size)
        density = str(settings.value(PREF_DENSITY, density) or density)
    except Exception:  # noqa: BLE001
        pass
    try:
        name = str(api.pref(PREF_THEME) or name)
        font_family = str(api.pref(PREF_FONT_FAMILY) or font_family)
        font_size = int(api.pref(PREF_FONT_SIZE) or font_size)
        density = str(api.pref(PREF_DENSITY) or density)
    except Exception:  # noqa: BLE001
        pass
    seed = theme_module.THEMES.get(name, theme_module.THEMES[theme_module.DEFAULT_THEME])
    return Theme.from_seed(name, seed, font_family=font_family,
                           font_size=font_size, density=density)


def _persist_theme_locally(theme: Theme) -> None:
    try:
        from PySide6.QtCore import QSettings

        settings = QSettings()
        settings.setValue(PREF_THEME, theme.name)
        settings.setValue(PREF_FONT_FAMILY, theme.font_family)
        settings.setValue(PREF_FONT_SIZE, theme.font_size)
        settings.setValue(PREF_DENSITY, theme.density)
    except Exception:  # noqa: BLE001
        pass


class DesktopApp:
    """Owns QApplication, splash, login and shell windows."""

    def __init__(self, argv: Optional[list] = None) -> None:
        self.qapp = QApplication(argv if argv is not None else sys.argv[:1])
        self.qapp.setApplicationName("psd.ai")
        self.qapp.setApplicationVersion(__version__)
        self.qapp.setOrganizationName("psd.ai")
        self.qapp.setQuitOnLastWindowClosed(False)
        self.api = get_api()
        self.splash: Optional[Splash] = None
        self.login: Optional[LoginView] = None
        self.shell: Optional[MainWindow] = None
        self._server: Optional[QLocalServer] = None
        self._backend_error: str = ""

    # ------------------------------------------------------------------ #
    def ensure_single_instance(self) -> bool:
        self._server = QLocalServer(self.qapp)
        self._server.setSocketOptions(QLocalServer.WorldAccessOption)
        if not self._server.listen(SINGLE_INSTANCE_KEY):
            # Another instance already owns the name: ping it and exit.
            from PySide6.QtNetwork import QLocalSocket

            socket = QLocalSocket(self.qapp)
            socket.connectToServer(SINGLE_INSTANCE_KEY)
            if socket.waitForConnected(1500):
                socket.write(b"show")
                socket.flush()
                socket.waitForBytesWritten(1000)
            socket.disconnectFromServer()
            return False
        self._server.newConnection.connect(self._focus_existing)
        return True

    def _focus_existing(self) -> None:
        if self.shell is not None:
            self.shell.show()
            self.shell.raise_()
            self.shell.activateWindow()

    # ------------------------------------------------------------------ #
    def run(self) -> int:
        if not self.ensure_single_instance():
            print("psd.ai is already running — focusing the existing window.")
            return 0
        logbus.get_bus()
        theme_module.set_current(
            Theme.from_seed(theme_module.DEFAULT_THEME,
                            theme_module.THEMES[theme_module.DEFAULT_THEME]))
        self.splash = Splash()
        self.splash.show()
        self.qapp.processEvents()

        backend = get_backend()
        self._start_timer = QTimer(self.qapp)
        self._start_timer.setInterval(120)
        self._start_timer.timeout.connect(lambda: self._poll_startup(backend))
        self._start_timer.start()
        backend_thread_ready = [False]

        def _start() -> None:
            try:
                backend.start(timeout=900)
            except BaseException as exc:  # noqa: BLE001 - surfaced in the GUI
                self._backend_error = f"{type(exc).__name__}: {exc}\n\n{backend.error_trace}"
            finally:
                backend_thread_ready[0] = True

        import threading

        threading.Thread(target=_start, name="backend-start", daemon=True).start()
        self._ready_flag = backend_thread_ready
        return self.qapp.exec()

    def _poll_startup(self, backend) -> None:
        if self.splash is None:
            return
        status = backend.status()
        if status.starting or (not status.ready and not self._backend_error):
            seconds = status.import_seconds or 0.0
            self.splash.set_message(
                "Starting the psd.ai engine…",
                f"importing the workspace in-process (no port, no browser)"
                + (f" · {seconds:.0f}s" if seconds else ""),
            )
            return
        self._start_timer.stop()
        splash, self.splash = self.splash, None
        if splash is not None:
            splash.close()
            splash.deleteLater()
        if self._backend_error:
            error_dialog(
                None, "psd.ai could not start",
                "The in-process backend failed to start. The details below include the "
                "original traceback — usually a missing dependency.",
                self._backend_error)
            self.qapp.quit()
            return
        theme = _load_saved_theme(self.api)
        theme_module.set_current(theme)
        theme.apply_to_app(self.qapp)
        _persist_theme_locally(theme)
        self._enter_after_auth(theme)

    # ------------------------------------------------------------------ #
    def _enter_after_auth(self, theme: Theme) -> None:
        from gui.workers import run

        run(self.api.auth_status, on_done=lambda status: self._auth_ready(status, theme),
            on_error=self._auth_failed)

    def _auth_failed(self, exc: BaseException) -> None:
        error_dialog(None, "psd.ai", f"Could not read auth status: {exc}")
        self.qapp.quit()

    def _auth_ready(self, status: dict, theme: Theme) -> None:
        if status.get("authenticated"):
            self._show_shell(status.get("username") or "", theme)
            return
        self.login = LoginView(self.api)
        self.login.configure_from_status(status)
        self.login.logged_in.connect(lambda username: self._on_logged_in(username, theme))
        self.login.resize(760, 620)
        self.login.show()

    def _on_logged_in(self, username: str, theme: Theme) -> None:
        login, self.login = self.login, None
        if login is not None:
            login.close()
            login.deleteLater()
        from gui.workers import run

        def _show(loaded: Theme) -> None:
            self._show_shell(username, loaded or theme)

        run(lambda: _load_saved_theme(self.api),
            on_done=_show, on_error=lambda _exc: _show(theme))

    def _show_shell(self, username: str, theme: Theme) -> None:
        theme_module.set_current(theme)
        theme.apply_to_app(self.qapp)
        _persist_theme_locally(theme)
        self.shell = MainWindow(username or self.api.auth_status().get("username") or "")
        self.shell.apply_theme(theme)
        self.shell.show()
        self.qapp.aboutToQuit.connect(self._shutdown)

    # ------------------------------------------------------------------ #
    def _shutdown(self) -> None:
        try:
            if self.shell is not None:
                self.shell._shutdown_children()  # noqa: SLF001
        except Exception:  # noqa: BLE001
            logger.debug("shell shutdown failed", exc_info=True)
        try:
            get_backend().stop(timeout=20)
        except Exception:  # noqa: BLE001
            logger.debug("backend stop failed", exc_info=True)
        logbus.uninstall()


def main(argv: Optional[list] = None) -> int:
    """Entry point used by ``psd_gui.py`` and ``python -m gui``."""
    application = DesktopApp(argv)
    return application.run()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
