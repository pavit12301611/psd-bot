"""The Linux/Wayland backend layer, verified without a compositor.

CI runs on a headless Ubuntu runner, so none of these tools exist there — and
none of them need to. Every case drives :mod:`services.computer.service` with a
fake `which` and a fake `run`, then asserts on the *command line* that would be
executed. That is the part worth pinning down: ydotool's keycode protocol, its
click bitmask, the grim/portal split between wlroots and GNOME, PipeWire's
`wpctl` verbs, and the fact that pyautogui must never be used on Wayland.
"""

import json
import struct
import subprocess

import pytest

from services.computer import service as svc
from services.computer.service import ComputerActionError

WAYLAND_TOOLS = [
    "ydotool", "wtype", "grim", "slurp", "wl-copy", "wl-paste", "wpctl",
    "notify-send", "xdg-open", "gtk-launch", "gio", "gdbus", "swaymsg",
    "hyprctl", "wlr-randr",
]
X11_TOOLS = [
    "xdotool", "wmctrl", "xclip", "xsel", "scrot", "pactl", "notify-send",
    "xdg-open", "gtk-launch", "gdbus", "xrandr",
]

SWAY_TREE = {
    "name": "root",
    "type": "root",
    "nodes": [
        {
            "name": None,
            "type": "output",
            "nodes": [
                {
                    "name": "Mozilla Firefox",
                    "type": "con",
                    "visible": True,
                    "focused": True,
                    "app_id": "firefox",
                    "rect": {"x": 10, "y": 20, "width": 1200, "height": 800},
                    "nodes": [],
                },
                {
                    "name": "  ",
                    "type": "con",
                    "app_id": "empty",
                    "rect": {},
                    "nodes": [],
                },
            ],
        }
    ],
    "floating_nodes": [
        {
            "name": "Picture-in-Picture",
            "type": "floating_con",
            "visible": True,
            "focused": False,
            "app_id": "firefox",
            "rect": {"x": 0, "y": 0, "width": 400, "height": 300},
            "nodes": [],
        }
    ],
}

SWAY_OUTPUTS = [
    {"name": "eDP-1", "focused": False, "rect": {"width": 1366, "height": 768}},
    {"name": "DP-2", "focused": True, "rect": {"width": 2560, "height": 1440}},
]

HYPR_CLIENTS = [
    {
        "title": "kitty", "class": "kitty", "at": [0, 0], "size": [1920, 1080],
        "workspace": {"name": "1"}, "focusHistoryID": 0, "hidden": False,
    },
    {"title": "", "class": "ghost", "at": [0, 0], "size": [1, 1]},
]

HYPR_MONITORS = [
    {"name": "eDP-1", "width": 1920, "height": 1080, "focused": True},
]

WLR_RANDR = "eDP-1 \"Unknown\"\n  Enabled: yes\n  Mode: 1920x1080 px\n"

XRANDR = (
    "Screen 0: minimum 8 x 8, current 1920 x 1080, maximum 32767 x 32767\n"
    "eDP1 connected primary 1920x1080+0+0\n"
)


def _fake_png(width: int = 1920, height: int = 1080) -> bytes:
    """A header-valid PNG stub — enough for `_png_size` to measure."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
        + struct.pack(">I", 0)
    )


def canned(cmd, _stdin=None):
    """Plausible stdout for the tools these tests drive."""
    argv = [str(part) for part in cmd]
    joined = " ".join(argv)
    if "get_tree" in joined:
        return json.dumps(SWAY_TREE)
    if "get_outputs" in joined:
        return json.dumps(SWAY_OUTPUTS)
    if "hyprctl" in joined and "clients" in joined:
        return json.dumps(HYPR_CLIENTS)
    if "hyprctl" in joined and "monitors" in joined:
        return json.dumps(HYPR_MONITORS)
    if argv[:1] == ["wlr-randr"]:
        return WLR_RANDR
    if argv[:1] == ["xrandr"]:
        return XRANDR
    if "wpctl" in joined and "get-volume" in joined:
        return "Volume: 0.40"
    if "pactl" in joined and "get-sink-volume" in joined:
        return "Volume: front-left: 26214 /  40% / -23.90 dB"
    if "pactl" in joined and "get-sink-mute" in joined:
        return "Mute: no"
    if "wl-paste" in joined:
        return "copied text"
    if "xdpyinfo" in joined:
        return "  dimensions:    1920x1080 pixels (508x285 millimeters)\n"
    if "xdotool" in joined and ("search" in joined or "getwindowname" in joined):
        return "12345678\n"
    if "wmctrl" in joined and "-lG" in joined:
        return "0x03200003  0 10   20   1200 800  user@host Mozilla Firefox\n"
    if "Screenshot.Screenshot" in joined:
        return "(('/org/freedesktop/portal/desktop/request/1_42/psd_ai1',),)"
    return ""


class Harness:
    """Fake `which` + fake `run`, recording every command line."""

    def __init__(self, tools, impl=None):
        self.tools = set(tools)
        self.calls = []
        self.stdins = []
        self.popen = []
        self.failures = {}
        self.impl = impl or canned

    # ── patched module surface ──

    def which(self, name):
        return f"/usr/bin/{name}" if name in self.tools else None

    def run(self, cmd, timeout=15, stdin_text=None):
        self.calls.append([str(part) for part in cmd])
        self.stdins.append(stdin_text)
        head = str(cmd[0])
        if head in self.failures:
            raise ComputerActionError(self.failures[head])
        result = self.impl(list(cmd), stdin_text)
        if isinstance(result, Exception):
            raise result
        return result

    def capture(self, cmd):
        self.calls.append([str(part) for part in cmd])
        return _fake_png()

    def run_subprocess(self, cmd, **kwargs):
        """Stand-in for subprocess.run: clipboard tools are called directly."""
        payload = kwargs.get("input")
        self.calls.append([str(part) for part in cmd])
        self.stdins.append(payload)
        head = str(cmd[0])
        if head in self.failures:
            raise ComputerActionError(self.failures[head])
        out = self.impl(list(cmd), payload)
        if isinstance(out, bytes):
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr=b"")
        return subprocess.CompletedProcess(cmd, 0, stdout=(out or "").encode(),
                                           stderr=b"")

    def fork(self, argv, *args, **kwargs):
        self.popen.append([str(part) for part in argv])

        class _Done:
            returncode = 0

        return _Done()

    # ── assertions helpers ──

    @property
    def argv(self):
        return [" ".join(call) for call in self.calls]

    def first(self, tool):
        for call in self.calls:
            if call and call[0] == tool:
                return call
        return None

    def count(self, tool):
        return sum(1 for call in self.calls if call and call[0] == tool)


@pytest.fixture
def build(monkeypatch):
    """`build(session="wayland", compositor="sway", tools=[...])` -> Harness."""

    def make(session="wayland", desktop="", compositor="", tools=None,
             impl=None, with_popen=False):
        if tools is None:
            tools = WAYLAND_TOOLS if session == "wayland" else X11_TOOLS
        harness = Harness(tools, impl=impl)

        monkeypatch.setattr(svc, "_tool", harness.which)
        monkeypatch.setattr(svc, "_run", harness.run)
        monkeypatch.setattr(svc, "_run_capture", harness.capture)
        monkeypatch.setattr(svc.subprocess, "run", harness.run_subprocess)
        monkeypatch.setattr(svc.time, "sleep", lambda *_a, **_k: None)
        monkeypatch.setattr(svc, "_MOUSEMOVE_FORM", None)
        svc._BACKEND_CACHE.clear()

        # Session environment. Everything the port reads comes from here.
        for name in ("XDG_SESSION_TYPE", "WAYLAND_DISPLAY", "DISPLAY",
                     "XDG_CURRENT_DESKTOP", "DESKTOP_SESSION", "SWAYSOCK",
                     "HYPRLAND_INSTANCE_SIGNATURE", "YDOTOOL_SOCKET"):
            monkeypatch.delenv(name, raising=False)
        if session == "wayland":
            monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
            monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        else:
            monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
            monkeypatch.setenv("DISPLAY", ":1")
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", desktop)
        if compositor == "sway":
            monkeypatch.setenv("SWAYSOCK", "/run/user/1000/sway-ipc.1000.sock")
        if compositor == "hyprland":
            monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "deadbeef")
        if with_popen:
            monkeypatch.setattr(svc.subprocess, "Popen", harness.fork)
        return harness

    yield make
    svc._BACKEND_CACHE.clear()


# ─────────────────────────────────────────────────────────────────────────────
# keycodes: ydotool speaks Linux input-event codes, nothing else
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,code",
    [
        ("enter", 28), ("esc", 1), ("escape", 1), ("tab", 15), ("space", 57),
        ("a", 30), ("z", 44), ("0", 11), ("1", 2), ("f4", 62), ("f12", 88),
        ("ctrl", 29), ("control", 29), ("alt", 56), ("shift", 42),
        ("super", 125), ("win", 125), ("cmd", 125), ("meta", 125),
        ("up", 103), ("down", 108), ("left", 105), ("right", 106),
        ("home", 102), ("end", 107), ("pageup", 104), ("pagedown", 109),
        ("delete", 111), ("backspace", 14), ("printscreen", 99),
        ("volumeup", 115), ("volumedown", 114), ("mute", 113),
    ],
)
def test_linux_keycodes_match_input_event_codes(name, code):
    assert svc._linux_keycode(name) == code


def test_unknown_key_is_rejected_with_its_name():
    with pytest.raises(ComputerActionError) as excinfo:
        svc._linux_keycode("teleport")
    assert "teleport" in str(excinfo.value)


def test_x11_keysyms_use_the_names_x_accepts():
    # XKeysymFromString is case-sensitive: "esc" would fail, "Escape" works.
    assert svc._x11_keysym("enter") == "Return"
    assert svc._x11_keysym("esc") == "Escape"
    assert svc._x11_keysym("pageup") == "Page_Up"
    assert svc._x11_keysym("super") == "Super_L"
    assert svc._x11_keysym("ctrl") == "ctrl"
    assert svc._x11_keysym("a") == "a"
    assert svc._x11_keysym("f7") == "F7"


# ─────────────────────────────────────────────────────────────────────────────
# Wayland input via ydotool
# ─────────────────────────────────────────────────────────────────────────────


def test_wayland_move_uses_absolute_ydotool(build):
    h = build()
    svc._move(640, 480)
    call = h.first("ydotool")
    assert call[:3] == ["ydotool", "mousemove", "--absolute"]
    assert call[3:] == ["640", "480"]


def test_wayland_move_falls_back_to_the_flagged_syntax(build):
    """Some ydotool builds want `-x/-y`; the working form is then remembered."""
    h = build()
    h.failures["ydotool"] = "unrecognised argument"
    calls = []

    def impl(cmd, _stdin=None):
        calls.append([str(c) for c in cmd])
        if "--absolute" in calls[-1] and "-x" not in calls[-1]:
            raise ComputerActionError("unrecognised option")
        return ""

    h.impl = impl
    del h.failures["ydotool"]
    svc._move(10, 20)
    assert svc._MOUSEMOVE_FORM == "flagged"
    assert calls[-1] == ["ydotool", "mousemove", "--absolute", "-x", "10", "-y", "20"]


@pytest.mark.parametrize(
    "button,bitmask", [("left", "0xC0"), ("right", "0xC1"), ("middle", "0xC2")]
)
def test_wayland_click_bitmasks(build, button, bitmask):
    h = build()
    svc._click(None, None, button, 1)
    assert h.first("ydotool") == ["ydotool", "click", bitmask]


def test_wayland_click_moves_first_and_repeats(build):
    h = build()
    svc._click(100, 200, "left", 3)
    assert h.calls[0][:3] == ["ydotool", "mousemove", "--absolute"]
    assert h.calls[1] == ["ydotool", "click", "--repeat", "3", "--next-delay", "25", "0xC0"]


def test_wayland_drag_holds_the_button_while_moving(build):
    h = build()
    svc._drag(0, 0, 100, 100, duration=0.2)
    bitmasks = [c[-1] for c in h.calls if c[1:2] == ["click"]]
    # Left button down, then up — with pointer moves in between.
    assert bitmasks[0] == "0x40"
    assert bitmasks[-1] == "0x80"
    moves = [c for c in h.calls if c[1:2] == ["mousemove"]]
    assert len(moves) >= 3, moves


def test_wayland_hotkey_is_a_keycode_chord(build):
    h = build()
    svc._hotkey(["ctrl", "alt", "f4"])
    # modifiers down in order, then released in reverse — exactly like a hand.
    assert h.first("ydotool") == [
        "ydotool", "key", "29:1", "56:1", "62:1", "62:0", "56:0", "29:0"
    ]


def test_wayland_hotkey_falls_back_to_wtype(build):
    h = build(tools=["wtype"])
    svc._hotkey(["ctrl", "c"])
    assert h.first("wtype") == ["wtype", "-M", "ctrl", "-k", "c", "-m", "ctrl"]


def test_wayland_type_streams_ascii_through_ydotool(build):
    h = build()
    svc._type_text("hello world")
    assert h.first("ydotool") == ["ydotool", "type", "-d", "8", "-f", "-"]
    assert h.stdins[0] == "hello world"


def test_wayland_type_prefers_wtype_for_unicode(build):
    """Devanagari has no US keycode; libxkbcommon handles it correctly."""
    h = build()
    svc._type_text("नमस्ते")
    assert h.calls[0][0] == "wtype"
    assert h.calls[0] == ["wtype", "--", "नमस्ते"]


def test_wayland_type_rejects_oversized_text(build):
    h = build()
    with pytest.raises(ComputerActionError) as excinfo:
        svc._type_text("x" * (svc.MAX_TYPED_CHARS + 1))
    assert "limit" in str(excinfo.value)
    assert h.calls == []


def test_pyautogui_is_never_used_on_wayland(build, monkeypatch):
    """pyautogui drives XWayland: it would report success and move nothing."""
    h = build()
    seen = []

    class FakePyautogui:
        @staticmethod
        def moveTo(*args, **kwargs):
            seen.append(("moveTo", args, kwargs))

        @staticmethod
        def click(*args, **kwargs):
            seen.append(("click", args, kwargs))

    monkeypatch.setattr(svc, "_opt", lambda name: FakePyautogui
                        if name == "pyautogui" else None)
    svc._move(5, 5)
    svc._click(None, None, "left", 1)
    assert seen == []
    assert h.count("ydotool") == 2


def test_no_input_backend_explains_the_dnf_fix(build):
    h = build(tools=[])
    with pytest.raises(ComputerActionError) as excinfo:
        svc._move(1, 1)
    message = str(excinfo.value)
    assert "wayland" in message.lower()
    assert "sudo dnf install" in message


def test_ydotoold_socket_is_located(build, monkeypatch, tmp_path):
    socket_path = tmp_path / ".ydotool_socket"
    socket_path.write_text("")
    monkeypatch.setenv("YDOTOOL_SOCKET", str(socket_path))
    assert svc._ydotool_socket() == str(socket_path)

    monkeypatch.setenv("YDOTOOL_SOCKET", str(tmp_path / "missing"))
    assert svc._ydotool_socket() is None


def test_ydotool_socket_errors_become_a_daemon_hint(build):
    h = build()
    h.failures["ydotool"] = "failed to connect to socket"
    with pytest.raises(ComputerActionError) as excinfo:
        svc._move(1, 1)
    assert "ydotoold" in str(excinfo.value)


def test_wayland_scroll_uses_page_keys_for_long_scrolls(build):
    """No wheel injection exists on Wayland; Page keys are the stand-in."""
    h = build()
    svc._scroll(-24)
    codes = {c[2].split(":")[0] for c in h.calls if c[1:2] == ["key"]}
    assert codes == {str(svc._linux_keycode("pagedown"))}
    assert h.count("ydotool") == 3  # 24 notches / 8 per page


def test_wayland_scroll_uses_arrows_for_a_single_notch(build):
    h = build()
    svc._scroll(2)
    codes = [c[2].split(":")[0] for c in h.calls if c[1:2] == ["key"]]
    assert codes == [str(svc._linux_keycode("up"))] * 2


# ─────────────────────────────────────────────────────────────────────────────
# X11 input via xdotool
# ─────────────────────────────────────────────────────────────────────────────


def test_x11_input_still_goes_through_xdotool(build):
    h = build(session="x11")
    svc._move(640, 480)
    svc._click(None, None, "right", 1)
    svc._hotkey(["ctrl", "shift", "esc"])
    assert h.first("xdotool") == ["xdotool", "mousemove", "640", "480"]
    assert ["xdotool", "click", "3"] in h.calls
    assert ["xdotool", "key", "--clearmodifiers", "ctrl+shift+Escape"] in h.calls


def test_x11_scroll_is_a_wheel_button(build):
    h = build(session="x11")
    svc._scroll(3)
    assert h.count("xdotool") == 3
    assert all(call == ["xdotool", "click", "4"] for call in h.calls)


def test_x11_without_xdotool_reports_the_package(build):
    h = build(session="x11", tools=[])
    with pytest.raises(ComputerActionError) as excinfo:
        svc._click(None, None, "left", 1)
    assert "sudo dnf install xdotool" in str(excinfo.value)


# ─────────────────────────────────────────────────────────────────────────────
# screen geometry + screenshots
# ─────────────────────────────────────────────────────────────────────────────


def test_sway_screen_size_comes_from_the_compositor(build):
    h = build(compositor="sway")
    assert svc._screen_size() == {"width": 2560, "height": 1440}


def test_hyprland_screen_size_comes_from_the_compositor(build):
    h = build(compositor="hyprland")
    assert svc._screen_size() == {"width": 1920, "height": 1080}


def test_gnome_wayland_screen_size_falls_back_to_wlr_randr(build):
    h = build(desktop="GNOME", tools=["wlr-randr", "grim", "gdbus"])
    assert svc._screen_size() == {"width": 1920, "height": 1080}


def test_wayland_screen_size_can_be_measured_from_a_capture(build):
    h = build(desktop="GNOME", tools=["grim", "gdbus"])
    assert svc._screen_size() == {"width": 1920, "height": 1080}
    assert h.first("grim")[:2] == ["grim", "-t"]


def test_x11_screen_size_falls_back_to_xrandr(build, monkeypatch):
    h = build(session="x11")
    monkeypatch.setattr(svc, "_opt", lambda _name: None)
    assert svc._screen_size() == {"width": 1920, "height": 1080}


def test_wlroots_capture_uses_grim(build):
    h = build(compositor="sway")
    data = svc._screenshot_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert h.first("grim") == ["grim", "-t", "png", "-"]


def test_grim_can_target_one_output(build):
    """`monitor=1` means the focused output, the way mss numbers them."""
    h = build(compositor="sway", tools=WAYLAND_TOOLS + ["wlr-randr"])
    svc._screenshot_bytes(monitor=1)
    assert h.first("grim")[:4] == ["grim", "-o", "DP-2", "-t"]

    h2 = build(compositor="sway", tools=WAYLAND_TOOLS + ["wlr-randr"])
    h2.calls.clear()
    svc._screenshot_bytes(monitor=2)
    assert h2.first("grim")[:4] == ["grim", "-o", "eDP-1", "-t"]


def test_gnome_wayland_capture_prefers_the_portal(build, monkeypatch):
    """GNOME has no wlr-screencopy: only the portal can capture there."""
    h = build(desktop="GNOME")
    portal_calls = []

    def fake_portal():
        portal_calls.append(True)
        return _fake_png()

    monkeypatch.setattr(svc, "_portal_screenshot", fake_portal)
    assert svc._screenshot_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert portal_calls == [True]
    assert h.first("grim") is None


def test_grim_failure_falls_back_to_the_portal(build, monkeypatch):
    h = build(compositor="sway")
    h.failures["grim"] = "compositor does not support wlr-screencopy"
    monkeypatch.setattr(svc, "_portal_screenshot", lambda: _fake_png())
    assert svc._screenshot_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_capture_with_no_backend_lists_the_tools(build):
    h = build(tools=[])
    with pytest.raises(ComputerActionError) as excinfo:
        svc._screenshot_bytes()
    message = str(excinfo.value)
    assert "grim" in message and "gdbus" in message
    assert "sudo dnf install" in message


def test_x11_capture_prefers_scrot(build, monkeypatch):
    h = build(session="x11")
    monkeypatch.setattr(svc, "_opt", lambda _name: None)
    svc._screenshot_bytes()
    assert h.first("scrot") == ["scrot", "-o", "-"]


def test_png_size_reads_the_ihdr(build):
    assert svc._png_size(_fake_png(800, 600)) == {"width": 800, "height": 600}
    assert svc._png_size(b"not a png") is None


# ─────────────────────────────────────────────────────────────────────────────
# windows
# ─────────────────────────────────────────────────────────────────────────────


def test_sway_windows_come_from_the_ipc_tree(build):
    h = build(compositor="sway")
    windows = svc._windows()
    titles = [w["title"] for w in windows]
    assert titles == ["Mozilla Firefox", "Picture-in-Picture"]
    assert windows[0]["focused"] is True
    assert windows[0]["width"] == 1200
    assert windows[0]["app_id"] == "firefox"


def test_hyprland_windows_come_from_clients(build):
    h = build(compositor="hyprland")
    windows = svc._windows()
    assert [w["title"] for w in windows] == ["kitty"]
    assert windows[0]["workspace"] == "1"
    assert windows[0]["width"] == 1920


def test_gnome_wayland_lists_no_windows_without_lying(build):
    """GNOME keeps its tree private; an empty list beats a fabricated one."""
    h = build(desktop="GNOME")
    assert svc._windows() == []


def test_gnome_wayland_focus_says_why_it_cannot(build):
    h = build(desktop="GNOME")
    with pytest.raises(ComputerActionError) as excinfo:
        svc._focus("Firefox")
    assert "Wayland" in str(excinfo.value)


def test_sway_focus_uses_window_criteria(build):
    h = build(compositor="sway")
    svc._focus("Firefox")
    call = h.first("swaymsg")
    assert call[:3] == ["swaymsg", "-t", "command"]
    assert '[title~"Firefox"] focus' in call[3]


def test_sway_focus_escapes_regex_metacharacters(build):
    h = build(compositor="sway")
    svc._focus("report (final).odt")
    call = h.first("swaymsg")
    assert "\\(" in call[3] and "\\." in call[3]


def test_hyprland_close_dispatches_to_the_compositor(build):
    h = build(compositor="hyprland")
    svc._close_window("kitty")
    assert h.first("hyprctl") == [
        "hyprctl", "dispatch", "closewindow", "title:kitty"
    ]


def test_x11_windows_use_wmctrl(build, monkeypatch):
    h = build(session="x11")
    monkeypatch.setattr(svc, "_opt", lambda _name: None)
    windows = svc._windows()
    assert windows[0]["title"] == "Mozilla Firefox"
    assert windows[0]["width"] == 1200


def test_x11_focus_falls_back_to_xdotool(build, monkeypatch):
    h = build(session="x11", tools=["xdotool"])
    monkeypatch.setattr(svc, "_opt", lambda _name: None)
    svc._focus("Firefox")
    assert ["xdotool", "search", "--name", "Firefox"] in h.calls
    assert any(call[:2] == ["xdotool", "windowactivate"] for call in h.calls)


def test_empty_title_never_reaches_the_compositor(build):
    h = build(compositor="sway")
    with pytest.raises(ComputerActionError):
        svc._focus("   ")
    assert h.calls == []


# ─────────────────────────────────────────────────────────────────────────────
# clipboard
# ─────────────────────────────────────────────────────────────────────────────


def test_wayland_clipboard_uses_wl_clipboard(build):
    h = build()
    assert svc._clipboard_get() == "copied text"
    assert h.first("wl-paste") == ["wl-paste", "--no-newline"]

    svc._clipboard_set("paste me")
    assert h.first("wl-copy") == ["wl-copy", "--", "paste me"]


def test_wayland_clipboard_without_wl_clipboard_names_the_package(build):
    h = build(tools=["ydotool"])
    with pytest.raises(ComputerActionError) as excinfo:
        svc._clipboard_get()
    assert "sudo dnf install wl-clipboard" in str(excinfo.value)


def test_x11_clipboard_uses_xclip(build, monkeypatch):
    h = build(session="x11")
    monkeypatch.setattr(svc, "_opt", lambda _name: None)
    svc._clipboard_set("hi")
    assert h.first("xclip") == ["xclip", "-selection", "clipboard"]
    assert h.stdins[-1] == b"hi"


def test_x11_clipboard_read_uses_xclip_then_xsel(build, monkeypatch):
    h = build(session="x11", tools=["xsel"])
    monkeypatch.setattr(svc, "_opt", lambda _name: None)
    svc._clipboard_get()
    assert h.first("xsel") == ["xsel", "--clipboard", "--output"]


# ─────────────────────────────────────────────────────────────────────────────
# audio, notifications, launching
# ─────────────────────────────────────────────────────────────────────────────


def test_volume_level_uses_pipewire(build):
    h = build()
    result = svc._volume(level=40)
    assert h.first("wpctl") == ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "40%"]
    assert result["level"] == 40
    assert result["muted"] is False


def test_volume_mute_uses_pipewire(build):
    h = build()
    assert svc._volume(mute=True)["muted"] is True
    assert h.first("wpctl") == ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"]


def test_volume_steps_use_relative_pipewire_verbs(build):
    h = build()
    svc._set_volume_by_steps(2, "down")
    assert h.calls[0] == ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "6%-"]
    assert h.count("wpctl") >= 2


def test_volume_falls_back_to_pactl_without_pipewire(build):
    h = build(tools=["pactl"])
    svc._volume(level=25)
    assert h.first("pactl") == ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "25%"]


def test_volume_falls_back_to_real_keys_without_a_mixer(build):
    h = build(tools=["ydotool"])
    result = svc._set_volume_by_steps(2, "up")
    assert result["backend"] == "keys"
    codes = {c[2].split(":")[0] for c in h.calls if c[1:2] == ["key"]}
    assert codes == {str(svc._linux_keycode("volumeup"))}


def test_volume_rejects_a_bad_direction(build):
    h = build()
    with pytest.raises(ComputerActionError):
        svc._volume(direction="sideways")


def test_notification_uses_notify_send(build):
    h = build()
    assert svc._notify("Done", "3 files converted") == "Notification shown"
    call = h.first("notify-send")
    assert call[:3] == ["notify-send", "--app-name", "psd.ai"]
    assert call[-2:] == ["Done", "3 files converted"]


def test_notification_without_notify_send_names_the_package(build):
    h = build(tools=["ydotool"])
    with pytest.raises(ComputerActionError) as excinfo:
        svc._notify("Done", "x")
    assert "sudo dnf install libnotify" in str(excinfo.value)


def test_notepad_maps_onto_a_fedora_text_editor(build):
    """People say "notepad"; Fedora ships gnome-text-editor."""
    h = build(tools=WAYLAND_TOOLS + ["gnome-text-editor"], with_popen=True)
    assert svc._open_target("notepad") == "Opened gnome-text-editor"
    # The resolved absolute path is what gets exec'd, not a bare name.
    assert h.popen == [["/usr/bin/gnome-text-editor"]]


def test_app_id_launchers_go_through_gtk_launch(build, tmp_path, monkeypatch):
    """A reverse-DNS app id has no binary: launch its .desktop file instead."""
    apps = tmp_path / "applications"
    apps.mkdir()
    (apps / "org.gnome.Calculator.desktop").write_text("[Desktop Entry]\nName=Calculator\n")
    monkeypatch.setattr(svc, "_DESKTOP_DIRS", (str(apps),))
    h = build(tools=WAYLAND_TOOLS + ["gtk-launch"])
    assert "Opened" in svc._open_target("org.gnome.Calculator")
    assert h.first("gtk-launch") == ["gtk-launch", "org.gnome.Calculator"]


def test_dnf_hint_is_a_package_name_not_an_app_id(build):
    h = build(tools=["xdg-open"])
    with pytest.raises(ComputerActionError) as excinfo:
        svc._open_target("photos")
    # "loupe" is the Fedora package; "org.gnome.Loupe" is not.
    assert "sudo dnf install loupe" in str(excinfo.value)


def test_known_app_missing_everywhere_reports_dnf(build):
    h = build(tools=["xdg-open", "gtk-launch"])
    with pytest.raises(ComputerActionError) as excinfo:
        svc._open_target("calculator")
    assert "sudo dnf install gnome-calculator" in str(excinfo.value)


def test_url_is_handed_to_xdg_open(build):
    h = build(with_popen=True)
    svc._open_target("https://fedoraproject.org")
    assert h.popen[0] == ["xdg-open", "https://fedoraproject.org"]


def test_binary_on_path_is_executed_directly(build):
    h = build(tools=["xdg-open", "ffmpeg"], with_popen=True)
    assert svc._open_target("ffmpeg") == "Launched ffmpeg"
    assert h.popen == [["/usr/bin/ffmpeg"]]


def test_local_file_is_handed_to_xdg_open(build, tmp_path):
    h = build(with_popen=True)
    document = tmp_path / "report.odt"
    document.write_text("x")
    assert svc._open_target(str(document)) == f"Opened {document}"
    assert h.popen[0][0] == "xdg-open"


def test_unknown_target_is_reported_not_executed(build):
    h = build(tools=[], with_popen=True)
    with pytest.raises(ComputerActionError) as excinfo:
        svc._open_target("definitely-not-an-app")
    assert "dnf search" in str(excinfo.value)
    assert h.popen == []


def test_destructive_target_never_reaches_a_launcher(build):
    """The policy runs before the handler; this is the second line of defence."""
    h = build(with_popen=True)
    from services.computer.policy import check_action

    allowed, _reason, _risk, _confirm = check_action(
        "open", {"target": "sudo rm -rf /"}, confirm_risky=False
    )
    assert allowed is False
    assert h.popen == []


# ─────────────────────────────────────────────────────────────────────────────
# processes and system facts
# ─────────────────────────────────────────────────────────────────────────────


def test_processes_read_proc_without_psutil(build, monkeypatch):
    monkeypatch.setattr(svc, "_opt", lambda _name: None)
    rows = [{"pid": 1, "name": "systemd", "memory_mb": 12.0},
            {"pid": 2, "name": "kthreadd", "memory_mb": 0.0},
            {"pid": 900, "name": "llama-server", "memory_mb": 8000.0}]
    monkeypatch.setattr(svc, "_proc_rows", lambda: rows)
    result = svc._processes(limit=2)
    assert [row["pid"] for row in result] == [900, 1]


def test_kill_uses_sigterm_from_proc_without_psutil(build, monkeypatch):
    monkeypatch.setattr(svc, "_opt", lambda _name: None)
    monkeypatch.setattr(svc, "_proc_rows",
                        lambda: [{"pid": 4242, "name": "gedit", "memory_mb": 1.0}])
    signals = []
    monkeypatch.setattr(svc.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    assert "4242" in svc._kill(name="gedit")
    import signal as _signal
    assert signals == [(4242, _signal.SIGTERM)]


def test_kill_reports_a_missing_process(build, monkeypatch):
    monkeypatch.setattr(svc, "_opt", lambda _name: None)
    monkeypatch.setattr(svc, "_proc_rows", lambda: [])
    with pytest.raises(ComputerActionError):
        svc._kill(name="nothing-here")


def test_system_info_reports_the_fedora_session(build, monkeypatch):
    build(compositor="sway")
    monkeypatch.setattr(svc, "_opt", lambda _name: None)
    monkeypatch.setattr(svc, "_proc_based_system_info", lambda: {"ram_total_gb": 32.0})
    info = svc._system_info()
    assert info["session"] == "wayland"
    assert info["compositor"] == "sway"
    assert info["ram_total_gb"] == 32.0
    assert info["screen"] == {"width": 2560, "height": 1440}


def test_proc_based_system_info_reads_meminfo_and_uptime(build, monkeypatch, tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       32212254 kB\nMemAvailable:   16106127 kB\n")
    uptime = tmp_path / "uptime"
    uptime.write_text("3600.42 7200.00\n")

    def read_first(path):
        if path == "/proc/meminfo":
            return meminfo.read_text()
        if path == "/proc/uptime":
            return uptime.read_text()
        return ""

    monkeypatch.setattr(svc, "_read_first", read_first)
    info = svc._proc_based_system_info()
    assert info["ram_total_gb"] == round(32212254 / (1024 ** 2), 1)
    assert info["ram_percent"] == 50.0
    assert info["uptime_minutes"] == 60


# ─────────────────────────────────────────────────────────────────────────────
# status()
# ─────────────────────────────────────────────────────────────────────────────


def test_status_on_sway_reports_a_working_desktop(build, monkeypatch):
    monkeypatch.setenv("PSD_AI_COMPUTER_CONTROL", "1")
    monkeypatch.setattr(svc, "_ydotool_socket", lambda: "/run/user/1000/.ydotool_socket")
    build(compositor="sway")
    status = svc.ComputerService().status()
    assert status["wayland"] is True
    assert status["compositor"] == "sway"
    assert status["session"] == "wayland"
    assert status["supports_input"] is True
    assert status["supports_windows"] is True
    assert status["supports_screenshot"] is True
    assert status["supports_clipboard"] is True
    assert status["supports_volume"] is True
    assert status["supports_notify"] is True
    assert status["missing_tools"] == []
    assert status["install_hint"] == ""


def test_status_on_gnome_wayland_warns_about_the_portal(build):
    h = build(desktop="GNOME", tools=["ydotool", "gdbus", "wl-copy", "wl-paste",
                                     "notify-send", "xdg-open"])
    status = svc.ComputerService().status()
    assert status["compositor"] == "gnome"
    assert status["supports_windows"] is False
    assert status["supports_input"] is True
    joined = " ".join(status["notes"])
    assert "portal" in joined.lower()
    assert "window tree private" in joined
    assert "grim" in status["missing_tools"]
    assert status["install_hint"].startswith("sudo dnf install ")


def test_status_flags_a_missing_ydotoold(build, monkeypatch):
    monkeypatch.setattr(svc, "_ydotool_socket", lambda: None)
    build(tools=["ydotool"])
    status = svc.ComputerService().status()
    assert status["tools"]["ydotool"] is True
    assert status["tools"]["ydotoold"] is False
    assert any("ydotoold" in note for note in status["notes"])


def test_status_on_x11_reports_the_x_toolchain(build):
    build(session="x11")
    status = svc.ComputerService().status()
    assert status["wayland"] is False
    assert status["session"] == "x11"
    assert status["supports_input"] is True
    assert status["supports_windows"] is True
    assert status["supports_screenshot"] is True
    assert "mouse" in status["native"] and "keyboard" in status["native"]
    assert status["tools"]["xdotool"] is True
    assert status["tools"]["wmctrl"] is True
