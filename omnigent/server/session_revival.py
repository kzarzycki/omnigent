"""Relaunch a host-bound session whose runner died while the server holds work for it.

Two callers: a sub-agent's result or loss notice for a parent with no reachable
runner, and a top-level session whose runner died mid-turn. Both take the
``retry_session`` ladder (relaunch on the session's host, then session init),
single-flight per session. The relaunched runner's init recovers the rest: it
re-queues undelivered sub-agent results and wakes the parent, and with
``resume_interrupted_turn`` it tells the session its turn was interrupted.

Bounds, so a session that can't come back never loops: one attempt per dead
runner (the session's ``runner_id`` when it was asked for), at most
:data:`MAX_REVIVES_PER_HOUR` per session, and never for an archived, deleted or
unhosted session.

The server configures this module in its lifespan; until then :func:`revive`
refuses.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from omnigent.entities import Conversation
from omnigent.stores import ConversationStore

_logger = logging.getLogger(__name__)

MAX_REVIVES_PER_HOUR = 3

_app: Any = None
_store: ConversationStore | None = None
# custom-lint: disable-next=workspace-scoped-cache -- task objects
_tasks: dict[str, asyncio.Task[None]] = {}
# ponytail: per-process memory; a server restart forgets attempts. Persist on the
# session row if restarts ever turn into a revive loop.
# custom-lint: disable-next=workspace-scoped-cache -- keys are globally unique session/runner ids
_tried_runners: set[tuple[str, str]] = set()
# custom-lint: disable-next=workspace-scoped-cache -- keys are globally unique session ids
_attempts: dict[str, list[float]] = {}


def configure(app: Any, store: ConversationStore | None) -> None:
    """Bind the server app and store; ``configure(None, None)`` turns revival off."""
    global _app, _store
    _app, _store = app, store
    _tasks.clear()
    _tried_runners.clear()
    _attempts.clear()


def revive(conv: Conversation, *, why: str, resume_interrupted_turn: bool = False) -> bool:
    """
    Start, or join, a relaunch of *conv*'s runner.

    :param conv: The session row, as last read; its ``runner_id`` is the dead runner.
    :param why: What the relaunch delivers, for the log, e.g. ``"sub-agent conv_x lost"``.
    :param resume_interrupted_turn: Tell the session its turn was interrupted.
    :returns: ``True`` while a relaunch is running for it; ``False`` when it won't be revived.
    """
    if _app is None or _store is None or conv.archived or not conv.host_id or not conv.runner_id:
        return False
    if conv.id in _tasks:
        return True
    cause = (conv.id, conv.runner_id)
    if cause in _tried_runners:
        return False
    now = time.monotonic()
    recent = [t for t in _attempts.get(conv.id, []) if now - t < 3600]
    if len(recent) >= MAX_REVIVES_PER_HOUR:
        _attempts[conv.id] = recent
        return False
    _attempts[conv.id] = [*recent, now]
    _tried_runners.add(cause)
    _logger.info(
        "Relaunching session %s's runner: %s", conv.id, why, extra={"session_id": conv.id}
    )
    task = asyncio.get_running_loop().create_task(
        _relaunch(conv.id, resume_interrupted_turn), name=f"revive:{conv.id}"
    )
    _tasks[conv.id] = task
    task.add_done_callback(lambda _done: _tasks.pop(conv.id, None))
    return True


async def _relaunch(session_id: str, resume_interrupted_turn: bool) -> None:
    from starlette.datastructures import Headers

    from omnigent.runtime import get_runner_router
    from omnigent.server.routes.sessions.routes_events import (
        _retry_session_single_flight,
        _ServerEventContext,
    )

    assert _store is not None
    try:
        await _retry_session_single_flight(
            request=_ServerEventContext(_app, Headers(), None),  # type: ignore[arg-type]
            session_id=session_id,
            conversation_store=_store,
            runner_router=get_runner_router(),
            resume_interrupted_turn=resume_interrupted_turn,
        )
    except Exception:  # noqa: BLE001 - a session that can't come back stays down, logged
        _logger.warning(
            "Could not relaunch session %s's runner",
            session_id,
            exc_info=True,
            extra={"session_id": session_id},
        )
