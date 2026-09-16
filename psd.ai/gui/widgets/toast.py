"""Transient in-window toasts (the desktop equivalent of the web UI's snackbars)."""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer, QRect
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from gui.theme import SEMANTIC, current_theme

KIND_COLORS = {
    "info": SEMANTIC["info"],
    "success": SEMANTIC["success"],
    "warning": SEMANTIC["warning"],
    "error": SEMANTIC["error"],
}


class Toast(QFrame):
    """A single floating notification pinned to the top-right of a window."""

    def __init__(self, parent: QWidget, text: str, kind: str = "info", timeout_ms: int = 4200) -> None:
        super().__init__(parent)
        theme = current_theme()
        accent = KIND_COLORS.get(kind, KIND_COLORS["info"])
        self.setObjectName("Toast")
        self.setStyleSheet(
            f"QFrame#Toast {{ background-color: {theme.surface_alt};"
            f" border: 1px solid {accent}; border-left: 4px solid {accent};"
            f" border-radius: 8px; }}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 9, 14, 9)
        label = QLabel(text)
        label.setWordWrap(True)
        label.setMaximumWidth(430)
        label.setStyleSheet(f"color: {theme.text}; background: transparent;")
        layout.addWidget(label)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(24)
        shadow.setColor(QColor(0, 0, 0, 120))
        shadow.setOffset(0, 4)
        self.setGraphicsEffect(shadow)

        self._timeout = timeout_ms
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(timeout_ms)
        self._timer.timeout.connect(self.fade_out)

        self._anim: Optional[QPropertyAnimation] = None

    def show_toast(self) -> None:
        self.adjustSize()
        self._reposition()
        self.show()
        self.raise_()
        self._timer.start()

    def _reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        margin = 18
        stack = [w for w in parent.findChildren(Toast) if w is not self and w.isVisible()]
        offset = margin + sum(w.height() + 8 for w in stack)
        self.move(parent.width() - self.width() - margin, offset)

    def fade_out(self) -> None:
        self._timer.stop()
        self._anim = QPropertyAnimation(self, b"windowOpacity")
        self._anim.setDuration(220)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.setEasingCurve(QEasingCurve.InQuad)
        self._anim.finished.connect(self.deleteLater)
        self._anim.start()


def toast(parent: Optional[QWidget], text: str, kind: str = "info", timeout_ms: int = 4200) -> None:
    """Show a toast on ``parent``'s window."""
    if parent is None:
        return
    window = parent.window()
    item = Toast(window, text, kind, timeout_ms)
    item.show_toast()
    QTimer.singleShot(timeout_ms + 400, lambda: None)
