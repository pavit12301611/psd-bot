"""PSD profile, idle-learning, and local-model status routes."""

from __future__ import annotations

import json
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.database import ModelEndpoint, SessionLocal
from src.auth_helpers import require_user
from src.psd_model import PSD_MODEL_ID, psd_profile
from routes.prefs_routes import _load_for_user, _save_for_user


class PsdSettingsPatch(BaseModel):
    idle_learning_enabled: Optional[bool] = None
    idle_after_minutes: Optional[int] = None
    idle_cycle_minutes: Optional[int] = None
    auto_memory: Optional[bool] = None
    auto_skills: Optional[bool] = None
    idle_code_changes: Optional[bool] = None
    idle_workspace: Optional[str] = None
    idle_max_sessions: Optional[int] = None


_ALLOWED = {
    "idle_learning_enabled",
    "idle_after_minutes",
    "idle_cycle_minutes",
    "auto_memory",
    "auto_skills",
    "idle_code_changes",
    "idle_workspace",
    "idle_max_sessions",
}


def _cached_models(endpoint: ModelEndpoint) -> list[str]:
    values = []
    for field in (getattr(endpoint, "cached_models", None), getattr(endpoint, "pinned_models", None)):
        if not field:
            continue
        try:
            raw = json.loads(field) if isinstance(field, str) else field
        except (TypeError, ValueError):
            raw = []
        if isinstance(raw, list):
            values.extend(str(item).strip() for item in raw if str(item).strip())
    return list(dict.fromkeys(values))


def _find_psd_endpoint(owner: Optional[str]):
    db = SessionLocal()
    try:
        query = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        if owner:
            query = query.filter((ModelEndpoint.owner == None) | (ModelEndpoint.owner == owner))  # noqa: E711
        for endpoint in query.order_by(ModelEndpoint.created_at.asc()).all():
            models = _cached_models(endpoint)
            if endpoint.id.startswith("local-llama-psd") or PSD_MODEL_ID in models:
                return {
                    "id": endpoint.id,
                    "name": endpoint.name,
                    "base_url": endpoint.base_url,
                    "model": PSD_MODEL_ID if (
                        PSD_MODEL_ID in models or endpoint.id.startswith("local-llama-psd")
                    ) else (models[0] if models else PSD_MODEL_ID),
                    "models": models,
                    "supports_tools": endpoint.supports_tools,
                }
    finally:
        db.close()
    return None


def _owner(request: Request) -> Optional[str]:
    # PSD settings, status, and manual learning are owner-scoped. This keeps an
    # unauthenticated request from reading/changing a named user's prefs while
    # retaining the existing single-user/first-run behavior of require_user.
    return require_user(request) or None


def setup_psd_routes(learner) -> APIRouter:
    router = APIRouter(prefix="/api/psd", tags=["psd"])

    @router.get("/status")
    async def psd_status(request: Request):
        owner = _owner(request)
        endpoint = _find_psd_endpoint(owner)
        settings = learner.status(owner) if learner else {"settings": {}}
        profile = psd_profile(
            available=bool(endpoint),
            endpoint_id=(endpoint or {}).get("id", ""),
            endpoint_url=(endpoint or {}).get("base_url", ""),
            model=(endpoint or {}).get("model", PSD_MODEL_ID),
            endpoint_name=(endpoint or {}).get("name", ""),
            models=(endpoint or {}).get("models", []),
            supports_tools=(endpoint or {}).get("supports_tools"),
        )
        return {"profile": profile, "idle": settings}

    @router.patch("/settings")
    async def update_psd_settings(payload: PsdSettingsPatch, request: Request):
        owner = _owner(request)
        prefs = _load_for_user(owner)
        patch = payload.model_dump(exclude_none=True) if hasattr(payload, "model_dump") else payload.dict(exclude_none=True)
        for key, value in patch.items():
            if key not in _ALLOWED:
                continue
            if key == "idle_workspace":
                value = str(value or "").strip()
                if value and not os.path.isabs(value):
                    raise HTTPException(status_code=400, detail="Idle workspace must be an absolute path")
                value = value[:1000]
            elif key == "idle_after_minutes":
                value = max(1, min(24 * 60, int(value)))
            elif key == "idle_cycle_minutes":
                value = max(5, min(7 * 24 * 60, int(value)))
            elif key == "idle_max_sessions":
                value = max(1, min(3, int(value)))
            prefs[key] = value
        _save_for_user(owner, prefs)
        return learner.status(owner) if learner else {"settings": prefs}

    @router.post("/learn-now")
    async def psd_learn_now(request: Request):
        owner = _owner(request)
        if not learner:
            raise HTTPException(status_code=503, detail="PSD idle learner is unavailable")
        job = learner.request_run(owner)
        return {"ok": True, "queued": True, "job": job}

    return router
