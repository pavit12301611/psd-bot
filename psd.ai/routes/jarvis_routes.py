"""Jarvis routes — the voice agent's HTTP surface.

    GET  /api/jarvis/status        what this machine can do right now
    POST /api/jarvis/transcribe    audio  -> text            (server STT)
    POST /api/jarvis/turn          audio|text -> reply + audio + actions
    POST /api/jarvis/act           run one PC action
    GET  /api/jarvis/screen        screenshot (PNG)
    POST /api/jarvis/reset         forget the rolling voice history

Gating mirrors ``routes/shell_routes.py``: these endpoints drive the real
desktop, so they are admin / single-user only, and cross-site navigations are
rejected. Every action still passes through the computer-control policy in
``services.computer.policy`` before a single input event is synthesised.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from core.middleware import INTERNAL_TOOL_USER
from src.tool_security import owner_is_admin_or_single_user
from src.upload_limits import STT_MAX_AUDIO_BYTES, read_upload_limited

logger = logging.getLogger(__name__)

# Audio clips are short; a spoken turn is seconds, not minutes.
JARVIS_AUDIO_MAX_BYTES = min(STT_MAX_AUDIO_BYTES, 25 * 1024 * 1024)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _current_user(request: Request) -> Optional[str]:
    return getattr(request.state, "current_user", None)


def _require_owner(request: Request) -> Optional[str]:
    """Admin / single-user only. Returns the owner string to pass downwards.

    Raises 403 for everyone else — including bare API tokens, which must not
    be able to drive the desktop.
    """

    user = _current_user(request)
    if user == INTERNAL_TOOL_USER:
        return None
    if not user or user == "api":
        raise HTTPException(403, "Sign in to use Jarvis")
    if not owner_is_admin_or_single_user(user):
        raise HTTPException(403, "Admin only")
    return user


def _reject_cross_site(request: Request) -> None:
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Cross-site request rejected")


def _enabled_or_503() -> None:
    from src.settings import get_setting

    raw = get_setting("jarvis_enabled", True)
    if str(raw).strip().lower() in {"0", "false", "no", "off"}:
        raise HTTPException(503, "Jarvis voice mode is switched off")


# ---------------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------------


class TurnRequest(BaseModel):
    text: str = ""
    session_id: Optional[str] = None
    allow_actions: bool = True
    speak: bool = True
    history: Optional[list] = None


class ActRequest(BaseModel):
    action: str = Field(..., min_length=1, max_length=64)
    params: Dict[str, Any] = Field(default_factory=dict)
    confirm: bool = False


class ResetRequest(BaseModel):
    session_id: Optional[str] = None


# ---------------------------------------------------------------------------
# router
# ---------------------------------------------------------------------------


def setup_jarvis_routes(jarvis_service, computer_service=None):
    """Build the Jarvis router.

    ``computer_service`` is optional; when omitted the router resolves the
    process-wide singleton lazily, which keeps app wiring simple.
    """

    router = APIRouter(prefix="/api/jarvis", tags=["jarvis"])

    def _computer():
        if computer_service is not None:
            return computer_service
        from services.computer import get_computer_service

        return get_computer_service()

    # ---------------------------------------------------------------- status

    @router.get("/status")
    async def jarvis_status(request: Request):
        try:
            return jarvis_service.status()
        except Exception as exc:
            logger.error("jarvis status failed: %s", exc, exc_info=True)
            raise HTTPException(500, f"Jarvis status failed: {exc}")

    # ----------------------------------------------------------- transcribe

    @router.post("/transcribe")
    async def jarvis_transcribe(request: Request, file: UploadFile = File(...)):
        _reject_cross_site(request)
        _require_owner(request)
        audio = await read_upload_limited(file, JARVIS_AUDIO_MAX_BYTES, "Audio clip")
        if not audio:
            raise HTTPException(400, "Empty audio clip")
        try:
            text = await jarvis_service.transcribe(audio)
        except Exception as exc:
            # JarvisUnavailable carries the actionable message.
            raise HTTPException(503, str(exc))
        if not text:
            return {"text": "", "empty": True}
        return {"text": text}

    # ------------------------------------------------------------------ turn

    @router.post("/turn")
    async def jarvis_turn(
        request: Request,
        file: Optional[UploadFile] = File(None),
        text: str = Form(""),
        session_id: str = Form(""),
        allow_actions: str = Form("1"),
        speak: str = Form("1"),
    ):
        """Run one spoken turn.

        Accepts either multipart audio (``file``) or a plain-text form field
        (``text``) — the desktop uses text when the browser transcribed the
        clip with the Web Speech API.
        """

        _reject_cross_site(request)
        owner = _require_owner(request)
        _enabled_or_503()

        audio: Optional[bytes] = None
        if file is not None:
            audio = await read_upload_limited(file, JARVIS_AUDIO_MAX_BYTES, "Audio clip")

        spoken = (text or "").strip()
        if not spoken and not audio:
            # Maybe it is JSON (the API is also used by scripts/tests).
            try:
                body = await request.json()
                if isinstance(body, dict):
                    spoken = str(body.get("text") or "").strip()
                    session_id = str(body.get("session_id") or session_id or "")
                    allow_actions = str(body.get("allow_actions", allow_actions))
                    speak = str(body.get("speak", speak))
            except Exception:
                pass
        if not spoken and not audio:
            raise HTTPException(400, "Send audio (file) or text")

        try:
            result = await jarvis_service.turn(
                audio=audio,
                text=spoken or None,
                owner=owner,
                session_id=session_id or None,
                allow_actions=str(allow_actions).strip().lower()
                not in {"0", "false", "no", "off"},
                speak=str(speak).strip().lower() not in {"0", "false", "no", "off"},
            )
        except Exception as exc:
            logger.error("jarvis turn failed: %s", exc, exc_info=True)
            raise HTTPException(500, f"Voice turn failed: {exc}")
        return result

    # ------------------------------------------------------------------- act

    @router.post("/act")
    async def jarvis_act(request: Request, body: ActRequest):
        """Execute a single PC action (used by the UI's confirm chip)."""

        _reject_cross_site(request)
        _require_owner(request)
        _enabled_or_503()

        params = dict(body.params or {})
        if body.confirm:
            params["confirm"] = True
        try:
            result = await _computer().act(body.action, params)
        except Exception as exc:
            logger.error("jarvis act failed: %s", exc, exc_info=True)
            raise HTTPException(500, f"Action failed: {exc}")

        # A screenshot action carries megabytes of base64; keep it, but only
        # for this endpoint (the turn endpoint drops it from the transcript).
        result = dict(result or {})
        if result.get("image_base64") and len(result["image_base64"]) > 12 * 1024 * 1024:
            result["image_base64"] = None
            result["error"] = "Screenshot too large to return"
            result["ok"] = False
        return result

    # ---------------------------------------------------------------- screen

    @router.get("/screen")
    async def jarvis_screen(request: Request, monitor: int = 0):
        """PNG screenshot of the primary monitor. Admin only."""

        _reject_cross_site(request)
        _require_owner(request)
        _enabled_or_503()
        try:
            data = await _computer().screenshot(int(monitor or 0))
        except Exception as exc:
            raise HTTPException(500, f"Screenshot failed: {exc}")
        return Response(content=data, media_type="image/png")

    # ----------------------------------------------------------------- reset

    @router.post("/reset")
    async def jarvis_reset(request: Request, body: Optional[ResetRequest] = None):
        _reject_cross_site(request)
        _require_owner(request)
        try:
            jarvis_service.clear_history(
                (body.session_id if body else None) or None
            )
        except Exception as exc:
            raise HTTPException(500, f"Could not reset: {exc}")
        return {"ok": True}

    return router
