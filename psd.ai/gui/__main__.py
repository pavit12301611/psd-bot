"""``python -m gui`` — launch the psd.ai desktop app."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # run as a script from inside psd.ai/
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
