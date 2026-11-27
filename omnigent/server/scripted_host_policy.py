"""Placement policy for scripted ``placement="host"`` session creates.

With the ``scripted_sessions` flag on, a private session may start a session on any
host its owner has connected, as any agent. The operator limits that in the server
config (``config.yaml``) with a ``scripted_host_policy`` block::

    scripted_host_policy:
      rules:
        - host: build-host          # host name or host id
          agents: ["*"]             # any agent
        - host: orchestration-host
          agents: ["@cloud-bridge"] # only cloud-bridge agents
        - host: review-host
          agents: [reviewer, triager]  # operator-registered agents, by name

A request is allowed only when a rule names the host and lists the agent. Anything
else is refused before a session exists. With no policy configured the policy is
absent and every host-placed request is refused (fail closed). Child, same-host and
managed placement are not affected.

Repo rules (matching the workspace's git origin) are not supported: the server has
no independent way to learn a workspace's origin from the host.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.machine_access import CLOUD_SESSION_HARNESS, spec_harness_id

if TYPE_CHECKING:
    from omnigent.entities import Agent
    from omnigent.stores.host_store import Host

_logger = logging.getLogger(__name__)

POLICY_KEY = "scripted_host_policy"
ANY_AGENT = "*"
CLOUD_BRIDGE_AGENT = "@cloud-bridge"


@dataclass(frozen=True)
class HostRule:
    """One rule: a host (name or id) and the agents allowed there.

    :param host: Host name or host id, e.g. ``"build-host"``.
    :param agents: Operator-registered agent names, ``"*"`` for any agent, or
        ``"@cloud-bridge"`` for agents on the cloud-bridge harness.
    """

    host: str
    agents: frozenset[str]


@dataclass(frozen=True)
class ScriptedHostPolicy:
    """The operator's placement rules for host-placed scripted creates."""

    rules: tuple[HostRule, ...]

    def refusal(self, host: Host | None, host_id: str, agent_name: str | None) -> OmnigentError:
        """The error for a request no rule allows."""
        host_label = host.name if host is not None else host_id
        return OmnigentError(
            f"The placement policy refused this request: agent {agent_name or 'unknown'!r} "
            f"is not allowed on host {host_label!r}. No session was created.",
            code=ErrorCode.FORBIDDEN,
        )

    def check(
        self, host: Host | None, host_id: str, agent: Agent | None, agent_cache: Any
    ) -> None:
        """Raise unless a rule allows *agent* on the host.

        :param host: The host row, or ``None`` when it is unknown (matched by id only).
        :param host_id: The requested host id.
        :param agent: The agent the session binds to, or ``None``.
        :param agent_cache: Cache for loading the agent's spec to read its harness.
        :raises OmnigentError: 403 when no rule allows the request.
        """
        identifiers = {host_id}
        if host is not None:
            identifiers.update({host.host_id, host.name})
        for rule in self.rules:
            if rule.host not in identifiers:
                continue
            if ANY_AGENT in rule.agents:
                return
            if agent is None:
                continue
            if agent.operator_authored and agent.name in rule.agents:
                return
            if CLOUD_BRIDGE_AGENT in rule.agents and _is_cloud_bridge(agent, agent_cache):
                return
        raise self.refusal(host, host_id, agent.name if agent is not None else None)


def _is_cloud_bridge(agent: Agent, agent_cache: Any) -> bool:
    if agent_cache is None or agent.bundle_location is None:
        return False
    try:
        loaded = agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.operator_authored
        )
        return spec_harness_id(loaded.spec) == CLOUD_SESSION_HARNESS
    except Exception:
        # A spec that can't be read can't be shown to be a cloud bridge.
        _logger.warning("scripted host policy: agent %s failed to load", agent.id, exc_info=True)
        return False


def load_scripted_host_policy(config: Mapping[str, Any]) -> ScriptedHostPolicy | None:
    """Read the policy from the server config; ``None`` when absent or invalid.

    An invalid block is logged and treated as absent, so the server fails closed
    instead of refusing to start.

    :param config: Loaded server config, e.g. ``{"scripted_host_policy": {"rules": [...]}}``.
    """
    raw = config.get(POLICY_KEY)
    if raw is None:
        return None
    rules_raw = raw.get("rules") if isinstance(raw, dict) else None
    if not isinstance(rules_raw, list):
        _logger.error("server config %s needs a 'rules' list — policy not loaded", POLICY_KEY)
        return None
    rules: list[HostRule] = []
    for item in rules_raw:
        host = item.get("host") if isinstance(item, dict) else None
        agents = item.get("agents") if isinstance(item, dict) else None
        if isinstance(agents, str):
            agents = [agents]
        if (
            not isinstance(host, str)
            or not host.strip()
            or not isinstance(agents, list)
            or not agents
            or not all(isinstance(a, str) and a.strip() for a in agents)
            or set(item) - {"host", "agents"}
        ):
            _logger.error(
                "server config %s has an invalid rule %r — policy not loaded", POLICY_KEY, item
            )
            return None
        rules.append(HostRule(host.strip(), frozenset(a.strip() for a in agents)))
    return ScriptedHostPolicy(tuple(rules))


async def require_scripted_host_placement(
    policy: ScriptedHostPolicy | None,
    host_store: Any,
    host_id: str,
    agent_store: Any,
    agent_id: str | None,
    agent_cache: Any,
) -> None:
    """Refuse a host-placed scripted create the policy does not allow.

    :raises OmnigentError: 403 when no policy is loaded or no rule allows it.
    """
    if policy is None:
        raise OmnigentError(
            "The placement policy refused this request: no scripted host placement policy "
            f"({POLICY_KEY}) is configured on this server. No session was created.",
            code=ErrorCode.FORBIDDEN,
        )
    host = await asyncio.to_thread(host_store.get_host, host_id)
    agent = await asyncio.to_thread(agent_store.get, agent_id) if agent_id is not None else None
    await asyncio.to_thread(policy.check, host, host_id, agent, agent_cache)
