"""Shared base classes for workspace views."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from gui.api import get_api
from gui.workers import describe_error, run
from gui.widgets.toast import toast

logger = logging.getLogger("psd.gui.views")


class View(QWidget):
    """A workspace screen: title bar + content area + helpers."""

    #: emitted when the view wants the shell to switch screens
    navigate = Signal(str)
    #: emitted when the view wants the shell to open a document in the editor
    open_document = Signal(str)

    def __init__(self, title: str, subtitle: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.title_text = title
        self.api = get_api()
        self._loaded_once = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 12)
        outer.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(12)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("PageTitle")
        titles.addWidget(self.title_label)
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("PageSubtitle")
        self.subtitle_label.setWordWrap(True)
        titles.addWidget(self.subtitle_label)
        header.addLayout(titles, 1)
        self.header_actions = QHBoxLayout()
        self.header_actions.setSpacing(8)
        header.addLayout(self.header_actions)
        outer.addLayout(header)

        self.content_layout = QVBoxLayout()
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(10)
        outer.addLayout(self.content_layout, 1)

    # -- convenience ------------------------------------------------------ #
    def add_action(self, widget: QWidget) -> None:
        self.header_actions.addWidget(widget)

    def set_content(self, widget: QWidget) -> None:
        self.content_layout.addWidget(widget, 1)

    def toast(self, text: str, kind: str = "info", ms: int = 3600) -> None:
        toast(self, text, kind, ms)

    def error(self, exc: BaseException, prefix: str = "") -> None:
        message = describe_error(exc)
        self.toast(f"{prefix}{message}" if prefix else message, "error", 6000)
        logger.debug("view error: %s", exc, exc_info=False)

    def run(self, fn: Callable[..., Any], *args: Any,
            on_done: Optional[Callable[[Any], None]] = None,
            on_error: Optional[Callable[[BaseException], None]] = None,
            **kwargs: Any):
        return run(fn, *args, on_done=on_done,
                   on_error=on_error or self.error, **kwargs)

    # -- lifecycle --------------------------------------------------------- #
    def activate(self, force: bool = False) -> None:
        """Called by the shell whenever the view becomes visible."""
        if not self._loaded_once or force:
            self._loaded_once = True
            self.refresh()

    def refresh(self) -> None:
        """Load/refresh data. Override in subclasses."""

    def deactivate(self) -> None:
        """Called when the shell switches away from this view."""


class ScrollableView(View):
    """A :class:`View` whose content scrolls (settings, diagnostics, …)."""

    def __init__(self, title: str, subtitle: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(title, subtitle, parent)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._inner = QWidget()
        self._inner.setStyleSheet("background: transparent;")
        self.inner_layout = QVBoxLayout(self._inner)
        self.inner_layout.setContentsMargins(2, 2, 8, 18)
        self.inner_layout.setSpacing(12)
        self.inner_layout.addStretch(1)
        self.scroll.setWidget(self._inner)
        self.set_content(self.scroll)

    def add_block(self, widget: QWidget, stretch: int = 0) -> None:
        self.inner_layout.insertWidget(self.inner_layout.count() - 1, widget, stretch)

    def clear_blocks(self) -> None:
        while self.inner_layout.count() > 1:
            item = self.inner_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()


def card(title: str = "") -> QFrame:
    """A titled card container."""
    frame = QFrame()
    frame.setObjectName("Card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 12, 14, 12)
    layout.setSpacing(8)
    if title:
        label = QLabel(title)
        label.setObjectName("Accent")
        layout.addWidget(label)
    frame.layout_ = layout  # type: ignore[attr-defined]
    return frame


def row(*widgets: QWidget, spacing: int = 8) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setSpacing(spacing)
    for widget in widgets:
        layout.addWidget(widget)
    return layout


def stretch_row(*widgets: QWidget) -> QHBoxLayout:
    layout = row(*widgets)
    layout.addStretch(1)
    return layout


def muted(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("Muted")
    label.setWordWrap(True)
    return label


def faint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("Faint")
    label.setWordWrap(True)
    return label
