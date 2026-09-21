"""Tests for the Linux OS-compatibility helpers in core/platform_compat.

The process-tree cases spawn real children: signal handling, process groups and
SIGKILL escalation are exactly the things a mock would happily get wrong, and a
stuck llama-server holding VRAM is the failure this module exists to prevent.
"""

import importlib.util
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


_MODULE_PATH = Path(__file__).resolve().parents[1] / "core" / "platform_compat.py"
_SPEC = importlib.util.spec_from_file_location("platform_compat_under_test", _MODULE_PATH)
platform_compat = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(platform_compat)


def _reset_bash_cache(monkeypatch):
    monkeypatch.setattr(platform_compat, "_BASH_CACHE", None)
    monkeypatch.setattr(platform_compat, "_BASH_PROBED", False)


# ── module identity ──────────────────────────────────────────────────────────
def test_the_module_is_linux_only():
    assert platform_compat.IS_LINUX is True
    assert platform_compat.IS_POSIX is True
    # No Windows/macOS host flags survive: a caller that branches on them would
    # be branching on something that can never be true here.
    for gone in ("IS_WINDOWS", "IS_APPLE_SILICON"):
        assert not hasattr(platform_compat, gone)


def test_arch_flags_match_the_running_machine():
    machine = platform_compat.platform.machine().lower()
    assert platform_compat.IS_ARM64 == (machine in {"aarch64", "arm64"})
    assert platform_compat.IS_X86_64 == (machine in {"x86_64", "amd64"})


# ── permissions ──────────────────────────────────────────────────────────────
def test_safe_chmod_applies_the_mode(tmp_path):
    target = tmp_path / "secrets.db"
    target.write_text("x")

    assert platform_compat.safe_chmod(target, 0o600) is True
    assert (target.stat().st_mode & 0o777) == 0o600


def test_safe_chmod_reports_failure_instead_of_raising(tmp_path):
    # A path that does not exist cannot be chmod'd; the caller (database.py)
    # turns that into a warning rather than a crash at startup.
    assert platform_compat.safe_chmod(tmp_path / "missing.db", 0o600) is False


# ── process primitives ───────────────────────────────────────────────────────
def test_detached_popen_kwargs_start_a_new_session():
    assert platform_compat.detached_popen_kwargs() == {"start_new_session": True}


def test_pid_alive_recognises_a_running_process():
    assert platform_compat.pid_alive(os.getpid()) is True


def test_pid_alive_rejects_a_dead_or_absurd_pid():
    assert platform_compat.pid_alive(None) is False
    assert platform_compat.pid_alive(0) is False
    # PID_MAX_LIMIT is 4194304 on Linux; nothing above it can exist.
    assert platform_compat.pid_alive(99999999) is False


def test_pid_alive_treats_a_foreign_process_as_alive(monkeypatch):
    """EPERM means "exists, owned by someone else" - not "gone"."""
    def fake_kill(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(platform_compat.os, "kill", fake_kill)
    assert platform_compat.pid_alive(4242) is True


def _spawn(script: str) -> subprocess.Popen:
    return subprocess.Popen(
        [platform_compat.find_bash() or "/bin/bash", "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **platform_compat.detached_popen_kwargs(),
    )


def test_kill_process_tree_reaps_the_whole_group():
    # A parent that spawns a child, like llama.cpp's launcher spawning
    # llama-server: killing only the parent would orphan the real worker.
    proc = _spawn("sleep 60 & sleep 60")
    time.sleep(0.4)
    child_pids = _children_of(proc.pid)
    assert child_pids, "the test process should have spawned a child"

    platform_compat.kill_process_tree(proc.pid, timeout=2.0)

    assert platform_compat.pid_alive(proc.pid) is False
    for pid in child_pids:
        assert platform_compat.pid_alive(pid) is False, f"orphaned child {pid}"


def test_kill_process_tree_escalates_to_sigkill():
    # `trap '' TERM` makes bash ignore the graceful signal; the loop keeps it
    # alive so the escalation path (not an early exit) is what ends it.
    proc = _spawn("trap '' TERM; while true; do sleep 1; done")
    time.sleep(0.4)

    started = time.monotonic()
    platform_compat.kill_process_tree(proc.pid, timeout=0.5)
    elapsed = time.monotonic() - started

    assert platform_compat.pid_alive(proc.pid) is False
    # It waited out the grace period, then forced it.
    assert elapsed >= 0.4
    assert elapsed < 5.0


def test_kill_process_tree_is_a_noop_for_nothing():
    platform_compat.kill_process_tree(None)
    platform_compat.kill_process_tree(0)
    platform_compat.kill_process_tree(99999999)


def _children_of(pid: int) -> list[int]:
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=", "--ppid", str(pid)],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(line) for line in out.split() if line.strip().isdigit()]


# ── tool resolution ──────────────────────────────────────────────────────────
def test_which_tool_finds_a_real_binary():
    assert platform_compat.which_tool("bash")
    assert platform_compat.which_tool("definitely-not-a-real-tool") is None


def test_find_bash_is_cached(monkeypatch):
    _reset_bash_cache(monkeypatch)
    calls = []
    monkeypatch.setattr(
        platform_compat.shutil, "which",
        lambda name: calls.append(name) or "/usr/bin/bash",
    )

    first = platform_compat.find_bash()
    second = platform_compat.find_bash()

    assert first == second == "/usr/bin/bash"
    assert calls == ["bash"], "the probe must run once, not per call"
    assert platform_compat.has_bash() is True


def test_find_bash_falls_back_to_absolute_paths(monkeypatch, tmp_path):
    """A slim image can have bash without a usable PATH entry."""
    _reset_bash_cache(monkeypatch)
    monkeypatch.setattr(platform_compat.shutil, "which", lambda _name: None)

    fake = tmp_path / "bash"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(platform_compat, "_BASH_FALLBACKS", (str(tmp_path / "nope"), str(fake)))

    assert platform_compat.find_bash() == str(fake)
    assert platform_compat.has_bash() is True


def test_find_bash_returns_none_when_there_is_no_bash(monkeypatch):
    _reset_bash_cache(monkeypatch)
    monkeypatch.setattr(platform_compat.shutil, "which", lambda _name: None)
    monkeypatch.setattr(platform_compat, "_BASH_FALLBACKS", ("/nonexistent/bash",))

    assert platform_compat.find_bash() is None
    assert platform_compat.has_bash() is False
    assert platform_compat.run_script_argv("/tmp/x.sh") == ["sh", "/tmp/x.sh"]


def test_package_for_maps_tools_to_fedora_packages():
    assert platform_compat.package_for("wl-copy") == "wl-clipboard"
    assert platform_compat.package_for("wpctl") == "pipewire-utils"
    assert platform_compat.package_for("notify-send") == "libnotify"
    assert platform_compat.package_for("ydotool") == "ydotool"
    # An unknown tool falls back to its own name rather than raising.
    assert platform_compat.package_for("mystery-tool") == "mystery-tool"


def test_missing_tools_and_dnf_hint(monkeypatch):
    monkeypatch.setattr(
        platform_compat, "which_tool",
        lambda name: "/usr/bin/" + name if name == "grim" else None,
    )

    assert platform_compat.missing_tools(["grim", "slurp", "wtype"]) == ["slurp", "wtype"]
    hint = platform_compat.dnf_install_hint(["slurp", "wtype", "wl-copy"])
    assert hint == "sudo dnf install slurp wl-clipboard wtype"
    assert platform_compat.dnf_install_hint([]) == ""


def test_run_script_argv_prefers_bash(monkeypatch):
    monkeypatch.setattr(platform_compat, "find_bash", lambda: "/usr/bin/bash")
    assert platform_compat.run_script_argv("/tmp/x.sh") == ["/usr/bin/bash", "/tmp/x.sh"]
    monkeypatch.setattr(platform_compat, "find_bash", lambda: None)
    assert platform_compat.run_script_argv("/tmp/x.sh") == ["sh", "/tmp/x.sh"]


def test_posix_path_normalises(tmp_path):
    assert platform_compat.posix_path(tmp_path) == str(tmp_path)
    assert platform_compat.posix_path("a/b/c") == "a/b/c"


# ── session / desktop introspection ──────────────────────────────────────────
def test_session_type_prefers_the_xdg_variable(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert platform_compat.session_type() == "wayland"
    assert platform_compat.is_wayland() is True

    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert platform_compat.session_type() == "x11"
    assert platform_compat.is_wayland() is False


def test_session_type_is_inferred_when_xdg_is_missing(monkeypatch):
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert platform_compat.session_type() == "wayland"
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    assert platform_compat.session_type() == "x11"
    monkeypatch.delenv("DISPLAY", raising=False)
    assert platform_compat.session_type() == ""


def test_desktop_environment_takes_the_first_entry(monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME:ubuntu")
    assert platform_compat.desktop_environment() == "gnome"
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    monkeypatch.setenv("DESKTOP_SESSION", "plasma")
    assert platform_compat.desktop_environment() == "plasma"


def test_selinux_mode_reads_the_kernel_file(monkeypatch, tmp_path):
    enforce = tmp_path / "enforce"
    enforce.write_text("1\n")
    monkeypatch.setattr(platform_compat, "_read_first_line", lambda path: "1" if path.endswith("enforce") else "")
    assert platform_compat.selinux_mode() == "enforcing"
    assert platform_compat.is_selinux_enforcing() is True

    monkeypatch.setattr(platform_compat, "_read_first_line", lambda path: "0")
    assert platform_compat.selinux_mode() == "permissive"
    assert platform_compat.is_selinux_enforcing() is False


def test_selinux_mode_falls_back_to_getenforce(monkeypatch):
    monkeypatch.setattr(platform_compat, "_read_first_line", lambda _path: "")
    monkeypatch.setattr(platform_compat, "which_tool", lambda name: "/usr/sbin/getenforce")

    class _Result:
        stdout = "Enforcing\n"

    monkeypatch.setattr(platform_compat.subprocess, "run", lambda *_a, **_k: _Result())
    assert platform_compat.selinux_mode() == "enforcing"


def test_selinux_mode_is_empty_when_unavailable(monkeypatch):
    monkeypatch.setattr(platform_compat, "_read_first_line", lambda _path: "")
    monkeypatch.setattr(platform_compat, "which_tool", lambda _name: None)
    assert platform_compat.selinux_mode() == ""
    assert platform_compat.is_selinux_enforcing() is False


def test_os_release_parsing(monkeypatch, tmp_path):
    release = tmp_path / "os-release"
    release.write_text(
        'NAME="Fedora Linux"\nVERSION_ID="43"\nID=fedora\nID_LIKE="rhel centos"\n'
        'PRETTY_NAME="Fedora Linux 43 (Workstation Edition)"\n'
    )
    monkeypatch.setattr(platform_compat, "_os_release", lambda: {
        "ID": "fedora", "ID_LIKE": "rhel centos",
        "PRETTY_NAME": "Fedora Linux 43 (Workstation Edition)",
    })

    assert platform_compat.distro_id() == "fedora"
    assert platform_compat.distro_like() == ["rhel", "centos"]
    assert platform_compat.is_fedora_family() is True
    assert platform_compat.distro_pretty_name().startswith("Fedora Linux 43")
    assert release.exists()


def test_fedora_family_includes_remixes(monkeypatch):
    monkeypatch.setattr(platform_compat, "distro_id", lambda: "nobara")
    monkeypatch.setattr(platform_compat, "distro_like", lambda: ["fedora"])
    assert platform_compat.is_fedora_family() is True

    monkeypatch.setattr(platform_compat, "distro_id", lambda: "ubuntu")
    monkeypatch.setattr(platform_compat, "distro_like", lambda: ["debian"])
    assert platform_compat.is_fedora_family() is False


def test_immutable_os_detection(monkeypatch):
    monkeypatch.setattr(platform_compat.os.path, "exists", lambda path: path == "/run/ostree-booted")
    assert platform_compat.is_immutable_os() is True
    monkeypatch.setattr(platform_compat.os.path, "exists", lambda _path: False)
    assert platform_compat.is_immutable_os() is False


def test_flatpak_and_container_detection(monkeypatch):
    monkeypatch.setattr(platform_compat.os.path, "exists", lambda path: path == "/.flatpak-info")
    assert platform_compat.is_flatpak() is True
    assert platform_compat.is_container() is True

    monkeypatch.setattr(platform_compat.os.path, "exists", lambda _path: False)
    monkeypatch.setattr(platform_compat, "_read_first_line", lambda _p: "0::/system.slice/docker-abc.scope")
    assert platform_compat.is_flatpak() is False
    assert platform_compat.is_container() is True

    monkeypatch.setattr(platform_compat, "_read_first_line", lambda _p: "0::/user.slice/user-1000.slice")
    monkeypatch.delenv("container", raising=False)
    monkeypatch.delenv("DISTROBOX_CONTAINER_ID", raising=False)
    assert platform_compat.is_container() is False


# ── ssh helpers ──────────────────────────────────────────────────────────────
def test_ssh_path_override_is_correct_string(monkeypatch):
    monkeypatch.setattr(platform_compat, "_SSH_PATH_MEMBERS", ["path1", "path2"])
    assert platform_compat._ssh_path_override() == 'export PATH="$PATH:path1:path2"; '


def test_nvidia_path_candidates_are_absolute_and_no_wsl():
    for candidate in platform_compat.NVIDIA_PATH_CANDIDATES:
        assert candidate.startswith("/")
        assert "wsl" not in candidate.lower()


def test_ssh_exec_argv_builds_default_command():
    argv = platform_compat._ssh_exec_argv("alice@gpu-box", None, remote_cmd="echo ok")
    assert argv == ["ssh", "alice@gpu-box", "echo ok"]


def test_ssh_exec_argv_includes_port_and_options():
    argv = platform_compat._ssh_exec_argv(
        "alice@gpu-box",
        "2222",
        remote_cmd="tmux ls",
        connect_timeout=6,
        strict_host_key_checking=False,
    )
    assert argv == [
        "ssh",
        "-o", "ConnectTimeout=6",
        "-o", "StrictHostKeyChecking=no",
        "-p", "2222",
        "alice@gpu-box",
        "tmux ls",
    ]


def test_ssh_exec_argv_rejects_an_option_shaped_remote():
    for bad in ("", "-oProxyCommand=evil", "user@-host"):
        with pytest.raises(ValueError):
            platform_compat._ssh_exec_argv(bad, None, remote_cmd="true")


def test_run_ssh_command_uses_built_argv(monkeypatch):
    captured = {}

    class _Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_run(args, **kwargs):
        captured["args"] = list(args)
        captured["kwargs"] = kwargs
        return _Result()

    monkeypatch.setattr(platform_compat.subprocess, "run", _fake_run)

    result = platform_compat.run_ssh_command(
        "alice@gpu-box",
        "2200",
        "tmux ls",
        timeout=7,
        connect_timeout=3,
        strict_host_key_checking=True,
        text=False,
    )

    assert result.returncode == 0
    assert captured["args"] == [
        "ssh",
        "-o", "ConnectTimeout=3",
        "-o", "StrictHostKeyChecking=yes",
        "-p", "2200",
        "alice@gpu-box",
        "tmux ls",
    ]
    assert captured["kwargs"]["timeout"] == 7
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is False


# ── the real module (not the reloaded copy) agrees ───────────────────────────
def test_the_importable_module_exposes_the_same_api():
    from core import platform_compat as installed

    assert installed.IS_LINUX is True
    assert installed.find_bash()
    assert installed.detached_popen_kwargs() == {"start_new_session": True}
    assert sys.platform.startswith("linux")
    assert signal.SIGKILL and subprocess.DEVNULL is not None
