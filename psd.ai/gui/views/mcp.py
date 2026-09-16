"""MCP workspace screen: Model Context Protocol servers and their tools."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QHeaderView, QLabel, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QWidget,
)

from gui.views.base import View
from gui.widgets.dialogs import confirm, form_dialog
from gui.workers import run


class McpView(View):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "MCP Servers",
            "External Model Context Protocol servers the agent can call.",
            parent,
        )
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Server", "Transport", "Target", "Status"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)

        self.tools_table = QTableWidget(0, 2)
        self.tools_table.setHorizontalHeaderLabels(["Tool", "Description"])
        self.tools_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tools_table.verticalHeader().setVisible(False)
        self.tools_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(self.tools_table)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        self.set_content(splitter)

        add_button = QPushButton("+ Add server")
        add_button.setObjectName("Primary")
        add_button.clicked.connect(self._add)
        self.add_action(add_button)
        delete_button = QPushButton("Remove")
        delete_button.setObjectName("Danger")
        delete_button.clicked.connect(self._delete)
        self.add_action(delete_button)
        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self.refresh)
        self.add_action(refresh_button)
        self.count_label = QLabel("")
        self.count_label.setObjectName("MetaLine")
        self.add_action(self.count_label)
        self._servers: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        self.run(self._load, on_done=self._loaded)

    def _load(self) -> Dict[str, Any]:
        servers = self.api.mcp_servers()
        tools = []
        try:
            payload = self.api.mcp_tools()
            tools = payload if isinstance(payload, list) else (payload or {}).get("tools", [])
        except Exception:  # noqa: BLE001
            tools = []
        return {"servers": servers, "tools": tools}

    def _loaded(self, payload: Dict[str, Any]) -> None:
        self._servers = payload.get("servers") or []
        self.table.setRowCount(len(self._servers))
        for row, server in enumerate(self._servers):
            values = [
                server.get("name") or server.get("id") or "",
                server.get("transport") or "",
                server.get("url") or server.get("command") or "",
                server.get("status") or ("enabled" if server.get("enabled", True) else "disabled"),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.UserRole, server.get("id") or server.get("name"))
                self.table.setItem(row, column, item)
        tools = payload.get("tools") or []
        self.tools_table.setRowCount(len(tools))
        for row, tool in enumerate(tools):
            name = tool.get("name") or ""
            description = tool.get("description") or ""
            self.tools_table.setItem(row, 0, QTableWidgetItem(str(name)))
            self.tools_table.setItem(row, 1, QTableWidgetItem(str(description)[:200]))
        self.count_label.setText(f"{len(self._servers)} servers · {len(tools)} tools")

    def _add(self) -> None:
        values = form_dialog(self, "Add MCP server", [
            ("name", "Name", "text", ""),
            ("transport", "Transport", "combo", ["stdio", "http", "sse"]),
            ("command", "Command (stdio)", "text", ""),
            ("args", "Arguments (space separated)", "text", ""),
            ("url", "URL (http/sse)", "text", ""),
        ])
        if not values or not values.get("name"):
            return
        run(self.api.post, "/api/mcp/servers", data={
            "name": values["name"],
            "transport": values.get("transport") or "stdio",
            "command": values.get("command") or None,
            "args": values.get("args") or "",
            "url": values.get("url") or None,
        }, on_done=lambda _r: (self.toast("Server added", "success"), self.refresh()),
            on_error=self.error)

    def _delete(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._servers):
            return
        server = self._servers[row]
        server_id = server.get("id") or server.get("name")
        if not confirm(self, "Remove MCP server?", server.get("name") or "",
                       yes="Remove", destructive=True):
            return
        run(self.api.delete_mcp_server, server_id,
            on_done=lambda _r: self.refresh(), on_error=self.error)
