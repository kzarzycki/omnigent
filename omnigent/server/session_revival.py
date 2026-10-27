"""Relaunch a host-bound session whose runner died while the server holds work for it.

Two callers: a sub-agent's result or loss notice for a parent with no reachable
runner, and a top-level session whose runner died mid-turn. Both take the
``retry_session`` ladder (relaunch on the session's host, then session init),
single-flight per session. The relaunched runner's init recovers the rest: it
re-queues undelivered sub-agent results and wakes the parent.

Whether the session continues an interrupted turn is decided once, from current
state, as its runner session init is posted (:func:`take_resume`): it does when
its runner was recorded dying mid-turn (:func:`note_interrupted`, before the
failure is published) and no person has cancelled that turn or sent a message
since (:func:`supersede`). Every init path (this relaunch, the runner
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
from dataclasses import dataclass
from typing import Any

from omnigent.entities import Conversation
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)

MAX_REVIVES_PER_HOUR = 3


@dataclass(eq=False)
class _Resume:
    """One interrupted turn being resumed by an init in flight."""

    runner_id: str


_app: Any = None
_store: ConversationStore | None = None
_permissions: PermissionStore | None = None
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
# Session → the resume an init is posting now; identity tells a superseded one apart.
# custom-lint: disable-next=workspace-scoped-cache -- keys are globally unique session ids
_taken: dict[str, _Resume] = {}
# ponytail: grows by one pair per runner death; persist on the row to bound it.
# custom-lint: disable-next=workspace-scoped-cache -- keys are globally unique session ids
_noted: set[tuple[str, str]] = set()


class RevivalWithdrawn(Exception):
    """The session was stopped or archived before its automatic relaunch launched."""


def configure(
    app: Any, store: ConversationStore | None, permissions: PermissionStore | None = None
) -> None:
    """Bind the server app and stores; ``configure(None, None)`` turns revival off."""
    global _app, _store, _permissions
    _app, _store, _permissions = app, store, permissions
    _tasks.clear()
    _tried_runners.clear()
    _attempts.clear()
    _interrupted.clear()
    _taken.clear()
    _noted.clear()


def _stopped(session_id: str, runner_id: str | None) -> bool:
    """Whether *runner_id* went away because someone stopped *session_id* on purpose."""
    from omnigent.server.routes._sessions.common import _intentional_stop_sessions

    return runner_id is not None and _intentional_stop_sessions.get(session_id) == runner_id


def note_interrupted(session_id: str, runner_id: str | None) -> None:
    """Record that *runner_id* died while *session_id*'s turn was running."""
    if runner_id and (session_id, runner_id) not in _noted:
        # One record per dead runner: a second sighting must not resume twice.
        _noted.add((session_id, runner_id))
        _interrupted[session_id] = runner_id


def supersede(session_id: str) -> None:
    """A person's message replaces the interrupted turn: forget it, taken or not."""
    _interrupted.pop(session_id, None)
    _taken.pop(session_id, None)


def take_resume(conv: Conversation) -> _Resume | None:
    """
    Take the interrupted turn the init being posted now should continue.

    A sub-agent's continuation belongs to its parent's recovery, and a turn a
    person cancelled is not continued; both drop the record.

    :param conv: The session being initialized.
    :returns: A token for :func:`settle_resume` when the init should resume, else ``None``.
    """
    from omnigent.server.routes._sessions.common import _interrupt_fenced_sessions

    runner_id = _interrupted.pop(conv.id, None)
    if runner_id is None or conv.parent_conversation_id is not None:
        return None
    if conv.id in _interrupt_fenced_sessions:
        return None
    _taken[conv.id] = token = _Resume(runner_id)
    return token


def settle_resume(session_id: str, token: _Resume, *, delivered: bool) -> None:
    """
    Finish a taken resume; an undelivered one waits for the session's next init.

    Nothing is put back once a message superseded it (:func:`supersede`) or a
    person cancelled the turn.
    """
    from omnigent.server.routes._sessions.common import _interrupt_fenced_sessions

    if _taken.get(session_id) is not token:
        return
    del _taken[session_id]
    if not delivered and session_id not in _interrupt_fenced_sessions:
        _interrupted.setdefault(session_id, token.runner_id)


async def check_launch(
    session_id: str, runner_ids: tuple[str | None, ...], store: ConversationStore
) -> None:
    """
    Veto an automatic relaunch's host launch for a session stopped or archived since.

    Called right before the launch frame is sent; a launch a person asked for
    isn't vetoed. A read too slow to trust withdraws the relaunch; the session's
    next message launches it.

    :param session_id: The session being launched.
    :param runner_ids: The runners a stop may name: the one replaced and its successor.
    :param store: Store to re-read the row from.
    :raises RevivalWithdrawn: The relaunch must not launch.
    """
    from omnigent.server.routes import sessions as _facade

    if _reviving.get() != session_id:
        return
    # Well inside the archive undo grace: an archive that commits during the read
    # either is seen, or its deferred stop fires after this frame and reaps it.
    bound = min(2.0, _facade._ARCHIVE_STOP_UNDO_GRACE_S / 4)
    try:
        row = await asyncio.wait_for(
            asyncio.to_thread(store.get_conversation, session_id), timeout=bound
        )
    except TimeoutError:
        _logger.warning(
            "Not relaunching session %s: its row didn't read back within %.1fs",
            session_id,
            bound,
            extra={"session_id": session_id},
        )
        raise RevivalWithdrawn(session_id) from None
    if row is None or row.archived or any(_stopped(session_id, r) for r in runner_ids):
        raise RevivalWithdrawn(session_id)


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
    from omnigent.server.routes._auth_helpers import get_session_owner_id
    from omnigent.server.routes.sessions.routes_events import (
        _retry_session_single_flight,
        _ServerEventContext,
    )

    assert _store is not None
    _reviving.set(session_id)
    try:
        conv = await asyncio.to_thread(_store.get_conversation, session_id)
        # The relaunch acts for the session's owner, so side-chat access checks apply.
        owner = await asyncio.to_thread(get_session_owner_id, session_id, _permissions)
        await _retry_session_single_flight(
            request=_ServerEventContext(_app, Headers(), owner),  # type: ignore[arg-type]
            session_id=session_id,
            conversation=conv,
            conversation_store=_store,
            runner_router=get_runner_router(),
            user_id=owner,
            permission_store=_permissions,
        )
    except RevivalWithdrawn:
        _logger.info(
            "Not relaunching session %s: it was stopped or archived",
            session_id,
            extra={"session_id": session_id},
        )
    except Exception:
        _logger.warning(
            "Could not relaunch session %s's runner",
            session_id,
            exc_info=True,
            extra={"session_id": session_id},
        )
