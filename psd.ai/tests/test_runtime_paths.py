"""Path resolution: source checkout and packaged installs share one rule.

There is no frozen/PyInstaller build any more — the Windows portable launcher
that needed `sys._MEIPASS` is gone, and Fedora packaging is a plain directory
layout (RPM venv in /usr/lib/psd.ai, or the repo venv `run.sh` creates). So
resolution is a walk out from this package, and persistent state can be moved
with PSD_AI_DATA_DIR.
"""

import os

from src.runtime_paths import get_app_root, get_default_data_dir


def test_app_root_is_the_directory_that_holds_src():
    root = get_app_root()
    assert os.path.isdir(root)
    assert os.path.isdir(os.path.join(root, "src"))
    assert os.path.isfile(os.path.join(root, "src", "runtime_paths.py"))


def test_app_root_does_not_depend_on_the_process_cwd():
    """A systemd unit or a cron job starts elsewhere; paths must not move."""
    here = os.getcwd()
    try:
        os.chdir(os.path.expanduser("~"))
        assert get_app_root() == here or os.path.samefile(get_app_root(), here)
    finally:
        os.chdir(here)


def test_default_data_dir_sits_under_the_app_root():
    assert get_default_data_dir() == os.path.join(get_app_root(), "data")
