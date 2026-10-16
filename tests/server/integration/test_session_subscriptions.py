"""Session subscriptions: one session hears when another ends a turn or blocks."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import sqlalchemy as sa

from omnigent.runner.tool_dispatch import execute_tool
from omnigent.runtime import pending_elicitations
from omnigent.server import session_subscriptions
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ
from omnigent.server.routes import sessions as sessions_facade
from omnigent.server.routes._sessions import orchestration
from omnigent.server.schemas import ErrorDetail
from omnigent.spec.types import AgentSpec
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

    notice = f"[System: session worker ({worker}) is waiting on a prompt: Run rm -rf build?.]"
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

    assert runner.notices(watcher) == [
        f"[System: session worker ({worker}) is waiting on a prompt: Allow it?.]"
    ]


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
        await session_subscriptions._deliver_prompt(target, prompt, "approval", only=watcher)

    assert len(poster.posted) == 2


async def test_a_replayed_older_turn_is_not_delivered_again(
    outbox: tuple[SqlAlchemyConversationStore, str, str],
) -> None:
    store, _watcher, target = outbox
    poster = _Poster()
    session_subscriptions.configure(store, None, poster)

    for response_id in ("resp_1", "resp_2", "resp_1"):
        await session_subscriptions._deliver_turn_end(target, "completed", response_id)

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

    assert sorted(runner.notices(watcher)) == [
        f"[System: session worker ({worker}) is waiting on a prompt: First?.]",
        f"[System: session worker ({worker}) is waiting on a prompt: Second?.]",
    ]


async def test_a_recovered_subscriber_receives_the_notice_a_failed_post_left_pending(
    outbox: tuple[SqlAlchemyConversationStore, str, str],
) -> None:
    store, watcher, target = outbox
    poster = _Poster(failures=1)
    session_subscriptions.configure(store, None, poster)

    await session_subscriptions._deliver_turn_end(target, "failed", "resp_1")
    assert (poster.attempts, poster.posted) == (1, [])
    await _settle()

    assert poster.posted == [
        (
            watcher,
            f"[System: session worker ({target}) ended its turn (failed). "
            "Read it with sys_session_get_history.]",
        )
    ]
    # Settled: neither a sweep nor a repeated edge posts it again.
    assert await session_subscriptions.retry_due_deliveries() == 0
    await session_subscriptions._deliver_turn_end(target, "failed", "resp_1")
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
    with pytest.raises(asyncio.CancelledError):
        await session_subscriptions._deliver_turn_end(target, "completed", "resp_1")
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

    await session_subscriptions._deliver_turn_end(target, "completed", "resp_1")
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

    await session_subscriptions._deliver_turn_end(target, "completed", "resp_1")

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

    await session_subscriptions._deliver_prompt(target, "elicit_secret", "Paste the API key?")
    with store._session("read outbox") as session:
        rows = session.execute(sa.text("SELECT * FROM session_subscription_deliveries")).all()
    assert len(rows) == 1
    assert "API key" not in repr(rows)

    store.update_conversation(target, title="renamed")
    with patch.object(session_subscriptions.time, "time", lambda: 10**10):
        assert await session_subscriptions.retry_due_deliveries() == 1
    assert poster.posted == [
        (
            watcher,
            f"[System: session renamed ({target}) is waiting on a prompt: Paste the API key?.]",
        )
    ]
    pending_elicitations.resolve(target, "elicit_secret")


async def test_a_prompt_answered_before_its_retry_is_settled_without_a_notice(
    outbox: tuple[SqlAlchemyConversationStore, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _watcher, target = outbox
    poster = _Poster(failures=1)
    session_subscriptions.configure(store, None, poster)
    monkeypatch.setattr(session_subscriptions, "_wake_at", lambda _due: None)
    pending_elicitations.record_publish(target, _prompt("elicit_1", "Proceed?"))

    await session_subscriptions._deliver_prompt(target, "elicit_1", "Proceed?")
    pending_elicitations.resolve(target, "elicit_1")
    store.set_pending_elicitation_count(target, 0)

    with patch.object(session_subscriptions.time, "time", lambda: 10**10):
        assert await session_subscriptions.retry_due_deliveries() == 1
        assert await session_subscriptions.retry_due_deliveries() == 0
    assert (poster.attempts, poster.posted) == (1, [])


async def test_a_notice_keeps_retrying_past_thirty_failures_until_the_subscriber_recovers(
    outbox: tuple[SqlAlchemyConversationStore, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, watcher, target = outbox
    poster = _Poster(failures=40)
    session_subscriptions.configure(store, None, poster)
    monkeypatch.setattr(session_subscriptions, "_wake_at", lambda _due: None)

    await session_subscriptions._deliver_turn_end(target, "failed", "resp_1")
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
    assert runner.notices(watcher) == [
        f"[System: session worker ({worker}) is waiting on a prompt: Proceed?.]"
    ]

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
    await session_subscriptions._deliver_turn_end(target, "completed", "resp_1")

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
