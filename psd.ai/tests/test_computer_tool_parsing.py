"""The agent-facing computer tool: argument shapes the model actually emits."""

import pytest

from src.agent_tools.computer_tools import _parse_content


def test_json_payload():
    parsed = _parse_content('{"action": "open", "params": {"target": "notepad"}}')
    assert parsed == {"action": "open", "params": {"target": "notepad"}}


def test_flat_json_payload_without_a_params_key():
    parsed = _parse_content('{"action": "type", "text": "hello"}')
    assert parsed["action"] == "type"
    assert parsed["params"]["text"] == "hello"


def test_bare_text_payload():
    assert _parse_content("open notepad")["params"]["target"] == "notepad"
    assert _parse_content("type hello there")["params"]["text"] == "hello there"
    assert _parse_content("press ctrl+s")["params"]["combo"] == "ctrl+s"
    assert _parse_content("volume 30")["params"]["level"] == 30
    assert _parse_content("volume down")["params"]["direction"] == "down"


def test_click_coordinates():
    parsed = _parse_content('{"action": "click", "params": {"x": 120, "y": 340}}')
    assert parsed["params"] == {"x": 120, "y": 340}
    assert _parse_content("click 120 340")["params"] == {"x": 120, "y": 340}


def test_empty_content_yields_nothing():
    assert _parse_content("") == {}
    assert _parse_content("   ") == {}


def test_malformed_json_is_reported_not_guessed():
    """A broken JSON object must not be salvaged into a random action."""
    parsed = _parse_content('{"action": "open"')
    assert parsed["action"] == ""
    assert parsed.get("malformed") is True


@pytest.mark.asyncio
async def test_tool_reports_a_usable_error_when_the_action_is_missing():
    from src.agent_tools.computer_tools import ComputerControlTool

    result = await ComputerControlTool().execute("", {})
    assert result["exit_code"] == 1
    assert "computer_control needs JSON" in result["error"]
