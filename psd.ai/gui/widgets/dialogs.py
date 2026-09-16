"""Modal dialogs: confirmations, inputs, and error reports with details."""

from __future__ import annotations

import traceback
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from gui.theme import SEMANTIC, current_theme


def confirm(parent: Optional[QWidget], title: str, text: str,
            yes: str = "Yes", no: str = "Cancel", destructive: bool = False) -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Warning if destructive else QMessageBox.Question)
    yes_btn = box.addButton(yes, QMessageBox.YesRole)
    if destructive:
        yes_btn.setProperty("objectName", "Danger")
    box.addButton(no, QMessageBox.NoRole)
    box.setDefaultButton(no and box.buttons()[-1] or yes_btn)
    box.exec()
    return box.clickedButton() is yes_btn


def prompt(parent: Optional[QWidget], title: str, label: str, value: str = "",
           password: bool = False) -> Optional[str]:
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(10)
    text_label = QLabel(label)
    text_label.setWordWrap(True)
    layout.addWidget(text_label)
    field = QLineEdit(value)
    if password:
        field.setEchoMode(QLineEdit.Password)
    layout.addWidget(field)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    field.selectAll()
    if dialog.exec() == QDialog.Accepted:
        return field.text()
    return None


def prompt_password(parent: Optional[QWidget], title: str, label: str) -> Optional[str]:
    return prompt(parent, title, label, password=True)


def error_dialog(parent: Optional[QWidget], title: str, message: str,
                 details: str = "") -> None:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Critical)
    box.setText(message)
    if details:
        box.setDetailedText(details)
    box.exec()


def show_exception(parent: Optional[QWidget], exc: BaseException,
                   title: str = "Something went wrong",
                   message: Optional[str] = None) -> None:
    from gui.workers import describe_error

    text = message or describe_error(exc)
    details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    error_dialog(parent, title, text, details)


def info_dialog(parent: Optional[QWidget], title: str, text: str, details: str = "") -> None:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Information)
    box.setText(text)
    if details:
        box.setDetailedText(details)
    box.exec()


def choose_files(parent: Optional[QWidget], title: str = "Attach files",
                 filter: str = "All files (*)") -> List[str]:
    paths, _ = QFileDialog.getOpenFileNames(parent, title, "", filter)
    return list(paths or [])


def choose_directory(parent: Optional[QWidget], title: str = "Choose folder") -> Optional[str]:
    path = QFileDialog.getExistingDirectory(parent, title, "")
    return path or None


def save_file(parent: Optional[QWidget], title: str, suggested: str,
              filter: str = "All files (*)") -> Optional[str]:
    path, _ = QFileDialog.getSaveFileName(parent, title, suggested, filter)
    return path or None


class FormDialog(QDialog):
    """A labelled form dialog; returns the collected values as a dict."""

    def __init__(self, parent: Optional[QWidget], title: str,
                 fields: Sequence[Tuple[str, str, str, object]] = None,
                 intro: str = "") -> None:
        """``fields`` is a sequence of ``(key, label, kind, default)`` where kind
        is one of ``text``, ``password``, ``number``, ``check``, ``multiline``,
        ``combo`` (default is the list of options)."""
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(430)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(12)
        if intro:
            label = QLabel(intro)
            label.setWordWrap(True)
            outer.addWidget(label)
        form = QFormLayout()
        form.setSpacing(8)
        self._fields: dict = {}
        self._widgets: dict = {}
        for key, label, kind, default in fields or []:
            self._fields[key] = (kind, default)
            widget: QWidget
            if kind == "password":
                widget = QLineEdit(str(default or ""))
                widget.setEchoMode(QLineEdit.Password)
            elif kind == "number":
                widget = QLineEdit(str(default if default not in (None, "") else ""))
            elif kind == "check":
                widget = QCheckBox()
                widget.setChecked(bool(default))
            elif kind == "multiline":
                widget = QPlainTextEdit(str(default or ""))
                widget.setMaximumHeight(120)
            elif kind == "combo":
                from PySide6.QtWidgets import QComboBox

                widget = QComboBox()
                widget.addItems([str(item) for item in (default or [])])
            else:
                widget = QLineEdit(str(default or ""))
            self._widgets[key] = widget
            form.addRow(label, widget)
        outer.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def values(self) -> dict:
        out = {}
        for key, (kind, default) in self._fields.items():
            widget = self._widgets[key]
            if kind == "check":
                out[key] = bool(widget.isChecked())
            elif kind == "multiline":
                out[key] = widget.toPlainText()
            elif kind == "number":
                text = widget.text().strip()
                out[key] = float(text) if text.replace(".", "", 1).replace("-", "", 1).isdigit() else None
            elif kind == "combo":
                out[key] = widget.currentText()
            else:
                out[key] = widget.text().strip()
        return out


def form_dialog(parent: Optional[QWidget], title: str,
                fields: Sequence[Tuple[str, str, str, object]] = None,
                intro: str = "") -> Optional[dict]:
    dialog = FormDialog(parent, title, fields, intro)
    if dialog.exec() == QDialog.Accepted:
        return dialog.values()
    return None
