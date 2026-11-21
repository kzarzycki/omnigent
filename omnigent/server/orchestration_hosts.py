"""Hosts marked orchestration-only: they coordinate, they don't run work.

An operator marks hosts with ``OMNIGENT_ORCHESTRATION_HOSTS`` (comma-separated
host ids or names) and lists the agents that may still run there with
``OMNIGENT_ORCHESTRATION_ALLOWED_AGENTS`` (comma-separated names of operator
registered agents). A session on the ``acp:claude-cloud`` harness is always
allowed, since its work runs in the cloud and the host only carries the bridge.
Unset ``OMNIGENT_ORCHESTRATION_HOSTS`` changes nothing.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.machine_access import CLOUD_SESSION_HARNESS, spec_harness_id

if TYPE_CHECKING:
    from omnigent.entities import Agent
    from omnigent.stores.host_store import Host

_logger = logging.getLogger(__name__)

HOSTS_ENV = "OMNIGENT_ORCHESTRATION_HOSTS"
ALLOWED_AGENTS_ENV = "OMNIGENT_ORCHESTRATION_ALLOWED_AGENTS"


def _names(raw: str) -> frozenset[str]:
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


@dataclass(frozen=True)
class OrchestrationHosts:
    """The operator's orchestration-only host marks.

    :param hosts: Host ids or names that run no local work sessions.
    :param allowed_agents: Names of operator registered agents allowed there.
    """

    hosts: frozenset[str]
    allowed_agents: frozenset[str]

    @staticmethod
    def from_env() -> OrchestrationHosts | None:
        """Read the marks from the environment, or ``None`` when no host is marked.

        :raises RuntimeError: When an allow list is set without a marked host.
        """
        hosts = _names(os.environ.get(HOSTS_ENV, ""))
        allowed = _names(os.environ.get(ALLOWED_AGENTS_ENV, ""))
        if not hosts:
            if allowed:
                raise RuntimeError(
                    f"{ALLOWED_AGENTS_ENV} needs {HOSTS_ENV}; unset it or mark a host"
                )
            return None
        return OrchestrationHosts(hosts=hosts, allowed_agents=allowed)

    def marks(self, host: Host) -> bool:
        """Whether *host* is marked orchestration-only."""
        return host.host_id in self.hosts or host.name in self.hosts


def refusal(host: Host) -> OmnigentError:
    """The error for a local work session on a marked host."""
    return OmnigentError(
        f"Host {host.name!r} is marked orchestration-only and runs no local work sessions "
        "for this agent. Run the work in the cloud or on another host.",
        code=ErrorCode.FORBIDDEN,
    )


async def require_host_allows_agent(
    marks: OrchestrationHosts | None,
    host: Host | None,
    agent: Agent | None,
    agent_cache: Any,
) -> None:
    """Refuse a local session of *agent* on *host* when the host is marked.

    :param marks: The operator's marks, or ``None`` when none are configured.
    :param host: The host the session would run on.
    :param agent: The agent the session binds to; ``None`` is refused on a marked host.
    :param agent_cache: Cache for loading the agent's spec to read its harness.
    :raises OmnigentError: 403 naming the mark when the session is not allowed.
    """
    if marks is None or host is None or not marks.marks(host):
        return
    if agent is not None and agent.operator_authored and agent.name in marks.allowed_agents:
        return
    if agent is not None and agent_cache is not None and agent.bundle_location is not None:
        try:
            loaded = await asyncio.to_thread(
                agent_cache.load,
                agent.id,
                agent.bundle_location,
                expand_env=agent.operator_authored,
            )
            if spec_harness_id(loaded.spec) == CLOUD_SESSION_HARNESS:
                return
        except Exception:
            # A spec that can't be read can't be shown to be a cloud bridge.
            _logger.warning("orchestration host: agent %s failed to load", agent.id, exc_info=True)
    raise refusal(host)


class OrchestrationLaunchGuard:
    """Re-check a session each time a runner is launched on a host, whatever started it."""

    def __init__(
        self, marks: OrchestrationHosts, host_store: Any, agent_store: Any, agent_cache: Any
    ) -> None:
        self._marks = marks
        self._host_store = host_store
        self._agent_store = agent_store
        self._agent_cache = agent_cache

    async def __call__(self, conv: Any, host_id: str) -> None:
        """Refuse the launch of *conv* on *host_id* when it is not allowed there.

        :param conv: The session a runner is about to launch for.
        :param host_id: The host the runner would run on.
        :raises OmnigentError: 403 naming the mark.
        """
        host = await asyncio.to_thread(self._host_store.get_host, host_id)
        if host is None or not self._marks.marks(host):
            return
        agent = (
            await asyncio.to_thread(self._agent_store.get, conv.agent_id)
            if self._agent_store is not None and conv.agent_id is not None
            else None
        )
        await require_host_allows_agent(self._marks, host, agent, self._agent_cache)
