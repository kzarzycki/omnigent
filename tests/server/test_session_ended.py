"""An archived ACP session tells its runner it ended; a cancel does not."""

from __future__ import annotations

import asyncio
import logging
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omnigent.server import session_ended, session_live_state
from omnigent.server.routes import sessions as sessions_facade
from omnigent.server.routes._sessions import orchestration
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore

pytestmark = [pytest.mark.asyncio, pytest.mark.filterwarnings("ignore")]

CONV = SimpleNamespace(id="s" * 32)


class _Runner:
    def __init__(self, status_code: int = 204) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.status_code = status_code

    async def post(self, url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        self.posts.append((url, json))
        return httpx.Response(self.status_code)


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> _Runner:
    fake = _Runner()

    async def client(*_args: object, **_kwargs: object) -> _Runner:
        return fake

    monkeypatch.setattr(sessions_facade, "_get_runner_client", client)
    monkeypatch.setattr(sessions_facade, "_resolve_harness", lambda *a, **k: "acp:claude-cloud")
    return fake


async def test_an_acp_session_end_is_posted_to_its_runner(runner: _Runner) -> None:
    assert await session_ended.notify_session_ended(CONV) is True  # type: ignore[arg-type]
    assert runner.posts == [(f"/v1/sessions/{CONV.id}/events", {"type": "session_ended"})]


async def test_another_harness_is_not_told(runner: _Runner, monkeypatch) -> None:
    monkeypatch.setattr(sessions_facade, "_resolve_harness", lambda *a, **k: "claude-sdk")
    assert await session_ended.notify_session_ended(CONV) is False  # type: ignore[arg-type]
    assert runner.posts == []


async def test_no_runner_is_logged_and_not_raised(monkeypatch, caplog) -> None:
    async def gone(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(sessions_facade, "_get_runner_client", gone)
    monkeypatch.setattr(sessions_facade, "_resolve_harness", lambda *a, **k: "acp")
    with caplog.at_level(logging.WARNING):
        assert await session_ended.notify_session_ended(CONV) is False  # type: ignore[arg-type]
    assert "no runner to tell" in caplog.text


@pytest.mark.parametrize("failure", ["status", "transport"])
async def test_a_failed_notice_is_logged_and_not_raised(runner, caplog, failure) -> None:
    if failure == "status":
        runner.status_code = 503
    else:

        async def broken(*_args: object, **_kwargs: object) -> httpx.Response:
            raise httpx.ConnectError("down")

        runner.post = broken  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        assert await session_ended.notify_session_ended(CONV) is False  # type: ignore[arg-type]
    assert caplog.records


async def test_the_archive_teardown_tells_the_runner_after_stopping_the_turn(
    db_uri: str, monkeypatch
) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    conv = conversations.create_conversation(workspace="/workspace")
    conversations.update_conversation(conv.id, archived=True)
    calls: list[str] = []

    async def stop_turn(*_args: object, **_kwargs: object) -> list[str]:
        calls.append("stop-turn")
        return []

    async def ended(told: Any, **_kwargs: object) -> bool:
        calls.append(f"ended:{told.id}")
        return True

    monkeypatch.setattr(sessions_facade, "_best_effort_stop", stop_turn)
    monkeypatch.setattr(orchestration, "notify_session_ended", ended)
    await orchestration._archive_stop(conv.id, conversations, None, None)
    assert calls == ["stop-turn", f"ended:{conv.id}"]


async def test_a_scheduled_run_archived_when_done_tells_the_runner(
    db_uri: str, monkeypatch
) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    tasks = SqlAlchemyScheduledTaskStore(db_uri)
    task = tasks.create(
        scheduled_task_id=uuid.uuid4().hex,
        name="check",
        prompt="p",
        rrule="FREQ=HOURLY",
        user_id=None,
        agent_id=uuid.uuid4().hex,
        timezone="UTC",
        archive_when_done=True,
    )
    conv = conversations.create_conversation(title="check")
    tasks.create_run(uuid.uuid4().hex, task.id, "running", scheduled_at=0, conversation_id=conv.id)
    told: list[str] = []

    async def ended(archived: Any, **_kwargs: object) -> bool:
        told.append(archived.id)
        return True

    monkeypatch.setattr(session_live_state, "notify_session_ended", ended)
    session_live_state.configure(conversations, tasks)
    try:
        session_live_state.persist_scheduled_run_completion(conv.id, "succeeded")
        await asyncio.to_thread(session_live_state.submit("drain", lambda: None).result, 10)
        await asyncio.sleep(0.05)
    finally:
        session_live_state.configure(None)
    assert told == [conv.id]


def test_a_scheduled_run_archived_with_no_server_loop_is_logged(db_uri: str, caplog) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    tasks = SqlAlchemyScheduledTaskStore(db_uri)
    task = tasks.create(
        scheduled_task_id=uuid.uuid4().hex,
        name="check",
        prompt="p",
        rrule="FREQ=HOURLY",
        user_id=None,
        agent_id=uuid.uuid4().hex,
        timezone="UTC",
        archive_when_done=True,
    )
    conv = conversations.create_conversation(title="check")
    tasks.create_run(uuid.uuid4().hex, task.id, "running", scheduled_at=0, conversation_id=conv.id)
    session_live_state.configure(conversations, tasks)
    try:
        with caplog.at_level(logging.WARNING):
            session_live_state.persist_scheduled_run_completion(conv.id, "succeeded")
            session_live_state.submit("drain", lambda: None).result(10)
    finally:
        session_live_state.configure(None)
    assert "no server loop to tell its runner" in caplog.text
    assert conversations.get_conversation(conv.id).archived
