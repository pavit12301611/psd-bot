"""Sign-in / first-run setup screen (the desktop equivalent of login.html)."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from gui.api import Api
from gui.backend import BackendError
from gui.theme import brand_icon, current_theme
from gui.widgets.toast import toast


class LoginView(QWidget):
    """Handles first-run admin setup, password login and 2FA."""

    logged_in = Signal(str)          # username

    def __init__(self, api: Optional[Api] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.api = api or Api()
        self._needs_totp = False
        self._username = ""
        self._busy = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 40, 40, 40)
        outer.addStretch(1)

        card = QFrame()
        card.setObjectName("Card")
        card.setFixedWidth(430)
        card.setSizePolicy(card.sizePolicy().horizontalPolicy(), card.sizePolicy().verticalPolicy())
        layout = QVBoxLayout(card)
        layout.setContentsMargins(30, 28, 30, 26)
        layout.setSpacing(14)

        brand = QHBoxLayout()
        brand.setSpacing(12)
        icon = QLabel()
        icon.setPixmap(brand_icon(64, current_theme().accent).pixmap(44, 44))
        brand.addWidget(icon)
        name = QLabel("psd.ai")
        name.setObjectName("AppBrand")
        brand.addWidget(name)
        brand.addStretch(1)
        layout.addLayout(brand)

        self.headline = QLabel("Sign in to your workspace")
        self.headline.setObjectName("PageTitle")
        self.headline.setWordWrap(True)
        layout.addWidget(self.headline)
        self.hint = QLabel("Everything runs on this machine — no browser, no server port.")
        self.hint.setObjectName("PageSubtitle")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        form = QFormLayout()
        form.setSpacing(10)
        self.username = QLineEdit()
        self.username.setPlaceholderText("username")
        self.password = QLineEdit()
        self.password.setPlaceholderText("password")
        self.password.setEchoMode(QLineEdit.Password)
        self.totp = QLineEdit()
        self.totp.setPlaceholderText("6-digit code")
        self.totp.setVisible(False)
        self.confirm = QLineEdit()
        self.confirm.setPlaceholderText("repeat password")
        self.confirm.setEchoMode(QLineEdit.Password)
        self.confirm.setVisible(False)
        form.addRow("Username", self.username)
        form.addRow("Password", self.password)
        form.addRow("Repeat", self.confirm)
        form.addRow("2FA code", self.totp)
        layout.addLayout(form)

        self.remember = QCheckBox("Keep me signed in on this machine")
        self.remember.setChecked(True)
        layout.addWidget(self.remember)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(
            f"color: {current_theme().syntax.get('string', '#ff6b6b')}; background: transparent;")
        layout.addWidget(self.error_label)

        self.button = QPushButton("Sign in")
        self.button.setObjectName("Primary")
        self.button.setMinimumHeight(36)
        self.button.clicked.connect(self.submit)
        layout.addWidget(self.button)

        center = QHBoxLayout()
        center.addStretch(1)
        center.addWidget(card)
        center.addStretch(1)
        outer.addLayout(center)
        outer.addStretch(1)

        for field in (self.username, self.password, self.confirm, self.totp):
            field.returnPressed.connect(self.submit)

        self._mode = "login"

    # ------------------------------------------------------------------ #
    def configure_from_status(self, status: dict) -> None:
        configured = bool(status.get("configured", True))
        self.set_mode("setup" if not configured else "login")

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        setup = mode == "setup"
        self.headline.setText("Create your admin account" if setup else "Sign in to your workspace")
        self.hint.setText(
            "First run: choose the username and password for this machine."
            if setup else
            "Everything runs on this machine — no browser, no server port.")
        self.confirm.setVisible(setup)
        self.button.setText("Create account" if setup else "Sign in")
        if setup:
            self.username.setFocus()
        else:
            self.username.setFocus()

    def show_totp(self, username: str) -> None:
        self._needs_totp = True
        self._username = username
        self.totp.setVisible(True)
        self.hint.setText(f"Two-factor code required for {username}.")
        self.totp.setFocus()

    # ------------------------------------------------------------------ #
    def submit(self) -> None:
        if self._busy:
            return
        username = self.username.text().strip()
        password = self.password.text()
        if not username or not password:
            self.error_label.setText("Enter a username and password.")
            return
        if self._mode == "setup":
            if password != self.confirm.text():
                self.error_label.setText("Passwords do not match.")
                return
            if len(password) < 8:
                self.error_label.setText("Password must be at least 8 characters.")
                return
            self._busy = True
            self.button.setEnabled(False)
            self.button.setText("Creating…")
            from gui.workers import run

            run(self._setup, username, password,
                on_done=self._on_setup_done, on_error=self._on_error)
            return
        self._busy = True
        self.button.setEnabled(False)
        self.button.setText("Signing in…")
        from gui.workers import run

        run(self._login, username, password, self.totp.text().strip(),
            on_done=self._on_login_done, on_error=self._on_error)

    def _setup(self, username: str, password: str) -> str:
        self.api.first_run_setup(username, password)
        self.api.login(username, password, remember=self.remember.isChecked())
        return username

    def _on_setup_done(self, username: str) -> None:
        self._finish(username)

    def _login(self, username: str, password: str, totp: str) -> dict:
        result = self.api.login(username, password, remember=self.remember.isChecked(),
                                totp_code=totp)
        return result

    def _on_login_done(self, result: dict) -> None:
        if result.get("requires_totp"):
            self.button.setEnabled(True)
            self.button.setText("Sign in")
            self.show_totp(result.get("username") or self.username.text().strip())
            return
        self._finish(result.get("username") or self.username.text().strip())

    def _finish(self, username: str) -> None:
        self.error_label.setText("")
        self.logged_in.emit(username)

    def _on_error(self, exc: BaseException) -> None:
        self._busy = False
        self.button.setEnabled(True)
        self.button.setText("Create account" if self._mode == "setup" else "Sign in")
        message = str(exc)
        if isinstance(exc, BackendError):
            message = str(exc)
        self.error_label.setText(message[:300])

