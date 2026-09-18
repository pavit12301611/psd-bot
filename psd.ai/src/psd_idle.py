"""Bounded idle learning for the PSD local model.

The normal chat path already performs small asynchronous memory/skill
extractions after a response. This service is the quiet, catch-up pass for a
user who leaves psd.ai open: after a configurable idle period it revisits one
completed conversation, extracts only durable facts and reusable procedures,
and then waits for the next cycle.

Important boundaries:
* work is owner-scoped and persisted in a small state file;
* one session is processed per cycle, so idle learning cannot monopolise a
  local model or grow without bound;
* active requests and model streams always win;
* code edits are opt-in, require an existing absolute workspace, and are never
  enabled by default. The normal on-command coding agent remains available.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from core.atomic_io import atomic_write_json
from core.constants import AUTH_FILE, DATA_DIR
from core.database import Session as DbSession, SessionLocal
from src.psd_model import is_psd_model

logger = logging.getLogger(__name__)


DEFAULT_IDLE_SETTINGS: dict[str, Any] = {
    "idle_learning_enabled": True,
    "idle_after_minutes": 10,
    "idle_cycle_minutes": 30,
    "auto_memory": True,
    "auto_skills": True,
    "idle_code_changes": False,
    "idle_workspace": "",
    "idle_max_sessions": 1,
}

_STATE_FILE = Path(DATA_DIR) / "psd_idle_state.json"
_TICK_SECONDS = 30.0
_MAX_IDLE_AFTER_MINUTES = 24 * 60
_MAX_CYCLE_MINUTES = 7 * 24 * 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: Optional[datetime]) -> str:
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _owner_key(owner: Optional[str]) -> str:
    return owner or "__single_user__"


def _load_state() -> dict[str, Any]:
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        atomic_write_json(str(_STATE_FILE), state, indent=2)
    except OSError:
        logger.debug("Could not persist PSD idle state", exc_info=True)


def _auth_owners() -> list[Optional[str]]:
    """Return real owners without creating synthetic assistant identities."""
    if os.getenv("AUTH_ENABLED", "true").strip().lower() == "false":
        try:
            from src.owner_identity import DEFAULT_LOCAL_OWNER

            return [DEFAULT_LOCAL_OWNER, None]
        except Exception:
            return [None]
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as fh:
            users = json.load(fh).get("users", {})
        if isinstance(users, dict) and users:
            return [str(name) for name in users if str(name).strip()]
    except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
        pass
    return [None]


def _user_prefs(owner: Optional[str]) -> dict[str, Any]:
    try:
        from routes.prefs_routes import _load_for_user
        from src.owner_identity import DEFAULT_LOCAL_OWNER

        # Auth-disabled UI requests use the flat/single-user prefs bucket,
        # while newer rows may be stamped with DEFAULT_LOCAL_OWNER.
        prefs_owner = None if owner == DEFAULT_LOCAL_OWNER else owner
        raw = _load_for_user(prefs_owner) or {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _settings_for(owner: Optional[str]) -> dict[str, Any]:
    prefs = dict(DEFAULT_IDLE_SETTINGS)
    prefs.update({key: value for key, value in _user_prefs(owner).items() if key in DEFAULT_IDLE_SETTINGS})
    # A deployment-wide off switch is useful for servers that do not want any
    # background model work, while the per-user toggle remains the normal UI.
    env = os.getenv("PSD_AI_IDLE_LEARNING", "").strip().lower()
    if env in {"0", "false", "no", "off"}:
        prefs["idle_learning_enabled"] = False
    return prefs


def _safe_workspace(raw: Any) -> Optional[str]:
    """Return a tool-policy-vetted workspace, never merely an existing folder."""
    path = str(raw or "").strip()
    if not path:
        return None
    try:
        from src.tool_execution import vet_workspace

        return vet_workspace(path)
    except Exception:
        # The normal app has tool_execution available. Keep status harmless in
        # a reduced/test process rather than treating an arbitrary directory as
        # writable when the policy module cannot be imported.
        return None


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return default if value is None else bool(value)


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(message, dict):
        content = message.get("content", content)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(block.get("text") or block.get("content") or "")
            for block in content
            if isinstance(block, dict)
        )
    return str(content or "")


def _tool_count(session: Any) -> int:
    count = 0
    for message in getattr(session, "history", []) or []:
        meta = getattr(message, "metadata", None)
        if not isinstance(meta, dict):
            continue
        events = meta.get("tool_events")
        if isinstance(events, list):
            count += len(events)
    return count


def _looks_like_coding_session(session: Any) -> bool:
    text = "\n".join(_message_text(m) for m in (getattr(session, "history", []) or [])[-8:])
    return bool(
        re.search(
            r"\b(code|coding|bug|debug|debugging|fix|implement|refactor|test|build|compile|"
            r"repo|repository|workspace|file|python|typescript|javascript|api|component|function|class)\b",
            text,
            re.IGNORECASE,
        )
    )


def _recent_session(owner: Optional[str], *, idle_after_minutes: int, processed: dict[str, Any], force: bool = False):
    """Find one completed, owner-scoped conversation not already consumed."""
    cutoff = _utcnow() - timedelta(minutes=idle_after_minutes)
    db = SessionLocal()
    try:
        query = db.query(DbSession).filter(
            DbSession.archived == False,  # noqa: E712
            DbSession.last_message_at.isnot(None),
            DbSession.message_count >= 4,
        )
        if owner:
            query = query.filter(DbSession.owner == owner)
        else:
            query = query.filter((DbSession.owner.is_(None)) | (DbSession.owner == ""))
        rows = query.order_by(DbSession.last_message_at.desc()).limit(25).all()
        for row in rows:
            if (row.name or "").strip().lower() == "assistant" or row.crew_member_id:
                continue
            if not force and row.last_message_at:
                last_message = row.last_message_at
                if last_message.tzinfo is not None:
                    last_message = last_message.replace(tzinfo=None)
                if last_message > cutoff:
                    continue
            marker = processed.get(row.id) or {}
            last_seen = str(marker.get("message_at") or "")
            current = _iso(row.last_message_at)
            if last_seen and current and current <= last_seen:
                continue
            return row.id, current, row.mode or ""
    finally:
        db.close()
    return None


class PsdIdleLearner:
    """One low-priority idle-learning worker shared by the app process."""

    def __init__(self, session_manager, memory_manager, memory_vector, skills_manager):
        self.session_manager = session_manager
        self.memory_manager = memory_manager
        self.memory_vector = memory_vector
        self.skills_manager = skills_manager
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._state = _load_state()
        if not isinstance(self._state.get("owners"), dict):
            self._state["owners"] = {}
        if not isinstance(self._state.get("processed"), dict):
            self._state["processed"] = {}
        self._lock = asyncio.Lock()
        self._manual_tasks: set[asyncio.Task] = set()

    def start(self) -> Optional[asyncio.Task]:
        if self._running:
            return self._task
        env = os.getenv("PSD_AI_IDLE_LEARNING", "").strip().lower()
        if env in {"0", "false", "no", "off"}:
            logger.info("PSD idle learning disabled by PSD_AI_IDLE_LEARNING")
            return None
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="psd-idle-learning")
        logger.info("PSD idle learning worker started")
        return self._task

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for task in list(self._manual_tasks):
            task.cancel()
        self._manual_tasks.clear()

    def settings(self, owner: Optional[str]) -> dict[str, Any]:
        values = _settings_for(owner)
        values["idle_after_minutes"] = _bounded_int(values.get("idle_after_minutes"), 10, 1, _MAX_IDLE_AFTER_MINUTES)
        values["idle_cycle_minutes"] = _bounded_int(values.get("idle_cycle_minutes"), 30, 5, _MAX_CYCLE_MINUTES)
        values["idle_max_sessions"] = _bounded_int(values.get("idle_max_sessions"), 1, 1, 3)
        values["idle_workspace"] = str(values.get("idle_workspace") or "").strip()
        for key in ("idle_learning_enabled", "auto_memory", "auto_skills", "idle_code_changes"):
            values[key] = _bool(values.get(key), bool(DEFAULT_IDLE_SETTINGS[key]))
        return values

    def status(self, owner: Optional[str]) -> dict[str, Any]:
        settings = self.settings(owner)
        owners = self._state.get("owners") or {}
        marker = owners.get(_owner_key(owner), {})
        if not marker and owner is None and os.getenv("AUTH_ENABLED", "true").strip().lower() == "false":
            try:
                from src.owner_identity import DEFAULT_LOCAL_OWNER

                marker = owners.get(_owner_key(DEFAULT_LOCAL_OWNER), {})
            except Exception:
                pass
        workspace = settings.get("idle_workspace") or ""
        workspace_ok = bool(_safe_workspace(workspace))
        return {
            "running": bool(self._running),
            "settings": settings,
            "last_run_at": marker.get("run_at") or "",
            "last_session_id": marker.get("session_id") or "",
            "last_result": marker.get("result") or "",
            "workspace_valid": workspace_ok,
            "workspace": workspace,
        }

    def request_run(self, owner: Optional[str]) -> str:
        """Queue a manual catch-up pass and return immediately."""
        task = asyncio.create_task(self.learn_owner(owner, force=True), name="psd-manual-learning")
        self._manual_tasks.add(task)
        task.add_done_callback(self._manual_tasks.discard)
        return task.get_name()

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(_TICK_SECONDS)
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("PSD idle-learning tick failed", exc_info=True)

    async def tick(self) -> None:
        from src.interactive_gate import has_active_foreground_work

        if has_active_foreground_work():
            return
        for owner in _auth_owners():
            settings = self.settings(owner)
            if not settings["idle_learning_enabled"]:
                continue
            marker = (self._state.get("owners") or {}).get(_owner_key(owner), {})
            last_run = marker.get("run_at")
            if last_run:
                try:
                    when = datetime.fromisoformat(str(last_run).replace("Z", "+00:00"))
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) - when < timedelta(minutes=settings["idle_cycle_minutes"]):
                        continue
                except (TypeError, ValueError):
                    pass
            result = await self.learn_owner(owner)
            # If this partition has no work, let the next owner compete in the
            # same low-priority tick. Once a pass is queued/completed, stop so
            # one user cannot monopolise a shared local model.
            if result.get("skipped") in {"nothing_new", "disabled"}:
                continue
            return

    async def learn_owner(self, owner: Optional[str], *, force: bool = False) -> dict[str, Any]:
        from src.interactive_gate import has_active_foreground_work

        async with self._lock:
            if has_active_foreground_work() and not force:
                return {"ok": False, "skipped": "foreground_active"}
            settings = self.settings(owner)
            if not settings["idle_learning_enabled"] and not force:
                return {"ok": False, "skipped": "disabled"}
            key = _owner_key(owner)
            processed = self._state.setdefault("processed", {})
            candidate = _recent_session(
                owner,
                idle_after_minutes=settings["idle_after_minutes"],
                processed=processed,
                force=force,
            )
            if not candidate:
                return {"ok": True, "skipped": "nothing_new"}

            session_id, message_at, mode = candidate
            try:
                session = self.session_manager.get_session(session_id)
                from src.task_endpoint import resolve_task_endpoint

                endpoint_url, model, headers = resolve_task_endpoint(
                    session.endpoint_url,
                    session.model,
                    session.headers,
                    owner=owner,
                )
                if not endpoint_url or not model:
                    raise RuntimeError("No model endpoint is available for PSD idle learning")

                results: list[str] = []
                if settings["auto_memory"]:
                    from services.memory.memory_extractor import extract_and_store

                    await extract_and_store(
                        session,
                        self.memory_manager,
                        self.memory_vector,
                        endpoint_url,
                        model,
                        headers or {},
                        workload="idle",
                    )
                    results.append("memory")

                tool_count = _tool_count(session)
                if settings["auto_skills"] and (mode == "agent" or tool_count >= 2) and self.skills_manager:
                    from services.memory.skill_extractor import maybe_extract_skill

                    entry = await maybe_extract_skill(
                        session,
                        self.skills_manager,
                        endpoint_url,
                        model,
                        headers or {},
                        max(2, tool_count),
                        tool_count,
                        owner=owner,
                        workload="idle",
                    )
                    if entry:
                        results.append("skill")

                code_result = ""
                workspace = settings.get("idle_workspace") or ""
                safe_workspace = _safe_workspace(workspace)
                if (
                    settings["idle_code_changes"]
                    and safe_workspace
                    and (mode == "agent" or _looks_like_coding_session(session))
                ):
                    code_result = await self._run_code_pass(
                        session,
                        endpoint_url,
                        model,
                        headers or {},
                        owner,
                        safe_workspace,
                    )
                    if code_result:
                        results.append("code")

                marker = {
                    "run_at": _iso(_utcnow()),
                    "session_id": session_id,
                    "message_at": message_at,
                    "result": ", ".join(results) or "reviewed",
                    "model": model,
                    "psd_model": is_psd_model(model),
                }
                self._state.setdefault("owners", {})[key] = marker
                processed[session_id] = marker
                # Keep the state file bounded if a user has thousands of chats.
                if len(processed) > 200:
                    for old_id in list(processed)[:-200]:
                        processed.pop(old_id, None)
                _save_state(self._state)
                logger.info("PSD idle learning complete owner=%s session=%s result=%s", owner, session_id, marker["result"])
                return {"ok": True, "result": marker["result"], "session_id": session_id, "code": code_result}
            except asyncio.CancelledError:
                # Foreground local-model work deliberately preempts an idle
                # call. Do not let that cancellation kill the long-lived
                # worker task; the next tick can retry once the chat is done.
                logger.info("PSD idle learning preempted by foreground work owner=%s", owner)
                return {"ok": False, "skipped": "foreground_preempted", "session_id": session_id}
            except Exception as exc:
                # Mark the candidate as attempted so one broken endpoint cannot
                # be retried every 30 seconds. A later conversation or manual
                # "Learn now" can try again with a fresh route.
                marker = {
                    "run_at": _iso(_utcnow()),
                    "session_id": session_id,
                    "message_at": message_at,
                    "result": f"error: {type(exc).__name__}",
                }
                self._state.setdefault("owners", {})[key] = marker
                processed[session_id] = marker
                _save_state(self._state)
                logger.warning("PSD idle learning failed for %s: %s", owner, exc)
                return {"ok": False, "error": str(exc), "session_id": session_id}

    async def _run_code_pass(self, session, endpoint_url: str, model: str, headers: dict, owner: Optional[str], workspace: str) -> str:
        """Run a tightly bounded, explicitly opted-in coding improvement pass."""
        from src.agent_loop import stream_agent_loop

        recent = []
        for message in (getattr(session, "history", []) or [])[-6:]:
            text = re.sub(r"\s+", " ", _message_text(message)).strip()
            if text:
                recent.append(f"{getattr(message, 'role', 'message')}: {text[:900]}")
        transcript = "\n".join(recent)[-4500:]
        prompt = (
            "You are running PSD's explicitly enabled idle coding pass. Review the recent "
            "coding conversation below against the active workspace. Only make a code change "
            "when it is a small, clearly justified fix or missing test directly supported by "
            "the conversation. Inspect before editing, keep the change in the workspace, and "
            "run the narrowest relevant verification. Do not change dependencies, secrets, "
            "authentication, deployment, or destructive/system files. If there is no clear "
            "safe improvement, make no edits and say so.\n\n"
            f"Active workspace: {workspace}\nRecent conversation:\n{transcript}"
        )
        toolset = {
            "get_workspace", "grep", "glob", "ls", "read_file", "code_stats",
            "edit_file", "write_file", "apply_patch", "todowrite", "todoread",
            "bash", "python",
        }
        output: list[str] = []
        async for event in stream_agent_loop(
            endpoint_url,
            model,
            [{"role": "user", "content": prompt}],
            headers=headers,
            owner=owner,
            relevant_tools=toolset,
            forced_tools=toolset,
            workspace=workspace,
            max_rounds=5,
            max_tool_calls=20,
            max_tokens=1800,
            context_length=0,
            workload="idle",
        ):
            if not event.startswith("data: "):
                continue
            try:
                payload = json.loads(event[6:])
            except (json.JSONDecodeError, TypeError):
                continue
            if payload.get("delta") and not payload.get("thinking"):
                output.append(str(payload["delta"]))
        return " ".join(output)[-1200:]


__all__ = ["DEFAULT_IDLE_SETTINGS", "PsdIdleLearner"]
