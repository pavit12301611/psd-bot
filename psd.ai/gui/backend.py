"""gui/backend.py — run the psd.ai FastAPI backend for the desktop GUI.

Two modes, mirroring the battle-tested terminal interface:

* **embedded** (default): import :mod:`app` and drive it **in-process** through
  its ASGI lifespan.  Nothing is bound to a port and no browser opens.
  First-run admin setup / authentication go through the same ``AuthManager``
  the web flow uses, so the database, ``auth.json`` and every data directory
  live in the exact same DATA_DIR the existing app uses — chats, history,
  memory and settings are shared with anything created previously.

* **attached**: talk to a server already running on ``host:port`` over
  loopback HTTP.  **serve** mode additionally boots :mod:`app` with uvicorn
  for us, acting as a drop-in for `python -m uvicorn app:app`.

Threading model
---------------

The backend's async work always runs on its own dedicated asyncio loop
(:func:`loop`), never on Qt's event loop.

* :func:`schedule` hands an awaitable to the backend loop and returns the
  :class:`concurrent.futures.Future` that completes with its result
  (``asyncio.run_coroutine_threadsafe``).  The GUI waits on that future while
  pumping Qt events, so the desktop never blocks without reason.
* :func:`consume` runs a "fire and forget" async generator (streaming chat)
  and funnels every chunk into an ``emit(chunk)`` callback; the GUI passes a
  Qt signal's ``.emit`` so cross-thread delivery is queued automatically.
* Blocking calls (bcrypt, file I/O) run through the backend loop's default
  executor, sized generously at startup.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any, Awaitable, Callable, Optional

import uvicorn
from uvicorn.lifespan.on import LifespanOn

# Uniform ceiling for backend waiters in the GUI (short, bounded calls only —
# long streaming work never uses this path).
REQUEST_HARD_TIMEOUT = 30.0


class BackendError(Exception):
    """Raised for backend lifecycle errors (import failures, server errors)."""


_LOOP: asyncio.AbstractEventLoop = asyncio.new_event_loop()
_STATE: dict = {
    "thread": None,
    "app": None,
    "auth": None,
    "fastapi": None,
    "server": None,
    "started": False,
    "booting": False,
    "boot_error": None,
    "stop_event": None,
}


# --------------------------------------------------------------------------- #
# Loop lifecycle
# --------------------------------------------------------------------------- #

def loop() -> asyncio.AbstractEventLoop:
    """The backend's dedicated event loop (never the Qt loop)."""
    return _LOOP


def _set_default_executor() -> None:
    import concurrent.futures

    _LOOP.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(
            max_workers=max(12, (os.cpu_count() or 4) * 4),
            thread_name_prefix="psd-gui-io",
        )
    )


def _run_loop_forever() -> None:
    asyncio.set_event_loop(_LOOP)
    _set_default_executor()
    _LOOP.run_forever()


def start() -> None:
    """Start the backend event loop on a background thread (idempotent)."""
    if _STATE["thread"] is not None and _STATE["thread"].is_alive():
        return
    if _LOOP.is_running():
        return
    t = threading.Thread(target=_run_loop_forever, name="psd-gui-backend", daemon=True)
    t.start()
    _STATE["thread"] = t

    ready = threading.Event()
    _LOOP.call_soon_threadsafe(ready.set)
    if not ready.wait(timeout=10):
        raise BackendError("backend event loop failed to start")


def schedule(coro: Awaitable, fire_and_forget: bool = False):
    """Schedule ``coro`` on the backend loop.

    Returns the concurrent future completing with the coroutine's result, or
    ``None`` when ``fire_and_forget`` is set (errors are then logged).
    """
    if not _LOOP.is_running():
        raise BackendError("backend loop is not running — call backend.start() first")
    fut = asyncio.run_coroutine_threadsafe(coro, _LOOP)
    if fire_and_forget:
        def _observe(f: Any) -> None:
            try:
                f.result()
            except BaseException as exc:  # noqa: BLE001
                import logging

                logging.getLogger("psd.gui.backend").warning(
                    "background backend task failed: %s", exc
                )

        fut.add_done_callback(_observe)
        return None
    return fut


# --------------------------------------------------------------------------- #
# Embedded app
# --------------------------------------------------------------------------- #

async def _boot_embedded() -> None:
    try:
        import app as psd_app  # noqa: PLC0415 — heavy import, embedded path only

        _STATE["app"] = psd_app
        _STATE["auth"] = getattr(psd_app, "auth_manager")
        _STATE["fastapi"] = getattr(psd_app, "app")
        _STATE["stop_event"] = asyncio.Event()

        config = uvicorn.Config(_STATE["fastapi"], lifespan="on")
        lifespan = LifespanOn(config)
        try:
            await lifespan.startup()
            _STATE["started"] = True
            try:
                await _STATE["stop_event"].wait()
            finally:
                _STATE["started"] = False
                await lifespan.shutdown()
        finally:
            _STATE["started"] = False
    except BaseException as exc:  # noqa: BLE001
        _STATE["boot_error"] = exc
        raise
    finally:
        # Release the app so a later re-embed in the same process starts fresh.
        _STATE["stop_event"] = None
        _STATE["fastapi"] = None
        _STATE["auth"] = None
        _STATE["app"] = None
        _STATE["booting"] = False


async def _wait_embedded_ready(boot_fut: asyncio.Future) -> bool:
    awaitable = asyncio.wrap_future(boot_fut)
    while True:
        if _STATE["started"]:
            return True
        if awaitable.done():
            try:
                awaitable.result()
            except BaseException as exc:  # noqa: BLE001
                raise BackendError(f"backend failed to start: {exc}") from exc
            raise BackendError("backend failed to start")
        await asyncio.sleep(0.05)


def run_embedded():
    """Boot the embedded app; returns a future that resolves when it is ready.

    Idempotent: if the app is already running, the returned future resolves
    immediately (covers re-entrancy, e.g. a test harness that pre-boots).
    """
    if _STATE["started"] or _STATE["fastapi"] is not None or _STATE["booting"]:
        async def _already_ready() -> bool:
            return True

        return schedule(_already_ready())
    _STATE["booting"] = True
    _STATE["boot_error"] = None
    _STATE["started"] = False
    boot_fut = schedule(_boot_embedded())
    return schedule(_wait_embedded_ready(boot_fut))


def stop_embedded() -> None:
    """Signal the embedded app to run its lifespan shutdown."""
    stop_event = _STATE.get("stop_event")
    if stop_event is None:
        return
    _LOOP.call_soon_threadsafe(stop_event.set)


# --------------------------------------------------------------------------- #
# Attached uvicorn (serve mode)
# --------------------------------------------------------------------------- #

async def _serve_attached(host: str, port: int) -> None:
    try:
        import app as psd_app  # noqa: PLC0415

        _STATE["app"] = psd_app
        _STATE["auth"] = getattr(psd_app, "auth_manager")
        _STATE["fastapi"] = getattr(psd_app, "app")

        config = uvicorn.Config(
            psd_app.app, host=host, port=port, log_level="info", lifespan="on"
        )
        server = uvicorn.Server(config)
        _STATE["server"] = server
        await server.serve()
    finally:
        _STATE["server"] = None
        _STATE["started"] = False


async def _wait_attached_ready(serve_fut: asyncio.Future) -> bool:
    awaitable = asyncio.wrap_future(serve_fut)
    while True:
        server = _STATE.get("server")
        if server is not None and getattr(server, "started", False):
            _STATE["started"] = True
            return True
        if awaitable.done():
            try:
                awaitable.result()
            except BaseException as exc:  # noqa: BLE001
                raise BackendError(f"server failed to start: {exc}") from exc
            raise BackendError("server failed to start")
        await asyncio.sleep(0.05)


def run_attached(host: str, port: int):
    """Start the server on the backend loop; returns a readiness future."""
    if _STATE["server"] is not None:
        raise BackendError("server already running")
    _STATE["boot_error"] = None
    _STATE["started"] = False
    serve_fut = schedule(_serve_attached(host, port))
    return schedule(_wait_attached_ready(serve_fut))


def stop_attached() -> None:
    server = _STATE.get("server")
    if server is None:
        return
    _LOOP.call_soon_threadsafe(setattr, server, "should_exit", True)


# --------------------------------------------------------------------------- #
# Introspection
# --------------------------------------------------------------------------- #

def fastapi_app() -> Any:
    return _STATE.get("fastapi")


def auth_manager() -> Any:
    return _STATE.get("auth")


# --------------------------------------------------------------------------- #
# Embedded auth (direct AuthManager access — same trust path as the TUI)
# --------------------------------------------------------------------------- #

async def _to_thread(fn: Callable[[], Any]) -> Any:
    return await asyncio.get_running_loop().run_in_executor(None, fn)


async def embedded_configured() -> bool:
    auth = auth_manager()
    if auth is None:
        return False
    return bool(await _to_thread(lambda: auth.is_configured))


async def embedded_setup(username: str, password: str) -> bool:
    auth = auth_manager()
    if auth is None:
        raise BackendError("embedded app is not running")
    ok = await _to_thread(lambda: auth.setup(username, password))
    if not ok:
        raise BackendError(f"could not create admin account '{username}'")
    return True


async def embedded_login(username: str, password: str, totp: str = "") -> str:
    """Verify credentials against AuthManager and mint a trusted session token."""
    auth = auth_manager()
    if auth is None:
        raise BackendError("embedded app is not running")

    def _do() -> Optional[str]:
        if not auth.verify_password(username, password):
            raise BackendError("Invalid credentials")
        if auth.totp_enabled(username):
            if not totp:
                raise BackendError("2FA_REQUIRED")
            if not auth.totp_verify(username, totp):
                raise BackendError("Invalid 2FA code")
        token = auth.create_session_trusted(username)
        if not token:
            raise BackendError("Could not create a session")
        return token

    return str(await _to_thread(_do))


# --------------------------------------------------------------------------- #
# Streaming pump
# --------------------------------------------------------------------------- #

def consume(generator: Any, emit: Callable[[dict], None]) -> None:
    """Consume an async generator on the backend loop, emitting every chunk.

    A terminal ``{"_fin": True}`` chunk is always emitted when the generator
    ends (success or error), so the UI knows the stream is over.
    """

    async def _consume() -> None:
        try:
            async for chunk in generator:
                emit(chunk)
        except BaseException as exc:  # noqa: BLE001
            emit({"_error": f"{type(exc).__name__}: {exc}"})
        finally:
            emit({"_fin": True})

    schedule(_consume(), fire_and_forget=True)
