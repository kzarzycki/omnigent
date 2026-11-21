"""Where a ``claude-cloud`` session's child sessions run.

A cloud session's runner is the bridge on a host, so a child that inherits that
runner (the default placement) or a ``same_host`` session runs on the bridge
host, which the cloud session never chose. Its helpers belong in the cloud: a
default create starts a ``claude-cloud`` child (the caller's own agent when it
names none) in the parent's workspace, whose pin selects the cloud environment,
and a local agent runs only on a host the create names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from omnigent.server.machine_access import CLOUD_SESSION_HARNESS, spec_harness_id

if TYPE_CHECKING:
    from omnigent.spec import AgentSpec

NAME_A_HOST = (
    "pass placement='host' with host_id and workspace to run it on a host you name, "
    "or placement='managed' for a sandbox"
)


@dataclass(frozen=True)
class CloudCaller:
    """The calling cloud session's own agent and workspace, each ``None`` when unread."""

    agent_id: str | None
    workspace: str | None


def is_cloud_session(agent_spec: AgentSpec | None, effective_harness: str | None = None) -> bool:
    """Whether the calling agent runs in a Claude cloud session.

    :param agent_spec: The caller's spec, or ``None`` when unknown.
    :param effective_harness: The session's harness override, which wins over the spec's.
    :returns: ``True`` for a session on the ``acp:claude-cloud`` harness.
    """
    if effective_harness == CLOUD_SESSION_HARNESS:
        return True
    # The runner canonicalizes ``acp:<slug>`` to ``acp``, so that value defers to the spec.
    if effective_harness and effective_harness != "acp":
        return False
    if agent_spec is None or getattr(agent_spec, "executor", None) is None:
        return False
    return spec_harness_id(agent_spec) == CLOUD_SESSION_HARNESS


async def read_caller(server_client: httpx.AsyncClient, conversation_id: str) -> CloudCaller:
    """Read the calling session's agent id and workspace.

    :param server_client: HTTP client pointed at the Omnigent server.
    :param conversation_id: The caller's session id.
    :returns: What the session snapshot holds, ``None`` fields when it can't be read.
    """
    from omnigent.runner.tool_dispatch import _get_session_metadata

    try:
        resp = await _get_session_metadata(server_client, conversation_id, timeout=30.0)
    except httpx.HTTPError:
        return CloudCaller(None, None)
    snapshot = resp.json() if resp.status_code == 200 else {}
    agent_id, workspace = snapshot.get("agent_id"), snapshot.get("workspace")
    return CloudCaller(
        agent_id if isinstance(agent_id, str) and agent_id else None,
        workspace if isinstance(workspace, str) and workspace else None,
    )


async def _agent_harness(server_client: httpx.AsyncClient, agent_id: str) -> str | None:
    try:
        resp = await server_client.get(f"/v1/agents/{agent_id}", timeout=30.0)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    harness = resp.json().get("harness")
    return harness if isinstance(harness, str) else None


def local_child_refusal(agent: str) -> str:
    """The error for a local agent that would start as a child on the bridge host.

    :param agent: The agent's id or name, e.g. ``"ag_local"``.
    :returns: The error text, naming how to place it on a host.
    """
    return (
        f"Agent {agent!r} is not a claude-cloud agent, so as a child of this claude-cloud "
        f"session it would run on the bridge host; {NAME_A_HOST}. Omit agent_id for a "
        "claude-cloud child in this session's environment; no session was created"
    )


async def bridge_host_refusal(
    server_client: httpx.AsyncClient,
    *,
    placement: str,
    agent_id: str,
    own_agent: str | None,
    pinned: bool,
) -> str | None:
    """Why a cloud caller's create would put a local session on the bridge host.

    :param server_client: HTTP client pointed at the Omnigent server.
    :param placement: The requested placement, e.g. ``"child"``.
    :param agent_id: The agent the create starts.
    :param own_agent: The caller's own agent id, a cloud agent by definition.
    :param pinned: Whether the create names a project by ``environment_id``,
        which places a ``same_host`` create on that project's pinned host.
    :returns: The error text, or ``None`` when the create may go ahead.
    """
    if placement == "same_host" and not pinned:
        return (
            "placement='same_host' from a claude-cloud session would start the session on the "
            f"bridge host; {NAME_A_HOST}, or name a project's pinned host with environment_id; "
            "no session was created"
        )
    if placement != "child" or agent_id == own_agent:
        return None
    if await _agent_harness(server_client, agent_id) == CLOUD_SESSION_HARNESS:
        return None
    return local_child_refusal(agent_id)
