"""Per-session Claude cloud environment for ACP agents, set when the session is created."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace

from omnigent.spec.types import AgentSpec

# Server-owned label holding the environment a session was created with. Clients set
# it only through the create request's ``cloud_environment_id``, never as a raw label.
CLOUD_ENVIRONMENT_LABEL_KEY = "omnigent.acp.cloud_environment_id"

# Sent on the runner's bundle download because the id is per session while the
# runner caches one parsed spec per bundle.
CLOUD_ENVIRONMENT_HEADER = "X-Omnigent-Cloud-Environment-Id"

# The variable the claude-cloud bridge reads to pick the environment of a new cloud session.
CLOUD_ENVIRONMENT_ENV_VAR = "CLAUDE_CLOUD_ENVIRONMENT_ID"

# Spec config key for the session's environment id.
SESSION_CLOUD_ENVIRONMENT_CONFIG_KEY = "acp_session_cloud_environment_id"

_CLOUD_ENVIRONMENT_ID_RE = re.compile(r"env_[A-Za-z0-9]{8,64}")


def validate_cloud_environment_id(value: str) -> str:
    """Return *value* when it has the form of a Claude cloud environment id.

    :param value: Requested id, e.g. ``"env_01AbCdEfGhIjKlMn"``.
    :raises ValueError: When the id is not ``env_`` plus letters and digits.
    """
    if not _CLOUD_ENVIRONMENT_ID_RE.fullmatch(value):
        raise ValueError(
            f"cloud_environment_id {value!r} is not a Claude cloud environment id "
            "(expected the form env_01...)"
        )
    return value


def with_cloud_environment_id(spec: AgentSpec, environment_id: str) -> AgentSpec:
    """Return *spec* carrying the session's cloud environment id."""
    config = {**spec.executor.config, SESSION_CLOUD_ENVIRONMENT_CONFIG_KEY: environment_id}
    return replace(spec, executor=replace(spec.executor, config=config))


def apply_cloud_environment_env(env: dict[str, str], config: Mapping[str, object]) -> None:
    """Add the session's cloud environment id to *env* and name it for the ACP agent.

    The ACP wrap forwards only passthrough-named variables to the agent process.
    """
    value = config.get(SESSION_CLOUD_ENVIRONMENT_CONFIG_KEY)
    if not isinstance(value, str) or not value:
        return
    env[CLOUD_ENVIRONMENT_ENV_VAR] = value
    names = {n.strip() for n in env.get("HARNESS_ACP_ENV_PASSTHROUGH", "").split(",") if n.strip()}
    names.add(CLOUD_ENVIRONMENT_ENV_VAR)
    env["HARNESS_ACP_ENV_PASSTHROUGH"] = ",".join(sorted(names))
