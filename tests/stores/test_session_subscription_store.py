"""Session subscription rows on SqlAlchemyConversationStore."""

from __future__ import annotations

import asyncio

import pytest

from omnigent.stores.conversation_store import sqlalchemy_store
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


def test_subscribe_is_idempotent_and_lists_both_directions(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    watcher = conversation_store.create_conversation().id
    first = conversation_store.create_conversation().id
    second = conversation_store.create_conversation().id

    assert conversation_store.add_session_subscription(watcher, first) is True
    assert conversation_store.add_session_subscription(watcher, first) is False
    assert conversation_store.add_session_subscription(watcher, second) is True

    assert conversation_store.list_session_subscriptions(watcher) == sorted([first, second])
    assert conversation_store.list_session_subscribers(first) == [watcher]

    assert conversation_store.delete_session_subscription(watcher, first) is True
    assert conversation_store.delete_session_subscription(watcher, first) is False
    assert conversation_store.list_session_subscriptions(watcher) == [second]


def test_listings_page_by_continuation_key(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    target = conversation_store.create_conversation().id
    watchers = sorted(f"{index + 1:032x}" for index in range(5))
    for watcher in watchers:
        conversation_store.add_session_subscription(watcher, target)

    first = conversation_store.list_session_subscribers(target, limit=2)
    second = conversation_store.list_session_subscribers(target, after=first[-1], limit=2)
    third = conversation_store.list_session_subscribers(target, after=second[-1], limit=2)

    assert [first, second, third] == [watchers[:2], watchers[2:4], watchers[4:]]
    assert conversation_store.list_session_subscriptions(watchers[0], limit=1) == [target]
    assert conversation_store.has_session_subscription(watchers[0], target) is True
    assert conversation_store.has_session_subscription(target, watchers[0]) is False


def test_cleanup_removes_more_subscriptions_than_one_batch(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sqlalchemy_store, "_SESSION_SUBSCRIPTION_DELETE_BATCH", 2)
    session = conversation_store.create_conversation().id
    for index in range(5):
        conversation_store.add_session_subscription(f"{index + 1:032x}", session)
        conversation_store.add_session_subscription(session, f"{index + 101:032x}")

    assert conversation_store.delete_session_subscriptions_for(session) == 10
    assert conversation_store.list_session_subscribers(session) == []
    assert conversation_store.list_session_subscriptions(session) == []


def test_a_delivery_is_recorded_once_per_subscriber_and_event(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    enqueue = conversation_store.enqueue_subscription_delivery
    watcher, target = f"{1:032x}", f"{2:032x}"

    assert enqueue(watcher, target, "p1", "n", due_at=100) is True
    assert enqueue(watcher, target, "p2", "n", due_at=100) is True
    # A replay of the older event is still a duplicate once a newer one landed.
    assert enqueue(watcher, target, "p1", "n", due_at=100) is False
    conversation_store.settle_subscription_delivery(watcher, target, "p1", settled_at=100)
    assert enqueue(watcher, target, "p1", "n", due_at=100) is False
    assert enqueue(f"{3:032x}", target, "p1", "n", due_at=100) is True


def test_due_deliveries_are_claimed_once_and_settled_ones_pruned(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    watcher, target = f"{1:032x}", f"{2:032x}"
    conversation_store.enqueue_subscription_delivery(watcher, target, "due", "hi", due_at=10)
    conversation_store.enqueue_subscription_delivery(watcher, target, "later", "hi", due_at=50)

    claimed = conversation_store.claim_due_subscription_deliveries(
        now=20, lease_until=80, limit=10
    )
    assert [(d.event_key, d.attempts, d.notice) for d in claimed] == [("due", 2, "hi")]
    # Leased until 80: another sweep at 30 does not take it again.
    assert (
        conversation_store.claim_due_subscription_deliveries(now=30, lease_until=90, limit=10)
        == []
    )
    conversation_store.reschedule_subscription_delivery(watcher, target, "due", due_at=40)
    assert [
        d.event_key
        for d in conversation_store.claim_due_subscription_deliveries(
            now=60, lease_until=120, limit=10
        )
    ] == ["due", "later"]

    conversation_store.settle_subscription_delivery(watcher, target, "due", settled_at=70)
    assert conversation_store.prune_subscription_deliveries(settled_before=70, limit=10) == 0
    assert conversation_store.prune_subscription_deliveries(settled_before=71, limit=10) == 1
    # Pending rows are never pruned.
    assert conversation_store.prune_subscription_deliveries(settled_before=10**9, limit=10) == 0


def test_deleting_either_session_drops_its_subscriptions(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    watcher = conversation_store.create_conversation().id
    target = conversation_store.create_conversation().id
    other = conversation_store.create_conversation().id
    conversation_store.add_session_subscription(watcher, target)
    conversation_store.add_session_subscription(other, watcher)
    conversation_store.add_session_subscription(other, target)

    assert asyncio.run(conversation_store.delete_conversation(watcher)) is True

    assert conversation_store.list_session_subscribers(target) == [other]
    assert conversation_store.list_session_subscriptions(other) == [target]
    assert conversation_store.delete_session_subscriptions_for(target) == 1
    assert conversation_store.list_session_subscriptions(other) == []
