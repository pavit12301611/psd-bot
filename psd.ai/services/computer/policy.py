"""Safety policy for the computer-control surface.

psd.ai's owner asked for full access to the machine — mouse, keyboard,
windows, files, processes — so the allow-list here is deliberately broad.
What it *never* allows is the small set of actions that are unsafe no matter
who asked:

* wiping or reformatting a drive
* deleting or overwriting system directories
* changing firmware / boot configuration
* power-state changes that lose unsaved work without an explicit request
* killing the process that hosts psd.ai itself

Anything else is permitted, and the owner can additionally flip
``computer_control_confirm`` to have the agent ask before *risky* actions
(those are listed in :data:`RISKY_ACTIONS`).

The policy is a pure function of (action, arguments) — it performs no I/O and
never imports platform modules, which keeps it trivially unit-testable.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, FrozenSet, Tuple

from enum import Enum


class ActionRisk(str, Enum):
    """How much an action can change the machine."""

    READ_ONLY = "read_only"
    WRITE = "write"
    RISKY = "risky"


# ── Action registry ────────────────────────────────────────────────────────
# "What the assistant may do", in one place. `routes/jarvis_routes.py`,
# `src/agent_tools/computer_tools.py` and the desktop UI all read this, so a
# new action only has to be added here (plus a handler) to light up
# everywhere.

READ_ONLY_ACTIONS: FrozenSet[str] = frozenset(
    {
        "screen_size",   # monitor geometry
        "screenshot",    # capture the screen
        "windows",       # list open windows
        "clipboard_get",
        "info",          # CPU / RAM / disk / battery / hostname
        "processes",
        "wait",          # sleep N seconds
    }
)

WRITE_ACTIONS: FrozenSet[str] = frozenset(
    {
        "click",
        "move",
        "drag",
        "scroll",
        "type",
        "key",
        "open",          # launch an app / file / URL
        "focus",
        "close_window",
        "clipboard_set",
        "notify",
        "volume",
    }
)

RISKY_ACTIONS: FrozenSet[str] = frozenset(
    {
        "kill",          # terminate a process
    }
)

ALL_ACTIONS: FrozenSet[str] = READ_ONLY_ACTIONS | WRITE_ACTIONS | RISKY_ACTIONS

ACTION_RISK: Dict[str, ActionRisk] = {
    **{a: ActionRisk.READ_ONLY for a in READ_ONLY_ACTIONS},
    **{a: ActionRisk.WRITE for a in WRITE_ACTIONS},
    **{a: ActionRisk.RISKY for a in RISKY_ACTIONS},
}

# ── Hard denies ────────────────────────────────────────────────────────────

# Substrings that must never appear in a launch target or a process name we
# are asked to kill. Matched case-insensitively against the raw argument.
#
# These are the Fedora/Linux ways to lose a machine. The list is deliberately
# substring-based rather than a parsed grammar: the goal is to stop the
# catastrophic command whatever shell quoting or wrapper the model wrapped it
# in, not to be a general command validator.
_DENY_SUBSTRINGS: Tuple[str, ...] = (
    # Recursive deletion of something that is not a project directory.
    "rm -rf /",
    "rm -rf /*",
    "rm -rf ~",
    "rm -rf /home",
    "rm -fr /",
    "find / -delete",
    "find / -exec rm",
    # Filesystems, partitions and volumes.
    "mkfs",
    "wipefs",
    "fdisk",
    "parted",
    "sgdisk --zap-all",
    "dd if=",
    "dd of=/dev/",
    "shred /dev/",
    "lvremove",
    "vgremove",
    "pvremove",
    "cryptsetup luksformat",
    "cryptsetup lukserase",
    "mdadm --zero-superblock",
    ":(){ ",              # fork bomb
    # Power state: loses unsaved work in every other application.
    "shutdown",
    "poweroff",
    "reboot",
    "halt",
    "init 0",
    "init 6",
    "systemctl poweroff",
    "systemctl reboot",
    "systemctl halt",
    "loginctl terminate",
    # Boot loader, kernel and firmware: a mistake here does not boot again.
    "grubby",
    "grub2-install",
    "grub2-mkconfig",
    "grub-mkconfig",
    "efibootmgr",
    "mokutil",
    "flashrom",
    "fwupdmgr",
    "rpm -e kernel",
    "dnf remove kernel",
    "dnf erase kernel",
    # Security downgrade: SELinux is the one thing keeping a compromised
    # agent process away from the rest of the system.
    "setenforce 0",
    "sestatus -b",
    "sed -i /etc/selinux/config",
)

# Commands whose danger depends on their arguments, so a substring cannot
# express them: a *recursive* permission or ownership change aimed at the
# filesystem root or a top-level system directory. ``chmod -R 755 ~/project``
# stays allowed — the point is to stop the machine-wide variants
# (``chown -R nobody /``, ``chmod -R 000 /etc``), not to police a project tree.
_DENY_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(
        r"\bch(?:mod|own)\b.*\s-[a-z]*r[a-z]*\s+(?:[^\s]+\s+)?"
        r"/(?:bin|boot|dev|etc|home|lib|lib64|proc|root|run|sbin|sys|usr|var)?(?:\s|$)",
        re.IGNORECASE,
    ),
)

# Paths the assistant must never touch through the `open` (shell-execute)
# action — deleting or "opening" these is never a legitimate assistant task.
# On Fedora several of these are symlinks into /usr, so both the traditional
# root and the merged-/usr layout are covered.
_DENY_PATH_RE = re.compile(
    r"^("
    r"/(bin|boot|dev|etc|lib|lib64|proc|root|run|sbin|sys|usr|var)(/|$)"
    r"|/(lost\+found)(/|$)"
    r")",
    re.IGNORECASE,
)

# Processes psd.ai must never kill: doing so would kill its own engine, the
# desktop shell, the session's audio/input plumbing, or PID 1 — and leave the
# owner with a broken session they have to reboot out of.
_SELF_GUARD_NAMES: FrozenSet[str] = frozenset(
    {
        # psd.ai itself, and the model server it launched.
        "python",
        "python3",
        "uvicorn",
        "psd-ai",
        "psd-ai-desktop",
        "psd.ai",
        "llama-server",
        "ydotoold",
        # PID 1 and the session manager.
        "init",
        "systemd",
        "dbus-daemon",
        "dbus-broker",
        "gdm",
        "gdm-x-session",
        "gdm-wayland-session",
        "sddm",
        "lightdm",
        "polkitd",
        "logind",
        # Compositors / window managers: killing one ends the graphical session.
        "gnome-shell",
        "gnome-session-binary",
        "mutter",
        "plasmashell",
        "kwin_wayland",
        "kwin_x11",
        "sway",
        "hyprland",
        "xorg",
        "xwayland",
        # Audio and notifications the voice mode depends on.
        "pipewire",
        "pipewire-pulse",
        "wireplumber",
        "pulseaudio",
        # Network: killing this strands a remote session.
        "networkmanager",
        "sshd",
    }
)


def action_risk(action: Any) -> ActionRisk:
    """Return the risk class of ``action`` (unknown ⇒ risky)."""

    name = str(action or "").strip().lower()
    return ACTION_RISK.get(name, ActionRisk.RISKY)


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else str(value or "")


def is_denied_target(target: Any) -> bool:
    """True when ``target`` must never be launched or killed."""

    raw = _as_text(target).strip()
    if not raw:
        return False
    low = raw.lower()
    for bad in _DENY_SUBSTRINGS:
        if bad in low:
            return True
    for pattern in _DENY_PATTERNS:
        if pattern.search(raw):
            return True
    # Path-shaped targets: reject OS directories and drive roots.
    candidate = raw.replace('"', "").replace("'", "").strip()
    if _DENY_PATH_RE.match(candidate):
        return True
    return False


def check_action(
    action: Any,
    params: Dict[str, Any] | None = None,
    *,
    confirm_risky: bool = False,
) -> Tuple[bool, str, ActionRisk, bool]:
    """Validate one computer action.

    Returns ``(allowed, reason, risk, needs_confirmation)``.

    ``allowed=False`` is a hard deny — the caller must not retry with the same
    arguments. ``needs_confirmation=True`` means the action is legal but the
    owner asked to be asked first (``computer_control_confirm``); the UI shows
    a confirm chip and the agent receives the result only after approval.
    """

    name = str(action or "").strip().lower()
    params = params or {}
    risk = action_risk(name)

    if not name:
        return False, "No action supplied", risk, False
    if name not in ALL_ACTIONS:
        return False, (
            f"Unknown computer action '{name}'. Allowed: "
            + ", ".join(sorted(ALL_ACTIONS))
        ), risk, False

    # Target-bearing actions get the hard-deny scan.
    for key in ("target", "path", "name", "process", "process_name", "title"):
        value = params.get(key)
        if value and is_denied_target(value):
            return False, (
                f"Refused: '{value}' is on the never-run list "
                "(system/destructive targets are blocked by policy)."
            ), risk, False

    # `kill` additionally protects psd.ai's own processes and the shell.
    if name == "kill":
        victim = _as_text(params.get("name") or params.get("pid") or "").strip()
        base = os.path.basename(victim.lower())
        base_noext = os.path.splitext(base)[0]
        if not victim:
            return False, "Refused: no process specified", risk, False
        if base_noext in _SELF_GUARD_NAMES or base in _SELF_GUARD_NAMES:
            return False, (
                f"Refused: '{victim}' is a protected system/self process."
            ), risk, False
        try:
            if int(victim) == os.getpid():
                return False, "Refused: cannot kill the psd.ai engine process.", risk, False
        except (TypeError, ValueError):
            pass

    needs_confirm = confirm_risky and risk is ActionRisk.RISKY
    return True, "", risk, needs_confirm


def describe_policy() -> Dict[str, Any]:
    """Machine-readable policy summary for /api/jarvis/status and the UI."""

    session = (os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    if not session:
        # A service started by systemd may not carry the variable; infer it the
        # same way the rest of the app does. Reading the environment (and not
        # /proc) keeps this function I/O-free.
        session = "wayland" if os.environ.get("WAYLAND_DISPLAY") else (
            "x11" if os.environ.get("DISPLAY") else ""
        )
    return {
        "read_only": sorted(READ_ONLY_ACTIONS),
        "write": sorted(WRITE_ACTIONS),
        "risky": sorted(RISKY_ACTIONS),
        "all": sorted(ALL_ACTIONS),
        "denied_targets": list(_DENY_SUBSTRINGS),
        "protected_processes": sorted(_SELF_GUARD_NAMES),
        "platform": sys.platform,
        "os": "linux",
        "session": session,
        "package_manager": "dnf",
    }
