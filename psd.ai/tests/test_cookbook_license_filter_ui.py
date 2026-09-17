"""Frontend guards for the Cookbook license filter + model-type expansion.

The Cookbook UI is built from JS strings (cookbook.js) and rendered by
cookbook-hwfit.js, which pulls browser globals and can't run under node —
so these pin the feature at the SOURCE level, the repo's standard for
frontend regressions (see test_cookbook_cpu_only_serve.py).

Covers: the License filter select + help chip, the Coding/Reasoning/Chat
model-type options, the access query param, OSS/LIC row badges, the
expanded-panel license line, badge CSS, use-case glyphs, scan-cache
signature, empty-state copy, and filter persistence.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COOKBOOK_JS = (ROOT / "static/js/cookbook.js").read_text(encoding="utf-8")
HWFIT_JS = (ROOT / "static/js/cookbook-hwfit.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static/style.css").read_text(encoding="utf-8")


# ── cookbook.js builds the filter controls ─────────────────────────────

def test_model_type_picker_offers_coding_reasoning_chat():
    for value, label in (
        ('value="coding"', ">Coding<"),
        ('value="reasoning"', ">Reasoning<"),
        ('value="chat"', ">Chat<"),
    ):
        assert value in COOKBOOK_JS, f"missing usecase option {value}"
        assert label in COOKBOOK_JS, f"missing usecase label {label}"


def test_license_filter_select_built_with_both_classes():
    assert 'id="hwfit-access"' in COOKBOOK_JS
    assert 'class="cookbook-field-input hwfit-access"' in COOKBOOK_JS
    assert '<option value="unrestricted">Open</option>' in COOKBOOK_JS
    assert '<option value="restricted">Restricted</option>' in COOKBOOK_JS


def test_license_filter_has_help_chip_explaining_the_split():
    assert "hwfit-access-help" in COOKBOOK_JS
    # The chip must actually explain both classes, not just exist.
    chip_start = COOKBOOK_JS.index("hwfit-access-help")
    chip = COOKBOOK_JS[chip_start:chip_start + 600]
    assert "Open = permissive" in chip or "permissive" in chip
    assert "Restricted" in chip


# ── cookbook-hwfit.js wires the filter to the API ──────────────────────

def test_access_param_sent_to_models_endpoint():
    assert "const accessPref = document.getElementById('hwfit-access')?.value || '';" in HWFIT_JS
    assert "params.set('access', accessPref)" in HWFIT_JS


def test_access_in_scan_signature():
    # The scan-cache signature must include the license filter, otherwise a
    # cached list from a different filter could be repainted.
    assert "a: document.getElementById('hwfit-access')?.value || ''" in HWFIT_JS


def test_license_filter_bound_and_refetches():
    assert "const apref = document.getElementById('hwfit-access');" in HWFIT_JS
    assert "apref.addEventListener('change'" in HWFIT_JS


def test_empty_state_mentions_license_filter():
    assert "|| document.getElementById('hwfit-access')?.value" in HWFIT_JS
    assert "license" in HWFIT_JS.split("No models match these filters")[1][:140]


# ── row + panel rendering ──────────────────────────────────────────────

def test_row_badges_render_oss_and_lic():
    assert "m.access === 'unrestricted'" in HWFIT_JS
    assert "m.access === 'restricted'" in HWFIT_JS
    assert "hwfit-access-open" in HWFIT_JS and ">OSS</span>" in HWFIT_JS
    assert "hwfit-access-restricted" in HWFIT_JS and ">LIC</span>" in HWFIT_JS
    # The row template actually includes the badge between MoE and IMG.
    assert "${moeBadge}${accessBadge}${imgBadge}" in HWFIT_JS


def test_badge_tooltips_carry_the_license():
    # Tooltips embed the license label so users can see the terms inline.
    assert "esc(m.license)" in HWFIT_JS


def test_expanded_panel_shows_license_before_download():
    assert "Open license${modelData.license" in HWFIT_JS
    assert "Restricted license${modelData.license" in HWFIT_JS
    # The license line is rendered inside _expandModelRow — i.e. in the panel
    # header above the Download/Run actions, not appended after them.
    panel_fn = HWFIT_JS.split("export function _expandModelRow")[1]
    assert "hwfit-access-open" in panel_fn.split("hwfit-panel-actions")[0]


def test_unknown_access_never_guessed_in_ui():
    # Rendering must be conditional on the two known classes only — there is
    # no else-branch that fabricates a badge for unknown metadata.
    badge_block = HWFIT_JS.split("let accessBadge = '';")[1].split("const imgBadge")[0]
    assert "else {" not in badge_block.replace("} else if (m.access === 'restricted') {", "")


# ── use-case glyphs + persistence ──────────────────────────────────────

def test_glyphs_exist_for_new_model_types():
    glyphs_block = HWFIT_JS.split("_HWFIT_USECASE_GLYPHS = {")[1].split("};")[0]
    for key in ("general", "coding", "multimodal", "reasoning", "chat", "image_gen"):
        assert f"{key}:" in glyphs_block, f"missing glyph for {key}"


def test_filter_picks_persist_across_reloads():
    assert "hwfit_filters_v1" in HWFIT_JS
    assert "localStorage.setItem('hwfit_filters_v1'" in HWFIT_JS
    assert "localStorage.getItem('hwfit_filters_v1'" in HWFIT_JS
    # Restored values are validated against real options (no phantom picks).
    assert "Array.from(sel.options).some((o) => o.value === val)" in HWFIT_JS


# ── styles ─────────────────────────────────────────────────────────────

def test_badge_styles_exist():
    assert ".hwfit-access-open" in STYLE_CSS
    assert ".hwfit-access-restricted" in STYLE_CSS
    open_block = STYLE_CSS.split(".hwfit-access-open {")[1].split("}")[0]
    assert "var(--green" in open_block
