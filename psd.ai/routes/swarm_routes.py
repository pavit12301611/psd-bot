# routes/swarm_routes.py
"""Swarm routes — the whole local model group working as one team.

POST /api/swarm/chat streams the orchestration as SSE events:

  {"type":"phase","phase":"plan","manager":{...}}
  {"type":"plan","steps":[{"kind","task","worker","kind_label"}]}
  {"type":"step_done","index":0,"worker":"...","ok":true,"text":"..."}
  {"type":"synth_delta","delta":"..."}
  {"type":"final","text":"...","sources":[...],"steps":[...]}

The engine itself lives in src/swarm.py; this file is auth + HTTP only.
"""
import json
import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src import swarm
from src.auth_helpers import effective_user, require_api_token_scope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/swarm", tags=["swarm"])


class SwarmSettingsBody(BaseModel):
    internet: Optional[bool] = None
    parallel: Optional[bool] = None
    auto_learn: Optional[bool] = None
    verify: Optional[bool] = None
    cloud_workers: Optional[bool] = None
    max_steps: Optional[int] = None
    disabled_workers: Optional[List[str]] = None


class KnowledgeBody(BaseModel):
    text: str


class LearnUrlBody(BaseModel):
    url: str


class SwarmChatBody(BaseModel):
    message: str
    history: Optional[List[dict]] = None


def setup_swarm_routes() -> APIRouter:
    @router.get("/status")
    def swarm_status(request: Request):
        owner = effective_user(request) or ""
        return swarm.status(owner)

    @router.post("/settings")
    def update_settings(body: SwarmSettingsBody, request: Request):
        from core.middleware import require_admin
        require_admin(request)
        update: dict = {}
        if body.internet is not None:
            update["internet"] = bool(body.internet)
        if body.parallel is not None:
            update["parallel"] = bool(body.parallel)
        if body.auto_learn is not None:
            update["auto_learn"] = bool(body.auto_learn)
        if body.verify is not None:
            update["verify"] = bool(body.verify)
        if body.cloud_workers is not None:
            update["cloud_workers"] = bool(body.cloud_workers)
        if body.max_steps is not None:
            update["max_steps"] = int(body.max_steps)
        if body.disabled_workers is not None:
            update["disabled_workers"] = [str(s) for s in body.disabled_workers][:12]
        return swarm.save_swarm_settings(update)

    @router.post("/chat")
    async def swarm_chat(request: Request):
        require_api_token_scope(request, "chat")
        owner = effective_user(request) or ""
        body: dict = {}
        try:
            if request.headers.get("content-type", "").startswith("application/json"):
                body = await request.json()
            else:
                form = await request.form()
                body = dict(form)
        except Exception:
            raise HTTPException(400, "Invalid swarm chat request")

        message = str(body.get("message") or "").strip()
        if not message:
            raise HTTPException(400, "message is required")
        raw_history = body.get("history") or []
        history: List[dict] = []
        if isinstance(raw_history, str):
            try:
                raw_history = json.loads(raw_history)
            except ValueError:
                raw_history = []
        if isinstance(raw_history, list):
            for item in raw_history[-8:]:
                if isinstance(item, dict) and item.get("role") in ("user", "assistant") and item.get("content"):
                    history.append({"role": item["role"], "content": str(item["content"])[:800]})

        async def event_stream():
            try:
                async for event in swarm.orchestrate(message, owner=owner, history=history):
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except Exception as exc:
                logger.exception("swarm chat failed")
                yield f"data: {json.dumps({'type': 'error', 'error': str(exc)[:300]})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/knowledge")
    def get_knowledge(request: Request):
        owner = effective_user(request) or ""
        entries = swarm.load_knowledge(owner)
        entries.sort(key=lambda e: e.get("ts", 0), reverse=True)
        return {"entries": entries}

    @router.post("/knowledge")
    def add_knowledge(body: KnowledgeBody, request: Request):
        owner = effective_user(request) or ""
        item = swarm.add_knowledge(body.text, owner=owner, source="user")
        if item is None:
            raise HTTPException(409, "Already remembered (or empty)")
        return item

    @router.delete("/knowledge/{entry_id}")
    def remove_knowledge(entry_id: str, request: Request):
        owner = effective_user(request) or ""
        if not swarm.delete_knowledge(entry_id, owner):
            raise HTTPException(404, "Not found")
        return {"ok": True}

    @router.post("/knowledge/clear")
    def clear_knowledge_route(request: Request):
        from core.middleware import require_admin
        require_admin(request)
        owner = effective_user(request) or ""
        return {"removed": swarm.clear_knowledge(owner)}

    @router.post("/learn/url")
    async def learn_from_url(body: LearnUrlBody, request: Request):
        """Fetch a real page from the internet and remember its key facts."""
        owner = effective_user(request) or ""
        url = (body.url or "").strip()
        if not url:
            raise HTTPException(400, "url is required")

        from services.search.content import fetch_webpage_content
        try:
            content = await swarm.asyncio.to_thread(fetch_webpage_content, url, 12)
        except Exception as exc:
            raise HTTPException(502, f"Could not fetch that page: {str(exc)[:200]}")
        text = str(content or "").strip()
        if not text:
            raise HTTPException(502, "That page returned no readable text")

        facts: List[str] = []
        workers = [w for w in swarm.build_roster(owner) if w.enabled and w.base_url and not w.is_manager]
        fast = swarm.route_worker(swarm.build_roster(owner), "fast")
        extractor = fast or (workers[0] if workers else None)
        if extractor:
            try:
                reply, _ = await swarm._chat(
                    extractor.base_url.rstrip("/") + "/chat/completions",
                    extractor.model_id,
                    [{"role": "user", "content": (
                        "List the 3-5 most durable, useful facts from this page. "
                        "One per line, plain sentences, no dashes or numbering.\n\n"
                        f"URL: {url}\n\nPage text:\n{text[:6000]}"
                    )}],
                    timeout=90.0,
                    temperature=0.2,
                )
                for line in (reply or "").splitlines():
                    line = line.strip().lstrip("-•* ").strip()
                    if len(line) >= 12:
                        facts.append(line[:500])
            except Exception as exc:
                logger.warning("swarm learn/url extraction failed: %s", exc)
        if not facts:
            from services.search.content import extract_key_points
            facts = [p for p in extract_key_points(text)[:5] if len(p) >= 12]

        saved = []
        for fact in facts[:5]:
            item = swarm.add_knowledge(fact, owner=owner, source="url", url=url)
            if item:
                saved.append(item)
        return {"ok": True, "learned": len(saved), "facts": saved}

    return router
