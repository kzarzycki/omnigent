"""Run a host's session processes inside one memory-limited user-systemd slice.

With ``host.session_slice.enabled`` on, the daemon starts every runner (and
the zygote that forks runners) through ``systemd-run --user --scope`` in the
configured slice. Harness processes and tmux servers are runner descendants,
and tmux places its pane scopes in its server's slice, so a whole session stays
inside the slice while the daemon itself stays outside it. The slice's memory
limits are applied as runtime properties of the user manager, so no sudo is
needed. Hosts without a user manager that delegates the memory controller log
once and launch as before.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from omnigent._platform import IS_LINUX
from omnigent.util.user_systemd import (
    UserSystemdError,
    is_memory_limit,
    is_slice_name,
    missing_user_manager_controllers,
    scope_command_prefix,
    set_slice_properties,
)

_logger = logging.getLogger(__name__)

DEFAULT_SESSION_SLICE_NAME = "omnigent-sessions.slice"
# Set in each runner's environment to the active slice, so a session's own
# ``os_env.sandbox.resources`` slice nests inside it.
SESSION_SLICE_ENV_VAR = "OMNIGENT_SESSION_SLICE"

# Config key -> systemd slice property it sets.
_MEMORY_PROPERTIES = {
    "memory_high": "MemoryHigh",
    "memory_max": "MemoryMax",
    "memory_swap_max": "MemorySwapMax",
}


@dataclass(frozen=True)
class SessionSliceConfig:
    """``host.session_slice``: the slice and limits for session processes.

    :param name: Slice unit name, e.g. ``"omnigent-sessions.slice"``.
    :param properties: systemd property assignments, e.g.
        ``("MemoryHigh=8G", "MemoryMax=9G")``; empty sets no limit.
    """

    name: str
    properties: tuple[str, ...]


def load_session_slice_config(path: Path) -> SessionSliceConfig | None:
    """Read ``host.session_slice`` from the host's ``config.yaml``.

    :param path: The host's ``config.yaml``.
    :returns: The slice config when ``enabled: true``; ``None`` when the file,
        section or flag is missing or the flag is false.
    :raises ValueError: If a value has the wrong type or form.
    """
    if not path.exists():
        return None
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    host_section = cfg.get("host") if isinstance(cfg, dict) else None
    if not isinstance(host_section, dict) or "session_slice" not in host_section:
        return None
    section = host_section["session_slice"]
    if not isinstance(section, dict):
        raise ValueError(f"host.session_slice in {path} must be a mapping, got {section!r}.")
    enabled = section.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(
            f"host.session_slice.enabled in {path} must be true or false, got {enabled!r}."
        )
    if not enabled:
        return None
    name = section.get("name", DEFAULT_SESSION_SLICE_NAME)
    if not isinstance(name, str) or not is_slice_name(name):
        raise ValueError(
            f"host.session_slice.name in {path} must be a slice unit name such as "
            f"{DEFAULT_SESSION_SLICE_NAME!r}, got {name!r}."
        )
    properties: list[str] = []
    for key, systemd_property in _MEMORY_PROPERTIES.items():
        if key not in section:
            continue
        value = section[key]
        if isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str) or not is_memory_limit(value):
            raise ValueError(
                f"host.session_slice.{key} in {path} must be a size such as '8G', "
                f"a percentage such as '60%', or 'infinity', got {value!r}."
            )
        properties.append(f"{systemd_property}={value}")
    return SessionSliceConfig(name=name, properties=tuple(properties))


@dataclass(frozen=True)
class SessionSliceLaunch:
    """An active session slice: where runners go and how to start them there.

    :param name: The slice unit name, e.g. ``"omnigent-sessions.slice"``.
    :param command_prefix: ``systemd-run`` argv prefix that starts a command
        in its own scope in the slice.
    """

    name: str
    command_prefix: tuple[str, ...]


def activate_session_slice(config: SessionSliceConfig | None) -> SessionSliceLaunch | None:
    """Apply the slice's limits and return how to launch session processes in it.

    :param config: The loaded ``host.session_slice``, or ``None`` when it is off.
    :returns: The active slice, or ``None`` when the setting is off or this host
        has no user manager that can carry memory limits (logged once here).
    """
    if config is None:
        return None
    if not IS_LINUX or shutil.which("systemd-run") is None:
        _logger.warning(
            "host.session_slice is on, but this host has no systemd; "
            "session processes run without the %s memory limits",
            config.name,
        )
        return None
    missing = missing_user_manager_controllers(["memory"])
    if missing is None or missing:
        _logger.warning(
            "host.session_slice is on, but no user systemd manager with the memory "
            "controller is reachable; session processes run without the %s limits",
            config.name,
        )
        return None
    if config.properties:
        try:
            set_slice_properties(config.name, config.properties)
        except UserSystemdError as exc:
            _logger.warning(
                "host.session_slice is on, but setting %s's limits failed (%s); "
                "session processes run without them",
                config.name,
                exc,
            )
            return None
    _logger.info(
        "Session processes run in %s (%s)",
        config.name,
        ", ".join(config.properties) or "no memory limits",
    )
    return SessionSliceLaunch(
        name=config.name,
        command_prefix=scope_command_prefix(
            config.name, f"Omnigent session runner (host pid {os.getpid()})"
        ),
    )
