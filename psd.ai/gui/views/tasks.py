"""Tasks workspace screen: scheduled/agent jobs and their runs."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QHeaderView, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from gui.theme import current_theme
from gui.views.base import View
from gui.widgets.dialogs import confirm, form_dialog, info_dialog
from gui.workers import run

STATUS_COLORS = {
    "active": "#4caf50",
    "paused": "#f0ad4e",
    "running": "#00aaff",
    "completed": "#828997",
    "error": "#ff4444",
}


def _schedule_text(task: Dict[str, Any]) -> str:
    trigger = task.get("trigger_type") or "manual"
    if trigger == "schedule":
        schedule = task.get("schedule") or ""
        if schedule == "cron":
            return f"cron {task.get('cron_expression') or ''}"
        bits = [schedule or "once"]
        if task.get("scheduled_time"):
            bits.append(str(task["scheduled_time"])[:5])
        if task.get("scheduled_day"):
            bits.append(str(task["scheduled_day"]))
        if task.get("scheduled_date"):
            bits.append(str(task["scheduled_date"])[:10])
        return " ".join(bit for bit in bits if bit)
    if trigger == "event":
        return f"on {task.get('trigger_event') or 'event'} ×{task.get('trigger_count') or 1}"
    return trigger


class TasksView(View):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Tasks",
            "Scheduled prompts, event triggers and agent jobs — the desktop scheduler.",
            parent,
        )
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Name", "Type", "Schedule", "Status", "Next run", "Last run"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.doubleClicked.connect(self._edit_current)
        self.set_content(self.table)

        new_button = QPushButton("+ New task")
        new_button.setObjectName("Primary")
        new_button.clicked.connect(self._new_task)
        self.add_action(new_button)
        edit_button = QPushButton("Edit")
        edit_button.clicked.connect(self._edit_current)
        self.add_action(edit_button)
        run_button = QPushButton("Run now")
        run_button.clicked.connect(self._run_current)
        self.add_action(run_button)
        pause_button = QPushButton("Pause / resume")
        pause_button.clicked.connect(self._pause_current)
        self.add_action(pause_button)
        runs_button = QPushButton("Runs")
        runs_button.clicked.connect(self._runs_current)
        self.add_action(runs_button)
        delete_button = QPushButton("Delete")
        delete_button.setObjectName("Danger")
        delete_button.clicked.connect(self._delete_current)
        self.add_action(delete_button)

        self.summary = QLabel("")
        self.summary.setObjectName("MetaLine")
        self.add_action(self.summary)
        self._tasks: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        self.run(self.api.tasks, True, on_done=self._loaded)

    def _loaded(self, tasks: List[Dict[str, Any]]) -> None:
        self._tasks = tasks or []
        theme = current_theme()
        self.table.setRowCount(len(self._tasks))
        for row, task in enumerate(self._tasks):
            status = str(task.get("status") or "")
            items = [
                task.get("name") or "(unnamed)",
                task.get("task_type") or "",
                _schedule_text(task),
                status,
                str(task.get("next_run") or "").replace("T", " ")[:16],
                str((task.get("last_run") or {}).get("started_at")
                    if isinstance(task.get("last_run"), dict)
                    else task.get("last_run") or "").replace("T", " ")[:16],
            ]
            for column, value in enumerate(items):
                item = QTableWidgetItem(str(value))
                if column == 3:
                    item.setForeground(QColor(STATUS_COLORS.get(status, theme.text_muted)))
                item.setData(Qt.UserRole, task.get("id"))
                self.table.setItem(row, column, item)
        active = sum(1 for t in self._tasks if t.get("status") == "active")
        self.summary.setText(f"{len(self._tasks)} tasks · {active} active")

    def _current_id(self) -> str:
        row = self.table.currentRow()
        if row < 0:
            return ""
        item = self.table.item(row, 0)
        return str(item.data(Qt.UserRole) or "") if item else ""

    def _current_task(self) -> Dict[str, Any]:
        task_id = self._current_id()
        return next((t for t in self._tasks if t.get("id") == task_id), {})

    # -- actions ----------------------------------------------------------- #
    def _fields(self, task: Optional[Dict[str, Any]] = None) -> Optional[dict]:
        task = task or {}
        values = form_dialog(self, "Edit task" if task else "New task", [
            ("name", "Name", "text", task.get("name") or ""),
            ("prompt", "Prompt / instruction", "multiline", task.get("prompt") or ""),
            ("task_type", "Type", "combo", ["llm", "action", "shell", "workflow"]),
            ("trigger_type", "Trigger", "combo", ["schedule", "event", "manual", "chain"]),
            ("schedule", "Schedule", "combo", ["daily", "weekly", "monthly", "cron", "once", ""]),
            ("scheduled_time", "Time (HH:MM)", "text", task.get("scheduled_time") or "09:00"),
            ("cron_expression", "Cron expression", "text", task.get("cron_expression") or ""),
            ("output_target", "Output", "combo", ["session", "note", "email", "none"]),
            ("model", "Model override", "text", task.get("model") or ""),
        ], intro="Scheduled tasks run without you being here; results land in the chosen target.")
        if not values:
            return None
        payload = {
            "name": values.get("name") or "Task",
            "prompt": values.get("prompt") or None,
            "task_type": values.get("task_type") or "llm",
            "trigger_type": values.get("trigger_type") or "schedule",
            "schedule": values.get("schedule") or None,
            "scheduled_time": values.get("scheduled_time") or "09:00",
            "cron_expression": values.get("cron_expression") or None,
            "output_target": values.get("output_target") or "session",
            "model": values.get("model") or None,
        }
        return payload

    def _new_task(self) -> None:
        payload = self._fields()
        if not payload:
            return
        run(self.api.create_task, **payload,
            on_done=lambda _r: (self.toast("Task created", "success"), self.refresh()),
            on_error=self.error)

    def _edit_current(self) -> None:
        task = self._current_task()
        if not task:
            return
        payload = self._fields(task)
        if not payload:
            return
        run(self.api.update_task, task["id"], **payload,
            on_done=lambda _r: (self.toast("Task updated", "success"), self.refresh()),
            on_error=self.error)

    def _run_current(self) -> None:
        task = self._current_task()
        if not task:
            return
        run(self.api.run_task, task["id"],
            on_done=lambda _r: (self.toast("Task started", "info"), self.refresh()),
            on_error=self.error)

    def _pause_current(self) -> None:
        task = self._current_task()
        if not task:
            return
        paused = task.get("status") == "paused"
        fn = self.api.resume_task if paused else self.api.pause_task
        run(fn, task["id"], on_done=lambda _r: self.refresh(), on_error=self.error)

    def _runs_current(self) -> None:
        task = self._current_task()
        if not task:
            return

        def _load() -> List[Dict[str, Any]]:
            data = self.api.task_runs(task["id"])
            return (data or {}).get("runs", data if isinstance(data, list) else [])

        def _done(runs: List[Dict[str, Any]]) -> None:
            lines = []
            for entry in runs[:40]:
                lines.append(
                    f"{str(entry.get('started_at') or '')[:19]}  "
                    f"{entry.get('status') or '?'}  "
                    f"{str(entry.get('summary') or entry.get('output') or '')[:120]}")
            info_dialog(self, f"Runs — {task.get('name')}",
                        "\n".join(lines) or "No runs yet.")

        run(_load, on_done=_done, on_error=self.error)

    def _delete_current(self) -> None:
        task = self._current_task()
        if not task:
            return
        if not confirm(self, "Delete task?", f"“{task.get('name')}” will be removed.",
                       yes="Delete", destructive=True):
            return
        run(self.api.delete_task, task["id"],
            on_done=lambda _r: self.refresh(), on_error=self.error)
