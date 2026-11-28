"""Placement policy for scripted ``placement="host"`` session creates.

With the ``scripted_sessions` flag on, a private session may start a session on any
host its owner has connected, as any agent. The operator limits that in the server
config (``config.yaml``) with a ``scripted_host_policy`` block::

    scripted_host_policy:
      rules:
        - host_id: host_a1b2c3        # a host's stable id; names are not matched
          agents: ["*"]               # any agent
        - host_id: host_d4e5f6
          agents: ["@cloud-bridge"]   # only operator cloud-bridge agents
        - host_id: host_a7b8c9
          agents: [reviewer, triager] # operator-registered agents, by name

Rules match host ids only: a host name is advertised by the host and its owner can
change it. A request is allowed only when a rule names the host and lists the agent.
Anything else is refused before a session exists. The policy covers every orchestrated
create whose destination host differs from the sender's host. With no policy configured,
every such request is refused (fail closed). Child, managed and same-host placement on
the sender's own host are not affected.

Repo rules (matching the workspace's git origin) are not supported: the server has
no independent way to learn a workspace's origin from the host.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Callable, Mapping
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
    """One rule: a host id and the agents allowed there.

    :param host: Host id, e.g. ``"host_a1b2c3"``.
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
        return refusal_error(host.name if host is not None else None, host_id, agent_name)

    def allows(
        self,
        host_id: str,
        agent_name: str | None,
        operator_registered: bool,
        is_cloud_bridge: Callable[[], bool],
    ) -> bool:
        """Whether a rule allows the agent on the host: the one decision server and dry-run share.

        :param host_id: The destination host id.
        :param agent_name: The agent's name, or ``None`` when unknown.
        :param operator_registered: Whether the operator registered the agent.
        :param is_cloud_bridge: Called only when a rule needs it; whether the agent is
            an operator cloud bridge with no embedded command.
        """
        for rule in self.rules:
            if rule.host != host_id:
                continue
            if ANY_AGENT in rule.agents:
                return True
            if agent_name is None:
                continue
            if operator_registered and agent_name in rule.agents:
                return True
            if CLOUD_BRIDGE_AGENT in rule.agents and is_cloud_bridge():
                return True
        return False

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
        if self.allows(
            host_id,
            agent.name if agent is not None else None,
            agent is not None and agent.operator_authored,
            lambda: agent is not None and _is_cloud_bridge(agent, agent_cache),
        ):
            return
        raise self.refusal(host, host_id, agent.name if agent is not None else None)


def placement_check_applies(
    flag_on: bool, has_source: bool, source_host_id: str | None, dest_host_id: str | None
) -> bool:
    """Whether the policy governs a create: the route's own applicability condition.

    It governs only an orchestrated create (an orchestration source is present) with
    the flag on that leaves the sender's host. With ``scripted_sessions`` off, or for a
    top-level start, nothing is checked, so the create path behaves as it did before.
    """
    return flag_on and has_source and dest_host_id is not None and dest_host_id != source_host_id


def decide_request(
    policy: ScriptedHostPolicy | None, flag_on: bool, request: Mapping[str, Any]
) -> tuple[bool, str]:
    """Allow or refuse one dry-run request with the server's own functions.

    :param request: ``{"source_host_id", "dest_host_id", "agent", "harness",
        "operator_registered"}``; an optional ``"orchestrated"`` says whether an
        orchestration source is present (default: ``source_host_id`` is not null; set true
        for a hostless orchestrating sender), and an optional
        ``"embedded_command"`` marks a bundle that brings its own ACP command.
    :returns: ``(allowed, reason)``.
    """
    dest = request.get("dest_host_id")
    source_host_id = request.get("source_host_id")
    # A null source host means a top-level start with no orchestration source; a hostless
    # orchestrating sender sets "orchestrated": true.
    has_source = bool(request.get("orchestrated", source_host_id is not None))
    if not placement_check_applies(flag_on, has_source, source_host_id, dest):
        if flag_on and not has_source:
            return True, "not checked: no orchestration source"
        return True, "not checked: flag off or the create stays on the sender's host"
    if policy is None:
        return False, "no scripted host placement policy is configured"
    operator = bool(request.get("operator_registered"))
    bridge = (
        operator
        and request.get("harness") == CLOUD_SESSION_HARNESS
        and not request.get("embedded_command")
    )
    if policy.allows(str(dest), request.get("agent"), operator, lambda: bridge):
        return True, "a rule allows it"
    return False, "no rule allows this agent on this host"


def _dry_run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m omnigent.server.scripted_host_policy",
        description="Print allow or refuse for each request line of a JSONL file.",
    )
    parser.add_argument("--dry-run", metavar="REQUESTS_JSONL", required=True)
    parser.add_argument("--flag", choices=("on", "off"), default="on")
    parser.add_argument("--policy", metavar="YAML", help="a config file holding the block")
    args = parser.parse_args(argv)
    policy = None
    if args.policy:
        import yaml

        with open(args.policy, encoding="utf-8") as handle:
            policy = load_scripted_host_policy(yaml.safe_load(handle) or {})
    with open(args.dry_run, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            allowed, reason = decide_request(policy, args.flag == "on", json.loads(line))
            print(f"{number}: {'allow' if allowed else 'refuse'} ({reason})")
    return 0


def refusal_error(host_name: str | None, host_id: str, agent_name: str | None) -> OmnigentError:
    """The 403 naming the host and agent the placement policy refused."""
    host_label = f"{host_name!r} ({host_id})" if host_name else repr(host_id)
    return OmnigentError(
        f"The placement policy refused this request: agent {agent_name or 'unknown'!r} "
        f"is not allowed on host {host_label}. No session was created.",
        code=ErrorCode.FORBIDDEN,
    )


def _is_cloud_bridge(agent: Agent, agent_cache: Any) -> bool:
    """Whether *agent* is an operator-authored cloud bridge with no embedded command.

    A user-uploaded bundle can claim the bridge harness and bring its own ACP
    command, so only operator agents count, and an embedded ``acp_agent`` override
    (which replaces the configured bridge) disqualifies the spec.
    """
    if not agent.operator_authored or agent_cache is None or agent.bundle_location is None:
        return False
    try:
        loaded = agent_cache.load(agent.id, agent.bundle_location, expand_env=True)
        return (
            spec_harness_id(loaded.spec) == CLOUD_SESSION_HARNESS
            and "acp_agent" not in loaded.spec.executor.config
        )
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
    if not isinstance(rules_raw, list) or set(raw) - {"rules"}:
        _logger.error("server config %s needs a 'rules' list — policy not loaded", POLICY_KEY)
        return None
    rules: list[HostRule] = []
    for item in rules_raw:
        host = item.get("host_id") if isinstance(item, dict) else None
        agents = item.get("agents") if isinstance(item, dict) else None
        if isinstance(agents, str):
            agents = [agents]
        if (
            not isinstance(host, str)
            or not host.strip()
            or not isinstance(agents, list)
            or not agents
            or not all(isinstance(a, str) and a.strip() for a in agents)
            or set(item) - {"host_id", "agents"}
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
    host = await asyncio.to_thread(host_store.get_host, host_id)
    agent = await asyncio.to_thread(agent_store.get, agent_id) if agent_id is not None else None
    if policy is None:
        error = refusal_error(
            host.name if host is not None else None, host_id, agent.name if agent else None
        )
        raise OmnigentError(
            f"{error.message} No scripted host placement policy ({POLICY_KEY}) is configured "
            "on this server.",
            code=ErrorCode.FORBIDDEN,
        )
    await asyncio.to_thread(policy.check, host, host_id, agent, agent_cache)


if __name__ == "__main__":
    sys.exit(_dry_run())
