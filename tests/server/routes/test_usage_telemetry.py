"""Tests for ``GET /v1/usage/telemetry``: one per section, plus user scoping."""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.entities import FunctionCallData, MessageData, NewConversationItem
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.routes.usage_telemetry import (
    _build_telemetry,
    create_usage_telemetry_router,
)
from omnigent.stores.conversation_store import SCHEDULED_TASK_RUN_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore

_DAY = 86_400
_NOW = 1_784_678_400 + 3_600  # 2026-07-22T01:00:00Z
_AGENT_ID = "0123456789abcdef0123456789abcdef"


def _uid(seed: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


@pytest.fixture()
def store(db_uri: str, monkeypatch: pytest.MonkeyPatch) -> SqlAlchemyConversationStore:
    monkeypatch.setattr("omnigent.db.utils.now_epoch", lambda: _NOW)
    monkeypatch.setattr(
        "omnigent.stores.conversation_store.sqlalchemy_store.now_epoch", lambda: _NOW
    )
    return SqlAlchemyConversationStore(db_uri)


def _session(store: SqlAlchemyConversationStore, title: str, **kwargs: object) -> str:
    return store.create_conversation(title=title, agent_id=_AGENT_ID, **kwargs).id  # type: ignore[arg-type]


def _build(
    store: SqlAlchemyConversationStore,
    user_id: str | None = None,
    *,
    tasks: SqlAlchemyScheduledTaskStore | None = None,
    since: int = _NOW - 7 * _DAY,
    max_sessions: int = 200,
):
    return _build_telemetry(
        store, tasks, None, user_id, since=since, until=_NOW, max_sessions=max_sessions
    )


def test_daily_cost_is_windowed(store: SqlAlchemyConversationStore) -> None:
    store.add_daily_cost("local", "2026-07-22", 1.5)
    store.add_daily_cost("local", "2026-07-18", 2.0)
    store.add_daily_cost("local", "2026-06-01", 9.0)  # before the window
    report = _build(store)
    assert [(d.day, d.cost_usd) for d in report.daily_cost] == [
        ("2026-07-18", 2.0),
        ("2026-07-22", 1.5),
    ]


def test_models_sum_tokens_and_cost_over_sessions(store: SqlAlchemyConversationStore) -> None:
    for title, cost in (("a", 1.0), ("b", 2.0)):
        sid = _session(store, title)
        store.set_session_usage(
            sid,
            {
                "total_cost_usd": cost,
                "by_model": {
                    "model-x": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cache_read_input_tokens": 100,
                        "total_cost_usd": cost,
                    },
                    "unpriced": {"input_tokens": 7},
                },
            },
        )
    report = _build(store)
    by_name = {m.model: m for m in report.models}
    assert by_name["model-x"].input_tokens == 20
    assert by_name["model-x"].output_tokens == 10
    assert by_name["model-x"].cache_read_input_tokens == 200
    assert by_name["model-x"].cost_usd == 3.0
    assert by_name["model-x"].sessions == 2
    assert by_name["unpriced"].cost_usd is None
    assert by_name["unpriced"].input_tokens == 14


def test_sessions_tree_items_and_tool_calls(store: SqlAlchemyConversationStore) -> None:
    root = _session(store, "lead", labels={"omni.role": "lead"})
    child = store.create_conversation(
        kind="sub_agent", parent_conversation_id=root, agent_id=_AGENT_ID, title="worker"
    ).id
    store.append(
        root,
        [
            NewConversationItem(
                type="message",
                response_id="r1",
                data=MessageData(role="user", content=[{"type": "input_text", "text": "hi"}]),
            ),
            NewConversationItem(
                type="message",
                response_id="r1",
                data=MessageData(
                    role="assistant", agent="a", content=[{"type": "output_text", "text": "yo"}]
                ),
            ),
            NewConversationItem(
                type="function_call",
                response_id="r1",
                data=FunctionCallData(agent="a", name="Bash", arguments="{}", call_id="c1"),
            ),
            NewConversationItem(
                type="function_call",
                response_id="r1",
                data=FunctionCallData(agent="a", name="Bash", arguments="{}", call_id="c2"),
            ),
            NewConversationItem(
                type="native_tool",
                response_id="r1",
                data={"item": {"type": "web_search_call"}},  # type: ignore[arg-type]
            ),
        ],
    )
    other = _session(store, "watcher")
    store.add_session_subscription(other, root)

    report = _build(store)
    rows = {s.id: s for s in report.sessions}
    assert set(rows) == {root, child, other}
    assert rows[root].parent_id is None
    assert rows[child].parent_id == root
    assert rows[child].root_id == root
    assert rows[root].labels["omni.role"] == "lead"
    assert rows[root].items == 5
    assert rows[root].turns == 1
    assert rows[root].tool_calls == {"Bash": 2, "web_search_call": 1}
    assert rows[child].tool_calls == {}
    assert rows[other].subscribes_to == [root]


def test_scheduled_tasks_with_run_outcomes(
    store: SqlAlchemyConversationStore, db_uri: str
) -> None:
    tasks = SqlAlchemyScheduledTaskStore(db_uri)
    task_id = _uid("task")
    tasks.create(
        scheduled_task_id=task_id,
        name="nightly",
        prompt="go",
        rrule="FREQ=DAILY",
        user_id=None,
        agent_id=_AGENT_ID,
        timezone="UTC",
        workspace="/w",
    )
    for index, (status, code) in enumerate(
        [("succeeded", None), ("failed", "timeout"), ("skipped", None), ("succeeded", None)]
    ):
        tasks.create_run(
            _uid(f"run{index}"),
            task_id,
            status,
            _NOW - index * 3600,
            error_code=code,
        )
    tasks.create_run(_uid("old"), task_id, "failed", _NOW - 30 * _DAY)  # before the window
    report = _build(store, tasks=tasks)
    [row] = report.scheduled_tasks
    assert (row.name, row.state, row.workspace) == ("nightly", "active", "/w")
    assert (row.runs, row.succeeded, row.failed, row.skipped) == (4, 2, 1, 1)
    assert row.last_error == "timeout"


def test_sidebar_counts_unarchived_top_level(store: SqlAlchemyConversationStore) -> None:
    _session(store, "plain")
    _session(store, "from a run", labels={SCHEDULED_TASK_RUN_LABEL_KEY: "run-1"})
    archived = _session(store, "archived")
    store.update_conversation(archived, archived=True)
    report = _build(store)
    assert (report.sidebar.unarchived, report.sidebar.from_scheduled_tasks) == (2, 1)


def test_another_users_sessions_are_not_returned(
    store: SqlAlchemyConversationStore, db_uri: str
) -> None:
    perms = SqlAlchemyPermissionStore(db_uri)
    mine = _session(store, "mine")
    theirs = _session(store, "theirs")
    perms.grant("alice", mine, LEVEL_OWNER)
    perms.grant("bob", theirs, LEVEL_OWNER)
    store.add_daily_cost("alice", "2026-07-22", 1.0)
    store.add_daily_cost("bob", "2026-07-22", 50.0)

    alice = _build(store, "alice")
    assert [s.id for s in alice.sessions] == [mine]
    assert [d.cost_usd for d in alice.daily_cost] == [1.0]
    bob = _build(store, "bob")
    assert [s.id for s in bob.sessions] == [theirs]


def test_session_cap_is_reported(store: SqlAlchemyConversationStore) -> None:
    for index in range(3):
        _session(store, f"s{index}")
    report = _build(store, max_sessions=2)
    assert len(report.sessions) == 2
    assert "sessions" in report.truncated


def test_endpoint_window_bounds(store: SqlAlchemyConversationStore) -> None:
    app = FastAPI()
    app.include_router(create_usage_telemetry_router(store), prefix="/v1")
    client = TestClient(app)
    ok = client.get(f"/v1/usage/telemetry?since={_NOW - _DAY}&until={_NOW}")
    assert ok.status_code == 200
    assert ok.json()["object"] == "usage_telemetry"
    assert client.get(f"/v1/usage/telemetry?since={_NOW}&until={_NOW - 1}").status_code == 422
    assert client.get(f"/v1/usage/telemetry?since=0&until={_NOW}").status_code == 422
    assert client.get("/v1/usage/telemetry?max_sessions=0").status_code == 422
