"""Relaunch a host-bound session whose runner died while the server holds work for it.

Two callers: a sub-agent's result or loss notice for a parent with no reachable
runner, and a top-level session whose runner died mid-turn. Both take the
``retry_session`` ladder (relaunch on the session's host, then session init),
single-flight per session. The relaunched runner's init recovers the rest: it
re-queues undelivered sub-agent results and wakes the parent.

Whether the session continues an interrupted turn is decided once, from current
state, as its runner session init is posted (:func:`take_resume`): it does when
its runner was recorded dying mid-turn (:func:`note_interrupted`) and no person
has cancelled that turn since. Every init path (this relaunch, the runner
connect hook, a person's retry) asks the same question, so their order doesn't
matter.

Bounds, so a session that can't come back never loops: one attempt per dead
runner, at most :data:`MAX_REVIVES_PER_HOUR` per session, and never for an
archived, deleted or unhosted session. A deliberate stop or an archive vetoes
the launch itself (:func:`check_launch`), however late it lands.

The server configures this module in its lifespan; until then :func:`revive`
refuses.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from typing import Any

from omnigent.entities import Conversation
from omnigent.stores import ConversationStore

_logger = logging.getLogger(__name__)

MAX_REVIVES_PER_HOUR = 3

_app: Any = None
_store: ConversationStore | None = None
# The session an automatic relaunch is launching, for the launch-boundary veto.
_reviving: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "omnigent_session_revival", default=None
)
# custom-lint: disable-next=workspace-scoped-cache -- task objects
_tasks: dict[str, asyncio.Task[None]] = {}
# ponytail: per-process memory; a server restart forgets attempts and interrupted
# turns. Persist on the session row if restarts ever turn into a revive loop.
# custom-lint: disable-next=workspace-scoped-cache -- keys are globally unique session/runner ids
_tried_runners: set[tuple[str, str]] = set()
# custom-lint: disable-next=workspace-scoped-cache -- keys are globally unique session ids
_attempts: dict[str, list[float]] = {}
# Session → the runner that died while its turn was running.
# custom-lint: disable-next=workspace-scoped-cache -- keys are globally unique session ids
_interrupted: dict[str, str] = {}


class RevivalWithdrawn(Exception):
    """The session was stopped or archived before its automatic relaunch launched."""


def configure(app: Any, store: ConversationStore | None) -> None:
    """Bind the server app and store; ``configure(None, None)`` turns revival off."""
    global _app, _store
    _app, _store = app, store
    _tasks.clear()
    _tried_runners.clear()
    _attempts.clear()
    _interrupted.clear()


def _stopped(session_id: str, runner_id: str | None) -> bool:
    """Whether *runner_id* went away because someone stopped *session_id* on purpose."""
    from omnigent.server.routes._sessions.common import _intentional_stop_sessions

    return runner_id is not None and _intentional_stop_sessions.get(session_id) == runner_id


def note_interrupted(session_id: str, runner_id: str | None) -> None:
    """Record that *runner_id* died while *session_id*'s turn was running."""
    if runner_id:
        _interrupted[session_id] = runner_id


def take_resume(session_id: str, *, suppressed: bool) -> str | None:
    """
    Whether the init being posted now should continue an interrupted turn.

    Consumes the record: one init carries it, and a failed post puts it back
    (:func:`note_interrupted`). A message forward (*suppressed*) or a person's
    cancel supersedes the interrupted turn, so the record is dropped.

    :param session_id: The session being initialized.
    :param suppressed: The init precedes a message forward.
    :returns: The runner that died mid-turn when the init should resume, else ``None``.
    """
    from omnigent.server.routes._sessions.common import _interrupt_fenced_sessions

    dead_runner = _interrupted.pop(session_id, None)
    if suppressed or session_id in _interrupt_fenced_sessions:
        return None
    return dead_runner


def check_launch(conv: Conversation) -> None:
    """
    Veto an automatic relaunch's host launch for a session stopped or archived since.

    Called by the host-launch helper with the row it just re-read; a launch a
    person asked for isn't vetoed.

    :param conv: The session row as re-read under the launch lock.
    :raises RevivalWithdrawn: The relaunch must not launch.
    """
    if _reviving.get() == conv.id and (conv.archived or _stopped(conv.id, conv.runner_id)):
        raise RevivalWithdrawn(conv.id)


def revive(conv: Conversation, *, why: str) -> bool:
    """
    Start, or join, a relaunch of *conv*'s runner.

    :param conv: The session row, as last read; its ``runner_id`` is the dead runner.
    :param why: What the relaunch is for, for the log, e.g. ``"sub-agent conv_x lost"``.
    :returns: ``True`` while a relaunch is running for it; ``False`` when it won't be revived.
    """
    if _app is None or _store is None or conv.archived or not conv.host_id or not conv.runner_id:
        return False
    cause = (conv.id, conv.runner_id)
    if _stopped(*cause):
        return False
    if conv.id in _tasks:
        return True
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
    task = asyncio.get_running_loop().create_task(_relaunch(conv.id), name=f"revive:{conv.id}")
    _tasks[conv.id] = task
    task.add_done_callback(lambda _done: _tasks.pop(conv.id, None))
    return True


async def _relaunch(session_id: str) -> None:
    from starlette.datastructures import Headers

    from omnigent.runtime import get_runner_router
    from omnigent.server.routes.sessions.routes_events import (
        _retry_session_single_flight,
        _ServerEventContext,
    )

    assert _store is not None
    _reviving.set(session_id)
    try:
        await _retry_session_single_flight(
            request=_ServerEventContext(_app, Headers(), None),  # type: ignore[arg-type]
            session_id=session_id,
            conversation_store=_store,
            runner_router=get_runner_router(),
        )
    except RevivalWithdrawn:
        _logger.info(
            "Not relaunching session %s: it was stopped or archived",
            session_id,
            extra={"session_id": session_id},
        )
    except Exception:  # noqa: BLE001 - a session that can't come back stays down, logged
        _logger.warning(
            "Could not relaunch session %s's runner",
            session_id,
            exc_info=True,
            extra={"session_id": session_id},
        )
