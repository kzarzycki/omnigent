"""A session whose runner died comes back without a person when the server holds work for it."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omnigent.runner.subagent_work import SUBAGENT_DISPATCH_ID_LABEL_KEY
from omnigent.server import session_revival
from omnigent.server.schemas import ErrorDetail
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_HOST = "0" * 31 + "1"
_LOST = ErrorDetail(code="runner_disconnected", message="Runner disconnected unexpectedly.")


@pytest.fixture
def store(db_uri: str) -> SqlAlchemyConversationStore:
    return SqlAlchemyConversationStore(db_uri)


@pytest.fixture
def relaunches(
    store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch
) -> Iterator[list[tuple[str, bool]]]:
    """Revival on, recording each relaunch as (session, its init resumes the turn).

    The ``retry_session`` ladder is faked down to the two calls that decide:
    the host-launch helper's veto and the runner init's resume.
    """
    from omnigent.server.routes.sessions import routes_events

    calls: list[tuple[str, bool]] = []

    async def _retry(*, session_id: str, **_: Any) -> None:
        row = store.get_conversation(session_id)
        if row.runner_id.startswith("relaunched-"):
            return  # already connected: nothing to launch or initialize
        session_revival.check_launch(row)
        store.replace_runner_id(session_id, f"relaunched-{len(calls)}")
        resumed = session_revival.take_resume(session_id, suppressed=False) is not None
        calls.append((session_id, resumed))

    monkeypatch.setattr(routes_events, "_retry_session_single_flight", _retry)
    session_revival.configure(object(), store)
    yield calls
    session_revival.configure(None, None)


async def _sweep(
    store: SqlAlchemyConversationStore,
    ids: list[str],
    *,
    stopped: bool = False,
    fail_idle_top_level: bool = False,
) -> None:
    """Run the offline sweep over *ids* and let the relaunches it started finish."""
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    rows = [store.get_conversation(i) for i in ids]
    if stopped:
        for row in rows:
            sessions_module._intentional_stop_sessions[row.id] = row.runner_id
    try:
        await sessions_module._mark_runner_sessions_offline(
            rows, _LOST, store, fail_idle_top_level=fail_idle_top_level
        )
        await asyncio.gather(*session_revival._tasks.values())
    finally:
        for i in ids:
            sessions_module._intentional_stop_sessions.pop(i, None)
            sessions_module._session_status_cache.pop(i, None)
            session_stream.close(i)


def _lead_with_lanes(
    store: SqlAlchemyConversationStore, lanes: int, *, archived: bool = False
) -> tuple[str, list[str]]:
    """An idle host-bound lead and its lanes, all on one runner, the lanes mid-turn."""
    lead = store.create_conversation(host_id=_HOST, workspace="/ws", runner_id="runner-shared")
    if archived:
        store.update_conversation(lead.id, archived=True)
    ids = []
    for n in range(lanes):
        lane = store.create_conversation(
            kind="sub_agent",
            parent_conversation_id=lead.id,
            title=f"lane {n}",
            host_id=_HOST,
            workspace="/ws",
            runner_id="runner-shared",
        )
        store.set_session_live_status(lane.id, "running")
        ids.append(lane.id)
    return lead.id, ids


@pytest.mark.asyncio
async def test_lanes_lost_with_their_lead_s_runner_relaunch_the_idle_lead_once(
    store: SqlAlchemyConversationStore, relaunches: list[tuple[str, bool]]
) -> None:
    lead, lanes = _lead_with_lanes(store, 3)

    await _sweep(store, lanes)
    # A later sweep re-sends the notices for the same dead runner.
    await _sweep(store, lanes)

    assert relaunches == [(lead, False)]


@pytest.mark.asyncio
async def test_lanes_never_relaunch_an_archived_lead_and_log_once_each(
    store: SqlAlchemyConversationStore,
    relaunches: list[tuple[str, bool]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _lead, lanes = _lead_with_lanes(store, 2, archived=True)

    with caplog.at_level(logging.WARNING):
        await _sweep(store, lanes)
        await _sweep(store, lanes)

    assert relaunches == []
    waits = [r for r in caplog.records if "has no runner and can't be relaunched" in r.message]
    assert waits == [], "an archived lead takes no results, so nothing waits for it"


@pytest.mark.asyncio
async def test_a_lead_that_cannot_be_relaunched_is_logged_once_per_lane(
    store: SqlAlchemyConversationStore,
    relaunches: list[tuple[str, bool]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    lead = store.create_conversation(runner_id="runner-shared")  # no host to relaunch on
    lanes = []
    for n in range(2):
        lane = store.create_conversation(
            kind="sub_agent",
            parent_conversation_id=lead.id,
            title=f"l{n}",
            runner_id="runner-shared",
        )
        store.set_session_live_status(lane.id, "running")
        lanes.append(lane.id)

    with caplog.at_level(logging.WARNING):
        await _sweep(store, lanes)
        await _sweep(store, lanes)

    assert relaunches == []
    waits = [r for r in caplog.records if "has no runner and can't be relaunched" in r.message]
    assert len(waits) == 2


@pytest.mark.asyncio
async def test_a_deliberate_stop_of_the_shared_runner_does_not_relaunch_the_lead(
    store: SqlAlchemyConversationStore, relaunches: list[tuple[str, bool]]
) -> None:
    _lead, lanes = _lead_with_lanes(store, 2)

    await _sweep(store, lanes, stopped=True)

    assert relaunches == []


@pytest.mark.asyncio
async def test_a_top_level_session_lost_mid_turn_is_relaunched_to_continue(
    store: SqlAlchemyConversationStore, relaunches: list[tuple[str, bool]]
) -> None:
    lead, lanes = _lead_with_lanes(store, 1)
    store.set_session_live_status(lead, "running")

    await _sweep(store, [lead, *lanes])

    # The lane is its lead's to resume; the lead continues its own turn.
    assert relaunches == [(lead, True)]


@pytest.mark.asyncio
async def test_a_session_whose_runner_never_started_gets_no_turn_to_continue(
    store: SqlAlchemyConversationStore,
    relaunches: list[tuple[str, bool]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    lead, _lanes = _lead_with_lanes(store, 0)

    with caplog.at_level(logging.WARNING):
        await _sweep(store, [lead], fail_idle_top_level=True)

    assert any("failed_before_start" in r.message for r in caplog.records)
    assert relaunches == []


@pytest.mark.asyncio
async def test_a_lane_briefed_without_a_dispatch_reaches_its_lead_as_a_failed_turn(
    store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lead's runner ignores a ``lost`` notice naming no dispatch, so it isn't sent one."""
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions import orchestration

    lead = store.create_conversation(runner_id="runner-lead")
    plain = store.create_conversation(
        kind="sub_agent", parent_conversation_id=lead.id, title="plain", runner_id="runner-lane"
    )
    sent = store.create_conversation(
        kind="sub_agent", parent_conversation_id=lead.id, title="sent", runner_id="runner-lane"
    )
    store.set_labels(sent.id, {SUBAGENT_DISPATCH_ID_LABEL_KEY: "subagent_dispatch1"})
    posted: dict[str, dict[str, Any]] = {}

    def _lead_runner(request: httpx.Request) -> httpx.Response:
        posted[request.url.path.split("/")[3]] = json.loads(request.content)["data"]
        return httpx.Response(204)

    lead_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_lead_runner), base_url="http://runner"
    )

    async def _runner_client(session_id: str, *_args: Any, **_kwargs: Any) -> Any:
        return lead_client if session_id == lead.id else None

    monkeypatch.setattr(sessions_module, "_get_runner_client", _runner_client)
    try:
        for lane in (plain.id, sent.id):
            await orchestration._notify_parent_of_lost_subagent(lane, _LOST, store)
    finally:
        await lead_client.aclose()

    assert (posted[plain.id]["status"], posted[plain.id]["turn_outcome"]) == ("failed", "failed")
    assert (posted[sent.id]["status"], posted[sent.id]["work_id"]) == (
        "lost",
        "subagent_dispatch1",
    )


def test_revival_is_one_attempt_per_dead_runner_and_capped_per_hour(
    store: SqlAlchemyConversationStore, relaunches: list[tuple[str, bool]]
) -> None:
    conv = store.create_conversation(host_id=_HOST, workspace="/ws", runner_id="runner-0")

    async def _run() -> list[bool]:
        started = []
        for n in range(5):
            row = store.replace_runner_id(conv.id, f"runner-{n}")
            # The same death reported twice, and once while its relaunch is running.
            started += [session_revival.revive(row, why="test")]
            started += [session_revival.revive(row, why="test")]
            await asyncio.gather(*session_revival._tasks.values())
            started += [session_revival.revive(row, why="test")]
        return started

    started = asyncio.run(_run())

    assert started.count(True) == 2 * session_revival.MAX_REVIVES_PER_HOUR
    assert len(relaunches) == session_revival.MAX_REVIVES_PER_HOUR


@pytest.mark.asyncio
async def test_a_lane_of_a_deleted_lead_relaunches_nothing(
    store: SqlAlchemyConversationStore, relaunches: list[tuple[str, bool]]
) -> None:
    from omnigent.server.routes._sessions import orchestration

    lead, [lane] = _lead_with_lanes(store, 1)
    snapshot = store.get_conversation(lane)
    await store.delete_conversation(lead)

    assert not await orchestration._revive_subagent_parent(snapshot, None, store)
    assert relaunches == []


@pytest.mark.asyncio
async def test_a_lane_lost_first_does_not_cost_its_running_lead_the_resume(
    store: SqlAlchemyConversationStore, relaunches: list[tuple[str, bool]]
) -> None:
    lead, lanes = _lead_with_lanes(store, 1)
    store.set_session_live_status(lead, "running")

    # The lane's loss relaunches the lead before the lead's own loss is processed.
    await _sweep(store, [*lanes, lead])

    assert relaunches == [(lead, True)]


@pytest.mark.asyncio
async def test_a_lane_on_its_own_runner_never_relaunches_a_stopped_lead(
    store: SqlAlchemyConversationStore, relaunches: list[tuple[str, bool]]
) -> None:
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions import orchestration

    lead, [lane] = _lead_with_lanes(store, 1)
    store.replace_runner_id(lane, "runner-lane")
    sessions_module._intentional_stop_sessions[lead] = "runner-shared"
    try:
        await orchestration._revive_subagent_parent(store.get_conversation(lane), None, store)
        await asyncio.gather(*session_revival._tasks.values())
    finally:
        sessions_module._intentional_stop_sessions.pop(lead, None)

    assert relaunches == []


@pytest.mark.asyncio
async def test_a_lead_stopped_while_its_relaunch_waits_is_not_launched(
    store: SqlAlchemyConversationStore, relaunches: list[tuple[str, bool]]
) -> None:
    from omnigent.server.routes import sessions as sessions_module

    lead, _ = _lead_with_lanes(store, 0)
    assert session_revival.revive(store.get_conversation(lead), why="result")
    sessions_module._intentional_stop_sessions[lead] = "runner-shared"
    try:
        await asyncio.gather(*session_revival._tasks.values())
    finally:
        sessions_module._intentional_stop_sessions.pop(lead, None)

    assert relaunches == []


@pytest.mark.asyncio
async def test_a_turn_the_person_cancelled_is_not_resumed(
    store: SqlAlchemyConversationStore, relaunches: list[tuple[str, bool]]
) -> None:
    from omnigent.server.routes import sessions as sessions_module

    lead, lanes = _lead_with_lanes(store, 1)
    store.set_session_live_status(lead, "running")
    sessions_module._interrupt_fenced_sessions.add(lead)
    try:
        await _sweep(store, [lead])
        assert relaunches == []
        # A lane's result still brings the lead back, but not to continue its turn.
        await _sweep(store, [lead, *lanes])
    finally:
        sessions_module._interrupt_fenced_sessions.discard(lead)

    assert relaunches == [(lead, False)]


@pytest.mark.asyncio
async def test_a_stop_during_the_retry_ladder_prevents_the_launch(
    store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real ladder runs; the stop lands while it resolves the runner client."""
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions import helpers, orchestration
    from omnigent.server.routes.sessions import routes_events

    lead, _ = _lead_with_lanes(store, 0)
    conv = store.get_conversation(lead)
    launched: list[str] = []

    async def _client(*_: Any, **__: Any) -> None:
        sessions_module._intentional_stop_sessions[lead] = conv.runner_id

    async def _local(*_: Any, **__: Any) -> None:
        return None

    async def _launch(fresh: Any, *_: Any) -> Any:
        launched.append(fresh.id)
        raise RuntimeError("launched")

    async def _no_wake(**_: Any) -> bool:
        return False

    monkeypatch.setattr(routes_events, "_get_runner_client", _client)
    monkeypatch.setattr(orchestration, "_get_runner_client", _local)
    monkeypatch.setattr(routes_events, "_raise_if_runner_on_another_replica", _local)
    monkeypatch.setattr(orchestration, "_maybe_wake_stale_resumable_managed_sandbox", _no_wake)
    monkeypatch.setattr(sessions_module, "_HOST_BOUND_RUNNER_CONNECT_GRACE_S", 0)
    monkeypatch.setattr(helpers, "_launch_runner_on_host_locked", _launch)
    app = SimpleNamespace(state=SimpleNamespace(host_registry={_HOST: object()}))
    session_revival.configure(app, store)
    try:
        assert session_revival.revive(conv, why="result")
        await asyncio.gather(*session_revival._tasks.values())
    finally:
        session_revival.configure(None, None)
        sessions_module._intentional_stop_sessions.pop(lead, None)

    assert launched == []
