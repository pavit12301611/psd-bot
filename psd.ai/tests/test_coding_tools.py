"""Coding-helper agent tools: todoread, code_stats, regex_test.

Covers the read-only companions added alongside todowrite:

- todoread reads back the session todo list todowrite persists (and reports
  cleanly when none exists); lists are session-scoped.
- code_stats reports advisory line/symbol/import stats for a workspace file
  and stays workspace-confined.
- regex_test matches a pattern against inline text or a workspace file with
  spans, line/column, and groups; bad patterns and escapes fail closed.
- registry wiring: fence tags, name aliases, plan-mode allowlist, non-admin
  gating, and native function schemas.

End-to-end dispatch goes through execute_tool_block like the other tool
tests (see test_workspace_confine.py).
"""
import json
import os
import tempfile
from types import SimpleNamespace

import pytest


async def execute_tool_block(*args, **kwargs):
    # Import lazily: other test modules (e.g. test_fenced_inline_args) pop
    # and re-import the src.tool_* stack at collection time, so a module-top
    # import here can hold a stale module object whose workspace binding is
    # not the one dispatch reads. Resolving at call time always gets the
    # live module.
    from src.tool_execution import (
        NO_TOOL_SECURITY_CONTEXT,
        execute_tool_block as _execute_tool_block,
    )

    kwargs.setdefault("security_context", NO_TOOL_SECURITY_CONTEXT)
    return await _execute_tool_block(*args, **kwargs)


def _block(tool, content=""):
    return SimpleNamespace(tool_type=tool, content=content)


@pytest.fixture
def ws():
    d = tempfile.mkdtemp()
    return d


@pytest.fixture
def admin(monkeypatch):
    """Pass the public-tool gate so file tools dispatch in tests."""
    monkeypatch.setattr(
        "src.tool_execution.owner_is_admin_or_single_user", lambda owner: True
    )


# ── todoread ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_todoread_without_list_reports_cleanly(tmp_path, monkeypatch, admin):
    import src.agent_tools.coding_tools as coding_tools

    monkeypatch.setattr(coding_tools, "_TODO_DIR", str(tmp_path))
    _, r = await execute_tool_block(
        _block("todoread", ""),
        session_id="chat/fresh", owner="a", workspace=str(tmp_path),
    )
    assert r["exit_code"] == 0
    assert "No todo list" in r["output"]
    assert r["todos"] == []


@pytest.mark.asyncio
async def test_todoread_round_trip_after_todowrite(tmp_path, monkeypatch, admin):
    import src.agent_tools.coding_tools as coding_tools

    monkeypatch.setattr(coding_tools, "_TODO_DIR", str(tmp_path))
    payload = {
        "todos": [
            {"content": "Inspect code", "status": "completed", "priority": "high"},
            {"content": "Patch code", "status": "in_progress", "priority": "high"},
            {"content": "Run tests", "status": "pending", "priority": "medium"},
        ]
    }
    _, w = await execute_tool_block(
        _block("todowrite", json.dumps(payload)),
        session_id="chat/one", owner="a", workspace=str(tmp_path),
    )
    assert w["exit_code"] == 0

    _, r = await execute_tool_block(
        _block("todoread", ""),
        session_id="chat/one", owner="a", workspace=str(tmp_path),
    )
    assert r["exit_code"] == 0
    assert "[x] Inspect code (high)" in r["output"]
    assert "[>] Patch code (high)" in r["output"]
    assert "[ ] Run tests (medium)" in r["output"]
    assert "1/3 completed" in r["output"]
    assert "in progress: Patch code" in r["output"]
    assert [t["status"] for t in r["todos"]] == ["completed", "in_progress", "pending"]


@pytest.mark.asyncio
async def test_todoread_is_session_scoped(tmp_path, monkeypatch, admin):
    import src.agent_tools.coding_tools as coding_tools

    monkeypatch.setattr(coding_tools, "_TODO_DIR", str(tmp_path))
    payload = {"todos": [{"content": "Only in session A", "status": "pending"}]}
    _, w = await execute_tool_block(
        _block("todowrite", json.dumps(payload)),
        session_id="chat/A", owner="a", workspace=str(tmp_path),
    )
    assert w["exit_code"] == 0

    _, r = await execute_tool_block(
        _block("todoread", ""),
        session_id="chat/B", owner="a", workspace=str(tmp_path),
    )
    assert r["exit_code"] == 0
    assert "No todo list" in r["output"]


@pytest.mark.asyncio
async def test_todoread_rejects_non_object_json(admin):
    _, r = await execute_tool_block(_block("todoread", "[1,2]"), owner="a")
    assert r["exit_code"] == 1
    assert "JSON object required" in r["error"]


# ── code_stats ─────────────────────────────────────────────────────────

_PY_SAMPLE = '''"""Module docstring."""
import json
import os
from typing import Any

# A comment line
class Alpha:
    """Class docstring."""

    def method_one(self):
        return 1

    async def method_two(self):
        return 2


def helper():
    return 3
'''


@pytest.mark.asyncio
async def test_code_stats_python_file(ws, admin):
    with open(os.path.join(ws, "sample.py"), "w") as f:
        f.write(_PY_SAMPLE)
    _, r = await execute_tool_block(
        _block("code_stats", json.dumps({"path": "sample.py"})),
        owner="a", workspace=ws,
    )
    assert r["exit_code"] == 0, r
    stats = r["stats"]
    assert stats["language"] == "Python"
    assert stats["lines_total"] == _PY_SAMPLE.count("\n")
    assert stats["functions"] == 3
    assert stats["classes"] == 1
    assert "json" in stats["imports"] and "typing" in stats["imports"]
    assert stats["lines_blank"] >= 3
    assert stats["lines_comment"] >= 1  # the # comment line
    assert "3 functions" in r["output"]
    assert "1 classes" in r["output"]


@pytest.mark.asyncio
async def test_code_stats_requires_path(ws, admin):
    _, r = await execute_tool_block(_block("code_stats", ""), owner="a", workspace=ws)
    assert r["exit_code"] == 1
    assert "path required" in r["error"]


@pytest.mark.asyncio
async def test_code_stats_missing_file(ws, admin):
    _, r = await execute_tool_block(
        _block("code_stats", json.dumps({"path": "nope.py"})), owner="a", workspace=ws
    )
    assert r["exit_code"] == 1
    assert "not found" in r["error"]


@pytest.mark.asyncio
async def test_code_stats_confined_to_workspace(ws, admin):
    outside = tempfile.mkdtemp()
    outside_file = os.path.join(outside, "secret.py")
    with open(outside_file, "w") as f:
        f.write("x = 1\n")
    _, r = await execute_tool_block(
        _block("code_stats", json.dumps({"path": outside_file})),
        owner="a", workspace=ws,
    )
    assert r["exit_code"] == 1
    assert "code_stats:" in r["error"]


@pytest.mark.asyncio
async def test_code_stats_fence_path_format(ws, admin):
    with open(os.path.join(ws, "plain.py"), "w") as f:
        f.write("def a():\n    pass\n")
    _, r = await execute_tool_block(
        _block("code_stats", "plain.py"), owner="a", workspace=ws
    )
    assert r["exit_code"] == 0
    assert r["stats"]["functions"] == 1


# ── regex_test ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_regex_test_inline_text_with_groups(admin):
    args = {
        "pattern": r"(?P<user>\w+)@(?P<domain>\w+\.\w+)",
        "text": "mail bob@example.com and alice@test.org ok",
    }
    _, r = await execute_tool_block(
        _block("regex_test", json.dumps(args)), owner="a"
    )
    assert r["exit_code"] == 0, r
    assert r["count"] == 2
    m0 = r["matches"][0]
    assert m0["text"] == "bob@example.com"
    assert m0["line"] == 1 and m0["col"] == 6
    assert m0["named"]["user"] == "bob"
    assert m0["named"]["domain"] == "example.com"
    assert "2 match(es)" in r["output"]


@pytest.mark.asyncio
async def test_regex_test_line_and_column_tracking(admin):
    args = {"pattern": r"\d+", "text": "one\ntwo 42\nthree"}
    _, r = await execute_tool_block(_block("regex_test", json.dumps(args)), owner="a")
    assert r["exit_code"] == 0
    assert r["count"] == 1
    assert r["matches"][0]["line"] == 2
    assert r["matches"][0]["col"] == 5


@pytest.mark.asyncio
async def test_regex_test_bad_pattern_fails_closed(admin):
    args = {"pattern": r"(unclosed", "text": "abc"}
    _, r = await execute_tool_block(_block("regex_test", json.dumps(args)), owner="a")
    assert r["exit_code"] == 1
    assert "bad pattern" in r["error"]


@pytest.mark.asyncio
async def test_regex_test_requires_target(admin):
    _, r = await execute_tool_block(
        _block("regex_test", json.dumps({"pattern": "x"})), owner="a"
    )
    assert r["exit_code"] == 1
    assert "text" in r["error"] and "path" in r["error"]


@pytest.mark.asyncio
async def test_regex_test_rejects_text_and_path(admin):
    args = {"pattern": "x", "text": "x", "path": "f.txt"}
    _, r = await execute_tool_block(_block("regex_test", json.dumps(args)), owner="a")
    assert r["exit_code"] == 1


@pytest.mark.asyncio
async def test_regex_test_unknown_flag(admin):
    args = {"pattern": "x", "text": "x", "flags": "z"}
    _, r = await execute_tool_block(_block("regex_test", json.dumps(args)), owner="a")
    assert r["exit_code"] == 1
    assert "unknown flag" in r["error"]


@pytest.mark.asyncio
async def test_regex_test_flags_and_max_matches(admin):
    args = {
        "pattern": "^hello",
        "text": "hello\nHELLO\nsay hello",
        "flags": "im",
        "max_matches": 1,
    }
    _, r = await execute_tool_block(_block("regex_test", json.dumps(args)), owner="a")
    assert r["exit_code"] == 0
    assert r["count"] >= 2
    assert len(r["matches"]) == 1
    assert r["truncated"] is True
    assert "showing first 1" in r["output"]


@pytest.mark.asyncio
async def test_regex_test_file_mode_and_confinement(ws, admin):
    with open(os.path.join(ws, "data.txt"), "w") as f:
        f.write("foo 123\nbar 456\n")
    _, r = await execute_tool_block(
        _block("regex_test", json.dumps({"pattern": r"\d+", "path": "data.txt"})),
        owner="a", workspace=ws,
    )
    assert r["exit_code"] == 0
    assert r["count"] == 2
    assert "file data.txt" in r["output"]

    outside = tempfile.mkdtemp()
    secret = os.path.join(outside, "secret.txt")
    with open(secret, "w") as f:
        f.write("top secret\n")
    _, r = await execute_tool_block(
        _block("regex_test", json.dumps({"pattern": "secret", "path": secret})),
        owner="a", workspace=ws,
    )
    assert r["exit_code"] == 1


@pytest.mark.asyncio
async def test_regex_test_fence_fallback_format(admin):
    _, r = await execute_tool_block(
        _block("regex_test", r"\w+ world" + "\n" + "hello world, bye world"),
        owner="a",
    )
    assert r["exit_code"] == 0
    assert r["count"] == 2


@pytest.mark.asyncio
async def test_regex_test_no_matches(admin):
    args = {"pattern": "zebra", "text": "horse"}
    _, r = await execute_tool_block(_block("regex_test", json.dumps(args)), owner="a")
    assert r["exit_code"] == 0
    assert r["count"] == 0
    assert "No matches" in r["output"]


# ── registry wiring ────────────────────────────────────────────────────

def test_new_tools_are_fence_tags_and_handlers():
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS

    for name in ("todoread", "code_stats", "regex_test"):
        assert name in TOOL_TAGS, name
        assert name in TOOL_HANDLERS, name


def test_name_aliases_resolve():
    from src.tool_parsing import _TOOL_NAME_MAP

    assert _TOOL_NAME_MAP["todo_read"] == "todoread"
    assert _TOOL_NAME_MAP["file_stats"] == "code_stats"
    assert _TOOL_NAME_MAP["test_regex"] == "regex_test"
    assert _TOOL_NAME_MAP["regex"] == "regex_test"


def test_native_schemas_exist_for_new_tools():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert {"todoread", "code_stats", "regex_test"} <= names


def test_plan_mode_keeps_new_tools_readonly():
    from src.tool_security import (
        PLAN_MODE_READONLY_TOOLS,
        _PLAN_MODE_KNOWN_MUTATORS,
        plan_mode_disabled_tools,
    )

    for name in ("todoread", "code_stats", "regex_test"):
        assert name in PLAN_MODE_READONLY_TOOLS, name
        assert name not in _PLAN_MODE_KNOWN_MUTATORS, name
        assert name not in plan_mode_disabled_tools(), name


def test_non_admin_gating_matches_file_access():
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS

    # Workspace readers mirror read_file/grep gating; todoread only touches
    # session-scoped state (like todowrite) and stays available.
    assert "code_stats" in NON_ADMIN_BLOCKED_TOOLS
    assert "regex_test" in NON_ADMIN_BLOCKED_TOOLS
    assert "todoread" not in NON_ADMIN_BLOCKED_TOOLS
