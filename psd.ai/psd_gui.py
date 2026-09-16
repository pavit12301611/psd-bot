"""psd_gui.py — the psd.ai desktop application.

A native desktop window (Qt) that replaces the localhost browser interface.
It never re-implements psd.ai logic: it drives the exact same FastAPI backend.

* **embedded** (default) — the backend runs *in-process* (no TCP port, no
  browser).  First-run admin setup, sign-in, chats, sessions, model picker and
  streaming all go through the backend's own AuthManager + routes, so the
  database, sessions, memory and settings stay in the same DATA_DIR the web
  UI already uses.
* **attached** — talk to a server already running on ``--host --port``.
* **serve** — start the server ourselves (``--serve``), then attach to it.

Threading: the backend's asyncio work always runs on the dedicated loop in
:mod:`gui.backend`, never on Qt's loop; streaming results flow back through a
Qt signal, and short awaitable calls are waited on while pumping Qt events so
the window stays responsive.

Run: ``python psd_gui.py`` (or ``python psd_gui.py --help`` for options).
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

try:
    import gui.backend as backend  # noqa: E402  (heavy uvicorn import)
except SystemExit:
    raise
except BaseException as _be_exc:  # noqa: BLE001
    # The backend import pulls in uvicorn/FastAPI/etc. If a venv predates the
    # GUI (or a dep is corrupt), this fails before _run()'s crash handler can
    # wrap it — so leave a readable crash log and fail loudly here instead of
    # the window silently closing.
    import traceback as _tb

    _LOG_PATH = os.path.join(_SCRIPT_DIR, "psd_gui_crash.log")
    sys.stderr.write(
        "psd_gui: could not import the psd.ai backend: %r\n"
        "Reinstall dependencies with:  pip install -r requirements.txt\n" % (_be_exc,)
    )
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as _fh:
            _fh.write("\n=== psd_gui import crash ===\n")
            _fh.write(_tb.format_exc())
        sys.stderr.write("Full details written to: %s\n" % _LOG_PATH)
    except Exception:
        pass
    raise SystemExit(1)

# --------------------------------------------------------------------------- #
# Qt binding (prefer PySide6, fall back to PyQt5).
# The preference can be pinned with PSD_GUI_QT=PyQt5 / PSD_GUI_QT=PySide6
# (useful in odd environments where a binding's shared libraries are broken).
# --------------------------------------------------------------------------- #
_HAS_WEBENGINE = False
_PREFERRED_QT = os.getenv("PSD_GUI_QT", "").strip().lower()
_QT = None
QtCore = QtGui = QtWidgets = None

if _PREFERRED_QT == "pyqt5":
    try:
        from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: F401
    except Exception:
        QtCore = QtGui = QtWidgets = None
elif _PREFERRED_QT == "pyside6":
    try:
        from PySide6 import QtCore, QtGui, QtWidgets  # noqa: F401
    except Exception:
        QtCore = QtGui = QtWidgets = None

if QtWidgets is None and _PREFERRED_QT != "pyqt5":
    # no preference (or PySide6 preferred but missing): PySide6 then PyQt5
    try:
        from PySide6 import QtCore, QtGui, QtWidgets  # noqa: F401
    except Exception:
        try:
            from PyQt5 import QtCore, QtGui, QtWidgets  # type: ignore  # noqa: F401
        except Exception:
            QtCore = QtGui = QtWidgets = None

if QtWidgets is not None:
    _QT = "PySide6" if QtWidgets.__name__.startswith("PySide6") else "PyQt5"

if _QT == "PySide6":
    Signal = QtCore.Signal
    QDesktopServices = QtGui.QDesktopServices
    QUrl = QtCore.QUrl
    QAction = QtGui.QAction
    QColor = QtGui.QColor
    QTextCursor = QtGui.QTextCursor
elif _QT == "PyQt5":
    Signal = QtCore.pyqtSignal
    QDesktopServices = QtGui.QDesktopServices
    QUrl = QtCore.QUrl
    QAction = QtWidgets.QAction
    QColor = QtGui.QColor
    QTextCursor = QtGui.QTextCursor
    try:
        QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    except Exception:
        pass

# portable "process every pending event" flag
if hasattr(QtCore.QEventLoop, "ProcessEventsFlag"):
    _ALL_EVENTS = QtCore.QEventLoop.ProcessEventsFlag.AllEvents
else:
    _ALL_EVENTS = QtCore.QEventLoop.AllEvents

__version__ = "1.0.0"

if _QT is None:
    # No Qt binding available. Still allow --version / --help for diagnostics,
    # but otherwise fail with an actionable message before any Qt class is
    # defined (subclassing Qt with a missing binding would ImportError anyway).
    if "--version" in sys.argv:
        print(f"psd_gui {__version__}")
        raise SystemExit(0)
    if "--help" in sys.argv or "-h" in sys.argv:
        print(
            "psd_gui — the psd.ai desktop application (GUI window).\n\n"
            "  No Qt binding could be imported. Install one with:\n"
            "      pip install PySide6     (recommended)\n"
            "      pip install PyQt5      (alternative)\n"
        )
        raise SystemExit(1)
    sys.stderr.write(
        "psd_gui: no Qt binding available.\n"
        "Install PySide6 (pip install PySide6) or PyQt5 (pip install PyQt5).\n"
    )
    raise SystemExit(1)

# --------------------------------------------------------------------------- #
# Theme
# --------------------------------------------------------------------------- #
BG = "#14161d"
BG_PANEL = "#1a1d26"
BG_INPUT = "#22252f"
BG_BUBBLE_USER = "#2b5c8a"
BG_BUBBLE_ASST = "#252a35"
FG = "#dfe3ee"
FG_DIM = "#7d8598"
FG_FAINT = "#565d70"
ACCENT = "#e06c75"
ACCENT2 = "#6c8fe0"
BORDER = "#2c313e"
OK = "#7fbf7f"
WARN = "#e5c07b"
ERR = "#e06c75"


def _apply_theme() -> None:
    app = QtWidgets.QApplication.instance()
    if app is None:
        return
    app.setStyleSheet(
        f"""
        QWidget {{ background-color: {BG}; color: {FG}; font-size: 13px; }}
        QMainWindow, QDialog {{ background-color: {BG}; }}
        QMenuBar {{ background-color: {BG_PANEL}; border-bottom: 1px solid {BORDER}; }}
        QMenuBar::item {{ padding: 5px 12px; background: transparent; color: {FG}; }}
        QMenuBar::item:selected {{ background: {BG_INPUT}; }}
        QMenu {{ background-color: {BG_PANEL}; border: 1px solid {BORDER}; }}
        QMenu::item {{ padding: 6px 24px; color: {FG}; }}
        QMenu::item:selected {{ background: {BG_INPUT}; }}
        QListWidget {{ background: {BG_PANEL}; border: none; outline: none; }}
        QListWidget::item {{ padding: 8px 10px; color: {FG_DIM}; border-bottom: 1px solid {BORDER}; }}
        QListWidget::item:selected {{ background: {BG_INPUT}; color: {FG}; border-left: 3px solid {ACCENT}; }}
        QLineEdit, QTextEdit, QPlainTextEdit, QTextBrowser {{ background: {BG_INPUT}; border: 1px solid {BORDER};
            border-radius: 6px; padding: 6px; color: {FG}; selection-background-color: {ACCENT2}; }}
        QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{ border: 1px solid {ACCENT2}; }}
        QPushButton {{ background: {BG_INPUT}; border: 1px solid {BORDER}; border-radius: 6px;
            padding: 6px 14px; color: {FG}; }}
        QPushButton:hover {{ border: 1px solid {ACCENT2}; }}
        QPushButton:checked {{ background: {BG_BUBBLE_USER}; border: 1px solid {ACCENT2}; }}
        QPushButton:default {{ background: {ACCENT}; color: #14161d; border: 1px solid {ACCENT}; font-weight: 600; }}
        QPushButton:disabled {{ color: {FG_FAINT}; border-color: {BORDER}; }}
        QLabel {{ background: transparent; color: {FG}; }}
        QScrollBar:vertical {{ background: {BG_PANEL}; width: 10px; }}
        QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 24px; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
        QSplitter::handle {{ background: {BORDER}; }}
        QCheckBox {{ spacing: 6px; }}
        QMessageBox {{ background: {BG_PANEL}; }}
        """
    )


# --------------------------------------------------------------------------- #
# Markdown-lite rendering (no external dependency)
# --------------------------------------------------------------------------- #

def markdown_to_html(text: str) -> str:
    """Render the small, safe markdown subset chat needs to rich text."""
    esc = html.escape(text or "").replace("\r\n", "\n").replace("\r", "\n")

    def _fence(m: re.Match) -> str:
        code = (m.group(2) or "").replace("<", "&lt;").replace(">", "&gt;")
        lang = (m.group(1) or "").strip()
        header = (
            f'<span style="color:{FG_FAINT};">{html.escape(lang)}</span>\n' if lang else ""
        )
        return (
            f'<pre style="background:{BG_INPUT};border:1px solid {BORDER};'
            f'border-radius:4px;padding:6px 8px;margin:4px 0;white-space:pre-wrap;'
            f'font-family:Consolas,Menlo,monospace;font-size:12px;color:{FG};">'
            + header + code + "</pre>"
        )

    esc = re.sub(r"```([^\n`]*)\n(.*?)```", _fence, esc, flags=re.DOTALL)

    parts = esc.split("\n")
    out: List[str] = []
    for line in parts:
        if line.strip() == "":
            out.append("")
            continue
        s = line.strip()
        s = re.sub(r"`([^`]+)`", r'<code style="color:#9cd1e6;">\1</code>', s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", s)
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            size = max(14, 20 - (len(m.group(1)) * 1.5))
            s = f'<span style="font-size:{int(size)}px;font-weight:700;">{m.group(2)}</span>'
        elif re.match(r"^[-*]\s+", s):
            s = "&nbsp;&nbsp;•&nbsp; " + re.sub(r"^[-*]\s+", "", s)
        elif re.match(r"^\d+\.\s+", s):
            s = "&nbsp;&nbsp;" + s
        out.append(s)
    return "<br>".join(out)


# --------------------------------------------------------------------------- #
# Thread helpers
# --------------------------------------------------------------------------- #

def wait_future(fut: Any, timeout: float = backend.REQUEST_HARD_TIMEOUT) -> Any:
    """Block the Qt thread while pumping events until ``fut`` resolves."""
    if fut is None:
        raise backend.BackendError("backend unavailable (loop not started)")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fut.done():
            return fut
        QtWidgets.QApplication.processEvents(_ALL_EVENTS, 20)
        time.sleep(0.004)
    raise TimeoutError(f"backend call exceeded {timeout:.0f}s")


# --------------------------------------------------------------------------- #
# Auth state
# --------------------------------------------------------------------------- #

class AuthGate:
    """Encapsulates safe access to the psd.ai AuthManager / auth API."""

    def __init__(self, embedded: bool, base_url: str, label: str) -> None:
        self.embedded = embedded
        self.base_url = base_url
        self.label = label
        self.fastapi_app: Any = None
        self.token = ""
        self.cookie = ""
        self.username = ""


# --------------------------------------------------------------------------- #
# Widgets
# --------------------------------------------------------------------------- #

class BubbleFrame(QtWidgets.QFrame):
    """One chat message bubble (left = assistant, right = user)."""

    def __init__(self, role: str, author: str, text: str) -> None:
        super().__init__()
        self.setObjectName("bubble")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 7, 12, 8)
        layout.setSpacing(2)

        who = QtWidgets.QLabel(author)
        who.setStyleSheet("color:#8b93a7; font-size:11px; font-weight:600;")
        layout.addWidget(who)

        self.body = QtWidgets.QTextBrowser()
        self.body.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.body.setOpenExternalLinks(True)
        self.body.setStyleSheet(
            f"QTextBrowser {{ background: transparent; color:{FG}; border: none; font-size:13px; }}"
        )
        self.body.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.body.setHtml(markdown_to_html(text))
        self.body.document().adjustSize()
        self.body.setMinimumHeight(28)
        self.body.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred
        )
        layout.addWidget(self.body)

        if role == "user":
            self.setStyleSheet(
                f"QFrame#bubble {{ background:{BG_BUBBLE_USER}; border-radius:10px; }}"
            )
        else:
            self.setStyleSheet(
                f"QFrame#bubble {{ background:{BG_BUBBLE_ASST}; border-radius:10px; }}"
            )

    def set_text(self, markdown_text: str) -> None:
        self.body.setHtml(markdown_to_html(markdown_text))
        self.body.document().adjustSize()


class ModelPickerDialog(QtWidgets.QDialog):
    """Searchable picker over the flattened /api/models list."""

    def __init__(self, models: List[Dict[str, Any]], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose a model")
        self.resize(560, 420)
        self._models = models
        self.selected: Optional[Dict[str, Any]] = None

        layout = QtWidgets.QVBoxLayout(self)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("search models…")
        layout.addWidget(self.search)

        self.listw = QtWidgets.QListWidget()
        layout.addWidget(self.listw)

        btns = QtWidgets.QHBoxLayout()
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.ok_btn = QtWidgets.QPushButton("Use model")
        self.ok_btn.setDefault(True)
        btns.addStretch(1)
        btns.addWidget(self.cancel_btn)
        btns.addWidget(self.ok_btn)
        layout.addLayout(btns)

        self.search.textChanged.connect(self._filter)
        self.listw.itemDoubleClicked.connect(lambda _: self._accept())
        self.ok_btn.clicked.connect(self._accept)
        self.cancel_btn.clicked.connect(self.reject)

        self._populate("")

    def _filter(self, text: str) -> None:
        self._populate(text.strip().lower())

    def _populate(self, query: str) -> None:
        self.listw.clear()
        for item in self._models:
            model = (item.get("model") or "").lower()
            ep = (item.get("endpoint_name") or item.get("endpoint_url") or "").lower()
            if query and query not in model and query not in ep:
                continue
            offline = item.get("model") == "(offline)"
            label = (
                f"{item.get('model') or '(no model)'}   —   "
                f"{item.get('endpoint_name') or item.get('endpoint_url') or ''}"
            )
            if offline:
                label += "   (offline)"
            entry = QtWidgets.QListWidgetItem(label)
            entry.setData(QtCore.Qt.UserRole, item)
            if offline:
                entry.setForeground(QColor(FG_FAINT))
            self.listw.addItem(entry)

    def _accept(self) -> None:
        item = self.listw.currentItem()
        if item is None:
            return
        data = item.data(QtCore.Qt.UserRole)
        if not data or not data.get("model") or data.get("model") == "(offline)":
            return
        self.selected = data
        self.accept()


class SetupDialog(QtWidgets.QDialog):
    """First-run admin account creation."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("psd.ai — first-time setup")
        self.resize(420, 280)
        layout = QtWidgets.QVBoxLayout(self)

        title = QtWidgets.QLabel("Create your admin account")
        title.setStyleSheet("font-size:16px; font-weight:700;")
        layout.addWidget(title)
        layout.addWidget(
            QtWidgets.QLabel("This account owns all psd.ai data on this machine.")
        )

        self.username = QtWidgets.QLineEdit()
        self.username.setPlaceholderText("username (default: admin)")
        layout.addWidget(QtWidgets.QLabel("Username"))
        layout.addWidget(self.username)

        self.password = QtWidgets.QLineEdit()
        self.password.setEchoMode(QtWidgets.QLineEdit.Password)
        self.password.setPlaceholderText("at least 8 characters")
        layout.addWidget(QtWidgets.QLabel("Password"))
        layout.addWidget(self.password)

        self.confirm = QtWidgets.QLineEdit()
        self.confirm.setEchoMode(QtWidgets.QLineEdit.Password)
        layout.addWidget(QtWidgets.QLabel("Confirm password"))
        layout.addWidget(self.confirm)

        self.error = QtWidgets.QLabel("")
        self.error.setStyleSheet(f"color:{ERR};")
        self.error.setWordWrap(True)
        layout.addWidget(self.error)

        btns = QtWidgets.QHBoxLayout()
        self.ok_btn = QtWidgets.QPushButton("Create account")
        self.ok_btn.setDefault(True)
        btns.addStretch(1)
        btns.addWidget(self.ok_btn)
        layout.addLayout(btns)
        self.ok_btn.clicked.connect(self._try_submit)

        self.values: Dict[str, str] = {}

    def _try_submit(self) -> None:
        user = self.username.text().strip().lower() or "admin"
        pwd = self.password.text()
        if len(user) < 1:
            self.error.setText("Username is required.")
            return
        try:
            from core.auth import RESERVED_USERNAMES
        except Exception:
            RESERVED_USERNAMES = frozenset(("root", "owner"))
        if user in RESERVED_USERNAMES:
            self.error.setText("That username is reserved — pick another.")
            return
        try:
            from src.constants import PASSWORD_MIN_LENGTH
        except Exception:
            PASSWORD_MIN_LENGTH = 8
        if len(pwd) < PASSWORD_MIN_LENGTH:
            self.error.setText(f"Password must be at least {PASSWORD_MIN_LENGTH} characters.")
            return
        if pwd != self.confirm.text():
            self.error.setText("Passwords don't match.")
            return
        self.values = {"username": user, "password": pwd}
        self.accept()

    def set_busy(self, text: str) -> None:
        self.error.setStyleSheet(f"color:{FG_DIM};")
        self.error.setText(text)
        self.ok_btn.setEnabled(False)


class LoginDialog(QtWidgets.QDialog):
    """Username / password (+ optional 2FA) sign-in."""

    def __init__(self, username_hint: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("psd.ai — sign in")
        self.resize(400, 240)
        layout = QtWidgets.QVBoxLayout(self)

        title = QtWidgets.QLabel("Sign in to psd.ai")
        title.setStyleSheet("font-size:16px; font-weight:700;")
        layout.addWidget(title)

        self.username = QtWidgets.QLineEdit(username_hint)
        self.username.setPlaceholderText("username")
        layout.addWidget(QtWidgets.QLabel("Username"))
        layout.addWidget(self.username)

        self.password = QtWidgets.QLineEdit()
        self.password.setEchoMode(QtWidgets.QLineEdit.Password)
        layout.addWidget(QtWidgets.QLabel("Password"))
        layout.addWidget(self.password)

        self.totp = QtWidgets.QLineEdit()
        self.totp.setEchoMode(QtWidgets.QLineEdit.Password)
        self.totp.setPlaceholderText("6-digit code")
        self.totp.setVisible(False)
        self.totp_label = QtWidgets.QLabel("2FA code")
        self.totp_label.setVisible(False)
        layout.addWidget(self.totp_label)
        layout.addWidget(self.totp)

        self.error = QtWidgets.QLabel("")
        self.error.setStyleSheet(f"color:{ERR};")
        self.error.setWordWrap(True)
        layout.addWidget(self.error)

        btns = QtWidgets.QHBoxLayout()
        self.quit_btn = QtWidgets.QPushButton("Quit")
        self.ok_btn = QtWidgets.QPushButton("Sign in")
        self.ok_btn.setDefault(True)
        btns.addWidget(self.quit_btn)
        btns.addStretch(1)
        btns.addWidget(self.ok_btn)
        layout.addLayout(btns)
        self.ok_btn.clicked.connect(self._prepare)
        self.quit_btn.clicked.connect(self.reject)

        self.values: Dict[str, str] = {"username": "", "password": "", "totp": ""}

    def _prepare(self) -> None:
        self.values = {
            "username": self.username.text().strip().lower(),
            "password": self.password.text(),
            "totp": self.totp.text().strip(),
        }
        if not self.values["username"] or not self.values["password"]:
            self.error.setText("Username and password are required.")
            return
        self.accept()

    def prompt_totp(self) -> None:
        self.totp.setVisible(True)
        self.totp_label.setVisible(True)
        self.error.setStyleSheet(f"color:{WARN};")
        self.error.setText("This account has 2FA enabled — enter your code.")
        self.totp.setFocus()


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #

class _Prompt(QtWidgets.QPlainTextEdit):
    """Composer that sends on Ctrl+Enter."""

    def __init__(self, window: "PsdGuiWindow") -> None:
        super().__init__()
        self._window = window

    def keyPressEvent(self, ev: QtGui.QKeyEvent) -> None:  # type: ignore[override]
        if ev.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter) and (
            ev.modifiers() & QtCore.Qt.ControlModifier
        ):
            self._window._action_send()
            return
        super().keyPressEvent(ev)


class PsdGuiWindow(QtWidgets.QMainWindow):
    chunk_received = Signal(dict)

    def __init__(self, mode: str, host: str, port: int) -> None:
        super().__init__()
        self.setWindowTitle("psd.ai")
        self.resize(1180, 760)

        self._mode = mode  # "embedded" | "attached" | "serve"
        self._host = host
        self._port = port
        self.embedded = mode in ("", "embedded")
        self.serving = mode == "serve"
        self.base_url = f"http://{host}:{port}"

        label = "in-process" if self.embedded else self.base_url
        self._gate = AuthGate(
            self.embedded,
            "http://psd-gui.invalid" if self.embedded else self.base_url,
            label,
        )
        self._client: Optional[Any] = None
        self._pre_client: Optional[Any] = None
        self._current: Optional[Dict[str, Any]] = None
        self._sessions: List[Dict[str, Any]] = []
        self._models: List[Dict[str, Any]] = []
        self._agent_mode = False
        self._use_web = False
        self._in_stream = False
        self._stream_sid = ""
        self._assistant_bubble: Optional[BubbleFrame] = None
        self._assistant_buf: List[str] = []
        self._quitting = False
        self._keepalive: Optional[QtCore.QTimer] = None
        self._login_showing = False

        _apply_theme()
        self._build_menus()
        self._build_ui()
        self.chunk_received.connect(self._on_chunk)

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self.act_new = QAction("New chat", self)
        self.act_new.setShortcut("Ctrl+N")
        self.act_new.triggered.connect(self._action_new)
        file_menu.addAction(self.act_new)

        self.act_rename = QAction("Rename chat…", self)
        self.act_rename.setShortcut("Ctrl+R")
        self.act_rename.triggered.connect(self._action_rename)
        file_menu.addAction(self.act_rename)

        self.act_delete = QAction("Delete chat", self)
        self.act_delete.setShortcut("Ctrl+Delete")
        self.act_delete.triggered.connect(self._action_delete)
        file_menu.addAction(self.act_delete)

        file_menu.addSeparator()
        self.act_refresh = QAction("Refresh", self)
        self.act_refresh.setShortcut("F5")
        self.act_refresh.triggered.connect(self._action_refresh)
        file_menu.addAction(self.act_refresh)

        file_menu.addSeparator()
        self.act_logout = QAction("Sign out", self)
        self.act_logout.triggered.connect(self._action_logout)
        file_menu.addAction(self.act_logout)

        self.act_quit = QAction("Quit", self)
        self.act_quit.setShortcut("Ctrl+Q")
        self.act_quit.triggered.connect(self.close)
        file_menu.addAction(self.act_quit)

        tools_menu = self.menuBar().addMenu("&Settings")
        self.act_model = QAction("Choose model…", self)
        self.act_model.setShortcut("Ctrl+M")
        self.act_model.triggered.connect(self._action_model)
        tools_menu.addAction(self.act_model)

        self.act_agent = QAction("Agent mode (tools)", self)
        self.act_agent.setShortcut("Ctrl+E")
        self.act_agent.setCheckable(True)
        self.act_agent.triggered.connect(self._action_agent)
        tools_menu.addAction(self.act_agent)

        self.act_web = QAction("Web search", self)
        self.act_web.setCheckable(True)
        self.act_web.triggered.connect(self._action_web)
        tools_menu.addAction(self.act_web)

        help_menu = self.menuBar().addMenu("&Help")
        self.act_about = QAction("About psd.ai", self)
        self.act_about.triggered.connect(self._action_about)
        help_menu.addAction(self.act_about)

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.stack = QtWidgets.QStackedWidget()
        outer.addWidget(self.stack, 1)

        self.loading_page = self._build_loading_page()
        self.stack.addWidget(self.loading_page)

        self.main_page = self._build_main_page()
        self.stack.addWidget(self.main_page)

        self.setCentralWidget(central)
        self.statusBar().showMessage("Starting psd.ai…")

    def _build_loading_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.addStretch(1)
        title = QtWidgets.QLabel("⛵ psd.ai")
        title.setAlignment(QtCore.Qt.AlignCenter)
        title.setStyleSheet(f"font-size:30px; font-weight:800; color:{ACCENT};")
        layout.addWidget(title)
        self.loading_sub = QtWidgets.QLabel("Warming up the psd.ai backend…")
        self.loading_sub.setAlignment(QtCore.Qt.AlignCenter)
        self.loading_sub.setStyleSheet(f"color:{FG_DIM};")
        layout.addWidget(self.loading_sub)
        layout.addStretch(1)
        return page

    def _build_main_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)

        # ---- sessions sidebar
        side = QtWidgets.QWidget()
        side.setFixedWidth(264)
        side_lay = QtWidgets.QVBoxLayout(side)
        side_lay.setContentsMargins(8, 8, 8, 8)
        side_lay.setSpacing(6)
        side_title = QtWidgets.QLabel("CONVERSATIONS")
        side_title.setStyleSheet("color:#565d70; font-weight:700; font-size:11px;")
        side_lay.addWidget(side_title)
        self.session_list = QtWidgets.QListWidget()
        self.session_list.currentItemChanged.connect(self._on_session_changed)
        side_lay.addWidget(self.session_list, 1)
        self.new_btn = QtWidgets.QPushButton("+  New chat")
        self.new_btn.clicked.connect(self._action_new)
        side_lay.addWidget(self.new_btn)
        split.addWidget(side)

        # ---- chat column
        chat_col = QtWidgets.QWidget()
        chat_lay = QtWidgets.QVBoxLayout(chat_col)
        chat_lay.setContentsMargins(0, 0, 0, 0)
        chat_lay.setSpacing(0)

        self.model_bar = QtWidgets.QLabel("  model: —   ·   mode: chat")
        self.model_bar.setStyleSheet(
            f"background:{BG_PANEL}; color:{FG_DIM}; padding:5px 10px;"
            f"border-bottom:1px solid {BORDER}; font-size:12px;"
        )
        chat_lay.addWidget(self.model_bar)

        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.chat_inner = QtWidgets.QWidget()
        self.chat_layout = QtWidgets.QVBoxLayout(self.chat_inner)
        self.chat_layout.setContentsMargins(14, 14, 14, 14)
        self.chat_layout.setSpacing(10)
        self.chat_layout.addStretch(1)
        self.scroll.setWidget(self.chat_inner)
        chat_lay.addWidget(self.scroll, 1)

        # ---- composer
        comp = QtWidgets.QWidget()
        comp.setStyleSheet(f"background:{BG_PANEL}; border-top:1px solid {BORDER};")
        comp_lay = QtWidgets.QVBoxLayout(comp)
        comp_lay.setContentsMargins(12, 8, 12, 10)
        comp_lay.setSpacing(6)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        self.agent_btn = QtWidgets.QPushButton("Chat")
        self.agent_btn.setCheckable(True)
        self.agent_btn.setToolTip("Toggle agent mode (tools enabled)")
        self.agent_btn.clicked.connect(self._on_agent_toggle)
        row.addWidget(self.agent_btn)

        self.web_btn = QtWidgets.QPushButton("Web")
        self.web_btn.setCheckable(True)
        self.web_btn.setToolTip("Enable web search for this conversation")
        self.web_btn.clicked.connect(self._on_web_toggle)
        row.addWidget(self.web_btn)

        self.model_btn = QtWidgets.QPushButton("Model…")
        self.model_btn.clicked.connect(self._action_model)
        row.addWidget(self.model_btn)

        row.addStretch(1)
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.setStyleSheet(f"QPushButton {{ color:{ERR}; }}")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._action_stop)
        row.addWidget(self.stop_btn)
        comp_lay.addLayout(row)

        self.prompt = _Prompt(self)
        self.prompt.setPlaceholderText("Message psd.ai…   (Ctrl+Enter to send)")
        self.prompt.setFixedHeight(64)
        comp_lay.addWidget(self.prompt)

        send_row = QtWidgets.QHBoxLayout()
        send_row.addStretch(1)
        self.send_btn = QtWidgets.QPushButton("Send  (Ctrl+Enter)")
        self.send_btn.setDefault(True)
        self.send_btn.clicked.connect(self._action_send)
        send_row.addWidget(self.send_btn)
        comp_lay.addLayout(send_row)
        chat_lay.addWidget(comp)

        split.addWidget(chat_col)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([264, 900])

        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(split)
        return page

    # ------------------------------------------------------------------ #
    # Startup / backend wiring
    # ------------------------------------------------------------------ #

    def _bump_loading(self, text: str) -> None:
        self.loading_sub.setText(text)
        QtWidgets.QApplication.processEvents(_ALL_EVENTS, 10)

    def start_session(self) -> None:
        """Boot the backend and drive setup/login/main (callback-driven)."""
        self.stack.setCurrentWidget(self.loading_page)

        try:
            backend.start()
        except Exception as exc:  # noqa: BLE001
            self._fatal(f"Could not start backend: {exc}")
            return

        if self.embedded:
            self._bump_loading("Importing the psd.ai backend…")
            try:
                fut = backend.run_embedded()
            except Exception as exc:  # noqa: BLE001
                self._fatal(f"Could not start psd.ai: {exc}")
                return
        elif self.serving:
            self._bump_loading(f"Starting server on {self.base_url}…")
            try:
                fut = backend.run_attached(self._host, self._port)
            except Exception as exc:  # noqa: BLE001
                self._fatal(f"Could not start server: {exc}")
                return
        else:
            self._bump_loading(f"Connecting to {self.base_url}…")
            self._post_boot()
            return

        try:
            wait_future(fut, timeout=180.0)
        except Exception as exc:  # noqa: BLE001
            self._fatal(str(exc))
            return
        self._post_boot()

    def _post_boot(self) -> None:
        if self.embedded or self.serving:
            self._gate.fastapi_app = backend.fastapi_app()
            if self._gate.fastapi_app is None:
                self._fatal("Backend failed to initialise")
                return
        try:
            self._pre_client = self._make_client(pre_auth=True)
        except Exception as exc:  # noqa: BLE001
            self._fatal(f"Could not initialise client: {exc}")
            return

        configured = False
        try:
            if self.embedded:
                configured = bool(
                    wait_future(backend.schedule(backend.embedded_configured()), 60.0).result()
                )
            else:
                st = wait_future(backend.schedule(self._pre_client.status()), 60.0).result()
                configured = bool(st.get("configured"))
        except Exception as exc:  # noqa: BLE001
            self._fatal(f"Could not reach the backend: {exc}")
            return

        if not configured:
            self._show_setup()
        else:
            self._show_login()

    def _make_client(self, pre_auth: bool = False) -> Any:
        from gui.api_client import GuiApiClient

        kw: Dict[str, Any] = {"base_url": self._gate.base_url}
        if self.embedded:
            kw["app"] = self._gate.fastapi_app
        if not pre_auth:
            kw["cookie"] = self._gate.cookie
        return GuiApiClient(**kw)

    # ------------------------------------------------------------------ #
    # Auth flow (dialogs)
    # ------------------------------------------------------------------ #

    def _show_setup(self) -> None:
        dlg = SetupDialog(self)
        if dlg.exec_() != QtWidgets.QDialog.Accepted or not dlg.values:
            self._fatal("psd.ai needs an admin account — quitting.")
            return
        self._bump_loading("Creating the admin account…")
        try:
            if self.embedded:
                wait_future(
                    backend.schedule(
                        backend.embedded_setup(dlg.values["username"], dlg.values["password"])
                    ),
                    60.0,
                ).result()
            else:
                wait_future(
                    backend.schedule(
                        self._pre_client.setup_first_run(
                            dlg.values["username"], dlg.values["password"]
                        )
                    ),
                    60.0,
                ).result()
        except Exception as exc:  # noqa: BLE001
            self._fatal(f"Setup failed: {exc}")
            return
        self._show_login(username_hint=dlg.values["username"])

    def _show_login(self, username_hint: str = "") -> None:
        if self._login_showing:
            return
        self._login_showing = True
        dlg = LoginDialog(username_hint=username_hint, parent=self)
        try:
            while True:
                if dlg.exec_() != QtWidgets.QDialog.Accepted:
                    self._fatal("Sign-in cancelled.")
                    return
                creds = dlg.values
                self._bump_loading("Signing in…")
                try:
                    if self.embedded:
                        token = wait_future(
                            backend.schedule(
                                backend.embedded_login(
                                    creds["username"], creds["password"], creds.get("totp", "")
                                )
                            ),
                            60.0,
                        ).result()
                        self._gate.cookie = token
                    else:
                        result = wait_future(
                            backend.schedule(
                                self._pre_client.login(
                                    creds["username"], creds["password"], creds.get("totp", "")
                                )
                            ),
                            60.0,
                        ).result()
                        if not result.get("ok") and result.get("requires_totp"):
                            dlg.prompt_totp()
                            continue
                        self._gate.cookie = result.get("session") or ""
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc)
                    if "2FA_REQUIRED" in msg or "requires_totp" in msg:
                        dlg.prompt_totp()
                        continue
                    dlg.error.setText(f"{msg}")
                    dlg.error.setStyleSheet(f"color:{ERR};")
                    continue
                break

            self._gate.username = creds["username"]
            if self.embedded:
                self._client = self._make_client(pre_auth=False)
            else:
                self._client = self._pre_client

            self._enter_main()
        finally:
            self._login_showing = False

    def _fatal(self, msg: str) -> None:
        QtWidgets.QMessageBox.critical(self, "psd.ai", msg)
        self.close()

    def _enter_main(self) -> None:
        self.stack.setCurrentWidget(self.main_page)
        self.setWindowTitle(f"psd.ai — {self._gate.username} · ({self._gate.label})")
        self.statusBar().showMessage(f"Connected ({self._gate.label})")
        self._refresh_sessions()
        self._start_keepalive()
        self.prompt.setFocus()

    # ------------------------------------------------------------------ #
    # Keepalive (detects auth drops / reconnects)
    # ------------------------------------------------------------------ #

    def _start_keepalive(self) -> None:
        if self._keepalive is None:
            self._keepalive = QtCore.QTimer(self)
            self._keepalive.setInterval(4000)
            self._keepalive.timeout.connect(self._keepalive_once)
        self._keepalive.start()

    def _keepalive_once(self) -> None:
        if self._client is None or self._quitting:
            return

        async def _once() -> None:
            st = await self._client.status()
            QtCore.QTimer.singleShot(0, lambda: self._on_auth_status(st))

        backend.schedule(_once(), fire_and_forget=True)

    def _on_auth_status(self, st: Dict[str, Any]) -> None:
        if self._quitting:
            return
        if not st.get("authenticated") and st.get("configured"):
            self._stop_keepalive()
            self._show_login()

    def _stop_keepalive(self) -> None:
        if self._keepalive is not None:
            self._keepalive.stop()

    # ------------------------------------------------------------------ #
    # Sessions
    # ------------------------------------------------------------------ #

    def _current_sid(self) -> str:
        return (self._current or {}).get("id", "") or ""

    def _refresh_sessions(self, select_sid: Optional[str] = None) -> None:
        if self._client is None:
            return
        try:
            sessions = wait_future(backend.schedule(self._client.sessions()), timeout=60.0).result()
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Could not load chats: {exc}")
            return
        self._sessions = list(sessions or [])
        self.session_list.blockSignals(True)
        self.session_list.clear()
        for s in self._sessions:
            name = s.get("name") or "(untitled)"
            item = QtWidgets.QListWidgetItem(name)
            item.setData(QtCore.Qt.UserRole, s)
            item.setToolTip(f"model: {s.get('model') or '—'}")
            self.session_list.addItem(item)
        self.session_list.blockSignals(False)

        sid = select_sid or self._current_sid()
        if sid and any(s.get("id") == sid for s in self._sessions):
            self._select_session(sid)
        elif self._sessions:
            self._select_session(self._sessions[0]["id"])
        else:
            self._current = None
            self._clear_chat()
            self._hint("No chats yet — type a message to start one.")
            self._update_model_bar()

    def _on_session_changed(self, current: Optional[QtWidgets.QListWidgetItem],
                            _prev: Optional[QtWidgets.QListWidgetItem]) -> None:
        if current is None:
            return
        sid = (current.data(QtCore.Qt.UserRole) or {}).get("id")
        if sid and sid != self._current_sid():
            self._select_session(sid)

    def _select_session(self, sid: str) -> None:
        session = next((s for s in self._sessions if s.get("id") == sid), None)
        self._current = session or {"id": sid}
        self._clear_chat()
        self._update_model_bar()
        if self._client is None:
            return

        async def _load() -> Dict[str, Any]:
            return await self._client.history(sid, limit=200)

        try:
            hist = wait_future(backend.schedule(_load()), timeout=60.0).result()
        except Exception as exc:  # noqa: BLE001
            self._hint(f"⚠ could not load history: {exc}")
            return
        if hist.get("model"):
            self._current["model"] = hist["model"]
        if hist.get("name"):
            self._current["name"] = hist["name"]
        if hist.get("endpoint_url"):
            self._current["endpoint_url"] = hist["endpoint_url"]
        turns = list(hist.get("history") or [])
        if not turns:
            self._hint(f"Fresh chat — model **{self._current.get('model') or '?'}**.")
        for turn in turns:
            role = turn.get("role") or "assistant"
            if role not in ("user", "assistant"):
                continue
            self._append_bubble(role, turn.get("content") or "")
        self._update_model_bar()
        self.statusBar().showMessage(f"Opened “{self._current.get('name') or sid[:8]}”")

    def _clear_chat(self) -> None:
        while self.chat_layout.count() > 1:
            item = self.chat_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._assistant_bubble = None
        self._assistant_buf = []

    def _hint(self, text: str) -> None:
        hint = QtWidgets.QLabel(markdown_to_html(text))
        hint.setTextFormat(QtCore.Qt.RichText)
        hint.setStyleSheet(f"color:{FG_FAINT}; padding:6px 2px;")
        self._insert_widget(hint)

    def _insert_widget(self, w: QtWidgets.QWidget) -> None:
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, w)
        self._scroll_bottom()

    def _scroll_bottom(self) -> None:
        def _do() -> None:
            bar = self.scroll.verticalScrollBar()
            bar.setValue(bar.maximum())

        QtCore.QTimer.singleShot(0, _do)

    def _update_model_bar(self) -> None:
        model = (self._current or {}).get("model") or "—"
        mode = "agent" if self._agent_mode else "chat"
        web = " · web" if self._use_web else ""
        self.model_bar.setText(f"  model: {model}   ·   mode: {mode}{web}   ·   {self._gate.label}")

    # ------------------------------------------------------------------ #
    # Bubbles
    # ------------------------------------------------------------------ #

    def _append_bubble(self, role: str, text: str) -> BubbleFrame:
        author = "You" if role == "user" else ((self._current or {}).get("model") or "psd.ai")
        bubble = BubbleFrame(role, author, text)
        self._insert_widget(bubble)
        return bubble

    # ------------------------------------------------------------------ #
    # Model picker
    # ------------------------------------------------------------------ #

    def _load_models(self, refresh: bool = True) -> List[Dict[str, Any]]:
        if self._client is None:
            return []

        async def _go() -> List[Dict[str, Any]]:
            try:
                data = await self._client.models(refresh=refresh)
            except Exception:
                data = {}
            items: List[Dict[str, Any]] = []
            for host in (data or {}).get("items", []):
                ep_id = host.get("endpoint_id") or ""
                ep_name = host.get("endpoint_name") or host.get("url") or ""
                models = list(host.get("models") or []) + list(host.get("models_extra") or [])
                if not models and host.get("offline"):
                    models = ["(offline)"]
                for m in models:
                    items.append({
                        "model": m,
                        "endpoint_id": ep_id,
                        "endpoint_url": host.get("url") or "",
                        "endpoint_name": ep_name,
                        "category": host.get("category") or "",
                    })
            return items

        try:
            return list(wait_future(backend.schedule(_go()), timeout=120.0).result())
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"model list failed: {exc}")
            return []

    def _action_model(self) -> None:
        self._models = self._load_models(refresh=True)
        if not self._models:
            QtWidgets.QMessageBox.information(
                self, "psd.ai",
                "No model endpoints configured yet.\n\nStart the local model group "
                "(run.bat launches it) or add an endpoint in Settings, then try again.",
            )
            return
        dlg = ModelPickerDialog(self._models, self)
        if dlg.exec_() != QtWidgets.QDialog.Accepted or dlg.selected is None:
            return
        self._apply_model(dlg.selected)

    def _apply_model(self, item: Dict[str, Any]) -> None:
        if self._current is None:
            self._create_session_with(item)
            return

        async def _patch() -> None:
            await self._client.patch_session(
                self._current["id"],
                model=item["model"],
                endpoint_url=item.get("endpoint_url") or "",
                endpoint_id=item.get("endpoint_id") or "",
            )

        try:
            wait_future(backend.schedule(_patch()), timeout=60.0).result()
            self._current["model"] = item["model"]
            self._current["endpoint_url"] = item.get("endpoint_url", "")
            self._update_model_bar()
            self.statusBar().showMessage(f"Model set to {item['model']}")
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"model switch failed: {exc}")

    # ------------------------------------------------------------------ #
    # Session actions
    # ------------------------------------------------------------------ #

    def _create_session_with(self, item: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        async def _default() -> Dict[str, Any]:
            try:
                d = await self._client.default_chat()
            except Exception:
                d = {}
            return {
                "model": d.get("model") or (item or {}).get("model") or "",
                "endpoint_url": d.get("endpoint_url") or (item or {}).get("endpoint_url") or "",
                "endpoint_id": d.get("endpoint_id") or (item or {}).get("endpoint_id") or "",
            }

        try:
            d = wait_future(backend.schedule(_default()), timeout=60.0).result()
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"default model failed: {exc}")
            return None

        model = d.get("model") or ""
        endpoint_url = d.get("endpoint_url") or ""
        endpoint_id = d.get("endpoint_id") or ""

        # No per-user default set yet → fall back to the first model the user
        # can see in /api/models (same behaviour as the web UI's auto-pick).
        if not (model and endpoint_url):
            self._models = self._load_models(refresh=False)
            first = next(
                (m for m in self._models if m.get("model") and m.get("endpoint_url")),
                None,
            )
            if first:
                model = first["model"]
                endpoint_url = first["endpoint_url"]
                endpoint_id = first.get("endpoint_id") or ""

        if not model:
            self.statusBar().showMessage("No model selected — choose one with Settings → Choose model.")
            return None

        async def _create() -> Dict[str, Any]:
            return await self._client.create_session(
                "New chat", model=model, endpoint_url=endpoint_url, endpoint_id=endpoint_id
            )

        try:
            session = wait_future(backend.schedule(_create()), timeout=60.0).result()
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"new chat failed: {exc}")
            return None
        if not session.get("id"):
            self.statusBar().showMessage("new chat failed (no id returned)")
            return None
        self._current = {
            "id": session["id"],
            "name": "New chat",
            "model": model,
            "endpoint_url": endpoint_url,
            "endpoint_id": endpoint_id,
        }
        self._refresh_sessions(select_sid=session["id"])
        return self._current

    def _ensure_session(self) -> bool:
        if self._current is not None and self._current.get("model"):
            return True
        return self._create_session_with() is not None

    def _action_new(self) -> None:
        if self._current is None or not self._current.get("model"):
            self._create_session_with()
            return

        async def _create() -> Dict[str, Any]:
            return await self._client.create_session(
                "New chat",
                model=self._current["model"],
                endpoint_url=self._current.get("endpoint_url") or "",
                endpoint_id="",
            )

        try:
            session = wait_future(backend.schedule(_create()), timeout=60.0).result()
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"new chat failed: {exc}")
            return
        if session.get("id"):
            self._current = {
                "id": session["id"], "name": "New chat",
                "model": self._current["model"],
                "endpoint_url": self._current.get("endpoint_url", ""),
            }
            self._refresh_sessions(select_sid=session["id"])

    def _action_rename(self) -> None:
        if self._current is None:
            self.statusBar().showMessage("No chat open")
            return
        name, ok = QtWidgets.QInputDialog.getText(
            self, "Rename chat", "Name:", text=self._current.get("name") or ""
        )
        if not ok or not name.strip():
            return

        async def _rename() -> None:
            await self._client.rename_session(self._current["id"], name.strip())

        try:
            wait_future(backend.schedule(_rename()), timeout=60.0).result()
            self._current["name"] = name.strip()
            self._refresh_sessions()
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"rename failed: {exc}")

    def _action_delete(self) -> None:
        if self._current is None:
            self.statusBar().showMessage("No chat open")
            return
        name = self._current.get("name") or self._current.get("id")
        answer = QtWidgets.QMessageBox.question(
            self, "Delete chat",
            f'Delete the chat “{name}”? This cannot be undone.',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        sid = self._current["id"]

        async def _delete() -> None:
            await self._client.delete_session(sid)

        try:
            wait_future(backend.schedule(_delete()), timeout=60.0).result()
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"delete failed: {exc}")
            return
        self._current = None
        self._refresh_sessions()

    def _action_refresh(self) -> None:
        self._refresh_sessions()

    def _action_logout(self) -> None:
        if self._client is None:
            return
        self._stop_keepalive()

        async def _logout() -> None:
            await self._client.logout()

        backend.schedule(_logout(), fire_and_forget=True)
        self._client = None
        self._current = None
        self._clear_chat()
        self.session_list.clear()
        self._show_login()

    def _action_about(self) -> None:
        QtWidgets.QMessageBox.information(
            self,
            "About psd.ai",
            "psd.ai desktop — a native window for psd.ai.\n\n"
            "It drives the same backend as the web UI (chats, memory, models and "
            "settings all live in one place) — no browser, no localhost page.\n\n"
            f"UI toolkit: {_QT}   ·   backend: {self._gate.label}",
        )

    # ------------------------------------------------------------------ #
    # Mode toggles
    # ------------------------------------------------------------------ #

    def _action_agent(self, checked: bool) -> None:
        self._agent_mode = bool(checked)
        self.act_agent.setChecked(self._agent_mode)
        self.agent_btn.setChecked(self._agent_mode)
        self.agent_btn.setText("Agent" if self._agent_mode else "Chat")
        self._update_model_bar()

    def _on_agent_toggle(self, checked: bool) -> None:
        self._action_agent(checked)

    def _action_web(self, checked: bool) -> None:
        self._use_web = bool(checked)
        self.act_web.setChecked(self._use_web)
        self.web_btn.setChecked(self._use_web)
        self._update_model_bar()

    def _on_web_toggle(self, checked: bool) -> None:
        self._action_web(checked)

    # ------------------------------------------------------------------ #
    # Sending / streaming
    # ------------------------------------------------------------------ #

    def _action_send(self) -> None:
        if self._in_stream:
            return
        text = (self.prompt.toPlainText() or "").strip()
        if not text:
            return
        if self._client is None:
            self.statusBar().showMessage("Not signed in.")
            return
        if not self._ensure_session():
            return

        if text.lower().startswith("/agent"):
            self._action_agent(True)
            text = text[len("/agent"):].strip()
            if not text:
                return

        self.prompt.setPlainText("")
        self._append_bubble("user", text)
        bubble = self._append_bubble("assistant", "")
        bubble.body.setPlainText("…")
        self._assistant_bubble = bubble
        self._assistant_buf = []
        self._in_stream = True
        self._stream_sid = self._current["id"]
        self.send_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.statusBar().showMessage("Thinking…")

        gen = self._client.chat_stream(
            text,
            self._current["id"],
            mode="agent" if self._agent_mode else "chat",
            endpoint_id=self._current.get("endpoint_id") or "",
            use_web=self._use_web,
            allow_bash=self._agent_mode,
        )
        backend.consume(gen, self.chunk_received.emit)

    def _action_stop(self) -> None:
        if not self._in_stream or self._client is None:
            return

        async def _stop() -> None:
            await self._client.stop_stream(self._stream_sid)

        backend.schedule(_stop(), fire_and_forget=True)
        self.statusBar().showMessage("Stopping…")

    def _on_chunk(self, chunk: Dict[str, Any]) -> None:
        if not self._in_stream:
            return
        sid = chunk.get("_sid")
        if sid is not None and sid != self._stream_sid:
            return

        if "_error" in chunk:
            msg = str(chunk["_error"])
            self._assistant_buf.append(f"\n\n⚠ *{msg}*")
            if self._assistant_bubble is not None and self._assistant_buf:
                self._assistant_bubble.set_text("".join(self._assistant_buf))
            self.statusBar().showMessage(msg)
            self._finish_stream()
            return

        ctype = chunk.get("type")
        if "delta" in chunk:
            self._assistant_buf.append(str(chunk["delta"]))
            if self._assistant_bubble is not None:
                self._assistant_bubble.set_text("".join(self._assistant_buf))
            self._scroll_bottom()
        elif ctype in ("model_info", "model_actual", "fallback"):
            model = chunk.get("model") or chunk.get("answered_by") or chunk.get("suffix")
            if model and self._current is not None:
                self._current["model"] = model
                self._update_model_bar()
        elif ctype == "tool_start":
            self.statusBar().showMessage(f"⚙ {chunk.get('tool') or 'tool'}")
        elif ctype in ("tool_result", "tool_end"):
            self.statusBar().showMessage("Thinking…")
        elif ctype == "research_progress":
            self.statusBar().showMessage("🔍 research…")

        if chunk.get("_fin"):
            self._finish_stream()

    def _finish_stream(self) -> None:
        if not self._in_stream:
            return
        self._in_stream = False
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.statusBar().showMessage("")
        # pick up a possibly-renamed/auto-created thread in the sidebar
        QtCore.QTimer.singleShot(0, self._refresh_sessions)

    # ------------------------------------------------------------------ #
    # Shutdown
    # ------------------------------------------------------------------ #

    def shutdown(self) -> None:
        self._quitting = True
        self._stop_keepalive()
        if self.embedded:
            try:
                backend.stop_embedded()
            except Exception:
                pass
        elif self.serving:
            try:
                backend.stop_attached()
            except Exception:
                pass

    def closeEvent(self, ev: QtGui.QCloseEvent) -> None:  # type: ignore[override]
        self.shutdown()
        super().closeEvent(ev)


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="psd_gui.py",
        description="Desktop (GUI) application for psd.ai — replaces the localhost browser window.",
    )
    p.add_argument(
        "--embedded",
        action="store_true",
        help="run psd.ai in-process — no HTTP server, no browser, no port (default)",
    )
    p.add_argument(
        "--host",
        default=str(os.getenv("PSD_GUI_HOST", "127.0.0.1")),
        help="server host when attaching (default 127.0.0.1)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PSD_GUI_PORT", "7000")),
        help="server port when attaching (default 7000)",
    )
    p.add_argument(
        "--serve",
        action="store_true",
        help="(attached) start and serve the backend ourselves instead of expecting one",
    )
    p.add_argument("--version", action="store_true", help="print version and exit")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.version:
        print(f"psd_gui {__version__}")
        return 0

    # Hand QApplication only argv[0]: all psd_gui's own flags are parsed above.
    qt_app = QtWidgets.QApplication([sys.argv[0]])
    qt_app.setApplicationName("psd.ai")
    try:
        qt_app.setStyle("Fusion")
    except Exception:
        pass
    _apply_theme()

    raw = list(argv) if argv is not None else list(sys.argv[1:])
    explicit_hostport = any(a.startswith("--host") or a.startswith("--port") for a in raw)

    if args.serve:
        mode = "serve"
    elif args.embedded:
        mode = "embedded"
    elif explicit_hostport:
        mode = "attached"
    else:
        mode = "embedded"  # default: no localhost, root system runs in-process

    win = PsdGuiWindow(mode, args.host, args.port)
    win.show()

    # Kick off backend boot + auth flow without blocking Qt's event loop.
    QtCore.QTimer.singleShot(0, win.start_session)

    try:
        rc = qt_app.exec_()
    finally:
        win.shutdown()
    return int(rc)


def _run() -> int:
    """Top-level entry with a crash log so a silent window close still leaves
    a readable trace instead of vanishing without explanation."""
    try:
        return main()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001
        import traceback

        log_path = os.path.join(_SCRIPT_DIR, "psd_gui_crash.log")
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write("\n=== psd_gui crash ===\n")
                fh.write(traceback.format_exc())
        except Exception:
            pass
        traceback.print_exc()
        try:
            sys.stderr.write(
                f"\npsd.ai crashed: {exc}\nFull details written to: {log_path}\n"
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(_run())
