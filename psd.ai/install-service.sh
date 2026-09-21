#!/usr/bin/env bash
# ==================================================================
#  Install psd.ai as a systemd USER service on Fedora.
#
#  The engine then starts on login, restarts if it crashes, and logs to
#  the journal:
#
#    ./install-service.sh                 install + start (port 7000)
#    ./install-service.sh --port 7900     install on a different port
#    ./install-service.sh --host 0.0.0.0  reachable from the LAN
#    ./install-service.sh --uninstall     stop, disable and remove
#    ./install-service.sh --status        show the unit state + log tail
#
#  No root is needed for any of it: a user unit lives in
#  ~/.config/systemd/user/ and runs inside your session, which is what
#  gives psd.ai the microphone (PipeWire), screenshots (XDG Portal) and
#  PC control (Wayland/X11) that a system service would have to be
#  granted by hand.
#
#  To keep it running after logout (headless box, SSH-only server):
#    sudo loginctl enable-linger "$USER"
# ==================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/psd_ai-ui.service"
UNIT_NAME="psd_ai-ui.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$UNIT_DIR/$UNIT_NAME"

PORT="${APP_PORT:-7000}"
HOST="127.0.0.1"
ACTION="install"

while [ $# -gt 0 ]; do
    case "$1" in
        --port)      PORT="${2:?--port needs a value}"; shift 2 ;;
        --port=*)    PORT="${1#*=}"; shift ;;
        --host)      HOST="${2:?--host needs a value}"; shift 2 ;;
        --host=*)    HOST="${1#*=}"; shift ;;
        --uninstall) ACTION="uninstall"; shift ;;
        --status)    ACTION="status"; shift ;;
        -h|--help)   sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)           echo "  [ERROR] unknown option: $1 (try --help)"; exit 2 ;;
    esac
done

case "$PORT" in
    ''|*[!0-9]*) echo "  [ERROR] --port must be a number, got: $PORT"; exit 2 ;;
esac

systemctl_user() {
    # XDG_RUNTIME_DIR is unset in some non-login shells (cron, sudo -i), and
    # without it systemctl cannot find the user bus.
    if [ -z "${XDG_RUNTIME_DIR:-}" ] && [ -d "/run/user/$(id -u)" ]; then
        XDG_RUNTIME_DIR="/run/user/$(id -u)"
        export XDG_RUNTIME_DIR
    fi
    systemctl --user "$@"
}

show_status() {
    if ! systemctl_user list-unit-files "$UNIT_NAME" 2>/dev/null | grep -q "$UNIT_NAME"; then
        echo "  psd_ai-ui is not installed for $USER."
        echo "  Install it with:  ./install-service.sh"
        return 0
    fi
    systemctl_user --no-pager --full status "$UNIT_NAME" || true
    echo
    echo "  ---- last 20 log lines ----"
    journalctl --user -u "$UNIT_NAME" -n 20 --no-pager || true
}

if [ "$ACTION" = "status" ]; then
    show_status
    exit 0
fi

if [ "$ACTION" = "uninstall" ]; then
    echo "  ==> Stopping and removing $UNIT_NAME..."
    systemctl_user stop "$UNIT_NAME" 2>/dev/null || true
    systemctl_user disable "$UNIT_NAME" 2>/dev/null || true
    rm -f "$UNIT_PATH"
    systemctl_user daemon-reload
    systemctl_user reset-failed 2>/dev/null || true
    echo "      Removed. Your data, models and settings are untouched."
    exit 0
fi

if [ ! -f "$TEMPLATE" ]; then
    echo "  [ERROR] $TEMPLATE not found."
    echo "          Run this script from inside the psd.ai folder."
    exit 1
fi

VENV_PY="$SCRIPT_DIR/venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
    echo "  [ERROR] No virtual environment at $SCRIPT_DIR/venv."
    echo "          Run ../run.sh once first - it creates the venv and"
    echo "          installs the dependencies this service needs."
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "  [ERROR] systemctl not found - this installer needs systemd."
    exit 1
fi

echo "  ==> Installing $UNIT_NAME for $USER"
echo "      engine dir:  $SCRIPT_DIR"
echo "      interpreter: $VENV_PY"
echo "      listening:   http://$HOST:$PORT"
if [ "$HOST" = "0.0.0.0" ]; then
    echo
    echo "  [WARN] --host 0.0.0.0 exposes psd.ai to your network. Only do that"
    echo "         behind a firewall you control, and set a strong admin password:"
    echo "         the app is an agent that can run shell commands as you."
fi

mkdir -p "$UNIT_DIR"
sed -e "s|__APP_DIR__|$SCRIPT_DIR|g" \
    -e "s|__PORT__|$PORT|g" \
    -e "s|__HOST__|$HOST|g" \
    "$TEMPLATE" > "$UNIT_PATH"
chmod 0600 "$UNIT_PATH"

systemctl_user daemon-reload
systemctl_user enable "$UNIT_NAME" >/dev/null 2>&1 || true
systemctl_user restart "$UNIT_NAME"

echo
echo "      Installed and started."
echo
echo "      journalctl --user -u $UNIT_NAME -f     follow the log"
echo "      systemctl --user stop $UNIT_NAME       stop it"
echo "      ./install-service.sh --status          health + recent log"
echo "      ./install-service.sh --uninstall       remove it"
echo
if ! loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes'; then
    echo "  [ .. ]  Linger is off, so the service stops when you log out."
    echo "          To keep psd.ai running on a headless box:"
    echo "            sudo loginctl enable-linger $USER"
fi
