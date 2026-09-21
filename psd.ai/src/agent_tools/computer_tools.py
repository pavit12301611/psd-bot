"""computer_tools.py — drive the local desktop from agent mode.

Exposes the same allow-listed action set the voice agent uses
(``services/computer``) as an agent tool, so "open Firefox and type this"
works from a typed chat as well as from a spoken one. Targets are resolved
on a Fedora desktop, so the familiar aliases still work: "notepad" opens
the text editor, "explorer" opens Files, "task manager" opens System
Monitor.

The tool takes a single JSON object::

    {"action": "open", "params": {"target": "firefox"}}

and a bare ``open firefox`` line is accepted too, because small local models
like to emit that shape instead.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# Words models use for an action name instead of the canonical one.
_ACTION_ALIASES = {
    "press": "key", "tap": "key", "hotkey": "key", "shortcut": "key",
    "keystroke": "key",
    "launch": "open", "start": "open", "run": "open", "execute": "open",
    "browse": "open", "visit": "open",
    "enter": "type", "write": "type", "input": "type",
    "shot": "screenshot", "capture": "screenshot", "screen": "screenshot",
    "quit": "kill", "terminate": "kill", "stop": "kill", "end": "kill",
    "shut": "close_window", "exit": "close_window",
    "apps": "windows", "app": "windows",
    "stats": "info", "status": "info", "specs": "info",
    "sleep": "wait", "pause": "wait",
    "mouse": "click",
}


def _parse_content(content: str) -> Dict[str, Any]:
    """Accept JSON, or ``action key=value ...`` / ``action subject`` text."""

    raw = (content or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            # Looks like JSON but is broken — do not guess an action out of
            # the fragments; let the caller ask for a well-formed call.
            logger.warning("computer_control received malformed JSON: %r", raw[:200])
            return {"action": "", "params": {}, "malformed": True}
        if isinstance(data, dict):
            action = str(data.get("action") or data.get("name") or "").strip().lower()
            params = data.get("params")
            if action and not isinstance(params, dict):
                params = {
                    k: v for k, v in data.items()
                    if k not in ("action", "name", "params")
                }
            return {"action": _ACTION_ALIASES.get(action, action), "params": params or {}}

    # Text form: first token is the action, the rest is the subject.
    head, _, rest = raw.partition("\n")
    head = head.strip()
    rest = rest.strip()
    parts = head.split(None, 1)
    action = parts[0].strip().lower() if parts else ""
    action = _ACTION_ALIASES.get(action, action)
    subject = (parts[1].strip() if len(parts) > 1 else "") or rest
    params: Dict[str, Any] = {}
    subject = subject.strip()
    if subject:
        if action == "open":
            params["target"] = subject
        elif action == "type":
            params["text"] = subject
        elif action == "key":
            params["combo"] = subject
        elif action == "focus" or action == "close_window":
            params["title"] = subject
        elif action == "kill":
            params["name"] = subject
        elif action == "click":
            coords = [int(t) for t in subject.replace(",", " ").split() if t.lstrip("-").isdigit()]
            if len(coords) >= 2:
                params.update({"x": coords[0], "y": coords[1]})
        elif action == "volume":
            if subject.isdigit():
                params["level"] = int(subject)
            else:
                params["direction"] = "down" if subject in ("down", "lower") else "up"
    return {"action": action, "params": params}


class ComputerControlTool:
    """Run one allow-listed desktop action."""

    async def execute(self, content: str, ctx: Optional[dict] = None) -> Dict[str, Any]:
        ctx = ctx or {}
        parsed = _parse_content(content)
        action = parsed.get("action")
        params = parsed.get("params") or {}
        if not action:
            return {
                "error": (
                    "computer_control needs JSON like "
                    '{"action": "open", "params": {"target": "firefox"}}. '
                    "Use the screenshot or windows action first if you need to "
                    "see what is on screen."
                ),
                "exit_code": 1,
            }

        try:
            from services.computer import get_computer_service
        except Exception as exc:  # pragma: no cover - packaging guard
            return {"error": f"Computer control unavailable: {exc}", "exit_code": 1}

        confirm = bool(params.pop("confirm", False))
        result = await get_computer_service().act(action, params, confirm_risky=None)
        if confirm:
            params["confirm"] = True

        result = dict(result or {})
        # Screenshots are dropped from the transcript — the agent cannot see
        # images, and a multi-megabyte base64 blob would blow the context.
        if result.get("image_base64"):
            result["image_base64"] = None
            result["note"] = "screenshot captured (image not shown to the model)"

        if not result.get("ok"):
            message = result.get("error") or "action failed"
            result["error"] = message
            result.setdefault("exit_code", 1)
            if result.get("needs_confirmation"):
                result["hint"] = (
                    "The owner asked to be asked first. Tell them what you want "
                    "to run and wait for approval."
                )
            return result

        result.setdefault("exit_code", 0)
        return result


class ComputerScreenTool:
    """Capture the screen so the agent can be told what is on it."""

    async def execute(self, content: str, ctx: Optional[dict] = None) -> Dict[str, Any]:
        try:
            from services.computer import get_computer_service
        except Exception as exc:  # pragma: no cover - packaging guard
            return {"error": f"Computer control unavailable: {exc}", "exit_code": 1}

        monitor = 0
        raw = (content or "").strip()
        if raw.startswith("{"):
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    monitor = int(data.get("monitor") or 0)
            except (ValueError, TypeError):
                monitor = 0
        elif raw.isdigit():
            monitor = int(raw)

        try:
            data = await get_computer_service().screenshot(monitor)
        except Exception as exc:
            return {"error": f"screenshot failed: {exc}", "exit_code": 1}
        return {
            "exit_code": 0,
            "bytes": len(data),
            "monitor": monitor,
            "note": (
                "Screenshot captured and saved to the session gallery path; the "
                "model cannot see the pixels, so ask the user to describe what "
                "matters or use the windows/processes actions for structure."
            ),
        }
