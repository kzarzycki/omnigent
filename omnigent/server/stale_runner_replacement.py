"""Replace a host's idle adopted runner on an older build with a fresh one.

A host daemon asks (``host.replace_stale_runner``) after it adopted a runner
whose recorded build differs from its own. The server holds the session state
that decides whether the runner is idle and whether its owner is driving it,
then relaunches the session on the host through the normal relaunch path.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from omnigent.host.frames import (
    REPLACE_STALE_RUNNER_BUSY,
    REPLACE_STALE_RUNNER_FAILED,
    REPLACE_STALE_RUNNER_INELIGIBLE,
    REPLACE_STALE_RUNNER_REPLACED,
)

if TYPE_CHECKING:
    from omnigent.entities.conversation import Conversation
    from omnigent.server.host_registry import HostRegistry
    from omnigent.stores.conversation_store import ConversationStore

_logger = logging.getLogger(__name__)

# A session whose owner sent a message this recently is being driven; leave it.
OWNER_ACTIVE_WINDOW_S = 30 * 60
# Newest message items scanned for the owner's last message.
_RECENT_MESSAGES_SCANNED = 20
# Text a session-to-session message starts with; it is not the owner's own.
_PEER_MESSAGE_PREFIX = "[Message from session "
# Launch refusals that left the binding to another flight; nothing to restore.
_BINDING_ALREADY_MOVED = frozenset({"session_binding_changed", "session_not_found"})


def owner_messaged_since(
    conversation_store: ConversationStore, session_id: str, since: float
) -> bool:
    """Whether a user message other than a peer session's arrived at or after *since*.

    :param conversation_store: Store holding the session's items.
    :param session_id: Session to read, e.g. ``"conv_abc123"``.
    :param since: Unix epoch seconds, e.g. ``time.time() - 1800``.
    :returns: ``True`` when such a message exists.
    """
    page = conversation_store.list_items(
        session_id, limit=_RECENT_MESSAGES_SCANNED, order="desc", type="message"
    )
    for item in page.data:
        if item.created_at < since:
            return False
        if getattr(item.data, "role", None) != "user":
            continue
        content = getattr(item.data, "content", None) or []
        text = content[0].get("text") if content and isinstance(content[0], dict) else None
        if isinstance(text, str) and text.startswith(_PEER_MESSAGE_PREFIX):
            continue
        return True
    return False


def busy_reason(
    conv: Conversation, conversation_store: ConversationStore, now: float
) -> str | None:
    """Why *conv* can't have its runner replaced now, or ``None`` when it is idle.

    :param conv: The session row.
    :param conversation_store: Store holding the session's items.
    :param now: Unix epoch seconds.
    :returns: A short reason, e.g. ``"turn running"``, or ``None``.
    """
    from omnigent.runtime import pending_elicitations, pending_inputs
    from omnigent.server.routes._sessions.common import _session_status_cache
    from omnigent.server.routes._sessions.orchestration import _MID_TURN_STATUSES

    if _session_status_cache.get(conv.id, conv.live_status) in _MID_TURN_STATUSES:
        return "turn running"
    if pending_inputs.has_pending(conv.id):
        return "turn queued"
    if (
        pending_elicitations.count_for(conv.id)
        or conv.pending_elicitation_count
        or conv.open_questions
    ):
        return "question or elicitation pending"
    if owner_messaged_since(conversation_store, conv.id, now - OWNER_ACTIVE_WINDOW_S):
        return "owner messaged recently"
    return None


async def replace_stale_runner(
    host_id: str,
    runner_id: str,
    session_id: str,
    *,
    conversation_store: ConversationStore,
    host_registry: HostRegistry,
    clock: Callable[[], float] = time.time,
) -> tuple[str, str | None]:
    """Relaunch *session_id* on *host_id* if its stale runner is idle.

    The old runner stays up until the host has the new runner's process alive,
    so a refused launch keeps the session on the old runner.

    :param host_id: The asking host, e.g. ``"a1b2..."``.
    :param runner_id: Its stale adopted runner, e.g. ``"runner_abc123..."``.
    :param session_id: The session that runner serves, e.g. ``"conv_abc123"``.
    :param conversation_store: Session store.
    :param host_registry: Live host connections.
    :param clock: Current Unix time; tests pass a fixed one.
    :returns: ``(status, reason)`` with a ``REPLACE_STALE_RUNNER_*`` status.
    """
    from omnigent.server.routes._sessions.helpers import _owns_host_runner
    from omnigent.server.routes.sessions import _launch_runner_on_host

    conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
    if conv is None or conv.host_id != host_id or conv.runner_id != runner_id:
        return REPLACE_STALE_RUNNER_INELIGIBLE, "the session is no longer bound to this runner"
    if not await _owns_host_runner(conv, conversation_store):
        return REPLACE_STALE_RUNNER_INELIGIBLE, "the runner is shared with an ancestor session"
    host_conn = host_registry.get(host_id)
    if host_conn is None:
        return REPLACE_STALE_RUNNER_BUSY, "the host is not connected to this replica"
    reason = await asyncio.to_thread(busy_reason, conv, conversation_store, clock())
    if reason is not None:
        return REPLACE_STALE_RUNNER_BUSY, reason
    attempt = await _launch_runner_on_host(
        conv, conversation_store, host_registry, host_conn, stop_superseded_first=False
    )
    if attempt.error is None and attempt.error_code is None and attempt.acknowledged:
        _logger.info(
            "Replaced stale runner %s for session %s with %s",
            runner_id,
            session_id,
            attempt.runner_id,
        )
        return REPLACE_STALE_RUNNER_REPLACED, None
    refused = attempt.error is not None or attempt.error_code is not None
    if (
        refused
        and attempt.runner_id != runner_id
        and attempt.error_code not in _BINDING_ALREADY_MOVED
    ):
        # A refused launch spawned nothing, so the old runner still serves the session.
        await asyncio.to_thread(
            conversation_store.replace_runner_id,
            session_id,
            runner_id,
            expected_runner_id=attempt.runner_id,
        )
    return REPLACE_STALE_RUNNER_FAILED, attempt.error or attempt.error_code or "launch unconfirmed"
