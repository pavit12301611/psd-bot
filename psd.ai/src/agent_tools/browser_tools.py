"""Browser agent tools for live embedded browser access.

Gives the AI model full browser access:
- browser_navigate: Navigate to any URL
- browser_search: Search using the model's preferred search engine (DuckDuckGo, Google, Bing, Brave, etc.)
- browser_click: Click links/buttons on the page
- browser_type: Type into input fields
- browser_snapshot: Inspect the current page DOM / text
- browser_back / browser_forward: History navigation
"""

import json
import logging
from typing import Any, Dict

from services.browser import get_browser_service
from src.constants import MAX_OUTPUT_CHARS

logger = logging.getLogger(__name__)


class BrowserNavigateTool:
    """Navigate the embedded browser to a specific URL."""

    async def execute(self, content: str, ctx: dict) -> dict:
        progress_cb = ctx.get("progress_cb") if isinstance(ctx, dict) else None
        raw = content.strip()
        url = raw
        engine = None

        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    url = str(parsed.get("url") or parsed.get("target") or "").strip()
                    engine = parsed.get("engine")
            except json.JSONDecodeError:
                pass

        if not url:
            return {"error": "browser_navigate: specify a URL to visit", "exit_code": 1}

        if progress_cb:
            await progress_cb({
                "elapsed_s": 0,
                "tail": f"Navigating embedded browser to: {url[:100]}",
            })

        browser = get_browser_service()
        snapshot = await browser.navigate(url, source="model", engine=engine)

        summary = (
            f"Navigated embedded browser to: {snapshot.get('url')}\n"
            f"Title: {snapshot.get('title')}\n"
            f"Status: {snapshot.get('status')}\n\n"
            f"--- Page Content ---\n"
            f"{snapshot.get('text', '')[:MAX_OUTPUT_CHARS]}"
        )
        return {"output": summary, "exit_code": 0, "browser_url": snapshot.get("url")}


class BrowserSearchTool:
    """Search the web with the model's choice of search engine."""

    async def execute(self, content: str, ctx: dict) -> dict:
        progress_cb = ctx.get("progress_cb") if isinstance(ctx, dict) else None
        raw = content.strip()
        query = raw
        engine = None

        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    query = str(parsed.get("query") or parsed.get("q") or "").strip()
                    engine = parsed.get("engine")
            except json.JSONDecodeError:
                pass

        if not query:
            return {"error": "browser_search: specify a query to search", "exit_code": 1}

        browser = get_browser_service()
        chosen_engine = engine or browser.current_engine or "duckduckgo"

        if progress_cb:
            await progress_cb({
                "elapsed_s": 0,
                "tail": f"Searching on {chosen_engine} for: {query[:100]}",
            })

        snapshot = await browser.search(query, engine=engine, source="model")
        results = snapshot.get("results") or []

        res_lines = [
            f"Search completed on {snapshot.get('engine', chosen_engine).title()} (Model's Choice).",
            f"Query: {query}",
            f"Embedded Browser URL: {snapshot.get('url')}\n",
            "Top Results:",
        ]
        for idx, r in enumerate(results[:8], 1):
            res_lines.append(f"{idx}. {r.get('title')}")
            res_lines.append(f"   URL: {r.get('url')}")
            res_lines.append(f"   Snippet: {r.get('snippet')}\n")

        output = "\n".join(res_lines)
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + "\n\n[...truncated]"

        return {
            "output": output,
            "exit_code": 0,
            "browser_url": snapshot.get("url"),
            "engine": snapshot.get("engine"),
        }


class BrowserClickTool:
    """Click an element, link, or button in the embedded browser."""

    async def execute(self, content: str, ctx: dict) -> dict:
        progress_cb = ctx.get("progress_cb") if isinstance(ctx, dict) else None
        raw = content.strip()
        target = raw

        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    target = str(parsed.get("target") or parsed.get("selector") or parsed.get("link") or "").strip()
            except json.JSONDecodeError:
                pass

        if not target:
            return {"error": "browser_click: specify an element, link text, or selector to click", "exit_code": 1}

        if progress_cb:
            await progress_cb({
                "elapsed_s": 0,
                "tail": f"Clicking '{target}' in embedded browser",
            })

        browser = get_browser_service()
        res = await browser.click(target, source="model")

        return {
            "output": f"Clicked '{target}'. Current page URL: {res.get('url', browser.url)}\nTitle: {res.get('title', browser.title)}",
            "exit_code": 0,
            "browser_url": res.get("url", browser.url),
        }


class BrowserTypeTool:
    """Type into a field or input in the embedded browser."""

    async def execute(self, content: str, ctx: dict) -> dict:
        raw = content.strip()
        field = "search"
        text = ""
        submit = False

        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    field = str(parsed.get("field") or parsed.get("target") or "input").strip()
                    text = str(parsed.get("text") or parsed.get("value") or "").strip()
                    submit = bool(parsed.get("submit", False))
            except json.JSONDecodeError:
                pass
        else:
            text = raw

        browser = get_browser_service()
        res = await browser.type_text(field, text, submit=submit, source="model")

        return {
            "output": f"Typed '{text}' into {field}. Result URL: {res.get('url', browser.url)}",
            "exit_code": 0,
            "browser_url": res.get("url", browser.url),
        }


class BrowserSnapshotTool:
    """Capture a DOM / text snapshot of the current embedded browser page."""

    async def execute(self, content: str, ctx: dict) -> dict:
        browser = get_browser_service()
        snapshot = browser.get_snapshot()

        output = (
            f"Embedded Browser Page Snapshot:\n"
            f"URL: {snapshot.get('url')}\n"
            f"Title: {snapshot.get('title')}\n"
            f"Engine: {snapshot.get('engine')}\n"
            f"Status: {snapshot.get('status')}\n\n"
            f"Content:\n{snapshot.get('text', '')[:MAX_OUTPUT_CHARS]}\n\n"
            f"Clickable Links:\n"
        )
        for link in snapshot.get("links", [])[:15]:
            output += f"- [{link.get('text')}]({link.get('href')})\n"

        return {"output": output, "exit_code": 0, "browser_url": snapshot.get("url")}


class BrowserBackTool:
    """Navigate back in the embedded browser."""

    async def execute(self, content: str, ctx: dict) -> dict:
        browser = get_browser_service()
        snapshot = await browser.back()
        return {
            "output": f"Navigated back to: {snapshot.get('url')} ({snapshot.get('title')})",
            "exit_code": 0,
            "browser_url": snapshot.get("url"),
        }


class BrowserForwardTool:
    """Navigate forward in the embedded browser."""

    async def execute(self, content: str, ctx: dict) -> dict:
        browser = get_browser_service()
        snapshot = await browser.forward()
        return {
            "output": f"Navigated forward to: {snapshot.get('url')} ({snapshot.get('title')})",
            "exit_code": 0,
            "browser_url": snapshot.get("url"),
        }
