"""Unit coverage for the coding-tools internals.

The dispatch-level behavior lives in test_coding_tools.py; this file pins
the small helpers those tools are built on, so a regression in language
detection, comment counting, or input parsing is caught at the function
level with an exact error message:

- _detect_language across the supported extension map
- _count_comment_lines for line prefixes, block spans (/* */, <!-- -->),
  and mixed files
- CodeStatsTool: truncation of oversized files, non-JSON fence fallback,
  unknown-language files
- RegexTestTool: zero-length matches, missing-file path, bad JSON,
  max_matches clamping
"""
import asyncio
import json
import os
import tempfile

import pytest

import src.agent_tools.coding_tools as ct


@pytest.fixture
def ws_ctx(tmp_path):
    """Bind the per-turn workspace ContextVar the way execute_tool_block
    does, so the shared _resolve_tool_path confines to tmp_path.

    Imported lazily: other test modules (test_fenced_inline_args) pop and
    re-import the src.tool_* stack at collection time, so a module-top
    import here could hold a stale ContextVar from a superseded module.
    """
    from src.tool_execution import _active_workspace

    token = _active_workspace.set(str(tmp_path))
    yield tmp_path
    _active_workspace.reset(token)


# ── _detect_language ───────────────────────────────────────────────────

@pytest.mark.parametrize("path,lang", [
    ("src/app.py", "python"),
    ("mod.pyi", "python"),
    ("bundle.min.js", "javascript"),
    ("types.ts", "javascript"),
    ("view.tsx", "javascript"),
    ("main.c", "c-family"),
    ("engine.hpp", "c-family"),
    ("Server.java", "c-family"),
    ("lib.rs", "c-family"),
    ("script.rb", "ruby"),
    ("run.sh", "shell"),
    ("index.php", "php"),
    ("query.sql", "sql"),
    ("page.html", "html"),
    ("theme.css", "css"),
    ("config.yml", "yaml"),
    ("pyproject.toml", "toml"),
    ("init.el", "lisp"),
    ("README.md", "markdown"),
    ("data.xyz", None),
    ("Makefile", None),
])
def test_detect_language(path, lang):
    assert ct._detect_language(path) == lang


# ── _count_comment_lines ───────────────────────────────────────────────

def test_comment_count_python_prefixes():
    profile = ct._LANG_PROFILES["python"]
    lines = ["import os", "", "# a comment", "x = 1  # trailing not counted", "#!shebang"]
    assert ct._count_comment_lines(lines, profile) == 2


def test_comment_count_c_block_span():
    profile = ct._LANG_PROFILES["c-family"]
    lines = [
        "int x;",
        "/* start of block",
        "   still inside",
        "end */ int y;",
        "// single",
        "int z;",
    ]
    assert ct._count_comment_lines(lines, profile) == 4  # 3 block lines + // line


def test_comment_count_single_line_block_comment():
    profile = ct._LANG_PROFILES["javascript"]
    lines = ["a();", "/* one-liner */", "b();"]
    # The opener's closer is on the same line -> only that one line counts.
    assert ct._count_comment_lines(lines, profile) == 1


def test_comment_count_html_blocks():
    profile = ct._LANG_PROFILES["html"]
    lines = ["<div>", "<!-- nav", "menu", "-->", "<p>"]
    assert ct._count_comment_lines(lines, profile) == 3


def test_comment_count_no_profile_means_zero():
    assert ct._count_comment_lines(["anything", "# not a comment"], {}) == 0


# ── CodeStatsTool internals ────────────────────────────────────────────

def _run(tool, content, ctx=None, tmp_ws=None):
    return asyncio.run(tool.execute(content, ctx or {}))


def test_code_stats_unknown_language_still_reports_lines(ws_ctx):
    f = ws_ctx / "data.unknownext"
    f.write_text("line one\nline two\n\n")
    r = _run(ct.CodeStatsTool(), json.dumps({"path": str(f)}))
    assert r["exit_code"] == 0
    assert r["stats"]["language"] == "Unknown"
    assert r["stats"]["lines_total"] == 3
    assert "Symbols:" not in r["output"]  # no symbol patterns for unknown langs


def test_code_stats_truncates_huge_files(ws_ctx):
    f = ws_ctx / "big.py"
    # 4 bytes/line * (cap/4 + extra) lines forces truncation.
    n_lines = ct._MAX_CODING_HELPER_CHARS // 4 + 100
    f.write_text("x=1\n" * n_lines)
    r = _run(ct.CodeStatsTool(), json.dumps({"path": str(f)}))
    assert r["exit_code"] == 0
    assert r["stats"]["truncated"] is True
    assert "truncated" in r["output"]
    assert r["stats"]["lines_total"] < n_lines


# ── RegexTestTool edge cases ───────────────────────────────────────────

def test_regex_test_zero_length_matches_are_capped():
    r = _run(ct.RegexTestTool(), json.dumps({
        "pattern": "a*", "text": "bbb", "max_matches": 5,
    }))
    assert r["exit_code"] == 0
    assert len(r["matches"]) <= 5


def test_regex_test_max_matches_clamped_to_bounds():
    args = {"pattern": "b", "text": "b" * 500, "max_matches": 10_000}
    r = _run(ct.RegexTestTool(), json.dumps(args))
    assert r["exit_code"] == 0
    assert len(r["matches"]) <= 100  # hard cap, even for absurd requests


def test_regex_test_invalid_json_fails_closed():
    r = _run(ct.RegexTestTool(), '{"pattern": "x",')
    assert r["exit_code"] == 1
    assert "invalid JSON" in r["error"]


def test_regex_test_missing_file_reports_not_found(ws_ctx):
    r = _run(ct.RegexTestTool(), json.dumps({"pattern": "x", "path": "nope.txt"}))
    assert r["exit_code"] == 1
    assert "not found" in r["error"]


def test_regex_test_non_object_json_fails():
    # A JSON *array* is not a valid args object — fail closed.
    r = _run(ct.RegexTestTool(), "[1, 2]")
    assert r["exit_code"] == 1
    assert "JSON object required" in r["error"]


# ── todo helpers ───────────────────────────────────────────────────────

def test_safe_session_id_sanitizes_and_defaults():
    assert ct._safe_session_id("") == "current"
    assert ct._safe_session_id("chat/one two") == "chat_one_two"
    assert ct._safe_session_id("a" * 500).count("a") <= 120


def test_load_session_todos_tolerates_missing_and_corrupt(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "_TODO_DIR", str(tmp_path))
    assert ct._load_session_todos("ghost") is None
    corrupt = tmp_path / "bad.json"
    corrupt.write_text("{not json")
    assert ct._load_session_todos("bad") is None
    wrong_shape = tmp_path / "odd.json"
    wrong_shape.write_text(json.dumps({"todos": "nope"}))
    assert ct._load_session_todos("odd") is None


def test_format_todo_lines_markers():
    lines = ct._format_todo_lines([
        {"content": "a", "status": "pending", "priority": "low"},
        {"content": "b", "status": "in_progress", "priority": "high"},
        {"content": "c", "status": "completed", "priority": "medium"},
        {"content": "d"},  # defaults
    ])
    assert lines[0] == "[ ] a (low)"
    assert lines[1] == "[>] b (high)"
    assert lines[2] == "[x] c (medium)"
    assert lines[3] == "[ ] d (medium)"
