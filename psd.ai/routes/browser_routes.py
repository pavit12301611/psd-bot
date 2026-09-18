"""Embedded Browser API Routes.

Exposes endpoints for the embedded browser GUI and live action stream:
- GET  /api/browser/state
- GET  /api/browser/view
- GET  /api/browser/engines
- POST /api/browser/navigate
- POST /api/browser/search
- POST /api/browser/click
- POST /api/browser/type
- POST /api/browser/back
- POST /api/browser/forward
- POST /api/browser/reload
- GET  /api/browser/events (SSE)
"""

import asyncio
import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from services.browser import get_browser_service, SEARCH_ENGINES

logger = logging.getLogger(__name__)


async def _parse_body(request: Request) -> Dict[str, Any]:
    """Parse JSON or form parameters from request."""
    values: Dict[str, Any] = dict(request.query_params)
    content_type = (request.headers.get("content-type") or "").lower()
    try:
        if "application/json" in content_type:
            body = await request.json()
            if isinstance(body, dict):
                values.update(body)
        else:
            form = await request.form()
            values.update(dict(form))
    except Exception:
        pass
    return values


def setup_browser_routes() -> APIRouter:
    router = APIRouter(tags=["browser"])

    @router.get("/api/browser/state")
    async def get_state() -> Dict[str, Any]:
        """Get the current live browser state."""
        browser = get_browser_service()
        return browser.get_state()

    @router.get("/api/browser/engines")
    async def get_engines() -> Dict[str, Any]:
        """Return available search engines for the model and user."""
        return {
            "engines": list(SEARCH_ENGINES.values()),
            "keys": list(SEARCH_ENGINES.keys()),
            "default": "duckduckgo",
        }

    @router.get("/api/browser/view")
    async def get_rendered_view() -> Response:
        """Serve the live rendered HTML page for embedded iframe display."""
        browser = get_browser_service()
        content = browser.current_html or browser._generate_start_page_html()
        return HTMLResponse(
            content=content,
            headers={
                "Content-Type": "text/html; charset=utf-8",
                "X-Frame-Options": "ALLOWALL",
            },
        )

    @router.post("/api/browser/navigate")
    async def navigate(request: Request) -> Dict[str, Any]:
        """Navigate to a URL."""
        data = await _parse_body(request)
        url = str(data.get("url") or "").strip()
        engine = data.get("engine")
        browser = get_browser_service()
        snapshot = await browser.navigate(url, source="user", engine=engine)
        return {"status": "ok", "state": browser.get_state(), "snapshot": snapshot}

    @router.post("/api/browser/search")
    async def search(request: Request) -> Dict[str, Any]:
        """Perform a search with the chosen search engine."""
        data = await _parse_body(request)
        query = str(data.get("query") or "").strip()
        engine = data.get("engine")
        browser = get_browser_service()
        snapshot = await browser.search(query, engine=engine, source="user")
        return {"status": "ok", "state": browser.get_state(), "snapshot": snapshot}

    @router.post("/api/browser/click")
    async def click(request: Request) -> Dict[str, Any]:
        """Click an element or link."""
        data = await _parse_body(request)
        target = str(data.get("target") or "").strip()
        browser = get_browser_service()
        res = await browser.click(target, source="user")
        return {"status": "ok", "state": browser.get_state(), "result": res}

    @router.post("/api/browser/type")
    async def type_text(request: Request) -> Dict[str, Any]:
        """Type text into an input field."""
        data = await _parse_body(request)
        field = str(data.get("field") or "search").strip()
        text = str(data.get("text") or "").strip()
        submit = bool(data.get("submit", False))
        browser = get_browser_service()
        res = await browser.type_text(field, text, submit=submit, source="user")
        return {"status": "ok", "state": browser.get_state(), "result": res}

    @router.post("/api/browser/back")
    async def go_back() -> Dict[str, Any]:
        """Navigate back."""
        browser = get_browser_service()
        await browser.back()
        return {"status": "ok", "state": browser.get_state()}

    @router.post("/api/browser/forward")
    async def go_forward() -> Dict[str, Any]:
        """Navigate forward."""
        browser = get_browser_service()
        await browser.forward()
        return {"status": "ok", "state": browser.get_state()}

    @router.post("/api/browser/reload")
    async def reload_page() -> Dict[str, Any]:
        """Reload page."""
        browser = get_browser_service()
        await browser.reload()
        return {"status": "ok", "state": browser.get_state()}

    @router.get("/api/browser/events")
    async def browser_events(request: Request):
        """Live SSE stream of browser events for real-time GUI updates."""
        browser = get_browser_service()
        q = browser.subscribe()

        async def event_generator():
            try:
                # Send initial state
                initial = {
                    "type": "browser_state",
                    "state": browser.get_state(),
                    "timestamp": asyncio.get_event_loop().time(),
                }
                yield f"data: {json.dumps(initial)}\n\n"

                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        raw = await asyncio.wait_for(q.get(), timeout=25.0)
                        yield f"data: {raw}\n\n"
                    except asyncio.TimeoutError:
                        # Keep-alive comment
                        yield ": keepalive\n\n"
            finally:
                browser.unsubscribe(q)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router
