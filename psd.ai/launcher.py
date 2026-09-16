# launcher.py
"""Entrypoint for the standalone Windows portable launcher.

The desktop is a native Qt window now: this module boots the psd.ai desktop
app (``gui.app``). The whole workspace engine runs **in-process** inside the
same executable — there is no port, no localhost page and no browser launch.

Keeps the frozen-bundle niceties:
- ``multiprocessing.freeze_support()`` for PyInstaller spawn children.
- ``NullWriter`` so windowed (console-less) builds never crash on prints.
"""
import os
import sys

# PyInstaller multiprocessing children re-enter this executable with a private
# bootstrap argument. Consume it before any UI or application imports so a
# spawn-based worker does not relaunch the desktop application.
if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()


class NullWriter:
    """Suppress standard-stream crashes (isatty etc.) in windowed GUI mode."""

    def write(self, text):
        pass

    def flush(self):
        pass

    def isatty(self):
        return False


if sys.stdout is None:
    sys.stdout = NullWriter()
if sys.stderr is None:
    sys.stderr = NullWriter()

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# The desktop is its own trusted client and must never spawn a browser.
os.environ.setdefault("PSD_AI_TRUST_LOCAL", "1")
os.environ.setdefault("PSD_AI_DISABLE_BROWSER_LAUNCH", "1")


def main() -> int:
    from gui.app import main as gui_main

    return gui_main(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
