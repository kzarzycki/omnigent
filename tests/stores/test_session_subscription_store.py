"""Session subscription rows on SqlAlchemyConversationStore."""

from __future__ import annotations

import asyncio
import itertools

import pytest

from omnigent.stores.conversation_store import (
    MAX_SUBSCRIPTIONS_PER_SESSION,
    SessionSubscription,
    SubscriptionCycleError,
    SubscriptionLimitError,
    sqlalchemy_store,
)
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
    enqueue = conversation_store.enqueue_subscription_deliveries
    watcher, other, target = f"{1:032x}", f"{3:032x}", f"{2:032x}"

    assert enqueue([watcher], target, "p1", 1, due_at=100) == [watcher]
    assert enqueue([watcher], target, "p2", 1, due_at=100) == [watcher]
    # A replay of the older event is still a duplicate once a newer one landed.
    assert enqueue([watcher], target, "p1", 1, due_at=100) == []
    conversation_store.settle_subscription_delivery(watcher, target, "p1", settled_at=100)
    assert enqueue([watcher, other], target, "p1", 1, due_at=100) == [other]
    assert enqueue([], target, "p1", 1, due_at=100) == []


def test_a_subscription_that_closes_a_loop_is_refused(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    a, b, c, d = (conversation_store.create_conversation().id for _ in range(4))
    subscribe = conversation_store.add_session_subscription
    assert subscribe(a, b) and subscribe(b, c)

    for subscriber, target in ((c, a), (b, a), (c, b)):
        with pytest.raises(SubscriptionCycleError) as refused:
            subscribe(subscriber, target)
        assert refused.value.bounded is False
    # Fan-in without a loop is fine.
    assert subscribe(d, b) and subscribe(a, c) and subscribe(d, a)
    assert conversation_store.list_session_subscriptions(c) == []


def test_a_loop_search_past_its_bound_refuses(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sqlalchemy_store, "_SUBSCRIPTION_CYCLE_LIMIT", 3)
    chain = [f"{index + 1:032x}" for index in range(5)]
    for subscriber, target in itertools.pairwise(chain):
        conversation_store.add_session_subscription(subscriber, target)

    with pytest.raises(SubscriptionCycleError) as refused:
        conversation_store.add_session_subscription(f"{99:032x}", chain[0])
    assert refused.value.bounded is True
    # A short walk still decides.
    assert conversation_store.add_session_subscription(f"{99:032x}", chain[3]) is True


def test_a_subscription_past_the_cap_is_refused(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    subscriber = f"{0:032x}"
    targets = [f"{index + 1:032x}" for index in range(MAX_SUBSCRIPTIONS_PER_SESSION + 1)]
    for target in targets[:-1]:
        assert conversation_store.add_session_subscription(subscriber, target) is True

    with pytest.raises(SubscriptionLimitError):
        conversation_store.add_session_subscription(subscriber, targets[-1])
    # Repeating a held subscription is still a no-op, and a freed slot is reusable.
    assert conversation_store.add_session_subscription(subscriber, targets[0]) is False
    conversation_store.delete_session_subscription(subscriber, targets[0])
    assert conversation_store.add_session_subscription(subscriber, targets[-1]) is True


def test_due_deliveries_are_claimed_once_and_only_settled_ones_listed_for_the_prune(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    watcher, target = f"{1:032x}", f"{2:032x}"
    conversation_store.enqueue_subscription_deliveries([watcher], target, "due", 1, due_at=10)
    conversation_store.enqueue_subscription_deliveries([watcher], target, "later", 0, due_at=50)

    claimed = conversation_store.claim_due_subscription_deliveries(
        now=20, lease_until=80, limit=10
    )
    assert [(d.event_key, d.attempts, d.outcome) for d in claimed] == [("due", 2, 1)]
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
    old = conversation_store.list_settled_subscription_deliveries
    assert old(settled_before=70, limit=10) == []
    [settled] = old(settled_before=71, limit=10)
    assert (settled.event_key, settled.outcome) == ("due", 1)

    # A kept row is skipped until its new settle time ages out.
    conversation_store.touch_subscription_deliveries([settled], settled_at=500)
    assert old(settled_before=71, limit=10) == []
    assert [d.event_key for d in old(settled_before=501, limit=10)] == ["due"]

    assert conversation_store.delete_subscription_deliveries([settled]) == 1
    # Pending rows are never listed for the prune.
    assert old(settled_before=10**9, limit=10) == []


def test_quiet_seconds_is_set_on_subscribe_and_read_from_both_sides(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    watcher, target, other = (conversation_store.create_conversation().id for _ in range(3))
    assert conversation_store.add_session_subscription(watcher, target, quiet_seconds=300)
    assert not conversation_store.add_session_subscription(watcher, target, quiet_seconds=60)
    conversation_store.add_session_subscription(other, target)

    assert conversation_store.get_session_subscriptions(watcher, [target, other]) == {
        target: SessionSubscription(watcher, target, 60)
    }
    assert sorted(
        conversation_store.list_session_subscriber_settings(target), key=lambda s: s.subscriber_id
    ) == sorted(
        [SessionSubscription(watcher, target, 60), SessionSubscription(other, target, None)],
        key=lambda s: s.subscriber_id,
    )


def test_a_superseding_row_settles_only_older_pending_rows_of_its_outcome_and_pair(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    watcher, other, target = (conversation_store.create_conversation().id for _ in range(3))
    enqueue = conversation_store.enqueue_subscription_deliveries
    enqueue([watcher, other], target, "a" * 64, 4, due_at=100, attempts=0)
    enqueue([watcher], target, "p" * 64, 0, due_at=100)

    assert enqueue([watcher], target, "b" * 64, 4, due_at=200, attempts=0, supersede_at=50) == [
        watcher
    ]

    due = conversation_store.claim_due_subscription_deliveries(now=10**9, lease_until=0, limit=10)
    # The watcher's older held row is settled; its prompt and the other's row are not.
    assert sorted((d.subscriber_id, d.event_key, d.attempts) for d in due) == sorted(
        [(watcher, "b" * 64, 1), (watcher, "p" * 64, 2), (other, "a" * 64, 1)]
    )
    # A row of another outcome settles the held rows it names, and only those.
    enqueue([other], target, "f" * 64, 3, due_at=0, supersede_at=60, superseded_outcome=4)
    due = conversation_store.claim_due_subscription_deliveries(now=10**9, lease_until=0, limit=10)
    assert sorted((d.subscriber_id, d.event_key) for d in due) == sorted(
        [(watcher, "b" * 64), (watcher, "p" * 64), (other, "f" * 64)]
    )


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
