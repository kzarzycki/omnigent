"""Resume a session whose runner went away while its background work was pending.

Claude Code's background shells and background agents wake the agent when they
finish, but only while the runner holding them is alive. When that runner goes
away, the server records the loss on the session row
(``background_work_lost_at``) as it drops the stored background flag, and this
module tells the session: it posts a notice through the server's user-message
path, which relaunches a host-bound session's runner to take it. The notice
clears the record; a session that can't take it stays flagged, so the session
list shows it.

The server configures this module when it registers its message poster; until
then :func:`resume` leaves the record in place.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from omnigent.entities import Conversation
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)

NOTICE = (
    "[System: this session's runner went away while it had background work pending "
    "(a background shell or a background agent), and that work was lost with it. "
    "Nothing will report its result. Check what it finished, and redo what it did not.]"
)

PostMessage = Callable[[str, str | None, str], Awaitable[object]]

_store: ConversationStore | None = None
_permissions: PermissionStore | None = None
_post: PostMessage | None = None
# custom-lint: disable-next=workspace-scoped-cache -- task objects
_tasks: set[asyncio.Task[bool]] = set()
# Sessions a notice is being posted to; a relaunch can take minutes.
# custom-lint: disable-next=workspace-scoped-cache -- keys are globally unique session ids
_posting: set[str] = set()


def configure(
    store: ConversationStore | None,
    permissions: PermissionStore | None,
    post: PostMessage | None,
) -> None:
    """Bind the stores and the server's message poster; ``None`` turns resuming off."""
    global _store, _permissions, _post
    _store, _permissions, _post = store, permissions, post
    _posting.clear()


def _resumable(conv: Conversation | None) -> bool:
    """Whether *conv* lost background work and is a session a notice can resume."""
    from omnigent.server.routes._sessions.common import _intentional_stop_sessions

    return (
        conv is not None
        and conv.background_work_lost_at is not None
        and not conv.archived
        # A sub-agent's result belongs to its parent, which it would mislead.
        and conv.kind != "sub_agent"
        and conv.id not in _intentional_stop_sessions
    )


async def resume(session_id: str) -> bool:
    """
    Tell *session_id* its background work was lost, if the row records a loss.

    Best-effort: never raises. The record is cleared only once the notice was
    accepted, and only while it names the same loss. A call while another is
    posting to the session does nothing, so one loss gets one notice.

    :param session_id: The session whose runner went away.
    :returns: Whether the notice was posted.
    """
    store, post = _store, _post
    if store is None or post is None or session_id in _posting:
        return False
    _posting.add(session_id)
    try:
        conv = await asyncio.to_thread(store.get_conversation, session_id)
        if conv is None or not _resumable(conv):
            return False
        lost_at = conv.background_work_lost_at
        from omnigent.server.routes._auth_helpers import get_session_owner_id

        owner = await asyncio.to_thread(get_session_owner_id, session_id, _permissions)
        _logger.info(
            "Resuming session %s, whose runner went away with its background work",
            session_id,
            extra={"session_id": session_id},
        )
        await post(session_id, owner, NOTICE)
        await asyncio.to_thread(store.clear_background_work_lost, session_id, lost_at)
        return True
    except Exception:  # the record stays, so the session stays flagged
        _logger.warning(
            "Could not resume session %s after its background work was lost",
            session_id,
            exc_info=True,
            extra={"session_id": session_id},
        )
        return False
    finally:
        _posting.discard(session_id)


def resume_soon(session_id: str) -> None:
    """Start :func:`resume` without waiting on it: a relaunch can take minutes."""
    if _store is None or _post is None:
        return
    task = asyncio.get_running_loop().create_task(
        resume(session_id), name=f"resume-lost-background-work:{session_id}"
    )
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
