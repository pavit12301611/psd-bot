"""Email workspace screen: accounts, folders, inbox, reader, composer."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QHeaderView, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QTextBrowser, QVBoxLayout, QWidget,
)

from gui.theme import current_theme
from gui.views.base import View
from gui.widgets.dialogs import confirm, form_dialog
from gui.widgets.transcript import RichView
from gui.workers import run

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(html_text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html_text or "")
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class EmailView(View):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Email",
            "IMAP/SMTP inbox with triage, summaries and AI reply drafts.",
            parent,
        )
        top = QHBoxLayout()
        top.setSpacing(8)
        self.account_box = QComboBox()
        self.account_box.setMinimumWidth(170)
        self.account_box.currentIndexChanged.connect(lambda _i: self._reload_folders())
        top.addWidget(self.account_box)
        self.folder_box = QComboBox()
        self.folder_box.setMinimumWidth(140)
        self.folder_box.currentIndexChanged.connect(lambda _i: self._reload_messages())
        top.addWidget(self.folder_box)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search mailbox…")
        self.search_edit.returnPressed.connect(self._search)
        top.addWidget(self.search_edit, 1)
        search_button = QPushButton("Search")
        search_button.clicked.connect(self._search)
        top.addWidget(search_button)
        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self.refresh)
        top.addWidget(refresh_button)
        top_wrap = QWidget()
        top_wrap.setLayout(top)
        self.content_layout.addWidget(top_wrap)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["", "From", "Subject", "Date"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.itemSelectionChanged.connect(self._on_selected)

        self.reader = RichView()
        self.reader.setMinimumHeight(220)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(self.reader)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.set_content(splitter)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        compose = QPushButton("✉ Compose")
        compose.setObjectName("Primary")
        compose.clicked.connect(self._compose)
        actions.addWidget(compose)
        for label, handler in (
            ("Mark unread", self._mark_unread),
            ("Flag", self._flag),
            ("Delete", self._delete),
            ("Summarize", self._summarize),
            ("AI reply", self._ai_reply),
        ):
            button = QPushButton(label)
            if label == "Delete":
                button.setObjectName("Danger")
            button.clicked.connect(handler)
            actions.addWidget(button)
        actions.addStretch(1)
        self.status_label = QLabel("")
        self.status_label.setObjectName("MetaLine")
        actions.addWidget(self.status_label)
        actions_wrap = QWidget()
        actions_wrap.setLayout(actions)
        self.content_layout.addWidget(actions_wrap)

        self._accounts: List[Dict[str, Any]] = []
        self._messages: List[Dict[str, Any]] = []
        self._current: Dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        self.run(self.api.email_accounts, on_done=self._accounts_loaded)

    def _accounts_loaded(self, accounts: List[Dict[str, Any]]) -> None:
        self._accounts = accounts or []
        current = self.account_box.currentData()
        self.account_box.blockSignals(True)
        self.account_box.clear()
        self.account_box.addItem("Default account", "")
        for account in self._accounts:
            self.account_box.addItem(
                account.get("name") or account.get("imap_user") or "account",
                account.get("id") or account.get("account_id") or "")
        index = self.account_box.findData(current or "")
        self.account_box.setCurrentIndex(max(0, index))
        self.account_box.blockSignals(False)
        self._reload_folders()

    def _account_id(self) -> str:
        return str(self.account_box.currentData() or "")

    def _reload_folders(self) -> None:
        account_id = self._account_id()
        run(self.api.email_folders, account_id,
            on_done=self._folders_loaded, on_error=self._folders_failed)

    def _folders_failed(self, exc: BaseException) -> None:
        self.folder_box.blockSignals(True)
        self.folder_box.clear()
        self.folder_box.addItem("INBOX", "INBOX")
        self.folder_box.blockSignals(False)
        self.status_label.setText("no mailbox connected")
        self._reload_messages()

    def _folders_loaded(self, folders: List[Dict[str, Any]]) -> None:
        current = self.folder_box.currentData() or "INBOX"
        self.folder_box.blockSignals(True)
        self.folder_box.clear()
        names = []
        for folder in folders or []:
            name = folder.get("name") if isinstance(folder, dict) else str(folder)
            if name and name not in names:
                names.append(name)
        if not names:
            names = ["INBOX"]
        for name in names:
            self.folder_box.addItem(name, name)
        index = self.folder_box.findData(current)
        self.folder_box.setCurrentIndex(max(0, index))
        self.folder_box.blockSignals(False)
        self._reload_messages()

    def _reload_messages(self) -> None:
        folder = str(self.folder_box.currentData() or "INBOX")
        run(self.api.email_list, folder, 60, 0, "all", self._account_id(),
            on_done=self._messages_loaded, on_error=self.error)

    def _messages_loaded(self, payload: Dict[str, Any]) -> None:
        payload = payload or {}
        self._messages = payload.get("emails") or payload.get("messages") or payload.get("items") or []
        self.table.setRowCount(len(self._messages))
        for row, message in enumerate(self._messages):
            unread = bool(message.get("unread") or message.get("is_unread")
                          or (not message.get("seen", True)))
            flag = "●" if unread else " "
            starred = "★" if message.get("flagged") or message.get("flagged_") else ""
            values = [
                f"{flag}{starred}",
                message.get("from") or message.get("from_address") or "",
                message.get("subject") or "(no subject)",
                str(message.get("date") or "")[:22],
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.UserRole, message)
                if unread:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                self.table.setItem(row, column, item)
        self.status_label.setText(
            f"{len(self._messages)} messages in {self.folder_box.currentData() or 'INBOX'}"
            + (f" · {payload.get('error')}" if payload.get("error") else ""))

    # -- selection / reading ------------------------------------------------ #
    def _on_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        item = self.table.item(row, 0)
        message = item.data(Qt.UserRole) if item else None
        if not message:
            return
        self._current = message
        uid = str(message.get("uid") or message.get("id") or "")
        folder = str(self.folder_box.currentData() or "INBOX")
        run(self.api.email_read, uid, folder, self._account_id(),
            on_done=self._message_loaded, on_error=self.error)
        if message.get("unread") or message.get("is_unread"):
            run(self.api.email_mark_read, uid, folder, self._account_id(),
                on_done=lambda _r: None, on_error=lambda _e: None)

    def _message_loaded(self, payload: Dict[str, Any]) -> None:
        message = payload.get("email") or payload or {}
        subject = message.get("subject") or self._current.get("subject") or "(no subject)"
        sender = message.get("from") or message.get("from_address") or self._current.get("from") or ""
        date = str(message.get("date") or self._current.get("date") or "")
        body = message.get("text") or message.get("body") or ""
        html_body = message.get("html") or ""
        if not body and html_body:
            body = _strip_html(html_body)
        header = (
            f"<h3 style='color:{current_theme().text};'>{_esc(subject)}</h3>"
            f"<p style='color:{current_theme().text_muted};'>"
            f"From: {_esc(sender)}<br/>Date: {_esc(date)}</p><hr/>"
        )
        from gui import markdown_html as md

        rendered = md.render_markdown(body or "_(empty)_", current_theme())
        self.reader.setHtml(header + rendered.html)
        self.reader.load_images(self._fetch_image)

    def _fetch_image(self, src: str) -> bytes:
        return self.api.gallery_file_bytes(src)

    # -- actions -------------------------------------------------------------- #
    def _compose(self, to: str = "", subject: str = "", body: str = "") -> None:
        values = form_dialog(self, "Compose email", [
            ("to", "To", "text", to),
            ("cc", "Cc", "text", ""),
            ("subject", "Subject", "text", subject),
            ("body", "Message", "multiline", body),
        ])
        if not values or not values.get("to"):
            return
        run(self.api.email_send, values["to"], values.get("subject") or "(no subject)",
            values.get("body") or "", cc=values.get("cc") or "",
            account_id=self._account_id(),
            on_done=lambda _r: self.toast("Email sent", "success"),
            on_error=self.error)

    def _mark_unread(self) -> None:
        uid, folder = self._uid_folder()
        if uid:
            run(self.api.email_mark_unread, uid, folder, self._account_id(),
                on_done=lambda _r: self._reload_messages(), on_error=self.error)

    def _flag(self) -> None:
        uid, folder = self._uid_folder()
        if uid:
            run(self.api.email_flag, uid, folder, self._account_id(),
                on_done=lambda _r: self._reload_messages(), on_error=self.error)

    def _delete(self) -> None:
        uid, folder = self._uid_folder()
        if not uid:
            return
        if not confirm(self, "Delete email?", "It moves to Trash on the server.",
                       yes="Delete", destructive=True):
            return
        run(self.api.email_delete, uid, folder, self._account_id(),
            on_done=lambda _r: self._reload_messages(), on_error=self.error)

    def _summarize(self) -> None:
        uid, folder = self._uid_folder()
        if not uid:
            return
        run(self.api.email_summarize, uid, folder, self._account_id(),
            on_done=self._show_ai_text, on_error=self.error)

    def _ai_reply(self) -> None:
        uid, folder = self._uid_folder()
        if not uid:
            return

        def _draft() -> Dict[str, Any]:
            return self.api.post("/api/email/ai-reply", params={"account_id": self._account_id() or None},
                                 json_body={"uid": uid, "folder": folder})

        run(_draft, on_done=self._reply_draft_ready, on_error=self.error)

    def _reply_draft_ready(self, payload: Dict[str, Any]) -> None:
        draft = payload.get("reply") or payload.get("body") or payload.get("draft") or ""
        to = self._current.get("from") or self._current.get("from_address") or ""
        subject = f"Re: {self._current.get('subject') or ''}".strip()
        self._compose(to=str(to), subject=subject, body=str(draft))

    def _show_ai_text(self, payload: Dict[str, Any]) -> None:
        text = payload.get("summary") or payload.get("text") or str(payload)
        from gui.widgets.dialogs import info_dialog

        info_dialog(self, "Summary", str(text)[:2000])

    def _uid_folder(self):
        message = self._current or {}
        return (str(message.get("uid") or message.get("id") or ""),
                str(self.folder_box.currentData() or "INBOX"))

    def _search(self) -> None:
        query = self.search_edit.text().strip()
        if not query:
            self._reload_messages()
            return
        run(self.api.email_search, query, str(self.folder_box.currentData() or "INBOX"),
            self._account_id(), on_done=self._messages_loaded, on_error=self.error)


def _esc(text: Any) -> str:
    from gui.markdown_html import escape

    return escape(text)
