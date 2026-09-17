"""psd.ai desktop sidecar entrypoint.

Started by the Tauri shell (desktop/src-tauri). This process is *private*:

* it binds to a random free loopback port (never a well-known one, never
  0.0.0.0), so nothing is meant to be typed into a browser;
* it prints a single machine-readable handshake line on stdout so the
  desktop shell can learn the port and start proxying IPC calls to it;
* it exits as soon as the parent closes its stdin pipe, so closing the app
  window never leaves an orphaned Python server behind.

Usage (what the Tauri shell runs):

    python desktop_server.py

Environment:
    PSD_AI_DESKTOP_PORT        force a specific loopback port (default: random)
    PSD_AI_DESKTOP_STANDALONE  =1 to keep running without a parent pipe
"""
from __future__ import annotations

import os
import socket
import sys
import threading


def _pick_port() -> int:
    forced = os.environ.get("PSD_AI_DESKTOP_PORT", "").strip()
    if forced.isdigit() and int(forced) > 0:
        return int(forced)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _watch_parent() -> None:
    """Exit when the parent (Tauri) closes our stdin - i.e. the app quit."""
    try:
        stream = sys.stdin.buffer if hasattr(sys.stdin, "buffer") else sys.stdin
        while True:
            chunk = stream.read(1)
            if not chunk:
                break
    except Exception:
        pass
    os._exit(0)


def main() -> None:
    port = _pick_port()

    # The app resolves its own loopback base (agent tools, MCP OAuth, task
    # webhooks) from APP_PORT, so it must be set *before* app.py is imported.
    os.environ["APP_PORT"] = str(port)
    os.environ.setdefault("APP_BIND", "127.0.0.1")
    # The desktop shell is the only client; keep auth on (the GUI owns the
    # setup / login screens) and never allow a loopback bypass.
    os.environ.setdefault("LOCALHOST_BYPASS", "false")

    # The Tauri shell holds our stdin pipe open for as long as the app lives.
    # Set PSD_AI_DESKTOP_STANDALONE=1 to run this file by hand (UI dev / tests)
    # where stdin is closed or /dev/null and would otherwise trigger an exit.
    if os.environ.get("PSD_AI_DESKTOP_STANDALONE", "").strip() not in ("1", "true", "yes"):
        if sys.stdin is not None and not sys.stdin.closed:
            threading.Thread(target=_watch_parent, daemon=True).start()

    # Startup watchdog: importing app.py wires up every subsystem (auth, RAG,
    # embeddings, model discovery...). If any of those blocks - a slow import,
    # a network probe that never times out - dump every thread's stack to
    # stderr so the desktop "Engine" log shows exactly WHERE it is stuck
    # instead of a silent spinner. Repeats every 45s until import completes.
    import faulthandler
    import time as _time

    _t0 = _time.time()
    sys.stderr.write("PSD_AI_PHASE importing app (this can take 10-60s on first launch)\n")
    sys.stderr.flush()
    faulthandler.dump_traceback_later(45, repeat=True, file=sys.stderr)
    try:
        import uvicorn
        from app import app  # noqa: WPS433 - import after env is prepared
    finally:
        faulthandler.cancel_dump_traceback_later()
    sys.stderr.write(f"PSD_AI_PHASE app imported in {_time.time() - _t0:.1f}s\n")
    sys.stderr.flush()

    # Handshake for the shell. Flush so the pipe reader sees it immediately.
    sys.stdout.write(f"PSD_AI_READY port={port}\n")
    sys.stdout.flush()

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
