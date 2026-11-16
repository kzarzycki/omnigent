"""Route-level checks that confine the machine principal's session starts.

The grant itself lives in :mod:`omnigent.server.machine_access`. These helpers
run only for that principal, and only when the operator configured a grant.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from starlette.requests import HTTPConnection

from omnigent.server.machine_access import (
    MACHINE_CREATE_FIELDS,
    MachineSessionAccess,
    launch_args_bypass_permissions,
    refuse_machine_request,
    spec_declares_bypass,
    spec_harness_id,
)

if TYPE_CHECKING:
    from omnigent.entities import Agent
    from omnigent.runtime.agent_cache import AgentCache

_logger = logging.getLogger(__name__)


def machine_access_for(
    request: HTTPConnection, user_id: str | None
) -> MachineSessionAccess | None:
    """Return the grant when *user_id* is the configured machine principal.

    :param request: The request, for ``app.state``.
    :param user_id: Authenticated caller, or ``None`` when auth is disabled.
    :returns: The grant, or ``None`` for every other caller and when the
        operator configured none.
    """
    access = getattr(request.app.state, "machine_session_access", None)
    if access is not None and access.applies_to(user_id):
        return access
    return None


# Events that answer approvals or change what a session's terminal may do.
MACHINE_REFUSED_EVENT_TYPES = frozenset(
    {
        "approval",
        "slash_command",
        "external_permission_mode_change",
        "external_codex_approval_mode_change",
        "external_codex_collaboration_mode_change",
    }
)


def require_not_machine(request: HTTPConnection, user_id: str | None, action: str) -> None:
    """Refuse *action* when the caller is the machine principal under a grant.

    For owner-level routes whose effect reaches the user's host (a terminal, an
    approval, a permission mode), which the machine principal now owns a session
    for but must not drive.

    :param request: The request or WebSocket, for ``app.state``.
    :param user_id: Authenticated caller.
    :param action: What was refused, e.g. ``"open a terminal"``.
    :raises OmnigentError: 403 when the caller is the machine principal.
    """
    if machine_access_for(request, user_id) is not None:
        refuse_machine_request(action)


def require_machine_create_shape(access: MachineSessionAccess, fields_set: set[str]) -> None:
    """Refuse a create that sets anything beyond the fields a machine start needs.

    :param access: The machine principal's grant.
    :param fields_set: Fields the create request set explicitly.
    :raises OmnigentError: 403 naming the first refused field.
    """
    del access  # the field list is fixed; the parameter keeps the call site explicit
    extra = sorted(fields_set - MACHINE_CREATE_FIELDS)
    if extra:
        refuse_machine_request(f"set {extra[0]!r} when starting a session")


async def require_machine_agent_allowed(
    access: MachineSessionAccess,
    agent: Agent,
    agent_cache: AgentCache | None,
    *,
    launch_args: list[str] | None = None,
) -> None:
    """Refuse an agent the grant does not list, or one that starts in bypass.

    :param access: The machine principal's grant.
    :param agent: The agent the session binds to.
    :param agent_cache: Cache for loading the agent's spec.
    :param launch_args: A session's stored terminal launch args, when one exists
        already (the runner-launch route); ``None`` at create.
    :raises OmnigentError: 403 when the agent is not permitted or would open a
        terminal in a bypass permission mode.
    """
    if agent_cache is None or agent.bundle_location is None:
        refuse_machine_request("start an agent whose spec cannot be checked")
    try:
        loaded = await asyncio.to_thread(
            agent_cache.load, agent.id, agent.bundle_location, expand_env=agent.operator_authored
        )
    except Exception:
        # A spec that can't be read can't be shown to be safe.
        _logger.warning("machine start: agent %s failed to load", agent.id, exc_info=True)
        refuse_machine_request("start an agent whose spec cannot be checked")
    spec: Any = loaded.spec
    if not access.agent_permitted(agent, spec_harness_id(spec)):
        refuse_machine_request("start this agent")
    if launch_args_bypass_permissions(launch_args) or spec_declares_bypass(spec):
        refuse_machine_request("start a terminal that skips permission prompts")
