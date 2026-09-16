"""Qt worker plumbing.

Every backend call is blocking I/O against the in-process event loop, so it
must never run on the GUI thread. This module provides a tiny promise-style
helper around ``QThreadPool``:

    run(fetch_things, on_done=list_view.fill, on_error=show_error)

Callbacks are invoked on the GUI thread via queued signal connections.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal

logger = logging.getLogger("psd.gui.workers")

_MAX_THREADS = 8


class WorkerSignals(QObject):
    """Signals emitted by :class:`Worker`."""

    done = Signal(object)
    failed = Signal(object)          # Exception
    finished = Signal()              # always, after done/failed


class Worker(QRunnable):
    """Runs ``fn(*args, **kwargs)`` on a pool thread."""

    def __init__(
        self,
        fn: Callable[..., Any],
        *args: Any,
        on_done: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
        on_finally: Optional[Callable[[], None]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self.setAutoDelete(True)
        if on_done is not None:
            self.signals.done.connect(on_done, Qt.QueuedConnection)
        if on_error is not None:
            self.signals.failed.connect(on_error, Qt.QueuedConnection)
        if on_finally is not None:
            self.signals.finished.connect(on_finally, Qt.QueuedConnection)

    def run(self) -> None:  # noqa: D102 - QRunnable API
        try:
            result = self.fn(*self.args, **self.kwargs)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the GUI thread
            logger.debug("worker failed: %s", exc)
            self.signals.failed.emit(exc)
        else:
            self.signals.done.emit(result)
        finally:
            self.signals.finished.emit()


class Runner(QObject):
    """Shared pool + convenience API."""

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool.globalInstance()
        self._pool.setMaxThreadCount(max(_MAX_THREADS, self._pool.maxThreadCount()))
        self._live = 0
        self._workers: set = set()

    def submit(self, worker: Worker) -> Worker:
        self._live += 1
        # Strong reference: the pool keeps the C++ QRunnable but not the Python
        # wrapper — without this the worker (and its signals) may be collected
        # mid-flight.
        self._workers.add(worker)
        worker.signals.finished.connect(
            lambda w=worker: self._release(w), Qt.QueuedConnection)
        self._pool.start(worker)
        return worker

    def _release(self, worker: "Worker") -> None:
        self._live = max(0, self._live - 1)
        self._workers.discard(worker)

    @property
    def active_count(self) -> int:
        return self._live

    def run(
        self,
        fn: Callable[..., Any],
        *args: Any,
        on_done: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
        on_finally: Optional[Callable[[], None]] = None,
        **kwargs: Any,
    ) -> Worker:
        return self.submit(
            Worker(fn, *args, on_done=on_done, on_error=on_error,
                   on_finally=on_finally, **kwargs)
        )

    def wait(self, timeout_ms: int = 10_000) -> bool:
        return self._pool.waitForDone(timeout_ms)


_RUNNER: Optional[Runner] = None


def get_runner() -> Runner:
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = Runner()
    return _RUNNER


def run(
    fn: Callable[..., Any],
    *args: Any,
    on_done: Optional[Callable[[Any], None]] = None,
    on_error: Optional[Callable[[BaseException], None]] = None,
    on_finally: Optional[Callable[[], None]] = None,
    **kwargs: Any,
) -> Worker:
    """Run ``fn`` in the background; callbacks fire on the GUI thread."""
    return get_runner().run(
        fn, *args, on_done=on_done, on_error=on_error, on_finally=on_finally, **kwargs
    )


def describe_error(exc: BaseException) -> str:
    """One-line, user-facing description of a worker failure."""
    from gui.backend import BackendError, BackendNotReady

    if isinstance(exc, BackendNotReady):
        return "The psd.ai engine is still starting up — try again in a moment."
    if isinstance(exc, BackendError):
        return str(exc)
    if isinstance(exc, TimeoutError):
        return "The request timed out."
    logger.debug("unexpected worker error: %s", traceback.format_exc())
    return f"{type(exc).__name__}: {exc}"
