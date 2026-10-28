"""Session subscriptions: one session hears when another ends a turn or blocks."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import sqlalchemy as sa

from omnigent.entities import MessageData, NewConversationItem
from omnigent.runner.tool_dispatch import execute_tool
from omnigent.runtime import pending_elicitations
from omnigent.server import session_subscriptions
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ
from omnigent.server.routes import sessions as sessions_facade
from omnigent.server.routes._sessions import orchestration
from omnigent.server.schemas import ErrorDetail
from omnigent.spec.types import AgentSpec
from omnigent.stores.conversation_store import MAX_SUBSCRIPTIONS_PER_SESSION
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.helpers import create_test_session
from tests.server.routes.test_sessions_runner_relay import _ScriptedRunnerClient

pytestmark = pytest.mark.asyncio

_NATIVE = {"type": "omnigent", "config": {"harness": "claude-native"}}


class _Runner:
    """Stands in for every session's runner and records the messages it gets."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(self._handle), base_url="http://runner"
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        session_id = request.url.path.split("/")[3]
        self.events.append((session_id, json.loads(request.content)))
        return httpx.Response(200, json={"queued": False})

    def notices(self, session_id: str) -> list[str]:
        return [
            body["content"][0]["text"]
            for sid, body in self.events
            if sid == session_id and body.get("type") == "message"
        ]


@pytest.fixture
async def runner(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Runner]:
    fake = _Runner()

    async def get_runner_client(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return fake.client

    monkeypatch.setattr(sessions_facade, "_get_runner_client", get_runner_client)
    async with fake.client:
        yield fake


async def _settle() -> None:
    """Wait for the fire-and-forget deliveries scheduled so far."""
    for _ in range(5):
        await asyncio.sleep(0)
        await asyncio.gather(*list(session_subscriptions._tasks), return_exceptions=True)


async def _subscribe(client: httpx.AsyncClient, subscriber: str, target: str) -> httpx.Response:
    return await client.post(
        f"/v1/sessions/{subscriber}/subscriptions", json={"session_id": target}
    )


async def _status(client: httpx.AsyncClient, session_id: str, **data: Any) -> None:
    response = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
    )
    assert response.status_code == 202, response.text


async def _turn_end(target: str, outcome: str, response_id: str) -> None:
    await session_subscriptions.record_turn_end(target, outcome, response_id)  # type: ignore[arg-type]
    await _settle()


async def _prompt_edge(target: str, elicitation_id: str, *, only: str | None = None) -> None:
    key = session_subscriptions._event_key("prompt", elicitation_id)
    await session_subscriptions._record(target, key, session_subscriptions._PROMPT, only=only)
    await _settle()


def _waiting(title: str, session_id: str) -> str:
    return (
        f"[System: session {title} ({session_id}) is waiting on a prompt. "
        "Read it with sys_session_get_info.]"
    )


def _prompt(elicitation_id: str, message: str) -> dict[str, Any]:
    return {
        "type": "response.elicitation_request",
        "elicitation_id": elicitation_id,
        "params": {"message": message},
    }


async def test_subscribing_is_idempotent_and_listed_until_unsubscribed(
    client: httpx.AsyncClient,
) -> None:
    watcher = (await create_test_session(client, title="watcher"))["id"]
    worker = (await create_test_session(client, title="worker"))["id"]

    for _ in range(2):
        response = await _subscribe(client, watcher, worker)
        assert response.status_code == 200, response.text
        assert response.json() == {"subscriber_id": watcher, "session_id": worker}
    listing = await client.get(f"/v1/sessions/{watcher}/subscriptions")
    assert listing.json() == {
        "data": [{"subscriber_id": watcher, "session_id": worker}],
        "last_id": worker,
        "has_more": False,
    }

    for _ in range(2):
        deleted = await client.delete(f"/v1/sessions/{watcher}/subscriptions/{worker}")
        assert deleted.status_code == 204
    listing = await client.get(f"/v1/sessions/{watcher}/subscriptions")
    assert listing.json() == {"data": [], "last_id": None, "has_more": False}


async def test_subscribing_rejects_self_mutual_unknown_and_archived_sessions(
    client: httpx.AsyncClient,
) -> None:
    a = (await create_test_session(client))["id"]
    b = (await create_test_session(client))["id"]
    archived = (await create_test_session(client))["id"]
    with patch.object(sessions_facade, "_ARCHIVE_STOP_UNDO_GRACE_S", 3600.0):
        await client.patch(f"/v1/sessions/{archived}", json={"archived": True})

    assert (await _subscribe(client, a, a)).status_code == 400
    assert (await _subscribe(client, a, "0" * 32)).status_code == 404
    assert (await _subscribe(client, "0" * 32, a)).status_code == 404
    assert (await _subscribe(client, a, archived)).status_code == 409
    assert (await _subscribe(client, a, b)).status_code == 200
    assert (await _subscribe(client, b, a)).status_code == 409
    orchestration._cancel_pending_archive_stop(archived)


async def test_subscribing_needs_edit_on_the_subscriber_and_read_on_the_target(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    readable, editable, foreign = (store.create_conversation().id for _ in range(3))
    for user in ("bob@example.com", "carol@example.com"):
        permissions.ensure_user(user)
    permissions.grant("bob@example.com", readable, LEVEL_READ)
    permissions.grant("bob@example.com", editable, LEVEL_EDIT)
    permissions.grant("carol@example.com", foreign, LEVEL_EDIT)
    bob = {"X-Forwarded-Email": "bob@example.com"}

    async def subscribe(subscriber: str, target: str) -> int:
        response = await auth_client.post(
            f"/v1/sessions/{subscriber}/subscriptions",
            json={"session_id": target},
            headers=bob,
        )
        return response.status_code

    assert await subscribe(readable, editable) == 403
    assert await subscribe(editable, foreign) == 404
    assert await subscribe(editable, readable) == 200
    unsubscribe = await auth_client.delete(
        f"/v1/sessions/{readable}/subscriptions/{editable}", headers=bob
    )
    assert unsubscribe.status_code == 403


@pytest.mark.parametrize(
    ("data", "outcome"),
    [
        ({"status": "idle", "turn_completed": True}, "completed"),
        ({"status": "idle", "turn_outcome": "cancelled"}, "cancelled"),
        ({"status": "failed"}, "failed"),
    ],
)
async def test_a_native_turn_end_reaches_the_subscriber_as_a_system_message(
    client: httpx.AsyncClient, runner: _Runner, data: dict[str, Any], outcome: str
) -> None:
    watcher = (await create_test_session(client, title="watcher"))["id"]
    worker = (await create_test_session(client, title="worker", executor=_NATIVE))["id"]
    await _subscribe(client, watcher, worker)

    await _status(client, worker, response_id="resp_1", **data)
    await _settle()

    assert runner.notices(watcher) == [
        f"[System: session worker ({worker}) ended its turn ({outcome}). "
        "Read it with sys_session_get_history.]"
    ]


async def test_a_quiet_native_idle_is_not_a_turn_end(
    client: httpx.AsyncClient, runner: _Runner
) -> None:
    watcher = (await create_test_session(client))["id"]
    worker = (await create_test_session(client, executor=_NATIVE))["id"]
    await _subscribe(client, watcher, worker)

    await _status(client, worker, status="idle", response_id="resp_1")
    await _settle()

    assert runner.notices(watcher) == []


async def test_a_repeated_turn_end_is_delivered_once(
    client: httpx.AsyncClient, runner: _Runner
) -> None:
    watcher = (await create_test_session(client))["id"]
    worker = (await create_test_session(client, executor=_NATIVE))["id"]
    await _subscribe(client, watcher, worker)

    for _ in range(2):
        await _status(client, worker, status="idle", turn_completed=True, response_id="resp_1")
        await _settle()
    await _status(client, worker, status="idle", turn_completed=True, response_id="resp_2")
    await _settle()

    assert len(runner.notices(watcher)) == 2


async def test_an_sdk_turn_end_relayed_by_the_runner_is_delivered_but_a_native_one_is_not(
    client: httpx.AsyncClient, runner: _Runner
) -> None:
    watcher = (await create_test_session(client))["id"]
    sdk = (await create_test_session(client, title="sdk"))["id"]
    native = (await create_test_session(client, title="native", executor=_NATIVE))["id"]
    for target in (sdk, native):
        await _subscribe(client, watcher, target)

    for target in (sdk, native):
        session_subscriptions.notify_turn_end(target, "completed", "resp_1", from_runner=True)
    await _settle()

    assert runner.notices(watcher) == [
        f"[System: session sdk ({sdk}) ended its turn (completed). "
        "Read it with sys_session_get_history.]"
    ]


@pytest.mark.parametrize(
    ("terminal", "status", "outcome"),
    [
        ("completed", "idle", "completed"),
        ("cancelled", "idle", "cancelled"),
        ("failed", "failed", "failed"),
    ],
)
async def test_the_relay_turn_end_of_an_sdk_session_reaches_subscribers(
    db_uri: str, terminal: str, status: str, outcome: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    watcher = store.create_conversation()
    worker = store.create_conversation(title="worker")
    store.add_session_subscription(watcher.id, worker.id)
    posted: list[tuple[str, str]] = []

    async def post(session_id: str, _user_id: str | None, text: str) -> None:
        posted.append((session_id, text))

    session_subscriptions.configure(store, None, post)
    release = asyncio.Event()
    release.set()
    events = [
        {"type": "response.in_progress", "response": {"id": "turn-1"}},
        {"type": f"response.{terminal}", "response": {"id": "turn-1"}},
        {"type": "session.status", "status": status},
        {"type": "session.status", "status": status},
    ]
    try:
        await orchestration._relay_runner_stream_once(
            worker.id, _ScriptedRunnerClient(release, events), store
        )
        await _settle()
    finally:
        session_subscriptions.configure(None, None, None)

    assert posted == [
        (
            watcher.id,
            f"[System: session worker ({worker.id}) ended its turn ({outcome}). "
            "Read it with sys_session_get_history.]",
        )
    ]


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (ErrorDetail(code="runner_disconnected", message="lost"), "failed"),
        (orchestration._RUNNER_STOPPED_MID_TURN, "cancelled"),
    ],
)
async def test_a_runner_lost_mid_turn_tells_subscribers(
    client: httpx.AsyncClient,
    runner: _Runner,
    db_uri: str,
    error: ErrorDetail,
    outcome: str,
) -> None:
    watcher = (await create_test_session(client))["id"]
    worker = (await create_test_session(client, title="worker"))["id"]
    await _subscribe(client, watcher, worker)

    await orchestration._notify_parent_of_lost_subagent(
        worker, error, SqlAlchemyConversationStore(db_uri)
    )
    await _settle()

    assert runner.notices(watcher) == [
        f"[System: session worker ({worker}) ended its turn ({outcome}). "
        "Read it with sys_session_get_history.]"
    ]


async def test_a_prompt_is_delivered_once_and_a_new_subscriber_hears_a_pending_one(
    client: httpx.AsyncClient, runner: _Runner
) -> None:
    early = (await create_test_session(client))["id"]
    late = (await create_test_session(client))["id"]
    worker = (await create_test_session(client, title="worker"))["id"]
    await _subscribe(client, early, worker)

    for _ in range(2):
        pending_elicitations.record_publish(worker, _prompt("elicit_1", "Run rm -rf build?"))
        await _settle()
    await _subscribe(client, late, worker)
    await _settle()

    notice = _waiting("worker", worker)
    assert runner.notices(early) == [notice]
    assert runner.notices(late) == [notice]


async def test_a_prompt_published_off_the_event_loop_is_still_delivered(
    client: httpx.AsyncClient, runner: _Runner
) -> None:
    watcher = (await create_test_session(client))["id"]
    worker = (await create_test_session(client, title="worker"))["id"]
    await _subscribe(client, watcher, worker)
    session_subscriptions.set_loop(asyncio.get_running_loop())

    try:
        await asyncio.to_thread(
            pending_elicitations.record_publish, worker, _prompt("elicit_1", "Allow it?")
        )
        await _settle()
    finally:
        session_subscriptions.set_loop(None)

    assert runner.notices(watcher) == [_waiting("worker", worker)]


async def test_only_a_pending_prompt_is_delivered_on_subscribe(
    client: httpx.AsyncClient, runner: _Runner
) -> None:
    watcher = (await create_test_session(client))["id"]
    worker = (await create_test_session(client, executor=_NATIVE))["id"]
    await _status(client, worker, status="idle", turn_completed=True, response_id="resp_1")

    await _subscribe(client, watcher, worker)
    await _settle()

    assert runner.notices(watcher) == []


async def test_two_subscribers_and_three_targets_each_hear_every_event(
    client: httpx.AsyncClient, runner: _Runner
) -> None:
    watchers = [(await create_test_session(client))["id"] for _ in range(2)]
    workers = [(await create_test_session(client, executor=_NATIVE))["id"] for _ in range(3)]
    for watcher in watchers:
        for worker in workers:
            await _subscribe(client, watcher, worker)

    for worker in workers:
        await _status(client, worker, status="idle", turn_completed=True, response_id="resp_1")
    await _settle()

    for watcher in watchers:
        heard = runner.notices(watcher)
        assert len(heard) == 3
        assert {worker for worker in workers if any(worker in n for n in heard)} == set(workers)


async def test_subscribing_leaves_the_watched_session_unchanged(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    watcher = (await create_test_session(client))["id"]
    worker = (await create_test_session(client))["id"]
    before = await client.get(f"/v1/sessions/{worker}")

    await _subscribe(client, watcher, worker)

    after = await client.get(f"/v1/sessions/{worker}")
    keys = ("parent_session_id", "runner_id", "host_id", "kind", "labels", "status")
    assert {k: after.json()[k] for k in keys} == {k: before.json()[k] for k in keys}
    store = SqlAlchemyConversationStore(db_uri)
    assert not store.list_child_conversation_ids_by_parent([watcher]).get(watcher)


@pytest.mark.parametrize("archived", ["subscriber", "target"])
async def test_archiving_either_side_drops_the_subscription(
    client: httpx.AsyncClient, runner: _Runner, db_uri: str, archived: str
) -> None:
    watcher = (await create_test_session(client))["id"]
    worker = (await create_test_session(client, executor=_NATIVE))["id"]
    await _subscribe(client, watcher, worker)

    with patch.object(sessions_facade, "_ARCHIVE_STOP_UNDO_GRACE_S", 0.0):
        archived_id = watcher if archived == "subscriber" else worker
        response = await client.patch(f"/v1/sessions/{archived_id}", json={"archived": True})
        assert response.status_code == 200, response.text
        await asyncio.gather(*list(orchestration._detached_stop_tasks), return_exceptions=True)

    store = SqlAlchemyConversationStore(db_uri)
    assert store.list_session_subscriptions(watcher) == []
    assert store.list_session_subscribers(worker) == []


@pytest.mark.parametrize("deleted", ["subscriber", "target"])
async def test_deleting_either_side_drops_the_subscription(
    client: httpx.AsyncClient, db_uri: str, deleted: str
) -> None:
    watcher = (await create_test_session(client))["id"]
    worker = (await create_test_session(client))["id"]
    await _subscribe(client, watcher, worker)

    response = await client.delete(
        f"/v1/sessions/{watcher if deleted == 'subscriber' else worker}"
    )
    assert response.status_code in (200, 204), response.text

    store = SqlAlchemyConversationStore(db_uri)
    assert store.list_session_subscriptions(watcher) == []
    assert store.list_session_subscribers(worker) == []


async def test_the_agent_tools_subscribe_list_and_unsubscribe_the_calling_session(
    client: httpx.AsyncClient,
) -> None:
    watcher = (await create_test_session(client))["id"]
    worker = (await create_test_session(client))["id"]

    async def call(tool: str, **args: Any) -> Any:
        output = await execute_tool(
            tool_name=tool,
            arguments=json.dumps(args),
            server_client=client,
            conversation_id=watcher,
            agent_spec=AgentSpec(spec_version=1),
        )
        return json.loads(output)

    assert await call("sys_session_subscribe", session_id=worker) == {
        "subscriber_id": watcher,
        "session_id": worker,
    }
    assert await call("sys_session_subscriptions") == {
        "data": [{"subscriber_id": watcher, "session_id": worker}],
        "last_id": worker,
        "has_more": False,
    }
    assert await call("sys_session_unsubscribe", session_id=worker) == {"unsubscribed": worker}
    assert await call("sys_session_subscriptions") == {
        "data": [],
        "last_id": None,
        "has_more": False,
    }
    refused = await call("sys_session_subscribe", session_id=watcher)
    assert refused["error"].startswith("sys_session_subscribe returned 400")


class _Poster:
    """Records posted notices; fails the first ``failures`` posts."""

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.attempts = 0
        self.posted: list[tuple[str, str]] = []

    async def __call__(self, session_id: str, _user_id: str | None, text: str) -> None:
        self.attempts += 1
        if self.failures:
            self.failures -= 1
            raise ConnectionError("subscriber host temporarily offline")
        self.posted.append((session_id, text))


@pytest.fixture
def outbox(db_uri: str) -> AsyncIterator[tuple[SqlAlchemyConversationStore, str, str]]:
    store = SqlAlchemyConversationStore(db_uri)
    watcher = store.create_conversation().id
    target = store.create_conversation(title="worker").id
    store.add_session_subscription(watcher, target)
    yield store, watcher, target
    session_subscriptions.configure(None, None, None)


async def test_a_replayed_older_prompt_is_not_delivered_again(
    outbox: tuple[SqlAlchemyConversationStore, str, str],
) -> None:
    store, watcher, target = outbox
    poster = _Poster()
    session_subscriptions.configure(store, None, poster)

    for prompt in ("p1", "p2", "p1", "p2"):
        await _prompt_edge(target, prompt, only=watcher)

    assert len(poster.posted) == 2


async def test_a_replayed_older_turn_is_not_delivered_again(
    outbox: tuple[SqlAlchemyConversationStore, str, str],
) -> None:
    store, _watcher, target = outbox
    poster = _Poster()
    session_subscriptions.configure(store, None, poster)

    for response_id in ("resp_1", "resp_2", "resp_1"):
        await _turn_end(target, "completed", response_id)

    assert len(poster.posted) == 2


async def test_subscribing_again_with_two_pending_prompts_delivers_two_notices(
    client: httpx.AsyncClient, runner: _Runner
) -> None:
    watcher = (await create_test_session(client))["id"]
    worker = (await create_test_session(client, title="worker"))["id"]
    pending_elicitations.record_publish(worker, _prompt("elicit_1", "First?"))
    pending_elicitations.record_publish(worker, _prompt("elicit_2", "Second?"))

    for _ in range(3):
        assert (await _subscribe(client, watcher, worker)).status_code == 200
        await _settle()

    assert runner.notices(watcher) == [_waiting("worker", worker)] * 2


async def test_a_recovered_subscriber_receives_the_notice_a_failed_post_left_pending(
    outbox: tuple[SqlAlchemyConversationStore, str, str],
) -> None:
    store, watcher, target = outbox
    poster = _Poster(failures=1)
    session_subscriptions.configure(store, None, poster)

    # The failed first post schedules the retry that _settle waits for.
    await _turn_end(target, "failed", "resp_1")
    assert poster.attempts == 2

    assert poster.posted == [
        (
            watcher,
            f"[System: session worker ({target}) ended its turn (failed). "
            "Read it with sys_session_get_history.]",
        )
    ]
    # Settled: neither a sweep nor a repeated edge posts it again.
    assert await session_subscriptions.retry_due_deliveries() == 0
    await _turn_end(target, "failed", "resp_1")
    assert len(poster.posted) == 1


async def test_a_notice_recorded_before_a_restart_is_posted_once_by_the_sweep(
    outbox: tuple[SqlAlchemyConversationStore, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _watcher, target = outbox
    session_subscriptions.configure(store, None, _Poster())

    async def crash(*_args: Any, **_kwargs: Any) -> None:
        raise asyncio.CancelledError  # the server dies between recording and posting

    monkeypatch.setattr(session_subscriptions, "_attempt", crash)
    await _turn_end(target, "completed", "resp_1")
    monkeypatch.undo()

    restarted = _Poster()
    session_subscriptions.configure(store, None, restarted)
    # Still leased to the dead attempt.
    assert await session_subscriptions.retry_due_deliveries() == 0
    monkeypatch.setattr(session_subscriptions, "_LEASE_S", -1)
    with patch.object(session_subscriptions.time, "time", lambda: 10**10):
        assert await session_subscriptions.retry_due_deliveries() == 1
        assert await session_subscriptions.retry_due_deliveries() == 0

    assert [notice for _, notice in restarted.posted] == [
        f"[System: session worker ({target}) ended its turn (completed). "
        "Read it with sys_session_get_history.]"
    ]


async def test_a_pending_notice_to_an_unsubscribed_session_is_dropped(
    outbox: tuple[SqlAlchemyConversationStore, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, watcher, target = outbox
    poster = _Poster(failures=1)
    session_subscriptions.configure(store, None, poster)
    monkeypatch.setattr(session_subscriptions, "_wake_at", lambda _due: None)

    await _turn_end(target, "completed", "resp_1")
    store.delete_session_subscription(watcher, target)

    with patch.object(session_subscriptions.time, "time", lambda: 10**10):
        assert await session_subscriptions.retry_due_deliveries() == 1
        assert await session_subscriptions.retry_due_deliveries() == 0
    assert poster.posted == []


async def test_more_than_a_thousand_subscribers_all_hear_a_fan_out(
    outbox: tuple[SqlAlchemyConversationStore, str, str],
) -> None:
    store, watcher, target = outbox
    watchers = [watcher]
    for _ in range(1001):
        subscriber = store.create_conversation().id
        store.add_session_subscription(subscriber, target)
        watchers.append(subscriber)
    poster = _Poster()
    session_subscriptions.configure(store, None, poster)

    await _turn_end(target, "completed", "resp_1")

    assert sorted(session_id for session_id, _ in poster.posted) == sorted(watchers)


async def test_the_outbox_keeps_no_prompt_text_and_a_retry_names_the_current_title(
    outbox: tuple[SqlAlchemyConversationStore, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, watcher, target = outbox
    poster = _Poster(failures=1)
    session_subscriptions.configure(store, None, poster)
    monkeypatch.setattr(session_subscriptions, "_wake_at", lambda _due: None)
    pending_elicitations.record_publish(target, _prompt("elicit_secret", "Paste the API key?"))

    await _prompt_edge(target, "elicit_secret")
    with store._session("read outbox") as session:
        rows = session.execute(sa.text("SELECT * FROM session_subscription_deliveries")).all()
    assert len(rows) == 1
    assert "API key" not in repr(rows)

    store.update_conversation(target, title="renamed")
    with patch.object(session_subscriptions.time, "time", lambda: 10**10):
        assert await session_subscriptions.retry_due_deliveries() == 1
    assert poster.posted == [(watcher, _waiting("renamed", target))]
    pending_elicitations.resolve(target, "elicit_secret")


async def test_a_notice_keeps_retrying_past_thirty_failures_until_the_subscriber_recovers(
    outbox: tuple[SqlAlchemyConversationStore, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, watcher, target = outbox
    poster = _Poster(failures=40)
    session_subscriptions.configure(store, None, poster)
    monkeypatch.setattr(session_subscriptions, "_wake_at", lambda _due: None)

    await _turn_end(target, "failed", "resp_1")
    for hour in range(1, 45):
        with patch.object(session_subscriptions.time, "time", lambda h=hour: 10**10 + h * 3600):
            await session_subscriptions.retry_due_deliveries()

    assert poster.attempts == 41
    assert [session_id for session_id, _ in poster.posted] == [watcher]


async def test_a_repeated_subscribe_or_republish_never_redelivers_a_prompt_still_pending(
    client: httpx.AsyncClient, runner: _Runner
) -> None:
    watcher = (await create_test_session(client))["id"]
    worker = (await create_test_session(client, title="worker"))["id"]
    pending_elicitations.record_publish(worker, _prompt("elicit_1", "Proceed?"))
    await _subscribe(client, watcher, worker)
    await _settle()

    # Past the retention window the prompt's row survives while it is pending.
    with patch.object(session_subscriptions.time, "time", lambda: 10**10):
        assert await session_subscriptions.prune_settled_deliveries() == 0
    await _subscribe(client, watcher, worker)
    pending_elicitations.record_publish(worker, _prompt("elicit_1", "Proceed?"))
    await _settle()
    assert runner.notices(watcher) == [_waiting("worker", worker)]

    pending_elicitations.resolve(worker, "elicit_1")
    with patch.object(session_subscriptions.time, "time", lambda: 10**11):
        assert await session_subscriptions.prune_settled_deliveries() == 1


async def test_a_notice_to_a_native_subscriber_with_no_runner_stays_pending_until_it_returns(
    client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    watcher = (await create_test_session(client, executor=_NATIVE))["id"]
    target = (await create_test_session(client, title="worker"))["id"]
    store = SqlAlchemyConversationStore(db_uri)
    store.add_session_subscription(watcher, target)
    monkeypatch.setattr(session_subscriptions, "_wake_at", lambda _due: None)

    async def offline(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(sessions_facade, "_get_runner_client", offline)
    await _turn_end(target, "completed", "resp_1")

    transcript = store.list_items(watcher, limit=100, order="asc").data
    assert [item.type for item in transcript if item.type == "error"] == []
    # Still pending: claim it now, as a sweep after the lease would, to retry below.
    due = store.claim_due_subscription_deliveries(now=10**10, lease_until=10**10, limit=10)
    assert len(due) == 1

    runner = _Runner()
    async with runner.client:

        async def online(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
            return runner.client

        monkeypatch.setattr(sessions_facade, "_get_runner_client", online)
        with patch.object(session_subscriptions.time, "time", lambda: 10**10 + 1):
            assert await session_subscriptions.retry_due_deliveries() == 1
    assert runner.notices(watcher) == [
        f"[System: session worker ({target}) ended its turn (completed). "
        "Read it with sys_session_get_history.]"
    ]


async def test_a_turn_end_that_cannot_be_recorded_fails_the_edge_and_its_retry_delivers(
    client: httpx.AsyncClient, runner: _Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    watcher = (await create_test_session(client))["id"]
    worker = (await create_test_session(client, title="worker", executor=_NATIVE))["id"]
    await _subscribe(client, watcher, worker)
    edge = {"status": "idle", "turn_completed": True, "response_id": "resp_1"}

    def outage(*_args: Any, **_kwargs: Any) -> list[str]:
        raise ConnectionError("database briefly unavailable")

    with monkeypatch.context() as patched, pytest.raises(ConnectionError):
        patched.setattr(SqlAlchemyConversationStore, "enqueue_subscription_deliveries", outage)
        await client.post(
            f"/v1/sessions/{worker}/events",
            json={"type": "external_session_status", "data": edge},
        )
    await _status(client, worker, **edge)
    await _settle()

    assert runner.notices(watcher) == [
        f"[System: session worker ({worker}) ended its turn (completed). "
        "Read it with sys_session_get_history.]"
    ]


async def test_a_relayed_turn_end_retries_its_recording_until_it_lands(
    outbox: tuple[SqlAlchemyConversationStore, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, watcher, target = outbox
    poster = _Poster()
    session_subscriptions.configure(store, None, poster)
    monkeypatch.setattr(session_subscriptions, "_RETRY_BASE_S", 0)
    enqueue = store.enqueue_subscription_deliveries
    calls: list[str] = []

    def flaky(*args: Any, **kwargs: Any) -> list[str]:
        calls.append(args[2])
        if len(calls) == 1:
            raise ConnectionError("database briefly unavailable")
        return enqueue(*args, **kwargs)

    monkeypatch.setattr(store, "enqueue_subscription_deliveries", flaky)
    session_subscriptions.notify_turn_end(target, "completed", "resp_1")
    await _settle()

    assert len(calls) == 2
    assert [session_id for session_id, _ in poster.posted] == [watcher]


async def test_a_native_permission_block_wakes_subscribers_once_and_replays_while_it_stands(
    client: httpx.AsyncClient, runner: _Runner
) -> None:
    early = (await create_test_session(client))["id"]
    late = (await create_test_session(client))["id"]
    after = (await create_test_session(client))["id"]
    worker = (await create_test_session(client, title="worker", executor=_NATIVE))["id"]
    await _subscribe(client, early, worker)

    for _ in range(2):
        await _status(
            client, worker, status="running", blocked_on="permission", response_id="resp_1"
        )
        await _settle()
    await _subscribe(client, late, worker)
    await _settle()
    await _status(client, worker, status="running", response_id="resp_1")
    await _subscribe(client, after, worker)
    await _status(client, worker, status="running", blocked_on="permission", response_id="resp_2")
    await _settle()

    notice = _waiting("worker", worker)
    # The next turn's block is a new event; the repeated edge of the first is not.
    assert runner.notices(early) == [notice, notice]
    assert runner.notices(late) == [notice, notice]
    assert runner.notices(after) == [notice]


async def test_a_prompt_answered_while_the_subscriber_was_down_is_still_delivered(
    outbox: tuple[SqlAlchemyConversationStore, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, watcher, target = outbox
    poster = _Poster(failures=1)
    session_subscriptions.configure(store, None, poster)
    monkeypatch.setattr(session_subscriptions, "_wake_at", lambda _due: None)
    pending_elicitations.record_publish(target, _prompt("elicit_1", "Proceed?"))
    await _settle()
    pending_elicitations.resolve(target, "elicit_1")
    store.set_pending_elicitation_count(target, 0)

    with patch.object(session_subscriptions.time, "time", lambda: 10**10):
        assert await session_subscriptions.retry_due_deliveries() == 1
        assert await session_subscriptions.retry_due_deliveries() == 0
    assert poster.posted == [(watcher, _waiting("worker", target))]


async def test_a_subscription_that_would_close_a_loop_is_refused(
    client: httpx.AsyncClient,
) -> None:
    a, b, c = [(await create_test_session(client))["id"] for _ in range(3)]
    assert (await _subscribe(client, a, b)).status_code == 200
    assert (await _subscribe(client, b, c)).status_code == 200

    refused = await _subscribe(client, c, a)

    assert refused.status_code == 409
    assert "loop" in refused.text
    assert (await client.get(f"/v1/sessions/{c}/subscriptions")).json()["data"] == []


async def test_a_block_relayed_by_the_runner_reaches_subscribers_once(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    watcher = store.create_conversation()
    worker = store.create_conversation(title="worker")
    store.add_session_subscription(watcher.id, worker.id)
    poster = _Poster()
    session_subscriptions.configure(store, None, poster)
    release = asyncio.Event()
    release.set()
    blocked = {"type": "session.status", "status": "running", "blocked_on": "permission"}
    try:
        await orchestration._relay_runner_stream_once(
            worker.id, _ScriptedRunnerClient(release, [blocked, blocked]), store
        )
        await _settle()
    finally:
        session_subscriptions.configure(None, None, None)

    assert poster.posted == [(watcher.id, _waiting("worker", worker.id))]


async def test_a_first_attempt_queued_past_its_lease_does_not_post_a_swept_row_again(
    outbox: tuple[SqlAlchemyConversationStore, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, watcher, target = outbox
    poster = _Poster()
    session_subscriptions.configure(store, None, poster)
    queue = asyncio.Semaphore(0)
    monkeypatch.setattr(session_subscriptions, "_post_limit", queue)
    await session_subscriptions.record_turn_end(target, "completed", "resp_1")
    await asyncio.sleep(0)

    with patch.object(session_subscriptions.time, "time", lambda: 10**10):
        assert await session_subscriptions.retry_due_deliveries() == 1
        queue.release()
        await _settle()

    notice = session_subscriptions.turn_notice("worker", target, "completed")
    assert poster.posted == [(watcher, notice)]


async def test_a_relayed_block_republished_after_a_restart_reaches_only_a_new_subscriber(
    outbox: tuple[SqlAlchemyConversationStore, str, str],
) -> None:
    store, watcher, target = outbox
    late = store.create_conversation().id
    poster = _Poster()
    session_subscriptions.configure(store, None, poster)
    session_subscriptions.notify_status(target, "permission", None)
    await _settle()

    # A restarted server starts empty; the reconnecting runner republishes its status.
    session_subscriptions.configure(store, None, poster)
    session_subscriptions.notify_status(target, "permission", None)
    await _settle()
    store.add_session_subscription(late, target)
    await session_subscriptions.record_pending_prompts(late, target)
    await _settle()

    notice = _waiting("worker", target)
    assert poster.posted == [(watcher, notice), (late, notice)]


async def test_a_retry_queued_past_its_lease_does_not_post_a_row_another_sweep_settled(
    outbox: tuple[SqlAlchemyConversationStore, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, watcher, target = outbox
    other = store.create_conversation().id
    store.add_session_subscription(other, target)
    store.enqueue_subscription_deliveries([watcher, other], target, "a" * 64, 1, due_at=0)
    calls: list[str] = []
    release = asyncio.Event()

    async def post(session_id: str, _user_id: str | None, _text: str) -> None:
        calls.append(session_id)
        if len(calls) == 1:
            await release.wait()

    monkeypatch.setattr(session_subscriptions, "_POST_CONCURRENCY", 1)
    session_subscriptions.configure(store, None, post)
    now = [10**9]
    with patch.object(session_subscriptions.time, "time", lambda: now[0]):
        first = asyncio.create_task(session_subscriptions.retry_due_deliveries())
        while not calls:
            await asyncio.sleep(0)
        # The first sweep's second row waits behind a slow post until its lease ends.
        now[0] += session_subscriptions._LEASE_S + 1
        assert await session_subscriptions.retry_due_deliveries() == 2
        release.set()
        await first

    queued = ({watcher, other} - {calls[0]}).pop()
    assert calls.count(queued) == 1


async def test_listing_shows_only_watched_sessions_the_caller_can_read(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    watcher = store.create_conversation().id
    targets = sorted(store.create_conversation().id for _ in range(3))
    for user in ("owner@example.com", "reader@example.com"):
        permissions.ensure_user(user)
    for session_id in (watcher, *targets):
        permissions.grant("owner@example.com", session_id, LEVEL_EDIT)
    permissions.grant("reader@example.com", watcher, LEVEL_READ)
    permissions.grant("reader@example.com", targets[2], LEVEL_READ)
    owner = {"X-Forwarded-Email": "owner@example.com"}
    reader = {"X-Forwarded-Email": "reader@example.com"}
    for target in targets:
        response = await auth_client.post(
            f"/v1/sessions/{watcher}/subscriptions", json={"session_id": target}, headers=owner
        )
        assert response.status_code == 200, response.text

    async def listing(headers: dict[str, str], **params: Any) -> dict[str, Any]:
        response = await auth_client.get(
            f"/v1/sessions/{watcher}/subscriptions", params=params, headers=headers
        )
        assert response.status_code == 200, response.text
        return response.json()

    def ids(page: dict[str, Any]) -> list[str]:
        return [row["session_id"] for row in page["data"]]

    assert ids(await listing(owner)) == targets
    # The two unreadable targets sort first; neither is listed nor a continuation key.
    first = await listing(reader, limit=1)
    assert (ids(first), first["last_id"], first["has_more"]) == ([targets[2]], targets[2], False)

    permissions.grant("reader@example.com", targets[0], LEVEL_READ)
    page = await listing(reader, limit=1)
    assert (ids(page), page["last_id"], page["has_more"]) == ([targets[0]], targets[0], True)
    rest = await listing(reader, limit=1, after=page["last_id"])
    assert (ids(rest), rest["has_more"]) == ([targets[2]], False)

    permissions.revoke("reader@example.com", targets[0])
    assert ids(await listing(reader)) == [targets[2]]


async def test_a_full_subscriber_refuses_a_subscribe_and_bounds_its_listing_scan(
    auth_client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    watcher, target = (store.create_conversation().id for _ in range(2))
    for user in ("owner@example.com", "reader@example.com"):
        permissions.ensure_user(user)
    for session_id in (watcher, target):
        permissions.grant("owner@example.com", session_id, LEVEL_EDIT)
    permissions.grant("reader@example.com", watcher, LEVEL_EDIT)
    # Watched sessions neither user can read fill the subscriber to the cap.
    for index in range(MAX_SUBSCRIPTIONS_PER_SESSION):
        store.add_session_subscription(watcher, f"{index + 1:032x}")

    refused = await auth_client.post(
        f"/v1/sessions/{watcher}/subscriptions",
        json={"session_id": target},
        headers={"X-Forwarded-Email": "owner@example.com"},
    )
    assert refused.status_code == 409
    assert f"already watches {MAX_SUBSCRIPTIONS_PER_SESSION} sessions" in refused.text

    listed = SqlAlchemyConversationStore.list_session_subscriptions
    scanned: list[str] = []

    def counted(self: SqlAlchemyConversationStore, *args: Any, **kwargs: Any) -> list[str]:
        rows = listed(self, *args, **kwargs)
        scanned.extend(rows)
        return rows

    monkeypatch.setattr(SqlAlchemyConversationStore, "list_session_subscriptions", counted)
    page = await auth_client.get(
        f"/v1/sessions/{watcher}/subscriptions",
        params={"limit": 1},
        headers={"X-Forwarded-Email": "reader@example.com"},
    )
    assert page.status_code == 200, page.text
    assert page.json()["data"] == []
    assert len(scanned) <= MAX_SUBSCRIPTIONS_PER_SESSION


async def test_a_bare_runner_failure_status_reaches_subscribers_once(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    watcher = store.create_conversation()
    worker = store.create_conversation(title="worker", harness_override="claude-native")
    store.add_session_subscription(watcher.id, worker.id)
    poster = _Poster()
    session_subscriptions.configure(store, None, poster)
    release = asyncio.Event()
    release.set()
    # What the runner publishes when a required terminal exits mid-turn: no
    # terminal response event precedes it.
    failed = {
        "type": "session.status",
        "status": "failed",
        "error": {"code": "required_terminal_exited", "message": "Native terminal exited"},
    }
    try:
        await orchestration._relay_runner_stream_once(
            worker.id,
            _ScriptedRunnerClient(
                release, [{"type": "session.status", "status": "running"}, failed, failed]
            ),
            store,
        )
        await _settle()
    finally:
        session_subscriptions.configure(None, None, None)

    notice = session_subscriptions.turn_notice("worker", worker.id, "failed")
    assert poster.posted == [(watcher.id, notice)]


_QUIET_S = 300
_T0 = 10**10


@pytest.fixture
def quiet_outbox(
    outbox: tuple[SqlAlchemyConversationStore, str, str], monkeypatch: pytest.MonkeyPatch
) -> tuple[SqlAlchemyConversationStore, str, str]:
    """The outbox with a debounced subscription and no in-process timers."""
    store, watcher, target = outbox
    store.add_session_subscription(watcher, target, quiet_seconds=_QUIET_S)
    monkeypatch.setattr(session_subscriptions, "_wake_at", lambda _due: None)
    return store, watcher, target


async def _at(seconds: int, step: Callable[[], Awaitable[Any]]) -> Any:
    """Run *step* with the delivery module's clock at ``_T0 + seconds``."""
    with patch.object(session_subscriptions.time, "time", lambda: _T0 + seconds):
        return await step()


def _sweep(seconds: int) -> Awaitable[int]:
    return _at(seconds, session_subscriptions.retry_due_deliveries)


def _deliveries(store: SqlAlchemyConversationStore) -> dict[str, tuple[int, int, int]]:
    """Each outbox row's event key mapped to its (state, due_at, attempts)."""
    query = "SELECT event_key, state, due_at, attempts FROM session_subscription_deliveries"
    with store._session("read outbox") as session:
        rows = session.execute(sa.text(query)).all()
    return {row.event_key: (row.state, row.due_at, row.attempts) for row in rows}


def _completed(target: str) -> str:
    return session_subscriptions.turn_notice("worker", target, "completed")


def _reply(store: SqlAlchemyConversationStore, target: str, text: str) -> None:
    """Append an assistant reply to the target's transcript."""
    content = [{"type": "output_text", "text": text}]
    reply = MessageData(role="assistant", agent="worker", content=content)
    store.append(target, [NewConversationItem(type="message", response_id="r1", data=reply)])


async def test_quiet_seconds_is_stored_listed_updated_and_bounded(
    client: httpx.AsyncClient,
) -> None:
    watcher = (await create_test_session(client))["id"]
    worker = (await create_test_session(client))["id"]
    url = f"/v1/sessions/{watcher}/subscriptions"
    debounced = {"subscriber_id": watcher, "session_id": worker, "quiet_seconds": 300}

    subscribed = await client.post(url, json={"session_id": worker, "quiet_seconds": 300})
    assert subscribed.status_code == 200, subscribed.text
    assert subscribed.json() == debounced
    assert (await client.get(url)).json()["data"] == [debounced]

    await client.post(url, json={"session_id": worker, "quiet_seconds": 60})
    assert (await client.get(url)).json()["data"][0]["quiet_seconds"] == 60
    for cleared in ({"quiet_seconds": 0}, {}):
        await client.post(url, json={"session_id": worker, **cleared})
        assert (await client.get(url)).json()["data"] == [
            {"subscriber_id": watcher, "session_id": worker}
        ]
    for invalid in (-1, 86401, "soon"):
        refused = await client.post(url, json={"session_id": worker, "quiet_seconds": invalid})
        assert refused.status_code in (400, 422), refused.text


async def test_a_debounced_subscriber_hears_once_for_two_quick_turn_ends(
    quiet_outbox: tuple[SqlAlchemyConversationStore, str, str],
) -> None:
    store, watcher, target = quiet_outbox
    poster = _Poster()
    session_subscriptions.configure(store, None, poster)

    await _at(0, lambda: _turn_end(target, "completed", "resp_1"))
    await _at(10, lambda: _turn_end(target, "completed", "resp_2"))
    assert poster.posted == []

    # The first end's hold is over, but the second end replaced it.
    assert await _sweep(_QUIET_S + 1) == 0
    assert await _sweep(10 + _QUIET_S) == 1
    assert await _sweep(10**6) == 0

    assert poster.posted == [(watcher, _completed(target))]
    second = session_subscriptions._event_key("turn", "resp_2")
    assert _deliveries(store)[second][0] == 1


@pytest.mark.parametrize(("edge", "last_reply"), [("completed", "DONE: merged"), ("failed", None)])
async def test_a_later_end_posted_at_once_settles_the_held_end(
    quiet_outbox: tuple[SqlAlchemyConversationStore, str, str], edge: str, last_reply: str | None
) -> None:
    store, watcher, target = quiet_outbox
    poster = _Poster()
    session_subscriptions.configure(store, None, poster)

    await _at(0, lambda: _turn_end(target, "completed", "resp_1"))
    if last_reply is not None:
        _reply(store, target, last_reply)
    await _at(10, lambda: _turn_end(target, edge, "resp_2"))
    assert await _sweep(10**6) == 0

    assert poster.posted == [(watcher, session_subscriptions.turn_notice("worker", target, edge))]


async def test_a_hold_is_extended_while_a_child_of_the_target_runs_without_inflating_backoff(
    quiet_outbox: tuple[SqlAlchemyConversationStore, str, str],
) -> None:
    store, watcher, target = quiet_outbox
    child = store.create_conversation(kind="sub_agent", parent_conversation_id=target).id
    store.set_session_live_status(child, "running")
    poster = _Poster(failures=1)
    session_subscriptions.configure(store, None, poster)
    key = session_subscriptions._event_key("turn", "resp_1")

    await _at(0, lambda: _turn_end(target, "completed", "resp_1"))
    assert await _sweep(_QUIET_S) == 1
    assert poster.attempts == 0
    assert _deliveries(store)[key] == (0, _T0 + 2 * _QUIET_S, 0)

    store.set_session_live_status(child, "idle")
    store.set_session_live_status(target, "idle")
    assert await _sweep(2 * _QUIET_S - 1) == 0
    assert await _sweep(2 * _QUIET_S) == 1
    # The first real post failed: its backoff is a first attempt's, not the hold's claims'.
    assert poster.attempts == 1
    assert _deliveries(store)[key] == (0, _T0 + 2 * _QUIET_S + 1, 1)
    assert await _sweep(2 * _QUIET_S + 1) == 1

    assert poster.posted == [(watcher, _completed(target))]


async def test_a_hold_is_extended_while_the_target_itself_runs(
    quiet_outbox: tuple[SqlAlchemyConversationStore, str, str],
) -> None:
    store, _watcher, target = quiet_outbox
    poster = _Poster()
    session_subscriptions.configure(store, None, poster)
    await _at(0, lambda: _turn_end(target, "completed", "resp_1"))
    store.set_session_live_status(target, "running")

    assert await _sweep(_QUIET_S) == 1
    assert poster.posted == []
    key = session_subscriptions._event_key("turn", "resp_1")
    assert _deliveries(store)[key] == (0, _T0 + 2 * _QUIET_S, 0)


async def test_a_failed_busy_check_extends_the_hold_instead_of_posting(
    quiet_outbox: tuple[SqlAlchemyConversationStore, str, str],
) -> None:
    store, watcher, target = quiet_outbox
    poster = _Poster()
    session_subscriptions.configure(store, None, poster)
    key = session_subscriptions._event_key("turn", "resp_1")
    await _at(0, lambda: _turn_end(target, "completed", "resp_1"))

    async def unavailable(*_args: object) -> bool:
        raise RuntimeError("store unavailable")

    with patch.object(session_subscriptions, "_busy", unavailable):
        assert await _sweep(_QUIET_S) == 1
    assert poster.posted == []
    assert _deliveries(store)[key] == (0, _T0 + 2 * _QUIET_S, 0)

    assert await _sweep(2 * _QUIET_S) == 1
    assert poster.posted == [(watcher, _completed(target))]


async def test_a_debounced_subscriber_of_a_quiet_target_hears_once_after_the_hold(
    quiet_outbox: tuple[SqlAlchemyConversationStore, str, str],
) -> None:
    store, watcher, target = quiet_outbox
    poster = _Poster()
    session_subscriptions.configure(store, None, poster)

    await _at(0, lambda: _turn_end(target, "completed", "resp_1"))
    assert await _sweep(_QUIET_S - 1) == 0
    assert poster.posted == []
    assert await _sweep(_QUIET_S) == 1
    assert await _sweep(10**6) == 0

    assert poster.posted == [(watcher, _completed(target))]


@pytest.mark.parametrize(
    ("edge", "last_reply"),
    [
        ("failed", None),
        ("cancelled", None),
        ("prompt", None),
        ("completed", "Merged the fix.\nDONE: PR is green"),
        ("completed", "USER NEEDED: which region?"),
        ("completed", "Waiting on CI.\n  PARKED until tomorrow"),
    ],
)
async def test_failures_cancels_prompts_and_named_ends_skip_the_hold(
    quiet_outbox: tuple[SqlAlchemyConversationStore, str, str], edge: str, last_reply: str | None
) -> None:
    store, watcher, target = quiet_outbox
    poster = _Poster()
    session_subscriptions.configure(store, None, poster)
    if last_reply is not None:
        _reply(store, target, last_reply)

    if edge == "prompt":
        await _prompt_edge(target, "elicit_1")
    else:
        await _turn_end(target, edge, "resp_1")

    assert [session_id for session_id, _ in poster.posted] == [watcher]


async def test_a_completed_end_without_a_named_line_is_held(
    quiet_outbox: tuple[SqlAlchemyConversationStore, str, str],
) -> None:
    store, _watcher, target = quiet_outbox
    poster = _Poster()
    session_subscriptions.configure(store, None, poster)
    _reply(store, target, "Not DONE yet, still going.")

    await _turn_end(target, "completed", "resp_1")

    assert poster.posted == []


async def test_a_plain_subscription_never_reads_the_transcript_for_markers(
    outbox: tuple[SqlAlchemyConversationStore, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, watcher, target = outbox
    poster = _Poster()
    session_subscriptions.configure(store, None, poster)

    async def unreadable(*_args: object) -> bool:
        raise AssertionError("read the transcript for a plain subscription")

    monkeypatch.setattr(session_subscriptions, "_names_a_settled_end", unreadable)
    await _turn_end(target, "completed", "resp_1")

    assert poster.posted == [(watcher, _completed(target))]


async def test_a_held_notice_is_delivered_by_the_sweep_after_a_restart(
    quiet_outbox: tuple[SqlAlchemyConversationStore, str, str],
) -> None:
    store, watcher, target = quiet_outbox
    session_subscriptions.configure(store, None, _Poster())
    await _at(0, lambda: _turn_end(target, "completed", "resp_1"))

    restarted = _Poster()
    session_subscriptions.configure(store, None, restarted)
    assert await _sweep(_QUIET_S) == 1
    assert await _sweep(10**6) == 0

    assert restarted.posted == [(watcher, _completed(target))]


async def test_the_subscribe_tool_passes_quiet_seconds(client: httpx.AsyncClient) -> None:
    watcher = (await create_test_session(client))["id"]
    worker = (await create_test_session(client))["id"]

    output = await execute_tool(
        tool_name="sys_session_subscribe",
        arguments=json.dumps({"session_id": worker, "quiet_seconds": 120}),
        server_client=client,
        conversation_id=watcher,
        agent_spec=AgentSpec(spec_version=1),
    )

    expected = {"subscriber_id": watcher, "session_id": worker, "quiet_seconds": 120}
    assert json.loads(output) == expected
