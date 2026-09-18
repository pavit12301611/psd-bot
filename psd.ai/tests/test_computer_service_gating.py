"""ComputerService gates — nothing may touch a device from a test.

Every case here stops before platform code runs: switched off, unknown action,
denied target, or a risky action waiting for confirmation. That is the point —
the suite must be safe to run on a developer's own desktop.
"""

import pytest

from services.computer import service as computer_module
from services.computer.service import ComputerService


@pytest.fixture
def disabled(monkeypatch):
    monkeypatch.setattr(computer_module, "computer_control_enabled", lambda: False)
    return ComputerService()


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(computer_module, "computer_control_enabled", lambda: True)
    return ComputerService()


@pytest.mark.asyncio
async def test_actions_are_refused_when_switched_off(disabled):
    result = await disabled.act("click", {"x": 10, "y": 10})
    assert result["ok"] is False
    assert result["blocked"] is True
    assert "switched off" in result["error"]


@pytest.mark.asyncio
async def test_unknown_action_never_reaches_a_handler(enabled):
    result = await enabled.act("teleport", {})
    assert result["ok"] is False
    assert result["blocked"] is True
    assert "Unknown computer action" in result["error"]


@pytest.mark.asyncio
async def test_denied_target_is_refused_before_launching(enabled):
    result = await enabled.act("open", {"target": "format c:"})
    assert result["ok"] is False
    assert "never-run list" in result["error"]


@pytest.mark.asyncio
async def test_risky_action_waits_for_confirmation(enabled, monkeypatch):
    monkeypatch.setattr(computer_module, "_confirm_risky", lambda: True)
    result = await enabled.act("kill", {"name": "notepad.exe"})
    assert result["ok"] is False
    assert result["needs_confirmation"] is True
    assert result["confirm_hint"]


@pytest.mark.asyncio
async def test_confirmed_risky_action_proceeds_to_the_handler(enabled, monkeypatch):
    """`confirm=True` clears the gate — the handler is reached and its error
    is reported rather than the confirmation prompt."""

    seen = {}

    def fake_kill(params):
        seen.update(params)
        raise RuntimeError("nope")

    monkeypatch.setattr(computer_module, "_confirm_risky", lambda: True)
    monkeypatch.setitem(computer_module._HANDLERS, "kill", fake_kill)

    result = await enabled.act("kill", {"name": "notepad.exe", "confirm": True})
    assert result["ok"] is False
    assert "nope" in result["error"]
    # The synthetic `confirm` flag must not leak into the action params.
    assert "confirm" not in seen


def test_status_reports_capabilities_without_touching_devices(enabled):
    status = enabled.status()
    assert status["enabled"] is True
    assert isinstance(status["supports_screenshot"], bool)
    assert isinstance(status["supports_input"], bool)
    assert set(status["policy"]["all"]) == set(
        status["policy"]["read_only"]
        + status["policy"]["write"]
        + status["policy"]["risky"]
    )


def test_status_never_raises_when_packages_are_missing(monkeypatch):
    monkeypatch.setattr(computer_module, "_have", lambda _name: False)
    status = ComputerService().status()
    assert "backends" in status
    assert "native" in status
