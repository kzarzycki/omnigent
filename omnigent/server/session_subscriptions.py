"""Tell subscribed sessions when a watched session ends a turn or blocks on a prompt.

A subscription is a server-side row ``(subscriber, target)``. When the target
ends a turn (``completed`` / ``cancelled`` / ``failed``) or raises an approval
prompt, every subscriber gets a one-line ``[System: …]`` notice posted as a
``message`` event, through the same path a user message takes, so a subscriber
whose runner was idle-reaped is relaunched to receive it. Each event is claimed
once per subscriber in the store (a turn by its response id, a prompt by its
elicitation id), so a repeated status edge never delivers twice.

The server configures this module once in ``create_app``; until then every
entry point is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Literal

from omnigent.entities import Conversation
from omnigent.harness_aliases import is_native_harness
from omnigent.runtime import pending_elicitations
from omnigent.runtime.subagent_block_notifier import _block_reason
from omnigent.server.auth import LEVEL_READ
from omnigent.server.permissions import check_session_access
from omnigent.server.subagent_activity import latest_turn_id
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)

TurnOutcome = Literal["completed", "cancelled", "failed"]

# Posts ``text`` into a session as a user message, acting as ``user_id``.
PostMessage = Callable[[str, str | None, str], Awaitable[object]]

_store: ConversationStore | None = None
_permission_store: PermissionStore | None = None
_post_message: PostMessage | None = None
# Strong refs so fire-and-forget deliveries aren't collected mid-flight.
# custom-lint: disable-next=workspace-scoped-cache -- task objects
_tasks: set[asyncio.Task[None]] = set()
_MAX_PARENT_HOPS = 32


def configure(
    conversation_store: ConversationStore | None,
    permission_store: PermissionStore | None,
    post_message: PostMessage | None,
) -> None:
    """
    Wire the stores and the message poster; ``None`` store disables delivery.

    :param conversation_store: Store holding the subscription rows.
    :param permission_store: Re-checks that a subscriber's owner may still read
        the target at delivery; ``None`` when permissions are disabled.
    :param post_message: Posts a notice into a session as a ``message`` event.
    :returns: None.
    """
    global _store, _permission_store, _post_message
    _store = conversation_store
    _permission_store = permission_store
    _post_message = post_message
    pending_elicitations.add_elicitation_observer(observe_elicitation)


def turn_notice(title: str | None, target_id: str, outcome: str) -> str:
    """The notice a subscriber gets when the target ends a turn."""
    return (
        f"[System: session {_label(title, target_id)} ended its turn ({outcome}). "
        "Read it with sys_session_get_history.]"
    )


def prompt_notice(title: str | None, target_id: str, reason: str | None) -> str:
    """The notice a subscriber gets when the target blocks on a prompt."""
    detail = f": {reason}" if reason else ""
    return f"[System: session {_label(title, target_id)} is waiting on a prompt{detail}.]"


def _label(title: str | None, target_id: str) -> str:
    return f"{title} ({target_id})" if title else f"({target_id})"


def notify_turn_end(
    target_id: str,
    outcome: TurnOutcome,
    response_id: str | None,
    *,
    from_runner: bool = False,
) -> None:
    """
    Schedule turn-end notices to the target's subscribers. Never raises.

    :param target_id: Session whose turn ended.
    :param outcome: How it ended.
    :param response_id: The turn's response id; ``None`` reads it from the
        target's transcript.
    :param from_runner: The edge is the runner's relayed turn end. For a native
        harness a ``completed`` one only acknowledges prompt delivery, so it is
        dropped; the forwarder's confirmed edge reports the real end.
    :returns: None.
    """
    if _store is None:
        return
    _spawn(_deliver_turn_end(target_id, outcome, response_id, from_runner=from_runner))


def observe_elicitation(conversation_id: str, event: dict[str, Any]) -> None:
    """Pending-elicitation observer: schedule ``blocked`` notices for a new prompt."""
    if _store is None or event.get("type") != "response.elicitation_request":
        return
    elicitation_id = event.get("elicitation_id")
    if not isinstance(elicitation_id, str) or not elicitation_id:
        return
    _spawn(_deliver_prompt(conversation_id, elicitation_id, _block_reason(event)))


async def deliver_pending_prompts(subscriber_id: str, target_id: str) -> None:
    """
    Tell a new subscriber about prompts the target is already blocked on.

    :param subscriber_id: The session that just subscribed.
    :param target_id: The session it watches.
    :returns: None.
    """
    for event in pending_elicitations.snapshot_for(target_id):
        elicitation_id = event.get("elicitation_id")
        if isinstance(elicitation_id, str) and elicitation_id:
            _spawn(
                _deliver_prompt(
                    target_id, elicitation_id, _block_reason(event), only=subscriber_id
                )
            )


def _spawn(coro: Coroutine[Any, Any, None]) -> None:
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        # ponytail: publishes from a thread without a loop are dropped; route them
        # through the server loop if one ever needs delivering.
        coro.close()
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _deliver_turn_end(
    target_id: str,
    outcome: TurnOutcome,
    response_id: str | None,
    *,
    from_runner: bool = False,
) -> None:
    from omnigent.server.routes._sessions.orchestration import _native_pane_harness

    store = _store
    if store is None:
        return
    try:
        if not await asyncio.to_thread(store.list_session_subscribers, target_id):
            return
        target = await asyncio.to_thread(store.get_conversation, target_id)
        if target is None or target.archived:
            return
        if from_runner and outcome == "completed":
            harness = await asyncio.to_thread(_native_pane_harness, target)
            if is_native_harness(harness):
                return
        # An interrupted turn may have written only the user's message.
        key = response_id or await latest_turn_id(target_id, store, failed=outcome != "completed")
        if key is None:
            if outcome == "completed":
                return
            # A runner can die or be stopped before the turn wrote anything.
            key = target_id
        await _fan_out(target_id, "turn", key, turn_notice(target.title, target_id, outcome))
    except Exception:  # noqa: BLE001 — a notice must never break the status path
        _logger.warning("Could not notify subscribers of %s", target_id, exc_info=True)


async def _deliver_prompt(
    target_id: str, elicitation_id: str, reason: str | None, *, only: str | None = None
) -> None:
    store = _store
    if store is None:
        return
    try:
        if only is None and not await asyncio.to_thread(store.list_session_subscribers, target_id):
            return
        target = await asyncio.to_thread(store.get_conversation, target_id)
        if target is None or target.archived:
            return
        notice = prompt_notice(target.title, target_id, reason)
        await _fan_out(target_id, "prompt", elicitation_id, notice, only=only)
    except Exception:  # noqa: BLE001 — a notice must never break the publish path
        _logger.warning("Could not notify subscribers of %s", target_id, exc_info=True)


def _owner(store: ConversationStore, session: Conversation) -> str | None:
    """The session's owner; a sub-agent's grants live on its root session."""
    for _ in range(_MAX_PARENT_HOPS):
        owner = store.get_session_owner(session.id)
        if owner is not None or session.parent_conversation_id is None:
            return owner
        parent = store.get_conversation(session.parent_conversation_id)
        if parent is None:
            return None
        session = parent
    return None


async def _fan_out(
    target_id: str, kind: str, key: str, notice: str, *, only: str | None = None
) -> None:
    store = _store
    assert store is not None
    subscribers = (
        [only]
        if only is not None
        else await asyncio.to_thread(store.list_session_subscribers, target_id)
    )
    await asyncio.gather(
        *(
            _deliver_one(subscriber_id, target_id, kind, key, notice)
            for subscriber_id in subscribers
        )
    )


async def _deliver_one(
    subscriber_id: str, target_id: str, kind: str, key: str, notice: str
) -> None:
    store = _store
    post = _post_message
    if store is None or post is None:
        return
    try:
        subscriber = await asyncio.to_thread(store.get_conversation, subscriber_id)
        if subscriber is None or subscriber.archived:
            return
        owner = await asyncio.to_thread(_owner, store, subscriber)
        permission_store = _permission_store
        if permission_store is not None and not await asyncio.to_thread(
            check_session_access, owner, target_id, LEVEL_READ, permission_store, store
        ):
            return
        if not await asyncio.to_thread(
            store.claim_session_subscription_event, subscriber_id, target_id, kind, key
        ):
            return
    except Exception:  # noqa: BLE001
        _logger.warning(
            "Could not prepare a notice from %s for %s",
            target_id,
            subscriber_id,
            exc_info=True,
        )
        return
    try:
        await post(subscriber_id, owner, notice)
    except Exception:  # noqa: BLE001 — release so a repeated edge can retry
        _logger.warning(
            "Could not deliver a notice from %s to %s",
            target_id,
            subscriber_id,
            exc_info=True,
            extra={"session_id": subscriber_id},
        )
        await asyncio.to_thread(
            store.release_session_subscription_event, subscriber_id, target_id, kind, key
        )
