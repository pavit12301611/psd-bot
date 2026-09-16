"""In-process bridge to the psd.ai FastAPI application.

The desktop app has **no web server**. Instead of starting uvicorn on
``localhost:7000`` and pointing a browser at it, this module runs the very same
``app`` ASGI object inside a private asyncio event loop on a background thread
and drives it with :class:`httpx.AsyncClient` over a custom *streaming* ASGI
transport.

Why a custom transport? ``httpx.ASGITransport`` buffers the whole response
before returning it, which would turn the token-by-token chat SSE stream into
one blocking call. :class:`StreamingASGITransport` hands body chunks to the
caller as the application produces them, so streaming works exactly as it does
under uvicorn — but with zero sockets, zero ports and zero localhost involved.

Threading model
---------------
* one background thread owns the asyncio loop, the lifespan and the client;
* GUI/worker threads call the synchronous helpers here, which schedule
  coroutines onto that loop with ``run_coroutine_threadsafe``;
* SSE streams are pumped by the loop into a thread-safe :class:`queue.Queue`
  that a worker thread consumes, so the Qt event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import codecs
import concurrent.futures
import contextlib
import json
import logging
import os
import queue
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Tuple

FuturesTimeout = concurrent.futures.TimeoutError

import httpx

logger = logging.getLogger("psd.gui.backend")

# httpx needs a syntactically valid origin to build request URLs from. This one
# is reserved (RFC 2606 ``.internal``) and is *never* resolved or connected to:
# the transport calls the ASGI app directly. There is no localhost anywhere in
# the desktop app.
INTERNAL_ORIGIN = "http://psd-desktop.internal"

# The peer address recorded on the ASGI scope. The app is genuinely running on
# this machine, in this process, so loopback semantics (first-run allowances,
# rate-limit buckets) stay correct. No socket is bound to it.
INTERNAL_CLIENT = ("127.0.0.1", 0)

DEFAULT_TIMEOUT = httpx.Timeout(connect=15.0, read=600.0, write=120.0, pool=15.0)


class BackendError(RuntimeError):
    """Raised when the in-process backend cannot serve a request."""


class BackendNotReady(BackendError):
    """Raised when a call is attempted before :meth:`InProcessBackend.start`."""


# --------------------------------------------------------------------------- #
# Streaming ASGI transport
# --------------------------------------------------------------------------- #

_END = object()  # sentinel: no more body bytes


class _StreamingBody(httpx.AsyncByteStream):
    """Async byte stream fed chunk-by-chunk by the ASGI application."""

    def __init__(
        self,
        chunks: "asyncio.Queue[Any]",
        task: "asyncio.Task[None]",
        disconnect: asyncio.Event,
    ) -> None:
        self._chunks = chunks
        self._task = task
        self._disconnect = disconnect
        self._finished = False

    async def __aiter__(self) -> Iterator[bytes]:  # type: ignore[override]
        while True:
            item = await self._chunks.get()
            if item is _END:
                self._finished = True
                return
            if isinstance(item, BaseException):
                self._finished = True
                raise item
            yield item  # type: ignore[misc]

    async def aclose(self) -> None:
        """Stop the application task (used by the chat Stop button).

        Telling ``receive()`` to report ``http.disconnect`` is the ASGI-correct
        way to abort: Starlette cancels the response generator, which lets the
        agent loop wind down instead of being killed mid-write.
        """
        if self._finished and self._task.done():
            return
        self._disconnect.set()
        if not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._finished = True


class StreamingASGITransport(httpx.AsyncBaseTransport):
    """Drive an ASGI app in-process with incremental (streaming) responses."""

    def __init__(
        self,
        app: Any,
        client: Tuple[str, int] = INTERNAL_CLIENT,
        root_path: str = "",
        raise_app_exceptions: bool = True,
    ) -> None:
        self.app = app
        self.client = client
        self.root_path = root_path
        self.raise_app_exceptions = raise_app_exceptions

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = b"".join([chunk async for chunk in request.stream])

        scope: Dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": request.method,
            "headers": [(k.lower(), v) for (k, v) in request.headers.raw],
            "scheme": request.url.scheme,
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?")[0],
            "query_string": request.url.query,
            "server": (request.url.host, request.url.port or 80),
            "client": self.client,
            "root_path": self.root_path,
            "state": {},
        }

        loop = asyncio.get_running_loop()
        chunks: "asyncio.Queue[Any]" = asyncio.Queue()
        started = asyncio.Event()
        disconnect = asyncio.Event()
        state: Dict[str, Any] = {
            "status": None,
            "headers": None,
            "request_sent": False,
            "terminated": False,
        }

        async def receive() -> Dict[str, Any]:
            if state["request_sent"]:
                await disconnect.wait()
                return {"type": "http.disconnect"}
            state["request_sent"] = True
            return {"type": "http.request", "body": body, "more_body": False}

        def terminate() -> None:
            if not state["terminated"]:
                state["terminated"] = True
                chunks.put_nowait(_END)

        async def send(message: Dict[str, Any]) -> None:
            kind = message["type"]
            if kind == "http.response.start":
                state["status"] = message["status"]
                state["headers"] = message.get("headers", [])
                started.set()
            elif kind == "http.response.body":
                payload = message.get("body", b"")
                if payload and request.method != "HEAD":
                    await chunks.put(payload)
                if not message.get("more_body", False):
                    terminate()

        async def run_app() -> None:
            try:
                await self.app(scope, receive, send)
            except asyncio.CancelledError:
                terminate()
                raise
            except Exception as exc:  # noqa: BLE001 - surfaced to the caller
                if self.raise_app_exceptions:
                    if not started.is_set():
                        state["status"] = 500
                        state["headers"] = []
                        started.set()
                    chunks.put_nowait(exc)
                    terminate()
                else:
                    logger.exception("ASGI application error")
                    if not started.is_set():
                        state["status"] = 500
                        state["headers"] = [(b"content-type", b"text/plain; charset=utf-8")]
                        started.set()
                    chunks.put_nowait(b"Internal Server Error")
                    terminate()
            finally:
                terminate()

        task = loop.create_task(run_app(), name=f"asgi {request.method} {request.url.path}")

        waiter = loop.create_task(started.wait(), name="asgi-response-start")
        try:
            await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not waiter.done():
                waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await waiter

        if not started.is_set():
            if task.done() and not task.cancelled():
                exc = task.exception()
                if exc is not None:
                    raise exc
            raise BackendError(
                f"ASGI app finished without a response for {request.method} {request.url.path}"
            )

        stream = _StreamingBody(chunks, task, disconnect)
        return httpx.Response(
            state["status"],
            headers=state["headers"],
            stream=stream,
            extensions={"asgi_task": task},
        )


# --------------------------------------------------------------------------- #
# Lifespan
# --------------------------------------------------------------------------- #


class _Lifespan:
    """Async context manager running the app's startup/shutdown handlers."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._cm: Any = None

    async def __aenter__(self) -> "_Lifespan":
        factory = getattr(self.app.router, "lifespan_context", None)
        if factory is None:
            return self
        self._cm = factory(self.app)
        await self._cm.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._cm is None:
            return False
        return await self._cm.__aexit__(exc_type, exc, tb)


# --------------------------------------------------------------------------- #
# Server-sent events
# --------------------------------------------------------------------------- #


@dataclass
class SseEvent:
    """One parsed ``text/event-stream`` event."""

    event: str = "message"
    data: str = ""
    json: Any = None

    @property
    def kind(self) -> str:
        """``type`` field of the JSON payload, or ``""`` for plain deltas."""
        payload = self.json
        if isinstance(payload, dict):
            return str(payload.get("type") or "")
        return ""

    @property
    def is_delta(self) -> bool:
        return isinstance(self.json, dict) and "delta" in self.json

    @property
    def text(self) -> str:
        if self.is_delta:
            return str(self.json.get("delta") or "")
        return self.data


def parse_sse_chunk(buffer: str) -> Tuple[List[SseEvent], str]:
    """Split raw SSE text into complete events, returning the leftovers.

    Pure function so it can be unit-tested without Qt, asyncio or a backend.
    """
    events: List[SseEvent] = []
    # Normalise line endings, then split on the blank line that terminates an
    # event. ``\r\n\r\n``, ``\n\n`` and ``\r\r`` all appear in the wild.
    buffer = buffer.replace("\r\n", "\n").replace("\r", "\n")
    while "\n\n" in buffer:
        raw_event, buffer = buffer.split("\n\n", 1)
        event_name = "message"
        data_lines: List[str] = []
        for line in raw_event.split("\n"):
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip() or "message"
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" ") if line[5:6] == " " else line[5:])
        if not data_lines:
            continue
        data = "\n".join(data_lines)
        parsed: Any = None
        if data.strip() and data.strip() != "[DONE]":
            try:
                parsed = json.loads(data)
            except ValueError:
                parsed = None
        events.append(SseEvent(event=event_name, data=data, json=parsed))
    return events, buffer


class SseStream:
    """A cancellable SSE stream consumed from a worker thread.

    ``for event in stream:`` yields :class:`SseEvent` objects. ``stream.status``
    is the HTTP status; a non-2xx status raises :class:`BackendError` with the
    server's error detail.
    """

    def __init__(self, backend: "InProcessBackend", request_kwargs: Dict[str, Any]) -> None:
        self._backend = backend
        self._request_kwargs = request_kwargs
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=2048)
        self._cancel = threading.Event()
        self._pump: Optional["asyncio.Future[Any]"] = None
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""
        self.status: Optional[int] = None
        self.headers: Dict[str, str] = {}
        self.started_at = time.time()
        self._open()

    # -- lifecycle -------------------------------------------------------- #
    def _open(self) -> None:
        self._pump = self._backend.submit(self._pump_coro())

    async def _pump_coro(self) -> None:
        client = self._backend.client
        kwargs = dict(self._request_kwargs)
        try:
            async with client.stream(**kwargs) as response:
                self.status = response.status_code
                self.headers = dict(response.headers)
                self._queue.put(("status", response.status_code, dict(response.headers)))
                if response.status_code >= 400:
                    payload = await response.aread()
                    self._queue.put(("error", response.status_code, payload))
                    return
                async for chunk in response.aiter_bytes():
                    if self._cancel.is_set():
                        break
                    self._queue.put(("bytes", chunk, None))
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - forwarded to the consumer
            self._queue.put(("exception", 0, f"{type(exc).__name__}: {exc}"))
        finally:
            self._queue.put(("end", 0, None))

    def cancel(self) -> None:
        """Abort the stream (chat Stop button / view closed)."""
        self._cancel.set()

        async def _close() -> None:
            # Closing the response triggers the transport's aclose(), which
            # reports http.disconnect to the app and cancels its task.
            if self._pump is not None and not self._pump.done():
                self._pump.cancel()

        with contextlib.suppress(Exception):
            self._backend.submit_coro(_close(), timeout=10)

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # -- consumption ------------------------------------------------------ #
    def __iter__(self) -> Iterator[SseEvent]:
        return self._iter_events()

    def _iter_events(self) -> Iterator[SseEvent]:
        while True:
            try:
                item = self._queue.get(timeout=900)
            except queue.Empty:
                raise BackendError("Timed out waiting for the backend stream")
            kind, a, b = item
            if kind == "status":
                continue
            if kind == "error":
                detail = _decode_error(a, b)
                self._cancel.set()
                raise BackendError(detail)
            if kind == "exception":
                self._cancel.set()
                raise BackendError(str(b))
            if kind == "end":
                for event in self._flush():
                    yield event
                return
            if kind == "bytes":
                self._buffer += self._decoder.decode(a)
                events, self._buffer = parse_sse_chunk(self._buffer)
                for event in events:
                    if event.data.strip() == "[DONE]":
                        return
                    yield event

    def _flush(self) -> List[SseEvent]:
        if not self._buffer.strip():
            return []
        events, rest = parse_sse_chunk(self._buffer + "\n\n")
        self._buffer = rest
        return [e for e in events if e.data.strip() != "[DONE]"]

    def __enter__(self) -> "SseStream":
        return self

    def __exit__(self, *exc_info) -> None:
        self.cancel()


def _decode_error(status: int, payload: Any) -> str:
    """Turn an error response body into a readable one-line message."""
    if isinstance(payload, (bytes, bytearray)):
        try:
            text = payload.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            text = str(payload)
    else:
        text = str(payload or "")
    text = text.strip()
    if not text:
        return f"Backend error (HTTP {status})"
    try:
        data = json.loads(text)
    except ValueError:
        return text[:500] if len(text) > 500 else text
    if isinstance(data, dict):
        for key in ("detail", "message", "error", "error_message"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                inner = value.get("message") or value.get("detail")
                if isinstance(inner, str) and inner.strip():
                    return inner.strip()
        return f"HTTP {status}: {json.dumps(data)[:400]}"
    return text[:500]


# --------------------------------------------------------------------------- #
# The backend
# --------------------------------------------------------------------------- #


@dataclass
class BackendStatus:
    """Snapshot of backend health for the status bar / diagnostics view."""

    ready: bool = False
    starting: bool = False
    error: str = ""
    started_at: float = 0.0
    import_seconds: float = 0.0
    startup_seconds: float = 0.0
    requests: int = 0
    streams: int = 0
    inflight: int = 0
    user: str = ""
    auth_enabled: bool = False
    setup_required: bool = False
    version: str = ""


class InProcessBackend:
    """Owns the asyncio loop, the FastAPI app and the httpx client."""

    def __init__(
        self,
        origin: str = INTERNAL_ORIGIN,
        client_host: Tuple[str, int] = INTERNAL_CLIENT,
        app_import_path: str = "app",
        default_timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    ) -> None:
        self.origin = origin.rstrip("/")
        self.client_host = client_host
        self.app_import_path = app_import_path
        self.default_timeout = default_timeout

        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._app: Any = None
        self._ready = threading.Event()
        self._failed = threading.Event()
        self._stop_requested = threading.Event()
        self._error: str = ""
        self._error_trace: str = ""
        self._lock = threading.RLock()
        self._inflight = 0
        self._counts = {"requests": 0, "streams": 0}
        self._started_at = 0.0
        self._import_seconds = 0.0
        self._startup_seconds = 0.0
        self._log_records: List[str] = []

    # -- lifecycle -------------------------------------------------------- #
    def start(self, timeout: float = 600.0) -> None:
        """Import the app, run its lifespan startup, and open the client.

        Blocks the calling thread until the backend is ready (import + startup
        takes a few seconds on a warm machine, longer on first run). Raises
        :class:`BackendError` with the original traceback if it fails.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                if not self._ready.wait(0):
                    raise BackendError("Backend is already starting")
                return
            self._ready.clear()
            self._failed.clear()
            self._stop_requested.clear()
            self._error = ""
            self._error_trace = ""
            self._started_at = time.time()
            self._thread = threading.Thread(
                target=self._thread_main, name="psd-backend-loop", daemon=True
            )
            self._thread.start()

        if not self._ready.wait(timeout) and not self._failed.wait(0.1):
            raise BackendError(
                f"Backend did not become ready within {timeout:.0f}s"
            )
        if self._failed.is_set():
            raise BackendError(self._error or "Backend failed to start", )

    def _thread_main(self) -> None:
        try:
            if sys.platform == "win32":
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            loop.run_until_complete(self._serve())
        except BaseException as exc:  # noqa: BLE001 - reported to the GUI
            self._error = f"{type(exc).__name__}: {exc}"
            self._error_trace = traceback.format_exc()
            self._failed.set()
            logger.error("Backend loop crashed: %s", self._error_trace)
        finally:
            with contextlib.suppress(Exception):
                if self._loop is not None:
                    self._loop.close()
            self._loop = None

    async def _serve(self) -> None:
        t0 = time.time()
        app = await asyncio.to_thread(self._import_app)
        self._app = app
        self._import_seconds = time.time() - t0
        logger.info(
            "Imported %s in %.1fs (in-process, no port bound)",
            self.app_import_path,
            self._import_seconds,
        )

        t1 = time.time()
        transport = StreamingASGITransport(app, client=self.client_host)
        async with _Lifespan(app):
            self._startup_seconds = time.time() - t1
            logger.info("Backend startup finished in %.1fs", self._startup_seconds)
            async with httpx.AsyncClient(
                transport=transport,
                base_url=self.origin,
                timeout=self.default_timeout,
                follow_redirects=False,
            ) as client:
                self._client = client
                self._ready.set()
                try:
                    while not self._stop_requested.is_set():
                        await asyncio.sleep(0.1)
                finally:
                    self._client = None
        logger.info("Backend lifespan closed")

    def _import_app(self) -> Any:
        """Import ``app.py`` (or ``PSD_GUI_APP_MODULE``) and return the app."""
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if base_dir not in sys.path:
            sys.path.insert(0, base_dir)
        module_name = os.getenv("PSD_GUI_APP_MODULE", self.app_import_path)
        import importlib

        module = importlib.import_module(module_name)
        app = getattr(module, "app", None)
        if app is None:
            raise BackendError(f"{module_name} has no `app` object")
        return app

    def stop(self, timeout: float = 30.0) -> None:
        """Ask the lifespan to shut down and join the loop thread."""
        self._stop_requested.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None
        self._ready.clear()

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set() and self._client is not None

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise BackendNotReady("Backend loop is not running")
        return self._loop

    @property
    def client(self) -> httpx.AsyncClient:
        client = self._client
        if client is None:
            raise BackendNotReady("Backend is not ready")
        return client

    @property
    def app(self) -> Any:
        return self._app

    @property
    def error_trace(self) -> str:
        return self._error_trace

    def status(self) -> BackendStatus:
        """Snapshot used by the status bar and the Diagnostics view."""
        return BackendStatus(
            ready=self.is_ready,
            starting=bool(self._thread and self._thread.is_alive() and not self.is_ready),
            error=self._error,
            started_at=self._started_at,
            import_seconds=self._import_seconds,
            startup_seconds=self._startup_seconds,
            requests=self._counts["requests"],
            streams=self._counts["streams"],
            inflight=self._inflight,
            user=getattr(self, "_username", "") or "",
            auth_enabled=bool(getattr(self, "_auth_enabled", False)),
            setup_required=bool(getattr(self, "_setup_required", False)),
        )

    # -- scheduling helpers ------------------------------------------------ #
    def submit_coro(self, coro: Any, timeout: Optional[float] = None) -> Any:
        """Run ``coro`` on the backend loop and return its result (blocking)."""
        future = self.submit(coro)
        try:
            return future.result(timeout)
        except FuturesTimeout as exc:  # pragma: no cover - defensive
            future.cancel()
            raise BackendError(f"Backend call timed out after {timeout}s") from exc

    def submit(self, coro: Any) -> "concurrent.futures.Future[Any]":
        """Schedule ``coro`` on the backend loop; return its concurrent Future."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # -- request helpers --------------------------------------------------- #
    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        data: Any = None,
        files: Any = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        expect: str = "json",
    ) -> Any:
        """Perform one request and return decoded content.

        ``expect`` is ``json`` (default), ``text`` or ``bytes``. Non-2xx
        responses raise :class:`BackendError` with the server's message.
        """
        if not self.is_ready:
            raise BackendNotReady("Backend is still starting up")

        async def _call() -> Any:
            client = self.client
            response = await client.request(
                method,
                path,
                params=params,
                json=json_body,
                data=data,
                files=files,
                headers=headers,
                timeout=timeout if timeout is not None else self.default_timeout,
            )
            content = await response.aread()
            return response.status_code, dict(response.headers), content

        with self._inflight_guard():
            status, resp_headers, content = self.submit_coro(
                _call(), timeout=(timeout or 600.0) + 30.0
            )
        if status >= 400:
            raise BackendError(_decode_error(status, content))
        if expect == "bytes":
            return content
        text = content.decode("utf-8", "replace") if content else ""
        if expect == "text":
            return text
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except ValueError:
            return {"raw": text}

    @contextlib.contextmanager
    def _inflight_guard(self) -> Iterator[None]:
        self._inflight += 1
        self._counts["requests"] += 1
        try:
            yield
        finally:
            self._inflight -= 1

    def stream(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        data: Any = None,
        files: Any = None,
        json_body: Any = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> SseStream:
        """Open an SSE stream (``/api/chat_stream``, ``/api/research/stream``)."""
        if not self.is_ready:
            raise BackendNotReady("Backend is still starting up")
        self._counts["streams"] += 1
        kwargs: Dict[str, Any] = {
            "method": method,
            "url": path,
            "params": params,
            "headers": headers,
            "timeout": timeout if timeout is not None else self.default_timeout,
        }
        if json_body is not None:
            kwargs["json"] = json_body
        if data is not None:
            kwargs["data"] = data
        if files is not None:
            kwargs["files"] = files
        return SseStream(self, kwargs)

    # -- conveniences ------------------------------------------------------ #
    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Any:
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs)


# --------------------------------------------------------------------------- #
# Process-wide singleton
# --------------------------------------------------------------------------- #

_BACKEND: Optional[InProcessBackend] = None
_BACKEND_LOCK = threading.Lock()


def get_backend() -> InProcessBackend:
    """Return (creating once) the shared in-process backend."""
    global _BACKEND
    with _BACKEND_LOCK:
        if _BACKEND is None:
            _BACKEND = InProcessBackend()
        return _BACKEND


def timezone_headers() -> Dict[str, str]:
    """Headers the backend uses to resolve user-local times.

    Mirrors what the browser UI sent (``X-Tz-Offset`` / ``X-Tz-Name``) so the
    agent reads "tonight at 9pm" in the user's timezone rather than UTC.
    """
    now = time.localtime()
    offset_seconds = -(time.altzone if now.tm_isdst else time.timezone)
    try:
        tz_name = datetime.now().astimezone().tzname() or ""
    except Exception:  # noqa: BLE001 - very old/odd platforms
        tz_name = str(time.tzname[bool(now.tm_isdst)] or "")
    return {"X-Tz-Offset": str(int(offset_seconds // 60)), "X-Tz-Name": tz_name}
