#!/usr/bin/env python3
"""psd.ai — Desktop.

Launches the native GUI. The FastAPI workspace runs *in-process* (no port, no
browser, no localhost page) and the Qt front end talks to it directly.

    python psd_gui.py

Windows users normally double-click ``run.bat`` at the repo root instead.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The desktop app is always a "trusted client": the backend runs in the same
# process, so there is nothing to protect from a network point of view.
os.environ.setdefault("PSD_AI_TRUST_LOCAL", "1")
os.environ.setdefault("PSD_AI_DISABLE_BROWSER_LAUNCH", "1")


def main() -> int:
    try:
        from gui.app import main as gui_main
    except ImportError as exc:
        sys.stderr.write(
            "\npsd.ai desktop could not start.\n\n"
            f"  {type(exc).__name__}: {exc}\n\n"
            "The GUI needs PySide6 (Qt for Python). Install the desktop extras:\n\n"
            "    python -m pip install -r requirements.txt\n\n"
            "then run this file again. On Windows, run.bat does this for you.\n")
        try:
            input("Press Enter to exit… ")
        except (EOFError, KeyboardInterrupt):
            pass
        return 1
    return gui_main(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
