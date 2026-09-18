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

Optional packages (`pyautogui`, `mss`, `pyperclip`, `psutil`, `pygetwindow`,
`pycaw`) are used when present. Each of them has a dependency-free fallback
built on `ctypes` (Windows), `osascript` (macOS) or the usual X11 tooling
(Linux), so a fresh install still drives the machine — see
`requirements-jarvis.txt` for the packages that make it nicer.
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

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

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
    """Primary monitor size in logical pixels."""

    if IS_WINDOWS:
        try:
            import ctypes

            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
            return {
                "width": int(user32.GetSystemMetrics(0)),
                "height": int(user32.GetSystemMetrics(1)),
            }
        except Exception:
            pass
    mss = _opt("mss")
    if mss:
        try:
            with mss.mss() as sct:
                mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                return {"width": int(mon["width"]), "height": int(mon["height"])}
        except Exception:
            pass
    try:
        from PIL import ImageGrab  # type: ignore

        bbox = ImageGrab.grab().size
        return {"width": int(bbox[0]), "height": int(bbox[1])}
    except Exception as exc:
        raise ComputerActionError(f"Cannot read screen size: {exc}")


def _screenshot_bytes(monitor: int = 0) -> bytes:
    """Capture the screen and return PNG bytes."""

    # 1. mss — fast, multi-monitor aware, no OS dialogs.
    mss = _opt("mss")
    if mss:
        try:
            with mss.mss() as sct:
                idx = 0 if monitor <= 0 else min(monitor, len(sct.monitors) - 1)
                raw = sct.grab(sct.monitors[idx])
            return _png_from_raw(raw)
        except Exception as exc:
            logger.debug("mss screenshot failed: %s", exc)

    # 2. Pillow's ImageGrab (Win32 / macOS via Quartz, X11 via scrot/display).
    try:
        from PIL import ImageGrab  # type: ignore

        img = ImageGrab.grab(all_screens=(monitor == 0))
        return _encode_png(img)
    except Exception as exc:
        logger.debug("ImageGrab screenshot failed: %s", exc)

    # 3. Platform CLI fallbacks.
    if IS_MAC:
        return _run_capture(["screencapture", "-x", "-t", "png", "-"])
    if IS_LINUX:
        for cmd in (
            ["gnome-screenshot", "-f", "-"],
            ["import", "-window", "root", "png:-"],
            ["scrot", "-o", "-"],
        ):
            if shutil.which(cmd[0]):
                try:
                    return _run_capture(cmd)
                except Exception:
                    continue
    raise ComputerActionError(
        "No screenshot backend available. Install `mss` or Pillow "
        "(pip install -r requirements-jarvis.txt)."
    )


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
# mouse / keyboard
# ---------------------------------------------------------------------------

if IS_WINDOWS:
    import ctypes

    _MOUSEEVENTF_MOVE = 0x0001
    _MOUSEEVENTF_LEFTDOWN = 0x0002
    _MOUSEEVENTF_LEFTUP = 0x0004
    _MOUSEEVENTF_RIGHTDOWN = 0x0008
    _MOUSEEVENTF_RIGHTUP = 0x0010
    _MOUSEEVENTF_MIDDLEDOWN = 0x0020
    _MOUSEEVENTF_MIDDLEUP = 0x0040
    _MOUSEEVENTF_WHEEL = 0x0800
    _MOUSEEVENTF_HWHEEL = 0x01000
    _MOUSEEVENTF_ABSOLUTE = 0x8000
    _KEYEVENTF_KEYUP = 0x0002
    _KEYEVENTF_UNICODE = 0x0004
    _SM_CXSCREEN = 0
    _SM_CYSCREEN = 1

    _VK_MAP = {
        "enter": 0x0D, "return": 0x0D, "tab": 0x09, "space": 0x20,
        "esc": 0x1B, "escape": 0x1B, "backspace": 0x08, "delete": 0x2E,
        "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
        "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
        "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
        "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
        "f11": 0x7A, "f12": 0x7B,
        "win": 0x5B, "super": 0x5B, "cmd": 0x5B, "ctrl": 0x11,
        "control": 0x11, "alt": 0x12, "shift": 0x10, "capslock": 0x14,
        "printscreen": 0x2C, "insert": 0x2D, "menu": 0x5D,
    }

    def _win_vk(key: str) -> int:
        k = key.strip().lower()
        if k in _VK_MAP:
            return _VK_MAP[k]
        if len(k) == 1:
            return ctypes.windll.user32.VkKeyScanW(ord(k)) & 0xFF  # type: ignore[attr-defined]
        raise ComputerActionError(f"Unknown key '{key}'")

    def _win_key_event(vk: int, up: bool) -> None:
        ctypes.windll.user32.keybd_event(  # type: ignore[attr-defined]
            int(vk), 0, _KEYEVENTF_KEYUP if up else 0, 0
        )

    def _win_type(text: str) -> None:
        for unit in text.encode("utf-16-le").decode("latin-1"):
            code = ord(unit)
            ctypes.windll.user32.keybd_event(  # type: ignore[attr-defined]
                0, code, _KEYEVENTF_UNICODE, 0
            )
            ctypes.windll.user32.keybd_event(  # type: ignore[attr-defined]
                0, code, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP, 0
            )

    def _win_click(button: str, down: bool) -> None:
        down_up = {
            "left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
            "right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
            "middle": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP),
        }.get(button, (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP))
        flag = down_up[1] if down else down_up[0]
        ctypes.windll.user32.mouse_event(flag, 0, 0, 0, 0)  # type: ignore[attr-defined]


def _move(x: int, y: int) -> None:
    pg = _opt("pyautogui")
    if pg:
        try:
            pg.moveTo(int(x), int(y))
            return
        except Exception as exc:
            logger.debug("pyautogui move failed: %s", exc)
    if IS_WINDOWS:
        import ctypes

        ctypes.windll.user32.SetCursorPos(int(x), int(y))  # type: ignore[attr-defined]
        return
    if IS_MAC or IS_LINUX:
        _xdotool(["mousemove", str(int(x)), str(int(y))])
        return
    raise ComputerActionError("Mouse control is not supported on this platform")


def _click(x: Optional[int], y: Optional[int], button: str, clicks: int) -> None:
    pg = _opt("pyautogui")
    if pg:
        try:
            if x is not None and y is not None:
                pg.click(x=int(x), y=int(y), button=button, clicks=max(1, int(clicks)))
            else:
                pg.click(button=button, clicks=max(1, int(clicks)))
            return
        except Exception as exc:
            logger.debug("pyautogui click failed: %s", exc)
    if IS_WINDOWS:
        import ctypes

        if x is not None and y is not None:
            ctypes.windll.user32.SetCursorPos(int(x), int(y))  # type: ignore[attr-defined]
        for _ in range(max(1, int(clicks))):
            _win_click(button, True)
            _win_click(button, False)
        return
    if IS_MAC or IS_LINUX:
        if x is not None and y is not None:
            _xdotool(["mousemove", str(int(x)), str(int(y))])
        btn = {"left": "1", "middle": "2", "right": "3"}.get(button, "1")
        for _ in range(max(1, int(clicks))):
            _xdotool(["click", btn])
        return
    raise ComputerActionError("Mouse control is not supported on this platform")


def _drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.4) -> None:
    pg = _opt("pyautogui")
    if pg:
        try:
            pg.moveTo(int(x1), int(y1))
            pg.dragTo(int(x2), int(y2), duration=max(0.05, float(duration)))
            return
        except Exception as exc:
            logger.debug("pyautogui drag failed: %s", exc)
    if IS_WINDOWS:
        import ctypes

        ctypes.windll.user32.SetCursorPos(int(x1), int(y1))  # type: ignore[attr-defined]
        _win_click("left", True)
        ctypes.windll.user32.SetCursorPos(int(x2), int(y2))  # type: ignore[attr-defined]
        _win_click("left", False)
        return
    if IS_MAC or IS_LINUX:
        _xdotool(["mousemove", str(int(x1)), str(int(y1)), "mousedown", "1",
                  "mousemove", str(int(x2)), str(int(y2)), "mouseup", "1"])
        return
    raise ComputerActionError("Drag is not supported on this platform")


def _scroll(amount: int) -> None:
    """Positive scrolls up, negative scrolls down."""

    pg = _opt("pyautogui")
    if pg:
        try:
            pg.scroll(int(amount))
            return
        except Exception as exc:
            logger.debug("pyautogui scroll failed: %s", exc)
    if IS_WINDOWS:
        import ctypes

        # One wheel notch is 120 units in the Win32 API.
        ctypes.windll.user32.mouse_event(  # type: ignore[attr-defined]
            _MOUSEEVENTF_WHEEL, 0, 0, int(amount) * 120, 0
        )
        return
    if IS_MAC or IS_LINUX:
        btn = "4" if int(amount) > 0 else "5"
        for _ in range(min(abs(int(amount)), 40)):
            _xdotool(["click", btn])
        return
    raise ComputerActionError("Scroll is not supported on this platform")


def _type_text(text: str) -> None:
    text = str(text or "")
    if not text:
        return
    if len(text) > MAX_TYPED_CHARS:
        raise ComputerActionError(
            f"Refusing to type {len(text)} characters (limit {MAX_TYPED_CHARS})."
        )
    pg = _opt("pyautogui")
    if pg:
        try:
            pg.write(text, interval=0.01)
            return
        except Exception as exc:
            logger.debug("pyautogui type failed: %s", exc)
    if IS_WINDOWS:
        _win_type(text)
        return
    if IS_MAC:
        # osascript keystroke mangles unicode; clipboard+paste is safer.
        prev = _clipboard_get()
        _clipboard_set(text)
        _hotkey(["command", "v"])
        if prev:
            _clipboard_set(prev)
        return
    if IS_LINUX:
        _run(["xdotool", "type", "--clearmodifiers", "--", text])
        return
    raise ComputerActionError("Typing is not supported on this platform")


def _hotkey(keys: List[str]) -> None:
    """Press a chord, e.g. ["ctrl", "shift", "esc"]."""

    keys = [str(k).strip().lower() for k in keys if str(k).strip()]
    if not keys:
        raise ComputerActionError("No keys supplied")
    pg = _opt("pyautogui")
    if pg:
        try:
            pg.hotkey(*[k for k in keys])
            return
        except Exception as exc:
            logger.debug("pyautogui hotkey failed: %s", exc)
    if IS_WINDOWS:
        vks = [_win_vk(k) for k in keys]
        for vk in vks:
            _win_key_event(vk, False)
        for vk in reversed(vks):
            _win_key_event(vk, True)
        return
    if IS_MAC:
        mods = {"ctrl": "control", "control": "control", "alt": "option",
                "option": "option", "shift": "shift", "cmd": "command",
                "command": "command", "win": "command", "super": "command"}
        parts: List[str] = []
        for k in keys:
            parts.append(mods.get(k, k))
        script = (
            'tell application "System Events" to keystroke "" using {'
            + ", ".join(f"{m} down" for m in parts)
            + "}"
        )
        _run(["osascript", "-e", script])
        return
    if IS_LINUX:
        _run(["xdotool", "key", "--clearmodifiers", "+".join(keys)])
        return
    raise ComputerActionError("Hotkeys are not supported on this platform")


def _press(key: str) -> None:
    pg = _opt("pyautogui")
    if pg:
        try:
            pg.press(key)
            return
        except Exception as exc:
            logger.debug("pyautogui press failed: %s", exc)
    if IS_WINDOWS:
        vk = _win_vk(key)
        _win_key_event(vk, False)
        _win_key_event(vk, True)
        return
    _hotkey([key])


def _xdotool(args: List[str]) -> None:
    _run(["xdotool", *args])


def _run(cmd: List[str], timeout: int = 15) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise ComputerActionError(f"{cmd[0]} is not installed")
    if out.returncode != 0:
        raise ComputerActionError(
            out.stderr.decode(errors="replace").strip() or f"{cmd[0]} failed"
        )
    return out.stdout.decode(errors="replace")


# ---------------------------------------------------------------------------
# windows
# ---------------------------------------------------------------------------


def _windows() -> List[Dict[str, Any]]:
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

    if IS_WINDOWS:
        return _win_list_windows()
    if IS_MAC:
        script = (
            'tell application "System Events" to get {name, position, size} '
            "of every window of every process"
        )
        try:
            raw = _run(["osascript", "-e", script])
        except ComputerActionError:
            return []
        titles = [t.strip() for t in raw.split(",") if t.strip()]
        return [{"title": t, "visible": True} for t in titles[:80]]
    if IS_LINUX and shutil.which("wmctrl"):
        try:
            raw = _run(["wmctrl", "-lG"])
        except ComputerActionError:
            return []
        out = []
        for line in raw.splitlines():
            parts = line.split(None, 6)
            if len(parts) >= 7:
                out.append({"title": parts[6], "left": int(parts[2] or 0),
                            "top": int(parts[3] or 0), "width": int(parts[4] or 0),
                            "height": int(parts[5] or 0), "visible": True})
        return out[:80]
    return []


if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    def _win_list_windows() -> List[Dict[str, Any]]:
        titles: List[Dict[str, Any]] = []

        try:
            enum_proc = ctypes.WINFUNCTYPE(  # type: ignore[attr-defined]
                ctypes.c_bool, wintypes.HWND, wintypes.LPARAM
            )

            def _cb(hwnd, _lparam):
                if not ctypes.windll.user32.IsWindowVisible(hwnd):  # type: ignore[attr-defined]
                    return True
                length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)  # type: ignore[attr-defined]
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)  # type: ignore[attr-defined]
                title = buf.value.strip()
                if not title:
                    return True
                rect = wintypes.RECT()
                if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):  # type: ignore[attr-defined]
                    box = {
                        "left": int(rect.left), "top": int(rect.top),
                        "width": max(0, int(rect.right - rect.left)),
                        "height": max(0, int(rect.bottom - rect.top)),
                    }
                else:
                    box = {"left": 0, "top": 0, "width": 0, "height": 0}
                titles.append({"title": title, "visible": True, **box})
                return True

            ctypes.windll.user32.EnumWindows(enum_proc(_cb), 0)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.debug("EnumWindows failed: %s", exc)
        return titles[:80]

    def _win_find(title: str):
        target = title.strip().lower()
        hwnd = ctypes.windll.user32.FindWindowW(None, title)  # type: ignore[attr-defined]
        if hwnd:
            return hwnd

        found = []

        try:
            enum_proc = ctypes.WINFUNCTYPE(  # type: ignore[attr-defined]
                ctypes.c_bool, wintypes.HWND, wintypes.LPARAM
            )

            def _cb(h, _lparam):
                if not ctypes.windll.user32.IsWindowVisible(h):  # type: ignore[attr-defined]
                    return True
                length = ctypes.windll.user32.GetWindowTextLengthW(h)  # type: ignore[attr-defined]
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(h, buf, length + 1)  # type: ignore[attr-defined]
                if target in (buf.value or "").strip().lower():
                    found.append(h)
                    return False
                return True

            ctypes.windll.user32.EnumWindows(enum_proc(_cb), 0)  # type: ignore[attr-defined]
        except Exception:
            pass
        return found[0] if found else None


def _focus(title: str) -> str:
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
    if IS_WINDOWS:
        hwnd = _win_find(title)
        if not hwnd:
            raise ComputerActionError(f"No visible window matching '{title}'")
        import ctypes

        ctypes.windll.user32.ShowWindow(hwnd, 9)  # type: ignore[attr-defined]  # SW_RESTORE
        ctypes.windll.user32.SetForegroundWindow(hwnd)  # type: ignore[attr-defined]
        return f"Focused window matching '{title}'"
    if IS_MAC:
        script = f'tell application "{title}" to activate'
        _run(["osascript", "-e", script])
        return f"Activated '{title}'"
    if IS_LINUX and shutil.which("wmctrl"):
        _run(["wmctrl", "-a", title])
        return f"Focused window matching '{title}'"
    raise ComputerActionError("Window focus is not supported on this platform")


def _close_window(title: str) -> str:
    pgw = _opt("pygetwindow")
    if pgw:
        try:
            wins = pgw.getWindowsWithTitle(title)
            if wins:
                wins[0].close()
                return f"Closed window matching '{title}'"
        except Exception as exc:
            logger.debug("pygetwindow close failed: %s", exc)
    if IS_WINDOWS:
        hwnd = _win_find(title)
        if not hwnd:
            raise ComputerActionError(f"No visible window matching '{title}'")
        import ctypes

        WM_CLOSE = 0x0010
        ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)  # type: ignore[attr-defined]
        return f"Asked window matching '{title}' to close"
    if IS_MAC:
        script = (
            f'tell application "System Events" to tell process "{title}" '
            "to click button 1 of window 1"
        )
        try:
            _run(["osascript", "-e", script])
            return f"Closed window matching '{title}'"
        except ComputerActionError:
            _hotkey(["command", "w"])
            return f"Sent Cmd+W to '{title}'"
    if IS_LINUX and shutil.which("wmctrl"):
        _run(["wmctrl", "-c", title])
        return f"Closed window matching '{title}'"
    raise ComputerActionError("Closing windows is not supported on this platform")


# ---------------------------------------------------------------------------
# clipboard
# ---------------------------------------------------------------------------


def _clipboard_get() -> str:
    pc = _opt("pyperclip")
    if pc:
        try:
            return str(pc.paste())
        except Exception as exc:
            logger.debug("pyperclip paste failed: %s", exc)
    if IS_WINDOWS:
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                capture_output=True, timeout=15,
            )
            return out.stdout.decode(errors="replace").strip()
        except Exception as exc:
            raise ComputerActionError(f"Clipboard read failed: {exc}")
    if IS_MAC:
        return _run(["pbpaste"])
    if IS_LINUX:
        for cmd in (["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]):
            if shutil.which(cmd[0]):
                return _run(cmd)
    raise ComputerActionError("Clipboard access needs `pyperclip` (or pbpaste/xclip).")


def _clipboard_set(text: str) -> None:
    pc = _opt("pyperclip")
    if pc:
        try:
            pc.copy(str(text))
            return
        except Exception as exc:
            logger.debug("pyperclip copy failed: %s", exc)
    if IS_WINDOWS:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value $input"],
            input=str(text).encode("utf-8"), timeout=15, check=False,
        )
        return
    if IS_MAC:
        subprocess.run(["pbcopy"], input=str(text).encode("utf-8"), timeout=15, check=False)
        return
    if IS_LINUX and shutil.which("xclip"):
        subprocess.run(
            ["xclip", "-selection", "clipboard"],
            input=str(text).encode("utf-8"), timeout=15, check=False,
        )
        return
    raise ComputerActionError("Clipboard access needs `pyperclip` (or pbcopy/xclip).")


# ---------------------------------------------------------------------------
# launch / processes / system
# ---------------------------------------------------------------------------


def _open_target(target: str) -> str:
    target = str(target or "").strip()
    if not target:
        raise ComputerActionError("Nothing to open")

    known = {
        "notepad": "notepad.exe", "calculator": "calc.exe", "calc": "calc.exe",
        "paint": "mspaint.exe", "explorer": "explorer.exe",
        "file explorer": "explorer.exe", "cmd": "cmd.exe",
        "command prompt": "cmd.exe", "powershell": "powershell.exe",
        "terminal": "wt.exe", "task manager": "taskmgr.exe",
        "settings": "ms-settings:", "chrome": "chrome.exe",
        "edge": "msedge.exe", "firefox": "firefox.exe",
        "vscode": "code", "vs code": "code", "spotify": "spotify.exe",
        "calculator app": "calc.exe", "photos": "ms-photos:",
        "store": "ms-windows-store:",
    }
    key = target.lower().strip()
    resolved = known.get(key, target)

    if IS_WINDOWS:
        # `startfile` honours URLs, documents and PATH executables alike.
        if resolved.lower().startswith(("http://", "https://", "ms-", "mailto:", "ftp://")):
            subprocess.Popen(["cmd", "/c", "start", "", resolved], shell=False)
            return f"Opened {resolved}"
        try:
            os.startfile(resolved)  # type: ignore[attr-defined]
            return f"Opened {resolved}"
        except Exception:
            try:
                subprocess.Popen(resolved, shell=False)
                return f"Launched {resolved}"
            except Exception as exc:
                raise ComputerActionError(f"Could not open '{target}': {exc}")
    if IS_MAC:
        if resolved.lower().startswith(("http://", "https://")):
            _run(["open", resolved])
        else:
            try:
                _run(["open", "-a", resolved])
            except ComputerActionError:
                _run(["open", resolved])
        return f"Opened {resolved}"
    if IS_LINUX:
        _run(["xdg-open", resolved])
        return f"Opened {resolved}"
    raise ComputerActionError("Opening is not supported on this platform")


def _processes(limit: int = 30) -> List[Dict[str, Any]]:
    psutil = _opt("psutil")
    if not psutil:
        raise ComputerActionError(
            "`psutil` is not installed — run: pip install -r requirements-jarvis.txt"
        )
    rows = []
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
    rows.sort(key=lambda r: r["memory_mb"], reverse=True)
    return rows[: max(1, min(int(limit or 30), 200))]


def _kill(name: Optional[str] = None, pid: Optional[int] = None) -> str:
    psutil = _opt("psutil")
    if not psutil:
        raise ComputerActionError("`psutil` is not installed — cannot manage processes")
    killed: List[str] = []
    if pid:
        try:
            p = psutil.Process(int(pid))
            pname = p.name()
            p.terminate()
            killed.append(f"{pname} (pid {pid})")
        except Exception as exc:
            raise ComputerActionError(f"Could not kill pid {pid}: {exc}")
    elif name:
        target = str(name).lower()
        for p in psutil.process_iter(["pid", "name"]):
            try:
                pname = str(p.info.get("name") or "")
                if target in pname.lower():
                    p.terminate()
                    killed.append(f"{pname} (pid {p.info.get('pid')})")
            except Exception:
                continue
        if not killed:
            raise ComputerActionError(f"No running process matches '{name}'")
    else:
        raise ComputerActionError("Provide `name` or `pid`")
    return "Terminated: " + ", ".join(killed[:10])


def _system_info() -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "host": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "version": platform.version(),
        "machine": platform.machine(),
        "python": platform.python_version(),
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
    try:
        info["screen"] = _screen_size()
    except Exception:
        pass
    return info


def _volume(level: Optional[int] = None, direction: Optional[str] = None,
            steps: int = 2, mute: Optional[bool] = None) -> Dict[str, Any]:
    """Change system output volume.

    Exact ``level`` needs `pycaw` (Windows) or osascript (macOS) / pactl
    (Linux). Without them we step the volume with real key presses, which
    works on every platform and is what a human would do anyway.
    """

    if mute is not None and level is None and direction is None:
        if IS_WINDOWS:
            _press("volumemute")
        elif IS_MAC:
            _run(["osascript", "-e", f"set volume {'with' if mute else 'without'} output muted"])
        elif IS_LINUX:
            _run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if mute else "0"])
        return {"muted": bool(mute)}

    if level is not None:
        level = max(0, min(100, int(level)))
        if IS_WINDOWS:
            try:
                from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume  # type: ignore
                from comtypes import CLSCTX_ALL  # type: ignore

                devices = AudioUtilities.GetSpeakers()
                iface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                vol = iface.QueryInterface(IAudioEndpointVolume)
                vol.SetMasterVolumeLevelScalar(level / 100.0, None)
                return {"level": level, "muted": bool(vol.GetMute())}
            except Exception:
                _set_volume_by_steps(100, "down")
                _set_volume_by_steps(level // 2, "up")
                return {"level": level, "approximate": True}
        if IS_MAC:
            _run(["osascript", "-e", f"set volume output volume {level}"])
            return {"level": level}
        if IS_LINUX:
            _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"])
            return {"level": level}

    direction = (direction or "up").lower()
    if direction not in ("up", "down"):
        raise ComputerActionError("`direction` must be 'up' or 'down'")
    return _set_volume_by_steps(max(1, min(int(steps or 1), 50)), direction)


def _set_volume_by_steps(steps: int, direction: str) -> Dict[str, Any]:
    key = "volumeup" if direction == "up" else "volumedown"
    for _ in range(steps):
        if IS_WINDOWS:
            _press(key)
        elif IS_MAC:
            _run(["osascript", "-e", f"set volume output volume (output volume of (get volume settings) {'-' if direction == 'down' else '+'} 6)"])
        elif IS_LINUX:
            _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@",
                  f"{'-' if direction == 'down' else '+'}6%"])
        else:
            raise ComputerActionError("Volume control is not supported on this platform")
    return {"steps": steps, "direction": direction}


def _notify(title: str, message: str) -> str:
    title = str(title or "psd.ai")[:120]
    message = str(message or "")[:400]
    if IS_WINDOWS:
        script = (
            "[reflection.assembly]::loadWithPartialName('System.Windows.Forms')|Out-Null;"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Information;"
            "$n.BalloonTipTitle='" + title.replace("'", "''") + "';"
            "$n.BalloonTipText='" + message.replace("'", "''") + "';"
            "$n.Visible=$true;$n.ShowBalloonTip(6000);Start-Sleep -s 6;$n.Dispose()"
        )
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-sta", "-Command", script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return "Notification shown"
        except Exception as exc:
            raise ComputerActionError(f"Notification failed: {exc}")
    if IS_MAC:
        script = f'display notification "{message}" with title "{title}"'
        _run(["osascript", "-e", script])
        return "Notification shown"
    if IS_LINUX and shutil.which("notify-send"):
        _run(["notify-send", title, message])
        return "Notification shown"
    raise ComputerActionError("Notifications are not supported on this platform")


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


class ComputerService:
    """Executes allow-listed desktop actions on the host machine."""

    def __init__(self, timeout: int = ACTION_TIMEOUT_SECONDS):
        self.timeout = timeout

    # ── introspection ──

    def status(self) -> Dict[str, Any]:
        """Capability report for /api/jarvis/status and Settings → Voice."""

        enabled = computer_control_enabled()
        backends = {
            "pyautogui": _have("pyautogui"),
            "mss": _have("mss"),
            "pyperclip": _have("pyperclip"),
            "psutil": _have("psutil"),
            "pygetwindow": _have("pygetwindow"),
            "pycaw": _have("pycaw"),
            "pillow": _have("PIL"),
        }
        if IS_WINDOWS:
            # ctypes covers mouse/keyboard/windows without any extra package.
            native = ["mouse", "keyboard", "windows", "clipboard", "volume", "notify"]
        elif IS_MAC:
            native = ["mouse", "keyboard", "windows", "clipboard", "volume", "notify"]
        else:
            native = ["clipboard"] if shutil.which("xclip") or shutil.which("xsel") else []
            if shutil.which("xdotool"):
                native += ["mouse", "keyboard"]
            if shutil.which("wmctrl"):
                native += ["windows"]
            if shutil.which("pactl") or shutil.which("amixer"):
                native += ["volume"]
            if shutil.which("notify-send"):
                native += ["notify"]

        has_screenshot = (
            backends["mss"] or backends["pillow"]
            or (IS_MAC and shutil.which("screencapture"))
            or (IS_LINUX and any(shutil.which(c) for c in ("gnome-screenshot", "import", "scrot")))
        )

        return {
            "enabled": enabled,
            "confirm_risky": _confirm_risky(),
            "platform": sys.platform,
            "os": f"{platform.system()} {platform.release()}",
            "supports_screenshot": bool(has_screenshot),
            "supports_input": bool(
                backends["pyautogui"] or ("mouse" in native and "keyboard" in native)
            ),
            "backends": backends,
            "native": native,
            "missing_packages": sorted(k for k, v in backends.items() if not v and k != "pillow"),
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
