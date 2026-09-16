"""Local Models workspace screen — the model group lives inside the app now.

Previously ``run.bat`` opened a second console window that downloaded and
served the hardware-fit GGUF group. That whole workflow is embedded here:
hardware report, fit recommendations, download/serve, and a managed
``local_llama.py`` process with a live log pane and a stop button.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QProcess, Qt
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from gui.theme import current_theme
from gui.views.base import View, card, muted
from gui.widgets.dialogs import confirm, prompt
from gui.workers import run

FIT_COLORS = {
    "great": "#4caf50",
    "good": "#8bc34a",
    "ok": "#f0ad4e",
    "marginal": "#ff9800",
    "poor": "#ff4444",
    "no": "#ff4444",
}


class LocalModelsView(View):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Local Models",
            "Your on-machine model group: hardware fit, downloads and the llama.cpp servers.",
            parent,
        )
        self._base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self._proc: Optional[QProcess] = None

        # -- hardware -------------------------------------------------------- #
        self.hw_card = card("This machine")
        hw_layout = self.hw_card.layout_  # type: ignore[attr-defined]
        self.hw_label = QLabel("Probing hardware…")
        self.hw_label.setObjectName("Muted")
        self.hw_label.setWordWrap(True)
        hw_layout.addWidget(self.hw_label)
        self.content_layout.addWidget(self.hw_card)

        # -- fit table -------------------------------------------------------- #
        fit_card = card("Hardware-fit recommendations")
        fit_layout = fit_card.layout_  # type: ignore[attr-defined]
        self.fit_table = QTableWidget(0, 7)
        self.fit_table.setHorizontalHeaderLabels(
            ["Model", "Params", "Quant", "Context", "RAM needed", "Fit", "Score"])
        self.fit_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.fit_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.fit_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.fit_table.verticalHeader().setVisible(False)
        self.fit_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.fit_table.setAlternatingRowColors(True)
        self.fit_table.setMaximumHeight(260)
        fit_layout.addWidget(self.fit_table)
        fit_buttons = QHBoxLayout()
        fit_buttons.setSpacing(8)
        download_button = QPushButton("Download selected")
        download_button.setObjectName("Primary")
        download_button.clicked.connect(self._download_selected)
        fit_buttons.addWidget(download_button)
        serve_button = QPushButton("Serve selected")
        serve_button.clicked.connect(self._serve_selected)
        fit_buttons.addWidget(serve_button)
        cached_button = QPushButton("Cached models")
        cached_button.clicked.connect(self._show_cached)
        fit_buttons.addWidget(cached_button)
        fit_buttons.addStretch(1)
        fit_layout.addLayout(fit_buttons)
        self.content_layout.addWidget(fit_card)

        # -- group runner ------------------------------------------------------ #
        run_card = card("Local model group (llama.cpp servers)")
        run_layout = run_card.layout_  # type: ignore[attr-defined]
        controls = QHBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(muted("First port:"))
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65000)
        self.port_spin.setValue(int(os.getenv("LLAMA_PORT", "8080")))
        controls.addWidget(self.port_spin)
        self.start_button = QPushButton("▶ Start model group")
        self.start_button.setObjectName("Primary")
        self.start_button.clicked.connect(self._start_group)
        controls.addWidget(self.start_button)
        self.stop_button = QPushButton("■ Stop")
        self.stop_button.setObjectName("Danger")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop_group)
        controls.addWidget(self.stop_button)
        self.proc_status = QLabel("stopped")
        self.proc_status.setObjectName("StatusPill")
        controls.addWidget(self.proc_status)
        controls.addStretch(1)
        run_layout.addLayout(controls)
        run_layout.addWidget(muted(
            "Starts the same hardware-fit group the launcher used to run in a second "
            "console window: downloads 3–5 GGUF models on first run, then serves one "
            "llama-server per model and registers them as endpoints. Keep the app open "
            "while the group runs; Stop shuts every server down."))
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("CodeEditor")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(4000)
        self.log_view.setPlaceholderText("Model group output appears here…")
        self.log_view.setMinimumHeight(180)
        run_layout.addWidget(self.log_view)
        self.content_layout.addWidget(run_card, 1)

        # -- cookbook servers ---------------------------------------------------- #
        servers_card = card("Cookbook servers")
        servers_layout = servers_card.layout_  # type: ignore[attr-defined]
        self.servers_table = QTableWidget(0, 4)
        self.servers_table.setHorizontalHeaderLabels(["Repo", "PID", "Port", "Status"])
        self.servers_table.verticalHeader().setVisible(False)
        self.servers_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.servers_table.setMaximumHeight(160)
        servers_layout.addWidget(self.servers_table)
        servers_buttons = QHBoxLayout()
        refresh_servers = QPushButton("Refresh")
        refresh_servers.clicked.connect(self._load_servers)
        servers_buttons.addWidget(refresh_servers)
        kill_button = QPushButton("Kill selected")
        kill_button.setObjectName("Danger")
        kill_button.clicked.connect(self._kill_selected)
        servers_buttons.addWidget(kill_button)
        servers_buttons.addStretch(1)
        servers_layout.addLayout(servers_buttons)
        self.content_layout.addWidget(servers_card)

        self._fit_models: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        self.run(self._load_fit, on_done=self._fit_loaded)
        self._load_servers()

    def _load_fit(self) -> Dict[str, Any]:
        return self.api.hwfit_models()

    def _fit_loaded(self, payload: Dict[str, Any]) -> None:
        system = payload.get("system") or {}
        self._fit_models = payload.get("models") or []
        gpu = system.get("gpu_name")
        self.hw_label.setText(
            f"RAM {system.get('total_ram_gb')} GB total / {system.get('available_ram_gb')} GB free · "
            f"CPU {system.get('cpu_cores')} threads ({system.get('cpu_name')}) · "
            f"GPU {gpu or 'none'} · backend {system.get('backend')}")
        self.fit_table.setRowCount(len(self._fit_models))
        for row, model in enumerate(self._fit_models[:120]):
            fit = str(model.get("fit_level") or "")
            values = [
                model.get("name") or "",
                model.get("parameter_count") or "",
                model.get("quant") or "",
                str(model.get("context") or ""),
                f"{model.get('required_gb')} GB",
                fit,
                str(model.get("score") or ""),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 5:
                    item.setForeground(QColor(FIT_COLORS.get(fit, current_theme().text)))
                self.fit_table.setItem(row, column, item)

    def _selected_model(self) -> Dict[str, Any]:
        row = self.fit_table.currentRow()
        if row < 0 or row >= len(self._fit_models):
            return {}
        return self._fit_models[row]

    # -- downloads / serving -------------------------------------------------- #
    def _download_selected(self) -> None:
        model = self._selected_model()
        sources = model.get("gguf_sources") or []
        repo = sources[0].get("repo") if sources else None
        if not repo:
            self.toast("This model has no GGUF source listed", "warning")
            return
        if not confirm(self, "Download model?",
                       f"{model.get('name')} from {repo}\nThis can be several GB.",
                       yes="Download"):
            return
        run(self.api.download_model, repo,
            on_done=lambda result: self.toast(
                f"Download finished: {str(result)[:160]}", "success", 8000),
            on_error=self.error)

    def _serve_selected(self) -> None:
        model = self._selected_model()
        sources = model.get("gguf_sources") or []
        repo = sources[0].get("repo") if sources else None
        if not repo:
            self.toast("This model has no GGUF source listed", "warning")
            return
        command = prompt(
            self, "Serve model", "llama-server command:",
            f"llama-server -m {repo} --port {self.port_spin.value()} --ctx-size 8192")
        if not command:
            return
        run(self.api.serve_model, repo, command,
            on_done=lambda result: self.toast(
                f"Server started: {str(result)[:160]}", "success"),
            on_error=self.error)
        self._load_servers()

    def _show_cached(self) -> None:
        from gui.widgets.dialogs import info_dialog

        run(self.api.cached_models, on_done=lambda payload: info_dialog(
            self, "Cached models",
            "\n".join(str(entry)[:160] for entry in
                      (payload if isinstance(payload, list) else (payload or {}).get("models", [])))
            or "Nothing cached yet."), on_error=self.error)

    # -- servers ---------------------------------------------------------------- #
    def _load_servers(self) -> None:
        run(self.api.cookbook_state, on_done=self._servers_loaded,
            on_error=lambda _e: None)

    def _servers_loaded(self, payload: Dict[str, Any]) -> None:
        env = payload.get("env") or {}
        servers = env.get("servers") or payload.get("servers") or []
        self.servers_table.setRowCount(len(servers))
        for row, server in enumerate(servers):
            values = [
                server.get("repo_id") or server.get("repo") or "",
                str(server.get("pid") or ""),
                str(server.get("port") or ""),
                server.get("status") or ("running" if server.get("pid") else "?"),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, server.get("pid"))
                self.servers_table.setItem(row, column, item)

    def _kill_selected(self) -> None:
        row = self.servers_table.currentRow()
        if row < 0:
            return
        item = self.servers_table.item(row, 1)
        pid = int(item.data(Qt.UserRole) or 0) if item else 0
        if not pid:
            return
        if not confirm(self, "Kill server?", f"PID {pid} will be terminated.",
                       yes="Kill", destructive=True):
            return
        run(self.api.kill_pid, pid, on_done=lambda _r: self._load_servers(),
            on_error=self.error)

    # -- model group process ------------------------------------------------------- #
    def _script_path(self) -> str:
        return os.path.join(self._base_dir, "scripts", "local_llama.py")

    def _start_group(self) -> None:
        script = self._script_path()
        if not os.path.exists(script):
            self.toast(f"Cannot find {script}", "error")
            return
        if self._proc is not None and self._proc.state() != QProcess.NotRunning:
            self.toast("Model group already running", "warning")
            return
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.setWorkingDirectory(self._base_dir)
        proc.readyReadStandardOutput.connect(lambda: self._read_output(proc))
        proc.finished.connect(self._group_finished)
        proc.errorOccurred.connect(self._group_error)
        env = QProcess.systemEnvironment() if False else None
        self._proc = proc
        args = [script, "--port", str(self.port_spin.value()), "--foreground"]
        self._log(f"$ {sys.executable} {' '.join(args[1:])}")
        proc.start(sys.executable, args)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.proc_status.setText("starting…")
        self.proc_status.setProperty("busy", "true")

    def _read_output(self, proc: QProcess) -> None:
        data = proc.readAllStandardOutput()
        text = bytes(data).decode("utf-8", "replace")
        for line in text.splitlines():
            self._log(line)
            lowered = line.lower()
            if "registered" in lowered or "ready" in lowered or "serving" in lowered:
                self.proc_status.setText("running")
                self.proc_status.setProperty("busy", "false")
                self.proc_status.setProperty("ok", "true")

    def _log(self, line: str) -> None:
        self.log_view.appendPlainText(line)

    def _group_finished(self, exit_code: int, status) -> None:
        self._log(f"[model group exited with code {exit_code}]")
        self._reset_buttons("stopped")
        self._load_servers()

    def _group_error(self, error) -> None:
        self._log(f"[model group process error: {error}]")
        self._reset_buttons("error")

    def _reset_buttons(self, label: str) -> None:
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.proc_status.setText(label)
        self.proc_status.setProperty("busy", "false")
        self.proc_status.setProperty("ok", "false")
        self.proc_status.style().unpolish(self.proc_status)
        self.proc_status.style().polish(self.proc_status)

    def _stop_group(self) -> None:
        proc = self._proc
        if proc is None or proc.state() == QProcess.NotRunning:
            self._reset_buttons("stopped")
            return
        self._log("[stopping model group …]")
        proc.terminate()
        if not proc.waitForFinished(4000):
            proc.kill()
        self._reset_buttons("stopped")
        self._load_servers()

    def deactivate(self) -> None:
        # The group keeps running while the app is open; only the view sleeps.
        pass

    def close_group(self) -> None:
        """Called by the shell on app exit so no orphan servers survive."""
        self._stop_group()
