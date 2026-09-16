"""psd.ai look & feel for the desktop app.

The browser UI is driven by five CSS custom properties per theme
(``bg``, ``fg``, ``panel``, ``border``, ``red``) defined in
``static/js/theme.js``. This module ports those same themes so the desktop app
looks like psd.ai, then derives the rest of the palette (surfaces, hover and
selection colours, syntax colours) from them and renders a Qt Style Sheet.

Everything is computed from the five seed colours, so a custom theme a user
saved in the web UI keeps working here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Dict, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QColor, QFont, QFontDatabase, QGuiApplication, QIcon, QPainter, QPixmap, QPolygonF,
)
from PySide6.QtCore import QPointF

# --------------------------------------------------------------------------- #
# Seed palettes — copied from static/js/theme.js (THEMES)
# --------------------------------------------------------------------------- #

THEMES: Dict[str, Dict[str, str]] = {
    "dark":      {"bg": "#282c34", "fg": "#9cdef2", "panel": "#111111", "border": "#355a66", "red": "#e06c75"},
    "light":     {"bg": "#f0ebe3", "fg": "#5a5248", "panel": "#faf6f0", "border": "#d4cdc2", "red": "#c47d5a"},
    "midnight":  {"bg": "#0d1117", "fg": "#c9d1d9", "panel": "#161b22", "border": "#30363d", "red": "#f85149"},
    "paper":     {"bg": "#faf8f5", "fg": "#3b3836", "panel": "#ffffff", "border": "#d5d0c8", "red": "#c5ac4a"},
    "cyberpunk": {"bg": "#0a0a0f", "fg": "#0ff0fc", "panel": "#12101a", "border": "#9b30ff", "red": "#e040fb"},
    "retrowave": {"bg": "#1a1a2e", "fg": "#e94560", "panel": "#16213e", "border": "#533483", "red": "#e94560"},
    "forest":    {"bg": "#1b2a1b", "fg": "#a8d5a2", "panel": "#142414", "border": "#3d6b3d", "red": "#7cb871"},
    "ocean":     {"bg": "#0b1a2c", "fg": "#64d2ff", "panel": "#091422", "border": "#1e5074", "red": "#4facfe"},
    "ume":       {"bg": "#2b1b2e", "fg": "#f5c2e7", "panel": "#1e1420", "border": "#6c4675", "red": "#f5a0c0"},
    "copper":    {"bg": "#1c1410", "fg": "#e8c39e", "panel": "#140f0a", "border": "#7a5533", "red": "#d4764e"},
    "terminal":  {"bg": "#000000", "fg": "#00ff41", "panel": "#0a0a0a", "border": "#003b00", "red": "#00ff41"},
    "organs":    {"bg": "#0a0406", "fg": "#efe1c8", "panel": "#15080a", "border": "#3a1519", "red": "#c83240"},
    "lavender":  {"bg": "#f3eef8", "fg": "#3d3551", "panel": "#faf7ff", "border": "#cec3de", "red": "#9b6dcc"},
    "gpt":       {"bg": "#212121", "fg": "#ececec", "panel": "#171717", "border": "#424242", "red": "#949494"},
    "claude":    {"bg": "#262624", "fg": "#f5f4f0", "panel": "#30302e", "border": "#4a4a47", "red": "#c6613f"},
    "cute":      {"bg": "#fff0f5", "fg": "#d4608a", "panel": "#fff8fa", "border": "#f0c0d0", "red": "#ff6b9d"},
}

DEFAULT_THEME = "dark"

# Syntax colours from static/style.css (:root / :root.light)
SYNTAX_DARK = {
    "bg": "#1e2228", "fg": "#9cdef2", "keyword": "#c678dd", "string": "#e5c07b",
    "comment": "#828997", "function": "#61afef", "number": "#d19a66",
    "builtin": "#56b6c2", "variable": "#abb2bf",
}
SYNTAX_LIGHT = {
    "bg": "#f9f9f9", "fg": "#2b2b2b", "keyword": "#7928a1", "string": "#986801",
    "comment": "#6a737d", "function": "#005cc5", "number": "#986801",
    "builtin": "#0070a0", "variable": "#383a42",
}

SEMANTIC = {
    "success": "#4caf50",
    "warning": "#f0ad4e",
    "error": "#ff4444",
    "info": "#00aaff",
    "agent": "#00ff00",
}

HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

FONT_UI_CANDIDATES = ["Segoe UI Variable Text", "Segoe UI", "Ubuntu", "Cantarell",
                      "Helvetica Neue", "Arial", "Sans Serif"]
FONT_MONO_CANDIDATES = ["Cascadia Mono", "Consolas", "Fira Code", "JetBrains Mono",
                        "DejaVu Sans Mono", "Menlo", "Monospace"]
FONT_SERIF_CANDIDATES = ["Georgia", "Times New Roman", "DejaVu Serif", "Serif"]


def _qcolor(value: str, fallback: str = "#888888") -> QColor:
    if isinstance(value, QColor):
        return QColor(value)
    text = str(value or "").strip()
    if not HEX_RE.match(text):
        text = fallback
    color = QColor(text)
    if not color.isValid():
        color = QColor(fallback)
    return color


def _mix(a: QColor, b: QColor, weight: float) -> QColor:
    """Linear RGB mix; ``weight`` is how much of ``b`` to use."""
    weight = max(0.0, min(1.0, float(weight)))
    return QColor(
        round(a.red() + (b.red() - a.red()) * weight),
        round(a.green() + (b.green() - a.green()) * weight),
        round(a.blue() + (b.blue() - a.blue()) * weight),
        a.alpha(),
    )


def _luminance(color: QColor) -> float:
    return (0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()) / 255.0


def _alpha(color: QColor, alpha: float) -> str:
    clone = QColor(color)
    clone.setAlphaF(max(0.0, min(1.0, alpha)))
    return clone.name(QColor.HexArgb)


@dataclass
class Theme:
    """A fully resolved psd.ai palette."""

    name: str = DEFAULT_THEME
    bg: str = "#282c34"
    fg: str = "#9cdef2"
    panel: str = "#111111"
    border: str = "#355a66"
    red: str = "#e06c75"
    ui_font: str = ""
    mono_font: str = ""
    serif_font: str = ""
    density: str = "comfortable"      # compact | comfortable
    font_family: str = "sans"         # sans | mono | serif  (matches web UI pref)
    font_size: int = 10

    # derived (filled by resolve())
    surface: str = ""
    surface_alt: str = ""
    surface_hover: str = ""
    surface_active: str = ""
    input_bg: str = ""
    text: str = ""
    text_muted: str = ""
    text_faint: str = ""
    accent: str = ""
    accent_soft: str = ""
    accent_text: str = ""
    selection_bg: str = ""
    rail_bg: str = ""
    syntax: Dict[str, str] = field(default_factory=dict)
    is_dark: bool = True

    # ------------------------------------------------------------------ #
    @classmethod
    def from_seed(cls, name: str, seed: Dict[str, str], **overrides) -> "Theme":
        base = dict(THEMES.get(name, THEMES[DEFAULT_THEME]))
        for key in ("bg", "fg", "panel", "border", "red"):
            value = str(seed.get(key) or base.get(key) or "").strip()
            if HEX_RE.match(value):
                base[key] = value
        theme = cls(name=name, **base)
        for key, value in overrides.items():
            if hasattr(theme, key) and value is not None:
                setattr(theme, key, value)
        return theme.resolve()

    def ensure_fonts(self) -> "Theme":
        """Populate ui/mono/serif font names (safe to call before any widget)."""
        self.resolved_fonts()
        return self

    def resolve(self) -> "Theme":
        bg = _qcolor(self.bg, "#282c34")
        fg = _qcolor(self.fg, "#d1d4e0")
        panel = _qcolor(self.panel, "#111111")
        border = _qcolor(self.border, "#355a66")
        accent = _qcolor(self.red, "#e06c75")

        dark = _luminance(bg) < 0.5
        white = QColor("#ffffff")
        black = QColor("#000000")

        # Surfaces: the panel colour is the deepest layer (chat area), the bg
        # is the raised layer (cards, sidebar) — same relationship as the web UI.
        self.is_dark = dark
        self.surface = bg.name()
        self.surface_alt = _mix(bg, panel, 0.55).name()
        self.surface_hover = _mix(bg, white if dark else black, 0.07).name()
        self.surface_active = _mix(bg, accent, 0.18).name()
        self.input_bg = _mix(panel, bg, 0.35).name()
        self.rail_bg = _mix(panel, bg, 0.25).name()

        self.text = fg.name()
        self.text_muted = _mix(fg, bg, 0.45).name()
        self.text_faint = _mix(fg, bg, 0.68).name()

        self.accent = accent.name()
        self.accent_soft = _mix(accent, bg, 0.72).name()
        # Text drawn on top of the accent must stay readable.
        self.accent_text = "#111111" if _luminance(accent) > 0.62 else "#ffffff"
        self.selection_bg = _mix(accent, bg, 0.45).name()

        self.syntax = dict(SYNTAX_DARK if dark else SYNTAX_LIGHT)
        self.syntax["border"] = _mix(border, bg, 0.35).name()
        return self

    # ------------------------------------------------------------------ #
    def resolved_fonts(self) -> Tuple[str, str, str]:
        """Pick (ui, mono, serif) families that actually exist on this machine."""
        if self.ui_font and self.mono_font and getattr(self, "serif_font", ""):
            return self.ui_font, self.mono_font, self.serif_font
        if QGuiApplication.instance() is None:
            # Pure-logic context (unit tests, headless renders): no font DB yet.
            self.ui_font = self.ui_font or "Sans Serif"
            self.mono_font = self.mono_font or "Monospace"
            self.serif_font = self.serif_font or "Serif"
            return self.ui_font, self.mono_font, self.serif_font
        available = set(QFontDatabase.families())

        def pick(candidates, fallback: str) -> str:
            for candidate in candidates:
                if candidate in available:
                    return candidate
            return fallback

        default_ui = QFont().family() or "Sans Serif"
        default_mono = QFontDatabase.systemFont(QFontDatabase.FixedFont).family() or "Monospace"
        ui = pick(FONT_UI_CANDIDATES, default_ui)
        mono = pick(FONT_MONO_CANDIDATES, default_mono)
        serif = pick(FONT_SERIF_CANDIDATES, "Serif")
        return ui, mono, serif

    def apply_to_app(self, app) -> None:
        """Install fonts, base palette and the generated stylesheet."""
        ui, mono, serif = self.resolved_fonts()
        self.ui_font = ui
        self.mono_font = mono
        self.serif_font = serif

        family = {"sans": ui, "mono": mono, "serif": serif}.get(self.font_family, ui)
        size = max(8, min(16, int(self.font_size or 10)))
        font = QFont(family, size)
        font.setStyleHint(QFont.SansSerif if family == ui else QFont.TypeWriter)
        app.setFont(font)

        # Native dialogs / menus take colours from the palette, not the QSS.
        palette = app.palette()
        from PySide6.QtGui import QPalette

        bg = _qcolor(self.surface)
        palette.setColor(QPalette.Window, bg)
        palette.setColor(QPalette.WindowText, _qcolor(self.text))
        palette.setColor(QPalette.Base, _qcolor(self.input_bg))
        palette.setColor(QPalette.AlternateBase, _qcolor(self.surface_alt))
        palette.setColor(QPalette.Text, _qcolor(self.text))
        palette.setColor(QPalette.Button, _qcolor(self.surface_alt))
        palette.setColor(QPalette.ButtonText, _qcolor(self.text))
        palette.setColor(QPalette.BrightText, _qcolor(self.accent))
        palette.setColor(QPalette.Highlight, _qcolor(self.accent))
        palette.setColor(QPalette.HighlightedText, _qcolor(self.accent_text))
        palette.setColor(QPalette.ToolTipBase, _qcolor(self.surface_alt))
        palette.setColor(QPalette.ToolTipText, _qcolor(self.text))
        palette.setColor(QPalette.Link, _qcolor(self.accent))
        palette.setColor(QPalette.PlaceholderText, _qcolor(self.text_faint))
        app.setPalette(palette)
        app.setStyleSheet(self.stylesheet())

    # ------------------------------------------------------------------ #
    def spacing(self) -> Dict[str, int]:
        compact = self.density == "compact"
        return {
            "pad": 8 if compact else 12,
            "pad_s": 5 if compact else 7,
            "row": 26 if compact else 32,
            "radius": 8,
            "gap": 8 if compact else 12,
        }

    def stylesheet(self) -> str:
        sp = self.spacing()
        radius = sp["radius"]
        border = _qcolor(self.border)
        border_soft = _mix(border, _qcolor(self.surface), 0.55).name()
        success, warning, error, info = (
            SEMANTIC["success"], SEMANTIC["warning"], SEMANTIC["error"], SEMANTIC["info"]
        )
        scrollbar_bg = self.surface_alt
        return f"""
/* ===== psd.ai desktop — generated from theme "{self.name}" ===== */
* {{
    font-family: "{self.ui_font}";
    outline: none;
}}
QWidget {{
    background-color: {self.surface};
    color: {self.text};
    font-size: {max(8, min(16, int(self.font_size or 10)))}pt;
    selection-background-color: {self.selection_bg};
    selection-color: {self.text};
}}
QToolTip {{
    background-color: {self.surface_alt};
    color: {self.text};
    border: 1px solid {border.name()};
    padding: 5px 8px;
    border-radius: 4px;
}}

/* ---------- navigation rail ---------- */
QWidget#NavRail {{
    background-color: {self.rail_bg};
    border-right: 1px solid {border_soft};
}}
QLabel#AppBrand {{
    color: {self.accent};
    font-size: {int(self.font_size or 10) + 5}pt;
    font-weight: 700;
    padding: 6px 10px 10px 12px;
    background: transparent;
}}
QPushButton#NavButton {{
    background: transparent;
    border: none;
    border-left: 3px solid transparent;
    color: {self.text_muted};
    text-align: left;
    padding: 9px 12px 9px 13px;
    border-radius: 0px;
    font-size: {max(8, min(16, int(self.font_size or 10)))}pt;
}}
QPushButton#NavButton:hover {{
    background-color: {self.surface_hover};
    color: {self.text};
}}
QPushButton#NavButton:checked {{
    background-color: {self.surface_active};
    border-left: 3px solid {self.accent};
    color: {self.text};
    font-weight: 600;
}}
QLabel#NavSection {{
    color: {self.text_faint};
    font-size: 8pt;
    font-weight: 700;
    letter-spacing: 1px;
    padding: 14px 12px 4px 14px;
    background: transparent;
}}
QWidget#NavFooter {{
    background-color: {self.rail_bg};
    border-top: 1px solid {border_soft};
}}

/* ---------- panels / cards ---------- */
QFrame#Card {{
    background-color: {self.surface_alt};
    border: 1px solid {border_soft};
    border-radius: {radius}px;
}}
QFrame#Divider {{
    background-color: {border_soft};
    border: none;
    max-height: 1px;
    min-height: 1px;
}}
QLabel#PageTitle {{
    font-size: {int(self.font_size or 10) + 7}pt;
    font-weight: 700;
    color: {self.text};
    background: transparent;
}}
QLabel#PageSubtitle, QLabel#Muted {{
    color: {self.text_muted};
    background: transparent;
}}
QLabel#Faint {{
    color: {self.text_faint};
    background: transparent;
}}
QLabel#Accent {{
    color: {self.accent};
    font-weight: 600;
    background: transparent;
}}
QLabel#EmptyHint {{
    color: {self.text_faint};
    font-size: {int(self.font_size or 10) + 1}pt;
    background: transparent;
}}
QLabel[role="status-ok"] {{ color: {success}; background: transparent; }}
QLabel[role="status-warn"] {{ color: {warning}; background: transparent; }}
QLabel[role="status-error"] {{ color: {error}; background: transparent; }}

/* ---------- inputs ---------- */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QDateTimeEdit, QComboBox {{
    background-color: {self.input_bg};
    border: 1px solid {border.name()};
    border-radius: {radius - 2}px;
    padding: 6px 8px;
    color: {self.text};
    selection-background-color: {self.accent};
    selection-color: {self.accent_text};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QDateTimeEdit:focus, QComboBox:focus {{
    border: 1px solid {self.accent};
}}
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled,
QSpinBox:disabled, QComboBox:disabled {{
    color: {self.text_faint};
    background-color: {self.surface_alt};
}}
QPlainTextEdit#CodeEditor, QTextEdit#RichText {{
    font-family: "{self.mono_font}";
}}
QComboBox::drop-down {{
    border: none;
    width: 22px;
    subcontrol-origin: padding;
    subcontrol-position: center right;
}}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {self.text_muted};
    width: 0; height: 0;
    margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background-color: {self.surface_alt};
    border: 1px solid {border.name()};
    selection-background-color: {self.selection_bg};
    selection-color: {self.text};
    color: {self.text};
    padding: 4px;
}}
QCheckBox, QRadioButton {{
    background: transparent;
    color: {self.text};
    spacing: 8px;
    padding: 2px 0px;
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {border.name()};
    background-color: {self.input_bg};
}}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {self.accent}; }}
QCheckBox::indicator:checked {{
    background-color: {self.accent};
    border-color: {self.accent};
    image: url(:/qt-project.org/styles/commonstyle/images/standardbutton-apply-16.png);
}}
QRadioButton::indicator:checked {{
    background-color: {self.accent};
    border-color: {self.accent};
}}

/* ---------- buttons ---------- */
QPushButton {{
    background-color: {self.surface_alt};
    border: 1px solid {border.name()};
    border-radius: {radius - 2}px;
    padding: 6px 14px;
    color: {self.text};
    min-height: 18px;
}}
QPushButton:hover {{
    background-color: {self.surface_hover};
    border-color: {self.accent};
}}
QPushButton:pressed {{ background-color: {self.selection_bg}; }}
QPushButton:disabled {{ color: {self.text_faint}; border-color: {border_soft}; background-color: {self.surface_alt}; }}
QPushButton#Primary {{
    background-color: {self.accent};
    border: 1px solid {self.accent};
    color: {self.accent_text};
    font-weight: 600;
}}
QPushButton#Primary:hover {{ background-color: {_mix(_qcolor(self.accent), QColor("#ffffff"), 0.14).name()}; }}
QPushButton#Primary:disabled {{
    background-color: {self.surface_alt}; color: {self.text_faint}; border-color: {border_soft};
}}
QPushButton#Danger {{
    background-color: transparent;
    border: 1px solid {error};
    color: {error};
}}
QPushButton#Danger:hover {{ background-color: {_alpha(_qcolor(error), 0.16)}; }}
QPushButton#Ghost {{
    background: transparent;
    border: 1px solid transparent;
    color: {self.text_muted};
}}
QPushButton#Ghost:hover {{ background-color: {self.surface_hover}; color: {self.text}; border-color: {border_soft}; }}
QPushButton#Chip {{
    background-color: {self.surface_alt};
    border: 1px solid {border_soft};
    border-radius: 12px;
    padding: 3px 10px;
    color: {self.text_muted};
    min-height: 14px;
}}
QPushButton#Chip:checked, QPushButton#Chip[on="true"] {{
    background-color: {self.accent_soft};
    border-color: {self.accent};
    color: {self.text};
    font-weight: 600;
}}
QPushButton#Chip:hover {{ border-color: {self.accent}; color: {self.text}; }}

/* ---------- lists / trees / tables ---------- */
QListWidget, QTreeWidget, QTreeView, QListView, QTableView, QTableWidget {{
    background-color: {self.panel};
    alternate-background-color: {self.surface_alt};
    border: 1px solid {border_soft};
    border-radius: {radius - 2}px;
    color: {self.text};
    padding: 3px;
    gridline-color: {border_soft};
}}
QListWidget::item, QTreeWidget::item, QTableView::item {{
    padding: 6px 8px;
    border-radius: 6px;
    color: {self.text};
}}
QListWidget::item:hover, QTreeWidget::item:hover, QTableView::item:hover {{
    background-color: {self.surface_hover};
}}
QListWidget::item:selected, QTreeWidget::item:selected, QTableView::item:selected {{
    background-color: {self.selection_bg};
    color: {self.text};
}}
QHeaderView::section {{
    background-color: {self.surface_alt};
    color: {self.text_muted};
    border: none;
    border-bottom: 1px solid {border_soft};
    padding: 6px 8px;
    font-weight: 600;
}}
QHeaderView {{ background-color: {self.surface_alt}; border: none; }}
QTableCornerButton::section {{ background-color: {self.surface_alt}; border: none; }}

/* ---------- scrollbars ---------- */
QScrollBar:vertical {{
    background: {scrollbar_bg}; width: 11px; margin: 0px; border: none;
}}
QScrollBar::handle:vertical {{
    background: {_mix(border, _qcolor(self.text_muted), 0.35).name()};
    min-height: 28px; border-radius: 5px; margin: 2px;
}}
QScrollBar::handle:vertical:hover {{ background: {self.accent}; }}
QScrollBar:horizontal {{
    background: {scrollbar_bg}; height: 11px; margin: 0px; border: none;
}}
QScrollBar::handle:horizontal {{
    background: {_mix(border, _qcolor(self.text_muted), 0.35).name()};
    min-width: 28px; border-radius: 5px; margin: 2px;
}}
QScrollBar::handle:horizontal:hover {{ background: {self.accent}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------- tabs / splitters / progress ---------- */
QTabWidget::pane {{ border: 1px solid {border_soft}; border-radius: {radius - 2}px; top: -1px; }}
QTabBar::tab {{
    background: transparent; color: {self.text_muted};
    padding: 7px 14px; border: none;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:hover {{ color: {self.text}; }}
QTabBar::tab:selected {{ color: {self.text}; border-bottom: 2px solid {self.accent}; font-weight: 600; }}
QSplitter::handle {{ background-color: {border_soft}; }}
QSplitter::handle:hover {{ background-color: {self.accent}; }}
QProgressBar {{
    background-color: {self.input_bg};
    border: 1px solid {border_soft};
    border-radius: 6px;
    text-align: center;
    color: {self.text};
    min-height: 12px;
}}
QProgressBar::chunk {{ background-color: {self.accent}; border-radius: 5px; }}
QStatusBar {{
    background-color: {self.rail_bg};
    color: {self.text_muted};
    border-top: 1px solid {border_soft};
}}
QStatusBar::item {{ border: none; }}
QMenuBar {{ background-color: {self.rail_bg}; color: {self.text}; border-bottom: 1px solid {border_soft}; }}
QMenuBar::item:selected {{ background-color: {self.selection_bg}; }}
QMenu {{
    background-color: {self.surface_alt};
    color: {self.text};
    border: 1px solid {border.name()};
    padding: 5px;
}}
QMenu::item {{ padding: 6px 22px 6px 14px; border-radius: 5px; }}
QMenu::item:selected {{ background-color: {self.selection_bg}; }}
QMenu::separator {{ height: 1px; background: {border_soft}; margin: 5px 8px; }}
QGroupBox {{
    border: 1px solid {border_soft};
    border-radius: {radius}px;
    margin-top: 14px;
    padding: {sp['pad']}px;
    font-weight: 600;
    color: {self.text};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: {self.text_muted};
}}
QSlider::groove:horizontal {{ height: 4px; background: {self.surface_hover}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    width: 14px; height: 14px; margin: -6px 0;
    border-radius: 7px; background: {self.accent};
}}
QSlider::sub-page:horizontal {{ background: {self.accent}; border-radius: 2px; }}

/* ---------- chat specifics ---------- */
QFrame#MessageUser {{
    background-color: {_mix(_qcolor(self.accent), _qcolor(self.surface), 0.86).name()};
    border: 1px solid {_mix(_qcolor(self.accent), _qcolor(self.border), 0.6).name()};
    border-radius: {radius + 2}px;
}}
QFrame#MessageAssistant {{
    background-color: {self.surface_alt};
    border: 1px solid {border_soft};
    border-radius: {radius + 2}px;
}}
QFrame#MessageSystem {{
    background-color: {self.surface_alt};
    border-left: 3px solid {info};
    border-radius: 4px;
}}
QFrame#ToolCard {{
    background-color: {_mix(_qcolor(self.panel), _qcolor(self.surface), 0.5).name()};
    border: 1px solid {border_soft};
    border-left: 3px solid {self.accent};
    border-radius: 6px;
}}
QFrame#ApprovalCard {{
    background-color: {_mix(_qcolor(warning), _qcolor(self.surface), 0.88).name()};
    border: 1px solid {warning};
    border-radius: {radius}px;
}}
QLabel#RoleUser {{ color: {self.accent}; font-weight: 700; background: transparent; }}
QLabel#RoleAssistant {{ color: {_mix(_qcolor(self.fg), QColor('#ffffff'), 0.2).name()}; font-weight: 700; background: transparent; }}
QLabel#MetaLine {{ color: {self.text_faint}; font-size: 8pt; background: transparent; }}
QFrame#Composer {{
    background-color: {self.input_bg};
    border: 1px solid {border.name()};
    border-radius: {radius + 2}px;
}}
QFrame#Composer:focus-within {{ border: 1px solid {self.accent}; }}
QPlainTextEdit#ComposerInput {{
    background: transparent;
    border: none;
    padding: 8px 10px;
    font-family: "{self.mono_font if self.font_family == 'mono' else self.ui_font}";
    font-size: {max(8, min(16, int(self.font_size or 10)))}pt;
}}
QLabel#StatusPill {{
    background-color: {self.surface_alt};
    border: 1px solid {border_soft};
    border-radius: 9px;
    padding: 2px 10px;
    color: {self.text_muted};
}}
QLabel#StatusPill[ok="true"] {{ color: {success}; border-color: {_alpha(_qcolor(success), 0.5)}; }}
QLabel#StatusPill[busy="true"] {{ color: {warning}; border-color: {_alpha(_qcolor(warning), 0.5)}; }}
QLabel#StatusPill[bad="true"] {{ color: {error}; border-color: {_alpha(_qcolor(error), 0.5)}; }}
QTextBrowser#Transcript, QTextBrowser#RichView {{
    background: transparent;
    border: none;
    padding: 2px;
}}
QFrame#SessionRow {{ background: transparent; border-radius: 6px; }}
QFrame#SessionRow:hover {{ background-color: {self.surface_hover}; }}
QFrame#SessionRow[selected="true"] {{
    background-color: {self.surface_active};
    border-left: 2px solid {self.accent};
}}
QLabel#SessionTitle {{ color: {self.text}; font-weight: 600; background: transparent; }}
QLabel#SessionMeta {{ color: {self.text_faint}; font-size: 8pt; background: transparent; }}
QFrame#ThumbCard {{
    background-color: {self.surface_alt};
    border: 1px solid {border_soft};
    border-radius: {radius}px;
}}
QFrame#ThumbCard:hover {{ border-color: {self.accent}; }}
"""

    # ------------------------------------------------------------------ #
    def seed_dict(self) -> Dict[str, str]:
        return {"bg": self.bg, "fg": self.fg, "panel": self.panel,
                "border": self.border, "red": self.red}


# --------------------------------------------------------------------------- #
# Icons
# --------------------------------------------------------------------------- #

def brand_icon(size: int = 64, accent: str = "#e06c75") -> QIcon:
    """The psd.ai sail mark, drawn at runtime (same shape as launcher.py's tray icon)."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    s = size / 64.0
    main = _qcolor(accent, "#e06c75")
    light = QColor(main)
    light.setAlpha(150)

    painter.setBrush(main)
    painter.setPen(Qt.NoPen)
    painter.drawPolygon(QPolygonF([QPointF(32 * s, 10 * s), QPointF(32 * s, 45 * s), QPointF(12 * s, 45 * s)]))
    painter.setBrush(light)
    painter.drawPolygon(QPolygonF([QPointF(32 * s, 18 * s), QPointF(32 * s, 45 * s), QPointF(48 * s, 45 * s)]))
    painter.setBrush(main)
    painter.drawPolygon(QPolygonF([
        QPointF(8 * s, 48 * s), QPointF(56 * s, 48 * s),
        QPointF(44 * s, 56 * s), QPointF(20 * s, 56 * s),
    ]))
    painter.end()
    return QIcon(pixmap)


def dot_icon(color: str, size: int = 12) -> QIcon:
    """Small coloured dot, used for status pills in the nav rail."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setBrush(_qcolor(color))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(1, 1, size - 2, size - 2)
    painter.end()
    return QIcon(pixmap)


def theme_swatch(theme_name: str, size: int = 34) -> QPixmap:
    """A little preview tile for the theme picker."""
    seed = THEMES.get(theme_name, THEMES[DEFAULT_THEME])
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setBrush(_qcolor(seed["bg"]))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(0, 0, size, size, 6, 6)
    painter.setBrush(_qcolor(seed["panel"]))
    painter.drawRoundedRect(4, size - 14, size - 8, 10, 3, 3)
    painter.setBrush(_qcolor(seed["red"]))
    painter.drawRoundedRect(4, 4, size - 8, 8, 3, 3)
    painter.setBrush(_qcolor(seed["fg"]))
    painter.drawRoundedRect(7, 16, size - 20, 3, 1, 1)
    painter.drawRoundedRect(7, 21, size - 26, 3, 1, 1)
    painter.end()
    return pixmap


def current_theme() -> Theme:
    """The theme held by the running application (set by :func:`set_current`)."""
    return _CURRENT


_CURRENT: Theme = Theme.from_seed(DEFAULT_THEME, THEMES[DEFAULT_THEME])


def set_current(theme: Theme) -> Theme:
    global _CURRENT
    _CURRENT = theme
    return theme
