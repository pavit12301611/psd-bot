#!/usr/bin/env python3
"""psd.ai — Desktop.

Launches the native GUI. The FastAPI workspace runs *in-process* (no port, no
browser, no localhost page) and the Qt front end talks to it directly.

    python psd_gui.py

Windows users normally double-click ``run.bat`` at the repo root instead.

Crashes are never silent: any startup failure is written to
``desktop_crash.log`` next to this file and shown in a native message box
(even when running console-less under pythonw.exe).
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The desktop app is always a "trusted client": the backend runs in the same
# process, so there is nothing to protect from a network point of view.
os.environ.setdefault("PSD_AI_TRUST_LOCAL", "1")
os.environ.setdefault("PSD_AI_DISABLE_BROWSER_LAUNCH", "1")

CRASH_LOG = ROOT / "desktop_crash.log"

_MISSING_QT = """
psd.ai desktop could not start.

  {exc}

The GUI needs PySide6 (Qt for Python). Install the desktop extras:

    python -m pip install -r requirements.txt

then run this file again. On Windows, run.bat does this for you.
"""


def _show_message_box(title: str, text: str) -> bool:
    """Native Windows message box — works without a console and without Qt."""
    if os.name != "nt":
        sys.stderr.write(f"\n{title}\n{text}\n")
        return False
    import ctypes

    try:
        ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
            None, text[:1800], title, 0x10)  # MB_OK | MB_ICONERROR
        return True
    except Exception:  # noqa: BLE001
        return False


def _report_crash(detail: str) -> None:
    """Persist + display a startup crash so pythonw builds are never silent."""
    try:
        CRASH_LOG.write_text(detail, encoding="utf-8")
    except OSError:
        pass
    tail = "\n".join(detail.strip().splitlines()[-12:])
    _show_message_box(
        "psd.ai could not start",
        "psd.ai desktop failed to start.\n\n"
        f"{tail}\n\nFull details: {CRASH_LOG}",
    )


def _excepthook(kind, value, tb) -> None:  # noqa: ANN001 - sys.excepthook API
    _report_crash("".join(traceback.format_exception(kind, value, tb)))


def main() -> int:
    # A crash log from an earlier run is stale once we get going again.
    try:
        if CRASH_LOG.exists():
            CRASH_LOG.unlink()
    except OSError:
        pass
    try:
        from gui.app import main as gui_main
    except ImportError as exc:
        sys.stderr.write(_MISSING_QT.format(exc=f"{type(exc).__name__}: {exc}"))
        _show_message_box(
            "psd.ai could not start",
            _MISSING_QT.format(exc=f"{type(exc).__name__}: {exc}").strip()[:1800],
        )
        try:
            input("Press Enter to exit… ")
        except (EOFError, KeyboardInterrupt, ValueError):
            pass
        return 1
    sys.excepthook = _excepthook
    try:
        return gui_main(sys.argv)
    except BaseException as exc:  # noqa: BLE001 - surfaced to the user
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        _report_crash(detail)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
