"""Policy gates for the desktop (computer) control surface.

These tests assert what psd.ai must NEVER do to the host machine, even though
the owner granted full access: wiping a drive, killing the engine that is
serving the request, or inventing an action outside the allow-list.
"""

import pytest

from services.computer import policy


@pytest.mark.parametrize(
    "target",
    [
        "format c:",
        "diskpart",
        "shutdown /s /t 0",
        "rm -rf /",
        "rm -rf /home",
        "del /f /s C:\\Windows",
        "bcdedit /set safeboot minimal",
        "reg delete HKLM\\Software",
        "dd if=/dev/zero of=/dev/sda",
        "C:\\Windows\\System32",
        "/etc/passwd",
    ],
)
def test_destructive_targets_are_denied(target):
    assert policy.is_denied_target(target) is True


@pytest.mark.parametrize(
    "target",
    ["notepad", "https://example.com", "C:\\Users\\me\\notes.txt", "code", "spotify.exe"],
)
def test_ordinary_targets_are_allowed(target):
    assert policy.is_denied_target(target) is False


@pytest.mark.parametrize("action", ["click", "type", "open", "volume", "screenshot"])
def test_allow_listed_actions_pass(action):
    allowed, reason, _risk, needs = policy.check_action(action, {})
    assert allowed is True
    assert reason == ""
    assert needs is False


def test_unknown_action_is_refused_with_the_allow_list():
    allowed, reason, _risk, _needs = policy.check_action("teleport", {})
    assert allowed is False
    assert "Unknown computer action" in reason
    assert "click" in reason


def test_kill_is_refused_for_protected_processes():
    for name in ("python.exe", "pythonw", "explorer.exe", "psd-ai-desktop.exe"):
        allowed, reason, _risk, _needs = policy.check_action("kill", {"name": name})
        assert allowed is False
        assert "protected" in reason.lower()


def test_kill_is_refused_for_own_pid():
    import os

    allowed, reason, _risk, _needs = policy.check_action("kill", {"pid": os.getpid()})
    assert allowed is False


def test_kill_of_an_ordinary_app_is_allowed():
    allowed, _reason, risk, _needs = policy.check_action("kill", {"name": "notepad.exe"})
    assert allowed is True
    assert risk is policy.ActionRisk.RISKY


def test_confirm_mode_asks_before_risky_actions():
    allowed, _reason, _risk, needs = policy.check_action(
        "kill", {"name": "notepad.exe"}, confirm_risky=True
    )
    assert allowed is True
    assert needs is True

    allowed, _reason, _risk, needs = policy.check_action(
        "open", {"target": "notepad"}, confirm_risky=True
    )
    assert allowed is True
    assert needs is False


def test_risk_classes_are_partitioned():
    assert policy.READ_ONLY_ACTIONS & policy.WRITE_ACTIONS == set()
    assert policy.READ_ONLY_ACTIONS & policy.RISKY_ACTIONS == set()
    assert policy.WRITE_ACTIONS & policy.RISKY_ACTIONS == set()
    assert (
        policy.READ_ONLY_ACTIONS | policy.WRITE_ACTIONS | policy.RISKY_ACTIONS
        == policy.ALL_ACTIONS
    )


def test_screenshot_and_read_only_actions_are_read_only():
    for action in ("screenshot", "windows", "info", "processes", "clipboard_get"):
        assert policy.action_risk(action) is policy.ActionRisk.READ_ONLY


def test_describe_policy_is_json_shaped():
    doc = policy.describe_policy()
    assert set(doc) >= {"read_only", "write", "risky", "all", "platform"}
    assert "kill" in doc["risky"]
    assert "open" in doc["write"]
