"""Markdown -> Qt rich text.

The browser UI renders model output with a JavaScript markdown pipeline
(``static/js/markdown/``). The desktop app needs the same fidelity inside
``QTextBrowser``, whose rich-text engine supports an HTML 4 / CSS 2 subset and
no stylesheets-by-class — so this module converts markdown with the ``markdown``
package (already a hard dependency of psd.ai) and then rewrites the result into
inline-styled Qt-friendly HTML:

* fenced code blocks become a two-row table: a language/copy header and the
  highlighted source;
* inline code, quotes, tables, headings, lists and links get inline styles from
  the active theme;
* ``<img>`` tags are rewritten to ``psd-image:`` anchors that the view resolves
  through the in-process backend (no localhost fetches);
* copy buttons and links use ``psd-copy:N`` / ``psd-open:URL`` anchors so the
  view can handle them natively.

The renderer is pure (no Qt widgets) apart from reading colours out of
:class:`gui.theme.Theme`, which keeps it unit-testable headless.
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:  # `markdown` is a hard psd.ai dependency (see requirements.txt)
    import markdown as _markdown
except Exception:  # pragma: no cover - degrade gracefully
    _markdown = None

from gui.theme import Theme, current_theme

CODE_COPY_PREFIX = "psd-copy:"
LINK_PREFIX = "psd-open:"
IMAGE_PREFIX = "psd-image:"

# --------------------------------------------------------------------------- #
# Syntax highlighting
# --------------------------------------------------------------------------- #

KEYWORDS = {
    "python": (
        "and as assert async await break class continue def del elif else except "
        "finally for from global if import in is lambda nonlocal not or pass raise "
        "return try while with yield match case self cls None True False"
    ),
    "js": (
        "abstract arguments await break case catch class const continue debugger "
        "default delete do else enum eval export extends false finally for from "
        "function get if implements import in instanceof interface let new null "
        "package private protected public return set static super switch this "
        "throw true try typeof var void while with yield of async"
    ),
    "bash": (
        "if then else elif fi for while until do done case esac function in select "
        "time echo printf cd ls rm cp mv mkdir cat grep sed awk curl export source "
        "sudo apt pip python python3 git npm node docker kill sleep test local "
        "return set unset shift read exit"
    ),
    "sql": (
        "select from where insert into values update set delete create table alter "
        "drop join left right inner outer on group by order having limit offset "
        "and or not null as distinct case when then else end union all index view "
        "primary foreign key references default constraint commit rollback begin"
    ),
    "yaml": "",
    "json": "",
}
KEYWORDS["javascript"] = KEYWORDS["js"]
KEYWORDS["typescript"] = KEYWORDS["js"]
KEYWORDS["ts"] = KEYWORDS["js"]
KEYWORDS["jsx"] = KEYWORDS["js"]
KEYWORDS["tsx"] = KEYWORDS["js"]
KEYWORDS["sh"] = KEYWORDS["bash"]
KEYWORDS["shell"] = KEYWORDS["bash"]
KEYWORDS["zsh"] = KEYWORDS["bash"]
KEYWORDS["py"] = KEYWORDS["python"]
KEYWORDS["python3"] = KEYWORDS["python"]

BUILTINS = (
    "print len range int str float list dict set tuple bool open isinstance "
    "enumerate zip map sorted sum min max abs round repr id type super getattr "
    "setattr hasattr vars dir next iter Exception ValueError TypeError KeyError "
    "RuntimeError IndexError AttributeError ImportError OSError FileNotFoundError "
    "console document window require module exports process JSON Math Object Array "
    "String Number Promise setTimeout setInterval fetch localStorage null undefined "
    "True False None self"
)

_ALIAS = {
    "py": "python", "python3": "python", "js": "javascript", "ts": "typescript",
    "tsx": "typescript", "jsx": "javascript", "sh": "bash", "shell": "bash",
    "zsh": "bash", "yml": "yaml", "jsonc": "json", "c++": "cpp", "golang": "go",
    "rs": "rust", "rb": "ruby", "ps1": "powershell", "bat": "bash", "cmd": "bash",
    "md": "markdown", "txt": "text", "": "text",
}


def normalize_language(language: str) -> str:
    lang = (language or "").strip().lower()
    lang = _ALIAS.get(lang, lang)
    return lang or "text"


_TOKEN_RE = re.compile(
    r"""
    (?P<comment>\#[^\n]*|//[^\n]*|/\*.*?\*/|--[^\n]*|;[^\n]*)
  | (?P<string>\"\"\".*?\"\"\"|'''.*?'''|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)
  | (?P<number>\b0[xXbBoO][0-9a-fA-F_]+\b|\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b)
  | (?P<decorator>^\s*@\w+)
  | (?P<name>[A-Za-z_$][\w$]*)
  | (?P<punct>[{}()\[\]<>=+\-*/%&|^!~?:,.])
    """,
    re.VERBOSE | re.DOTALL | re.MULTILINE,
)


def highlight_code(code: str, language: str, theme: Theme) -> str:
    """Return syntax-highlighted, HTML-escaped code for Qt rich text."""
    lang = normalize_language(language)
    colors = theme.syntax
    keyword_set = set()
    for key in (lang, _ALIAS.get(lang, "")):
        words = KEYWORDS.get(key)
        if words:
            keyword_set.update(words.split())
            break
    if not keyword_set:
        # Unknown language: still colour strings/comments/numbers.
        keyword_set = set()
    builtin_set = set(BUILTINS.split())

    out: List[str] = []
    pos = 0
    for match in _TOKEN_RE.finditer(code):
        if match.start() > pos:
            out.append(html_lib.escape(code[pos:match.start()]))
        pos = match.end()
        text = match.group(0)
        kind = match.lastgroup or ""
        if kind == "comment" and lang not in ("yaml", "text", "markdown"):
            # '#' starts a comment in most languages but is a YAML key marker.
            if lang == "json" or lang == "sql" and text.startswith("#"):
                out.append(html_lib.escape(text))
                continue
            out.append(_span(text, colors["comment"], italic=True))
        elif kind == "string":
            out.append(_span(text, colors["string"]))
        elif kind == "number":
            out.append(_span(text, colors["number"]))
        elif kind == "decorator":
            out.append(_span(text, colors["keyword"]))
        elif kind == "name":
            if text in keyword_set:
                out.append(_span(text, colors["keyword"], bold=True))
            elif text in builtin_set:
                out.append(_span(text, colors["builtin"]))
            elif text[:1].isupper():
                out.append(_span(text, colors["builtin"]))
            else:
                out.append(_span(text, colors["variable"]))
        else:
            out.append(html_lib.escape(text))
    if pos < len(code):
        out.append(html_lib.escape(code[pos:]))
    return "".join(out)


def _span(text: str, color: str, bold: bool = False, italic: bool = False) -> str:
    style = f"color:{color};"
    if bold:
        style += "font-weight:700;"
    if italic:
        style += "font-style:italic;"
    return f'<span style="{style}">{html_lib.escape(text)}</span>'


# --------------------------------------------------------------------------- #
# Renderer
# --------------------------------------------------------------------------- #


@dataclass
class RenderResult:
    """HTML for ``QTextBrowser.setHtml`` plus the payloads the view needs."""

    html: str = ""
    code_blocks: Dict[str, str] = field(default_factory=dict)   # anchor id -> source
    images: List[str] = field(default_factory=list)             # requested src values
    links: List[str] = field(default_factory=list)              # rewritten hrefs
    plain_text: str = ""                                        # markdown source


@dataclass
class RenderOptions:
    nl2br: bool = False            # chat turns single newlines into <br>
    tables: bool = True
    max_html_chars: int = 400_000  # safety valve for pathological payloads
    linkify_bare_urls: bool = True


_FENCE_RE = re.compile(
    r"^(?P<indent>[ \t]*)```+(?P<lang>[^\n`]*)\n(?P<body>.*?)^([ \t]*)```+[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


def _extract_fences(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Pull fenced code blocks out before markdown runs.

    ``markdown``'s fenced_code extension is good, but pre-extracting lets us
    keep the *exact* source for the copy button and keeps nested fences (```
    inside ~~~) predictable.
    """
    blocks: List[Tuple[str, str]] = []

    def repl(match: "re.Match[str]") -> str:
        language = (match.group("lang") or "").strip()
        body = match.group("body")
        if body.endswith("\n"):
            body = body[:-1]
        blocks.append((language, body))
        return f"\n\n@@PSDCODE{len(blocks) - 1}@@\n\n"

    replaced = _FENCE_RE.sub(repl, text)
    return replaced, blocks


_PLACEHOLDER_RE = re.compile(r"@@PSDCODE(\d+)@@")


def _rewrite_images(html_text: str, images: List[str]) -> str:
    def repl(match: "re.Match[str]") -> str:
        src = match.group("src")
        alt = match.group("alt") or ""
        if src.startswith("data:"):
            # Qt cannot render data: URIs; hand it to the view as a resource.
            images.append(src)
            idx = len(images) - 1
            return f'<img src="{IMAGE_PREFIX}{idx}" alt="{html_lib.escape(alt)}"/>'
        images.append(src)
        idx = len(images) - 1
        return f'<img src="{IMAGE_PREFIX}{idx}" alt="{html_lib.escape(alt)}"/>'

    return re.sub(
        r'<img[^>]*?src="(?P<src>[^"]*)"[^>]*?(?:alt="(?P<alt>[^"]*)")?[^>]*/?>',
        repl,
        html_text,
    )


_INTERNAL_PREFIXES = (CODE_COPY_PREFIX, LINK_PREFIX, IMAGE_PREFIX, "#")


def _rewrite_links(html_text: str, links: List[str], accent: str) -> str:
    """Route every anchor through the view (``psd-open:N``).

    Anchors we generated ourselves (copy buttons, in-page fragments) are left
    untouched so the view can recognise them.
    """

    def repl(match: "re.Match[str]") -> str:
        href = match.group("href")
        inner = match.group("inner")
        if href.startswith(_INTERNAL_PREFIXES):
            return (
                f'<a href="{href}" style="color:{accent};text-decoration:none;">{inner}</a>'
            )
        links.append(href)
        return (
            f'<a href="{LINK_PREFIX}{len(links) - 1}" '
            f'style="color:{accent};text-decoration:none;">{inner}</a>'
        )

    return re.sub(
        r'<a href="(?P<href>[^"]*)"[^>]*>(?P<inner>.*?)</a>',
        repl,
        html_text,
        flags=re.DOTALL,
    )


_MD_LINK_RE = re.compile(r"(!?)\[[^\]]*\]\([^)]*\)")
_BARE_URL_RE = re.compile(r"(?<![\w\"'>=/\(\[])(https?://[^\s<>\)\]]+[^\s<>\)\].,;:])")


def _linkify_bare_urls(text: str) -> str:
    """Turn naked URLs in plain paragraphs into links (the web UI does this).

    Existing ``[label](url)`` / ``![alt](url)`` markdown is protected first so
    a URL that is already a link is never wrapped twice.
    """
    protected: List[str] = []

    def stash(match: "re.Match[str]") -> str:
        protected.append(match.group(0))
        return f"\x00MDLINK{len(protected) - 1}\x00"

    stashed = _MD_LINK_RE.sub(stash, text)
    linked = _BARE_URL_RE.sub(r"[\1](\1)", stashed)

    def unstash(match: "re.Match[str]") -> str:
        return protected[int(match.group(1))]

    return re.sub(r"\x00MDLINK(\d+)\x00", unstash, linked)


def _fallback_markdown(text: str) -> str:
    """Minimal converter used only if the `markdown` package is unavailable."""
    escaped = html_lib.escape(text)
    lines = escaped.split("\n")
    out: List[str] = []
    in_list = False
    for line in lines:
        stripped = line.strip()
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        item = re.match(r"^[-*+]\s+(.*)$", stripped)
        if heading:
            level = min(6, len(heading.group(1)) + 1)
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h{level}>{heading.group(2)}</h{level}>")
        elif item:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{item.group(1)}</li>")
        elif not stripped:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("<br/>")
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<p>{stripped}</p>")
    if in_list:
        out.append("</ul>")
    joined = "\n".join(out)
    joined = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", joined)
    joined = re.sub(r"(?<!\*)\*([^\*\n]+)\*(?!\*)", r"<i>\1</i>", joined)
    joined = re.sub(r"`([^`]+)`", r"<code>\1</code>", joined)
    return joined


def render_markdown(
    text: str,
    theme: Optional[Theme] = None,
    options: Optional[RenderOptions] = None,
) -> RenderResult:
    """Convert markdown to Qt rich-text HTML."""
    theme = (theme or current_theme()).ensure_fonts()
    options = options or RenderOptions()
    source = text or ""
    result = RenderResult(plain_text=source)
    if len(source) > options.max_html_chars:
        source = source[: options.max_html_chars] + "\n\n…(truncated)"

    prepared, fences = _extract_fences(source)
    if options.linkify_bare_urls:
        prepared = _linkify_bare_urls(prepared)

    if _markdown is not None:
        extensions = ["sane_lists"]
        if options.nl2br:
            extensions.append("nl2br")
        if options.tables:
            extensions.append("tables")
        try:
            body = _markdown.markdown(
                prepared,
                extensions=extensions,
                output_format="html",
                tab_length=4,
            )
        except Exception:  # pragma: no cover - malformed input
            body = _fallback_markdown(prepared)
    else:  # pragma: no cover
        body = _fallback_markdown(prepared)

    body = body.replace("<p></p>", "")

    # ---- code fences -> themed block with language + copy anchor ---------- #
    def fence_repl(match: "re.Match[str]") -> str:
        index = int(match.group(1))
        if index >= len(fences):
            return match.group(0)
        language, code = fences[index]
        anchor = f"code{index}"
        result.code_blocks[anchor] = code
        return _code_block_html(anchor, language, code, theme)

    # Standalone placeholders arrive wrapped in a paragraph; unwrap first so Qt
    # doesn't add paragraph margins around the code table.
    body = re.sub(r"<p>\s*@@PSDCODE(\d+)@@\s*</p>", fence_repl, body)
    body = _PLACEHOLDER_RE.sub(fence_repl, body)
    body = re.sub(r"<p>\s*</p>", "", body)
    body = re.sub(r"\n{3,}", "\n\n", body)

    # ---- strikethrough (~~text~~) ----------------------------------------- #
    body = re.sub(
        r"~~(.+?)~~",
        lambda m: f'<s style="color:{theme.text_faint};">{m.group(1)}</s>',
        body,
        flags=re.DOTALL,
    )

    # ---- inline code ------------------------------------------------------ #
    body = re.sub(
        r"<code>(.*?)</code>",
        lambda m: (
            f'<code style="font-family:\'{theme.mono_font}\';background-color:{theme.syntax["bg"]};'
            f'color:{theme.syntax["fg"]};padding:1px 4px;border-radius:4px;'
            f'border:1px solid {theme.syntax["border"]};">{m.group(1)}</code>'
        ),
        body,
        flags=re.DOTALL,
    )

    # ---- images / links --------------------------------------------------- #
    body = _rewrite_images(body, result.images)
    body = _rewrite_links(body, result.links, theme.accent)

    # ---- block level styling Qt can actually render ---------------------- #
    body = _style_blocks(body, theme)
    result.html = body
    return result


def _code_block_html(anchor: str, language: str, code: str, theme: Theme) -> str:
    lang = normalize_language(language)
    highlighted = highlight_code(code, lang, theme)
    # Qt honours \n inside <pre>; trailing newline would add a blank line.
    highlighted = highlighted.rstrip("\n")
    label = lang if lang != "text" else "text"
    header = (
        f'<table width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td bgcolor="{theme.syntax["border"]}" style="padding:3px 8px;">'
        f'<span style="color:{theme.text_muted};font-family:\'{theme.mono_font}\';'
        f'font-size:8pt;">{html_lib.escape(label)}</span>'
        f'<span style="color:{theme.text_faint};"> &nbsp;·&nbsp; </span>'
        f'<a href="{CODE_COPY_PREFIX}{anchor}" style="color:{theme.accent};'
        f'font-size:8pt;text-decoration:none;">copy</a>'
        f"</td></tr>"
        f'<tr><td bgcolor="{theme.syntax["bg"]}" style="padding:8px 10px;">'
        f'<pre style="font-family:\'{theme.mono_font}\';font-size:9pt;'
        f'color:{theme.syntax["fg"]};margin:0;white-space:pre-wrap;">{highlighted}</pre>'
        f"</td></tr></table>"
    )
    return header


def _style_blocks(html_text: str, theme: Theme) -> str:
    """Apply inline styles Qt understands to headings, quotes, tables, rules."""
    text_color = theme.text
    muted = theme.text_muted
    faint = theme.text_faint
    border = theme.syntax["border"]
    surface_alt = theme.surface_alt

    replacements: List[Tuple[str, str]] = [
        (r"<h1>", f'<h1 style="color:{text_color};font-size:15pt;margin:10px 0 6px 0;">'),
        (r"<h2>", f'<h2 style="color:{text_color};font-size:13pt;margin:10px 0 5px 0;">'),
        (r"<h3>", f'<h3 style="color:{text_color};font-size:11.5pt;margin:9px 0 4px 0;">'),
        (r"<h4>", f'<h4 style="color:{text_color};font-size:10.5pt;margin:8px 0 4px 0;">'),
        (r"<h5>", f'<h5 style="color:{muted};font-size:10pt;margin:8px 0 3px 0;">'),
        (r"<h6>", f'<h6 style="color:{muted};font-size:9.5pt;margin:8px 0 3px 0;">'),
        (
            r"<blockquote>",
            f'<blockquote style="color:{muted};border-left:3px solid {theme.accent};'
            f'margin:6px 0;padding:2px 10px;background-color:{surface_alt};">',
        ),
        (r"<hr\s*/?>", f'<hr style="background-color:{border};height:1px;border:none;margin:8px 0;"/>'),
        (
            r"<table>",
            f'<table cellpadding="4" cellspacing="0" border="0" width="100%" '
            f'style="border-collapse:collapse;margin:6px 0;">',
        ),
        (r"<th>", f'<th align="left" bgcolor="{surface_alt}" style="color:{muted};border:1px solid {border};">'),
        (r"<td>", f'<td style="color:{text_color};border:1px solid {border};">'),
        (r"<ul>", '<ul style="margin:2px 0 2px 0;padding-left:18px;">'),
        (r"<ol>", '<ol style="margin:2px 0 2px 0;padding-left:20px;">'),
        (r"<li>", f'<li style="color:{text_color};margin:1px 0;">'),
        (r"<p>", f'<p style="color:{text_color};margin:3px 0;line-height:145%;">'),
        (r"<strong>", f'<b style="color:{text_color};">'),
        (r"</strong>", "</b>"),
        (r"<em>", f'<i style="color:{text_color};">'),
        (r"</em>", "</i>"),
        (r"<del>", f'<s style="color:{faint};">'),
        (r"</del>", "</s>"),
    ]
    for pattern, replacement in replacements:
        html_text = re.sub(pattern, replacement, html_text)
    return html_text


# --------------------------------------------------------------------------- #
# Small helpers used across views
# --------------------------------------------------------------------------- #

def escape(text: str) -> str:
    return html_lib.escape(str(text if text is not None else ""))


def inline(text: str, theme: Optional[Theme] = None) -> str:
    """One-line markdown (titles, statuses) -> escaped HTML with bold/code."""
    theme = theme or current_theme()
    out = escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    code_style = f'color:{theme.syntax["fg"]};background-color:{theme.syntax["bg"]};'
    out = re.sub(r"`([^`]+)`", lambda m: f'<code style="{code_style}">{m.group(1)}</code>', out)
    return out


def document_css(theme: Theme) -> str:
    """Base HTML wrapper for full-document previews (Documents/Research)."""
    return (
        f'<html><head><style>'
        f'body {{ background-color: {theme.panel}; color: {theme.text}; '
        f'font-family: "{theme.ui_font}"; font-size: {max(9, theme.font_size)}pt; }}'
        f'a {{ color: {theme.accent}; }}'
        f'</style></head><body>'
    )


def wrap_page(theme: Theme, body_html: str) -> str:
    return document_css(theme) + body_html + "</body></html>"
