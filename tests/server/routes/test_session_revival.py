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
        await session_revival.check_launch(session_id, (row.runner_id,), store)
        relaunched = store.replace_runner_id(session_id, f"relaunched-{len(calls)}")
        calls.append((session_id, session_revival.take_resume(relaunched) is not None))

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


class _Host(dict[str, Any]):
    """A host registry that launches nothing: it records each launch frame it is sent."""

    def __init__(self, on_admit: Any = None) -> None:
        super().__init__({_HOST: SimpleNamespace(pending_launches={})})
        self.on_admit = on_admit
        self.sent: list[str] = []

    async def admit_launch(self, conn: Any, session_id: str) -> None:
        if self.on_admit is not None:
            self.on_admit()

    def send_text(self, conn: Any, frame: str) -> None:
        self.sent.append(frame)
        raise ConnectionError("no host here")


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    """The real host-launch body down to the frame, with nothing launched."""
    from omnigent.server.routes._sessions import helpers

    async def _not_shared(*_: Any) -> bool:
        return True

    monkeypatch.setattr(helpers, "_ancestor_shares_runner", _not_shared)
    monkeypatch.setattr(helpers, "_resolve_harness", lambda conv: None)
    return _Host()


@pytest.mark.asyncio
async def test_a_stop_during_the_retry_ladder_prevents_the_launch(
    store: SqlAlchemyConversationStore, host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real ladder runs; the stop lands while it resolves the runner client."""
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions import orchestration
    from omnigent.server.routes.sessions import routes_events

    lead, _ = _lead_with_lanes(store, 0)
    conv = store.get_conversation(lead)

    async def _client(*_: Any, **__: Any) -> None:
        sessions_module._intentional_stop_sessions[lead] = conv.runner_id

    async def _local(*_: Any, **__: Any) -> None:
        return None

    async def _no_wake(**_: Any) -> bool:
        return False

    monkeypatch.setattr(routes_events, "_get_runner_client", _client)
    monkeypatch.setattr(orchestration, "_get_runner_client", _local)
    monkeypatch.setattr(routes_events, "_raise_if_runner_on_another_replica", _local)
    monkeypatch.setattr(orchestration, "_maybe_wake_stale_resumable_managed_sandbox", _no_wake)
    monkeypatch.setattr(sessions_module, "_HOST_BOUND_RUNNER_CONNECT_GRACE_S", 0)
    session_revival.configure(SimpleNamespace(state=SimpleNamespace(host_registry=host)), store)
    try:
        assert session_revival.revive(conv, why="result")
        await asyncio.gather(*session_revival._tasks.values())
    finally:
        session_revival.configure(None, None)
        sessions_module._intentional_stop_sessions.pop(lead, None)

    assert host.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("withdrawal", ["stop", "archive"])
async def test_a_stop_or_archive_during_host_admission_prevents_the_launch(
    store: SqlAlchemyConversationStore, host: _Host, withdrawal: str
) -> None:
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions import helpers

    lead, _ = _lead_with_lanes(store, 0)
    conv = store.get_conversation(lead)

    def _withdraw() -> None:
        if withdrawal == "stop":
            sessions_module._intentional_stop_sessions[lead] = conv.runner_id
        else:
            store.update_conversation(lead, archived=True)

    host.on_admit = _withdraw
    token = session_revival._reviving.set(lead)
    try:
        with pytest.raises(session_revival.RevivalWithdrawn):
            await helpers._launch_runner_on_host_impl(conv, store, host, host[_HOST])
    finally:
        session_revival._reviving.reset(token)
        sessions_module._intentional_stop_sessions.pop(lead, None)

    assert host.sent == []


@pytest.mark.asyncio
async def test_a_launch_a_person_asked_for_is_never_vetoed(
    store: SqlAlchemyConversationStore, host: _Host
) -> None:
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions import helpers

    lead, _ = _lead_with_lanes(store, 0)
    conv = store.get_conversation(lead)
    sessions_module._intentional_stop_sessions[lead] = conv.runner_id
    try:
        await helpers._launch_runner_on_host_impl(conv, store, host, host[_HOST])
    finally:
        sessions_module._intentional_stop_sessions.pop(lead, None)

    assert len(host.sent) == 1


@pytest.mark.asyncio
async def test_a_lane_s_result_during_its_lead_s_failure_bookkeeping_keeps_the_resume(
    store: SqlAlchemyConversationStore,
    relaunches: list[tuple[str, bool]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lead's sweep has published ``failed`` and awaits its labels when the lane reports."""
    from omnigent.server.routes._sessions import orchestration

    lead, [lane] = _lead_with_lanes(store, 1)
    store.replace_runner_id(lane, "runner-lane")
    store.set_session_live_status(lead, "running")
    entered, release = asyncio.Event(), asyncio.Event()
    persist = orchestration._persist_session_status_error_labels

    async def _paused(session_id: str, *args: Any, **kwargs: Any) -> None:
        if session_id == lead:
            entered.set()
            await release.wait()
        await persist(session_id, *args, **kwargs)

    monkeypatch.setattr(orchestration, "_persist_session_status_error_labels", _paused)
    sweep = asyncio.create_task(_sweep(store, [lead]))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await orchestration._revive_subagent_parent(store.get_conversation(lane), None, store)
        await asyncio.gather(*session_revival._tasks.values())
    finally:
        release.set()
        await sweep

    assert relaunches == [(lead, True)]


@pytest.mark.asyncio
async def test_a_runner_launched_as_its_session_is_archived_is_torn_down_by_the_archive(
    store: SqlAlchemyConversationStore, host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The archive commits during the launch veto's own row read, so the frame goes out."""
    import threading

    from omnigent.host.frames import decode_host_frame
    from omnigent.runner.identity import token_bound_runner_id
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions import helpers, orchestration

    lead, _ = _lead_with_lanes(store, 0)
    conv = store.get_conversation(lead)
    reading, archived = threading.Event(), threading.Event()
    armed = False
    get_meta = store._get_meta

    def _meta(session_id: str) -> Any:
        nonlocal armed
        if armed and session_id == lead:
            armed = False
            reading.set()
            assert archived.wait(10)
        return get_meta(session_id)

    check_launch = session_revival.check_launch

    async def _armed_check(*args: Any) -> None:
        nonlocal armed
        armed = True
        await check_launch(*args)

    stopped: list[str] = []

    async def _stop_host_runner(_sid: str, _host: str, runner_id: str, *_: Any, **__: Any) -> bool:
        stopped.append(runner_id)
        return True

    async def _nothing(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(store, "_get_meta", _meta)
    monkeypatch.setattr(session_revival, "check_launch", _armed_check)
    monkeypatch.setattr(sessions_module, "_stop_session_host_runner", _stop_host_runner)
    monkeypatch.setattr(sessions_module, "_best_effort_stop", _nothing)
    monkeypatch.setattr(sessions_module, "_ARCHIVE_STOP_UNDO_GRACE_S", 0)
    token = session_revival._reviving.set(lead)
    launch = asyncio.create_task(
        helpers._launch_runner_on_host_impl(conv, store, host, host[_HOST])
    )
    try:
        assert await asyncio.to_thread(reading.wait, 10)
        await asyncio.to_thread(store.update_conversation, lead, archived=True)
        archived.set()
        attempt = await launch
        orchestration._spawn_archive_stop(lead, store, None, host)
        await orchestration._pending_archive_stops[lead]
    finally:
        archived.set()
        session_revival._reviving.reset(token)
        sessions_module._intentional_stop_sessions.pop(lead, None)

    [frame] = host.sent
    launched = token_bound_runner_id(decode_host_frame(frame).binding_token)
    assert attempt.runner_id == launched == store.get_conversation(lead).runner_id
    assert stopped == [launched]
