#!/usr/bin/env bash
# ==================================================================
#  psd.ai - one-command setup + launch for Fedora Linux (desktop app)
#
#  Run it and it will:
#    1. install the system packages this project builds and runs
#       against (dnf - first run only, skipped if already present)
#    2. find Python 3.11+
#    3. create a virtual environment            (first run only)
#    4. install all dependencies                (first run only)
#    5. install the Jarvis extras               (first run only -
#       voice + PC control, see requirements-jarvis.txt)
#    6. run setup - creates data folders        (the app asks you to
#       create your admin account in its own window on first launch)
#    7. download + run a hardware-fit group of 3-5 local models
#       (first run only - a few GB per model, in the background,
#       logging to logs/local-model.log)
#    8. open the psd.ai DESKTOP APP             (no browser, no
#       localhost URL)
#
#  The desktop app is a native window (Tauri + React). It starts the
#  Python engine privately inside itself and talks to it over IPC.
#  Nothing is served on a public port and nothing opens in a browser.
#
#  Safe to re-run - steps already done are skipped, so later launches
#  start straight away. Keep this next to the psd.ai folder. Closing
#  the app window stops everything, including the model group.
#
#  ------------------------------------------------------------------
#  Options (./run.sh --help)
#
#    ./run.sh                normal launch
#    ./run.sh --help         show this help
#    ./run.sh --doctor       check this machine and report what is missing
#    ./run.sh --repair       rebuild the venv and reinstall everything
#    ./run.sh --update       git pull + refresh dependencies
#    ./run.sh --rebuild      force-rebuild the desktop app
#    ./run.sh --no-voice     skip the Jarvis extras (no local Whisper,
#                            no PC-control packages)
#    ./run.sh --no-models    skip the local model group this run
#    ./run.sh --no-app       set everything up, then stop (no window)
#    ./run.sh --no-gui       no browser install dashboard (terminal only)
#    ./run.sh --no-system-deps  do not touch dnf, use what is installed
#    ./run.sh --skip-numpy-check skip the numpy import check
#
#  Environment overrides
#
#    PSD_NO_LOCAL_STT=1      install the PC-control extras but not the
#                            local Whisper package (use the browser for
#                            speech-to-text instead)
#    PSD_NO_LOCAL_MODEL=1    never download local models
#    PSD_NO_SYSTEM_DEPS=1    same as --no-system-deps
#    PSD_AI_COMPUTER_CONTROL=0/1  switch desktop control off/on
#    PSD_MODEL_PROFILE=power stronger model + longer context (default
#                            "balanced"; "max" spends the whole machine
#                            on one model instead of a group)
#    PSD_MODEL_TIER=coding   make the primary local model a coding one
#    LLAMA_PORT=9090         first model port for the group
#    MODEL_WAIT_SECONDS=2400 how long to wait for a first-run download
#    PSD_AI_RUNTIME_DIR=     where llama.cpp + weights are cached
#                            (default ~/.local/share/psd.ai/runtime)
#    PSD_AI_SKIP_NUMPY_CHECK=1  skip the numpy import check
#    NUMPY_IMPORT_BUDGET=60  seconds to allow for "import numpy"
#    PSD_NO_GUI=1            same as --no-gui
#    PSD_INSTALL_GUI_PORT=   dashboard port (default 7123, loopback only)
#
#  ------------------------------------------------------------------
#  Fedora notes
#
#   * Packages come from the Fedora repos with dnf. ffmpeg is not in
#     them - it lives in RPM Fusion, and step 1 tells you the one
#     command that adds it if you want video/audio transcription.
#   * Fedora Workstation runs Wayland. Simulating the keyboard and
#     mouse needs uinput access, so enable it once with:
#         systemctl --user enable --now ydotoold
#     (or the system-wide ydotool.service). Screenshots go through the
#     XDG Desktop Portal, which prompts the first time - that is the
#     compositor doing its job, not a bug.
#   * SELinux stays Enforcing. Everything here runs as your user out
#     of your home directory, which is exactly what the policy allows;
#     nothing needs a boolean and nothing needs a relabel. If you move
#     the project under /opt, restore contexts with:
#         sudo dnf install policycoreutils-python-utils
#         sudo semanage fcontext -a -t bin_t /opt/psd-ai/psd.ai/venv/bin
#   * Fedora Atomic (Silverblue/Kinoite) has no writable /usr: run this
#     inside a toolbox/distrobox container, or install the packages
#     with `rpm-ostree install` and re-run with --no-system-deps.
# ==================================================================

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/"
APP_DIR="${ROOT}psd.ai"
DESKTOP_DIR="${ROOT}desktop"
LOG_DIR="${ROOT}logs"
LOG_FILE="${LOG_DIR}/run.log"
MODEL_LOG="${LOG_DIR}/local-model.log"

# ------------------------------------------------------------------
# Options
# ------------------------------------------------------------------
DOCTOR=0
REPAIR=0
UPDATE=0
REBUILD=0
NO_VOICE=0
NO_APP=0
NO_GUI="${PSD_NO_GUI:-0}"
NO_SYSTEM_DEPS="${PSD_NO_SYSTEM_DEPS:-0}"
SKIP_NUMPY_CHECK="${PSD_AI_SKIP_NUMPY_CHECK:-0}"

usage() {
    # The header comment IS the help text: one source of truth, no drift.
    awk 'NR>1 && /^# =+$/ && seen {exit} NR>1 {if (/^# =+$/) seen=1; print}' \
        "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

for arg in "$@"; do
    case "$arg" in
        --help|-h|-?)         usage; exit 0 ;;
        --doctor)             DOCTOR=1 ;;
        --repair)             REPAIR=1 ;;
        --update)             UPDATE=1 ;;
        --rebuild)            REBUILD=1 ;;
        --no-voice)           NO_VOICE=1 ;;
        --no-models)          PSD_NO_LOCAL_MODEL=1 ;;
        --no-app)             NO_APP=1 ;;
        --no-gui)             NO_GUI=1 ;;
        --no-system-deps)     NO_SYSTEM_DEPS=1 ;;
        --skip-numpy-check)   SKIP_NUMPY_CHECK=1 ;;
        *)
            echo "  [ERROR] unknown option: $arg"
            echo "          try:  ./run.sh --help"
            exit 2
            ;;
    esac
done

mkdir -p "$LOG_DIR" 2>/dev/null || true

log() {
    # One timestamped line in logs/run.log. Best effort, never fatal.
    printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG_FILE" 2>/dev/null || true
}

# ------------------------------------------------------------------
# Graphical install dashboard
#
#   run.sh appends one JSON-lines marker per step transition to
#   logs/install-steps.jsonl; scripts/install_gui.py serves a browser
#   dashboard from those markers plus the two log tails (steps, download
#   progress bar, model cards, error panel, live console). Everything
#   here is best effort: no python3, no browser, no free port - the
#   terminal install proceeds exactly as before.
# ------------------------------------------------------------------
GUI_STEP_FILE="${LOG_DIR}/install-steps.jsonl"
GUI_PID=""
GUI_URL=""
CURRENT_STEP=""

gui_step() {
    # gui_step <id> <pending|running|done|warn|failed|skipped> [message]
    local step="$1" state="$2" msg="${3:-}"
    if [ "$state" = "running" ]; then CURRENT_STEP="$step"; fi
    msg="$(printf '%s' "$msg" | tr '\n' ' ' | tr -d '"\\' | cut -c1-180)"
    printf '{"ts":"%s","step":"%s","state":"%s","msg":"%s"}\n' \
        "$(date '+%H:%M:%S')" "$step" "$state" "$msg" \
        >>"$GUI_STEP_FILE" 2>/dev/null || true
}

start_install_gui() {
    # start_install_gui <python interpreter>
    [ "$NO_GUI" = "1" ] && return 0
    [ -n "$GUI_URL" ] && return 0
    local py="$1"
    local port_file="${LOG_DIR}/install-gui.port"
    local pid_file="${LOG_DIR}/install-gui.pid"

    # A dashboard from a run minutes ago may still be in its grace window;
    # reuse it instead of stacking servers on the same port.
    if [ -r "$pid_file" ] && kill -0 "$(cat "$pid_file" 2>/dev/null)" 2>/dev/null \
       && [ -r "$port_file" ]; then
        GUI_URL="http://127.0.0.1:$(cat "$port_file" 2>/dev/null)"
    else
        rm -f "$port_file" "$pid_file" 2>/dev/null || true
        setsid "$py" "${APP_DIR}/scripts/install_gui.py" \
            --log-dir "$LOG_DIR" \
            --port "${PSD_INSTALL_GUI_PORT:-7123}" \
            --run-pid "$$" >>"${LOG_DIR}/install-gui.log" 2>&1 &
        GUI_PID=$!
        local i
        for i in 1 2 3 4 5 6 7 8 9 10; do
            [ -r "$port_file" ] && break
            sleep 0.3 2>/dev/null || sleep 1
        done
        [ -r "$port_file" ] || return 0   # server never came up: terminal-only
        GUI_URL="http://127.0.0.1:$(cat "$port_file" 2>/dev/null)"
    fi
    [ -n "$GUI_URL" ] || return 0
    echo "  ==> Install dashboard:  $GUI_URL"
    log "install dashboard at $GUI_URL"
    if [ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ] && have_bin xdg-open; then
        ( setsid xdg-open "$GUI_URL" >/dev/null 2>&1 & ) 2>/dev/null || true
    fi
}

die() {
    echo
    echo "  [ERROR] $*"
    echo
    # Mark the step that was in flight, then the run itself, so the browser
    # dashboard shows exactly where and why the install died - and stays up
    # for its grace window after this script exits.
    if [ -n "$CURRENT_STEP" ]; then gui_step "$CURRENT_STEP" failed "$*"; fi
    gui_step run failed "$*"
    if [ -n "$GUI_URL" ]; then
        echo "          Details are on the install dashboard: $GUI_URL"
        echo "          (it stays open for a while after this script exits)"
        echo
    fi
    log "ERROR: $*"
    exit 1
}

log "run.sh started (args: $*)"

# ------------------------------------------------------------------
# Linux only. Everything below assumes dnf, /proc, systemd and the
# Wayland/X11 stack that Fedora ships.
# ------------------------------------------------------------------
if [ "$(uname -s)" != "Linux" ]; then
    die "psd.ai runs on Linux only (this is $(uname -s))."
fi

OS_NAME="Linux"
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    OS_NAME="$(. /etc/os-release && echo "${PRETTY_NAME:-Linux}")"
fi

# Models + llama.cpp live OUTSIDE this folder, in
# ~/.local/share/psd.ai/runtime (override with PSD_AI_RUNTIME_DIR), so a fresh
# copy of the code reuses them. That is the XDG data dir, which local_llama.py
# also defaults to - the two must agree or the cache is downloaded twice.
PSD_AI_RUNTIME_DIR="${PSD_AI_RUNTIME_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/psd.ai/runtime}"
export PSD_AI_RUNTIME_DIR

LLAMA_PORT="${LLAMA_PORT:-8080}"
MODEL_WAIT_SECONDS="${MODEL_WAIT_SECONDS:-2400}"
NUMPY_IMPORT_BUDGET="${NUMPY_IMPORT_BUDGET:-60}"

# One process BLAS thread per library. psd.ai never does big matrix work in
# Python - the local model server owns the CPU - and a numpy import that wakes
# 16 OpenMP threads steals cores from llama.cpp and from the app itself.
export OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1

MODEL_PID=""

cleanup() {
    local rc=$?
    # The dashboard outlives this script by its grace window so a failure
    # stays readable in the browser; this marker flips it to "finished".
    gui_step run exited "code $rc"
    # The model group is a child of this script, so it goes when the app does.
    # setsid gave it its own process group; killing the group also reaps the
    # llama-server processes it spawned.
    if [ -n "$MODEL_PID" ] && kill -0 "$MODEL_PID" 2>/dev/null; then
        echo "  ==> Stopping the local model group..."
        kill -TERM "-$MODEL_PID" 2>/dev/null || kill -TERM "$MODEL_PID" 2>/dev/null || true
        sleep 1
        kill -KILL "-$MODEL_PID" 2>/dev/null || kill -KILL "$MODEL_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# ==================================================================
#  System packages (dnf)
# ==================================================================
# Split by purpose so a missing optional piece is a warning and a missing
# build prerequisite is a hard stop with the exact command to fix it.
PKGS_CORE=(python3 python3-pip python3-devel gcc gcc-c++ make git curl tar gzip
           findutils which pkgconf-pkg-config openssl-devel libffi-devel
           zlib-devel xz-devel sqlite-devel)
PKGS_DESKTOP=(nodejs npm rust cargo patchelf webkit2gtk4.1-devel gtk3-devel
              glib2-devel libsoup3-devel javascriptcoregtk4.1-devel
              alsa-lib-devel librsvg2-devel)
PKGS_JARVIS=(xdotool wmctrl xclip xprop xrandr scrot wl-clipboard grim slurp
             ydotool wtype pipewire-utils alsa-utils libnotify xdg-utils gvfs
             mesa-vulkan-drivers vulkan-tools)

have_pkg() { rpm -q "$1" >/dev/null 2>&1; }
have_bin() { command -v "$1" >/dev/null 2>&1; }

missing_packages() {
    # A package is "missing" when neither the RPM nor the binary it provides is
    # present: several of these (nodejs, xdotool, ...) may have been installed
    # by hand, from a different repo, or as part of a group install.
    local missing=()
    local pkg
    for pkg in "$@"; do
        if have_pkg "$pkg"; then continue; fi
        case "$pkg" in
            nodejs)          have_bin node && continue ;;
            npm)             have_bin npm && continue ;;
            rust|cargo)      have_bin cargo && continue ;;
            python3-pip)     have_bin pip3 && continue ;;
            findutils)       have_bin find && continue ;;
            which)           have_bin which && continue ;;
            pkgconf-pkg-config) have_bin pkg-config && continue ;;
            xdg-utils)       have_bin xdg-open && continue ;;
            gvfs)            have_bin gio && continue ;;
            libnotify)       have_bin notify-send && continue ;;
            pipewire-utils)  have_bin wpctl && continue ;;
            alsa-utils)      have_bin amixer && continue ;;
            wl-clipboard)    have_bin wl-copy && continue ;;
            vulkan-tools)    have_bin vulkaninfo && continue ;;
            python3-devel)   compgen -G "/usr/include/python3.*/pyconfig.h" >/dev/null && continue ;;
        esac
        missing+=("$pkg")
    done
    printf '%s\n' "${missing[@]-}"
}

sudo_cmd() {
    # Root is only needed for dnf. Under sudo already, or in a container
    # without sudo, adapt instead of failing.
    if [ "$(id -u)" = "0" ]; then
        echo ""
    elif have_bin sudo; then
        echo "sudo"
    else
        echo "pkexec"
    fi
}

install_system_deps() {
    if [ "$NO_SYSTEM_DEPS" = "1" ]; then
        echo "  ==> PSD_NO_SYSTEM_DEPS/--no-system-deps: not touching dnf."
        return 0
    fi
    if ! have_bin dnf; then
        echo "  [WARN] dnf not found. $OS_NAME is not a Fedora-family system,"
        echo "         so install the equivalent of these packages by hand:"
        echo "           ${PKGS_CORE[*]}"
        echo "           ${PKGS_DESKTOP[*]}"
        echo "           ${PKGS_JARVIS[*]}"
        log "WARN: dnf missing"
        return 0
    fi
    if [ -e /run/ostree-booted ]; then
        echo "  [WARN] This looks like Fedora Atomic - /usr is read-only."
        echo "         Either run this inside a toolbox/distrobox container, or:"
        echo "           rpm-ostree install ${PKGS_CORE[*]}"
        echo "         then reboot and re-run with --no-system-deps."
        log "WARN: rpm-ostree system"
        return 0
    fi

    local missing
    missing="$(missing_packages "${PKGS_CORE[@]}" "${PKGS_DESKTOP[@]}" "${PKGS_JARVIS[@]}")"
    if [ -z "$missing" ]; then
        echo "  ==> System packages already installed - skipping dnf."
        return 0
    fi

    echo "  ==> Installing system packages with dnf (first run only)..."
    echo "      $(echo "$missing" | tr '\n' ' ')"
    echo "      A password prompt is normal here - this is the only step that needs root."
    log "dnf install: $(echo "$missing" | tr '\n' ' ')"

    # shellcheck disable=SC2046
    if $(sudo_cmd) dnf install -y $(echo "$missing" | tr '\n' ' '); then
        echo "      System packages installed."
        return 0
    fi

    echo
    echo "  [WARN] dnf could not install everything. Common causes:"
    echo "           - the password prompt was declined (re-run to try again)"
    echo "           - no network, or a proxy is blocking the mirrors"
    echo "           - a package is not in the enabled repos"
    echo "         Install what you can by hand and re-run:"
    echo "           $(sudo_cmd) dnf install $(echo "$missing" | tr '\n' ' ')"
    echo "         Everything that is genuinely missing shows up in: ./run.sh --doctor"
    log "WARN: dnf install failed"
    return 0
}

# ==================================================================
#  --doctor : report on this machine and stop
# ==================================================================
doctor() {
    echo "  ============================================================"
    echo "    psd.ai doctor"
    echo "  ============================================================"
    echo
    echo "   Machine: $(hostname 2>/dev/null || uname -n) ( $(uname -m) )"
    echo "   OS:      $OS_NAME"
    if [ -r /etc/fedora-release ]; then
        echo "   Release: $(cat /etc/fedora-release)"
    fi
    echo "   Kernel:  $(uname -r)"
    echo

    echo "  -- Session -------------------------------------------------"
    local session="${XDG_SESSION_TYPE:-unknown}"
    echo "   [ OK ]  display server: $session  (desktop: ${XDG_CURRENT_DESKTOP:-unknown})"
    if [ "$session" = "wayland" ]; then
        echo "           Wayland: keyboard/mouse control needs uinput, so enable"
        echo "           the ydotool daemon once:"
        echo "             systemctl --user enable --now ydotoold"
        echo "           Screenshots go through the XDG Desktop Portal and will"
        echo "           ask for permission the first time."
    fi
    if [ -e /run/ostree-booted ]; then
        echo "   [WARN]  Fedora Atomic detected - install packages with rpm-ostree"
        echo "           or work inside a toolbox container."
    fi
    echo

    echo "  -- SELinux --------------------------------------------------"
    if have_bin getenforce; then
        local mode
        mode="$(getenforce 2>/dev/null || echo Unknown)"
        echo "   [ OK ]  SELinux is $mode"
        if [ "$mode" = "Enforcing" ] && [ -n "${ROOT##/home/*}" ] && [ -n "${ROOT##/root/*}" ]; then
            echo "   [WARN]  The project lives outside /home, where SELinux policy is"
            echo "           stricter about execmem/exec on user content. If the venv"
            echo "           or the built app is denied, check:"
            echo "             sudo ausearch -m avc -ts recent"
        fi
    else
        echo "   [ .. ]  getenforce not installed (selinux-policy-targeted missing?)"
    fi
    echo

    echo "  -- Python ---------------------------------------------------"
    local pycmd
    pycmd="$(find_python)"
    if [ -z "$pycmd" ]; then
        echo "   [FAIL]  No Python 3.11+ found."
        echo "             $(sudo_cmd) dnf install python3 python3-pip python3-devel"
    else
        echo "   [ OK ]  Python $("$pycmd" -c 'import platform;print(platform.python_version())') ($pycmd)"
    fi
    if have_bin pip3 || [ -n "$pycmd" ] && "$pycmd" -c 'import ensurepip' 2>/dev/null; then
        echo "   [ OK ]  venv can bootstrap pip"
    else
        echo "   [FAIL]  python3 -m venv cannot bootstrap pip."
        echo "             $(sudo_cmd) dnf install python3-pip"
    fi
    echo

    echo "  -- Virtual environment -------------------------------------"
    local venvpy="${APP_DIR}/venv/bin/python"
    if [ ! -x "$venvpy" ]; then
        echo "   [FAIL]  no venv yet - run ./run.sh once"
    else
        echo "   [ OK ]  venv present: ${APP_DIR}/venv"
        local stamp
        for stamp in .deps_ok:"dependencies installed":"dependencies missing - run ./run.sh" \
                     .jarvis_ok:"Jarvis extras installed":"Jarvis extras not installed yet" \
                     .stt_ok:"faster-whisper installed":"faster-whisper not installed (voice input will use the browser)"; do
            local file="${stamp%%:*}" rest="${stamp#*:}" ok="${rest%%:*}" no="${rest#*:}"
            if [ -f "${APP_DIR}/venv/${file}" ]; then
                echo "   [ OK ]  $ok"
            elif [ "$file" = ".deps_ok" ]; then
                echo "   [FAIL]  $no"
            else
                echo "   [ .. ]  $no"
            fi
        done
    fi
    echo

    echo "  -- Microphone / audio --------------------------------------"
    if have_bin wpctl; then
        local inputs
        inputs="$(wpctl status 2>/dev/null | grep -ci 'input\|capture' || true)"
        if [ "${inputs:-0}" -gt 0 ]; then
            echo "   [ OK ]  PipeWire sees $inputs capture node(s)."
        else
            echo "   [WARN]  PipeWire reports no capture node - check the mic in"
            echo "           GNOME Settings > Sound, or:  wpctl status"
        fi
    elif have_bin arecord; then
        if arecord -l 2>/dev/null | grep -q '^card'; then
            echo "   [ OK ]  ALSA sees a capture card: $(arecord -l | grep -c '^card') card(s)."
        else
            echo "   [FAIL]  No ALSA capture card found - voice mode needs a microphone."
        fi
    else
        echo "   [ .. ]  No wpctl/arecord - $(sudo_cmd) dnf install pipewire-utils alsa-utils"
    fi
    if have_bin pactl; then
        echo "   [ OK ]  PulseAudio compatibility layer present (pactl)."
    fi
    echo

    echo "  -- GPU ------------------------------------------------------"
    if have_bin lspci; then
        local vga
        vga="$(lspci 2>/dev/null | grep -Ei 'vga|3d|display' | head -3)"
        if [ -n "$vga" ]; then
            echo "$vga" | sed 's/^/   [ OK ]  /'
        else
            echo "   [ .. ]  lspci reports no display adapter (a VM?)."
        fi
    else
        echo "   [ .. ]  pciutils not installed - cannot list the GPU."
    fi
    if have_bin nvidia-smi; then
        echo "   [ OK ]  nvidia-smi: $(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>/dev/null | head -1)"
        echo "           llama.cpp will use the CUDA build."
    elif have_bin vulkaninfo; then
        local vk
        vk="$(vulkaninfo --summary 2>/dev/null | grep -m1 'deviceName' | sed 's/.*= //')"
        if [ -n "$vk" ]; then
            echo "   [ OK ]  Vulkan device: $vk (llama.cpp will use the Vulkan build)"
        else
            echo "   [WARN]  vulkaninfo found no device - $(sudo_cmd) dnf install mesa-vulkan-drivers"
        fi
    else
        echo "   [ .. ]  No CUDA or Vulkan tooling - models will run on the CPU."
        echo "           Intel/AMD iGPU: $(sudo_cmd) dnf install mesa-vulkan-drivers vulkan-tools"
    fi
    echo

    echo "  -- Disk space ------------------------------------------------"
    local free_gb
    free_gb="$(free_space_gb "$(dirname "$PSD_AI_RUNTIME_DIR")")"
    echo "   [ OK ]  ${free_gb} GB free on the filesystem holding $PSD_AI_RUNTIME_DIR"
    if [ "${free_gb:-0}" -lt 8 ]; then
        echo "   [WARN]  The local model group wants 10-20 GB. Point it somewhere"
        echo "           bigger with PSD_AI_RUNTIME_DIR=/mnt/data/psd.ai/runtime"
    fi
    echo

    echo "  -- Desktop app -----------------------------------------------"
    local app
    app="$(find_app_binary)"
    if [ -n "$app" ]; then
        echo "   [ OK ]  $app"
    else
        echo "   [ .. ]  no built app - ./run.sh will build it (needs Node + Rust once)."
    fi
    if [ -d "$PSD_AI_RUNTIME_DIR" ]; then
        echo "   [ OK ]  model cache: $PSD_AI_RUNTIME_DIR"
    else
        echo "   [ .. ]  model cache not created yet: $PSD_AI_RUNTIME_DIR"
    fi
    echo

    echo "  -- PC control stack ------------------------------------------"
    local tool
    for tool in xdotool wmctrl xclip wl-copy wl-paste grim slurp wtype ydotool \
                notify-send wpctl xdg-open ffmpeg; do
        if have_bin "$tool"; then
            echo "   [ OK ]  $tool"
        else
            echo "   [ .. ]  $tool missing ($(_package_for "$tool"))"
        fi
    done
    if [ "${XDG_SESSION_TYPE:-}" = "wayland" ]; then
        if have_bin ydotool && ! (systemctl --user is-active ydotoold >/dev/null 2>&1 \
                                 || systemctl is-active ydotool >/dev/null 2>&1); then
            echo "   [WARN]  ydotoold is not running - typing/clicking on Wayland needs it:"
            echo "             systemctl --user enable --now ydotoold"
        fi
        if ! have_bin grim || ! have_bin slurp; then
            echo "   [ .. ]  Wayland screenshots use the portal instead; grim+slurp are"
            echo "           the faster, prompt-free path:  $(sudo_cmd) dnf install grim slurp"
        fi
    fi
    echo

    echo "  -- numpy ------------------------------------------------------"
    if [ ! -x "$venvpy" ]; then
        echo "   [ .. ]  no venv yet - run ./run.sh once"
    else
        if verify_numpy "$NUMPY_IMPORT_BUDGET"; then
            echo "   [ OK ]  \"import numpy\" finished in ${NP_SECONDS}s"
        else
            echo "   [FAIL]  \"import numpy\" did not finish in ${NP_SECONDS}s"
            echo "             $venvpy -c \"import numpy; print(numpy.__version__)\""
            echo "           to see the real error, then ./run.sh --repair"
        fi
    fi
    echo

    echo "  -- Jarvis / PC control ----------------------------------------"
    if [ -x "$venvpy" ]; then
        ( cd "$APP_DIR" && "$venvpy" -c "import sys; sys.path.insert(0, '.'); from services.computer import get_computer_service as g; import json; print(json.dumps(g().status(), indent=2)[:1200])" 2>/dev/null ) \
            || echo "   [ .. ]  Run ./run.sh once before the PC-control report is available."
        "$venvpy" -c "import importlib.util as u; print('[ OK ]  faster-whisper installed' if u.find_spec('faster_whisper') else '[ .. ]  faster-whisper missing - voice input will use the browser')"
    else
        echo "   [ .. ]  venv not created yet."
    fi
    echo
    echo "   Log: $LOG_FILE"
    echo
}

_package_for() {
    case "$1" in
        xdotool) echo "dnf install xdotool" ;;
        wmctrl) echo "dnf install wmctrl" ;;
        xclip) echo "dnf install xclip" ;;
        wl-copy|wl-paste) echo "dnf install wl-clipboard" ;;
        grim) echo "dnf install grim" ;;
        slurp) echo "dnf install slurp" ;;
        wtype) echo "dnf install wtype" ;;
        ydotool) echo "dnf install ydotool" ;;
        notify-send) echo "dnf install libnotify" ;;
        wpctl) echo "dnf install pipewire-utils" ;;
        xdg-open) echo "dnf install xdg-utils" ;;
        ffmpeg) echo "needs RPM Fusion - see the note at the end of setup" ;;
        *) echo "package unknown" ;;
    esac
}

free_space_gb() {
    # Free GB on the filesystem holding $1 (rounded down), or empty.
    local path="${1:-$ROOT}"
    # The runtime directory does not exist before the first download, so walk
    # up to the nearest ancestor that does: df reports the mount that a new
    # directory would land on, which is the number that matters here.
    while [ ! -d "$path" ] && [ "$path" != "/" ]; do
        path="$(dirname "$path")"
    done
    local kb
    kb="$(df -Pk "$path" 2>/dev/null | awk 'NR==2 {print $4}')"
    [ -n "$kb" ] && echo $((kb / 1024 / 1024))
}

# ==================================================================
#  Python discovery
# ==================================================================
find_python() {
    # Prefer 3.12 / 3.13 / 3.11 (mature wheels for numpy, onnxruntime, ...).
    # Fedora 41+ ships 3.13, 40 ships 3.12; 3.14 is accepted last because some
    # native packages are still settling there.
    local candidate
    for candidate in python3.12 python3.13 python3.11 python3.14; do
        if have_bin "$candidate"; then echo "$candidate"; return 0; fi
    done
    if have_bin python3 && python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        echo python3; return 0
    fi
    if have_bin python && python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        echo python; return 0
    fi
    return 1
}

find_app_binary() {
    local candidate
    for candidate in \
        "${DESKTOP_DIR}/src-tauri/target/release/psd-ai-desktop" \
        "${DESKTOP_DIR}/psd.ai" \
        "${ROOT}psd.ai" \
        "/opt/psd-ai/psd-ai-desktop" \
        "/usr/bin/psd-ai-desktop"; do
        # -f matters: ${ROOT}psd.ai is the source *directory*, and a directory
        # is executable, so -x alone would report the app as already built.
        if [ -f "$candidate" ] && [ -x "$candidate" ]; then echo "$candidate"; return 0; fi
    done
    return 1
}

# ==================================================================
#  numpy import check
# ==================================================================
NP_SECONDS=0
verify_numpy() {
    # 0 = "import numpy" finished within the budget, 1 = it did not. A slow
    # first import is retried once: it warms the page cache, so the second pass
    # proves whether the library is genuinely stuck or merely cold.
    local budget="${1:-$NUMPY_IMPORT_BUDGET}"
    local attempt
    for attempt in 1 2; do
        _numpy_once "$budget" && return 0
        [ "$attempt" = 1 ] && echo "      ...no answer after ${NP_SECONDS}s - retrying once before calling it stuck"
    done
    return 1
}

_numpy_once() {
    local budget="${1:-60}"
    local start end
    start="$(date +%s)"
    if timeout --preserve-status -k 5 "$budget" "$VENVPY" -c 'import numpy' >"${APP_DIR}/venv/.np_err" 2>&1; then
        end="$(date +%s)"
        NP_SECONDS=$((end - start))
        rm -f "${APP_DIR}/venv/.np_err"
        return 0
    fi
    end="$(date +%s)"
    NP_SECONDS=$((end - start))
    if [ -s "${APP_DIR}/venv/.np_err" ]; then
        head -6 "${APP_DIR}/venv/.np_err" | sed 's/^/       /'
    fi
    return 1
}

# ==================================================================
#  pip with one retry
# ==================================================================
pip_run() {
    # $1 = interpreter, rest = pip arguments. Transient network failures are
    # common enough on a first run to be worth one automatic retry.
    local py="$1"; shift
    "$py" "$@" && return 0
    echo "      pip failed - retrying once (transient network errors are common)..."
    sleep 3
    "$py" "$@"
}

# ==================================================================
#  Main flow
# ==================================================================
if [ "$DOCTOR" = "1" ]; then
    VENVPY="${APP_DIR}/venv/bin/python"
    doctor
    exit 0
fi

if [ ! -f "${APP_DIR}/app.py" ]; then
    die "Could not find app.py inside ${APP_DIR}.
         Keep run.sh in the same folder as the psd.ai folder."
fi

echo
echo "  ============================================================"
echo "    psd.ai  -  setup + launch  (desktop app, Fedora Linux)"
echo "  ============================================================"
echo "    $OS_NAME"
echo

# Start the browser dashboard first so it covers the whole install,
# including the dnf step. Needs only a system python3 (any 3.x); the
# venv does not exist yet at this point.
#
# Markers from an earlier run (a --doctor exit also writes one) must not
# colour this one - the dashboard reads the file whole - so a fresh run
# starts with a fresh timeline.
rm -f "$GUI_STEP_FILE" 2>/dev/null || true
if command -v python3 >/dev/null 2>&1; then
    start_install_gui python3
fi

# ------------------------------------------------------------------
# --update : pull the latest code, then continue with setup
# ------------------------------------------------------------------
if [ "$UPDATE" = "1" ]; then
    echo "  ==> Updating psd.ai..."
    if ! have_bin git; then
        echo "      git is not installed - skipping the update."
        echo "      ($(sudo_cmd) dnf install git)"
    else
        if ( cd "$ROOT" && git pull --ff-only ); then
            echo "      Code updated."
        else
            echo "      [WARN] git pull failed - continuing with the current copy."
        fi
    fi
    echo
fi

# ------------------------------------------------------------------
# 1. System packages
# ------------------------------------------------------------------
gui_step system running "checking/installing RPM packages with dnf"
install_system_deps
gui_step system done "system packages present"

if ! have_bin ffmpeg; then
    echo
    echo "  [ .. ]  ffmpeg is not installed. It is only needed for video/audio"
    echo "          transcription, and Fedora keeps it in RPM Fusion:"
    echo "            $(sudo_cmd) dnf install https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-\$(rpm -E %fedora).noarch.rpm"
    echo "            $(sudo_cmd) dnf install ffmpeg"
fi

# ------------------------------------------------------------------
# 2. Python 3.11+
# ------------------------------------------------------------------
gui_step python running "looking for Python 3.11+"
echo "  ==> Looking for Python 3.11+..."
PYCMD="$(find_python)"
if [ -z "$PYCMD" ]; then
    die "Python 3.11+ was not found on this machine.
         Install it and re-run:
           $(sudo_cmd) dnf install python3 python3-pip python3-devel"
fi
PYVER="$("$PYCMD" -c 'import platform; print(platform.python_version())')"
echo "      Using Python $PYVER ($PYCMD)"
log "python=$PYVER via $PYCMD"
gui_step python done "Python $PYVER"
if [ -z "$GUI_URL" ]; then
    # No system python3 before the dnf step - start the dashboard now.
    start_install_gui "$PYCMD"
fi

# ------------------------------------------------------------------
# 3. Virtual environment
# ------------------------------------------------------------------
cd "$APP_DIR" || die "cannot enter $APP_DIR"
VENVPY="${APP_DIR}/venv/bin/python"

if [ "$REPAIR" = "1" ]; then
    echo
    echo "  ==> --repair : removing the venv and the dependency stamps..."
    rm -rf "${APP_DIR}/venv"
    log "repair: venv removed"
fi

if [ -x "$VENVPY" ] && [ -f "venv/.deps_ok" ]; then
    echo "  ==> Checking the virtual environment..."
    if verify_numpy 20; then
        echo "       venv OK"
    else
        echo "      numpy in the existing venv hangs or fails - rebuilding the venv."
        log "venv numpy check failed - rebuilding"
        rm -rf venv
    fi
fi

if [ ! -x "$VENVPY" ]; then
    gui_step venv running "creating the virtual environment"
    echo "  ==> Creating virtual environment (venv)..."
    if ! "$PYCMD" -m venv venv; then
        echo
        echo "  [ERROR] Failed to create the virtual environment."
        echo "          On Fedora this is almost always the pip bootstrap missing:"
        echo "            $(sudo_cmd) dnf install python3-pip python3-devel"
        echo
        gui_step venv failed "python3 -m venv could not bootstrap pip - install python3-pip python3-devel"
        log "ERROR: venv creation failed"
        exit 1
    fi
else
    echo "  ==> Virtual environment already exists - skipping."
fi
gui_step venv done

# ------------------------------------------------------------------
# 4. Dependencies
# ------------------------------------------------------------------
if [ ! -f "venv/.deps_ok" ]; then
    gui_step deps running "pip install -r requirements.txt (a few minutes on first run)"
    echo "  ==> Installing dependencies... first run can take a few minutes."
    pip_run "$VENVPY" -m pip install --upgrade pip --quiet --disable-pip-version-check \
        || echo "      [WARN] pip upgrade failed - continuing with the bundled pip."
    if ! pip_run "$VENVPY" -m pip install -r requirements.txt --disable-pip-version-check --no-input; then
        echo
        echo "  [ERROR] Dependency install failed - scroll up for the pip error."
        echo "          Common causes: no internet, a proxy, or a compiler missing"
        echo "          for a source build. Fix it, then re-run ./run.sh"
        echo "          (Or:  ./run.sh --repair  after installing the build deps:"
        echo "            $(sudo_cmd) dnf install ${PKGS_CORE[*]})"
        echo
        gui_step deps failed "pip install -r requirements.txt failed - see the pip error above"
        log "ERROR: pip install requirements.txt failed"
        exit 1
    fi
    echo ok > "venv/.deps_ok"
    gui_step deps done "requirements installed"
else
    echo "  ==> Dependencies already installed - skipping."
    echo "      (./run.sh --repair forces a fresh install.)"
    gui_step deps done "already installed"
fi
if [ "$UPDATE" = "1" ]; then
    echo "  ==> --update : refreshing dependencies..."
    pip_run "$VENVPY" -m pip install -r requirements.txt --disable-pip-version-check --no-input
fi

echo "  ==> Verifying numpy..."
if [ "$SKIP_NUMPY_CHECK" = "1" ]; then
    echo "      skipped (--skip-numpy-check)"
elif verify_numpy "$NUMPY_IMPORT_BUDGET"; then
    echo "       numpy OK (${NP_SECONDS}s)"
else
    echo
    echo "  [WARN] \"import numpy\" did not finish in ${NP_SECONDS}s inside this venv."
    echo
    echo "   On Linux that is nearly always one of:"
    echo "     1. A source-built numpy linked against a BLAS that is not there."
    echo "        Reinstall the wheel:  $VENVPY -m pip install --force-reinstall numpy"
    echo "     2. Thread oversubscription on a many-core box - this script already"
    echo "        exports OMP_NUM_THREADS=1, so check for a shell profile that"
    echo "        overrides it."
    echo "     3. A genuinely broken venv - rebuild it:  ./run.sh --repair"
    echo
    echo "   See the real error with:"
    echo "     $VENVPY -c \"import numpy; print(numpy.__version__)\""
    echo
    printf "   Continue anyway and start psd.ai? [y/N] "
    read -r answer
    if [ "${answer,,}" = "y" ]; then
        echo "      Continuing. If numpy really is broken, RAG and semantic search"
        echo "      will be degraded - run  ./run.sh --doctor  to check it again."
        log "WARN: continuing with a numpy import that did not finish"
    else
        gui_step deps failed "numpy import did not finish in this venv"
        log "ERROR: numpy import did not finish"
        exit 1
    fi
fi

# ------------------------------------------------------------------
# 5. Jarvis extras - voice mode ("Talk to psd.ai") + PC control
#
#     requirements-jarvis.txt adds:
#       * faster-whisper - offline speech-to-text for the microphone
#       * pyautogui / mss / pyperclip / psutil - mouse, keyboard,
#         screen capture, clipboard, process control
#     The Linux-specific pieces are system binaries (xdotool, wmctrl,
#     wl-clipboard, grim/slurp, wtype/ydotool, wpctl), installed by dnf
#     in step 1. Every one of them is optional in code: without them
#     psd.ai falls back to the XDG portal or to browser speech, so a
#     failure here is a warning, never a hard stop.
# ------------------------------------------------------------------
if [ "$NO_VOICE" = "1" ]; then
    gui_step voice skipped "--no-voice"
    echo
    echo "  ==> --no-voice : skipping the Jarvis extras."
    echo "      Voice mode will use the browser for speech, and PC control"
    echo "      will rely on the system binaries alone."
elif [ ! -f "requirements-jarvis.txt" ]; then
    gui_step voice skipped "requirements-jarvis.txt not found"
    echo "  ==> requirements-jarvis.txt not found - skipping the Jarvis extras."
else
    VOICE_WARN=0
    gui_step voice running "installing voice + PC-control extras"
    if [ ! -f "venv/.jarvis_ok" ]; then
        echo
        echo "  ==> Installing the Jarvis extras (voice + PC control)..."
        echo "      faster-whisper, pyautogui, mss, pyperclip, psutil"
        if pip_run "$VENVPY" -m pip install -r requirements-jarvis.txt --disable-pip-version-check --no-input; then
            echo ok > "venv/.jarvis_ok"
            echo "      Jarvis extras installed."
            log "jarvis extras installed"
        else
            echo "      [WARN] Some Jarvis extras failed to install."
            echo "             Voice + PC control still work using the built-in"
            echo "             fallbacks; re-run later with:"
            echo "               $VENVPY -m pip install -r requirements-jarvis.txt"
            log "WARN: jarvis extras install failed"
            VOICE_WARN=1
        fi
    else
        echo "  ==> Jarvis extras already installed - skipping."
    fi

    # faster-whisper is the one heavy piece (~1 GB with ctranslate2), so it
    # gets its own stamp and its own opt-out.
    if [ -n "${PSD_NO_LOCAL_STT:-}" ]; then
        echo "  ==> PSD_NO_LOCAL_STT is set - skipping faster-whisper."
    elif [ ! -f "venv/.stt_ok" ]; then
        echo "  ==> Installing faster-whisper (offline speech-to-text)..."
        if pip_run "$VENVPY" -m pip install faster-whisper --disable-pip-version-check --no-input; then
            echo ok > "venv/.stt_ok"
            echo "      faster-whisper installed - the microphone now works offline."
            log "faster-whisper installed"
        else
            echo "      [WARN] faster-whisper did not install."
            echo "             Set PSD_NO_LOCAL_STT=1 to stop asking, or install it"
            echo "             later. Voice mode will use the browser instead."
            log "WARN: faster-whisper install failed"
            VOICE_WARN=1
        fi
    fi
    if [ "${VOICE_WARN:-0}" = "1" ]; then
        gui_step voice warn "some extras failed - built-in fallbacks will be used"
    else
        gui_step voice done
    fi
fi

# ------------------------------------------------------------------
# 6. First-time setup (data folders, database, .env). The admin account
#    is created inside the desktop app's own first-run screen.
# ------------------------------------------------------------------
gui_step setup running "setup.py (data folders, database, .env)"
echo "  ==> Running setup..."
export PSD_AI_DEFER_ADMIN=1
export PSD_AI_SKIP_RUN_HINT=1
if ! "$VENVPY" setup.py; then
    die "setup.py failed - scroll up for details."
fi
gui_step setup done

if [ "$NO_APP" = "1" ]; then
    echo
    echo "  ==> --no-app : setup finished. The desktop app was not started."
    echo
    gui_step desktop skipped "--no-app"
    gui_step launch skipped "--no-app"
    log "setup done (--no-app)"
    exit 0
fi

# ------------------------------------------------------------------
# 7. Local AI model group (first run downloads 3-5 fit models)
#
#     Picks a 3-5 model group for THIS machine (RAM / GPU / VRAM),
#     downloads the llama.cpp server + model weights, serves one model
#     per port starting at $LLAMA_PORT, and registers the group in the app.
#
#     On Linux this runs in the background with its own log instead of a
#     second terminal window: spawning one is unreliable under Wayland
#     (the terminal is chosen by the desktop, not by us) and useless on a
#     headless box. Watch it with:  tail -f logs/local-model.log
#
#     Set PSD_NO_LOCAL_MODEL=1 to skip this and bring your own model.
# ------------------------------------------------------------------
if [ -z "${PSD_NO_LOCAL_MODEL:-}" ]; then
    gui_step models running "downloading & serving the model group (first run: several GB)"
    echo
    echo "  ==> Starting the local model group in the background..."
    echo "      First run downloads llama.cpp + 3-5 fit model weights (a few GB each)."
    echo "      Models are stored at: $PSD_AI_RUNTIME_DIR"
    echo "      (outside the project folder, so re-downloading the code never"
    echo "       re-downloads models). Progress:  tail -f logs/local-model.log"
    echo "      Profile: ${PSD_MODEL_PROFILE:-balanced}  Port: $LLAMA_PORT"

    free_gb="$(free_space_gb "$(dirname "$PSD_AI_RUNTIME_DIR")")"
    if [ -n "$free_gb" ] && [ "$free_gb" -lt 8 ]; then
        echo
        echo "      [WARN] Only ${free_gb} GB free on the filesystem holding the model"
        echo "             cache. The group needs roughly 10-20 GB. psd.ai will still"
        echo "             start, but the download may fail part-way. Free some space,"
        echo "             point PSD_AI_RUNTIME_DIR at a bigger filesystem, or set"
        echo "             PSD_NO_LOCAL_MODEL=1 to skip it."
        log "WARN: low disk space (${free_gb} GB)"
    fi

    mkdir -p "$PSD_AI_RUNTIME_DIR" 2>/dev/null || true
    rm -f "${PSD_AI_RUNTIME_DIR}/local_model_failed.txt"

    # setsid detaches it into its own process group so `cleanup` can stop the
    # whole tree (this script, the launcher, and every llama-server it forked)
    # with one signal when the app window closes.
    setsid "$VENVPY" scripts/local_llama.py --port "$LLAMA_PORT" --foreground \
        >>"$MODEL_LOG" 2>&1 &
    MODEL_PID=$!
    log "model group started (pid $MODEL_PID), log $MODEL_LOG"

    "$VENVPY" scripts/local_llama.py --wait-ready "$MODEL_WAIT_SECONDS"
    if [ -f "${PSD_AI_RUNTIME_DIR}/local_model_failed.txt" ]; then
        gui_step models warn "model group reported a failure - psd.ai still starts; add a model in Settings"
        echo "      [WARN] the model group reported a failure:"
        sed 's/^/             /' "${PSD_AI_RUNTIME_DIR}/local_model_failed.txt" 2>/dev/null | head -12
        echo "             psd.ai will still start; you can add a model in Settings."
        log "WARN: model group reported failure"
    elif [ -f "${PSD_AI_RUNTIME_DIR}/local_model.json" ]; then
        n_models="$(grep -o '"count":[[:space:]]*[0-9]*' \
            "${PSD_AI_RUNTIME_DIR}/local_model_group.json" 2>/dev/null \
            | head -1 | grep -o '[0-9]*$')"
        gui_step models done "${n_models:+$n_models }local models serving"
    else
        # wait-ready timed out but nothing failed: the bootstrap keeps
        # downloading in the background and the dashboard follows the log.
        gui_step models running "still downloading in the background - the dashboard follows it live"
    fi
else
    gui_step models skipped "PSD_NO_LOCAL_MODEL"
    echo
    echo "  ==> PSD_NO_LOCAL_MODEL is set - skipping the local model download."
fi

# ------------------------------------------------------------------
# 8. Launch the desktop app.
#
#     Preference order:
#       a) a built app:     desktop/src-tauri/target/release/psd-ai-desktop
#       b) a portable copy: desktop/psd.ai, /opt/psd-ai, or one on PATH
#       c) build from source: Node.js + Rust are installed by dnf in
#          step 1 if missing, then the app is built once (later runs
#          reuse the binary)
#
#     The app spawns psd.ai/desktop_server.py itself on a private,
#     random loopback port and talks to it through IPC. Nothing is
#     opened in a browser and no fixed port is used.
# ------------------------------------------------------------------
echo
echo "  ==> Opening the psd.ai desktop app..."
echo "      Close the app window to stop psd.ai."
echo

export PSD_AI_APP_DIR="$APP_DIR"
export PSD_AI_PYTHON="$VENVPY"

APP_BIN=""
if [ "$REBUILD" = "0" ]; then
    APP_BIN="$(find_app_binary)"
fi

if [ -n "$APP_BIN" ]; then
    gui_step desktop done "using the prebuilt app"
    gui_step launch running "starting the desktop app"
    echo "      Using $APP_BIN"
    log "launching app: $APP_BIN"
    "$APP_BIN"
    gui_step launch done "app closed"
    echo
    echo "  ------------------------------------------------------------"
    echo "   psd.ai has closed."
    echo
    echo "   Tip: press Ctrl+Shift+T inside the app (or click the mic in"
    echo "   the left rail) to open \"Talk to psd.ai\" - speak in any"
    echo "   language and it answers in English, and it can drive this PC."
    echo "  ------------------------------------------------------------"
    log "run.sh finished"
    exit 0
fi

if [ "$REBUILD" = "1" ] && [ -x "${DESKTOP_DIR}/src-tauri/target/release/psd-ai-desktop" ]; then
    echo "  ==> --rebuild : discarding the previous build."
    rm -f "${DESKTOP_DIR}/src-tauri/target/release/psd-ai-desktop"
fi

# ---- build from source ----
if ! have_bin node || ! have_bin cargo; then
    echo
    echo "  ==> No built app found. The build toolchain (Node.js, Rust) is"
    echo "      installed by dnf - see step 1 above, or run:"
    echo "        $(sudo_cmd) dnf install ${PKGS_DESKTOP[*]}"
    echo
    if [ "$NO_SYSTEM_DEPS" = "1" ]; then
        die "Node.js/Rust are missing and --no-system-deps blocks installing them."
    fi
    install_system_deps
fi
have_bin node || die "Node.js is still not available on PATH after install."
have_bin cargo || die "Rust (cargo) is still not available on PATH after install."

# ~/.cargo/bin is where rustup puts things, if it was used instead of dnf.
if [ -x "$HOME/.cargo/bin/cargo" ]; then
    PATH="$HOME/.cargo/bin:$PATH"
    export PATH
fi

cd "$DESKTOP_DIR" || die "cannot enter $DESKTOP_DIR"
gui_step desktop running "npm install + Rust build of the desktop app (first run: a few minutes)"
# The gate is the tauri binary, not the node_modules directory: an install
# that died halfway leaves the directory behind but no .bin/tauri, and then
# `npm run tauri` fails with "tauri: command not found" on a box that looks
# already installed.
if [ ! -x node_modules/.bin/tauri ]; then
    echo "  ==> Installing desktop UI dependencies (first run only)..."
    npm install --no-audit --no-fund || die "npm install failed - scroll up for details."
fi
if [ ! -x node_modules/.bin/tauri ]; then
    echo "  ==> node_modules is incomplete (no tauri binary) - reinstalling once..."
    log "WARN: node_modules present but tauri missing; clean reinstall"
    rm -rf node_modules
    npm install --no-audit --no-fund || die "npm install failed - scroll up for details."
fi

if [ ! -x "src-tauri/target/release/psd-ai-desktop" ]; then
    echo "  ==> Building the desktop app (first run only, a few minutes)..."
    if ! npm run tauri build -- --no-bundle; then
        # The engine's model group is already serving in the background, so
        # dying here would throw away a working install over a UI build
        # problem. Fall back to the headless server and say where it is.
        echo
        echo "  [WARN] Desktop app build failed - scroll up for the compiler output."
        echo "         The local model group is already running, so psd.ai continues"
        echo "         in headless mode instead of dying here:"
        echo
        echo "             open http://localhost:${APP_PORT:-7000} in any browser"
        echo
        echo "         Fix the window build later with:"
        echo "             cd desktop && npm install && npm run tauri build"
        log "WARN: desktop build failed; falling back to the headless server"
        gui_step desktop warn "tauri build failed - continuing with the headless server"
        gui_step launch running "headless server on http://localhost:${APP_PORT:-7000}"
        cd "$APP_DIR" || die "cannot enter $APP_DIR"
        "$VENVPY" -m uvicorn app:app \
            --host "${APP_BIND:-127.0.0.1}" --port "${APP_PORT:-7000}" &
        SRV_PID=$!
        wait "$SRV_PID"
        gui_step launch done "headless server stopped"
        echo
        echo "  ------------------------------------------------------------"
        echo "   psd.ai has closed."
        echo "  ------------------------------------------------------------"
        log "run.sh finished (headless fallback)"
        exit 0
    fi
fi

gui_step desktop done "built psd-ai-desktop"
gui_step launch running "starting the desktop app"
echo "      Starting desktop/src-tauri/target/release/psd-ai-desktop"
log "launching freshly built app"
"${DESKTOP_DIR}/src-tauri/target/release/psd-ai-desktop"
gui_step launch done "app closed"

echo
echo "  ------------------------------------------------------------"
echo "   psd.ai has closed."
echo
echo "   Tip: press Ctrl+Shift+T inside the app (or click the mic in"
echo "   the left rail) to open \"Talk to psd.ai\" - speak in any"
echo "   language and it answers in English, and it can drive this PC."
echo "  ------------------------------------------------------------"
log "run.sh finished"
exit 0
