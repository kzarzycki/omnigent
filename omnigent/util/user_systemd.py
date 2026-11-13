"""Place processes in the current user's systemd manager, without sudo.

Shared by the host (one slice for every session) and the runner (one slice
per session). A unit's limits only take effect when the user manager
delegates the matching cgroup controller, so callers check that first.
"""

from __future__ import annotations

import functools
import re
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from omnigent._platform import IS_LINUX

SYSTEMCTL_TIMEOUT_S = 10
_MOVE_TIMEOUT_S = 5
_MOVE_POLL_S = 0.02
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_UINT64_MAX = 2**64 - 1
_UINT32_MAX = 2**32 - 1
_SIZE_SUFFIXES = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
# Sizes as systemd writes them: bytes with an optional K/M/G/T suffix, a
# percentage of RAM, or ``infinity``.
_MEMORY_LIMIT = re.compile(r"(?P<number>\d+(\.\d+)?)(?P<unit>[KMGT%]?)|infinity")
_SLICE_NAME = re.compile(r"[A-Za-z0-9_.:-]+\.slice")


# Scopes accept OOMPolicy= from this release; older ones reject the property.
_SCOPE_OOM_POLICY_SINCE = 253


class UserSystemdError(RuntimeError):
    """The user manager refused a request or could not be reached."""


def is_memory_limit(value: str) -> bool:
    """Whether *value* is a memory limit systemd accepts, e.g. ``"8G"``."""
    return _MEMORY_LIMIT.fullmatch(value) is not None


def is_slice_name(value: str) -> bool:
    """Whether *value* is a slice unit name, e.g. ``"omnigent-sessions.slice"``."""
    return _SLICE_NAME.fullmatch(value) is not None


def child_slice_name(parent: str, leaf: str) -> str:
    """Name the slice nested directly under *parent*, e.g. ``a-b.slice`` under ``a.slice``.

    :param parent: The parent slice unit name.
    :param leaf: The child's own name part; characters other than ASCII letters
        and digits are dropped, since ``-`` would nest it further.
    """
    return f"{parent.removesuffix('.slice')}-{re.sub(r'[^A-Za-z0-9]', '', leaf)}.slice"


def missing_user_manager_controllers(controllers: Sequence[str]) -> list[str] | None:
    """Return which *controllers* the user manager does not delegate.

    :param controllers: cgroup controllers the caller's limits need, e.g.
        ``["memory"]``.
    :returns: The missing controllers (empty when all are delegated), or
        ``None`` when this host has no reachable user systemd manager.
    """
    if not IS_LINUX or shutil.which("systemctl") is None:
        return None
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", "--property=ControlGroup", "--value"],
            capture_output=True,
            text=True,
            timeout=SYSTEMCTL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    control_group = result.stdout.strip()
    if result.returncode != 0 or not control_group:
        return None
    subtree_control = _CGROUP_ROOT / control_group.lstrip("/") / "cgroup.subtree_control"
    try:
        delegated = set(subtree_control.read_text().split())
    except OSError:
        return None
    return [controller for controller in controllers if controller not in delegated]


@functools.cache
def _scope_oom_policy_supported() -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "--version"],
            capture_output=True,
            text=True,
            timeout=SYSTEMCTL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    match = re.match(r"systemd (\d+)", result.stdout)
    return match is not None and int(match[1]) >= _SCOPE_OOM_POLICY_SINCE


def set_slice_properties(slice_name: str, properties: Sequence[str]) -> None:
    """Set runtime properties on a slice, creating its unit if needed.

    ``--runtime`` keeps them out of persistent unit files, so the caller
    reapplies them on every start.

    :param slice_name: The slice unit, e.g. ``"omnigent-sessions.slice"``.
    :param properties: Assignments, e.g. ``["MemoryMax=9G"]``.
    :raises UserSystemdError: If ``systemctl`` fails.
    """
    try:
        subprocess.run(
            ["systemctl", "--user", "set-property", "--runtime", slice_name, *properties],
            capture_output=True,
            text=True,
            timeout=SYSTEMCTL_TIMEOUT_S,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", None) or exc
        raise UserSystemdError(str(detail).strip()) from exc


def scope_command_prefix(slice_name: str, description: str) -> tuple[str, ...]:
    """Return an argv prefix that runs a command in a new scope in *slice_name*.

    ``systemd-run --scope`` execs the command in place, so the pid, fds and
    environment the caller set up are kept.
    """
    # OOMPolicy=continue: an OOM kill takes only the chosen process, never every
    # session that shares the scope.
    oom_policy = ("--property=OOMPolicy=continue",) if _scope_oom_policy_supported() else ()
    return (
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        *oom_policy,
        f"--slice={slice_name}",
        f"--description={description}",
        "--",
    )


def move_into_new_slice(
    pids: Sequence[int],
    *,
    scope_name: str,
    slice_name: str,
    slice_properties: Mapping[str, str],
    description: str,
) -> None:
    """Move *pid* into a new scope inside a new slice that carries the limits.

    The slice, not the scope, carries the limits, so tmux pane scopes (which
    tmux puts in its server's slice) stay under them. One D-Bus call creates
    both units; the slice stops itself once its last unit is gone. The call
    returns once the first pid is in the scope, since the manager moves the
    processes in a job that finishes after the D-Bus reply.

    :param pids: The processes to move, e.g. the caller and its children.
    :param scope_name: The new scope unit, e.g. ``"omnigent-session-ab12.scope"``.
    :param slice_name: The new slice unit, e.g. ``"omnigent-sessions-ab12.slice"``.
    :param slice_properties: systemd properties to values, e.g.
        ``{"MemoryMax": "2G", "CPUWeight": "50"}``.
    :param description: Human-readable text shown by ``systemctl --user status``.
    :raises UserSystemdError: If ``busctl`` is missing, the manager refuses, or
        the first pid is not in the scope within the timeout.
    """
    if shutil.which("busctl") is None:
        raise UserSystemdError("busctl is not installed")
    slice_args: list[str] = []
    for name, value in slice_properties.items():
        slice_args.extend(_dbus_property(name, value))
    # OOMPolicy=continue: an OOM kill takes the session's largest process,
    # not the runner and every other process in the scope.
    scope_args = ["OOMPolicy", "s", "continue"] if _scope_oom_policy_supported() else []
    argv = [
        "busctl",
        "--user",
        "call",
        "org.freedesktop.systemd1",
        "/org/freedesktop/systemd1",
        "org.freedesktop.systemd1.Manager",
        "StartTransientUnit",
        "ssa(sv)a(sa(sv))",
        scope_name,
        "fail",
        str(4 + len(scope_args) // 3),
        "Slice",
        "s",
        slice_name,
        "PIDs",
        "au",
        str(len(pids)),
        *(str(pid) for pid in pids),
        "Description",
        "s",
        description,
        "CollectMode",
        "s",
        "inactive-or-failed",
        *scope_args,
        "1",
        slice_name,
        str(len(slice_properties) + 2),
        *slice_args,
        "Description",
        "s",
        description,
        "StopWhenUnneeded",
        "b",
        "true",
    ]
    try:
        subprocess.run(
            argv, capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT_S, check=True
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", None) or exc
        raise UserSystemdError(str(detail).strip()) from exc
    _wait_until_in_unit(pids[0], scope_name)


def _wait_until_in_unit(pid: int, unit_name: str) -> None:
    """Wait until *pid*'s cgroup is *unit_name*'s.

    :raises UserSystemdError: If it is not there within the timeout.
    """
    deadline = time.monotonic() + _MOVE_TIMEOUT_S
    cgroup = ""
    while time.monotonic() < deadline:
        try:
            cgroup = Path(f"/proc/{pid}/cgroup").read_text().strip()
        except OSError as exc:
            raise UserSystemdError(f"cannot read pid {pid}'s cgroup: {exc}") from exc
        if cgroup.endswith(f"/{unit_name}"):
            return
        time.sleep(_MOVE_POLL_S)
    raise UserSystemdError(f"pid {pid} did not move into {unit_name} (cgroup {cgroup!r})")


def _dbus_property(name: str, value: str) -> list[str]:
    """Encode one property assignment as ``busctl`` ``(sv)`` arguments.

    :param name: A memory property (e.g. ``"MemoryMax"``) or ``"CPUWeight"``.
    :param value: Its value as written in a unit file, e.g. ``"2G"`` or ``"50%"``.
    """
    if name == "CPUWeight":
        return [name, "t", str(int(value))]
    if value == "infinity":
        return [name, "t", str(_UINT64_MAX)]
    match = _MEMORY_LIMIT.fullmatch(value)
    if match is None:
        raise ValueError(f"{name}={value!r} is not a memory limit")
    number = float(match["number"])
    if match["unit"] == "%":
        # systemd's D-Bus API takes a percentage as a fraction of UINT32_MAX.
        return [f"{name}Scale", "u", str(round(number / 100 * _UINT32_MAX))]
    return [name, "t", str(int(number * _SIZE_SUFFIXES[match["unit"]]))]
