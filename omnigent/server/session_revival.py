"""Relaunch a host-bound session whose runner died while the server holds work for it.

Two callers: a sub-agent's result or loss notice for a parent with no reachable
runner, and a top-level session whose runner died mid-turn. Both take the
``retry_session`` ladder (relaunch on the session's host, then session init),
single-flight per session. The relaunched runner's init recovers the rest: it
re-queues undelivered sub-agent results and wakes the parent, and with
``resume_interrupted_turn`` it tells the session its turn was interrupted.

Bounds, so a session that can't come back never loops: one attempt per dead
runner (the session's ``runner_id`` when it was asked for), plus at most one
more to resume its turn, at most :data:`MAX_REVIVES_PER_HOUR` per session, and
never for an archived, deleted, unhosted or deliberately stopped session. A
turn a person cancelled is never resumed.

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
# Whether a running relaunch should resume the turn; a joined caller can only turn it on.
# custom-lint: disable-next=workspace-scoped-cache -- keys are globally unique session ids
_resume: dict[str, bool] = {}
# ponytail: per-process memory; a server restart forgets attempts. Persist on the
# session row if restarts ever turn into a revive loop.
# custom-lint: disable-next=workspace-scoped-cache -- keys are globally unique session/runner ids
_tried_runners: set[tuple[str, str]] = set()
# Dead runners whose session was already told its turn was interrupted.
# custom-lint: disable-next=workspace-scoped-cache -- keys are globally unique session/runner ids
_resumed_runners: set[tuple[str, str]] = set()
# custom-lint: disable-next=workspace-scoped-cache -- keys are globally unique session ids
_attempts: dict[str, list[float]] = {}


def configure(app: Any, store: ConversationStore | None) -> None:
    """Bind the server app and store; ``configure(None, None)`` turns revival off."""
    global _app, _store
    _app, _store = app, store
    _tasks.clear()
    _resume.clear()
    _tried_runners.clear()
    _resumed_runners.clear()
    _attempts.clear()


def _stopped(session_id: str, runner_id: str) -> bool:
    """Whether *runner_id* went away because someone stopped *session_id* on purpose."""
    from omnigent.server.routes._sessions.common import _intentional_stop_sessions

    return _intentional_stop_sessions.get(session_id) == runner_id


def _cancelled(session_id: str) -> bool:
    """Whether a person stopped the session's turn, so it must not be continued."""
    from omnigent.server.routes._sessions.common import _interrupt_fenced_sessions

    return session_id in _interrupt_fenced_sessions


def revive(
    conv: Conversation,
    *,
    why: str,
    resume_interrupted_turn: bool = False,
    dead_runner_id: str | None = None,
) -> bool:
    """
    Start, or join, a relaunch of *conv*'s runner.

    A caller without ``resume_interrupted_turn`` has a result for the session to
    receive. A joined caller asking for resume gets it, also after a relaunch for
    the same dead runner finished without it.

    :param conv: The session row, as last read.
    :param dead_runner_id: The runner that died; defaults to ``conv.runner_id``.
    :param why: What the relaunch delivers, for the log, e.g. ``"sub-agent conv_x lost"``.
    :param resume_interrupted_turn: Tell the session its turn was interrupted.
    :returns: ``True`` while a relaunch is running for it; ``False`` when it won't be revived.
    """
    dead_runner_id = dead_runner_id or conv.runner_id
    if _app is None or _store is None or conv.archived or not conv.host_id or not dead_runner_id:
        return False
    cause = (conv.id, dead_runner_id)
    if _stopped(*cause):
        return False
    resume = resume_interrupted_turn and not _cancelled(conv.id)
    if resume_interrupted_turn and not resume:
        return False
    if conv.id in _tasks:
        _resume[conv.id] = _resume[conv.id] or resume
        return True
    if cause in _tried_runners:
        # Relaunched already for a result; only the interrupted turn may still be owed.
        if not resume or cause in _resumed_runners:
            return False
    else:
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
    _resume[conv.id] = resume
    task = asyncio.get_running_loop().create_task(_relaunch(*cause), name=f"revive:{conv.id}")
    _tasks[conv.id] = task

    def _done(_task: asyncio.Task[None]) -> None:
        _tasks.pop(conv.id, None)
        _resume.pop(conv.id, None)

    task.add_done_callback(_done)
    return True


async def _relaunch(session_id: str, dead_runner_id: str) -> None:
    from starlette.datastructures import Headers

    from omnigent.runtime import get_runner_router
    from omnigent.server.routes.sessions import (
        _ensure_runner_session_initialized,
        _get_runner_client,
    )
    from omnigent.server.routes.sessions.routes_events import (
        _retry_session_single_flight,
        _ServerEventContext,
    )

    assert _store is not None
    resumed = False

    def _resume_now() -> bool:
        # Read as the new runner initializes, so a caller that joined meanwhile counts.
        nonlocal resumed
        resumed = _resume.get(session_id, False) and not _cancelled(session_id)
        return resumed

    try:
        conv = await asyncio.to_thread(_store.get_conversation, session_id)
        if conv is None or conv.archived or _stopped(session_id, dead_runner_id):
            return
        router = get_runner_router()
        await _retry_session_single_flight(
            request=_ServerEventContext(_app, Headers(), None),  # type: ignore[arg-type]
            session_id=session_id,
            conversation_store=_store,
            runner_router=router,
            resume_interrupted_turn=_resume_now,
        )
        if resumed or not _resume_now():
            return
        # Resume was asked after the runner came up; send it on the live runner.
        conv = await asyncio.to_thread(_store.get_conversation, session_id)
        client = await _get_runner_client(session_id, router)
        if conv is None or client is None or conv.runner_id == dead_runner_id:
            return
        await _ensure_runner_session_initialized(
            session_id,
            conv,
            client,
            _store,
            initializer=getattr(_app.state, "runner_session_initializer", None),
            resume_interrupted_turn=True,
            require_success=True,
        )
    except Exception:  # noqa: BLE001 - a session that can't come back stays down, logged
        _logger.warning(
            "Could not relaunch session %s's runner",
            session_id,
            exc_info=True,
            extra={"session_id": session_id},
        )
    finally:
        if resumed:
            _resumed_runners.add((session_id, dead_runner_id))
