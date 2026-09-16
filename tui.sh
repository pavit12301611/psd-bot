#!/usr/bin/env bash
# psd.ai - terminal interface launcher (replaces the localhost website).
#
#   ./tui.sh              run the psd.ai terminal interface (embedded: no
#                         web server, no browser, no port)
#   ./tui.sh --attached   attach to a psd.ai server already running on
#                         localhost:7000 (override with HOST/PORT env vars)
#
# No server is started in the default mode: psd.ai runs in-process and is
# drawn in this terminal. First run sets up data folders, the database and
# the admin account, exactly like `python setup.py` + the web first-run flow.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HERE/psd.ai"
VENVPY="$APP_DIR/venv/bin/python"

cd "$APP_DIR"

echo
echo "  ============================================================"
echo "    psd.ai - terminal interface (no browser, no localhost)"
echo "  ============================================================"
echo

# --- 1. Find Python 3.11+ ------------------------------------------------
PYCMD=""
for cand in python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      PYCMD="$cand"
      break
    fi
  fi
done
if [ -z "$PYCMD" ]; then
  echo "  [ERROR] Python 3.11+ not found. Install it and re-run tui.sh."
  exit 1
fi
echo "  Using Python $($PYCMD -c 'import platform; print(platform.python_version())')"

# --- 2. Create the virtual environment (first run only) ------------------
if [ ! -x "$VENVPY" ]; then
  echo "  ==> Creating virtual environment (venv)..."
  "$PYCMD" -m venv venv
fi

# --- 3. Install dependencies (first run only) ----------------------------
if [ ! -f "$APP_DIR/venv/.tui_deps_ok" ]; then
  echo "  ==> Installing dependencies... first run can take a few minutes."
  "$VENVPY" -m pip install --upgrade pip --quiet
  "$VENVPY" -m pip install -r requirements.txt "textual" "httpx<0.28"
  touch "$APP_DIR/venv/.tui_deps_ok"
else
  echo "  ==> Dependencies already installed - skipping."
fi

# --- 4. Launch ------------------------------------------------------------
echo
echo "  ==> Starting the psd.ai terminal interface..."
echo "      Press q inside the app (or Ctrl+C) to stop."
echo

if [ "${1:-}" = "--attached" ]; then
  exec "$VENVPY" psd_tui.py --host "${PSD_TUI_HOST:-127.0.0.1}" --port "${PSD_TUI_PORT:-7000}"
else
  exec "$VENVPY" psd_tui.py --embedded
fi
