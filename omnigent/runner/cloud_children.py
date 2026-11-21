"""Where a ``claude-cloud`` session's ``sys_session_create`` children run.

A cloud session's runner is the bridge on a host, so a child that inherits that
runner (the default placement) or a ``same_host`` session runs on the bridge
host, which the cloud session never chose. Its helpers belong in the cloud: a
default create starts a ``claude-cloud`` child (the caller's own agent when it
names none), and a local agent runs only on a host the create names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from omnigent.server.machine_access import CLOUD_SESSION_HARNESS, spec_harness_id

if TYPE_CHECKING:
    from omnigent.spec import AgentSpec

NAME_A_HOST = (
    "pass placement='host' with host_id and workspace to run it on a host you name, "
    "or placement='managed' for a sandbox"
)


def is_cloud_session(agent_spec: AgentSpec | None) -> bool:
    """Whether the calling agent runs in a Claude cloud session.

    :param agent_spec: The caller's spec, or ``None`` when unknown.
    :returns: ``True`` for an agent on the ``acp:claude-cloud`` harness.
    """
    return agent_spec is not None and spec_harness_id(agent_spec) == CLOUD_SESSION_HARNESS


async def own_agent_id(server_client: httpx.AsyncClient, conversation_id: str) -> str | None:
    """The agent id of the calling session, or ``None`` when it can't be read.

    :param server_client: HTTP client pointed at the Omnigent server.
    :param conversation_id: The caller's session id.
    :returns: e.g. ``"ag_cloud"``.
    """
    try:
        resp = await server_client.get(f"/v1/sessions/{conversation_id}", timeout=30.0)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    agent_id = resp.json().get("agent_id")
    return agent_id if isinstance(agent_id, str) and agent_id else None


async def _agent_harness(server_client: httpx.AsyncClient, agent_id: str) -> str | None:
    try:
        resp = await server_client.get(f"/v1/agents/{agent_id}", timeout=30.0)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    harness = resp.json().get("harness")
    return harness if isinstance(harness, str) else None


async def bridge_host_refusal(
    server_client: httpx.AsyncClient,
    *,
    placement: str,
    agent_id: str,
    own_agent: str | None,
) -> str | None:
    """Why a cloud caller's create would put a local session on the bridge host.

    :param server_client: HTTP client pointed at the Omnigent server.
    :param placement: The requested placement, e.g. ``"child"``.
    :param agent_id: The agent the create starts.
    :param own_agent: The caller's own agent id, a cloud agent by definition.
    :returns: The error text, or ``None`` when the create may go ahead.
    """
    if placement == "same_host":
        return (
            "placement='same_host' from a claude-cloud session would start the session on the "
            f"bridge host; {NAME_A_HOST}"
        )
    if placement != "child" or agent_id == own_agent:
        return None
    harness = await _agent_harness(server_client, agent_id)
    if harness == CLOUD_SESSION_HARNESS:
        return None
    return (
        f"Agent {agent_id!r} is not a claude-cloud agent, so as a child of this claude-cloud "
        f"session (the default placement) it would run on the bridge host; {NAME_A_HOST}. "
        "Omit agent_id for a claude-cloud child in this session's environment"
    )
