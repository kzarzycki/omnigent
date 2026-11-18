"""Telling a session that its runner went away with its background work."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from omnigent.server import orphaned_background_work
from omnigent.server.routes import sessions as sessions_module
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests._helpers.polling import poll_until_async

pytestmark = pytest.mark.asyncio

_RUNNER = "runner-gone"


@pytest.fixture
def store(db_uri: str) -> SqlAlchemyConversationStore:
    return SqlAlchemyConversationStore(db_uri)


@pytest.fixture
def notices(store: SqlAlchemyConversationStore) -> Iterator[list[str]]:
    posted: list[str] = []

    async def post(session_id: str, user_id: str | None, text: str) -> object:
        assert text == orphaned_background_work.NOTICE
        posted.append(session_id)
        return {}

    orphaned_background_work.configure(store, None, post)
    try:
        yield posted
    finally:
        orphaned_background_work.configure(None, None, None)


def _lost(store: SqlAlchemyConversationStore, **create: object) -> str:
    session_id = store.create_conversation(runner_id=_RUNNER, **create).id  # type: ignore[arg-type]
    store.set_background_work_running(session_id, True, runner_id=_RUNNER)
    store.set_background_work_running(
        session_id, False, runner_id=_RUNNER, unseen_since=2_000, lost_at=1_000
    )
    return session_id


async def test_a_session_that_lost_background_work_is_told_once(
    store: SqlAlchemyConversationStore, notices: list[str]
) -> None:
    session_id = _lost(store)

    assert await orphaned_background_work.resume(session_id) is True
    assert await orphaned_background_work.resume(session_id) is False

    assert notices == [session_id]
    told = store.get_conversation(session_id)
    assert told is not None and told.background_work_lost_at is None


async def test_sessions_a_notice_must_not_resume_keep_their_flag(
    store: SqlAlchemyConversationStore, notices: list[str]
) -> None:
    parent = store.create_conversation().id
    sub_agent = _lost(store, kind="sub_agent", parent_conversation_id=parent)
    archived = _lost(store)
    store.update_conversation(archived, archived=True)
    stopped = _lost(store)
    sessions_module._intentional_stop_sessions[stopped] = _RUNNER
    try:
        for session_id in (sub_agent, archived, stopped):
            assert await orphaned_background_work.resume(session_id) is False
    finally:
        sessions_module._intentional_stop_sessions.pop(stopped, None)

    assert notices == []
    flagged = store.get_conversation(sub_agent)
    assert flagged is not None and flagged.background_work_lost_at == 1_000


async def test_a_second_resume_while_the_first_posts_sends_nothing(
    store: SqlAlchemyConversationStore,
) -> None:
    session_id = _lost(store)
    release = asyncio.Event()
    posted: list[str] = []

    async def slow_post(target: str, user_id: str | None, text: str) -> object:
        posted.append(target)
        # A relaunch to take the notice can take minutes.
        await release.wait()
        return {}

    orphaned_background_work.configure(store, None, slow_post)
    try:
        first = asyncio.create_task(orphaned_background_work.resume(session_id))
        await poll_until_async(lambda: posted, timeout_s=5)
        second = asyncio.create_task(orphaned_background_work.resume(session_id))
        await asyncio.sleep(0)
        release.set()
        assert await asyncio.wait_for(asyncio.gather(first, second), timeout=5) == [True, False]
    finally:
        orphaned_background_work.configure(None, None, None)

    assert posted == [session_id]


async def test_a_failed_post_lets_the_next_resume_post_once(
    store: SqlAlchemyConversationStore,
) -> None:
    session_id = _lost(store)
    posted: list[str] = []

    async def post(target: str, user_id: str | None, text: str) -> object:
        posted.append(target)
        if len(posted) == 1:
            raise RuntimeError("the session's runner did not come back")
        return {}

    orphaned_background_work.configure(store, None, post)
    try:
        assert await orphaned_background_work.resume(session_id) is False
        flagged = store.get_conversation(session_id)
        assert flagged is not None and flagged.background_work_lost_at == 1_000
        assert await orphaned_background_work.resume(session_id) is True
    finally:
        orphaned_background_work.configure(None, None, None)

    assert posted == [session_id, session_id]
    told = store.get_conversation(session_id)
    assert told is not None and told.background_work_lost_at is None
