"""Helpers for resolving runtime paths.

The project ships two ways to run on Fedora: a source checkout (the venv
`run.sh` creates) and the RPM from `packaging/psd-ai.spec`, which installs the
tree under `/usr/lib/psd.ai` with its venv beside it. Both are plain directory
layouts, so path resolution is a simple walk from this file outward, and a
deployment that wants its database and uploads elsewhere sets
`PSD_AI_DATA_DIR` (read once, in `src/constants.py`).
"""

import os


def get_app_root() -> str:
    """Return the app root directory: the parent of the `src/` package."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_default_data_dir() -> str:
    """Return the default path to the data directory.

    Persistent state (SQLite, uploads, settings, auth) lives in a `data`
    subdirectory under the app root. RPM and systemd deployments override it
    with `PSD_AI_DATA_DIR`, so a read-only install tree can keep its state in
    `/var/lib/psd.ai` or under the user's home.
    """
    return os.path.join(get_app_root(), "data")
