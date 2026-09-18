"""Jarvis voice agent — planner parsing, rule fallback and spoken output.

No network, no model, no microphone: the planner helpers are pure functions,
and the rule parser is what keeps voice control working when the model is
unreachable.
"""

import pytest

from services.jarvis.prompts import JARVIS_SYSTEM, spoken_text
from services.jarvis.service import extract_plan, heuristic_plan


# ---------------------------------------------------------------------------
# JSON extraction — small local models are messy
# ---------------------------------------------------------------------------


def test_extract_plan_reads_plain_json():
    plan = extract_plan('{"say": "Right away, sir.", "action": null}')
    assert plan == {"say": "Right away, sir.", "action": None}


def test_extract_plan_skips_a_prose_preamble_and_fence():
    raw = (
        "Sure! Here you go:\n\n```json\n"
        '{"say": "Opening Notepad, sir.", '
        '"action": {"name": "open", "params": {"target": "notepad"}}}\n'
        "```\n\nLet me know if you need anything else."
    )
    plan = extract_plan(raw)
    assert plan["action"]["name"] == "open"
    assert plan["action"]["params"]["target"] == "notepad"


def test_extract_plan_tolerates_a_trailing_comma():
    plan = extract_plan('{"say": "hi", "action": null,}')
    assert plan is not None
    assert plan["action"] is None


def test_extract_plan_returns_none_for_prose():
    assert extract_plan("I am afraid I cannot do that, sir.") is None
    assert extract_plan("") is None


def test_extract_plan_handles_braces_inside_strings():
    raw = '{"say": "Use {braces} carefully", "action": {"name": "wait", "params": {"seconds": 2}}}'
    plan = extract_plan(raw)
    assert plan["action"]["params"]["seconds"] == 2


# ---------------------------------------------------------------------------
# Rule-based planner (works with no model at all)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "utterance,action,key",
    [
        ("open notepad", "open", "target"),
        ("open calculator", "open", "target"),
        ("launch chrome", "open", "target"),
        ("open https://example.com", "open", "target"),
        ("take a screenshot", "screenshot", None),
        ("volume up", "volume", "direction"),
        ("turn the volume down", "volume", "direction"),
        ("mute", "volume", "mute"),
        ("set volume to 40 percent", "volume", "level"),
        ("type hello world", "type", "text"),
        ("press ctrl+s", "key", "combo"),
        ("lock the pc", "key", "combo"),
        ("alt tab", "key", "combo"),
    ],
)
def test_heuristic_plan_recognises_common_commands(utterance, action, key):
    plan = heuristic_plan(utterance)
    assert plan is not None, utterance
    assert plan["action"]["name"] == action
    if key:
        assert key in plan["action"]["params"]
    # Whatever it says, it says in English and it says something.
    assert plan["say"].strip()


def test_heuristic_plan_returns_none_rather_than_guessing():
    for utterance in (
        "what is the meaning of life",
        "tell me about the history of Rome",
        "how are you today",
    ):
        assert heuristic_plan(utterance) is None


def test_heuristic_plan_extracts_the_url():
    plan = heuristic_plan("open https://arena.ai/pricing please")
    assert plan["action"]["params"]["target"] == "https://arena.ai/pricing"


def test_heuristic_plan_closes_a_named_window():
    plan = heuristic_plan("close notepad")
    assert plan["action"]["name"] == "close_window"
    assert "notepad" in plan["action"]["params"]["title"]


def test_heuristic_plan_falls_back_to_alt_f4_without_a_name():
    plan = heuristic_plan("close the window")
    assert plan["action"] == {"name": "key", "params": {"combo": "alt+f4"}}


def test_heuristic_plan_understands_hinglish():
    assert heuristic_plan("notepad kholo")["action"]["name"] == "open"
    assert heuristic_plan("volume badhao")["action"]["params"]["direction"] == "up"
    assert heuristic_plan("volume kam karo")["action"]["params"]["direction"] == "down"


# ---------------------------------------------------------------------------
# Spoken output
# ---------------------------------------------------------------------------


def test_spoken_text_strips_markdown_and_emoji():
    out = spoken_text("**Bold** and `code`\n\n- one\n- two\n\n🎉")
    assert "*" not in out and "`" not in out
    assert "one" in out and "two" in out
    assert "🎉" not in out


def test_spoken_text_keeps_link_labels_and_drops_urls():
    out = spoken_text("See [the docs](https://example.com/x) for more.")
    assert "the docs" in out
    assert "https://" not in out


def test_spoken_text_collapses_whitespace():
    assert spoken_text("  lots   of\n\nspace  ") == "lots of space"


def test_spoken_text_handles_empty_input():
    assert spoken_text("") == ""
    assert spoken_text(None) == ""


def test_persona_requires_english_only():
    lowered = JARVIS_SYSTEM.lower()
    assert "always reply in english" in lowered
    assert "text-to-speech" in lowered
    assert "no markdown" in lowered
