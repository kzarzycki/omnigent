"""Tell subscribed sessions when a watched session ends a turn or blocks on a prompt.

A subscription is a server-side row ``(subscriber, target)``. When the target
ends a turn (``completed`` / ``cancelled`` / ``failed``) or raises an approval
prompt, every subscriber gets a one-line ``[System: …]`` notice posted as a
``message`` event, through the same path a user message takes, so a subscriber
whose runner was idle-reaped is relaunched to receive it.

Each notice goes through a durable outbox row keyed by subscriber, target and
event (a turn's response id, a prompt's elicitation id, a native status block).
The producer records the rows before it acknowledges the event, so a recording
failure fails the producer instead of losing the notice; posting is
asynchronous. Inserting the row is the dedupe, so a repeated or replayed event
is a no-op. The row holds only that identity and an outcome code; the notice
text is built from the live sessions when it is posted. A failed post leaves
the row pending; a retry sweep (after
the failure, at startup and periodically) posts it with capped backoff for as
long as the subscription stands, and also picks up an attempt a crashed server
never finished. Delivery is at-least-once: a notice is a wake, so a rare
duplicate costs the reader one history read while a lost one stalls it.
Settled rows are pruned after a retention window, except a prompt's, which is
kept while its target may still have it pending.

The server configures this module once in ``create_app``; until then every
entry point is a no-op.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Literal, cast

from omnigent.db.db_models import current_workspace_id, workspace_scope
from omnigent.entities import Conversation
from omnigent.harness_aliases import is_native_harness
from omnigent.runtime import pending_elicitations
from omnigent.server.auth import LEVEL_READ
from omnigent.server.permissions import check_session_access
from omnigent.server.subagent_activity import latest_turn_id
from omnigent.stores import ConversationStore
from omnigent.stores.conversation_store import SubscriptionDelivery
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
_loop: asyncio.AbstractEventLoop | None = None
# Earliest scheduled in-process retry, so failures share one wake-up.
_next_wake: float | None = None
# Bounds the first attempts at freshly recorded rows; made on the server loop.
_post_limit: asyncio.Semaphore | None = None
# The native status block each target stands on: (blocked_on, response id). Like
# the pending-elicitation index, it lives on the server relaying the target; a
# runner republishes its status when it reconnects, which restores it.
# custom-lint: disable-next=workspace-scoped-cache -- keyed by unique session id
_standing_blocks: dict[str, tuple[str, str | None]] = {}

# An attempt in flight owns its row this long; a server that dies mid-attempt
# leaves the row to the sweep after it. Longer than a relaunching post takes.
_LEASE_S = 300
_RETRY_BASE_S = 1
_RETRY_CAP_S = 600
_SETTLED_RETENTION_S = 7 * 24 * 3600
_SWEEP_INTERVAL_S = 30
_SWEEP_BATCH = 100
_SWEEP_MAX_BATCHES = 10
_PRUNE_BATCH = 500
_SUBSCRIBER_PAGE = 100
_POST_CONCURRENCY = 16
_RECORD_RETRY_CAP_S = 60
_SKIP = object()

# Outcome codes stored in the outbox; the notice is rebuilt from them.
_PROMPT = 0
_TURN_OUTCOMES: dict[int, TurnOutcome] = {1: "completed", 2: "cancelled", 3: "failed"}
_TURN_CODES: dict[str, int] = {outcome: code for code, outcome in _TURN_OUTCOMES.items()}


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
    global _store, _permission_store, _post_message, _next_wake, _post_limit
    _store = conversation_store
    _permission_store = permission_store
    _post_message = post_message
    _next_wake = None
    _post_limit = None
    _standing_blocks.clear()
    pending_elicitations.add_elicitation_observer(observe_elicitation)


def turn_notice(title: str | None, target_id: str, outcome: str) -> str:
    """The notice a subscriber gets when the target ends a turn."""
    return (
        f"[System: session {_label(title, target_id)} ended its turn ({outcome}). "
        "Read it with sys_session_get_history.]"
    )


def prompt_notice(title: str | None, target_id: str) -> str:
    """The notice a subscriber gets when the target blocks on a prompt."""
    return (
        f"[System: session {_label(title, target_id)} is waiting on a prompt. "
        "Read it with sys_session_get_info.]"
    )


def _label(title: str | None, target_id: str) -> str:
    return f"{title} ({target_id})" if title else f"({target_id})"


async def record_turn_end(
    target_id: str,
    outcome: TurnOutcome,
    response_id: str | None,
    *,
    from_runner: bool = False,
) -> None:
    """
    Record turn-end notices to the target's subscribers, then post them in the background.

    :param target_id: Session whose turn ended.
    :param outcome: How it ended.
    :param response_id: The turn's response id; ``None`` reads it from the
        target's transcript.
    :param from_runner: The edge is the runner's relayed turn end. For a native
        harness a ``completed`` one only acknowledges prompt delivery, so it is
        dropped; the forwarder's confirmed edge reports the real end.
    :returns: None.
    :raises Exception: The notices could not be recorded; the caller fails the
        edge so its producer retries it.
    """
    from omnigent.server.routes._sessions.orchestration import _native_pane_harness

    store = _store
    if store is None:
        return
    if not await asyncio.to_thread(store.list_session_subscribers, target_id, limit=1):
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
    await _record(target_id, _event_key("turn", key), _TURN_CODES[outcome])


def notify_turn_end(
    target_id: str,
    outcome: TurnOutcome,
    response_id: str | None,
    *,
    from_runner: bool = False,
) -> None:
    """:func:`record_turn_end` for a producer with no acknowledgement to fail. Never raises."""
    if _store is None:
        return
    _spawn(
        _retain_recording(
            target_id,
            lambda: record_turn_end(target_id, outcome, response_id, from_runner=from_runner),
        )
    )


async def record_status(target_id: str, blocked_on: str | None, response_id: str | None) -> None:
    """
    Record prompt notices when a native status edge parks the target on a block.

    A block's identity is the target, its turn and *blocked_on*, so a repeated
    or replayed edge records nothing new; an edge without *blocked_on* ends it.

    :param target_id: Session whose status changed.
    :param blocked_on: What the session is parked on, or ``None``.
    :param response_id: The turn the edge belongs to, when known.
    :returns: None.
    :raises Exception: The notices could not be recorded.
    """
    if _track_block(target_id, blocked_on, response_id):
        await _record_block(target_id, cast(str, blocked_on), response_id)


def notify_status(target_id: str, blocked_on: str | None, response_id: str | None) -> None:
    """:func:`record_status` for a producer with no acknowledgement to fail. Never raises."""
    if _track_block(target_id, blocked_on, response_id):
        _spawn(
            _retain_recording(
                target_id,
                lambda: _record_block(target_id, cast(str, blocked_on), response_id),
            )
        )


def _track_block(target_id: str, blocked_on: str | None, response_id: str | None) -> bool:
    """Track the target's standing block; whether this edge is a block edge."""
    if _store is None:
        return False
    if blocked_on is None:
        _standing_blocks.pop(target_id, None)
        return False
    _standing_blocks[target_id] = (blocked_on, response_id)
    return True


async def _record_block(target_id: str, blocked_on: str, response_id: str | None) -> None:
    await _record(target_id, await _block_key(target_id, blocked_on, response_id), _PROMPT)


async def _block_key(target_id: str, blocked_on: str, response_id: str | None) -> str:
    """The event key of a block: target, turn and what it is parked on."""
    store = _store
    # A runner's relayed status names no turn, so the transcript's latest one does.
    # ponytail: a second block of the same kind in one turn shares the first's
    # identity and is not notified; a per-block id from the harness would split them.
    turn = response_id or (
        await latest_turn_id(target_id, store, failed=True) if store is not None else None
    )
    return _event_key("block", f"{target_id}:{turn or ''}:{blocked_on}")


def observe_elicitation(conversation_id: str, event: dict[str, Any]) -> None:
    """Pending-elicitation observer: record ``blocked`` notices for a new prompt."""
    if _store is None or event.get("type") != "response.elicitation_request":
        return
    elicitation_id = event.get("elicitation_id")
    if not isinstance(elicitation_id, str) or not elicitation_id:
        return
    # The publish path cannot await or fail, so the recording retries here.
    _spawn(
        _retain_recording(
            conversation_id,
            lambda: _record(conversation_id, _event_key("prompt", elicitation_id), _PROMPT),
        )
    )


async def record_pending_prompts(subscriber_id: str, target_id: str) -> None:
    """
    Record notices for a new subscriber about the target's standing prompts and block.

    :param subscriber_id: The session that just subscribed.
    :param target_id: The session it watches.
    :returns: None.
    :raises Exception: The notices could not be recorded.
    """
    keys = [
        _event_key("prompt", elicitation_id)
        for event in pending_elicitations.snapshot_for(target_id)
        if isinstance(elicitation_id := event.get("elicitation_id"), str) and elicitation_id
    ]
    if (standing := _standing_blocks.get(target_id)) is not None:
        keys.append(await _block_key(target_id, *standing))
    for key in keys:
        await _record(target_id, key, _PROMPT, only=subscriber_id)


async def _retain_recording(target_id: str, record: Callable[[], Awaitable[None]]) -> None:
    """Retry a recording until it lands; the producer had no way to retry it."""
    # ponytail: retained in memory only, so losing a notice takes a double
    # fault: a recording outage and the server dying during it. Producers that
    # can retry use the awaited ``record_*``.
    failures = 0
    while True:
        try:
            await record()
            return
        except Exception:  # noqa: BLE001 — retried below
            failures += 1
            _logger.warning(
                "Could not record notices from %s (attempt %d)", target_id, failures, exc_info=True
            )
        await asyncio.sleep(min(_RETRY_BASE_S * 2 ** min(failures - 1, 16), _RECORD_RETRY_CAP_S))


def set_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """Remember the server loop so off-loop publishes can schedule notices onto it."""
    global _loop
    _loop = loop


def _spawn(coro: Coroutine[Any, Any, None]) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = _loop
        try:
            if loop is None:
                raise RuntimeError("no server loop") from None
            loop.call_soon_threadsafe(_start, coro)
        except RuntimeError:
            coro.close()
        return
    _start(coro)


def _start(coro: Coroutine[Any, Any, None]) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _event_key(kind: str, event_id: str) -> str:
    return hashlib.sha256(f"{kind}:{event_id}".encode()).hexdigest()


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


async def _record(
    target_id: str, event_key: str, outcome: int, *, only: str | None = None
) -> None:
    """Record the event for each subscriber (or *only* one), then post the new rows.

    :raises Exception: A page could not be recorded; earlier pages stay recorded
        and a retried call skips them.
    """
    store = _store
    if store is None:
        return
    workspace_id = current_workspace_id()
    after: str | None = None
    leased_until = int(time.time()) + _LEASE_S
    while True:
        if only is not None:
            page = [only]
        else:
            page = await asyncio.to_thread(
                store.list_session_subscribers, target_id, after=after, limit=_SUBSCRIBER_PAGE
            )
        created = await asyncio.to_thread(
            store.enqueue_subscription_deliveries,
            page,
            target_id,
            event_key,
            outcome,
            due_at=leased_until,
        )
        for subscriber_id in created:
            _spawn(
                _post_recorded(
                    SubscriptionDelivery(
                        workspace_id, subscriber_id, target_id, event_key, 1, outcome
                    ),
                    leased_until,
                )
            )
        if only is not None or len(page) < _SUBSCRIBER_PAGE:
            return
        after = page[-1]


async def _post_recorded(delivery: SubscriptionDelivery, leased_until: int) -> None:
    """First attempt at a row just recorded, bounded like the sweep's."""
    global _post_limit
    if _post_limit is None:
        _post_limit = asyncio.Semaphore(_POST_CONCURRENCY)
    store = _store
    if store is None:
        return
    async with _post_limit:
        # A wait past the recording lease hands the row to the sweep, so take it
        # back only if it is still pending under that lease.
        with workspace_scope(delivery.workspace_id):
            try:
                now = int(time.time())
                claimed = await asyncio.to_thread(
                    store.claim_subscription_delivery,
                    delivery.subscriber_id,
                    delivery.target_id,
                    delivery.event_key,
                    leased_until=leased_until,
                    lease_until=now + _LEASE_S,
                )
            except Exception:  # noqa: BLE001 — the lease hands the row to the sweep
                _logger.warning(
                    "Could not claim a notice to %s", delivery.subscriber_id, exc_info=True
                )
                return
        if claimed:
            await _retry(store, delivery)


async def _deliverable(
    store: ConversationStore, subscriber_id: str, target_id: str
) -> str | None | object:
    """The subscriber's owner to post as, or ``_SKIP`` when it must not hear the target."""
    subscriber = await asyncio.to_thread(store.get_conversation, subscriber_id)
    if subscriber is None or subscriber.archived:
        return _SKIP
    owner = await asyncio.to_thread(_owner, store, subscriber)
    permission_store = _permission_store
    if permission_store is not None and not await asyncio.to_thread(
        check_session_access, owner, target_id, LEVEL_READ, permission_store, store
    ):
        return _SKIP
    return owner


async def _render(store: ConversationStore, target_id: str, outcome: int) -> str | None:
    """The notice text from the live target, or ``None`` once the target is gone."""
    target = await asyncio.to_thread(store.get_conversation, target_id)
    if target is None:
        return None
    if outcome == _PROMPT:
        return prompt_notice(target.title, target_id)
    return turn_notice(target.title, target_id, _TURN_OUTCOMES[outcome])


async def _attempt(
    store: ConversationStore,
    subscriber_id: str,
    target_id: str,
    event_key: str,
    outcome: int,
    attempts: int,
    owner: str | None,
) -> None:
    """Post one claimed delivery; settle it, or leave it pending for a retry."""
    post = _post_message
    try:
        if post is None:
            raise RuntimeError("subscription delivery is not configured")
        notice = await _render(store, target_id, outcome)
        if notice is not None:
            # ponytail: at-least-once. A server that dies, or a settle that
            # fails, after this post leaves the row to be posted again once its
            # lease expires; closing that needs idempotency at message acceptance.
            await post(subscriber_id, owner, notice)
    except Exception:  # noqa: BLE001 — the row stays pending for the retry sweep
        _logger.warning(
            "Could not deliver a notice from %s to %s (attempt %d)",
            target_id,
            subscriber_id,
            attempts,
            exc_info=True,
            extra={"session_id": subscriber_id},
        )
        due = int(time.time()) + min(_RETRY_BASE_S * 2 ** min(attempts - 1, 16), _RETRY_CAP_S)
        try:
            await asyncio.to_thread(
                store.reschedule_subscription_delivery,
                subscriber_id,
                target_id,
                event_key,
                due_at=due,
            )
            _wake_at(due)
        except Exception:  # noqa: BLE001 — the lease hands the row to the sweep
            _logger.warning("Could not reschedule a notice to %s", subscriber_id, exc_info=True)
        return
    try:
        await asyncio.to_thread(
            store.settle_subscription_delivery,
            subscriber_id,
            target_id,
            event_key,
            settled_at=int(time.time()),
        )
    except Exception:  # noqa: BLE001 — the lease hands the row to the sweep (a duplicate)
        _logger.warning("Could not settle a notice to %s", subscriber_id, exc_info=True)


def _wake_at(due: int) -> None:
    """Run the retry sweep at *due*, sharing one wake-up across failures."""
    global _next_wake
    if _next_wake is not None and time.time() < _next_wake <= due:
        return
    _next_wake = due
    _start(_sweep_at(due))


async def _sweep_at(due: int) -> None:
    global _next_wake
    await asyncio.sleep(max(0.0, due - time.time()) + 0.05)
    if _next_wake == due:
        _next_wake = None
    await retry_due_deliveries()


async def retry_due_deliveries() -> int:
    """
    Post pending notices that are due, in bounded batches. Never raises.

    :returns: Number of deliveries attempted.
    """
    store = _store
    if store is None:
        return 0
    attempted = 0
    limit = asyncio.Semaphore(_POST_CONCURRENCY)

    async def retry(delivery: SubscriptionDelivery) -> None:
        async with limit:
            await _retry(store, delivery)

    try:
        for _ in range(_SWEEP_MAX_BATCHES):
            now = int(time.time())
            batch = await asyncio.to_thread(
                store.claim_due_subscription_deliveries,
                now=now,
                lease_until=now + _LEASE_S,
                limit=_SWEEP_BATCH,
            )
            await asyncio.gather(*(retry(delivery) for delivery in batch))
            attempted += len(batch)
            if len(batch) < _SWEEP_BATCH:
                break
    except Exception:  # noqa: BLE001
        _logger.warning("Subscription notice retry sweep failed", exc_info=True)
    return attempted


async def _retry(store: ConversationStore, delivery: SubscriptionDelivery) -> None:
    with workspace_scope(delivery.workspace_id):
        key = (delivery.subscriber_id, delivery.target_id, delivery.event_key)
        try:
            owner = (
                await _deliverable(store, delivery.subscriber_id, delivery.target_id)
                if await asyncio.to_thread(
                    store.has_session_subscription, delivery.subscriber_id, delivery.target_id
                )
                else _SKIP
            )
            if owner is _SKIP:
                await asyncio.to_thread(
                    store.settle_subscription_delivery, *key, settled_at=int(time.time())
                )
                return
        except Exception:  # noqa: BLE001 — the lease hands the row to a later sweep
            _logger.warning(
                "Could not prepare a retried notice to %s", delivery.subscriber_id, exc_info=True
            )
            return
        await _attempt(store, *key, delivery.outcome, delivery.attempts, cast(str | None, owner))


async def prune_settled_deliveries() -> int:
    """
    Delete settled deliveries past the retention window, in bounded batches.

    A prompt's row is the only thing stopping a re-published, still-pending
    prompt from being delivered again, so it is kept (its age reset) while the
    subscription stands and the target may still have the prompt pending.

    :returns: Number of rows removed.
    """
    store = _store
    if store is None:
        return 0
    removed = 0
    now = int(time.time())
    for _ in range(_SWEEP_MAX_BATCHES):
        batch = await asyncio.to_thread(
            store.list_settled_subscription_deliveries,
            settled_before=now - _SETTLED_RETENTION_S,
            limit=_PRUNE_BATCH,
        )
        # ponytail: a turn's identity goes after the retention window, so a
        # replay of a turn older than that is delivered again.
        keep = [delivery for delivery in batch if await _keep_identity(store, delivery)]
        drop = [delivery for delivery in batch if delivery not in keep]
        removed += await asyncio.to_thread(store.delete_subscription_deliveries, drop)
        await asyncio.to_thread(store.touch_subscription_deliveries, keep, settled_at=now)
        if len(batch) < _PRUNE_BATCH:
            break
    return removed


async def _keep_identity(store: ConversationStore, delivery: SubscriptionDelivery) -> bool:
    if delivery.outcome != _PROMPT:
        return False
    with workspace_scope(delivery.workspace_id):
        if not await asyncio.to_thread(
            store.has_session_subscription, delivery.subscriber_id, delivery.target_id
        ):
            return False
        target = await asyncio.to_thread(store.get_conversation, delivery.target_id)
    if target is None:
        return False
    standing = {
        _event_key("prompt", event.get("elicitation_id", ""))
        for event in pending_elicitations.snapshot_for(target.id)
    }
    if (block := _standing_blocks.get(target.id)) is not None:
        standing.add(await _block_key(target.id, *block))
    # Held here, or a target that may still report a prompt pending elsewhere.
    return delivery.event_key in standing or target.pending_elicitation_count != 0


async def run_retry_loop() -> None:
    """Retry due notices and prune old ones at startup and then periodically."""
    while True:
        try:
            await retry_due_deliveries()
            await prune_settled_deliveries()
        except Exception:  # noqa: BLE001
            _logger.warning("Subscription notice maintenance failed", exc_info=True)
        await asyncio.sleep(_SWEEP_INTERVAL_S)
