"""The chat transcript: message cards, streaming markdown, tool & approval cards.

Each message is its own card so a streaming assistant turn can be re-rendered
without touching the rest of the conversation. Markdown goes through
:mod:`gui.markdown_html`; code-copy links, external links and in-message images
are intercepted and handled natively (clipboard / OS browser / in-process
fetch) instead of by a browser engine.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QMimeData, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QDesktopServices, QPixmap, QTextDocument,
)
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QInputDialog, QLabel, QMenu,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QTextBrowser,
    QVBoxLayout, QWidget,
)

from gui import markdown_html as md
from gui.backend import BackendError
from gui.theme import Theme, current_theme
from gui.widgets.toast import toast

logger = logging.getLogger("psd.gui.transcript")

ROLE_LABELS = {"user": "You", "assistant": "psd.ai", "system": "System", "tool": "Tool"}


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

@dataclass
class ToolCall:
    tool: str = ""
    command: str = ""
    output: str = ""
    exit_code: Optional[int] = None
    running: bool = True
    round: Optional[int] = None


@dataclass
class Approval:
    approval_id: str = ""
    question: str = ""
    description: str = ""
    options: List[Dict[str, Any]] = field(default_factory=list)
    resolved: str = ""
    kind: str = "tool_approval"      # tool_approval | ask_user


@dataclass
class MessageState:
    role: str = "user"
    content: str = ""
    tools: List[ToolCall] = field(default_factory=list)
    approvals: List[Approval] = field(default_factory=list)
    model: str = ""
    endpoint: str = ""
    timestamp: float = field(default_factory=time.time)
    streaming: bool = False
    stopped: bool = False
    metrics: Dict[str, Any] = field(default_factory=dict)
    sources: List[Dict[str, Any]] = field(default_factory=list)
    raw: str = ""                     # original markdown (for copy)


# --------------------------------------------------------------------------- #
# Rich text viewer
# --------------------------------------------------------------------------- #

class RichView(QTextBrowser):
    """A read-only markdown view that sizes itself to its content."""

    link_activated = Signal(str)        # raw url / anchor
    height_changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("RichView")
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setFrameShape(QFrame.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.document().setDocumentMargin(4)
        self.anchorClicked.connect(self._on_anchor)
        self._codes: Dict[str, str] = {}
        self._links: List[str] = []
        self._images: List[str] = []
        self._pending_images: Dict[str, str] = {}
        self._last_height = -1

    # -- content ---------------------------------------------------------- #
    def set_markdown(self, text: str, theme: Optional[Theme] = None,
                     streaming: bool = False) -> None:
        theme = theme or current_theme()
        result = md.render_markdown(
            text, theme, md.RenderOptions(nl2br=True)
        )
        self._codes = result.code_blocks
        self._links = result.links
        self._images = result.images
        self.setHtml(result.html)
        for index, src in enumerate(result.images):
            if not src:
                continue
            placeholder = QPixmap(2, 2)
            placeholder.fill(Qt.transparent)
            self.document().addResource(
                QTextDocument.ImageResource,
                QUrl(f"{md.IMAGE_PREFIX}{index}"), placeholder,
            )
            self._pending_images[f"{md.IMAGE_PREFIX}{index}"] = src
        self.fit()

    def set_plain(self, text: str) -> None:
        self.setPlainText(text or "")
        self.fit()

    def fit(self) -> None:
        doc = self.document()
        width = max(140, self.viewport().width() - 2)
        doc.setTextWidth(width)
        height = int(doc.size().height()) + 10
        if height != self._last_height:
            self._last_height = height
            self.setFixedHeight(max(26, height))
            self.height_changed.emit()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self.fit()

    # -- anchors ---------------------------------------------------------- #
    def _on_anchor(self, url: QUrl) -> None:
        target = url.toString()
        if target.startswith(md.CODE_COPY_PREFIX):
            anchor = target[len(md.CODE_COPY_PREFIX):]
            code = self._codes.get(anchor, "")
            QApplication.clipboard().setText(code)
            toast(self, "Code copied to clipboard", "success", 1800)
            return
        if target.startswith(md.LINK_PREFIX):
            index = int(target[len(md.LINK_PREFIX):] or -1)
            href = self._links[index] if 0 <= index < len(self._links) else ""
            if href:
                self.link_activated.emit(href)
            return
        self.link_activated.emit(target)

    def open_link(self, href: str) -> None:
        if not href:
            return
        url = QUrl(href)
        if url.scheme() in ("http", "https", "mailto"):
            QDesktopServices.openUrl(url)
        elif url.scheme() == "file":
            QDesktopServices.openUrl(url)
        else:
            toast(self, f"Cannot open link: {href}", "warning", 2600)

    # -- images ----------------------------------------------------------- #
    def load_images(self, fetch) -> None:
        """``fetch(src) -> bytes`` runs on a worker; resources are injected after."""
        from gui.workers import run

        for key, src in list(self._pending_images.items()):
            self._pending_images.pop(key, None)

            def _fetch(src=src, key=key) -> None:
                try:
                    data = fetch(src)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("image fetch failed: %s", exc)
                    return

                def _apply(data=data, key=key) -> None:
                    pixmap = QPixmap()
                    if pixmap.loadFromData(data):
                        if pixmap.width() > 720:
                            pixmap = pixmap.scaledToWidth(
                                720, Qt.SmoothTransformation)
                        self.document().addResource(
                            QTextDocument.ImageResource, QUrl(key), pixmap)
                        self.document().markContentsDirty(
                            self.document().begin(), self.document().end())
                        self.fit()

                QTimer.singleShot(0, _apply)

            run(_fetch)


# --------------------------------------------------------------------------- #
# Tool / approval cards
# --------------------------------------------------------------------------- #

class ToolCard(QFrame):
    """Collapsible card for one agent tool execution."""

    def __init__(self, call: ToolCall, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("ToolCard")
        self.call = call
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.toggle = QPushButton("▸")
        self.toggle.setObjectName("Ghost")
        self.toggle.setFixedWidth(22)
        self.toggle.clicked.connect(self._toggle)
        header.addWidget(self.toggle)
        self.title = QLabel()
        self.title.setObjectName("Accent")
        header.addWidget(self.title, 1)
        self.status = QLabel()
        self.status.setObjectName("MetaLine")
        header.addWidget(self.status)
        layout.addLayout(header)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setObjectName("CodeEditor")
        self.output.setMaximumHeight(260)
        self.output.setVisible(False)
        layout.addWidget(self.output)
        self.refresh()

    def _toggle(self) -> None:
        visible = not self.output.isVisible()
        self.output.setVisible(visible)
        self.toggle.setText("▾" if visible else "▸")

    def refresh(self) -> None:
        theme = current_theme()
        label = self.call.tool or "tool"
        if self.call.command:
            label = f"{self.call.tool}  ·  {self.call.command}"
        self.title.setText(label[:220])
        if self.call.running:
            self.status.setText("running…")
            self.status.setStyleSheet(f"color: {theme.accent};")
        elif self.call.exit_code not in (None, 0):
            self.status.setText(f"exit {self.call.exit_code}")
            self.status.setStyleSheet(f"color: {theme.syntax.get('string', '#e5c07b')};")
        else:
            self.status.setText("done")
            self.status.setStyleSheet(f"color: {theme.text_faint};")
        self.output.setPlainText(self.call.output or "")


class ApprovalCard(QFrame):
    """An approval / question card with clickable options."""

    decided = Signal(object, str)      # Approval, decision value

    def __init__(self, approval: Approval, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("ApprovalCard")
        self.approval = approval
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        question = QLabel(approval.question or "psd.ai needs your input")
        question.setWordWrap(True)
        question.setStyleSheet(
            f"color: {current_theme().text}; font-weight: 700; background: transparent;")
        layout.addWidget(question)
        if approval.description:
            detail = QLabel(approval.description)
            detail.setWordWrap(True)
            detail.setObjectName("Muted")
            layout.addWidget(detail)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        for option in approval.options or []:
            button = QPushButton(option.get("label") or option.get("value") or "Send")
            button.setObjectName("Primary" if option.get("primary") else "")
            button.setToolTip(option.get("description") or "")
            value = str(option.get("value") or option.get("label") or "")
            button.clicked.connect(lambda _=False, v=value: self.decided.emit(self.approval, v))
            buttons.addWidget(button)
        buttons.addStretch(1)
        self._button_row = QWidget()
        self._button_row.setLayout(buttons)
        layout.addWidget(self._button_row)

        self.result = QLabel("")
        self.result.setObjectName("MetaLine")
        self.result.setVisible(False)
        layout.addWidget(self.result)

    def resolve(self, decision: str) -> None:
        self.approval.resolved = decision
        self._button_row.setVisible(False)
        self.result.setText(f"→ {decision}")
        self.result.setVisible(True)


# --------------------------------------------------------------------------- #
# Message card
# --------------------------------------------------------------------------- #

class MessageCard(QFrame):
    """One message in the transcript."""

    link_activated = Signal(str)
    approval_decided = Signal(object, str)
    content_changed = Signal()

    def __init__(self, state: MessageState, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        theme = current_theme()
        self.state = state
        self.setObjectName({
            "user": "MessageUser", "assistant": "MessageAssistant",
        }.get(state.role, "MessageSystem"))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 14, 10)
        outer.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(10)
        self.role_label = QLabel(ROLE_LABELS.get(state.role, state.role.title()))
        self.role_label.setObjectName(
            "RoleUser" if state.role == "user" else "RoleAssistant")
        header.addWidget(self.role_label)
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("MetaLine")
        header.addWidget(self.meta_label, 1)
        self.copy_button = QPushButton("copy")
        self.copy_button.setObjectName("Ghost")
        self.copy_button.setFixedHeight(20)
        self.copy_button.clicked.connect(self._copy_content)
        header.addWidget(self.copy_button)
        outer.addLayout(header)

        self.body = RichView(self)
        self.body.link_activated.connect(self.link_activated)
        self.body.height_changed.connect(self.content_changed)
        outer.addWidget(self.body)

        self.tools_layout = QVBoxLayout()
        self.tools_layout.setSpacing(6)
        outer.addLayout(self.tools_layout)

        self.approvals_layout = QVBoxLayout()
        self.approvals_layout.setSpacing(6)
        outer.addLayout(self.approvals_layout)

        self._tool_cards: List[ToolCard] = []
        self._approval_cards: List[ApprovalCard] = []
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(110)
        self._render_timer.timeout.connect(self._render_body)
        self._dirty = False
        self.render()

    # -- rendering -------------------------------------------------------- #
    def render(self) -> None:
        self._render_meta()
        self._render_body()
        self._render_tools()
        self._render_approvals()

    def _render_meta(self) -> None:
        parts = [time.strftime("%H:%M", time.localtime(self.state.timestamp))]
        if self.state.model:
            parts.append(self.state.model)
        if self.state.metrics.get("total_time"):
            parts.append(f"{float(self.state.metrics['total_time']):.1f}s")
        if self.state.stopped:
            parts.append("stopped")
        self.meta_label.setText("  ·  ".join(parts))

    def _render_body(self) -> None:
        self._dirty = False
        text = self.state.content
        if not text and self.state.streaming:
            text = "_…_"
        if len(text) > 120_000:
            self.body.set_plain(text)
        else:
            self.body.set_markdown(text, streaming=self.state.streaming)
        self.content_changed.emit()

    def schedule_render(self) -> None:
        """Throttled re-render for streaming deltas."""
        if not self._render_timer.isActive():
            self._render_timer.start()
        self._dirty = True

    def flush_render(self) -> None:
        self._render_timer.stop()
        self._render_body()

    def _render_tools(self) -> None:
        while self.tools_layout.count():
            item = self.tools_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self._tool_cards = []
        for call in self.state.tools:
            card = ToolCard(call, self)
            self._tool_cards.append(card)
            self.tools_layout.addWidget(card)

    def _render_approvals(self) -> None:
        while self.approvals_layout.count():
            item = self.approvals_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self._approval_cards = []
        for approval in self.state.approvals:
            card = ApprovalCard(approval, self)
            card.decided.connect(self.approval_decided)
            if approval.resolved:
                card.resolve(approval.resolved)
            self._approval_cards.append(card)
            self.approvals_layout.addWidget(card)

    def approval_card(self, approval: Approval) -> ApprovalCard:
        card = ApprovalCard(approval, self)
        card.decided.connect(self.approval_decided)
        self._approval_cards.append(card)
        self.approvals_layout.addWidget(card)
        return card

    # -- updates ---------------------------------------------------------- #
    def append_text(self, text: str) -> None:
        self.state.content += text
        self.schedule_render()

    def add_tool(self, call: ToolCall) -> ToolCard:
        self.state.tools.append(call)
        card = ToolCard(call, self)
        self._tool_cards.append(card)
        self.tools_layout.addWidget(card)
        self.content_changed.emit()
        return card

    def update_last_tool(self, output: str = "", exit_code: Optional[int] = None,
                         running: bool = False, command: str = "") -> None:
        if not self.state.tools:
            return
        call = self.state.tools[-1]
        if output:
            call.output = (call.output + output) if call.output else output
        if exit_code is not None:
            call.exit_code = exit_code
        if command:
            call.command = command
        call.running = running
        if self._tool_cards:
            self._tool_cards[-1].refresh()

    def finalize(self, streaming: bool = False, stopped: bool = False) -> None:
        self.state.streaming = streaming
        self.state.stopped = stopped
        self.flush_render()
        self._render_meta()

    # -- actions ---------------------------------------------------------- #
    def _copy_content(self) -> None:
        QApplication.clipboard().setText(self.state.raw or self.state.content)
        toast(self, "Message copied", "success", 1600)

    def load_images(self, fetch) -> None:
        self.body.load_images(fetch)


# --------------------------------------------------------------------------- #
# Transcript container
# --------------------------------------------------------------------------- #

class Transcript(QScrollArea):
    """Vertically scrolling list of message cards."""

    link_activated = Signal(str)
    approval_decided = Signal(object, str)
    image_requested = Signal(object)     # callable fetch

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._container = QWidget()
        self._container.setStyleSheet("background: transparent;")
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(10, 10, 10, 10)
        self._layout.setSpacing(12)
        self._layout.addStretch(1)
        self.setWidget(self._container)
        self._cards: List[MessageCard] = []
        self._auto_scroll = True
        self.verticalScrollBar().rangeChanged.connect(self._maybe_scroll)

    # -- cards ------------------------------------------------------------ #
    def clear(self) -> None:
        for card in list(self._cards):
            self._layout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()

    def add_message(self, state: MessageState) -> MessageCard:
        card = MessageCard(state, self._container)
        card.link_activated.connect(self.link_activated)
        card.approval_decided.connect(self.approval_decided)
        card.content_changed.connect(self._maybe_scroll)
        self._layout.insertWidget(self._layout.count() - 1, card)
        self._cards.append(card)
        self._maybe_scroll()
        return card

    @property
    def last_card(self) -> Optional[MessageCard]:
        return self._cards[-1] if self._cards else None

    # -- scrolling -------------------------------------------------------- #
    def _maybe_scroll(self, *_args) -> None:
        if not self._auto_scroll:
            return
        bar = self.verticalScrollBar()
        QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))

    def scroll_to_bottom(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    def user_scrolled_up(self) -> bool:
        bar = self.verticalScrollBar()
        return bar.value() < bar.maximum() - 80

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._auto_scroll = not self.user_scrolled_up() or event.angleDelta().y() < 0
        super().wheelEvent(event)
