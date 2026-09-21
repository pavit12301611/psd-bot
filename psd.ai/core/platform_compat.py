"""Linux OS compatibility helpers.

psd.ai is a Linux application: it is developed and shipped against Fedora
Workstation, and everything that touches the operating system goes through this
module so the rest of the codebase stays free of ``sys.platform`` checks and
inline shell quoting.

The helpers fall into three groups:

  * **POSIX process and file primitives** — detach, liveness, teardown,
    permission bits. These are the calls that a web framework does not provide
    and that every caller used to reinvent slightly differently.
  * **Tool resolution** — ``which_tool``/``find_bash``, cached, so a missing
    binary is reported once with a ``dnf`` hint instead of raising from deep
    inside a request handler.
  * **Session introspection** — Wayland vs X11, the desktop environment,
    SELinux mode, Flatpak/toolbox confinement, and whether the OS is an
    immutable (rpm-ostree) Fedora. Desktop automation behaves very differently
    across those, so it is asked about here rather than guessed at per call
    site.

Design rules:
  * Stdlib only — no new third-party deps (no psutil).
  * Nothing in here raises for a missing tool or an unreadable ``/proc`` file:
    callers get ``None``/``False`` and decide how to degrade.
"""

from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

IS_LINUX = sys.platform.startswith("linux")
IS_POSIX = os.name == "posix"

# Kept as module constants because a few callers still branch on the shape of
# the machine (ARM64 Fedora on a GB10/Strix Halo box plans models differently
# from x86_64).
IS_ARM64 = platform.machine().lower() in {"aarch64", "arm64"}
IS_X86_64 = platform.machine().lower() in {"x86_64", "amd64"}


# ── File permissions ────────────────────────────────────────────────────────
def safe_chmod(path, mode: int) -> bool:
    """``os.chmod`` that never raises.

    Used to lock secret/key files and the SQLite database down to 0o600.
    Returns True when the mode was actually applied — a filesystem that does
    not support permission bits (some FUSE mounts, a Windows share mounted
    under /mnt) reports False so the caller can warn instead of silently
    believing the file is private.
    """
    try:
        os.chmod(path, mode)
        return True
    except (OSError, NotImplementedError):
        return False


# ── Process detach / liveness / teardown ────────────────────────────────────
def detached_popen_kwargs() -> dict:
    """Keyword args for :class:`subprocess.Popen` that fully detach a child so
    it outlives the request/stream that launched it.

    ``start_new_session=True`` is setsid: the child gets its own session and
    process group, so it survives the caller's terminal closing and — more
    importantly for this codebase — the whole tree can later be signalled with
    :func:`kill_process_tree`.
    """
    return {"start_new_session": True}


def pid_alive(pid: Optional[int]) -> bool:
    """True if a process with ``pid`` is currently running.

    ``os.kill(pid, 0)`` is the POSIX liveness probe: it performs the permission
    and existence checks without delivering a signal. ESRCH means "no such
    process"; EPERM means it exists but belongs to someone else, which still
    counts as alive.

    A signal check alone is not enough on Linux: a child nobody has reaped
    stays in the process table as a zombie, keeps its PID, and answers
    ``kill(pid, 0)`` happily. Every long-lived child this app launches is a
    ``Popen`` object that outlives the request which started it, so treating a
    zombie as alive would report a finished background job (or a stopped model
    server) as running forever. :func:`_is_zombie` closes that hole.
    """
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    """True when ``pid`` has exited but has not been waited for yet."""
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="ignore") as handle:
            data = handle.read()
    except OSError:
        # No procfs (or the pid vanished between the two calls): not a zombie.
        return False
    # Field 3 is the state character. Field 2 (comm) is parenthesised and may
    # itself contain spaces and parentheses — "sleep (weird name)" — so split
    # after the LAST ')' rather than on whitespace.
    tail = data.rsplit(")", 1)[-1].split()
    return bool(tail) and tail[0] == "Z"


def kill_process_tree(pid: Optional[int], timeout: float = 3.0) -> None:
    """Terminate ``pid`` and all of its descendants.

    Children launched through :func:`detached_popen_kwargs` are process-group
    leaders, so ``killpg`` reaches the whole tree in one call — that is how a
    runaway ``llama-server`` (which itself forks) gets stopped. A group that
    ignores SIGTERM is escalated to SIGKILL after ``timeout`` seconds, because
    a stuck model server holding VRAM blocks the next launch.
    """
    if not pid:
        return
    pid = int(pid)
    try:
        group = os.getpgid(pid)
    except (OSError, ProcessLookupError):
        group = None

    if group is not None:
        try:
            os.killpg(group, signal.SIGTERM)
            _wait_for_exit(pid, timeout)
            if pid_alive(pid):
                os.killpg(group, signal.SIGKILL)
                # SIGKILL is delivered asynchronously: without this wait the
                # caller can go on to bind the port or start the next model
                # server while the old one is still holding it.
                _wait_for_exit(pid, 2.0)
            return
        except (OSError, ProcessLookupError):
            pass

    # Not a group leader (or the group call was refused): fall back to the
    # single process. Orphaned grandchildren are not reachable from here, which
    # is exactly why every long-lived child is started detached.
    try:
        os.kill(pid, signal.SIGTERM)
        _wait_for_exit(pid, timeout)
        if pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
            _wait_for_exit(pid, 2.0)
    except (OSError, ProcessLookupError):
        pass


def _wait_for_exit(pid: int, timeout: float) -> None:
    """Poll until ``pid`` is gone or ``timeout`` seconds have passed."""
    import time

    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.1)


# ── Shell / executable resolution ───────────────────────────────────────────
_BASH_CACHE: Optional[str] = None
_BASH_PROBED = False
# Probed when bash is not on PATH (a slim container, a stripped-down image).
_BASH_FALLBACKS = ("/usr/bin/bash", "/bin/bash", "/usr/local/bin/bash")

# Paths appended to remote SSH probe commands so tools that live outside the
# default non-interactive PATH (CUDA, vendor drivers) are still found.
_SSH_PATH_MEMBERS = (
    "/usr/bin",
    "/usr/local/bin",
    "/usr/local/cuda/bin",
    "/opt/cuda/bin",
)
# Fallback locations for nvidia-smi where the driver package does not put it on
# PATH (cuda-toolkit installs, /opt layouts, container images).
NVIDIA_PATH_CANDIDATES = (
    "/usr/bin/nvidia-smi",
    "/usr/local/bin/nvidia-smi",
    "/usr/local/cuda/bin/nvidia-smi",
    "/opt/cuda/bin/nvidia-smi",
)

# Fedora package names for the tools psd.ai can use. A missing tool is not an
# error — the code degrades — but every report should be able to say how to fix
# it, and "install bash" is not advice a user can act on.
TOOL_PACKAGES: Dict[str, str] = {
    "bash": "bash",
    "ffmpeg": "ffmpeg (RPM Fusion)",
    "ffprobe": "ffmpeg (RPM Fusion)",
    "git": "git",
    "tmux": "tmux",
    "notify-send": "libnotify",
    "xdg-open": "xdg-utils",
    "gio": "gvfs",
    "wl-copy": "wl-clipboard",
    "wl-paste": "wl-clipboard",
    "grim": "grim",
    "slurp": "slurp",
    "wtype": "wtype",
    "ydotool": "ydotool",
    "xdotool": "xdotool",
    "wmctrl": "wmctrl",
    "xclip": "xclip",
    "xprop": "xprop",
    "xrandr": "xrandr",
    "scrot": "scrot",
    "xsel": "xsel",
    "slurp": "slurp",
    "wlr-randr": "wlr-randr",
    "import": "ImageMagick",
    "gnome-screenshot": "gnome-screenshot",
    "gdbus": "glib2",
    "gtk-launch": "gtk3",
    "swaymsg": "sway",
    "hyprctl": "hyprctl",
    "wpctl": "pipewire-utils",
    "pactl": "pulseaudio-utils",
    "amixer": "alsa-utils",
    "arecord": "alsa-utils",
    "lspci": "pciutils",
    "vulkaninfo": "vulkan-tools",
    "nvidia-smi": "akmod-nvidia / cuda (RPM Fusion)",
    "dnf": "dnf",
    "rpm-ostree": "rpm-ostree",
    "getenforce": "libselinux-utils",
    "node": "nodejs",
    "npm": "npm",
    "cargo": "rust cargo",
}


def _ssh_path_override() -> str:
    """Build the PATH export snippet used for remote SSH shell probes."""
    return f"export PATH=\"$PATH:{':'.join(_SSH_PATH_MEMBERS)}\"; "


SSH_PATH_OVERRIDE = _ssh_path_override()


def which_tool(name: str) -> Optional[str]:
    """``shutil.which`` with a package hint on failure.

    Returns the absolute path of an executable on PATH, or None. Callers that
    want to tell the user how to install what is missing should pair this with
    :func:`package_for`.
    """
    return shutil.which(name)


def package_for(tool: str) -> str:
    """Fedora package that provides ``tool``, or a generic hint."""
    return TOOL_PACKAGES.get(tool, tool)


def missing_tools(names: Iterable[str]) -> List[str]:
    """The subset of ``names`` that is not on PATH."""
    return [name for name in names if not which_tool(name)]


def dnf_install_hint(tools: Sequence[str]) -> str:
    """A copy-pasteable ``dnf`` line for the missing tools in ``tools``."""
    packages = sorted({package_for(t).split(" (")[0] for t in tools})
    if not packages:
        return ""
    return f"sudo dnf install {' '.join(packages)}"


def find_bash() -> Optional[str]:
    """Locate ``bash``, or None.

    The agent ``bash`` tool, background jobs and Cookbook scripts all emit bash
    syntax (arrays, ``[[ ]]``, process substitution), so ``sh`` — which is dash
    on some spins and a restricted bash on others — is only a last resort. On
    Fedora bash is always present; the fallbacks exist for minimal containers.
    Result is cached.
    """
    global _BASH_CACHE, _BASH_PROBED
    if _BASH_PROBED:
        return _BASH_CACHE
    _BASH_PROBED = True
    found = which_tool("bash")
    if not found:
        for candidate in _BASH_FALLBACKS:
            if os.path.exists(candidate) and os.access(candidate, os.X_OK):
                found = candidate
                break
    _BASH_CACHE = found
    return found


def has_bash() -> bool:
    return find_bash() is not None


def run_script_argv(script_path) -> List[str]:
    """argv to execute a shell *script file*.

    Prefers bash so ``.sh`` wrappers run with the syntax they were written in,
    and falls back to ``sh`` in a minimal container.
    """
    bash = find_bash()
    if bash:
        return [bash, str(script_path)]
    return ["sh", str(script_path)]


def posix_path(path) -> str:
    """Normalise ``path`` to a POSIX string (identity on Linux, kept for the
    callers that used to convert Windows paths for a bundled bash)."""
    return Path(path).as_posix()


# ── Session / desktop introspection ─────────────────────────────────────────
def _read_first_line(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.readline().strip()
    except OSError:
        return ""


def _os_release() -> Dict[str, str]:
    """Parsed ``/etc/os-release`` (empty dict when unavailable)."""
    values: Dict[str, str] = {}
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    if "=" not in line:
                        continue
                    key, _, raw = line.partition("=")
                    values[key.strip()] = raw.strip().strip('"')
        except OSError:
            continue
        if values:
            break
    return values


def distro_id() -> str:
    """``fedora``, ``rhel``, ``debian``, ... (lower-case, empty if unknown)."""
    return _os_release().get("ID", "").lower()


def distro_like() -> List[str]:
    """``ID_LIKE`` entries, e.g. ``["fedora"]`` on a Nobara/Asahi remix."""
    raw = _os_release().get("ID_LIKE", "")
    return [item for item in raw.split() if item]


def is_fedora_family() -> bool:
    """True on Fedora and on the distros that share its package manager."""
    return distro_id() == "fedora" or "fedora" in distro_like() or "rhel" in distro_like()


def distro_pretty_name() -> str:
    return _os_release().get("PRETTY_NAME") or platform.platform()


def is_immutable_os() -> bool:
    """True on Fedora Atomic (Silverblue/Kinoite/Sericea) and friends.

    ``/usr`` is read-only there, so ``dnf install`` cannot work and packages
    must come from ``rpm-ostree`` or a toolbox/distrobox container. Installers
    check this before promising the user a dnf command that will fail.
    """
    return os.path.exists("/run/ostree-booted")


def is_flatpak() -> bool:
    """True when running inside a Flatpak sandbox (``/.flatpak-info``)."""
    return os.path.exists("/.flatpak-info")


def is_container() -> bool:
    """True inside Docker/Podman/toolbox — no desktop session, no PC control."""
    if os.path.exists("/run/.containerenv"):
        return True
    if is_flatpak():
        return True
    cgroup = _read_first_line("/proc/1/cgroup")
    if any(tag in cgroup for tag in ("docker", "podman", "containerd", "libpod")):
        return True
    # toolbox/distrobox set this in the environment of the container.
    return bool(os.environ.get("container") or os.environ.get("DISTROBOX_CONTAINER_ID"))


def session_type() -> str:
    """``"wayland"``, ``"x11"``, or ``"tty"``/``""`` when unknown."""
    value = (os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    if value:
        return value
    # A nested/remote session may not export it; Wayland always sets this.
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return ""


def is_wayland() -> bool:
    return session_type() == "wayland"


def desktop_environment() -> str:
    """``"gnome"``, ``"kde"``, ``"sway"``, ... (lower-case, empty if unknown)."""
    raw = (os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("DESKTOP_SESSION") or "")
    return raw.split(":")[0].strip().lower()


def selinux_mode() -> str:
    """``"enforcing"``, ``"permissive"``, ``"disabled"``, or ``""`` if unknown."""
    mode = _read_first_line("/sys/fs/selinux/enforce")
    if mode in ("0", "1"):
        return "enforcing" if mode == "1" else "permissive"
    getenforce = which_tool("getenforce")
    if getenforce:
        try:
            out = subprocess.run(
                [getenforce], capture_output=True, text=True, timeout=5
            ).stdout.strip().lower()
            if out:
                return out
        except (OSError, subprocess.SubprocessError):
            pass
    return ""


def is_selinux_enforcing() -> bool:
    return selinux_mode() == "enforcing"


# ── SSH helpers ─────────────────────────────────────────────────────────────
def _ssh_exec_argv(
    remote: str,
    ssh_port: str | None,
    *,
    remote_cmd: str | None = None,
    connect_timeout: int | None = None,
    strict_host_key_checking: bool | None = None,
) -> list[str]:
    """Build a consistent ssh argv for remote command execution."""
    remote_value = str(remote or "").strip()
    remote_host = remote_value.rsplit("@", 1)[-1]
    if not remote_value or remote_value.startswith("-") or not remote_host or remote_host.startswith("-"):
        raise ValueError("Invalid SSH remote host")
    argv = ["ssh"]
    if connect_timeout is not None:
        argv.extend(["-o", f"ConnectTimeout={int(connect_timeout)}"])
    if strict_host_key_checking is not None:
        argv.extend(
            [
                "-o",
                "StrictHostKeyChecking=yes"
                if strict_host_key_checking
                else "StrictHostKeyChecking=no",
            ]
        )
    if ssh_port and ssh_port != "22":
        argv.extend(["-p", str(ssh_port)])
    argv.append(remote)
    if remote_cmd is not None:
        argv.append(remote_cmd)
    return argv


def run_ssh_command(
    remote: str,
    ssh_port: str | None,
    remote_cmd: str,
    *,
    timeout: float,
    connect_timeout: int | None = None,
    strict_host_key_checking: bool | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Run an ssh command with centralized timeout and stderr/stdout capture."""
    return subprocess.run(
        _ssh_exec_argv(
            remote,
            ssh_port,
            remote_cmd=remote_cmd,
            connect_timeout=connect_timeout,
            strict_host_key_checking=strict_host_key_checking,
        ),
        timeout=timeout,
        capture_output=True,
        text=text,
    )
