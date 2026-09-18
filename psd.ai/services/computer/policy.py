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
_DENY_SUBSTRINGS: Tuple[str, ...] = (
    "format c:",
    "format.com",
    "diskpart",
    "bcdedit",
    "bootrec",
    "cipher /w",
    "del /f /s",
    "del /s /q c:\\windows",
    "rm -rf /",
    "rm -rf /*",
    "rm -rf ~",
    "rm -rf /home",
    "mkfs",
    ":(){ ",              # fork bomb
    "dd if=",
    "shutdown",
    "poweroff",
    "reboot",
    "init 0",
    "init 6",
    "reg delete hklm",
    "reg delete hkcr",
    "takeown /f c:\\windows",
    "icacls c:\\windows",
    "vssadmin delete",
    "wmic diskdrive",
)

# Paths the assistant must never touch through the `open` (shell-execute)
# action — deleting or "opening" these is never a legitimate assistant task.
_DENY_PATH_RE = re.compile(
    r"^("
    r"[a-z]:[\\/]?(windows|winnt|system32|system volume information|\\\?\?)"
    r"|/(bin|boot|dev|etc|lib|proc|sbin|sys|usr|var)(/|$)"
    r"|[a-z]:[\\/]$"
    r")",
    re.IGNORECASE,
)

# Processes psd.ai must never kill: doing so would kill its own engine or the
# desktop shell and leave the owner with a broken session.
_SELF_GUARD_NAMES: FrozenSet[str] = frozenset(
    {
        "python",
        "pythonw",
        "psd-ai-desktop",
        "psd.ai",
        "explorer",
        "csrss",
        "wininit",
        "winlogon",
        "services",
        "smss",
        "lsass",
        "system",
        "launchd",
        "init",
        "systemd",
        "finder",
        "dwm",
        "shellexperiencehost",
        "sihost",
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

    return {
        "read_only": sorted(READ_ONLY_ACTIONS),
        "write": sorted(WRITE_ACTIONS),
        "risky": sorted(RISKY_ACTIONS),
        "all": sorted(ALL_ACTIONS),
        "denied_targets": list(_DENY_SUBSTRINGS),
        "protected_processes": sorted(_SELF_GUARD_NAMES),
        "platform": sys.platform,
        "windows": os.name == "nt",
    }
