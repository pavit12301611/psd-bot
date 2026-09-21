"""Jarvis — the voice agent: listen, think, act on the PC, answer in English.

One call to :meth:`JarvisService.turn` runs the whole loop:

    audio ──▶ STT ──▶ planner ──▶ computer action ──▶ narrator ──▶ TTS
                │         │              │                 │
             (or text) (LLM/JSON)  (policy-gated)    (English only)

Design notes:

* **English out, anything in.** The persona in ``prompts.py`` requires an
  English reply no matter what language the user spoke.
* **Degrades, never dies.** No STT package? The client can send the transcript
  as text instead. No model configured, or the model is unreachable? A
  rule-based planner still handles "open notepad", "volume up", "type hello",
  "screenshot". No server TTS? The browser speaks the reply instead.
* **Nothing executes without the policy gate.** Actions go through
  ``ComputerService.act`` → ``policy.check_action``, so the allow-list and the
  hard denies apply to voice exactly as they do to chat.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import socket
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.settings import get_setting

from services.computer.policy import ALL_ACTIONS, check_action
from services.computer.service import ComputerService, computer_control_enabled

from .prompts import (
    JARVIS_SYSTEM,
    CONTEXT_TEMPLATE,
    action_planner_prompt,
    outcome_prompt,
    spoken_text,
)

logger = logging.getLogger(__name__)

LLM_TIMEOUT_SECONDS = 45
PLANNER_MAX_CHARS = 8000
MAX_HISTORY_TURNS = 6          # keep 6 exchanges = 12 messages
HISTORY_SESSIONS = 32          # LRU cap on remembered conversations


class JarvisUnavailable(Exception):
    """Raised when a voice turn cannot be produced at all."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _setting(key: str, default: Any = None) -> Any:
    try:
        value = get_setting(key, default)
    except Exception:
        return default
    return default if value is None else value


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


_NON_CHAT_TOKENS = (
    "embed", "rerank", "moderation", "whisper", "tts", "stt",
    "clip", "diffusion", "flux", "sd-xl", "sdxl", "image",
)


def _is_chat_model(name: str) -> bool:
    low = (name or "").lower()
    if not low:
        return False
    return not any(tok in low for tok in _NON_CHAT_TOKENS)


def _endpoint_models(ep: Any) -> List[str]:
    out: List[str] = []
    for field in ("pinned_models", "cached_models"):
        raw = getattr(ep, field, None)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, list):
            out.extend(str(m) for m in parsed if m)
    # de-duplicate, keep order
    seen = set()
    unique = []
    for m in out:
        if m not in seen:
            seen.add(m)
            unique.append(m)
    return unique


def pick_chat_model(owner: Optional[str] = None) -> str:
    """Best-effort selection of a chat model for the voice agent.

    Order: the explicit ``jarvis_model`` setting → the first chat-capable
    model on the first enabled endpoint. Returns "" when there is nothing
    usable, which puts the turn on the rule-based planner path.
    """

    configured = str(_setting("jarvis_model", "") or "").strip()
    if configured:
        return configured

    try:
        from src.database import SessionLocal, ModelEndpoint
        from src.auth_helpers import owner_filter

        db = SessionLocal()
        try:
            query = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
            if owner:
                query = owner_filter(query, ModelEndpoint, owner)
            for ep in query.all():
                if str(getattr(ep, "model_type", "") or "llm").lower() not in ("llm", "", "none"):
                    continue
                for model in _endpoint_models(ep):
                    if _is_chat_model(model):
                        return model
        finally:
            db.close()
    except Exception as exc:
        logger.debug("jarvis model discovery failed: %s", exc)
    return ""


def _balanced_json(text: str) -> Optional[str]:
    """Return the first balanced {...} substring of ``text``, or None."""

    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        start = text.find("{", start + 1)
    return None


def extract_plan(text: str) -> Optional[Dict[str, Any]]:
    """Pull the planner's JSON object out of a model reply.

    Tolerates ```json fences, a prose preamble, trailing commentary, and the
    occasional trailing comma — small local models do all of it.
    """

    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"```\s*$", "", raw)
        raw = raw.strip()
    candidate = _balanced_json(raw)
    if not candidate:
        return None
    try:
        data = json.loads(candidate)
    except ValueError:
        # Try once more with trailing commas removed.
        try:
            data = json.loads(re.sub(r",\s*([}\]])", r"\1", candidate))
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# rule-based planner (works with no model at all)
# ---------------------------------------------------------------------------

# Spoken phrase -> the app alias that `services.computer.service._open_target`
# resolves. These are the names people say on a Fedora desktop (and their
# Hinglish verbs are handled by _OPEN_SUFFIX_RE), so the aliases are GNOME/KDE
# apps with Windows-ish phrases kept as synonyms: "notepad" opens the text
# editor, "task manager" opens the system monitor, "explorer" opens Files.
_APP_HINTS = [
    ("notepad", "notepad"), ("text editor", "text editor"),
    ("calculator", "calculator"), ("calc", "calculator"),
    ("paint", "paint"), ("drawing", "drawing"),
    ("file explorer", "files"), ("files", "files"), ("explorer", "files"),
    ("nautilus", "files"), ("file manager", "files"),
    ("chrome", "chrome"), ("google chrome", "chrome"), ("chromium", "chromium"),
    ("edge", "edge"), ("firefox", "firefox"), ("browser", "browser"),
    ("spotify", "spotify"), ("music", "music"), ("rhythmbox", "music"),
    ("videos", "videos"), ("video player", "videos"), ("totem", "videos"),
    ("vlc", "vlc"), ("mpv", "mpv"),
    ("terminal", "terminal"), ("console", "console"), ("gnome terminal", "terminal"),
    ("task manager", "task manager"), ("system monitor", "system monitor"),
    ("settings", "settings"), ("control center", "settings"),
    ("software", "software"), ("software center", "software"), ("store", "software"),
    ("vs code", "vscode"), ("vscode", "vscode"), ("code", "vscode"),
    ("libreoffice", "libreoffice"), ("writer", "writer"), ("word", "writer"),
    ("excel", "spreadsheet"), ("spreadsheet", "spreadsheet"),
    ("powerpoint", "impress"), ("presentation", "impress"),
    ("email", "email"), ("mail", "email"), ("outlook", "email"),
    ("thunderbird", "thunderbird"),
    ("photos", "photos"), ("image viewer", "photos"),
    ("pdf", "pdf"), ("document viewer", "pdf"), ("evince", "pdf"),
    ("calendar", "calendar"), ("clock", "clock"), ("weather", "weather"),
    ("maps", "maps"), ("disks", "disks"), ("logs", "logs"), ("help", "help"),
    ("camera", "camera"), ("discord", "discord"), ("telegram", "telegram"),
    ("steam", "steam"),
]

_OPEN_RE = re.compile(
    r"\b(?:open|launch|start|kholo|khol do|chalao|chala do|start karo|run)\b\s+(.{1,60})",
    re.IGNORECASE,
)
# Hinglish puts the verb last: "notepad kholo", "chrome chalao".
_OPEN_SUFFIX_RE = re.compile(
    r"^(.{1,40}?)\s+(?:kholo|khol do|khol|chalao|chala do|open karo|open kar do|"
    r"start karo|launch karo)\b",
    re.IGNORECASE,
)
_TYPE_RE = re.compile(
    r"\b(?:type|write|likho|type karo)\b\s+(.{1,500})", re.IGNORECASE
)
# "mute" on its own is already a volume request.
_VOLUME_RE = re.compile(
    r"\b(?:volume|sound|awaz|mute|unmute|loud|quiet)\b", re.IGNORECASE
)


def heuristic_plan(text: str) -> Optional[Dict[str, Any]]:
    """Very small intent parser used when no LLM is reachable.

    It only claims a request it recognises confidently — everything else
    returns None so the caller can fall back to an honest "I could not reach
    a model" instead of doing something random.
    """

    raw = str(text or "").strip()
    low = raw.lower()
    if not low:
        return None

    # URL
    url_match = re.search(r"https?://\S+", raw)
    if url_match and any(w in low for w in ("open", "browse", "go to", "visit", "kholo")):
        return {
            "say": "Opening it, sir.",
            "action": {"name": "open", "params": {"target": url_match.group(0)}},
        }

    subject = ""
    m = _OPEN_RE.search(raw)
    if m:
        subject = m.group(1)
    else:
        # Verb-last Hinglish: "notepad kholo", "chrome chalao".
        suffix = _OPEN_SUFFIX_RE.search(raw)
        if suffix:
            subject = suffix.group(1)
    if subject:
        subject = subject.strip().strip(".").strip()
        subject = re.sub(r"\b(please|pls|plz|krdo|kar do|karo|again)\b", "",
                         subject, flags=re.IGNORECASE).strip()
        for needle, target in _APP_HINTS:
            if needle in subject.lower():
                return {
                    "say": f"Opening {target}, sir.",
                    "action": {"name": "open", "params": {"target": target}},
                }
        if subject and len(subject) <= 40:
            return {
                "say": f"Opening {subject}, sir.",
                "action": {"name": "open", "params": {"target": subject}},
            }

    if re.search(r"\b(screenshot|screen shot|screen capture|take a (?:look|picture))\b", low):
        return {"say": "One moment, sir.", "action": {"name": "screenshot", "params": {}}}

    if re.search(r"\b(which windows|open windows|list windows|what.?s open)\b", low):
        return {"say": "Checking, sir.", "action": {"name": "windows", "params": {}}}

    if re.search(r"\b(system (?:info|status)|system|pc (?:info|status)|battery|memory|ram|cpu)\b", low) and \
       re.search(r"\b(how|what|tell|status|info|kitn|batao)\b", low):
        return {"say": "Checking the machine, sir.", "action": {"name": "info", "params": {}}}

    if _VOLUME_RE.search(low):
        if re.search(r"\b(mute|chup|band)\b", low):
            return {"say": "Muting, sir.", "action": {"name": "volume", "params": {"mute": True}}}
        if re.search(r"\b(unmute)\b", low):
            return {"say": "Unmuting, sir.", "action": {"name": "volume", "params": {"mute": False}}}
        if re.search(r"\b(up|increase|badhao|bada|loud|tej)\b", low):
            steps = 3 if re.search(r"\b(a lot|bahut|zyada)\b", low) else 2
            return {
                "say": "Turning it up, sir.",
                "action": {"name": "volume", "params": {"direction": "up", "steps": steps}},
            }
        if re.search(r"\b(down|decrease|kam|lower|soft|dhimi)\b", low):
            steps = 3 if re.search(r"\b(a lot|bahut|zyada)\b", low) else 2
            return {
                "say": "Turning it down, sir.",
                "action": {"name": "volume", "params": {"direction": "down", "steps": steps}},
            }
        pct = re.search(r"(\d{1,3})\s*(?:%|percent)", low)
        if pct:
            return {
                "say": f"Setting the volume to {pct.group(1)} percent, sir.",
                "action": {"name": "volume", "params": {"level": int(pct.group(1))}},
            }

    m = _TYPE_RE.search(raw)
    if m:
        body = m.group(1).strip().strip('"').strip("'").strip()
        if body:
            return {
                "say": "Typing it now, sir.",
                "action": {"name": "type", "params": {"text": body}},
            }

    key_match = re.search(
        r"\bpress\s+(ctrl|control|alt|shift|win|windows|cmd|command)?\s*\+?\s*"
        r"([a-z0-9]{1,12}|f[0-9]{1,2}|enter|escape|esc|tab|space|delete|backspace)\b",
        low,
    )
    if key_match:
        parts = [p for p in (key_match.group(1), key_match.group(2)) if p]
        return {
            "say": "Pressing it, sir.",
            "action": {"name": "key", "params": {"combo": "+".join(parts)}},
        }

    if re.search(r"\b(save)\b", low) and re.search(r"\b(file|it|this|document)\b", low):
        return {"say": "Saving, sir.", "action": {"name": "key", "params": {"combo": "ctrl+s"}}}

    if re.search(r"\b(copy)\b", low) and re.search(r"\b(that|it|this|selection)\b", low):
        return {"say": "Copying, sir.", "action": {"name": "key", "params": {"combo": "ctrl+c"}}}

    if re.search(r"\b(paste)\b", low):
        return {"say": "Pasting, sir.", "action": {"name": "key", "params": {"combo": "ctrl+v"}}}

    if re.search(r"\b(close|shut|band karo|band)\b", low):
        target = re.search(
            r"\b(?:close|shut|band(?:\s+karo)?)\s+(?:the\s+)?(.{1,40})", low
        )
        if target:
            title = re.sub(
                r"\b(window|app|application|tab|please|pls|plz|karo|kar do)\b",
                "", target.group(1), flags=re.IGNORECASE,
            ).strip(" .")
            if title:
                return {
                    "say": f"Closing {title}, sir.",
                    "action": {"name": "close_window", "params": {"title": title}},
                }
            # "close the window" with nothing named -> close whatever is focused.
            return {
                "say": "Closing it, sir.",
                "action": {"name": "key", "params": {"combo": "alt+f4"}},
            }

    if re.search(r"\b(switch|alt.?tab|next window)\b", low):
        return {"say": "Switching, sir.", "action": {"name": "key", "params": {"combo": "alt+tab"}}}

    if re.search(r"\b(minimi[sz]e)\b", low):
        return {"say": "Minimising, sir.", "action": {"name": "key", "params": {"combo": "win+down"}}}

    if re.search(r"\b(maximi[sz]e)\b", low):
        return {"say": "Maximising, sir.", "action": {"name": "key", "params": {"combo": "win+up"}}}

    if re.search(r"\b(lock)\b", low):
        return {"say": "Locking the machine, sir.",
                "action": {"name": "key", "params": {"combo": "win+l"}}}

    return None


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


class JarvisService:
    """Voice turn orchestration: STT → plan → act → narrate → TTS."""

    def __init__(
        self,
        computer: Optional[ComputerService] = None,
        stt_service: Any = None,
        tts_service: Any = None,
    ):
        self._computer = computer
        self._stt = stt_service
        self._tts = tts_service
        # session_key -> list of {"role","content"} (LRU-bounded)
        self._history: "OrderedDict[str, List[Dict[str, str]]]" = OrderedDict()

    # ── lazy collaborators ──

    @property
    def computer(self) -> ComputerService:
        if self._computer is None:
            from services.computer import get_computer_service

            self._computer = get_computer_service()
        return self._computer

    @property
    def stt(self):
        if self._stt is None:
            from services.stt import get_stt_service

            self._stt = get_stt_service()
        return self._stt

    @property
    def tts(self):
        if self._tts is None:
            from services.tts import get_tts_service

            self._tts = get_tts_service()
        return self._tts

    # ── introspection ──

    def status(self) -> Dict[str, Any]:
        """Everything the desktop UI needs to render the Talk screen."""

        stt_stats: Dict[str, Any] = {}
        try:
            stt_stats = self.stt.get_stats() or {}
        except Exception as exc:
            logger.debug("stt stats failed: %s", exc)
        tts_stats: Dict[str, Any] = {}
        try:
            tts_stats = self.tts.get_stats() or {}
        except Exception as exc:
            logger.debug("tts stats failed: %s", exc)

        try:
            comp = self.computer.status()
        except Exception as exc:
            logger.debug("computer status failed: %s", exc)
            comp = {"enabled": False, "error": str(exc)}

        model = ""
        try:
            model = pick_chat_model()
        except Exception:
            model = ""

        return {
            "enabled": _truthy(_setting("jarvis_enabled", True), True),
            "reply_language": str(_setting("jarvis_reply_language", "en") or "en"),
            "autonomy": str(_setting("jarvis_autonomy", "full") or "full"),
            "model": model,
            "stt": stt_stats,
            "tts": tts_stats,
            "computer": comp,
            "actions": sorted(ALL_ACTIONS),
            "llm_configured": bool(model),
        }

    # ── transcription ──

    async def transcribe(self, audio_bytes: bytes) -> str:
        """Server-side STT. Raises ``JarvisUnavailable`` when it cannot run."""

        if not audio_bytes:
            raise JarvisUnavailable("No audio received")
        try:
            stats = self.stt.get_stats() or {}
        except Exception:
            stats = {}
        if not stats.get("available"):
            provider = stats.get("provider", "disabled")
            raise JarvisUnavailable(
                "Server speech-to-text is off"
                + (f" (provider: {provider})" if provider else "")
                + ". Set STT to a local Whisper model in Settings → Voice & PC, "
                "or let the app transcribe in the browser."
            )
        text = await asyncio_to_thread(self.stt.transcribe, audio_bytes)
        return (text or "").strip()

    # ── the turn ──

    async def turn(
        self,
        audio: Optional[bytes] = None,
        text: Optional[str] = None,
        *,
        owner: Optional[str] = None,
        session_id: Optional[str] = None,
        allow_actions: bool = True,
        speak: bool = True,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        """Run one spoken turn. Returns a JSON-ready dict."""

        started = time.time()
        out: Dict[str, Any] = {
            "transcript": "",
            "reply": "",
            "audio": None,
            "speak_in_browser": False,
            "actions": [],
            "model": "",
            "planner": "",
            "took_ms": 0,
        }

        # 1 — understand
        transcript = (text or "").strip()
        if not transcript and audio:
            transcript = await self.transcribe(audio)
        if not transcript:
            out["reply"] = "I did not catch that, sir. Could you say it again?"
            out["speak_in_browser"] = True
            out["took_ms"] = _ms(started)
            return out
        out["transcript"] = transcript

        control_on = computer_control_enabled()
        autonomy = str(_setting("jarvis_autonomy", "full") or "full").strip().lower()
        # "confirm" still acts — it just asks before risky actions. "off" never
        # touches the machine, which is the setting to use on a shared PC.
        allow_actions = bool(allow_actions) and control_on and autonomy in ("full", "confirm")

        # 2 — plan
        model_used = ""
        say, action = "", None
        plan_source = "heuristic"
        plan = await self._plan(transcript, owner=owner, session_id=session_id,
                                history=history, control_on=control_on)
        if plan is not None:
            model_used = plan.get("model", "") or ""
            plan_source = plan.get("source", "heuristic")
            say = str(plan.get("say") or "").strip()
            action = plan.get("action")
        out["planner"] = plan_source
        out["model"] = model_used

        if not say and action is None:
            # No model and no rule matched.
            if model_used:
                say = "I could not parse that, sir. Try rephrasing?"
            else:
                say = (
                    "I heard you, sir, but no model is configured for me to "
                    "think with. Add one under Settings → Models."
                )

        # 3 — act
        result: Optional[Dict[str, Any]] = None
        if action and allow_actions:
            result = await self._run_action(action, owner=owner)
            out["actions"].append(result)
            say = await self._narrate(transcript, action, result, owner=owner,
                                      fallback=say)
        elif action and not allow_actions:
            say = (
                "Computer control is switched off, sir. Turn it on in "
                "Settings → Voice & PC and I will do it."
            )

        reply = spoken_text(say or "")
        if not reply:
            reply = "Done, sir."
        out["reply"] = reply

        # 4 — remember
        self._remember(session_id or owner or "default", transcript, reply)

        # 5 — speak
        if speak:
            audio_b64 = await self._synthesize(reply)
            if audio_b64:
                out["audio"] = audio_b64
            else:
                out["speak_in_browser"] = True

        out["took_ms"] = _ms(started)
        return out

    # ── internals ──

    async def _plan(
        self,
        user_text: str,
        *,
        owner: Optional[str],
        session_id: Optional[str],
        history: Optional[List[Dict[str, str]]],
        control_on: bool,
    ) -> Optional[Dict[str, Any]]:
        """Ask the model for {say, action}; fall back to the rule parser."""

        model_name = pick_chat_model(owner)
        if not model_name:
            plan = heuristic_plan(user_text)
            if plan is None:
                return {"say": "", "action": None, "source": "heuristic"}
            return {
                "say": plan.get("say", ""), "action": plan.get("action"),
                "source": "heuristic",
            }

        now = datetime.now().strftime("%A %d %B %Y, %H:%M")
        host = socket.gethostname()
        system = (
            JARVIS_SYSTEM
            + "\n"
            + CONTEXT_TEMPLATE.format(
                now=now,
                host=host,
                os=f"{platform.system()} {platform.release()}",
                control="on" if control_on else "off",
            )
        )
        messages: List[Dict[str, str]] = [{"role": "system", "content": system}]
        remembered = history or self._history.get(str(session_id or owner or "default"), [])
        for item in remembered[-MAX_HISTORY_TURNS * 2 :]:
            if item.get("role") in ("user", "assistant") and item.get("content"):
                messages.append({"role": item["role"], "content": item["content"][:2000]})
        messages.append(
            {
                "role": "user",
                "content": action_planner_prompt(
                    user_text, now=now, host=host,
                    control="on" if control_on else "off",
                ),
            }
        )

        try:
            raw = await self._chat(messages, owner=owner, model_name=model_name)
        except Exception as exc:
            logger.warning("jarvis planner failed (%s); using rules", exc)
            plan = heuristic_plan(user_text)
            if plan is None:
                return {"say": "", "action": None, "source": "heuristic"}
            return {"say": plan.get("say", ""), "action": plan.get("action"),
                    "source": "heuristic"}

        parsed = extract_plan(raw)
        if not isinstance(parsed, dict):
            plan = heuristic_plan(user_text)
            if plan is None:
                return {"say": _first_sentence(raw), "action": None,
                        "source": "model", "model": model_name}
            return {"say": plan.get("say", ""), "action": plan.get("action"),
                    "source": "heuristic-rules", "model": model_name}

        action = parsed.get("action")
        if isinstance(action, dict):
            name = str(action.get("name") or "").strip().lower()
            params = action.get("params") or {}
            if not isinstance(params, dict):
                params = {}
            allowed, reason, _risk, _needs = check_action(name, params)
            if not allowed:
                logger.info("jarvis planner proposed a blocked action: %s", reason)
                action = None
                if not str(parsed.get("say") or "").strip():
                    parsed["say"] = reason
            else:
                action = {"name": name, "params": params}
        else:
            action = None

        return {
            "say": str(parsed.get("say") or "").strip(),
            "action": action,
            "source": "model",
            "model": model_name,
        }

    async def _run_action(self, action: Any, *, owner: Optional[str]) -> Dict[str, Any]:
        if not isinstance(action, dict):
            return {"ok": False, "error": "Malformed action", "blocked": True}
        name = str(action.get("name") or "").strip().lower()
        params = action.get("params") if isinstance(action.get("params"), dict) else {}
        confirm_risky = str(_setting("jarvis_autonomy", "full") or "full").lower() == "confirm"
        try:
            result = await self.computer.act(name, params, confirm_risky=confirm_risky)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        result = dict(result or {})
        result.setdefault("action", name)
        # Never ship a 2 MB screenshot back through the chat transcript.
        if "image_base64" in result:
            result["image_base64"] = None
            result["image_dropped"] = True
        result["params"] = _safe_params(params)
        return result

    async def _narrate(
        self,
        user_text: str,
        action: Dict[str, Any],
        result: Dict[str, Any],
        *,
        owner: Optional[str],
        fallback: str = "",
    ) -> str:
        """Turn an action result into one spoken English sentence."""

        model_name = pick_chat_model(owner)
        if not model_name or not _truthy(_setting("jarvis_narrate", True), True):
            return _canned_reply(action, result) or fallback
        messages = [
            {"role": "system", "content": JARVIS_SYSTEM},
            {"role": "user", "content": outcome_prompt(user_text, action, result)},
        ]
        try:
            raw = await self._chat(messages, owner=owner, model_name=model_name,
                                   timeout=30)
        except Exception as exc:
            logger.debug("jarvis narration failed: %s", exc)
            return _canned_reply(action, result) or fallback
        text = spoken_text(_first_sentence(raw))
        return text or _canned_reply(action, result) or fallback

    async def _synthesize(self, text: str) -> Optional[str]:
        """Server TTS → base64, or ``None`` to let the browser speak."""

        if not text:
            return None
        try:
            if not self.tts.available:
                return None
            stats = self.tts.get_stats() or {}
            if str(stats.get("provider") or "disabled") in ("disabled", "browser"):
                return None
        except Exception as exc:
            logger.debug("tts availability check failed: %s", exc)
            return None
        try:
            return await asyncio_to_thread(self.tts.synthesize_to_base64, text)
        except Exception as exc:
            logger.warning("jarvis TTS failed: %s", exc)
            return None

    async def _chat(
        self,
        messages: List[Dict[str, str]],
        *,
        owner: Optional[str],
        model_name: str,
        timeout: int = LLM_TIMEOUT_SECONDS,
    ) -> str:
        from src.ai_interaction import _resolve_model
        from src.llm_core import llm_call_async

        url, model, headers = await asyncio_to_thread(
            _resolve_model, model_name, owner=owner
        )
        return await llm_call_async(
            url, model, messages, headers=headers, timeout=timeout
        )

    def _remember(self, key: str, user_text: str, reply: str) -> None:
        key = str(key or "default")
        hist = self._history.setdefault(key, [])
        hist.append({"role": "user", "content": user_text})
        hist.append({"role": "assistant", "content": reply})
        del hist[:-MAX_HISTORY_TURNS * 2]
        self._history.move_to_end(key)
        while len(self._history) > HISTORY_SESSIONS:
            self._history.popitem(last=False)

    def clear_history(self, key: Optional[str] = None) -> None:
        if key is None:
            self._history.clear()
        else:
            self._history.pop(str(key), None)


# ---------------------------------------------------------------------------
# module helpers
# ---------------------------------------------------------------------------


def _ms(started: float) -> int:
    return int((time.time() - started) * 1000)


def _safe_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Clip action params for the transcript (no 5 KB typing payloads)."""

    out = {}
    for key, value in (params or {}).items():
        if isinstance(value, str) and len(value) > 200:
            out[key] = value[:200] + "…"
        else:
            out[key] = value
    return out


def _first_sentence(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    match = re.search(r"(.+?[.!?])(\s|$)", raw, re.DOTALL)
    return (match.group(1) if match else raw[:400]).strip()


def _canned_reply(action: Dict[str, Any], result: Dict[str, Any]) -> str:
    """English one-liner used when no model is available for narration."""

    name = str((action or {}).get("name") or "action")
    if not result.get("ok"):
        if result.get("needs_confirmation"):
            return str(result.get("confirm_hint") or f"Shall I {name}, sir?")
        return f"That did not work, sir — {result.get('error') or 'unknown error'}."
    if name == "open":
        return f"Opened {result.get('target', 'it')}, sir."
    if name == "type":
        return "Typed it, sir."
    if name == "key":
        return "Done, sir."
    if name == "screenshot":
        return "I have the screen, sir."
    if name == "volume":
        if result.get("level") is not None:
            return f"Volume is at {result['level']} percent, sir."
        return "Adjusted the volume, sir."
    if name == "info":
        info = result.get("info") or {}
        bits = []
        if info.get("cpu_percent") is not None:
            bits.append(f"CPU at {round(info['cpu_percent'])} percent")
        if info.get("ram_percent") is not None:
            bits.append(f"memory at {round(info['ram_percent'])} percent")
        if info.get("battery_percent") is not None:
            bits.append(f"battery at {round(info['battery_percent'])} percent")
        return ("Machine status, sir: " + ", ".join(bits) + ".") if bits else "Checked, sir."
    if name == "windows":
        wins = result.get("windows") or []
        return f"There are {len(wins)} windows open, sir."
    if name == "clipboard_get":
        text = str(result.get("text") or "")
        return f"The clipboard says: {text[:200]}" if text else "The clipboard is empty, sir."
    if name == "kill":
        return str(result.get("killed") or "Terminated it, sir.")
    return "Done, sir."


async def asyncio_to_thread(fn, *args, **kwargs):
    """Run a blocking callable in a worker thread (async context)."""

    import asyncio
    import functools

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))


# ---------------------------------------------------------------------------
# singleton
# ---------------------------------------------------------------------------

_jarvis_service: Optional[JarvisService] = None


def get_jarvis_service() -> JarvisService:
    global _jarvis_service
    if _jarvis_service is None:
        _jarvis_service = JarvisService()
    return _jarvis_service
