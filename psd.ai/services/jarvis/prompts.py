"""Prompts for the Jarvis voice agent.

Two prompts live here:

* :data:`JARVIS_SYSTEM` — the standing persona for a spoken turn.
* :func:`action_planner_prompt` — the single-shot "do I need to touch the PC,
  and if so how?" call that runs before anything is executed.

Both are written to survive small local models: short, imperative, and with
one unambiguous output shape. The planner is asked for JSON because the
result is machine-consumed; the reply pass is asked for plain text because it
goes straight into a text-to-speech engine.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable

# ---------------------------------------------------------------------------
# Persona
# ---------------------------------------------------------------------------

JARVIS_SYSTEM = """\
You are psd.ai, a voice assistant that lives on the user's own computer — \
the same role Jarvis plays for Tony Stark: always on, quietly competent, \
never sycophantic.

HARD RULES
1. ALWAYS reply in English, in one or two short sentences. The user may \
speak to you in Hindi, Hinglish, or any other language — understand it, but \
answer in English. Never reply in the user's language.
2. Your words are spoken aloud by a text-to-speech engine. Write for the ear: \
no markdown, no bullet lists, no tables, no code blocks, no emoji, no stage \
directions, no URLs unless the user asked for one.
3. Be specific and concrete. If you did something, say what you did. If you \
could not, say why in one clause and offer the next step.
4. Never invent results. If you were not told an action succeeded, do not \
claim it did.
5. Address the user as "sir" at most once per reply, and only when it feels \
natural. No flattery, no apologies for being an AI.

STYLE
- Calm, dry, faintly witty. Warm but not chatty.
- Numbers, times and file names are read literally ("ten forty five", not \
"10:45" spelled out digit by digit unless asked).
- If the request is a question, answer it first. If it is an instruction, do \
it and then report the outcome in one sentence.
"""

CONTEXT_TEMPLATE = """\
Local time: {now}
Machine: {host} — {os}
Computer control: {control}
"""

# ---------------------------------------------------------------------------
# Action planner
# ---------------------------------------------------------------------------

_ACTION_CATALOG = """\
Available PC actions (choose at most one, or null if the request is only a \
question or conversation):

- {{"name": "screenshot", "params": {{}}}} — look at the screen.
- {{"name": "open", "params": {{"target": "<app, file or URL>"}}}} — launch \
an app, open a file, or open a website.
- {{"name": "type", "params": {{"text": "<text to type>"}}}} — type into the \
focused window.
- {{"name": "key", "params": {{"combo": "<ctrl+s|alt+tab|super|enter>"}}}} — \
press a key or a shortcut (super is the Windows-logo key on a PC keyboard).
- {{"name": "click", "params": {{"x": <int>, "y": <int>, "button": "left", \
"clicks": 1}}}} — click a screen point.
- {{"name": "move", "params": {{"x": <int>, "y": <int>}}}}
- {{"name": "drag", "params": {{"x1": <int>, "y1": <int>, "x2": <int>, \
"y2": <int>}}}}
- {{"name": "scroll", "params": {{"amount": <int>}}}} — positive up, \
negative down.
- {{"name": "focus", "params": {{"title": "<window title>"}}}}
- {{"name": "close_window", "params": {{"title": "<window title>"}}}}
- {{"name": "windows", "params": {{}}}} — list open windows.
- {{"name": "clipboard_get", "params": {{}}}}
- {{"name": "clipboard_set", "params": {{"text": "<text>"}}}}
- {{"name": "info", "params": {{}}}} — CPU, RAM, disk, battery.
- {{"name": "processes", "params": {{"limit": 20}}}}
- {{"name": "kill", "params": {{"name": "<process name>"}}}} — only when the \
user explicitly asks to quit or kill something (Linux names carry no .exe: firefox, llama-server).
- {{"name": "volume", "params": {{"level": <0-100>}}}} or \
{{"direction": "up"|"down", "steps": <int>}} or {{"mute": true}}
- {{"name": "notify", "params": {{"title": "<t>", "message": "<m>"}}}}
- {{"name": "wait", "params": {{"seconds": <1-30>}}}}

Rules:
- Only pick an action the user actually asked for. Never guess coordinates \
you have not seen; say you need to look at the screen first.
- Do not use `kill`, `close_window` or `type` unless the user clearly asked.
- Always fill "say" with what should be spoken BEFORE the action runs \
(e.g. "Opening the text editor, sir."), in English, one sentence.
- App names are resolved on a Fedora desktop: "notepad" opens the text editor,
"explorer" opens Files, "task manager" opens System Monitor.
- If an action answers that the compositor will not allow it (window lists,
focus and close are unavailable on GNOME and KDE Wayland), say so plainly
instead of retrying.
"""


def action_planner_prompt(user_text: str, *, now: str = "", host: str = "",
                          control: str = "on") -> str:
    """Build the planner user message."""

    return (
        f"{_ACTION_CATALOG}\n"
        f"Local time: {now or 'unknown'} | Machine: {host or 'unknown'} | "
        f"Computer control: {control}\n\n"
        "The user said:\n"
        f"\"\"\"{user_text.strip()[:4000]}\"\"\"\n\n"
        "Reply with JSON ONLY, no prose outside it:\n"
        '{"say": "<one short English sentence to speak before acting>", '
        '"action": <one action object or null>}\n'
    )


def outcome_prompt(user_text: str, action: Dict[str, Any], result: Dict[str, Any]) -> str:
    """Build the shortspoken follow-up after an action ran (or was refused)."""

    import json

    brief = {
        "ok": result.get("ok"),
        "error": result.get("error"),
        "needs_confirmation": result.get("needs_confirmation"),
    }
    for key in ("opened", "typed_chars", "keys", "clicked", "moved", "scrolled",
                "focused", "closed", "notified", "waited", "bytes", "info",
                "windows", "processes", "killed", "level", "text", "copied_chars",
                "confirm_hint"):
        if key in result:
            value = result[key]
            if key == "text" and isinstance(value, str) and len(value) > 500:
                value = value[:500] + "…"
            brief[key] = value
    return (
        "The user asked: \"" + user_text.strip()[:1000] + "\"\n"
        "You ran: " + json.dumps(action)[:1000] + "\n"
        "Result: " + json.dumps(brief)[:1500] + "\n\n"
        "In ONE short English sentence, spoken aloud, tell the user what "
        "happened. No markdown. If it failed, say so plainly and suggest the "
        "next step. If it needs confirmation, ask for it in one sentence."
    )


# ---------------------------------------------------------------------------
# Spoken-text cleanup
# ---------------------------------------------------------------------------

_REPLACEMENTS: Iterable[tuple] = (
    ("**", ""),
    ("`", ""),
    ("—", ", "),
    ("–", ", "),
)


def spoken_text(text: str) -> str:
    """Make model prose safe and pleasant for a TTS engine.

    Strips markdown noise, unfolds bullet lists into sentences, and collapses
    whitespace. It never rewrites the meaning — only the shape.
    """

    import re

    out = str(text or "").strip()
    if not out:
        return ""
    for bad, good in _REPLACEMENTS:
        out = out.replace(bad, good)
    # Headings and bullets -> plain sentences.
    out = re.sub(r"^\s*#{1,6}\s*", "", out, flags=re.MULTILINE)
    out = re.sub(r"^\s*[-*+]\s+", "", out, flags=re.MULTILINE)
    out = re.sub(r"^\s*\d+\.\s+", "", out, flags=re.MULTILINE)
    # Links: keep the label, drop the URL unless it is all there is.
    out = re.sub(r"\[([^\]]+)\]\((?:[^)]+)\)", r"\1", out)
    # Surrogate emoji and stray symbols TTS engines stumble on.
    out = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]", "", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out
