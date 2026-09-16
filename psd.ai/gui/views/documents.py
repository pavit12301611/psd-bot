"""Documents workspace screen: library + editor."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QSplitter, QTextBrowser, QVBoxLayout, QWidget,
)

from gui.theme import current_theme
from gui.views.base import View
from gui.widgets.dialogs import confirm, save_file
from gui.workers import run


class DocumentsView(View):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Documents",
            "The writing-first editor: markdown documents the agent can read and edit.",
            parent,
        )
        self.library = QListWidget()
        self.library.setMinimumWidth(230)
        self.library.currentItemChanged.connect(self._on_select)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search documents…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self._render_library())

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        left_layout.addWidget(self.search)
        left_layout.addWidget(self.library, 1)
        self.stats_label = QLabel("")
        self.stats_label.setObjectName("MetaLine")
        left_layout.addWidget(self.stats_label)

        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Document title")
        top.addWidget(self.title_edit, 1)
        self.dirty_label = QLabel("")
        self.dirty_label.setObjectName("MetaLine")
        top.addWidget(self.dirty_label)
        editor_layout.addLayout(top)

        self.body_edit = QPlainTextEdit()
        self.body_edit.setObjectName("CodeEditor")
        self.body_edit.setPlaceholderText("Start writing… the agent can edit this too.")
        self.body_edit.document().contentsChanged.connect(self._mark_dirty)
        editor_layout.addWidget(self.body_edit, 1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.new_button = QPushButton("+ New")
        self.new_button.setObjectName("Primary")
        self.new_button.clicked.connect(self._new_document)
        actions.addWidget(self.new_button)
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self._save)
        actions.addWidget(self.save_button)
        self.preview_button = QPushButton("Preview")
        self.preview_button.setCheckable(True)
        self.preview_button.toggled.connect(self._toggle_preview)
        actions.addWidget(self.preview_button)
        self.versions_button = QPushButton("Versions")
        self.versions_button.clicked.connect(self._show_versions)
        actions.addWidget(self.versions_button)
        self.export_button = QPushButton("Export PDF")
        self.export_button.clicked.connect(self._export_pdf)
        actions.addWidget(self.export_button)
        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("Danger")
        self.delete_button.clicked.connect(self._delete)
        actions.addWidget(self.delete_button)
        actions.addStretch(1)
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("MetaLine")
        actions.addWidget(self.meta_label)
        editor_layout.addLayout(actions)

        self.preview = QTextBrowser()
        self.preview.setObjectName("RichView")
        self.preview.setOpenExternalLinks(False)
        self.preview.setVisible(False)
        editor_layout.addWidget(self.preview, 1)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(editor)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 900])
        self.set_content(splitter)

        self._docs: List[Dict[str, Any]] = []
        self._current: str = ""
        self._dirty = False

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        self.run(self.api.documents, on_done=self._loaded)

    def _loaded(self, payload: Dict[str, Any]) -> None:
        self._docs = (payload or {}).get("documents", []) or []
        self.stats_label.setText(
            f"{len(self._docs)} documents · {(payload or {}).get('total', len(self._docs))} total")
        self._render_library()

    def _render_library(self) -> None:
        query = self.search.text().strip().lower()
        current = self._current
        self.library.blockSignals(True)
        self.library.clear()
        for doc in self._docs:
            title = doc.get("title") or "Untitled"
            if query and query not in title.lower():
                continue
            item = QListWidgetItem()
            item.setData(Qt.UserRole, doc.get("id"))
            updated = doc.get("updated_at") or doc.get("created_at") or ""
            item.setText(f"{title}\n{str(updated)[:16]}  ·  {doc.get('language') or 'text'}")
            self.library.addItem(item)
            if doc.get("id") == current:
                self.library.setCurrentItem(item)
        self.library.blockSignals(False)

    def _on_select(self, current: Optional[QListWidgetItem], _prev) -> None:
        if current is None:
            return
        doc_id = str(current.data(Qt.UserRole) or "")
        if doc_id == self._current:
            return
        if self._dirty and not confirm(self, "Discard changes?",
                                       "This document has unsaved edits.", yes="Discard"):
            self.library.blockSignals(True)
            self.library.setCurrentItem(_prev)
            self.library.blockSignals(False)
            return
        self._current = doc_id
        self.run(self.api.document, doc_id, on_done=self._document_loaded)

    def _document_loaded(self, doc: Dict[str, Any]) -> None:
        doc = doc.get("document", doc) if isinstance(doc, dict) else doc
        self.title_edit.blockSignals(True)
        self.body_edit.blockSignals(True)
        self.title_edit.setText(doc.get("title") or "")
        self.body_edit.setPlainText(doc.get("content") or "")
        self.title_edit.blockSignals(False)
        self.body_edit.blockSignals(False)
        self._dirty = False
        self.dirty_label.setText("")
        self.meta_label.setText(
            f"{doc.get('language') or 'text'} · v{doc.get('version', 1)} · "
            f"updated {str(doc.get('updated_at') or '')[:16]}")
        if self.preview.isVisible():
            self._render_preview()

    def _mark_dirty(self) -> None:
        self._dirty = True
        self.dirty_label.setText("unsaved changes")

    # -- actions ----------------------------------------------------------- #
    def _new_document(self) -> None:
        from gui.widgets.dialogs import form_dialog

        values = form_dialog(self, "New document", [
            ("title", "Title", "text", "Untitled"),
            ("language", "Language", "combo",
             ["markdown", "text", "python", "javascript", "html", "csv", "json"]),
        ])
        if not values:
            return
        self.run(self.api.create_document, values.get("title") or "Untitled", "",
                 values.get("language") or "",
                 on_done=lambda doc: (self.refresh(), self._open_new(doc)))

    def _open_new(self, doc: Dict[str, Any]) -> None:
        doc_id = (doc or {}).get("id") or (doc or {}).get("document", {}).get("id")
        if doc_id:
            self._current = doc_id
            self.run(self.api.document, doc_id, on_done=self._document_loaded)

    def _save(self) -> None:
        if not self._current:
            self.toast("Select or create a document first", "warning")
            return
        content = self.body_edit.toPlainText()
        title = self.title_edit.text().strip()

        def _do() -> Dict[str, Any]:
            result = self.api.save_document(self._current, content)
            if title:
                self.api.patch_document(self._current, title=title)
            return result

        run(_do, on_done=self._saved, on_error=self.error)

    def _saved(self, _result: Any) -> None:
        self._dirty = False
        self.dirty_label.setText("saved")
        self.toast("Document saved", "success", 1800)
        self.refresh()

    def _toggle_preview(self, on: bool) -> None:
        self.preview.setVisible(on)
        self.body_edit.setVisible(not on)
        if on:
            self._render_preview()

    def _render_preview(self) -> None:
        from gui import markdown_html as md

        result = md.render_markdown(self.body_edit.toPlainText(), current_theme())
        self.preview.setHtml(result.html)

    def _show_versions(self) -> None:
        if not self._current:
            return
        from gui.widgets.dialogs import info_dialog

        def _load() -> List[Dict[str, Any]]:
            return self.api.document_versions(self._current) or []

        def _done(versions: List[Dict[str, Any]]) -> None:
            lines = [f"v{v.get('version')}: {str(v.get('created_at'))[:19]}  "
                     f"({v.get('chars') or v.get('size') or '?'} chars)" for v in versions]
            info_dialog(self, "Document versions",
                        "\n".join(lines) if lines else "No versions yet.",
                        details="Select a version in the list and use Restore to roll back.")

        run(_load, on_done=_done, on_error=self.error)

    def _export_pdf(self) -> None:
        if not self._current:
            return
        path = save_file(self, "Export PDF", "document.pdf", "PDF files (*.pdf)")
        if not path:
            return

        def _do() -> None:
            data = self.api.export_document_pdf(self._current)
            with open(path, "wb") as handle:
                handle.write(data)

        run(_do, on_done=lambda _r: self.toast(f"PDF saved to {path}", "success"),
            on_error=self.error)

    def _delete(self) -> None:
        if not self._current:
            return
        if not confirm(self, "Delete document?", "This cannot be undone.",
                       yes="Delete", destructive=True):
            return
        doc_id = self._current
        self.run(self.api.delete_document, doc_id,
                 on_done=lambda _r: (self.refresh(), self.new_chat_state()))

    def new_chat_state(self) -> None:
        self._current = ""
        self.title_edit.clear()
        self.body_edit.clear()
