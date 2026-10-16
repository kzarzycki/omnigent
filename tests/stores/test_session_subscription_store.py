"""Session subscription rows on SqlAlchemyConversationStore."""

from __future__ import annotations

import asyncio

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


def test_claim_delivers_each_event_once_and_release_allows_a_retry(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    watcher = conversation_store.create_conversation().id
    target = conversation_store.create_conversation().id
    conversation_store.add_session_subscription(watcher, target)

    claim = conversation_store.claim_session_subscription_event
    assert claim(watcher, target, "turn", "resp_1") is True
    assert claim(watcher, target, "turn", "resp_1") is False
    # Turn and prompt keys are tracked separately.
    assert claim(watcher, target, "prompt", "elicit_1") is True
    assert claim(watcher, target, "turn", "resp_2") is True

    conversation_store.release_session_subscription_event(watcher, target, "turn", "resp_2")
    assert claim(watcher, target, "turn", "resp_2") is True
    # No row, no claim.
    assert claim(target, watcher, "turn", "resp_1") is False


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
