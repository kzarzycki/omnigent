"""A session whose runner died comes back without a person when the server holds work for it."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
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
    """Revival on, with the relaunch itself recorded instead of run."""
    calls: list[tuple[str, bool]] = []

    async def _relaunch(session_id: str, resume_interrupted_turn: bool) -> None:
        calls.append((session_id, resume_interrupted_turn))

    monkeypatch.setattr(session_revival, "_relaunch", _relaunch)
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
