"""Tell the runner that an archived session ended, so its agent can end what it holds.

A ``claude-cloud`` session keeps a claude.ai cloud session that outlives its runner. Only the
bridge on the runner host holds the claude.ai login and the cloud session id, so the server
sends the runner a ``session_ended`` event and the bridge archives its own cloud session.
A cancel or a runner restart sends nothing: that cloud session must keep running.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from omnigent.entities.conversation import Conversation
from omnigent.harness_aliases import canonicalize_harness

_logger = logging.getLogger(__name__)

SESSION_ENDED_EVENT_TYPE = "session_ended"
# Above the runner's own forward bound, which is above the bridge's archive call.
_SESSION_ENDED_TIMEOUT_S = 30.0


async def notify_session_ended(
    conv: Conversation,
    *,
    runner_router: Any = None,
    agent_store: Any = None,
) -> bool:
    """
    Post ``session_ended`` to the runner of *conv*; ``False`` when nothing was delivered.

    Only an ACP-harness session is told. No runner, or a failed post, is logged and the
    caller's own archive proceeds either way.

    :param conv: The archived Omnigent session.
    :param runner_router: The server's runner router, or ``None`` for the global one.
    :param agent_store: Store for resolving the session's harness, or ``None`` for the global one.
    :returns: Whether the runner accepted the event.
    """
    from omnigent.server.routes._sessions.common import get_server_runner_router
    from omnigent.server.routes._sessions.helpers import _get_runner_client, _resolve_harness

    try:
        harness = _resolve_harness(conv, agent_store=agent_store)
        if harness is None or canonicalize_harness(harness) != "acp":
            return False
        client = await _get_runner_client(
            conv.id, runner_router or get_server_runner_router(), conversation=conv
        )
        if client is None:
            _logger.warning(
                "Session %s ended with no runner to tell; its cloud session stays active",
                conv.id,
                extra={"session_id": conv.id},
            )
            return False
        resp = await client.post(
            f"/v1/sessions/{conv.id}/events",
            json={"type": SESSION_ENDED_EVENT_TYPE},
            timeout=_SESSION_ENDED_TIMEOUT_S,
        )
    except (httpx.HTTPError, ConnectionError, OSError):
        _logger.warning(
            "Could not tell the runner that session %s ended",
            conv.id,
            exc_info=True,
            extra={"session_id": conv.id},
        )
        return False
    except Exception:
        _logger.warning(
            "Session-ended notice failed for %s",
            conv.id,
            exc_info=True,
            extra={"session_id": conv.id},
        )
        return False
    if resp.status_code >= 400:
        _logger.warning(
            "Runner refused the session-ended notice for %s (status %s)",
            conv.id,
            resp.status_code,
            extra={"session_id": conv.id},
        )
        return False
    return True
