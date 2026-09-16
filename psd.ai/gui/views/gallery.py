"""Gallery workspace screen: photo/image grid, previews, uploads, actions."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QLayout, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from gui.theme import current_theme
from gui.views.base import View
from gui.widgets.dialogs import confirm, form_dialog, prompt, save_file
from gui.workers import run


class FlowLayout(QLayout):
    """A wrapping grid layout (the desktop equivalent of the web photo grid)."""

    def __init__(self, parent: Optional[QWidget] = None, margin: int = 8, spacing: int = 10) -> None:
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self._spacing = spacing
        self._items: List[Any] = []

    def addItem(self, item) -> None:  # noqa: N802 - Qt API
        self._items.append(item)

    def count(self) -> int:  # noqa: D102
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802, D102
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):  # noqa: N802, D102
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def spacing(self) -> int:
        return self._spacing

    def expandingDirections(self) -> Qt.Orientations:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(),
                      margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(
            margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0
        for item in self._items:
            space = self._spacing
            widget = item.widget()
            hint = item.sizeHint()
            if widget is not None:
                hint = widget.sizeHint()
            next_x = x + hint.width() + space
            if next_x - space > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + space
                next_x = x + hint.width() + space
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint.size() if hasattr(hint, "size") else hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()


class ThumbCard(QFrame):
    """One gallery tile."""

    clicked = Signal(object)

    def __init__(self, item: Dict[str, Any], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.item = item
        self.setObjectName("ThumbCard")
        self.setFixedSize(168, 190)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        self.image = QLabel()
        self.image.setFixedSize(150, 130)
        self.image.setAlignment(Qt.AlignCenter)
        self.image.setStyleSheet(
            f"background-color: {current_theme().panel}; border-radius: 6px;")
        self.image.setText("…")
        layout.addWidget(self.image)
        name = QLabel((item.get("filename") or item.get("prompt") or "image")[:26])
        name.setToolTip(item.get("prompt") or item.get("filename") or "")
        name.setObjectName("SessionTitle")
        layout.addWidget(name)
        meta = QLabel((item.get("model") or item.get("size") or "")[:26])
        meta.setObjectName("SessionMeta")
        layout.addWidget(meta)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit(self.item)
        super().mousePressEvent(event)

    def set_pixmap(self, pixmap: QPixmap) -> None:
        if pixmap.isNull():
            self.image.setText("no preview")
            return
        scaled = pixmap.scaled(150, 130, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.image.setPixmap(scaled)


class PreviewDialog(QDialog):
    """Full-size preview with per-image actions."""

    def __init__(self, item: Dict[str, Any], pixmap: QPixmap, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.item = item
        self.setWindowTitle(item.get("filename") or "image")
        self.resize(860, 700)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        view = QLabel()
        view.setAlignment(Qt.AlignCenter)
        if not pixmap.isNull():
            view.setPixmap(pixmap.scaled(820, 560, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        layout.addWidget(view, 1)

        info = QLabel(
            f"<b>{item.get('filename') or ''}</b><br/>"
            f"{(item.get('prompt') or '')[:400]}<br/>"
            f"tags: {item.get('tags') or item.get('ai_tags') or '—'} · "
            f"model: {item.get('model') or '—'} · {str(item.get('created_at') or '')[:16]}")
        info.setWordWrap(True)
        info.setTextFormat(Qt.RichText)
        info.setObjectName("Muted")
        layout.addWidget(info)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.actions: Dict[str, QPushButton] = {}
        for key, label in (("rename", "Rename"), ("rotate", "Rotate 90°"),
                           ("save", "Save as…"), ("delete", "Delete")):
            button = QPushButton(label)
            if key == "delete":
                button.setObjectName("Danger")
            buttons.addWidget(button)
            self.actions[key] = button
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        layout.addLayout(buttons)


class GalleryView(View):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Gallery",
            "Generated images and imported photos, with editing hooks.",
            parent,
        )
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.grid_widget = QWidget()
        self.flow = FlowLayout(self.grid_widget)
        self.scroll.setWidget(self.grid_widget)
        self.set_content(self.scroll)

        upload = QPushButton("⬆ Import images")
        upload.setObjectName("Primary")
        upload.clicked.connect(self._upload)
        self.add_action(upload)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        self.add_action(refresh)
        self.stats_label = QLabel("")
        self.stats_label.setObjectName("MetaLine")
        self.add_action(self.stats_label)
        self._items: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        self.run(self._load, on_done=self._loaded)

    def _load(self) -> Dict[str, Any]:
        library = self.api.gallery_library(limit=100)
        stats = {}
        try:
            stats = self.api.gallery_stats()
        except Exception:  # noqa: BLE001
            stats = {}
        return {"library": library, "stats": stats}

    def _loaded(self, payload: Dict[str, Any]) -> None:
        library = payload.get("library") or {}
        self._items = library.get("items") or []
        stats = payload.get("stats") or {}
        self.stats_label.setText(
            f"{stats.get('total_photos', len(self._items))} images · "
            f"{stats.get('total_size_human', '')} · {len(self._items)} shown")
        while self.flow.count():
            item = self.flow.takeAt(0)
            widget = item.widget() if item else None
            if widget:
                widget.deleteLater()
        for entry in self._items:
            tile = ThumbCard(entry, self.grid_widget)
            tile.clicked.connect(self._open_preview)
            self.flow.addWidget(tile)
            run(self._fetch_thumb, entry, on_done=lambda pix, t=tile: t.set_pixmap(pix),
                on_error=lambda _e, t=tile: t.set_pixmap(QPixmap()))

    def _fetch_thumb(self, item: Dict[str, Any]) -> QPixmap:
        url = item.get("url") or ""
        filename = item.get("filename") or ""
        data = b""
        if url:
            data = self.api.gallery_file_bytes(url)
        elif filename:
            data = self.api.generated_image_bytes(filename)
        pixmap = QPixmap()
        pixmap.loadFromData(data)
        return pixmap

    # -- actions ----------------------------------------------------------- #
    def _upload(self) -> None:
        from gui.widgets.dialogs import choose_files

        paths = choose_files(self, "Import images", "Images (*.png *.jpg *.jpeg *.webp *.gif)")
        if not paths:
            return
        run(self.api.gallery_upload, paths,
            on_done=lambda _r: (self.toast(f"Imported {len(paths)} image(s)", "success"),
                                self.refresh()),
            on_error=self.error)

    def _open_preview(self, item: Dict[str, Any]) -> None:
        run(self._fetch_thumb, item,
            on_done=lambda pixmap: self._show_preview(item, pixmap),
            on_error=self.error)

    def _show_preview(self, item: Dict[str, Any], pixmap: QPixmap) -> None:
        dialog = PreviewDialog(item, pixmap, self)
        image_id = item.get("id") or ""
        dialog.actions["rename"].clicked.connect(lambda: self._rename(image_id, dialog))
        dialog.actions["rotate"].clicked.connect(lambda: self._rotate(image_id, dialog))
        dialog.actions["save"].clicked.connect(lambda: self._save_as(item, pixmap))
        dialog.actions["delete"].clicked.connect(lambda: self._delete(image_id, dialog))
        dialog.exec()

    def _rename(self, image_id: str, dialog: PreviewDialog) -> None:
        name = prompt(self, "Rename image", "New name:",
                      dialog.item.get("filename") or "")
        if not name:
            return
        run(self.api.gallery_rename, image_id, name,
            on_done=lambda _r: (dialog.accept(), self.refresh()), on_error=self.error)

    def _rotate(self, image_id: str, dialog: PreviewDialog) -> None:
        run(self.api.gallery_rotate, image_id, 90,
            on_done=lambda _r: (dialog.accept(), self.refresh()), on_error=self.error)

    def _save_as(self, item: Dict[str, Any], pixmap: QPixmap) -> None:
        path = save_file(self, "Save image", item.get("filename") or "image.png",
                         "Images (*.png *.jpg)")
        if path and not pixmap.isNull():
            pixmap.save(path)
            self.toast(f"Saved {path}", "success")

    def _delete(self, image_id: str, dialog: PreviewDialog) -> None:
        if not confirm(self, "Delete image?", "This removes it from the gallery.",
                       yes="Delete", destructive=True):
            return
        run(self.api.gallery_delete, image_id,
            on_done=lambda _r: (dialog.accept(), self.refresh()), on_error=self.error)
