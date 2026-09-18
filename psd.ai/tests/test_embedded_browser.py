"""Unit tests for Embedded Browser Service and Agent Tools."""

import asyncio
import importlib.util
import os
import sys

# Add psd.ai root to sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE_DIR)

from services.browser import get_browser_service, SEARCH_ENGINES

# Load browser_tools directly to test in minimal environment without requiring full backend deps
spec = importlib.util.spec_from_file_location(
    "browser_tools", os.path.join(BASE_DIR, "src", "agent_tools", "browser_tools.py")
)
browser_tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(browser_tools)

BrowserNavigateTool = browser_tools.BrowserNavigateTool
BrowserSearchTool = browser_tools.BrowserSearchTool
BrowserClickTool = browser_tools.BrowserClickTool
BrowserTypeTool = browser_tools.BrowserTypeTool
BrowserSnapshotTool = browser_tools.BrowserSnapshotTool
BrowserBackTool = browser_tools.BrowserBackTool
BrowserForwardTool = browser_tools.BrowserForwardTool


def test_browser_engines():
    """Verify search engines list and model's choice support."""
    assert "duckduckgo" in SEARCH_ENGINES
    assert "google" in SEARCH_ENGINES
    assert "bing" in SEARCH_ENGINES
    assert "brave" in SEARCH_ENGINES
    assert "ecosia" in SEARCH_ENGINES
    print("PASS: test_browser_engines")


async def test_browser_service():
    """Verify navigation, search, and action logging in browser service."""
    browser = get_browser_service()

    # 1. Start page / initial state
    st = browser.get_state()
    assert st["url"] == "about:home"
    assert "Embedded Browser" in st["title"]

    # 2. Search on DuckDuckGo (Model's choice)
    snap = await browser.search("python programming", engine="duckduckgo", source="model")
    assert "duckduckgo" in snap["url"] or "search" in snap["url"]
    assert snap["engine"] == "duckduckgo"
    assert len(snap.get("results", [])) > 0

    # 3. Search on Google (Model's choice)
    snap_google = await browser.search("latest ai models 2026", engine="google", source="model")
    assert snap_google["engine"] == "google"
    assert "google" in snap_google["url"]

    # 4. Search on Brave (Model's choice)
    snap_brave = await browser.search("rust web development", engine="brave", source="model")
    assert snap_brave["engine"] == "brave"

    # 5. Click interaction
    click_res = await browser.click("python", source="model")
    assert click_res is not None

    # 6. Type interaction
    type_res = await browser.type_text("search", "fastapi async", submit=True, source="model")
    assert type_res is not None

    # 7. Action logs recorded
    state = browser.get_state()
    assert len(state["action_logs"]) > 0
    actions = [log["action"] for log in state["action_logs"]]
    assert "search" in actions

    # 8. Rendered HTML
    html_content = browser.current_html
    assert "<!DOCTYPE html>" in html_content
    assert "Model's Choice" in html_content or "Embedded" in html_content

    print("PASS: test_browser_service")


async def test_browser_agent_tools():
    """Verify agent tools execution and output formatting."""
    ctx = {}

    # Test browser_search
    search_tool = BrowserSearchTool()
    res = await search_tool.execute('{"query": "quantum computing", "engine": "brave"}', ctx)
    assert res["exit_code"] == 0
    assert "Brave" in res["output"] or "quantum computing" in res["output"]
    assert res["engine"] == "brave"

    # Test browser_navigate
    nav_tool = BrowserNavigateTool()
    res_nav = await nav_tool.execute('{"url": "about:home"}', ctx)
    assert res_nav["exit_code"] == 0
    assert "about:home" in res_nav["output"]

    # Test browser_snapshot
    snap_tool = BrowserSnapshotTool()
    res_snap = await snap_tool.execute("", ctx)
    assert res_snap["exit_code"] == 0
    assert "Embedded Browser" in res_snap["output"]

    # Test browser_click
    click_tool = BrowserClickTool()
    res_click = await click_tool.execute("DuckDuckGo", ctx)
    assert res_click["exit_code"] == 0

    # Test browser_type
    type_tool = BrowserTypeTool()
    res_type = await type_tool.execute('{"field": "query", "text": "agent mode", "submit": true}', ctx)
    assert res_type["exit_code"] == 0

    print("PASS: test_browser_agent_tools")


async def main():
    test_browser_engines()
    await test_browser_service()
    await test_browser_agent_tools()
    print("ALL TESTS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    asyncio.run(main())
