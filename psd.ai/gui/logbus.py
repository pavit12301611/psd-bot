"""Live backend log capture for the desktop app.

The psd.ai backend logs through the standard ``logging`` module. In a desktop
app there is no terminal to watch, so this module taps the root logger and
republishes every record on a Qt signal — the Logs view subscribes to it, and
a bounded ring buffer keeps history for views opened later.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional

from PySide6.QtCore import QObject, Signal

MAX_HISTORY = 2000

# Only our own namespaces + the backend's; third-party libs stay out of the
# GUI log unless they warn or worse.
TRACKED_PREFIXES = (
    "psd", "app", "routes", "src", "services", "core", "gui",
    "uvicorn", "httpx", "mcp", "apscheduler",
)


@dataclass(frozen=True)
class LogRecord:
    level: int
    level_name: str
    logger_name: str
    message: str
    timestamp: float
    thread: str = ""

    @property
    def time_text(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.timestamp))


class LogBus(QObject):
    """Publishes :class:`LogRecord` objects on the Qt event loop."""

    record = Signal(object)          # LogRecord
    cleared = Signal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._history: Deque[LogRecord] = deque(maxlen=MAX_HISTORY)
        self._lock = threading.Lock()

    def publish(self, item: LogRecord) -> None:
        with self._lock:
            self._history.append(item)
        self.record.emit(item)

    def history(self, limit: Optional[int] = None) -> List[LogRecord]:
        with self._lock:
            items = list(self._history)
        return items[-limit:] if limit else items

    def clear(self) -> None:
        with self._lock:
            self._history.clear()
        self.cleared.emit()


class _Handler(logging.Handler):
    """Logging handler that forwards records into a :class:`LogBus`."""

    def __init__(self, bus: LogBus) -> None:
        super().__init__(level=logging.DEBUG)
        self.bus = bus
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            name = record.name or ""
            if record.levelno < logging.WARNING and not name.startswith(TRACKED_PREFIXES):
                return
            message = record.getMessage()
            if record.exc_info:
                import traceback

                message = f"{message}\n{traceback.format_exception(*record.exc_info)}"
            item = LogRecord(
                level=record.levelno,
                level_name=record.levelname,
                logger_name=name,
                message=message,
                timestamp=record.created or time.time(),
                thread=record.threadName or "",
            )
            self.bus.publish(item)
        except Exception:  # noqa: BLE001 - logging must never crash the app
            pass


_BUS: Optional[LogBus] = None
_HANDLER: Optional[_Handler] = None
_BUS_LOCK = threading.Lock()


def get_bus() -> LogBus:
    """Return the process-wide log bus, installing the handler on first use."""
    global _BUS, _HANDLER
    with _BUS_LOCK:
        if _BUS is None:
            _BUS = LogBus()
            _HANDLER = _Handler(_BUS)
            root = logging.getLogger()
            if root.level > logging.INFO:
                root.setLevel(logging.INFO)
            root.addHandler(_HANDLER)
        return _BUS


def uninstall() -> None:
    """Detach the handler (used on shutdown / in tests)."""
    global _BUS, _HANDLER
    with _BUS_LOCK:
        if _HANDLER is not None:
            logging.getLogger().removeHandler(_HANDLER)
        _HANDLER = None
        _BUS = None
