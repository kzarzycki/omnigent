"""Keep open ``sys_ask_user_question`` cards in the conversation store.

The pending-prompt index (:mod:`omnigent.runtime.pending_elicitations`) is
in-memory, so a server restart empties it. A question card has no parked
awaiter to die with it: the answer is delivered as a user message whenever
it comes. So each card is also stored on the session, removed once answered,
and restored into the index when the session is next read after a restart.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from typing import Any

from cachetools import LRUCache

from omnigent.entities.conversation import Conversation
from omnigent.runtime import pending_elicitations
from omnigent.stores.conversation_store import ConversationStore

_logger = logging.getLogger(__name__)

# Answered here, so a session read before the removal committed cannot restore them.
# custom-lint: disable-next=workspace-scoped-cache -- question ids are globally unique
_answered: LRUCache[str, bool] = LRUCache(maxsize=4096)


async def store(
    conversation_store: ConversationStore,
    session_id: str,
    question_id: str,
    event: dict[str, Any],
    *,
    ancestor_ids: Sequence[str] = (),
    mirror: dict[str, Any] | None = None,
) -> None:
    """
    Store a new question card, and its ancestor copies, before it is published.

    :param conversation_store: The store that keeps the card.
    :param session_id: Session that asks, e.g. ``"conv_abc123"``.
    :param question_id: The card's id, e.g. ``"question_0f3a..."``.
    :param event: The card's ``response.elicitation_request`` event dict.
    :param ancestor_ids: Ancestors that show the card, nearest first.
    :param mirror: The copy those ancestors show, naming ``session_id`` as its target.
    :raises OpenQuestionLimitError: The session holds too many open questions.
    """
    await asyncio.to_thread(conversation_store.add_open_question, session_id, question_id, event)
    if mirror is None:
        return
    for ancestor_id in ancestor_ids:
        await asyncio.to_thread(
            conversation_store.add_open_question, ancestor_id, question_id, mirror
        )


async def forget(
    conversation_store: ConversationStore,
    session_id: str,
    question_id: str,
    *,
    ancestor_ids: Sequence[str] = (),
) -> None:
    """
    Remove an answered card and its ancestor copies; a failure is logged, not raised.

    The answer has already been delivered, so a failed removal must not fail
    the request. It only means the card comes back after a restart.

    :param conversation_store: The store that keeps the card.
    :param session_id: Session that asked, e.g. ``"conv_abc123"``.
    :param question_id: The card's id, e.g. ``"question_0f3a..."``.
    :param ancestor_ids: Ancestors that hold a copy of the card.
    """
    _answered[question_id] = True
    for holder_id in (session_id, *ancestor_ids):
        try:
            await asyncio.to_thread(
                conversation_store.remove_open_question, holder_id, question_id
            )
        except Exception:
            _logger.warning("Could not remove answered question %s", question_id, exc_info=True)


def restore(conversations: Iterable[Conversation]) -> None:
    """
    Put stored cards that the pending-prompt index lacks back into it.

    :param conversations: Sessions as read from the store.
    """
    for conversation in conversations:
        for question_id, event in conversation.open_questions.items():
            if question_id not in _answered and event.get("elicitation_id") == question_id:
                pending_elicitations.restore(conversation.id, event)


async def restore_session(conversation_store: ConversationStore, session_id: str) -> None:
    """
    Read one session and restore its stored cards.

    :param conversation_store: The store that keeps the cards.
    :param session_id: Session to restore, e.g. ``"conv_abc123"``.
    """
    conversation = await asyncio.to_thread(conversation_store.get_conversation, session_id)
    if conversation is not None:
        restore([conversation])


def reset_for_tests() -> None:
    """Forget which questions were answered in this process."""
    _answered.clear()
