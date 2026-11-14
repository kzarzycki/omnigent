"""Scale and read-count tests for ``GET /v1/usage``'s report builder."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from omnigent.runtime import _globals
from omnigent.server.routes import usage
from omnigent.server.routes.usage import _build_usage_report
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

_DAY = 86_400
_NOW = 1_784_678_400  # 2026-07-22T00:00:00Z
_SUB_AGENTS_PER_SESSION = 2
# Per-statement delay standing in for a database reached over a network.
_QUERY_LATENCY_SECONDS = 0.001


class _CountingAgentCache:
    """Stands in for the runtime's agent cache; every load returns a small spec."""

    def __init__(self) -> None:
        self.loads = 0
        self.harness_by_agent: dict[str, str] = {}

    def load(self, agent_id: str, bundle_location: str, *, expand_env: bool = False):
        self.loads += 1
        executor = SimpleNamespace(
            type="claude-sdk",
            config={"harness": self.harness_by_agent.get(agent_id, "claude-sdk")},
            model="model-a",
            reasoning_effort=None,
        )
        spec = SimpleNamespace(
            name="agent",
            executor=executor,
            sub_agents=[],
            tools=[],
            llm=SimpleNamespace(model="model-a"),
        )
        return SimpleNamespace(spec=spec)


def _seed(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sessions: int,
    spread_days: int,
) -> tuple[SqlAlchemyConversationStore, list[str]]:
    """Create *sessions* top-level sessions, each with sub-agents and its own agent."""
    store = SqlAlchemyConversationStore(db_uri)
    agent_store = SqlAlchemyAgentStore(db_uri)
    cache = _CountingAgentCache()
    monkeypatch.setattr(_globals, "_conversation_store", store)
    monkeypatch.setattr(_globals, "_agent_store", agent_store)
    monkeypatch.setattr(_globals, "_agent_cache", cache)
    monkeypatch.setattr("omnigent.db.utils.now_epoch", lambda: _NOW)
    clock = {"ts": _NOW}
    monkeypatch.setattr(
        "omnigent.stores.conversation_store.sqlalchemy_store.now_epoch", lambda: clock["ts"]
    )
    root_ids: list[str] = []
    for index in range(sessions):
        clock["ts"] = _NOW - (index * spread_days * _DAY) // sessions
        agent_id = uuid.uuid4().hex
        agent_store.create(agent_id, f"agent-{index}", f"{agent_id}/bundle")
        root = store.create_conversation(title=f"s{index}", agent_id=agent_id)
        store.set_session_usage(
            root.id, {"total_cost_usd": 1.0, "by_model": {"model-a": {"total_cost_usd": 1.0}}}
        )
        for sub_index in range(_SUB_AGENTS_PER_SESSION):
            child = store.create_conversation(
                kind="sub_agent",
                title=f"s{index}-c{sub_index}",
                parent_conversation_id=root.id,
                agent_id=agent_id,
                sub_agent_name=f"worker{sub_index}",
            )
            store.set_session_usage(child.id, {"total_cost_usd": 0.5})
        root_ids.append(root.id)
    return store, root_ids


@contextmanager
def _query_latency(per_query_seconds: float) -> Iterator[dict[str, int]]:
    """Count SQL statements, delaying each to model a database a network away."""
    counter = {"queries": 0}

    def _before_execute(*_args: object, **_kwargs: object) -> None:
        counter["queries"] += 1
        if per_query_seconds:
            time.sleep(per_query_seconds)

    event.listen(Engine, "before_cursor_execute", _before_execute)
    try:
        yield counter
    finally:
        event.remove(Engine, "before_cursor_execute", _before_execute)


@pytest.mark.parametrize(("sessions", "days", "max_queries"), [(250, 7, 100), (1000, None, 250)])
def test_usage_report_with_page_details_reads_a_bounded_number_of_rows(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    sessions: int,
    days: int | None,
    max_queries: int,
) -> None:
    store, _ = _seed(db_uri, monkeypatch, sessions=sessions, spread_days=7)
    with _query_latency(0) as counter:
        report = _build_usage_report(store, None, include_page_details=True, days=days)

    assert len(report.sessions) >= sessions * 6 // 7 - 1
    assert all(
        item.harness == "claude-sdk" and item.llm_model == "model-a" for item in report.sessions
    )
    assert counter["queries"] < max_queries


@pytest.mark.skipif(
    not os.environ.get("OMNIGENT_USAGE_TIMING"),
    reason="timing benchmark; set OMNIGENT_USAGE_TIMING=1 (wall-clock, never a gate)",
)
@pytest.mark.parametrize(
    ("sessions", "days", "budget_seconds"), [(250, 7, 2.0), (1000, None, 5.0)]
)
def test_usage_report_timing_benchmark(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    sessions: int,
    days: int | None,
    budget_seconds: float,
) -> None:
    store, _ = _seed(db_uri, monkeypatch, sessions=sessions, spread_days=7)
    with _query_latency(_QUERY_LATENCY_SECONDS) as counter:
        started = time.perf_counter()
        _build_usage_report(store, None, include_page_details=True, days=days)
        elapsed = time.perf_counter() - started
    print(f"\nUSAGE_TIMING sessions={sessions} days={days} {elapsed:.2f}s {counter['queries']}q")
    assert elapsed < budget_seconds


def test_verified_fallback_tree_resolves_sub_agent_harness_from_the_store(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root_ids = _seed(db_uri, monkeypatch, sessions=1, spread_days=1)
    other_agent_id = uuid.uuid4().hex
    _globals._agent_cache.harness_by_agent[other_agent_id] = "codex"
    SqlAlchemyAgentStore(db_uri).create(other_agent_id, "other", f"{other_agent_id}/bundle")
    store.create_conversation(
        kind="sub_agent",
        title="late child",
        parent_conversation_id=root_ids[0],
        agent_id=other_agent_id,
        sub_agent_name="late",
    )
    # An empty batched read forces the verified per-session fallback, which
    # finds a sub-agent bound to an agent the page never preloaded.
    monkeypatch.setattr(usage, "_load_trees_by_root", lambda _store, _roots: {})

    report = _build_usage_report(store, None, include_page_details=True)

    assert report.sessions[0].harness == "claude-sdk"
    assert report.sessions[0].other_harnesses == ["codex"]


def test_usage_report_query_count_does_not_grow_with_session_count(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _seed(db_uri, monkeypatch, sessions=300, spread_days=7)
    counts = []
    for days in (1, 7):
        with _query_latency(0) as counter:
            _build_usage_report(store, None, include_page_details=True, days=days)
        counts.append(counter["queries"])

    # A fixed handful of reads per page, not a handful per session.
    assert counts[1] < 100, counts


def test_one_day_window_reads_no_tree_outside_the_window(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _seed(db_uri, monkeypatch, sessions=200, spread_days=7)
    window_start = _NOW - _DAY
    tree_roots_read: set[str] = set()
    real_list = store.list_conversations

    def _spy(*args: object, **kwargs: object):
        tree_roots_read.update(kwargs.get("root_conversation_ids") or [])
        return real_list(*args, **kwargs)

    monkeypatch.setattr(store, "list_conversations", _spy)
    report = _build_usage_report(store, None, days=1)

    in_window = {item.id for item in report.sessions}
    assert in_window
    assert tree_roots_read == in_window
    assert all(item.updated_at >= window_start for item in report.sessions)
