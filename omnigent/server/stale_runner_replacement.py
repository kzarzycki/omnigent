"""Replace a host's adopted runner on an older build once its session is idle.

A host daemon that adopts runners after an update offers each one on another
build through ``host.replace_runner``. The server, which sees the session's
activity, replaces the runner only when the session is idle and not driven by
its owner: it launches a runner on the host's current build, stops the old one
and resumes the session on the new runner with the ``retry_session`` recovery.
A new runner that fails to come up is stopped and the old runner, still running
and untouched, is bound again.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from omnigent.entities.conversation import Conversation
from omnigent.host.frames import HostReplaceRunnerFrame, HostReplaceRunnerResultFrame
from omnigent.stores import ConversationStore

_logger = logging.getLogger(__name__)

# An owner message this recent means the owner is driving the session.
OWNER_ACTIVE_WINDOW_S = 30 * 60
# Newest messages read to find the last user message.
_RECENT_MESSAGE_PAGE = 50
# How long the new runner gets to connect its tunnel.
_NEW_RUNNER_CONNECT_TIMEOUT_S = 120.0
_MID_TURN_STATUSES = frozenset({"running", "waiting"})


@dataclass(frozen=True)
class ReplacementDeps:
    """What a replacement calls into; the server wires the session machinery.

    :param store: Conversation store.
    :param launch: Launches a runner for a session on its host without stopping
        the bound one; returns ``(new_runner_id, error)``.
    :param wait_connected: Whether a runner connects its tunnel within a timeout.
    :param is_connected: Whether a runner's tunnel is connected now.
    :param stop: Stops a runner on a host: ``(session_id, host_id, runner_id, reason)``.
    :param resume: Resumes a session on its bound runner, as ``retry_session`` does.
    :param status_of: The session status this replica last saw, if any.
    :param has_queued_input: Whether the session has a message waiting for its turn.
    :param has_terminal_attach: Whether a viewer has its terminal open for input.
    """

    store: ConversationStore
    launch: Callable[[Conversation], Awaitable[tuple[str, str | None]]]
    wait_connected: Callable[[str, float], Awaitable[bool]]
    is_connected: Callable[[str], bool]
    stop: Callable[[str, str, str, str], Awaitable[Any]]
    resume: Callable[[Conversation], Awaitable[Any]]
    status_of: Callable[[str], str | None]
    has_queued_input: Callable[[str], bool]
    has_terminal_attach: Callable[[str], bool]


def _message_role(item: Any) -> str | None:
    role = getattr(getattr(item, "data", None), "role", None)
    return role if isinstance(role, str) else None


def _has_recent_user_message(store: ConversationStore, session_id: str, now: float) -> bool:
    """Whether a person wrote to the session within :data:`OWNER_ACTIVE_WINDOW_S`.

    A page of newest messages with no user message, but reaching into the
    window, counts as recent: leaving a session alone is the safe answer.
    """
    page = store.list_items(session_id, limit=_RECENT_MESSAGE_PAGE, order="desc", type="message")
    items = list(page.data)
    for item in items:
        if _message_role(item) == "user":
            return now - item.created_at < OWNER_ACTIVE_WINDOW_S
    full_page = len(items) == _RECENT_MESSAGE_PAGE
    return full_page and now - items[-1].created_at < OWNER_ACTIVE_WINDOW_S


def session_blocker(conv: Conversation, deps: ReplacementDeps, now: float) -> str | None:
    """Why *conv* must keep its runner now, or ``None`` when it may be replaced.

    :param conv: A session bound to the runner.
    :param deps: Session state readers.
    :param now: Current Unix time.
    :returns: A reason such as ``"turn_running"``, or ``None``.
    """
    if conv.live_status in _MID_TURN_STATUSES or deps.status_of(conv.id) in _MID_TURN_STATUSES:
        return "turn_running"
    if conv.background_work_running:
        return "background_work_running"
    if deps.has_queued_input(conv.id) or deps.store.list_pending_inputs(conv.id):
        return "turn_queued"
    if conv.open_questions:
        return "pending_question"
    if conv.pending_elicitation_count:
        return "pending_elicitation"
    if deps.has_terminal_attach(conv.id):
        return "terminal_attached"
    if _has_recent_user_message(deps.store, conv.id, now):
        return "owner_active"
    return None


def _runner_blocker(sessions: Sequence[Conversation], deps: ReplacementDeps) -> str | None:
    now = time.time()
    for conv in sessions:
        if (reason := session_blocker(conv, deps, now)) is not None:
            return reason
    return None


async def _roll_back(
    conv: Conversation, old_runner_id: str, new_runner_id: str, deps: ReplacementDeps
) -> bool:
    """Bind the session to its old runner again and stop the new one.

    :returns: Whether the old runner is still connected.
    """
    if new_runner_id != old_runner_id:
        await asyncio.to_thread(
            deps.store.replace_runner_id,
            conv.id,
            old_runner_id,
            expected_runner_id=new_runner_id,
        )
        if conv.host_id is not None:
            await deps.stop(conv.id, conv.host_id, new_runner_id, "replacement_failed")
    return deps.is_connected(old_runner_id)


async def replace_stale_runner(
    host_id: str, frame: HostReplaceRunnerFrame, deps: ReplacementDeps
) -> HostReplaceRunnerResultFrame:
    """Replace the runner *frame* names if its sessions are idle.

    :param host_id: The host that asked; the session must be bound to it.
    :param frame: The host's request.
    :param deps: Session machinery.
    :returns: ``replaced``, ``skipped`` (busy or owner-driven: ask later) or
        ``failed`` (the host stops asking until it restarts).
    """

    def result(status: str, reason: str | None = None) -> HostReplaceRunnerResultFrame:
        return HostReplaceRunnerResultFrame(frame.request_id, status, reason)

    old_runner_id = frame.runner_id
    conv = await asyncio.to_thread(deps.store.get_conversation, frame.session_id)
    if conv is None or conv.archived:
        return result("failed", "the session no longer exists")
    if conv.host_id != host_id or conv.runner_id != old_runner_id:
        return result("failed", "the session is no longer bound to this runner")
    sessions = await asyncio.to_thread(deps.store.list_conversations_by_runner_id, old_runner_id)
    if (reason := await asyncio.to_thread(_runner_blocker, sessions, deps)) is not None:
        return result("skipped", reason)

    new_runner_id, launch_error = await deps.launch(conv)
    if launch_error is None and not await deps.wait_connected(
        new_runner_id, _NEW_RUNNER_CONNECT_TIMEOUT_S
    ):
        launch_error = "the new runner did not connect"
    if launch_error is not None:
        kept = await _roll_back(conv, old_runner_id, new_runner_id, deps)
        suffix = "; the old runner was kept" if kept else "; the old runner is gone"
        return result("failed", launch_error + suffix)

    # Work may have started while the new runner came up.
    fresh = await asyncio.to_thread(deps.store.list_conversations_by_runner_id, old_runner_id)
    current = await asyncio.to_thread(deps.store.get_conversation, conv.id)
    rechecked = [current or conv, *(other for other in fresh if other.id != conv.id)]
    if (reason := await asyncio.to_thread(_runner_blocker, rechecked, deps)) is not None:
        await _roll_back(conv, old_runner_id, new_runner_id, deps)
        return result("skipped", reason)
    for other in fresh:
        if other.id != conv.id:
            # Sub-agents ride their parent's runner; move them with it.
            await asyncio.to_thread(
                deps.store.replace_runner_id,
                other.id,
                new_runner_id,
                expected_runner_id=old_runner_id,
            )
    await deps.stop(conv.id, host_id, old_runner_id, "superseded")
    bound = await asyncio.to_thread(deps.store.get_conversation, conv.id)
    try:
        await deps.resume(bound or conv)
    except Exception as exc:  # the old runner is stopped; report the failed resume
        _logger.warning(
            "Session %s did not resume on its replacement runner %s",
            conv.id,
            new_runner_id,
            exc_info=True,
            extra={"session_id": conv.id},
        )
        return result("failed", f"the session did not resume on the new runner: {exc}")
    _logger.info(
        "Replaced runner %s (commit %s) with %s (commit %s) for session %s",
        old_runner_id,
        frame.runner_commit or "unknown",
        new_runner_id,
        frame.host_commit or "unknown",
        conv.id,
        extra={"session_id": conv.id},
    )
    return result("replaced")


def server_replacement_deps(
    app: Any,
    store: ConversationStore,
    permission_store: Any,
    host_registry: Any,
    tunnel_registry: Any,
) -> ReplacementDeps:
    """Wire a replacement to the server's launch, stop and ``retry_session`` paths.

    :param app: The FastAPI app, whose state the resume reads.
    :param store: Conversation store.
    :param permission_store: Permission store, for the session owner.
    :param host_registry: Live host connections.
    :param tunnel_registry: Live runner tunnels.
    :returns: The dependencies for :func:`replace_stale_runner`.
    """
    from starlette.datastructures import Headers

    from omnigent.runtime import get_runner_router, pending_inputs
    from omnigent.server.routes._auth_helpers import get_session_owner_id
    from omnigent.server.routes._sessions.common import _session_status_cache
    from omnigent.server.routes._sessions.helpers import (
        _launch_runner_on_host,
        _stop_session_host_runner,
    )
    from omnigent.server.routes.sessions.routes_events import (
        _retry_session_single_flight,
        _ServerEventContext,
    )
    from omnigent.server.routes.terminal_attach import has_write_terminal_attach

    async def launch(conv: Conversation) -> tuple[str, str | None]:
        host_conn = host_registry.get(conv.host_id) if conv.host_id else None
        if host_conn is None:
            return conv.runner_id or "", "the host is offline"
        attempt = await _launch_runner_on_host(
            conv, store, host_registry, host_conn, stop_superseded=False
        )
        if attempt.error_code is not None or attempt.error is not None:
            return attempt.runner_id, attempt.error or attempt.error_code
        if attempt.runner_id == conv.runner_id:
            return attempt.runner_id, "the session's runner changed during the launch"
        return attempt.runner_id, None

    async def wait_connected(runner_id: str, timeout_s: float) -> bool:
        return await tunnel_registry.wait_for_runner(runner_id, timeout_s=timeout_s) is not None

    async def stop(session_id: str, host_id: str, runner_id: str, reason: str) -> bool:
        return await _stop_session_host_runner(
            session_id,
            host_id,
            runner_id,
            host_registry,
            expect_already_stopped=reason != "superseded",
            reason=reason,
        )

    async def resume(conv: Conversation) -> object:
        owner = await asyncio.to_thread(get_session_owner_id, conv.id, permission_store)
        return await _retry_session_single_flight(
            request=_ServerEventContext(app, Headers(), owner),  # type: ignore[arg-type]
            session_id=conv.id,
            conversation=conv,
            conversation_store=store,
            runner_router=get_runner_router(),
            user_id=owner,
            permission_store=permission_store,
        )

    return ReplacementDeps(
        store=store,
        launch=launch,
        wait_connected=wait_connected,
        is_connected=lambda runner_id: tunnel_registry.get(runner_id) is not None,
        stop=stop,
        resume=resume,
        status_of=_session_status_cache.get,
        has_queued_input=pending_inputs.has_pending,
        has_terminal_attach=has_write_terminal_attach,
    )
