"""Per-session permission mode for ACP agents, set when the session is created."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from omnigent.spec.types import AgentSpec

# Server-owned label holding the mode a session was created with. Clients set it
# only through the create request's ``permission_mode``, never as a raw label.
ACP_PERMISSION_MODE_LABEL_KEY = "omnigent.acp.permission_mode"

# Sent on the runner's bundle download because the mode is per session while the
# runner caches one parsed spec per bundle.
ACP_PERMISSION_MODE_HEADER = "X-Omnigent-Acp-Permission-Mode"

# ``auto`` shows an approval card; ``bypassPermissions`` skips the card for a
# request no TOOL_CALL policy decided (policy deny/ask still apply).
ACP_PERMISSION_MODES: frozenset[str] = frozenset({"auto", "bypassPermissions"})


def validate_acp_permission_mode(mode: str) -> str:
    """Return *mode* when it is a known ACP permission mode.

    :param mode: Requested mode, e.g. ``"bypassPermissions"``.
    :raises ValueError: When the mode is not one of :data:`ACP_PERMISSION_MODES`.
    """
    if mode not in ACP_PERMISSION_MODES:
        allowed = ", ".join(sorted(ACP_PERMISSION_MODES))
        raise ValueError(f"unsupported permission_mode {mode!r}; expected one of: {allowed}")
    return mode


# Spec config key for the session's mode. Distinct from ``permission_mode`` (which
# other harness builders also read) so only the ACP builders ever see it.
SESSION_PERMISSION_MODE_CONFIG_KEY = "acp_session_permission_mode"


def acp_permission_mode_from_config(config: Mapping[str, object]) -> str | None:
    """Return the mode for ``HARNESS_ACP_PERMISSION_MODE``: the session's, else the spec's."""
    mode = config.get(SESSION_PERMISSION_MODE_CONFIG_KEY, config.get("permission_mode"))
    return None if mode is None else str(mode)


def with_acp_permission_mode(spec: AgentSpec, mode: str) -> AgentSpec:
    """Return *spec* carrying the session's ACP permission mode *mode*."""
    config = {**spec.executor.config, SESSION_PERMISSION_MODE_CONFIG_KEY: mode}
    return replace(spec, executor=replace(spec.executor, config=config))
