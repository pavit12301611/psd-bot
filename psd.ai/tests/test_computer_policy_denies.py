"""Policy gates for the desktop (computer) control surface.

These tests assert what psd.ai must NEVER do to the host machine, even though
the owner granted full access: wiping a filesystem, killing the engine that is
serving the request, dropping SELinux, or inventing an action outside the
allow-list.

The targets are Fedora-shaped: that is the platform the app ships for, and a
deny list written for another OS would block nothing here.
"""

import pytest

from services.computer import policy


@pytest.mark.parametrize(
    "target",
    [
        # Filesystems and partitions.
        "mkfs.ext4 /dev/sda1",
        "wipefs -a /dev/nvme0n1",
        "dd if=/dev/zero of=/dev/sda",
        "dd of=/dev/nvme0n1",
        "shred /dev/sda",
        "parted /dev/sda rm 1",
        "sgdisk --zap-all /dev/sda",
        "cryptsetup luksFormat /dev/sda2",
        "lvremove -y /dev/fedora/root",
        # Recursive deletion of something that is not a project directory.
        "rm -rf /",
        "rm -rf /*",
        "rm -rf ~",
        "rm -rf /home",
        "rm -fr /usr",
        "find / -delete",
        # Power state.
        "shutdown -h now",
        "systemctl poweroff",
        "systemctl reboot",
        "reboot",
        "init 0",
        "loginctl terminate-user me",
        # Boot loader, kernel and firmware.
        "grubby --update-kernel=ALL --args=nosmt",
        "grub2-install /dev/sda",
        "grub2-mkconfig -o /boot/grub2/grub.cfg",
        "efibootmgr -b 0001 -B",
        "dnf remove kernel-core",
        "rpm -e kernel",
        "fwupdmgr update",
        # Security downgrade.
        "setenforce 0",
        "chmod -R 777 /",
        "chown -R nobody /",
        "chmod -R 000 /etc",
        "chown -R root:root /usr",
        # System paths through `open`.
        "/etc/passwd",
        "/boot/efi",
        "/usr/bin",
        "/var/lib",
        "/proc/1",
    ],
)
def test_destructive_targets_are_denied(target):
    assert policy.is_denied_target(target) is True


@pytest.mark.parametrize(
    "target",
    [
        "gedit",
        "nautilus",
        "gnome-terminal",
        "firefox",
        "org.gnome.TextEditor",
        "https://example.com",
        "/home/me/notes.txt",
        "~/Projects/psd-bot",
        "spotify",
        "code",
        "libreoffice --writer",
    ],
)
def test_ordinary_targets_are_allowed(target):
    assert policy.is_denied_target(target) is False


def test_a_project_directory_under_usr_is_not_confused_with_a_system_path():
    """/usr is denied, but a user's own tree never is."""
    assert policy.is_denied_target("/usr/local/bin") is True
    assert policy.is_denied_target("/home/me/usr/notes") is False


@pytest.mark.parametrize(
    "target",
    [
        "chmod -R 755 /home/me/Projects/psd-bot",
        "chown -R me:me ~/Projects/psd-bot",
        "chmod 644 /home/me/notes.txt",
    ],
)
def test_recursive_permission_changes_in_a_user_tree_are_allowed(target):
    """Only machine-wide recursive chmod/chown is denied; a project tree is
    the owner's own business."""
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


@pytest.mark.parametrize(
    "name",
    [
        "python",
        "python3",
        "uvicorn",
        "psd-ai-desktop",
        "llama-server",
        "systemd",
        "gnome-shell",
        "plasmashell",
        "kwin_wayland",
        "Xorg",
        "Xwayland",
        "pipewire",
        "pipewire-pulse",
        "wireplumber",
        "gdm-wayland-session",
        "NetworkManager",
        "sshd",
        "ydotoold",
        "dbus-broker",
    ],
)
def test_kill_is_refused_for_protected_processes(name):
    allowed, reason, _risk, _needs = policy.check_action("kill", {"name": name})
    assert allowed is False
    assert "protected" in reason.lower()


def test_kill_is_refused_for_own_pid():
    import os

    allowed, reason, _risk, _needs = policy.check_action("kill", {"pid": os.getpid()})
    assert allowed is False


@pytest.mark.parametrize("victim", ["firefox", "gnome-text-editor", "steam", "spotify"])
def test_kill_of_an_ordinary_app_is_allowed(victim):
    allowed, _reason, risk, _needs = policy.check_action("kill", {"name": victim})
    assert allowed is True
    assert risk is policy.ActionRisk.RISKY


def test_confirm_mode_asks_before_risky_actions():
    allowed, _reason, _risk, needs = policy.check_action(
        "kill", {"name": "firefox"}, confirm_risky=True
    )
    assert allowed is True
    assert needs is True

    allowed, _reason, _risk, needs = policy.check_action(
        "open", {"target": "gedit"}, confirm_risky=True
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


def test_describe_policy_reports_a_fedora_host(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    doc = policy.describe_policy()

    assert doc["os"] == "linux"
    assert doc["session"] == "wayland"
    assert doc["package_manager"] == "dnf"
    # The Windows-only report key is gone: a UI that branched on it would be
    # branching on something that can never be true.
    assert "windows" not in doc


def test_describe_policy_infers_the_session_without_the_xdg_variable(monkeypatch):
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert policy.describe_policy()["session"] == "wayland"

    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    assert policy.describe_policy()["session"] == "x11"


def test_no_windows_only_denies_remain():
    """The deny list is what actually protects the machine; stale entries for
    another OS would give false confidence without blocking anything."""
    joined = " ".join(policy._DENY_SUBSTRINGS).lower()
    for windows_only in ("diskpart", "bcdedit", "reg delete", "vssadmin", "wmic", "icacls"):
        assert windows_only not in joined
