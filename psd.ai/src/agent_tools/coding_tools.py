import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional

from src.constants import DATA_DIR


_TODO_DIR = os.path.join(DATA_DIR, "agent_todos")

# Upper bound for file-backed coding helpers (code_stats / regex_test). The
# tools are advisory stats/inspection, not full-file readers (that's
# read_file's job), so we cap well above MAX_READ_CHARS but still bounded.
_MAX_CODING_HELPER_CHARS = 200_000

# regex_test gives up on a single match pass after this many seconds — a
# pathological pattern + text combination can otherwise wedge the agent loop
# in catastrophic backtracking.
_REGEX_MATCH_TIMEOUT_SECONDS = 15.0


def _safe_session_id(value: str) -> str:
    value = value or "current"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:120] or "current"


def _todo_path(session_id: str) -> str:
    return os.path.join(_TODO_DIR, f"{session_id}.json")


def _load_session_todos(session_id: str) -> Optional[List[Dict[str, Any]]]:
    """Return the persisted todo list for a session, or None when absent."""
    try:
        with open(_todo_path(session_id), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    todos = data.get("todos") if isinstance(data, dict) else None
    return todos if isinstance(todos, list) else None


def _format_todo_lines(todos: List[Dict[str, Any]]) -> List[str]:
    markers = {"pending": " ", "in_progress": ">", "completed": "x"}
    lines = []
    for item in todos:
        marker = markers.get(str(item.get("status") or "pending"), " ")
        lines.append(f"[{marker}] {item.get('content', '')} ({item.get('priority', 'medium')})")
    return lines


class TodoWriteTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = json.loads(content) if (content or "").strip().startswith("{") else {"todos": []}
        except (json.JSONDecodeError, TypeError):
            return {"error": "todowrite: JSON object required", "exit_code": 1}
        todos = args.get("todos")
        if not isinstance(todos, list):
            return {"error": "todowrite: todos must be a list", "exit_code": 1}

        normalized: List[Dict[str, Any]] = []
        allowed_statuses = {"pending", "in_progress", "completed"}
        allowed_priorities = {"low", "medium", "high"}
        active_count = 0
        for item in todos:
            if not isinstance(item, dict):
                return {"error": "todowrite: each todo must be an object", "exit_code": 1}
            content_text = str(item.get("content") or item.get("text") or "").strip()
            if not content_text:
                return {"error": "todowrite: todo content required", "exit_code": 1}
            status = str(item.get("status") or "pending").strip()
            if status not in allowed_statuses:
                return {"error": f"todowrite: invalid status {status!r}", "exit_code": 1}
            if status == "in_progress":
                active_count += 1
            priority = str(item.get("priority") or "medium").strip()
            if priority not in allowed_priorities:
                priority = "medium"
            normalized.append({
                "content": content_text,
                "status": status,
                "priority": priority,
            })
        if active_count > 1:
            return {"error": "todowrite: only one todo can be in_progress", "exit_code": 1}

        session_id = _safe_session_id(str(ctx.get("session_id") or args.get("session_id") or "current"))
        os.makedirs(_TODO_DIR, exist_ok=True)
        path = _todo_path(session_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"todos": normalized}, f, ensure_ascii=False, indent=2)

        lines = _format_todo_lines(normalized)
        return {
            "output": "Updated todo list:\n" + ("\n".join(lines) if lines else "(empty)"),
            "exit_code": 0,
            "todos": normalized,
        }


class TodoReadTool:
    """Read the persisted todo list for the current coding session.

    Read-only counterpart to `todowrite` — lets the agent re-check what it
    planned after context compaction or a long stretch of tool rounds, without
    rewriting the list.
    """

    async def execute(self, content: str, ctx: dict) -> dict:
        args: Dict[str, Any] = {}
        stripped = (content or "").strip()
        if stripped.startswith(("{", "[")):
            try:
                args = json.loads(stripped)
            except (json.JSONDecodeError, TypeError):
                return {"error": "todoread: JSON object required (or no arguments)", "exit_code": 1}
            if not isinstance(args, dict):
                return {"error": "todoread: JSON object required (or no arguments)", "exit_code": 1}

        session_id = _safe_session_id(str(ctx.get("session_id") or args.get("session_id") or "current"))
        todos = _load_session_todos(session_id)
        if todos is None:
            return {
                "output": (
                    "No todo list exists for this session yet. "
                    "Call `todowrite` to create one when the work has several steps."
                ),
                "exit_code": 0,
                "todos": [],
            }

        lines = _format_todo_lines(todos)
        done = sum(1 for t in todos if t.get("status") == "completed")
        active = next((t.get("content") for t in todos if t.get("status") == "in_progress"), None)
        header = f"Todo list ({done}/{len(todos)} completed" + (f", in progress: {active}" if active else "") + "):"
        return {
            "output": header + "\n" + ("\n".join(lines) if lines else "(empty)"),
            "exit_code": 0,
            "todos": todos,
        }


# ── code_stats ─────────────────────────────────────────────────────────
#
# Language profiles for the advisory stats tool. Deliberately conservative:
# only claim symbol counts where a regex can be trusted; otherwise omit the
# line from the report rather than guess.

_LANG_PROFILES: Dict[str, Dict[str, Any]] = {
    "python": {
        "exts": {".py", ".pyi"},
        "line_comments": ("#",),
        "function_re": re.compile(r"^\s*(?:async\s+)?def\s+[A-Za-z_]\w*", re.M),
        "class_re": re.compile(r"^\s*class\s+[A-Za-z_]\w*", re.M),
        "import_res": (
            re.compile(r"^\s*import\s+([\w.]+)", re.M),
            re.compile(r"^\s*from\s+([\w.]+)\s+import\b", re.M),
        ),
    },
    "javascript": {
        "exts": {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"},
        "line_comments": ("//",),
        "block_comments": (("/*", "*/"),),
        "function_re": re.compile(r"\bfunction\s+[A-Za-z_$][\w$]*|\b(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>", re.M),
        "class_re": re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+[A-Za-z_$][\w$]*", re.M),
        "import_res": (
            re.compile(r"^\s*import\s+[^;]*?\bfrom\s+['\"]([^'\"]+)['\"]", re.M),
            re.compile(r"\brequire\(\s*['\"]([^'\"]+)['\"]\s*\)"),
        ),
    },
    "c-family": {
        "exts": {".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".java", ".cs", ".go", ".rs", ".kt", ".swift", ".scala"},
        "line_comments": ("//",),
        "block_comments": (("/*", "*/"),),
    },
    "ruby": {
        "exts": {".rb"},
        "line_comments": ("#",),
        "function_re": re.compile(r"^\s*def\s+(?:self\.)?[A-Za-z_]\w*", re.M),
        "class_re": re.compile(r"^\s*(?:class|module)\s+[A-Za-z_]\w*", re.M),
    },
    "shell": {
        "exts": {".sh", ".bash", ".zsh"},
        "line_comments": ("#",),
        "function_re": re.compile(r"^\s*(?:function\s+)?[A-Za-z_]\w*\s*\(\s*\)", re.M),
    },
    "php": {
        "exts": {".php"},
        "line_comments": ("//", "#"),
        "block_comments": (("/*", "*/"),),
        "function_re": re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|static\s+)*function\s+[A-Za-z_]\w*", re.M),
        "class_re": re.compile(r"^\s*(?:abstract\s+|final\s+)?class\s+[A-Za-z_]\w*", re.M),
    },
    "sql": {"exts": {".sql"}, "line_comments": ("--",), "block_comments": (("/*", "*/"),)},
    "html": {"exts": {".html", ".htm", ".xml", ".svg"}, "block_comments": (("<!--", "-->"),)},
    "css": {"exts": {".css", ".scss", ".less"}, "block_comments": (("/*", "*/"),)},
    "yaml": {"exts": {".yml", ".yaml"}, "line_comments": ("#",)},
    "toml": {"exts": {".toml"}, "line_comments": ("#",)},
    "lisp": {"exts": {".lisp", ".el", ".clj"}, "line_comments": (";",)},
    "markdown": {"exts": {".md", ".markdown", ".rst", ".txt"}, "line_comments": ()},
}

_LANG_DISPLAY = {
    "python": "Python", "javascript": "JavaScript/TypeScript", "c-family": "C-family",
    "ruby": "Ruby", "shell": "Shell", "php": "PHP", "sql": "SQL", "html": "HTML/XML",
    "css": "CSS", "yaml": "YAML", "toml": "TOML", "lisp": "Lisp", "markdown": "Text/Markdown",
}


def _detect_language(path: str) -> Optional[str]:
    ext = os.path.splitext(path)[1].lower()
    for lang, profile in _LANG_PROFILES.items():
        if ext in profile["exts"]:
            return lang
    return None


def _count_comment_lines(lines: List[str], profile: Dict[str, Any]) -> int:
    """Approximate comment-line count (single-line prefixes + block spans)."""
    prefixes = profile.get("line_comments") or ()
    blocks = profile.get("block_comments") or ()
    if not prefixes and not blocks:
        return 0
    openers = [b[0] for b in blocks]
    closers = {b[0]: b[1] for b in blocks}
    count = 0
    in_block_closer: Optional[str] = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if in_block_closer:
            count += 1
            if in_block_closer in line:
                in_block_closer = None
            continue
        if any(line.startswith(p) for p in prefixes):
            count += 1
            continue
        opener = next((o for o in openers if line.startswith(o)), None)
        if opener:
            count += 1
            if closers[opener] not in line[len(opener):]:
                in_block_closer = closers[opener]
    return count


class CodeStatsTool:
    """Advisory statistics for a source file: lines, symbols, imports.

    Read-only and workspace-confined (paths resolve through the shared
    `_resolve_tool_path`). Useful mid-task to size a file up before reading
    or editing it, without spending the full read_file budget.
    """

    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _resolve_tool_path

        raw_path = (content or "").split("\n", 1)[0].strip()
        stripped = (content or "").strip()
        if stripped.startswith("{"):
            try:
                args = json.loads(stripped)
                raw_path = str(args.get("path") or raw_path).strip()
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
        if not raw_path:
            return {"error": "code_stats: path required", "exit_code": 1}

        try:
            path = _resolve_tool_path(raw_path)
        except ValueError as e:
            return {"error": f"code_stats: {e}", "exit_code": 1}

        try:
            def _read():
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    return f.read(_MAX_CODING_HELPER_CHARS + 1)
            data = await asyncio.to_thread(_read)
        except FileNotFoundError:
            return {"error": f"code_stats: {raw_path}: not found", "exit_code": 1}
        except IsADirectoryError:
            return {"error": f"code_stats: {raw_path}: is a directory", "exit_code": 1}
        except OSError as e:
            return {"error": f"code_stats: {raw_path}: {e}", "exit_code": 1}

        truncated = len(data) > _MAX_CODING_HELPER_CHARS
        if truncated:
            data = data[:_MAX_CODING_HELPER_CHARS]

        lines = data.split("\n")
        if lines and not lines[-1]:
            lines.pop()  # trailing newline artifact
        total = len(lines)
        blank = sum(1 for l in lines if not l.strip())
        lang = _detect_language(path)
        profile = _LANG_PROFILES.get(lang, {})
        comments = _count_comment_lines(lines, profile)
        code = max(total - blank - comments, 0)

        stats: Dict[str, Any] = {
            "path": raw_path,
            "language": _LANG_DISPLAY.get(lang or "", "Unknown"),
            "lines_total": total,
            "lines_code": code,
            "lines_blank": blank,
            "lines_comment": comments,
            "truncated": truncated,
        }

        report = [
            f"File: {raw_path}",
            f"Language: {stats['language']}",
            f"Lines: {total} total ({code} code, {blank} blank, {comments} comment)",
        ]

        function_re = profile.get("function_re")
        class_re = profile.get("class_re")
        symbols = []
        if function_re:
            n_funcs = len(function_re.findall(data))
            stats["functions"] = n_funcs
            symbols.append(f"{n_funcs} functions")
        if class_re:
            n_classes = len(class_re.findall(data))
            stats["classes"] = n_classes
            symbols.append(f"{n_classes} classes")
        if symbols:
            report.append("Symbols: " + ", ".join(symbols))

        import_res = profile.get("import_res")
        if import_res:
            modules: List[str] = []
            for rx in import_res:
                for m in rx.finditer(data):
                    name = m.group(1)
                    if name and name not in modules:
                        modules.append(name)
            stats["imports"] = modules
            if modules:
                shown = ", ".join(modules[:20]) + (f" (+{len(modules) - 20} more)" if len(modules) > 20 else "")
                report.append(f"Imports ({len(modules)}): {shown}")

        if truncated:
            report.append(f"(file truncated after {_MAX_CODING_HELPER_CHARS} chars — counts cover the head of the file)")

        return {"output": "\n".join(report), "exit_code": 0, "stats": stats}


# ── regex_test ─────────────────────────────────────────────────────────

_REGEX_FLAG_MAP = {
    "i": re.IGNORECASE,
    "m": re.MULTILINE,
    "s": re.DOTALL,
    "x": re.VERBOSE,
}


def _line_col(line_starts: List[int], pos: int):
    import bisect
    idx = bisect.bisect_right(line_starts, pos) - 1
    return idx + 1, pos - line_starts[idx] + 1


def _display_text(s: str, limit: int = 80) -> str:
    s = s if len(s) <= limit else s[:limit] + "…"
    if any(ch in s for ch in "\n\r\t"):
        return repr(s)
    return f'"{s}"'


class RegexTestTool:
    """Test a regex against inline text or a workspace file.

    Read-only; file mode is workspace-confined via `_resolve_tool_path`.
    Returns matches with spans, line/column, and captured groups — enough to
    iterate on a pattern without burning bash/python rounds.
    """

    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _resolve_tool_path

        stripped = (content or "").strip()
        pattern: Optional[str] = None
        text: Optional[str] = None
        path: Optional[str] = None
        flags_str = ""
        max_matches = 20

        if stripped.startswith(("{", "[")):
            # Structured args path — anything that isn't a JSON object is a
            # malformed call (e.g. a bare array); fail closed instead of
            # guessing that "[1, 2]" is a pattern.
            try:
                args = json.loads(stripped)
            except (json.JSONDecodeError, TypeError):
                return {"error": "regex_test: invalid JSON arguments", "exit_code": 1}
            if not isinstance(args, dict):
                return {"error": "regex_test: JSON object required", "exit_code": 1}
            pattern = args.get("pattern")
            text = args.get("text")
            path = args.get("path")
            flags_str = str(args.get("flags") or "")
            try:
                max_matches = int(args.get("max_matches") or 20)
            except (TypeError, ValueError):
                max_matches = 20
        elif stripped:
            # Fence fallback: first line is the pattern, the rest is text.
            first, _, rest = stripped.partition("\n")
            pattern, text = first.strip(), rest
        if not pattern:
            return {"error": "regex_test: pattern required", "exit_code": 1}
        if text is None and path is None:
            return {"error": "regex_test: provide `text` or `path` to match against", "exit_code": 1}
        if text is not None and path is not None:
            return {"error": "regex_test: use either `text` or `path`, not both", "exit_code": 1}

        max_matches = max(1, min(max_matches, 100))
        flags = 0
        for ch in flags_str.lower():
            if ch not in _REGEX_FLAG_MAP:
                return {"error": f"regex_test: unknown flag {ch!r} (allowed: i, m, s, x)", "exit_code": 1}
            flags |= _REGEX_FLAG_MAP[ch]

        try:
            rx = re.compile(pattern, flags)
        except re.error as e:
            return {"error": f"regex_test: bad pattern: {e}", "exit_code": 1}

        source = "text"
        if path is not None:
            source = f"file {path}"
            try:
                resolved = _resolve_tool_path(str(path))
            except ValueError as e:
                return {"error": f"regex_test: {e}", "exit_code": 1}
            try:
                def _read():
                    with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                        return f.read(_MAX_CODING_HELPER_CHARS)
                text = await asyncio.to_thread(_read)
            except FileNotFoundError:
                return {"error": f"regex_test: {path}: not found", "exit_code": 1}
            except IsADirectoryError:
                return {"error": f"regex_test: {path}: is a directory", "exit_code": 1}
            except OSError as e:
                return {"error": f"regex_test: {path}: {e}", "exit_code": 1}

        haystack = text or ""

        line_starts = [0]
        for i, ch in enumerate(haystack):
            if ch == "\n":
                line_starts.append(i + 1)

        def _match_pass():
            found: List[Dict[str, Any]] = []
            count = 0
            for m in rx.finditer(haystack):
                count += 1
                if len(found) < max_matches:
                    line, col = _line_col(line_starts, m.start())
                    entry: Dict[str, Any] = {
                        "start": m.start(),
                        "end": m.end(),
                        "line": line,
                        "col": col,
                        "text": m.group(0),
                    }
                    if m.groups():
                        entry["groups"] = list(m.groups())[:5]
                    if m.groupdict():
                        entry["named"] = {k: v for k, v in list(m.groupdict().items())[:5]}
                    found.append(entry)
                if count >= max_matches * 10:
                    # Don't enumerate pathological millions of zero-length hits;
                    # we only ever display max_matches of them anyway.
                    break
            return found, count

        try:
            matches, count = await asyncio.wait_for(
                asyncio.to_thread(_match_pass), timeout=_REGEX_MATCH_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            return {
                "error": (
                    f"regex_test: timed out after {int(_REGEX_MATCH_TIMEOUT_SECONDS)}s — "
                    "the pattern likely backtracks catastrophically on this input; "
                    "simplify it or anchor it more tightly."
                ),
                "exit_code": 1,
            }

        if not matches:
            return {
                "output": f"No matches for /{pattern}/ in {source}.",
                "exit_code": 0,
                "count": 0,
                "matches": [],
                "truncated": False,
            }

        out = [f"{count if count <= max_matches else f'{max_matches}+'} match(es) for /{pattern}/ in {source}:"]
        for i, m in enumerate(matches, 1):
            piece = f"#{i} line {m['line']}:{m['col']} (chars {m['start']}-{m['end']}) {_display_text(m['text'])}"
            if m.get("named"):
                piece += " named=" + json.dumps(m["named"], ensure_ascii=False)
            elif m.get("groups"):
                piece += " groups=" + json.dumps(m["groups"], ensure_ascii=False, default=str)
            out.append(piece)
        truncated = count > len(matches)
        if truncated:
            out.append(f"(showing first {len(matches)} of {count if count <= max_matches * 10 else 'many'} matches)")

        return {
            "output": "\n".join(out),
            "exit_code": 0,
            "count": count,
            "matches": matches,
            "truncated": truncated,
        }
