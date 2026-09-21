"""Desktop computer control — screen, mouse, keyboard, windows, processes.

Everything here runs on the machine that hosts psd.ai. That is the point:
the owner asked for an assistant that can actually *use* the PC, not just
talk about it. Two rules keep that honest:

1. **Nothing runs at import time.** No device is touched, no window is
   enumerated, no dependency is imported until an action asks for it. Import
   cost and failure surface stay at zero for the normal chat path.
2. **Every action goes through `policy.check_action` first.** The allow-list
   in `policy.py` is the single source of truth; a name that is not there is
   refused before any platform code runs.

This is a Linux service, written for Fedora Workstation: Wayland first with an
X11 fallback, because that is what a Fedora install actually runs.

Wayland changes the ground rules, and the backends below reflect them:

* **Input** — a client cannot inject events into another client. Everything
  goes through ``ydotool`` (uinput, compositor-independent) and ``wtype``
  (Wayland text input). ydotoold needs uinput access, so
  ``systemctl --user enable --now ydotoold`` is a one-time setup step that
  :func:`ComputerService.status` reports when it is missing. On X11 the
  classic ``xdotool`` does the same job.
* **Capture** — ``grim`` on wlroots compositors (Sway, Hyprland, labwc); the
  XDG Desktop Portal on GNOME and KDE, where a compositor that does not
  implement wlr-screencopy deliberately makes the user approve each capture.
  ``mss`` and Pillow's ImageGrab are X11-only and are tried there.
* **Windows** — there is no cross-compositor window list on Wayland. Sway and
  Hyprland expose their tree over an IPC socket; GNOME does not, and its
  Shell D-Bus ``Eval`` has been disabled since GNOME 41, so listing windows
  returns an empty list with an explanation instead of pretending.
* **Clipboard / audio / notifications** — ``wl-clipboard``, PipeWire's
  ``wpctl``, and ``notify-send``; each has an X11 or PulseAudio fallback.

Optional Python packages (`pyautogui`, `mss`, `pyperclip`, `psutil`) are used
when present and when the session allows them. Every one of them has a
dependency-free fallback built on the tools above, so a fresh install still
drives the machine — `run.sh` installs those tools with dnf.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.settings import get_setting

from .policy import ActionRisk, check_action, describe_policy

logger = logging.getLogger(__name__)

from core.platform_compat import (
    desktop_environment,
    dnf_install_hint,
    is_wayland,
    package_for,
    selinux_mode,
    session_type,
    which_tool,
)

IS_LINUX = sys.platform.startswith("linux")


# ---------------------------------------------------------------------------
# session + backend resolution
#
# Everything below is resolved lazily and cached: a headless engine (a systemd
# unit, a container, CI) must still import this module and answer `status()`
# without a display server being present.
# ---------------------------------------------------------------------------

_BACKEND_CACHE: Dict[str, Optional[str]] = {}


def _tool(name: str) -> Optional[str]:
    """Absolute path to a helper binary, or None."""
    return which_tool(name)


def _session() -> str:
    """`wayland`, `x11`, or "" when there is no graphical session."""
    return session_type()


def _is_wayland() -> bool:
    return is_wayland()


def _compositor() -> str:
    """Best guess at the running compositor: sway, hyprland, gnome, kde, ..."""
    cached = _BACKEND_CACHE.get("compositor")
    if cached is not None:
        return cached
    de = desktop_environment()
    guess = ""
    if os.environ.get("SWAYSOCK"):
        guess = "sway"
    elif os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        guess = "hyprland"
    elif "gnome" in de or "unity" in de or "budgie" in de:
        guess = "gnome"
    elif "kde" in de or "plasma" in de:
        guess = "kde"
    elif de:
        guess = de
    _BACKEND_CACHE["compositor"] = guess
    return guess


def _input_tools() -> List[str]:
    """Input backends in preference order for the current session."""
    if _is_wayland():
        # ydotool drives the kernel's uinput, so it works on every compositor;
        # wtype is the reliable way to get Unicode text in. xdotool is useless
        # here (it talks to an X server that only sees XWayland clients).
        return ["ydotool", "wtype"]
    return ["xdotool", "ydotool"]


def _capture_tools() -> List[str]:
    """Screenshot backends in preference order for the current session.

    The order is session-specific, not universal: grim is the right default on
    a wlroots compositor, but GNOME and KDE deliberately do not implement
    wlr-screencopy, so there the portal has to go first (and it will ask the
    user to approve every capture -- that is the compositor's policy, not ours).
    """
    if _is_wayland():
        order = ["grim", "portal"]
        if _compositor() in ("gnome", "kde"):
            order.reverse()
        return order
    return ["scrot", "import", "gnome-screenshot", "portal"]


def _missing_hint(tools: List[str]) -> str:
    """A copy-pasteable dnf line for the first missing tool in `tools`."""
    missing = [t for t in tools if not _tool(t)]
    return dnf_install_hint(missing) or ""


def _no_backend(what: str, tools: List[str]) -> ComputerActionError:
    hint = _missing_hint(tools)
    detail = f" Install it with: {hint}" if hint else ""
    session = _session() or "no graphical session"
    return ComputerActionError(
        f"Cannot {what}: no backend available on this {session} session "
        f"(tried {', '.join(tools)}).{detail}"
    )

# Hard ceilings so a stuck device driver or a runaway agent can never hang the
# engine: every action runs in a worker thread with this many seconds to live.
ACTION_TIMEOUT_SECONDS = 30
SCREENSHOT_TIMEOUT_SECONDS = 20
MAX_SCREENSHOT_EDGE = 1600   # longest edge of the JPEG/PNG we hand to the UI
MAX_TYPED_CHARS = 5000


class ComputerActionError(Exception):
    """Raised when an action is refused or fails on the platform."""


# ---------------------------------------------------------------------------
# settings helpers
# ---------------------------------------------------------------------------


def _setting(key: str, default: Any = None) -> Any:
    try:
        value = get_setting(key, default)
    except Exception:  # settings file unreadable — fall back to the default
        return default
    return default if value is None else value


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def computer_control_enabled() -> bool:
    """Master switch. Env var wins over the stored setting."""

    env = os.getenv("PSD_AI_COMPUTER_CONTROL")
    if env is not None:
        return _truthy(env, True)
    return _truthy(_setting("computer_control_enabled", True), True)


def _confirm_risky() -> bool:
    return _truthy(_setting("computer_control_confirm", False), False)


# ---------------------------------------------------------------------------
# optional dependency probes
# ---------------------------------------------------------------------------


def _opt(module: str):
    try:
        return __import__(module)
    except Exception:
        return None


def _have(module: str) -> bool:
    return _opt(module) is not None


# ---------------------------------------------------------------------------
# screen
# ---------------------------------------------------------------------------


def _screen_size() -> Dict[str, int]:
    """Primary monitor size in physical pixels.

    Wayland keeps the geometry private from ordinary clients, so the answer
    comes from the compositor's own IPC (Sway/Hyprland), from ``wlr-randr``
    where it is installed, or — failing all of that — from the dimensions of a
    capture, which is authoritative because ``grim`` renders the whole output.
    """

    if _is_wayland():
        for probe in (_wayland_size_from_compositor, _wayland_size_from_randr,
                      _wayland_size_from_capture):
            try:
                size = probe()
            except Exception as exc:
                logger.debug("screen size probe %s failed: %s", probe.__name__, exc)
                continue
            if size:
                return size
        raise _no_backend("read the screen size", ["grim", "wlr-randr"])

    mss = _opt("mss")
    if mss:
        try:
            with mss.mss() as sct:
                mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                return {"width": int(mon["width"]), "height": int(mon["height"])}
        except Exception as exc:
            logger.debug("mss screen size failed: %s", exc)
    for cmd in (["xrandr", "--current"], ["xdpyinfo"]):
        size = _x11_size_from(cmd)
        if size:
            return size
    try:
        from PIL import ImageGrab  # type: ignore

        bbox = ImageGrab.grab().size
        return {"width": int(bbox[0]), "height": int(bbox[1])}
    except Exception as exc:
        raise ComputerActionError(f"Cannot read screen size: {exc}")


def _wayland_size_from_compositor() -> Optional[Dict[str, int]]:
    """Ask Sway or Hyprland for the focused output's mode (they both expose it)."""
    import json

    comp = _compositor()
    if comp == "sway" and _tool("swaymsg"):
        raw = _run(["swaymsg", "-t", "get_outputs"], timeout=8)
        for out in json.loads(raw or "[]"):
            if out.get("focused") and out.get("rect"):
                rect = out["rect"]
                return {"width": int(rect["width"]), "height": int(rect["height"])}
    if comp == "hyprland" and _tool("hyprctl"):
        raw = _run(["hyprctl", "monitors", "-j"], timeout=8)
        for mon in json.loads(raw or "[]"):
            if mon.get("focused"):
                return {"width": int(mon["width"]), "height": int(mon["height"])}
    return None


def _wayland_size_from_randr() -> Optional[Dict[str, int]]:
    """`wlr-randr` prints "Mode: 2560x1440 px" per output."""
    import re

    if not _tool("wlr-randr"):
        return None
    raw = _run(["wlr-randr"], timeout=8)
    for line in (raw or "").splitlines():
        match = re.search(r"(\d+)x(\d+)\s*px", line)
        if match:
            return {"width": int(match.group(1)), "height": int(match.group(2))}
    return None


def _wayland_size_from_capture() -> Optional[Dict[str, int]]:
    """Last resort: measure a full-output capture."""
    if not _tool("grim"):
        return None
    data = _grim_capture()
    return _png_size(data) if data else None


def _png_size(data: bytes) -> Optional[Dict[str, int]]:
    """Width/height straight out of the PNG IHDR — no Pillow needed."""
    import struct

    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return {"width": int(width), "height": int(height)} if width and height else None


def _x11_size_from(cmd: List[str]) -> Optional[Dict[str, int]]:
    """Parse `xrandr --current` / `xdpyinfo` for the screen dimensions."""
    import re

    if not _tool(cmd[0]):
        return None
    try:
        raw = _run(cmd, timeout=8)
    except ComputerActionError:
        return None
    if cmd[0] == "xrandr":
        # "Screen 0: minimum 8 x 8, current 1920 x 1080, ..." -- the first
        # pair on that line is the minimum, so match `current` explicitly.
        match = (re.search(r"\bcurrent\s+(\d+) x (\d+)", raw or "")
                 or re.search(r"connected(?: primary)?\s+(\d+)x(\d+)\+", raw or ""))
    else:
        match = re.search(r"dimensions:\s*(\d+)x(\d+)", raw or "")
    if match:
        return {"width": int(match.group(1)), "height": int(match.group(2))}
    return None


def _screenshot_bytes(monitor: int = 0) -> bytes:
    """Capture the screen and return PNG bytes.

    ``monitor=0`` means "everything"; a positive index picks one output where
    the backend supports it (mss and grim do, the portal does not).
    """

    if _is_wayland():
        return _wayland_screenshot(monitor)

    # X11: mss is the fastest path and is multi-monitor aware.
    mss = _opt("mss")
    if mss:
        try:
            with mss.mss() as sct:
                idx = 0 if monitor <= 0 else min(monitor, len(sct.monitors) - 1)
                raw = sct.grab(sct.monitors[idx])
            return _png_from_raw(raw)
        except Exception as exc:
            logger.debug("mss screenshot failed: %s", exc)

    try:
        from PIL import ImageGrab  # type: ignore

        img = ImageGrab.grab(all_screens=(monitor == 0))
        return _encode_png(img)
    except Exception as exc:
        logger.debug("ImageGrab screenshot failed: %s", exc)

    cli_forms = {
        "scrot": ["scrot", "-o", "-"],
        "import": ["import", "-window", "root", "png:-"],
        "gnome-screenshot": ["gnome-screenshot", "-f", "-"],
    }
    for backend in _capture_tools():
        cmd = cli_forms.get(backend)
        if not cmd or not _tool(cmd[0]):
            continue
        try:
            return _run_capture(cmd)
        except Exception as exc:
            logger.debug("%s capture failed: %s", cmd[0], exc)
    raise _no_backend(
        "capture the screen", ["scrot", "ImageMagick", "gnome-screenshot"]
    )


def _wayland_screenshot(monitor: int = 0) -> bytes:
    """Capture through the backends :func:`_capture_tools` prefers here."""
    errors: List[str] = []
    tried: List[str] = []
    for backend in _capture_tools():
        if backend == "grim" and not _tool("grim"):
            continue
        if backend == "portal" and not _tool("gdbus"):
            continue
        tried.append(backend)
        try:
            if backend == "grim":
                return _grim_capture(monitor)
            return _portal_screenshot()
        except Exception as exc:
            errors.append(f"{backend}: {exc}")
            logger.debug("%s capture failed: %s", backend, exc)

    if not tried:
        raise _no_backend("capture the screen", ["grim", "gdbus"])
    hint = _missing_hint(["grim"])
    raise ComputerActionError(
        "Cannot capture the screen on Wayland (" + "; ".join(errors) + ")."
        + (f" Install grim with: {hint}" if hint else "")
        + " On GNOME or KDE the portal dialog has to be approved by the user."
    )


def _grim_capture(monitor: int = 0) -> bytes:
    """Full-output capture through grim (PNG on stdout)."""
    cmd = ["grim", "-t", "png", "-"]
    if monitor and monitor > 0:
        output = _wayland_output_name(monitor)
        if output:
            cmd = ["grim", "-o", output, "-t", "png", "-"]
    return _run_capture(cmd)


def _wayland_output_name(index: int) -> str:
    """Name of output #index for grim -o (eDP-1, DP-2, HDMI-A-1, ...)."""
    import json

    try:
        comp = _compositor()
        if comp == "sway" and _tool("swaymsg"):
            outputs = json.loads(_run(["swaymsg", "-t", "get_outputs"], timeout=8) or "[]")
            # A focused output is what "monitor 1" means to a person.
            ordered = ([o for o in outputs if o.get("focused")]
                       + [o for o in outputs if not o.get("focused")])
            if 0 <= index - 1 < len(ordered):
                return str(ordered[index - 1].get("name") or "")
        if comp == "hyprland" and _tool("hyprctl"):
            mons = json.loads(_run(["hyprctl", "monitors", "-j"], timeout=8) or "[]")
            if 0 <= index - 1 < len(mons):
                return str(mons[index - 1].get("name") or "")
        if _tool("wlr-randr"):
            names = [
                line.split()[0]
                for line in (_run(["wlr-randr"], timeout=8) or "").splitlines()
                if line and not line.startswith(" ")
            ]
            if 0 <= index - 1 < len(names):
                return names[index - 1]
    except Exception as exc:
        logger.debug("output enumeration failed: %s", exc)
    return ""


def _portal_screenshot() -> bytes:
    """XDG Desktop Portal Screenshot, driven over the session bus with gdbus.

    The portal is a two-step protocol: ``Screenshot`` returns an object path,
    then the user approves and the portal emits ``Response`` on that path with
    a ``file://`` URI. Both halves are handled here so the caller sees a plain
    "give me bytes" call — with a timeout, because a dialog nobody clicks must
    not hang the agent.
    """
    gdbus = _tool("gdbus")
    if not gdbus:
        raise ComputerActionError(
            "gdbus is not installed; the portal screenshot path needs it "
            "(sudo dnf install glib2)."
        )
    import re
    import tempfile
    import uuid

    token = "psd_ai" + uuid.uuid4().hex[:8]
    options = (
        "{'interactive': <false>, 'handle_token': <%s>, "
        "'modal': <false>}" % token
    )
    call = _run(
        [
            gdbus, "call", "--session",
            "--dest", "org.freedesktop.portal.Desktop",
            "--object-path", "/org/freedesktop/portal/desktop",
            "--method", "org.freedesktop.portal.Screenshot.Screenshot",
            "", options,
        ],
        timeout=10,
    )
    match = re.search(r"'/([^']+)'", call or "")
    if not match:
        raise ComputerActionError(f"portal did not return a request path: {call!r}")
    request_path = "/" + match.group(1)

    # Watch for the Response signal on that path. `gdbus monitor` streams every
    # signal from the destination, so filter for the path and pull the URI out.
    uri = ""
    deadline = time.time() + SCREENSHOT_TIMEOUT_SECONDS
    proc = subprocess.Popen(
        [gdbus, "monitor", "--session", "--dest", "org.freedesktop.portal.Desktop"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    try:
        while time.time() < deadline:
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                break
            if request_path not in line:
                continue
            found = re.search(r"'file://([^']+)'", line)
            if found:
                uri = found.group(1)
                break
            # A rejected request (user clicked "Deny") carries no URI.
            if ", {}" in line or "<0," in line:
                raise ComputerActionError("Screenshot permission was denied.")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    if not uri:
        raise ComputerActionError(
            f"No screenshot arrived from the portal within {SCREENSHOT_TIMEOUT_SECONDS}s "
            "(was the permission dialog approved?)"
        )
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "shot")
        _run(["gio", "copy", uri, target], timeout=10) if _tool("gio") else None
        if not os.path.exists(target):
            # No gio: the URI is a local file in nearly every implementation.
            from urllib.parse import unquote

            with open(unquote(uri), "rb") as handle:
                return handle.read()
        with open(target, "rb") as handle:
            return handle.read()


def _png_from_raw(raw: Any) -> bytes:
    """Convert an mss ScreenShot to PNG, downscaling to a sane size."""

    try:
        from PIL import Image  # type: ignore

        img = Image.frombytes("RGB", raw.size, raw.rgb)
    except Exception:
        # Pillow missing: mss can still write a PNG for us.
        import mss  # type: ignore

        buf = io.BytesIO()
        buf.write(mss.tools.to_png(raw.rgb, raw.size))
        return buf.getvalue()
    return _encode_png(img)


def _encode_png(img: Any) -> bytes:
    try:
        from PIL import Image  # type: ignore

        w, h = img.size
        longest = max(w, h)
        if longest > MAX_SCREENSHOT_EDGE:
            scale = MAX_SCREENSHOT_EDGE / float(longest)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception as exc:
        raise ComputerActionError(f"Cannot encode screenshot: {exc}")


def _run_capture(cmd: List[str]) -> bytes:
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=SCREENSHOT_TIMEOUT_SECONDS)
    except FileNotFoundError:
        raise ComputerActionError(f"{cmd[0]} not installed")
    if out.returncode != 0 or not out.stdout:
        raise ComputerActionError(
            out.stderr.decode(errors="replace").strip() or f"{cmd[0]} failed"
        )
    return out.stdout


# ---------------------------------------------------------------------------
# keyboard / mouse backends
#
# Wayland gives clients no way to inject events into another client's window,
# so there are exactly two doors:
#
#   ydotool  talks to ydotoold, which owns a virtual uinput device. That is
#            below the compositor, so it works everywhere -- GNOME included.
#            The daemon needs /dev/uinput, so it runs as root or in `input`.
#   wtype    uses the virtual-keyboard Wayland protocol: no daemon, no root,
#            and libxkbcommon gives correct Unicode. Only compositors that
#            implement that protocol accept it (wlroots, KDE -- not GNOME).
#
# On X11 it is still xdotool, which is also what pyautogui drives underneath.
# ---------------------------------------------------------------------------

# Linux input-event keycodes (see /usr/include/linux/input-event-codes.h).
# Seeded with the essentials so a minimal container still resolves keys; the
# header is parsed on top of this when kernel-headers is installed.
_LINUX_KEYCODES: Dict[str, int] = {
    "KEY_ESC": 1,
    "KEY_1": 2, "KEY_2": 3, "KEY_3": 4, "KEY_4": 5, "KEY_5": 6,
    "KEY_6": 7, "KEY_7": 8, "KEY_8": 9, "KEY_9": 10, "KEY_0": 11,
    "KEY_MINUS": 12, "KEY_EQUAL": 13, "KEY_BACKSPACE": 14, "KEY_TAB": 15,
    "KEY_Q": 16, "KEY_W": 17, "KEY_E": 18, "KEY_R": 19, "KEY_T": 20,
    "KEY_Y": 21, "KEY_U": 22, "KEY_I": 23, "KEY_O": 24, "KEY_P": 25,
    "KEY_LEFTBRACE": 26, "KEY_RIGHTBRACE": 27, "KEY_ENTER": 28,
    "KEY_LEFTCTRL": 29,
    "KEY_A": 30, "KEY_S": 31, "KEY_D": 32, "KEY_F": 33, "KEY_G": 34,
    "KEY_H": 35, "KEY_J": 36, "KEY_K": 37, "KEY_L": 38,
    "KEY_SEMICOLON": 39, "KEY_APOSTROPHE": 40, "KEY_GRAVE": 41,
    "KEY_LEFTSHIFT": 42, "KEY_BACKSLASH": 43,
    "KEY_Z": 44, "KEY_X": 45, "KEY_C": 46, "KEY_V": 47, "KEY_B": 48,
    "KEY_N": 49, "KEY_M": 50,
    "KEY_COMMA": 51, "KEY_DOT": 52, "KEY_SLASH": 53, "KEY_RIGHTSHIFT": 54,
    "KEY_LEFTALT": 56, "KEY_SPACE": 57, "KEY_CAPSLOCK": 58,
    "KEY_F1": 59, "KEY_F2": 60, "KEY_F3": 61, "KEY_F4": 62, "KEY_F5": 63,
    "KEY_F6": 64, "KEY_F7": 65, "KEY_F8": 66, "KEY_F9": 67, "KEY_F10": 68,
    "KEY_NUMLOCK": 69, "KEY_SCROLLLOCK": 70,
    "KEY_F11": 87, "KEY_F12": 88,
    "KEY_RIGHTCTRL": 97, "KEY_SYSRQ": 99, "KEY_RIGHTALT": 100,
    "KEY_HOME": 102, "KEY_UP": 103, "KEY_PAGEUP": 104, "KEY_LEFT": 105,
    "KEY_RIGHT": 106, "KEY_END": 107, "KEY_DOWN": 108, "KEY_PAGEDOWN": 109,
    "KEY_INSERT": 110, "KEY_DELETE": 111,
    "KEY_MUTE": 113, "KEY_VOLUMEDOWN": 114, "KEY_VOLUMEUP": 115,
    "KEY_PAUSE": 119, "KEY_LEFTMETA": 125, "KEY_RIGHTMETA": 126,
    "KEY_MENU": 127,
    "KEY_NEXTSONG": 163, "KEY_PLAYPAUSE": 164, "KEY_PREVIOUSSONG": 165,
    "KEY_BRIGHTNESSDOWN": 224, "KEY_BRIGHTNESSUP": 225,
}

# Human key names -> Linux KEY_* constants. Covers everything the agent and the
# policy tests ask for, plus the names macOS/Windows callers used to send.
_KEY_ALIASES: Dict[str, str] = {
    "enter": "KEY_ENTER", "return": "KEY_ENTER", "ret": "KEY_ENTER",
    "esc": "KEY_ESC", "escape": "KEY_ESC",
    "ctrl": "KEY_LEFTCTRL", "control": "KEY_LEFTCTRL",
    "alt": "KEY_LEFTALT", "option": "KEY_LEFTALT",
    "shift": "KEY_LEFTSHIFT",
    "super": "KEY_LEFTMETA", "win": "KEY_LEFTMETA", "windows": "KEY_LEFTMETA",
    "cmd": "KEY_LEFTMETA", "command": "KEY_LEFTMETA", "meta": "KEY_LEFTMETA",
    "logo": "KEY_LEFTMETA",
    "backspace": "KEY_BACKSPACE", "bksp": "KEY_BACKSPACE",
    "delete": "KEY_DELETE", "del": "KEY_DELETE",
    "insert": "KEY_INSERT", "ins": "KEY_INSERT",
    "tab": "KEY_TAB", "space": "KEY_SPACE", "spacebar": "KEY_SPACE",
    "up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT", "right": "KEY_RIGHT",
    "home": "KEY_HOME", "end": "KEY_END",
    "pageup": "KEY_PAGEUP", "pgup": "KEY_PAGEUP", "prior": "KEY_PAGEUP",
    "pagedown": "KEY_PAGEDOWN", "pgdn": "KEY_PAGEDOWN", "next": "KEY_NEXTSONG",
    "printscreen": "KEY_SYSRQ", "print": "KEY_SYSRQ", "prtsc": "KEY_SYSRQ",
    "capslock": "KEY_CAPSLOCK", "numlock": "KEY_NUMLOCK",
    "scrolllock": "KEY_SCROLLLOCK", "menu": "KEY_MENU", "apps": "KEY_MENU",
    "pause": "KEY_PAUSE", "break": "KEY_PAUSE",
    "minus": "KEY_MINUS", "dash": "KEY_MINUS", "-": "KEY_MINUS",
    "equal": "KEY_EQUAL", "equals": "KEY_EQUAL", "plus": "KEY_EQUAL",
    "=": "KEY_EQUAL",
    "[": "KEY_LEFTBRACE", "bracketleft": "KEY_LEFTBRACE",
    "leftbrace": "KEY_LEFTBRACE",
    "]": "KEY_RIGHTBRACE", "bracketright": "KEY_RIGHTBRACE",
    "rightbrace": "KEY_RIGHTBRACE",
    "\\": "KEY_BACKSLASH", "backslash": "KEY_BACKSLASH",
    ";": "KEY_SEMICOLON", "semicolon": "KEY_SEMICOLON",
    "'": "KEY_APOSTROPHE", "apostrophe": "KEY_APOSTROPHE", "quote": "KEY_APOSTROPHE",
    "`": "KEY_GRAVE", "grave": "KEY_GRAVE", "backtick": "KEY_GRAVE",
    ",": "KEY_COMMA", "comma": "KEY_COMMA",
    ".": "KEY_DOT", "period": "KEY_DOT", "dot": "KEY_DOT",
    "/": "KEY_SLASH", "slash": "KEY_SLASH",
    "volumeup": "KEY_VOLUMEUP", "volumedown": "KEY_VOLUMEDOWN",
    "mute": "KEY_MUTE", "playpause": "KEY_PLAYPAUSE",
    "prev": "KEY_PREVIOUSSONG", "previous": "KEY_PREVIOUSSONG",
    "brightnessup": "KEY_BRIGHTNESSUP", "brightnessdown": "KEY_BRIGHTNESSDOWN",
}

# Human key names -> X11 keysyms (correct case: XKeysymFromString is picky).
_X11_KEYSYMS: Dict[str, str] = {
    "enter": "Return", "return": "Return", "ret": "Return",
    "esc": "Escape", "escape": "Escape",
    "backspace": "BackSpace", "bksp": "BackSpace",
    "delete": "Delete", "del": "Delete", "insert": "Insert", "ins": "Insert",
    "tab": "Tab", "space": "space", "spacebar": "space",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "home": "Home", "end": "End",
    "pageup": "Page_Up", "pgup": "Page_Up", "prior": "Page_Up",
    "pagedown": "Page_Down", "pgdn": "Page_Down",
    "printscreen": "Print", "print": "Print", "prtsc": "Print",
    "capslock": "Caps_Lock", "numlock": "Num_Lock", "scrolllock": "Scroll_Lock",
    "menu": "Menu", "apps": "Menu", "pause": "Pause", "break": "Pause",
    "super": "Super_L", "win": "Super_L", "windows": "Super_L",
    "cmd": "Super_L", "command": "Super_L", "meta": "Super_L", "logo": "Super_L",
    "ctrl": "ctrl", "control": "ctrl", "alt": "alt", "option": "alt",
    "shift": "shift",
    "period": ".", "dot": ".", "comma": ",", "minus": "-", "dash": "-",
    "equal": "=", "slash": "/", "backslash": "\\", "semicolon": ";",
    "apostrophe": "'", "quote": "'", "grave": "`",
    "bracketleft": "[", "bracketright": "]", "leftbrace": "[", "rightbrace": "]",
    "volumeup": "XF86AudioRaiseVolume", "volumedown": "XF86AudioLowerVolume",
    "mute": "XF86AudioMute", "playpause": "XF86AudioPlay",
    "prev": "XF86AudioPrev", "previous": "XF86AudioPrev", "next": "XF86AudioNext",
    "brightnessup": "XF86MonBrightnessUp",
    "brightnessdown": "XF86MonBrightnessDown",
}

# Human key names -> wtype modifiers (wtype only accepts these seven).
_WL_MODIFIERS: Dict[str, str] = {
    "ctrl": "ctrl", "control": "ctrl", "shift": "shift", "alt": "alt",
    "option": "alt", "altgr": "altgr", "super": "logo", "win": "logo",
    "windows": "logo", "cmd": "logo", "command": "logo", "meta": "logo",
    "logo": "logo", "capslock": "capslock",
}

# ydotool click bitmask: 0x00 left, 0x01 right, 0x02 middle; 0x40 adds a press
# and 0x80 a release, so 0xC0 is a full left click.
_YDOTOOL_BUTTONS = {"left": 0x00, "right": 0x01, "middle": 0x02}
_YDOTOOL_DOWN = 0x40
_YDOTOOL_UP = 0x80

_KEYCODE_HEADER = "/usr/include/linux/input-event-codes.h"
_HEADER_LOADED = False
_MOUSEMOVE_FORM: Optional[str] = None


def _pg():
    """pyautogui, but only where it can actually work.

    pyautogui talks to X. On Wayland that means XWayland, where it would move a
    cursor that no Wayland client ever sees -- worse than failing, because it
    reports success. So it is an X11-only backend.
    """
    if _is_wayland():
        return None
    return _opt("pyautogui")


def _load_keycodes_from_header() -> None:
    """Extend the keycode table from kernel-headers if they are installed."""
    global _HEADER_LOADED
    if _HEADER_LOADED:
        return
    _HEADER_LOADED = True
    import re

    try:
        with open(_KEYCODE_HEADER, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                match = re.match(r"#define\s+(KEY_[A-Z0-9_]+)\s+(\d+)", line)
                if match:
                    _LINUX_KEYCODES.setdefault(match.group(1), int(match.group(2)))
    except OSError:
        pass


def _linux_keycode(key: str) -> int:
    """Linux input keycode for a human key name ("ctrl", "enter", "f4", "a")."""
    name = str(key).strip().lower()
    if not name:
        raise ComputerActionError("Empty key name")
    _load_keycodes_from_header()
    constant = _KEY_ALIASES.get(name)
    if constant is None:
        if len(name) == 1 and (name.isalnum()):
            constant = f"KEY_{name.upper()}"
        elif name.startswith("f") and name[1:].isdigit():
            constant = f"KEY_F{name[1:]}"
        else:
            constant = f"KEY_{name.upper().replace(' ', '_')}"
    if constant in _LINUX_KEYCODES:
        return int(_LINUX_KEYCODES[constant])
    raise ComputerActionError(f"Unknown key '{key}' (no {constant} keycode)")


def _x11_keysym(key: str) -> str:
    """X keysym for a human key name."""
    name = str(key).strip().lower()
    if name in _X11_KEYSYMS:
        return _X11_KEYSYMS[name]
    if len(name) == 1:
        return name
    if name.startswith("f") and name[1:].isdigit():
        return f"F{name[1:]}"
    return name.capitalize() if name else name


def _wl_keysym(key: str) -> str:
    """libxkbcommon key name for wtype ("Left", "Return", "a", "F4")."""
    name = str(key).strip().lower()
    table = {
        "enter": "Return", "return": "Return", "ret": "Return",
        "esc": "Escape", "escape": "Escape", "backspace": "BackSpace",
        "delete": "Delete", "insert": "Insert", "tab": "Tab",
        "space": "space", "spacebar": "space", "up": "Up", "down": "Down",
        "left": "Left", "right": "Right", "home": "Home", "end": "End",
        "pageup": "Page_Up", "pgup": "Page_Up", "pagedown": "Page_Down",
        "pgdn": "Page_Down", "printscreen": "Print", "print": "Print",
        "capslock": "Caps_Lock", "numlock": "Num_Lock", "menu": "Menu",
        "pause": "Pause", "minus": "minus", "equal": "equal",
        "period": "period", "comma": "comma", "slash": "slash",
    }
    if name in table:
        return table[name]
    if name.startswith("f") and name[1:].isdigit():
        return f"F{name[1:]}"
    return name


def _ydotool(args: List[str], stdin_text: Optional[str] = None,
             timeout: int = 15) -> str:
    """Run ydotool, with an actionable error when the daemon is not there."""
    if not _tool("ydotool"):
        raise _no_backend("inject input", ["ydotool", "wtype"])
    try:
        return _run(["ydotool", *args], timeout=timeout, stdin_text=stdin_text)
    except ComputerActionError as exc:
        message = str(exc)
        if "socket" in message.lower() or "connect" in message.lower():
            raise ComputerActionError(
                "ydotool cannot reach ydotoold. Start it with: "
                "sudo dnf install ydotool && "
                "sudo systemctl enable --now ydotoold"
                " (the daemon needs /dev/uinput)."
            ) from exc
        raise


def _distro_name() -> str:
    """`Fedora Linux 42 (Workstation Edition)` — from os-release."""
    from core.platform_compat import distro_pretty_name

    try:
        return distro_pretty_name()
    except Exception:
        return f"{platform.system()} {platform.release()}"


def _ydotool_socket() -> Optional[str]:
    """The ydotoold socket ydotool would use, if it exists."""
    explicit = os.environ.get("YDOTOOL_SOCKET")
    if explicit:
        return explicit if os.path.exists(explicit) else None
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    for candidate in (
        os.path.join(runtime, ".ydotool_socket") if runtime else "",
        "/run/.ydotool_socket",
        "/tmp/.ydotool_socket",
    ):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _wayland_move(x: int, y: int) -> None:
    """Absolute pointer move.

    ydotool's mousemove syntax differs between releases (`--absolute X Y` in
    the current man page, `--absolute -x X -y Y` in some builds), so the first
    working form is remembered and reused.
    """
    global _MOUSEMOVE_FORM
    x, y = int(x), int(y)
    forms = {
        "positional": ["mousemove", "--absolute", str(x), str(y)],
        "flagged": ["mousemove", "--absolute", "-x", str(x), "-y", str(y)],
    }
    order = ([_MOUSEMOVE_FORM, "positional", "flagged"] if _MOUSEMOVE_FORM
             else ["positional", "flagged"])
    seen: set = set()
    errors: List[str] = []
    for form in order:
        if form in seen:
            continue
        seen.add(form)
        try:
            _ydotool(forms[form])
            _MOUSEMOVE_FORM = form
            return
        except ComputerActionError as exc:
            errors.append(str(exc))
    raise ComputerActionError(f"Cannot move the pointer: {'; '.join(errors)}")


def _wayland_click(x: Optional[int], y: Optional[int], button: str,
                   clicks: int) -> None:
    if x is not None and y is not None:
        _wayland_move(x, y)
    base = _YDOTOOL_BUTTONS.get(str(button).strip().lower(), 0x00)
    bitmask = f"0x{base | _YDOTOOL_DOWN | _YDOTOOL_UP:02X}"
    clicks = max(1, int(clicks))
    args = ["click"]
    if clicks > 1:
        args += ["--repeat", str(clicks), "--next-delay", "25"]
    args.append(bitmask)
    _ydotool(args)


def _wayland_button_down(button: str) -> None:
    base = _YDOTOOL_BUTTONS.get(str(button).strip().lower(), 0x00)
    _ydotool(["click", f"0x{base | _YDOTOOL_DOWN:02X}"])


def _wayland_button_up(button: str) -> None:
    base = _YDOTOOL_BUTTONS.get(str(button).strip().lower(), 0x00)
    _ydotool(["click", f"0x{base | _YDOTOOL_UP:02X}"])


def _wayland_drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.4) -> None:
    _wayland_move(x1, y1)
    _wayland_button_down("left")
    # A drag the compositor cannot see unfolding is dropped, so walk the
    # pointer to the destination in steps instead of teleporting.
    duration = max(0.05, float(duration))
    steps = max(1, min(20, int(duration / 0.04)))
    for step in range(1, steps + 1):
        ratio = step / steps
        _wayland_move(int(x1 + (x2 - x1) * ratio), int(y1 + (y2 - y1) * ratio))
        time.sleep(duration / steps)
    _wayland_button_up("left")


def _wayland_press_key(name: str) -> None:
    """Single key press on Wayland: ydotool keycodes first, wtype second."""
    if _tool("ydotool"):
        try:
            code = _linux_keycode(name)
            _ydotool(["key", f"{code}:1", f"{code}:0"])
            return
        except ComputerActionError as exc:
            logger.debug("ydotool key %s failed: %s", name, exc)
            if _tool("wtype"):
                _run(["wtype", "-k", _wl_keysym(name)])
                return
            raise
    if _tool("wtype"):
        _run(["wtype", "-k", _wl_keysym(name)])
        return
    raise _no_backend("press a key", ["ydotool", "wtype"])


def _wayland_hotkey(keys: List[str]) -> None:
    """Chord, e.g. ["ctrl", "shift", "esc"]. Modifiers down, key, modifiers up."""
    if _tool("ydotool"):
        try:
            codes = [_linux_keycode(k) for k in keys]
            sequence: List[str] = []
            sequence += [f"{c}:1" for c in codes]
            sequence += [f"{c}:0" for c in reversed(codes)]
            _ydotool(["key", *sequence])
            return
        except ComputerActionError as exc:
            logger.debug("ydotool chord failed: %s", exc)
            if not _tool("wtype"):
                raise
    if _tool("wtype"):
        # wtype releases modifiers itself when it exits, but release them in
        # reverse order anyway so a partially typed chord does not stick.
        mods = [k for k in keys if k in _WL_MODIFIERS]
        rest = [k for k in keys if k not in _WL_MODIFIERS]
        cmd: List[str] = ["wtype"]
        cmd += sum([["-M", _WL_MODIFIERS[m]] for m in mods], [])
        cmd += sum([["-k", _wl_keysym(r)] for r in rest], [])
        cmd += sum([["-m", _WL_MODIFIERS[m]] for m in reversed(mods)], [])
        _run(cmd)
        return
    raise _no_backend("press a key combination", ["ydotool", "wtype"])


def _wayland_type(text: str) -> None:
    """Type a string on Wayland.

    wtype is preferred for anything non-ASCII: it goes through libxkbcommon, so
    a Devanagari string arrives as Devanagari. ydotool's `type` walks a US
    keycode table, which is fine for ASCII and wrong for most other scripts.
    """
    ascii_only = all(ord(ch) < 128 for ch in text)
    order = ["ydotool", "wtype"] if ascii_only else ["wtype", "ydotool"]
    errors: List[str] = []
    for backend in order:
        if not _tool(backend):
            continue
        try:
            if backend == "wtype":
                # `--` keeps text that starts with a dash from being read as an
                # option; stdin would also work but `--` needs no pipe handling.
                _run(["wtype", "--", text])
                return
            try:
                _ydotool(["type", "-d", "8", "-f", "-"], stdin_text=text)
                return
            except ComputerActionError as exc:
                # Older ydotool builds have no -f/--file: fall back to argv.
                logger.debug("ydotool type -f failed: %s", exc)
                _ydotool(["type", "-d", "8", "--", text] if "--" in str(exc)
                         else ["type", "-d", "8", text])
                return
        except ComputerActionError as exc:
            errors.append(f"{backend}: {exc}")
            logger.debug("wayland typing via %s failed: %s", backend, exc)
    raise _no_backend("type text", ["ydotool", "wtype"])


def _wayland_scroll(amount: int) -> None:
    """Scroll on Wayland.

    Nothing in a stock Fedora Wayland stack can inject a wheel event: ydotool
    exposes key/mousemove/click/type but no wheel, and the wheel is a relative
    event the compositor keeps to itself. Arrow and Page keys are the portable
    stand-in -- every toolkit maps them to scrolling -- so a notch becomes an
    arrow press and a long scroll becomes page moves.
    """
    steps = min(abs(int(amount)), 40)
    if steps == 0:
        return
    if steps >= 8:
        key = "pageup" if amount > 0 else "pagedown"
        repeats = max(1, steps // 8)
    else:
        key = "up" if amount > 0 else "down"
        repeats = steps
    for _ in range(repeats):
        _wayland_press_key(key)
        time.sleep(0.02)


def _move(x: int, y: int) -> None:
    pg = _pg()
    if pg:
        try:
            pg.moveTo(int(x), int(y))
            return
        except Exception as exc:
            logger.debug("pyautogui move failed: %s", exc)
    if _is_wayland():
        _wayland_move(int(x), int(y))
        return
    if _tool("xdotool"):
        _xdotool(["mousemove", str(int(x)), str(int(y))])
        return
    raise _no_backend("move the pointer", ["xdotool"])


def _click(x: Optional[int], y: Optional[int], button: str, clicks: int) -> None:
    pg = _pg()
    if pg:
        try:
            if x is not None and y is not None:
                pg.click(x=int(x), y=int(y), button=button, clicks=max(1, int(clicks)))
            else:
                pg.click(button=button, clicks=max(1, int(clicks)))
            return
        except Exception as exc:
            logger.debug("pyautogui click failed: %s", exc)
    if _is_wayland():
        _wayland_click(x, y, button, clicks)
        return
    if _tool("xdotool"):
        if x is not None and y is not None:
            _xdotool(["mousemove", str(int(x)), str(int(y))])
        btn = {"left": "1", "middle": "2", "right": "3"}.get(button, "1")
        for _ in range(max(1, int(clicks))):
            _xdotool(["click", btn])
        return
    raise _no_backend("click", ["xdotool"])


def _drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.4) -> None:
    pg = _pg()
    if pg:
        try:
            pg.moveTo(int(x1), int(y1))
            pg.dragTo(int(x2), int(y2), duration=max(0.05, float(duration)))
            return
        except Exception as exc:
            logger.debug("pyautogui drag failed: %s", exc)
    if _is_wayland():
        _wayland_drag(int(x1), int(y1), int(x2), int(y2), duration)
        return
    if _tool("xdotool"):
        _xdotool(["mousemove", str(int(x1)), str(int(y1)), "mousedown", "1",
                  "mousemove", str(int(x2)), str(int(y2)), "mouseup", "1"])
        return
    raise _no_backend("drag", ["xdotool"])


def _scroll(amount: int) -> None:
    """Positive scrolls up, negative scrolls down."""

    pg = _pg()
    if pg:
        try:
            pg.scroll(int(amount))
            return
        except Exception as exc:
            logger.debug("pyautogui scroll failed: %s", exc)
    if _is_wayland():
        _wayland_scroll(int(amount))
        return
    if _tool("xdotool"):
        btn = "4" if int(amount) > 0 else "5"
        for _ in range(min(abs(int(amount)), 40)):
            _xdotool(["click", btn])
        return
    raise _no_backend("scroll", ["xdotool"])


def _type_text(text: str) -> None:
    text = str(text or "")
    if not text:
        return
    if len(text) > MAX_TYPED_CHARS:
        raise ComputerActionError(
            f"Refusing to type {len(text)} characters (limit {MAX_TYPED_CHARS})."
        )
    if _is_wayland():
        _wayland_type(text)
        return
    pg = _pg()
    if pg:
        try:
            pg.write(text, interval=0.01)
            return
        except Exception as exc:
            logger.debug("pyautogui type failed: %s", exc)
    if _tool("xdotool"):
        _run(["xdotool", "type", "--clearmodifiers", "--", text])
        return
    raise _no_backend("type text", ["xdotool"])


def _hotkey(keys: List[str]) -> None:
    """Press a chord, e.g. ["ctrl", "shift", "esc"]."""

    keys = [str(k).strip().lower() for k in keys if str(k).strip()]
    if not keys:
        raise ComputerActionError("No keys supplied")
    if _is_wayland():
        _wayland_hotkey(keys)
        return
    pg = _pg()
    if pg:
        try:
            pg.hotkey(*[k for k in keys])
            return
        except Exception as exc:
            logger.debug("pyautogui hotkey failed: %s", exc)
    if _tool("xdotool"):
        _run(["xdotool", "key", "--clearmodifiers",
              "+".join(_x11_keysym(k) for k in keys)])
        return
    raise _no_backend("press a key combination", ["xdotool"])


def _press(key: str) -> None:
    if _is_wayland():
        _wayland_press_key(str(key))
        return
    pg = _pg()
    if pg:
        try:
            pg.press(key)
            return
        except Exception as exc:
            logger.debug("pyautogui press failed: %s", exc)
    _hotkey([str(key)])


def _xdotool(args: List[str]) -> None:
    """X11 input helper. Raises with a dnf hint if xdotool is missing."""
    if not _tool("xdotool"):
        raise _no_backend("inject input on X11", ["xdotool"])
    _run(["xdotool", *args])


def _run(cmd: List[str], timeout: int = 15,
         stdin_text: Optional[str] = None) -> str:
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            input=None if stdin_text is None else stdin_text.encode("utf-8"),
        )
    except FileNotFoundError:
        raise ComputerActionError(f"{cmd[0]} is not installed")
    if out.returncode != 0:
        raise ComputerActionError(
            out.stderr.decode(errors="replace").strip() or f"{cmd[0]} failed"
        )
    return out.stdout.decode(errors="replace")


# ---------------------------------------------------------------------------
# windows
#
# There is no portable Wayland window list, and that is deliberate: a client is
# not supposed to be able to enumerate or raise somebody else's windows. Sway
# and Hyprland expose their own trees over an IPC socket, so listing works
# there; GNOME and KDE do not, so those paths answer with an empty list plus an
# explanation rather than pretending. On X11, wmctrl/xprop see everything.
# ---------------------------------------------------------------------------


def _windows() -> List[Dict[str, Any]]:
    if _is_wayland():
        comp = _compositor()
        if comp == "sway":
            return _sway_windows()
        if comp == "hyprland":
            return _hyprland_windows()
        logger.debug(
            "No window listing on %s Wayland: the compositor keeps its tree private",
            comp or "this",
        )
        return []

    gw = _opt("pygetwindow")
    if gw:
        try:
            out: List[Dict[str, Any]] = []
            for w in gw.getAllWindows():
                try:
                    title = w.title or ""
                    if not title.strip():
                        continue
                    out.append(
                        {
                            "title": title,
                            "visible": bool(getattr(w, "visible", True)),
                            "left": int(getattr(w, "left", 0) or 0),
                            "top": int(getattr(w, "top", 0) or 0),
                            "width": int(getattr(w, "width", 0) or 0),
                            "height": int(getattr(w, "height", 0) or 0),
                        }
                    )
                except Exception:
                    continue
            return out[:80]
        except Exception as exc:
            logger.debug("pygetwindow listing failed: %s", exc)

    if _tool("wmctrl"):
        try:
            raw = _run(["wmctrl", "-lG"])
        except ComputerActionError as exc:
            logger.debug("wmctrl listing failed: %s", exc)
            return []
        out = []
        for line in raw.splitlines():
            # 0x03200003  <desktop>  <x> <y> <w> <h>  <host>  <title with spaces>
            parts = line.split(None, 7)
            if len(parts) >= 8:
                out.append({"title": parts[7], "left": int(parts[2] or 0),
                            "top": int(parts[3] or 0), "width": int(parts[4] or 0),
                            "height": int(parts[5] or 0), "visible": True})
        return out[:80]
    if _tool("xdotool"):
        # No wmctrl: ask X for every mapped window and read its name.
        out = []
        try:
            ids = _run(["xdotool", "search", "--onlyvisible", "--name", ""],
                       timeout=10).split()
        except ComputerActionError as exc:
            logger.debug("xdotool listing failed: %s", exc)
            return []
        for wid in ids[:80]:
            try:
                title = _run(["xdotool", "getwindowname", wid], timeout=5).strip()
            except ComputerActionError:
                continue
            if title:
                out.append({"title": title, "visible": True, "left": 0, "top": 0,
                            "width": 0, "height": 0})
        return out[:80]
    return []


def _sway_windows() -> List[Dict[str, Any]]:
    """Walk `swaymsg -t get_tree` for real, titled containers."""
    import json

    if not _tool("swaymsg"):
        return []
    try:
        raw = _run(["swaymsg", "-t", "get_tree"], timeout=10)
        tree = json.loads(raw)
    except (ComputerActionError, ValueError) as exc:
        logger.debug("sway tree failed: %s", exc)
        return []

    out: List[Dict[str, Any]] = []

    def walk(node: Dict[str, Any]) -> None:
        if not isinstance(node, dict):
            return
        title = (node.get("name") or "").strip()
        if title and node.get("type") in ("con", "floating_con"):
            rect = node.get("rect") or {}
            props = node.get("window_properties") or {}
            out.append({
                "title": title,
                "visible": bool(node.get("visible", True)),
                "focused": bool(node.get("focused", False)),
                "app_id": node.get("app_id") or props.get("class") or "",
                "left": int(rect.get("x") or 0),
                "top": int(rect.get("y") or 0),
                "width": int(rect.get("width") or 0),
                "height": int(rect.get("height") or 0),
            })
        for child in node.get("nodes") or []:
            walk(child)
        for child in node.get("floating_nodes") or []:
            walk(child)

    walk(tree)
    return out[:80]


def _hyprland_windows() -> List[Dict[str, Any]]:
    """`hyprctl clients -j` is a flat list already."""
    import json

    if not _tool("hyprctl"):
        return []
    try:
        raw = _run(["hyprctl", "clients", "-j"], timeout=10)
        clients = json.loads(raw)
    except (ComputerActionError, ValueError) as exc:
        logger.debug("hyprctl clients failed: %s", exc)
        return []

    out: List[Dict[str, Any]] = []
    for client in clients if isinstance(clients, list) else []:
        title = (client.get("title") or "").strip()
        if not title:
            continue
        at = client.get("at") or [0, 0]
        size = client.get("size") or [0, 0]
        out.append({
            "title": title,
            "visible": not client.get("hidden", False),
            "focused": bool(client.get("focusHistoryID", 1) == 0),
            "app_id": client.get("class") or "",
            "workspace": (client.get("workspace") or {}).get("name", ""),
            "left": int(at[0]), "top": int(at[1]),
            "width": int(size[0]), "height": int(size[1]),
        })
    return out[:80]


def _sway_criteria(title: str) -> str:
    """Sway window criteria matching a title substring (regex-escaped)."""
    import re

    return f'[title~"{re.escape(title)}"]'


def _focus(title: str) -> str:
    title = str(title or "").strip()
    if not title:
        raise ComputerActionError("No window title supplied")

    if _is_wayland():
        comp = _compositor()
        if comp == "sway" and _tool("swaymsg"):
            _run(["swaymsg", "-t", "command", f"{_sway_criteria(title)} focus"],
                 timeout=10)
            return f"Focused window matching '{title}'"
        if comp == "hyprland" and _tool("hyprctl"):
            _run(["hyprctl", "dispatch", "focuswindow", f"title:{title}"], timeout=10)
            return f"Focused window matching '{title}'"
        raise ComputerActionError(
            f"Window focus is not available on {comp or 'this'} Wayland session: "
            "the compositor does not let clients raise other clients' windows. "
            "Sway and Hyprland do (swaymsg/hyprctl); on GNOME or KDE switch "
            "windows manually, or run an X11 session."
        )

    pgw = _opt("pygetwindow")
    if pgw:
        try:
            wins = pgw.getWindowsWithTitle(title)
            if wins:
                w = wins[0]
                try:
                    w.activate()
                except Exception:
                    w.minimize()
                    w.restore()
                return f"Focused window matching '{title}'"
        except Exception as exc:
            logger.debug("pygetwindow focus failed: %s", exc)
    if _tool("wmctrl"):
        _run(["wmctrl", "-a", title])
        return f"Focused window matching '{title}'"
    if _tool("xdotool"):
        try:
            wid = _run(["xdotool", "search", "--name", title], timeout=10).split()
        except ComputerActionError as exc:
            raise ComputerActionError(f"No window matching '{title}': {exc}") from exc
        if not wid:
            raise ComputerActionError(f"No visible window matching '{title}'")
        _run(["xdotool", "windowactivate", "--sync", wid[0]], timeout=10)
        return f"Focused window matching '{title}'"
    raise _no_backend("focus a window", ["wmctrl", "xdotool"])


def _close_window(title: str) -> str:
    title = str(title or "").strip()
    if not title:
        raise ComputerActionError("No window title supplied")

    if _is_wayland():
        comp = _compositor()
        if comp == "sway" and _tool("swaymsg"):
            _run(["swaymsg", "-t", "command", f"{_sway_criteria(title)} close"],
                 timeout=10)
            return f"Asked window matching '{title}' to close"
        if comp == "hyprland" and _tool("hyprctl"):
            _run(["hyprctl", "dispatch", "closewindow", f"title:{title}"], timeout=10)
            return f"Asked window matching '{title}' to close"
        raise ComputerActionError(
            f"Closing windows is not available on {comp or 'this'} Wayland session "
            "(no compositor IPC). Sway and Hyprland support it; on GNOME or KDE "
            "close the window manually, or kill the owning process instead."
        )

    pgw = _opt("pygetwindow")
    if pgw:
        try:
            wins = pgw.getWindowsWithTitle(title)
            if wins:
                wins[0].close()
                return f"Closed window matching '{title}'"
        except Exception as exc:
            logger.debug("pygetwindow close failed: %s", exc)
    if _tool("wmctrl"):
        _run(["wmctrl", "-c", title])
        return f"Closed window matching '{title}'"
    if _tool("xdotool"):
        try:
            wid = _run(["xdotool", "search", "--name", title], timeout=10).split()
        except ComputerActionError as exc:
            raise ComputerActionError(f"No window matching '{title}': {exc}") from exc
        if not wid:
            raise ComputerActionError(f"No visible window matching '{title}'")
        # windowclose sends WM_DELETE_WINDOW, the polite equivalent of Alt+F4.
        _run(["xdotool", "windowclose", wid[0]], timeout=10)
        return f"Closed window matching '{title}'"
    raise _no_backend("close a window", ["wmctrl", "xdotool"])


# ---------------------------------------------------------------------------
# clipboard
#
# Wayland has no shared clipboard that any client can read on demand: the
# owning client serves it. wl-clipboard handles both sides -- `wl-copy` forks a
# small daemon that keeps the data alive after we exit, and `wl-paste` reads
# whatever the current owner offers.
# ---------------------------------------------------------------------------


def _clipboard_get() -> str:
    if _is_wayland():
        if _tool("wl-paste"):
            try:
                return _run(["wl-paste", "--no-newline"], timeout=10)
            except ComputerActionError as exc:
                logger.debug("wl-paste failed: %s", exc)
        else:
            hint = _missing_hint(["wl-clipboard"])
            raise ComputerActionError(
                "Cannot read the clipboard on Wayland: wl-paste is not installed."
                + (f" Install it with: {hint}" if hint else "")
            )
    pc = _opt("pyperclip")
    if pc:
        try:
            return str(pc.paste())
        except Exception as exc:
            logger.debug("pyperclip paste failed: %s", exc)
    for cmd in (["xclip", "-selection", "clipboard", "-o"],
                ["xsel", "--clipboard", "--output"]):
        if _tool(cmd[0]):
            return _run(cmd)
    raise ComputerActionError(
        "Cannot read the clipboard: no backend available. "
        + (_missing_hint(["xclip"]) or "Install `pyperclip` or xclip.")
    )


def _clipboard_set(text: str) -> None:
    text = str(text)
    if _is_wayland():
        if _tool("wl-copy"):
            try:
                subprocess.run(["wl-copy", "--", text], timeout=10, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                return
            except (subprocess.SubprocessError, OSError) as exc:
                logger.debug("wl-copy failed: %s", exc)
                raise ComputerActionError(f"Clipboard write failed: {exc}") from exc
        hint = _missing_hint(["wl-clipboard"])
        raise ComputerActionError(
            "Cannot set the clipboard on Wayland: wl-copy is not installed."
            + (f" Install it with: {hint}" if hint else "")
        )
    pc = _opt("pyperclip")
    if pc:
        try:
            pc.copy(text)
            return
        except Exception as exc:
            logger.debug("pyperclip copy failed: %s", exc)
    if _tool("xclip"):
        subprocess.run(
            ["xclip", "-selection", "clipboard"],
            input=text.encode("utf-8"), timeout=15, check=False,
        )
        return
    if _tool("xsel"):
        subprocess.run(
            ["xsel", "--clipboard", "--input"],
            input=text.encode("utf-8"), timeout=15, check=False,
        )
        return
    raise ComputerActionError(
        "Cannot set the clipboard: no backend available. "
        + (_missing_hint(["xclip"]) or "Install `pyperclip` or xclip.")
    )


# ---------------------------------------------------------------------------
# launch / processes / system
# ---------------------------------------------------------------------------


# Aliases -> candidate launchers, in the order a Fedora box is likely to have
# them. Names people actually say ("notepad", "task manager") map onto the
# GNOME/KDE equivalents, because that is what is installed.
_KNOWN_APPS: Dict[str, List[str]] = {
    "terminal": ["gnome-terminal", "org.gnome.Console", "konsole", "kitty",
                 "alacritty", "xfce4-terminal", "xterm"],
    "console": ["org.gnome.Console", "gnome-console", "gnome-terminal"],
    "shell": ["gnome-terminal", "org.gnome.Console", "konsole"],
    "files": ["nautilus", "org.gnome.Nautilus", "dolphin", "thunar", "pcmanfm"],
    "file explorer": ["nautilus", "org.gnome.Nautilus", "dolphin", "thunar"],
    "explorer": ["nautilus", "org.gnome.Nautilus", "dolphin"],
    "file manager": ["nautilus", "org.gnome.Nautilus", "dolphin", "thunar"],
    "calculator": ["gnome-calculator", "kcalc", "galculator"],
    "calc": ["gnome-calculator", "kcalc"],
    "notepad": ["org.gnome.TextEditor", "gnome-text-editor", "gedit", "kate",
                "mousepad", "xed"],
    "text editor": ["org.gnome.TextEditor", "gnome-text-editor", "gedit", "kate"],
    "editor": ["org.gnome.TextEditor", "gnome-text-editor", "gedit", "code"],
    "settings": ["gnome-control-center", "org.gnome.Settings",
                 "systemsettings", "xfce4-settings-manager"],
    "control panel": ["gnome-control-center", "systemsettings"],
    "task manager": ["gnome-system-monitor", "org.gnome.SystemMonitor",
                     "plasma-systemmonitor", "ksysguard", "htop"],
    "system monitor": ["gnome-system-monitor", "org.gnome.SystemMonitor",
                       "plasma-systemmonitor"],
    "software": ["gnome-software", "org.gnome.Software", "plasma-discover",
                 "org.kde.discover"],
    "store": ["gnome-software", "org.gnome.Software", "plasma-discover"],
    "browser": ["firefox", "org.mozilla.firefox", "google-chrome", "chromium"],
    "firefox": ["firefox", "org.mozilla.firefox"],
    "chrome": ["google-chrome", "chromium-browser", "chromium"],
    "chromium": ["chromium-browser", "chromium"],
    "edge": ["microsoft-edge", "microsoft-edge-stable"],
    "photos": ["loupe", "org.gnome.Loupe", "eog", "org.gnome.eog", "gwenview"],
    "image viewer": ["loupe", "org.gnome.Loupe", "eog", "gwenview"],
    "paint": ["org.gnome.Drawing", "drawing", "pinta", "kolourpaint", "gimp",
              "krita"],
    "drawing": ["org.gnome.Drawing", "drawing", "kolourpaint"],
    "videos": ["totem", "org.gnome.Totem", "mpv", "vlc", "org.gnome.Showtime"],
    "video player": ["mpv", "vlc", "totem"],
    "music": ["rhythmbox", "elisa", "lollypop", "org.gnome.Music"],
    "media player": ["mpv", "vlc", "totem"],
    "pdf": ["evince", "org.gnome.Evince", "okular", "org.kde.okular"],
    "document viewer": ["evince", "org.gnome.Evince", "okular"],
    "code": ["code", "codium", "vscodium"],
    "vscode": ["code", "codium", "vscodium"],
    "vs code": ["code", "codium", "vscodium"],
    "spotify": ["spotify", "com.spotify.Client"],
    "discord": ["discord", "com.discordapp.Discord"],
    "telegram": ["telegram-desktop", "org.telegram.desktop"],
    "clock": ["gnome-clocks", "org.gnome.clocks"],
    "calendar": ["gnome-calendar", "org.gnome.Calendar"],
    "weather": ["gnome-weather", "org.gnome.Weather"],
    "maps": ["gnome-maps", "org.gnome.Maps"],
    "disks": ["gnome-disks", "org.gnome.DiskUtility"],
    "logs": ["gnome-logs", "org.gnome.Logs"],
    "fonts": ["gnome-font-viewer", "org.gnome.FontViewer"],
    "characters": ["gnome-characters", "org.gnome.Characters"],
    "help": ["yelp", "org.gnome.Yelp"],
    "libreoffice": ["libreoffice", "org.libreoffice.LibreOffice", "soffice"],
    "writer": ["libreoffice-writer", "libreoffice", "soffice"],
    "spreadsheet": ["libreoffice-calc", "libreoffice", "soffice", "gnumeric"],
    "impress": ["libreoffice-impress", "libreoffice", "soffice"],
    "email": ["thunderbird", "org.mozilla.Thunderbird", "geary", "evolution"],
    "thunderbird": ["thunderbird", "org.mozilla.Thunderbird"],
    "camera": ["snapshot", "org.gnome.Snapshot", "cheese", "org.gnome.Cheese",
               "guvcview"],
    "vlc": ["vlc"],
    "mpv": ["mpv"],
    "steam": ["steam", "com.valvesoftware.Steam"],
}

_DESKTOP_DIRS = (
    "/usr/share/applications",
    "/usr/local/share/applications",
    os.path.expanduser("~/.local/share/applications"),
    "/var/lib/flatpak/exports/share/applications",
    os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
)


def _desktop_entry(name: str) -> Optional[str]:
    """Path to `<name>.desktop`, searching the XDG + Flatpak export dirs."""
    for directory in _DESKTOP_DIRS:
        candidate = os.path.join(directory, f"{name}.desktop")
        if os.path.isfile(candidate):
            return candidate
    return None


def _launch_candidate(candidate: str) -> Optional[str]:
    """Launch one candidate app. Returns what was launched, or None."""
    binary = _tool(candidate)
    if binary:
        from core.platform_compat import detached_popen_kwargs

        subprocess.Popen([binary], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **detached_popen_kwargs())
        return candidate

    entry = _desktop_entry(candidate)
    if entry and _tool("gtk-launch"):
        try:
            _run(["gtk-launch", os.path.basename(entry)[:-len(".desktop")]],
                 timeout=10)
            return candidate
        except ComputerActionError as exc:
            logger.debug("gtk-launch %s failed: %s", candidate, exc)
    if entry and _tool("gio"):
        try:
            _run(["gio", "launch", entry], timeout=10)
            return candidate
        except ComputerActionError as exc:
            logger.debug("gio launch %s failed: %s", candidate, exc)
    if entry:
        from core.platform_compat import detached_popen_kwargs

        subprocess.Popen(["xdg-open", entry], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **detached_popen_kwargs())
        return candidate
    return None


def _open_target(target: str) -> str:
    """Open an app, URL or file the way a Fedora desktop would.

    Everything routes through the freedesktop stack: `xdg-open` for URLs and
    files (it resolves the MIME type to the right handler), `gtk-launch` for
    desktop entries, and a plain fork/exec for binaries on PATH. No shell is
    involved, so nothing here is subject to word splitting.
    """
    from core.platform_compat import detached_popen_kwargs

    target = str(target or "").strip()
    if not target:
        raise ComputerActionError("Nothing to open")

    key = target.lower()
    if key in _KNOWN_APPS:
        tried = _KNOWN_APPS[key]
        for candidate in tried:
            launched = _launch_candidate(candidate)
            if launched:
                return f"Opened {launched}"
        # App ids ("org.gnome.TextEditor") are not package names, so the dnf
        # hint uses the first candidate that looks like a binary.
        package = next((c for c in tried if "." not in c), tried[0])
        raise ComputerActionError(
            f"None of the {key} apps are installed (looked for "
            f"{', '.join(tried[:4])}). On Fedora: sudo dnf install {package}"
        )

    if key.startswith(("http://", "https://", "mailto:", "ftp://", "file://")):
        for opener in ("xdg-open", "gio", "gvfs-open"):
            if _tool(opener):
                if opener == "gio":
                    _run([opener, "open", target], timeout=20)
                else:
                    subprocess.Popen([opener, target], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     **detached_popen_kwargs())
                return f"Opened {target}"
        raise ComputerActionError(
            "No URL handler available. " + (_missing_hint(["xdg-utils"]) or "")
        )

    expanded = os.path.expanduser(target)
    if os.path.exists(expanded):
        if _tool("xdg-open"):
            subprocess.Popen(["xdg-open", os.path.abspath(expanded)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             **detached_popen_kwargs())
            return f"Opened {target}"
        if _tool("gio"):
            _run(["gio", "open", os.path.abspath(expanded)], timeout=20)
            return f"Opened {target}"
        raise ComputerActionError(
            "Nothing can open that path. " + (_missing_hint(["xdg-utils"]) or "")
        )

    # A desktop entry name, with or without the .desktop suffix.
    stem = target[:-len(".desktop")] if target.endswith(".desktop") else target
    if _desktop_entry(stem):
        launched = _launch_candidate(stem)
        if launched:
            return f"Opened {launched}"

    binary = _tool(target)
    if binary:
        # Exec the resolved path: a bare name would be re-resolved by the
        # kernel against a PATH that may not be ours.
        subprocess.Popen([binary], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **detached_popen_kwargs())
        return f"Launched {target}"

    # Last idea: let xdg-open resolve it (handles bare names some setups have).
    if _tool("xdg-open"):
        try:
            subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, **detached_popen_kwargs())
            return f"Asked xdg-open to handle {target}"
        except OSError as exc:
            raise ComputerActionError(f"Could not open '{target}': {exc}") from exc

    raise ComputerActionError(
        f"'{target}' is not an installed app, URL or file. Try "
        "`dnf search " + stem + "` or check the name in /usr/share/applications."
    )


def _proc_rows() -> List[Dict[str, Any]]:
    """pid/name/memory_mb straight out of /proc — no psutil needed."""
    rows: List[Dict[str, Any]] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        name = ""
        rss_kb = 0
        try:
            with open(f"/proc/{pid}/stat", "rb") as fh:
                stat = fh.read().decode("utf-8", "replace")
            name = stat[stat.index("(") + 1:stat.rindex(")")]
        except (OSError, ValueError):
            continue
        try:
            with open(f"/proc/{pid}/status", "r", encoding="utf-8",
                      errors="replace") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        rss_kb = int(line.split()[1])
                        break
        except (OSError, ValueError):
            pass
        rows.append({"pid": pid, "name": name,
                     "memory_mb": round(rss_kb / 1024.0, 1)})
    return rows


def _processes(limit: int = 30) -> List[Dict[str, Any]]:
    """Top processes by resident memory."""
    psutil = _opt("psutil")
    rows: List[Dict[str, Any]] = []
    if psutil:
        for p in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
            try:
                info = p.info
                mem = getattr(info.get("memory_info"), "rss", 0) or 0
                rows.append(
                    {
                        "pid": int(info.get("pid")),
                        "name": str(info.get("name") or ""),
                        "memory_mb": round(mem / (1024 * 1024), 1),
                    }
                )
            except Exception:
                continue
    else:
        rows = _proc_rows()
    rows.sort(key=lambda r: r["memory_mb"], reverse=True)
    return rows[: max(1, min(int(limit or 30), 200))]


def _kill(name: Optional[str] = None, pid: Optional[int] = None) -> str:
    """SIGTERM a process by pid or by a case-insensitive name substring.

    Terminate, never SIGKILL: the app gets to flush its own state, which is the
    same thing the desktop does when you close a window.
    """
    import signal

    psutil = _opt("psutil")
    killed: List[str] = []

    if pid:
        pid = int(pid)
        if psutil:
            try:
                proc = psutil.Process(pid)
                pname = proc.name()
                proc.terminate()
                killed.append(f"{pname} (pid {pid})")
            except Exception as exc:
                raise ComputerActionError(f"Could not kill pid {pid}: {exc}") from exc
        else:
            try:
                label = f"pid {pid}"
                for row in _proc_rows():
                    if row["pid"] == pid:
                        label = f"{row['name']} (pid {pid})"
                        break
                os.kill(pid, signal.SIGTERM)
                killed.append(label)
            except ProcessLookupError:
                raise ComputerActionError(f"No process with pid {pid}")
            except PermissionError as exc:
                raise ComputerActionError(
                    f"Permission denied killing pid {pid} (is it owned by another "
                    "user? sudo is deliberately not used here)."
                ) from exc
    elif name:
        target = str(name).lower()
        if psutil:
            for p in psutil.process_iter(["pid", "name"]):
                try:
                    pname = str(p.info.get("name") or "")
                    if target in pname.lower():
                        p.terminate()
                        killed.append(f"{pname} (pid {p.info.get('pid')})")
                except Exception:
                    continue
        else:
            for row in _proc_rows():
                if target in row["name"].lower():
                    try:
                        os.kill(row["pid"], signal.SIGTERM)
                        killed.append(f"{row['name']} (pid {row['pid']})")
                    except (ProcessLookupError, PermissionError) as exc:
                        logger.debug("could not terminate %s: %s", row["pid"], exc)
        if not killed:
            raise ComputerActionError(f"No running process matches '{name}'")
    else:
        raise ComputerActionError("Provide `name` or `pid`")
    return "Terminated: " + ", ".join(killed[:10])


def _system_info() -> Dict[str, Any]:
    from core.platform_compat import distro_pretty_name

    info: Dict[str, Any] = {
        "host": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "distro": distro_pretty_name(),
        "version": platform.version(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "session": _session() or "none",
        "desktop": desktop_environment() or "",
        "compositor": _compositor(),
        "selinux": selinux_mode(),
    }
    psutil = _opt("psutil")
    if psutil:
        try:
            vm = psutil.virtual_memory()
            info["cpu_cores"] = psutil.cpu_count(logical=True)
            info["cpu_physical"] = psutil.cpu_count(logical=False)
            info["cpu_percent"] = psutil.cpu_percent(interval=0.2)
            info["ram_total_gb"] = round(vm.total / (1024 ** 3), 1)
            info["ram_used_gb"] = round(vm.used / (1024 ** 3), 1)
            info["ram_percent"] = vm.percent
        except Exception:
            pass
        try:
            disk = psutil.disk_usage(os.path.expanduser("~"))
            info["disk_total_gb"] = round(disk.total / (1024 ** 3), 1)
            info["disk_free_gb"] = round(disk.free / (1024 ** 3), 1)
            info["disk_percent"] = disk.percent
        except Exception:
            pass
        try:
            batt = psutil.sensors_battery()
            if batt:
                info["battery_percent"] = round(batt.percent, 1)
                info["battery_plugged"] = bool(batt.power_plugged)
        except Exception:
            pass
        try:
            info["uptime_minutes"] = round((time.time() - psutil.boot_time()) / 60)
        except Exception:
            pass
    else:
        info.update(_proc_based_system_info())
    try:
        info["screen"] = _screen_size()
    except Exception as exc:
        logger.debug("system info: screen size unavailable: %s", exc)
    return info


def _read_first(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _proc_based_system_info() -> Dict[str, Any]:
    """Memory/disk/battery/uptime without psutil — plain /proc, /sys, statvfs."""
    info: Dict[str, Any] = {}
    try:
        info["cpu_cores"] = os.cpu_count() or 0
    except Exception:
        pass
    try:
        info["load_average"] = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        pass

    meminfo: Dict[str, int] = {}
    for line in _read_first("/proc/meminfo").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            meminfo[parts[0].rstrip(":")] = int(parts[1])  # kB
    total = meminfo.get("MemTotal", 0)
    available = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
    if total:
        info["ram_total_gb"] = round(total / (1024 ** 2), 1)
        info["ram_used_gb"] = round((total - available) / (1024 ** 2), 1)
        info["ram_percent"] = round(100.0 * (total - available) / total, 1)

    try:
        usage = shutil.disk_usage(os.path.expanduser("~"))
        info["disk_total_gb"] = round(usage.total / (1024 ** 3), 1)
        info["disk_free_gb"] = round(usage.free / (1024 ** 3), 1)
        info["disk_percent"] = round(100.0 * usage.used / usage.total, 1)
    except OSError:
        pass

    uptime = _read_first("/proc/uptime").split()
    if uptime:
        try:
            info["uptime_minutes"] = round(float(uptime[0]) / 60.0)
        except ValueError:
            pass

    try:
        supplies = sorted(os.listdir("/sys/class/power_supply"))
    except OSError:
        supplies = []
    for supply in supplies:
        base = f"/sys/class/power_supply/{supply}"
        capacity = _read_first(f"{base}/capacity")
        if not capacity.isdigit():
            continue
        info["battery_percent"] = float(capacity)
        info["battery_plugged"] = _read_first(f"{base}/status") == "Charging" or (
            _read_first(f"{base}/online") == "1"
        )
        break
    return info


def _audio_backend() -> str:
    """`wpctl` (PipeWire), `pactl` (PulseAudio), `amixer` (ALSA), or ""."""
    for backend in ("wpctl", "pactl", "amixer"):
        if _tool(backend):
            return backend
    return ""


def _read_volume() -> Dict[str, Any]:
    """Current level/mute, best effort — used to report what actually happened."""
    import re

    backend = _audio_backend()
    try:
        if backend == "wpctl":
            raw = _run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], timeout=8)
            match = re.search(r"([01]?\.\d+)", raw)
            return {
                "level": int(round(float(match.group(1)) * 100)) if match else None,
                "muted": "[MUTED]" in raw.upper(),
                "backend": backend,
            }
        if backend == "pactl":
            raw = _run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"], timeout=8)
            match = re.search(r"(\d+)%", raw)
            muted_raw = _run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"], timeout=8)
            return {
                "level": int(match.group(1)) if match else None,
                "muted": "yes" in muted_raw.lower(),
                "backend": backend,
            }
    except (ComputerActionError, ValueError) as exc:
        logger.debug("volume read via %s failed: %s", backend, exc)
    return {"backend": backend} if backend else {}


def _volume(level: Optional[int] = None, direction: Optional[str] = None,
            steps: int = 2, mute: Optional[bool] = None) -> Dict[str, Any]:
    """Change the output volume.

    Fedora is PipeWire, so `wpctl` is the native control; `pactl` is kept
    because pipewire-pulse exposes it and it works on a PulseAudio-only box,
    and `amixer` covers a headless/ALSA setup. With none of them present the
    action degrades to pressing the real volume keys, which is exactly what a
    human does and works on Wayland too.
    """
    backend = _audio_backend()

    if mute is not None and level is None and direction is None:
        if backend == "wpctl":
            _run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1" if mute else "0"])
            return {"muted": bool(mute), "backend": backend}
        if backend == "pactl":
            _run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if mute else "0"])
            return {"muted": bool(mute), "backend": backend}
        if backend == "amixer":
            _run(["amixer", "-q", "sset", "Master", "mute" if mute else "unmute"])
            return {"muted": bool(mute), "backend": backend}
        _press("mute")
        return {"muted": bool(mute), "backend": "keys", "approximate": True}

    if level is not None:
        level = max(0, min(100, int(level)))
        if backend == "wpctl":
            _run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{level}%"])
            return {"level": level, **_read_volume()}
        if backend == "pactl":
            _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"])
            return {"level": level, **_read_volume()}
        if backend == "amixer":
            _run(["amixer", "-q", "sset", "Master", f"{level}%"])
            return {"level": level, **_read_volume()}
        # No mixer: walk to zero with the volume-down key, then back up.
        _set_volume_by_steps(100, "down")
        _set_volume_by_steps(level // 2, "up")
        return {"level": level, "approximate": True, "backend": "keys"}

    direction = (direction or "up").lower()
    if direction not in ("up", "down"):
        raise ComputerActionError("`direction` must be 'up' or 'down'")
    return _set_volume_by_steps(max(1, min(int(steps or 1), 50)), direction)


def _set_volume_by_steps(steps: int, direction: str) -> Dict[str, Any]:
    """Nudge the volume in ~6% increments (one step of a mixer, or one key)."""
    steps = max(1, min(int(steps), 50))
    backend = _audio_backend()
    sign = "+" if direction == "up" else "-"
    for _ in range(steps):
        if backend == "wpctl":
            _run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"6%{sign}"])
        elif backend == "pactl":
            _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{sign}6%"])
        elif backend == "amixer":
            _run(["amixer", "-q", "sset", "Master", f"6%{sign}"])
        else:
            _press("volumeup" if direction == "up" else "volumedown")
        time.sleep(0.03)
    return {"steps": steps, "direction": direction,
            "backend": backend or "keys", **_read_volume()}


def _notify(title: str, message: str) -> str:
    """Desktop notification through libnotify, falling back to the bus.

    `notify-send` is the standard route and honours the user's do-not-disturb
    setting. When it is missing, the same notification is sent by calling
    org.freedesktop.Notifications directly over the session bus, which is what
    notify-send does internally.
    """
    title = str(title or "psd.ai")[:120]
    message = str(message or "")[:400]

    if _tool("notify-send"):
        try:
            _run(["notify-send", "--app-name", "psd.ai", "--urgency", "normal",
                  title, message], timeout=10)
            return "Notification shown"
        except ComputerActionError as exc:
            logger.debug("notify-send failed: %s", exc)

    gdbus = _tool("gdbus")
    if gdbus and os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        try:
            _run([gdbus, "call", "--session",
                  "--dest", "org.freedesktop.Notifications",
                  "--object-path", "/org/freedesktop/Notifications",
                  "--method", "org.freedesktop.Notifications.Notify",
                  "psd.ai", "0", "", title, message, "[]", "{}", "5000"],
                 timeout=10)
            return "Notification shown"
        except ComputerActionError as exc:
            raise ComputerActionError(f"Notification failed: {exc}") from exc

    raise ComputerActionError(
        "Cannot send a notification: notify-send is not installed. "
        + (_missing_hint(["libnotify"]) or "")
    )


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


class ComputerService:
    """Executes allow-listed desktop actions on the host machine."""

    def __init__(self, timeout: int = ACTION_TIMEOUT_SECONDS):
        self.timeout = timeout

    # ── introspection ──

    def status(self) -> Dict[str, Any]:
        """Capability report for /api/jarvis/status and Settings → Voice.

        The honest question on Linux is not "is the Python package installed"
        but "does this session have a tool that can do it", and the answer
        differs between Wayland and X11. So capabilities are resolved for the
        session that is actually running, and every gap carries the `dnf` line
        that closes it.
        """

        enabled = computer_control_enabled()
        wayland = _is_wayland()
        session = _session()
        comp = _compositor()

        backends = {
            "pyautogui": _have("pyautogui"),
            "mss": _have("mss"),
            "pyperclip": _have("pyperclip"),
            "psutil": _have("psutil"),
            "pygetwindow": _have("pygetwindow"),
            "pillow": _have("PIL"),
        }

        tool_names = (
            "ydotool", "wtype", "xdotool", "wmctrl", "grim", "slurp",
            "wlr-randr", "scrot", "import", "gnome-screenshot", "wl-copy",
            "wl-paste", "xclip", "xsel", "wpctl", "pactl", "amixer",
            "notify-send", "xdg-open", "gtk-launch", "gio", "gdbus",
            "swaymsg", "hyprctl", "xrandr",
        )
        tools = {name: bool(_tool(name)) for name in tool_names}
        # ydotool is only useful with its daemon reachable on the socket.
        tools["ydotoold"] = bool(_ydotool_socket())

        notes: List[str] = []

        # ── input ──
        if wayland:
            keyboard = tools["ydotool"] or tools["wtype"]
            mouse = tools["ydotool"]  # wtype is keyboard-only
            if tools["ydotool"] and not tools["ydotoold"]:
                notes.append(
                    "ydotool is installed but ydotoold is not running, so input "
                    "injection will fail. Start it once: sudo dnf install ydotool "
                    "&& sudo systemctl enable --now ydotoold (it needs /dev/uinput)."
                )
            if keyboard and not mouse:
                notes.append(
                    "Only wtype is available: typing works, pointer control "
                    "does not. Install ydotool for mouse and drags."
                )
            if comp == "gnome":
                notes.append(
                    "GNOME Wayland: wtype's virtual-keyboard protocol is not "
                    "implemented, so ydotool is the only input path here."
                )
        else:
            keyboard = mouse = tools["xdotool"] or backends["pyautogui"]

        # ── screenshots ──
        if wayland:
            screenshot = tools["grim"] or tools["gdbus"] or backends["mss"]
            if not tools["grim"]:
                notes.append(
                    "grim is missing, so screenshots go through the XDG Desktop "
                    "Portal, which asks you to approve each capture. "
                    "sudo dnf install grim slurp"
                )
            elif comp in ("gnome", "kde"):
                notes.append(
                    f"{comp.title()} does not implement wlr-screencopy, so grim "
                    "will fail and captures fall back to the portal dialog."
                )
        else:
            screenshot = (backends["mss"] or backends["pillow"]
                          or tools["scrot"] or tools["import"]
                          or tools["gnome-screenshot"])

        # ── windows ──
        if wayland:
            windows = ((comp == "sway" and tools["swaymsg"])
                       or (comp == "hyprland" and tools["hyprctl"]))
            if not windows:
                notes.append(
                    "This Wayland compositor keeps its window tree private "
                    "(GNOME and KDE both do, and GNOME disabled the Shell "
                    "D-Bus Eval that used to expose it), so `windows`, `focus` "
                    "and `close` cannot be done. Sway and Hyprland can."
                )
        else:
            windows = (tools["wmctrl"] or tools["xdotool"]
                       or backends["pygetwindow"])

        # ── clipboard ──
        if wayland:
            clipboard = (tools["wl-paste"] and tools["wl-copy"]) or backends["pyperclip"]
        else:
            clipboard = tools["xclip"] or tools["xsel"] or backends["pyperclip"]

        volume = tools["wpctl"] or tools["pactl"] or tools["amixer"] or keyboard
        notify = tools["notify-send"] or tools["gdbus"]
        launch = tools["xdg-open"] or tools["gtk-launch"] or tools["gio"]

        native: List[str] = []
        if mouse:
            native.append("mouse")
        if keyboard:
            native.append("keyboard")
        if windows:
            native.append("windows")
        if clipboard:
            native.append("clipboard")
        if volume:
            native.append("volume")
        if notify:
            native.append("notify")
        if launch:
            native.append("open")
        if screenshot:
            native.append("screenshot")

        # What this session still needs, as one copy-pasteable command.
        wanted = {
            "ydotool": wayland and not tools["ydotool"],
            "wtype": wayland and not tools["wtype"],
            "grim": wayland and not tools["grim"],
            "slurp": wayland and not tools["slurp"],
            "wl-clipboard": wayland and not (tools["wl-paste"] and tools["wl-copy"]),
            "xdotool": not wayland and not tools["xdotool"],
            "wmctrl": not wayland and not tools["wmctrl"],
            "xclip": not wayland and not tools["xclip"],
            "scrot": not wayland and not screenshot,
            "wpctl": not tools["wpctl"],
            "notify-send": not tools["notify-send"],
            "xdg-open": not tools["xdg-open"],
        }
        missing = [tool for tool, needed in wanted.items() if needed]
        install_hint = dnf_install_hint(
            ["wl-copy" if tool == "wl-clipboard" else tool for tool in missing]
        )

        return {
            "enabled": enabled,
            "confirm_risky": _confirm_risky(),
            "platform": sys.platform,
            "os": f"{platform.system()} {platform.release()}",
            "distro": _distro_name(),
            "session": session or "none",
            "wayland": bool(wayland),
            "compositor": comp,
            "desktop": desktop_environment(),
            "selinux": selinux_mode(),
            "supports_screenshot": bool(screenshot),
            "supports_input": bool(mouse and keyboard),
            "supports_windows": bool(windows),
            "supports_clipboard": bool(clipboard),
            "supports_volume": bool(volume),
            "supports_notify": bool(notify),
            "backends": backends,
            "tools": tools,
            "native": native,
            "missing_packages": sorted(
                k for k, v in backends.items() if not v and k != "pillow"
            ),
            "missing_tools": missing,
            "install_hint": install_hint,
            "notes": notes,
            "policy": describe_policy(),
        }

    # ── single entry point ──

    async def act(
        self,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        confirm_risky: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Validate + run one action. Never raises for policy denials.

        Returns a dict with ``ok``, ``action``, ``risk`` and either ``result``
        or ``error``. ``needs_confirmation=True`` means the caller must ask
        the owner before running it again with ``confirm=True``.
        """

        params = dict(params or {})
        name = str(action or "").strip().lower()
        if confirm_risky is None:
            confirm_risky = _confirm_risky()
        confirmed = bool(params.pop("confirm", False)) or bool(params.pop("confirmed", False))

        allowed, reason, risk, needs_confirm = check_action(
            name, params, confirm_risky=confirm_risky
        )
        if not allowed:
            logger.warning("computer action refused: %s (%s)", name, reason)
            return {
                "ok": False, "action": name, "risk": risk.value,
                "error": reason, "blocked": True, "policy": "computer",
            }
        if needs_confirm and not confirmed:
            return {
                "ok": False, "action": name, "risk": risk.value,
                "needs_confirmation": True, "blocked": True,
                "confirm_hint": _confirm_hint(name, params),
                "error": f"'{name}' needs your approval before I run it.",
            }

        if not computer_control_enabled():
            return {
                "ok": False, "action": name, "risk": risk.value,
                "error": (
                    "Computer control is switched off. Turn it on in "
                    "Settings → Voice & PC (or set PSD_AI_COMPUTER_CONTROL=1)."
                ),
                "blocked": True, "policy": "computer",
            }

        handler: Optional[Callable[[Dict[str, Any]], Any]] = _HANDLERS.get(name)
        if handler is None:  # pragma: no cover - check_action already covers it
            return {
                "ok": False, "action": name, "risk": risk.value,
                "error": f"No handler for '{name}'", "blocked": True,
            }

        started = time.time()
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(handler, params), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            return {
                "ok": False, "action": name, "risk": risk.value,
                "error": f"'{name}' timed out after {self.timeout}s",
            }
        except ComputerActionError as exc:
            return {"ok": False, "action": name, "risk": risk.value, "error": str(exc)}
        except Exception as exc:
            logger.exception("computer action %s failed", name)
            return {
                "ok": False, "action": name, "risk": risk.value,
                "error": f"{name} failed: {exc}",
            }

        payload: Dict[str, Any] = {
            "ok": True, "action": name, "risk": risk.value,
            "elapsed_ms": int((time.time() - started) * 1000),
        }
        if isinstance(result, dict):
            payload.update(result)
        else:
            payload["result"] = result
        return payload

    # ── convenience wrappers ──

    async def screenshot(self, monitor: int = 0) -> bytes:
        if not computer_control_enabled():
            raise ComputerActionError("Computer control is switched off")
        return await asyncio.wait_for(
            asyncio.to_thread(_screenshot_bytes, int(monitor or 0)),
            timeout=SCREENSHOT_TIMEOUT_SECONDS,
        )

    async def screenshot_b64(self, monitor: int = 0) -> str:
        return base64.b64encode(await self.screenshot(monitor)).decode("ascii")

    async def screen_size(self) -> Dict[str, int]:
        return await asyncio.to_thread(_screen_size)

    async def system_info(self) -> Dict[str, Any]:
        return await asyncio.to_thread(_system_info)

    async def windows(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(_windows)


def _confirm_hint(action: str, params: Dict[str, Any]) -> str:
    if action == "kill":
        return f"Terminate {params.get('name') or params.get('pid')}?"
    return f"Run {action}?"


# ---------------------------------------------------------------------------
# handler table
# ---------------------------------------------------------------------------


def _h_click(p: Dict[str, Any]) -> Dict[str, Any]:
    _click(p.get("x"), p.get("y"), str(p.get("button") or "left"),
           int(p.get("clicks") or 1))
    return {"clicked": {"x": p.get("x"), "y": p.get("y"), "button": p.get("button") or "left"}}


def _h_move(p: Dict[str, Any]) -> Dict[str, Any]:
    _move(int(p.get("x") or 0), int(p.get("y") or 0))
    return {"moved": {"x": int(p.get("x") or 0), "y": int(p.get("y") or 0)}}


def _h_drag(p: Dict[str, Any]) -> Dict[str, Any]:
    _drag(int(p.get("x1") or 0), int(p.get("y1") or 0),
          int(p.get("x2") or 0), int(p.get("y2") or 0),
          float(p.get("duration") or 0.4))
    return {"dragged": True}


def _h_scroll(p: Dict[str, Any]) -> Dict[str, Any]:
    amount = int(p.get("amount") or p.get("clicks") or 3)
    _scroll(amount)
    return {"scrolled": amount}


def _h_type(p: Dict[str, Any]) -> Dict[str, Any]:
    text = str(p.get("text") or "")
    _type_text(text)
    return {"typed_chars": len(text)}


def _h_key(p: Dict[str, Any]) -> Dict[str, Any]:
    combo = p.get("combo") or p.get("keys") or p.get("key")
    if isinstance(combo, str):
        parts = [c.strip() for c in combo.replace("-", "+").split("+") if c.strip()]
    elif isinstance(combo, list):
        parts = [str(c).strip() for c in combo if str(c).strip()]
    else:
        raise ComputerActionError("Supply `key` (a key) or `combo` (e.g. 'ctrl+s')")
    if len(parts) == 1:
        _press(parts[0])
    else:
        _hotkey(parts)
    return {"keys": parts}


def _h_open(p: Dict[str, Any]) -> Dict[str, Any]:
    target = str(p.get("target") or p.get("path") or p.get("name") or "")
    return {"opened": _open_target(target), "target": target}


def _h_windows(_p: Dict[str, Any]) -> Dict[str, Any]:
    return {"windows": _windows()}


def _h_focus(p: Dict[str, Any]) -> Dict[str, Any]:
    title = str(p.get("title") or p.get("name") or "")
    if not title:
        raise ComputerActionError("Supply a window `title`")
    return {"focused": _focus(title)}


def _h_close(p: Dict[str, Any]) -> Dict[str, Any]:
    title = str(p.get("title") or p.get("name") or "")
    if not title:
        raise ComputerActionError("Supply a window `title`")
    return {"closed": _close_window(title)}


def _h_clipboard_get(_p: Dict[str, Any]) -> Dict[str, Any]:
    text = _clipboard_get()
    preview = text[:4000]
    return {"text": preview, "truncated": len(text) > len(preview)}


def _h_clipboard_set(p: Dict[str, Any]) -> Dict[str, Any]:
    text = str(p.get("text") or "")
    if len(text) > 200_000:
        raise ComputerActionError("Clipboard text is too large (200 KB limit)")
    _clipboard_set(text)
    return {"copied_chars": len(text)}


def _h_info(_p: Dict[str, Any]) -> Dict[str, Any]:
    return {"info": _system_info()}


def _h_processes(p: Dict[str, Any]) -> Dict[str, Any]:
    return {"processes": _processes(int(p.get("limit") or 30))}


def _h_kill(p: Dict[str, Any]) -> Dict[str, Any]:
    pid = p.get("pid")
    name = p.get("name") or p.get("process") or p.get("process_name")
    return {"killed": _kill(name=name, pid=int(pid) if pid else None)}


def _h_notify(p: Dict[str, Any]) -> Dict[str, Any]:
    return {"notified": _notify(str(p.get("title") or "psd.ai"), str(p.get("message") or ""))}


def _h_volume(p: Dict[str, Any]) -> Dict[str, Any]:
    level = p.get("level")
    mute = p.get("mute")
    return _volume(
        level=int(level) if level is not None else None,
        direction=(str(p.get("direction")).lower() if p.get("direction") else None),
        steps=int(p.get("steps") or 2),
        mute=(None if mute is None else bool(mute)),
    )


def _h_wait(p: Dict[str, Any]) -> Dict[str, Any]:
    seconds = max(0.0, min(float(p.get("seconds") or p.get("duration") or 1), 30))
    time.sleep(seconds)
    return {"waited": seconds}


def _h_screen_size(_p: Dict[str, Any]) -> Dict[str, Any]:
    return {"screen": _screen_size()}


def _h_screenshot(p: Dict[str, Any]) -> Dict[str, Any]:
    data = _screenshot_bytes(int(p.get("monitor") or 0))
    return {
        "bytes": len(data),
        "image_base64": base64.b64encode(data).decode("ascii"),
    }


_HANDLERS: Dict[str, Callable[[Dict[str, Any]], Any]] = {
    "screenshot": _h_screenshot,
    "screen_size": _h_screen_size,
    "click": _h_click,
    "move": _h_move,
    "drag": _h_drag,
    "scroll": _h_scroll,
    "type": _h_type,
    "key": _h_key,
    "open": _h_open,
    "windows": _h_windows,
    "focus": _h_focus,
    "close_window": _h_close,
    "clipboard_get": _h_clipboard_get,
    "clipboard_set": _h_clipboard_set,
    "info": _h_info,
    "processes": _h_processes,
    "kill": _h_kill,
    "notify": _h_notify,
    "volume": _h_volume,
    "wait": _h_wait,
}


# ---------------------------------------------------------------------------
# singleton
# ---------------------------------------------------------------------------

_computer_service: Optional[ComputerService] = None


def get_computer_service() -> ComputerService:
    global _computer_service
    if _computer_service is None:
        _computer_service = ComputerService()
    return _computer_service
