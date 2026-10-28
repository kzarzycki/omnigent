"""Per-model cost/token attribution for the native cumulative usage path.

``_persist_native_cumulative_usage`` receives *cumulative* session totals from
native harnesses (claude-native / codex-native). It must attribute each
report's growth to the currently-active model so per-model buckets hold each
model's own spend and sum to the flat session total across mid-session model
switches — rather than SETting every bucket to the whole running total (which
double-counted the shared baseline once the active model changed).
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from omnigent.llms.context_window import ModelPricing
from omnigent.runtime import _globals, usage_metrics
from omnigent.server.routes._sessions.orchestration import (
    _persist_native_cumulative_usage,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

# Agent ids are stored as 16-byte uuids, so tests use a valid 32-char hex id.
_AGENT_ID = "0123456789abcdef0123456789abcdef"


def _usage(store: SqlAlchemyConversationStore, conv_id: str) -> dict:
    conv = store.get_conversation(conv_id)
    assert conv is not None
    return dict(conv.session_usage)


def test_per_model_cost_sums_across_model_switch(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="switch", agent_id=_AGENT_ID)

    # Cumulative session cost climbs on opus, then keeps climbing after a
    # /model switch to sonnet (native /cost is a running session total).
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 3.0, "model": "claude-opus-4-8"}, store
    )
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 10.80, "model": "claude-opus-4-8"}, store
    )
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 11.91, "model": "claude-sonnet-4-5"}, store
    )

    usage = _usage(store, conv.id)
    by_model = usage["by_model"]
    # Each model holds only its own spend (opus's growth 0->10.80, sonnet's
    # 10.80->11.91), and the buckets sum to the flat total — the bug fixed here.
    assert usage["total_cost_usd"] == pytest.approx(11.91)
    assert by_model["claude-opus-4-8"]["total_cost_usd"] == pytest.approx(10.80)
    assert by_model["claude-sonnet-4-5"]["total_cost_usd"] == pytest.approx(1.11)
    assert sum(m["total_cost_usd"] for m in by_model.values()) == pytest.approx(11.91)


def test_per_model_cost_accumulates_when_switching_back(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="switch-back", agent_id=_AGENT_ID)

    # A -> B -> A: model A's bucket must accumulate across its two separate
    # active spans (not be overwritten on the second visit), so the returning
    # model's growth adds onto its earlier spend.
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 10.0, "model": "model-a"}, store
    )
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 12.50, "model": "model-b"}, store
    )
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 15.0, "model": "model-a"}, store
    )

    usage = _usage(store, conv.id)
    by_model = usage["by_model"]
    # A = 10.0 (first span) + 2.50 (15.0 - 12.50 on return); B = 12.50 - 10.0.
    assert usage["total_cost_usd"] == pytest.approx(15.0)
    assert by_model["model-a"]["total_cost_usd"] == pytest.approx(12.50)
    assert by_model["model-b"]["total_cost_usd"] == pytest.approx(2.50)
    assert sum(m["total_cost_usd"] for m in by_model.values()) == pytest.approx(15.0)


def test_single_model_bucket_equals_flat_total(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="single", agent_id=_AGENT_ID)

    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 2.0, "model": "claude-opus-4-8"}, store
    )
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 5.5, "model": "claude-opus-4-8"}, store
    )

    usage = _usage(store, conv.id)
    assert usage["total_cost_usd"] == pytest.approx(5.5)
    assert usage["by_model"]["claude-opus-4-8"]["total_cost_usd"] == pytest.approx(5.5)


def test_lowered_report_does_not_claw_back_bucket(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="rebased", agent_id=_AGENT_ID)

    _persist_native_cumulative_usage(conv.id, {"cumulative_cost_usd": 10.0, "model": "m1"}, store)
    # A forged / rebased lower report: the flat total is monotonic-clamped, and
    # the per-model delta is clamped >= 0, so neither is clawed back.
    _persist_native_cumulative_usage(conv.id, {"cumulative_cost_usd": 2.0, "model": "m1"}, store)

    usage = _usage(store, conv.id)
    assert usage["total_cost_usd"] == pytest.approx(10.0)
    assert usage["by_model"]["m1"]["total_cost_usd"] == pytest.approx(10.0)


def test_per_model_tokens_accumulate_from_cumulative(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="tokens", agent_id=_AGENT_ID)

    # codex-native style: cumulative token totals plus an explicit cost.
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100,
            "cumulative_output_tokens": 20,
            "cumulative_cost_usd": 1.0,
            "model": "gpt-5.6",
        },
        store,
    )
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 300,
            "cumulative_output_tokens": 50,
            "cumulative_cost_usd": 3.0,
            "model": "gpt-5.6",
        },
        store,
    )

    usage = _usage(store, conv.id)
    bucket = usage["by_model"]["gpt-5.6"]
    # The bucket accumulates deltas to match the flat cumulative session totals.
    assert bucket["input_tokens"] == usage["input_tokens"] == 300
    assert bucket["output_tokens"] == usage["output_tokens"] == 50
    assert bucket["total_tokens"] == usage["total_tokens"] == 350
    assert bucket["total_cost_usd"] == pytest.approx(3.0)


def test_per_model_tokens_sum_across_model_switch(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="switch-tokens", agent_id=_AGENT_ID)

    # codex-style: cumulative tokens (with explicit cost to avoid the catalog)
    # climb on opus, then keep climbing after a /model switch to sonnet.
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100,
            "cumulative_output_tokens": 20,
            "cumulative_cost_usd": 3.0,
            "model": "claude-opus-4-8",
        },
        store,
    )
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 300,
            "cumulative_output_tokens": 60,
            "cumulative_cost_usd": 11.91,
            "model": "claude-sonnet-4-5",
        },
        store,
    )

    usage = _usage(store, conv.id)
    opus = usage["by_model"]["claude-opus-4-8"]
    sonnet = usage["by_model"]["claude-sonnet-4-5"]
    # Each model holds only its own token growth, and the per-model token +
    # cost buckets sum to the flat session totals across the switch.
    assert opus["input_tokens"] == 100
    assert sonnet["input_tokens"] == 200
    assert opus["input_tokens"] + sonnet["input_tokens"] == usage["input_tokens"] == 300
    assert opus["output_tokens"] + sonnet["output_tokens"] == usage["output_tokens"] == 60
    assert opus["total_tokens"] + sonnet["total_tokens"] == usage["total_tokens"] == 360
    assert opus["total_cost_usd"] + sonnet["total_cost_usd"] == pytest.approx(11.91)


def test_token_priced_bucket_matches_flat_cost(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="codex-priced", agent_id=_AGENT_ID)

    # codex-native reports cumulative tokens but NO explicit cost, so cost is
    # computed from the running token totals. Stub pricing so the test does not
    # depend on the live catalog; assert the per-model cost bucket still tracks
    # the flat token-priced total (the elif-has_tokens branch feeds by_model).
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: {"stub": True},
    )
    monkeypatch.setattr(
        "omnigent.llms.context_window.compute_llm_cost",
        lambda usage, pricing: float(usage.get("total_tokens", 0)) * 0.01,
    )

    _persist_native_cumulative_usage(
        conv.id,
        {"cumulative_input_tokens": 200, "cumulative_output_tokens": 50, "model": "gpt-5.6"},
        store,
    )

    usage = _usage(store, conv.id)
    # total_tokens = 200 (input) + 0 (cache) + 50 (output) = 250; cost = 2.50.
    assert usage["total_cost_usd"] == pytest.approx(2.50)
    assert usage["by_model"]["gpt-5.6"]["total_cost_usd"] == pytest.approx(2.50)


def test_token_report_without_cache_field_keeps_persisted_cache_split(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="codex-cache-miss", agent_id=_AGENT_ID)

    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: ModelPricing(
            input_per_token=2.5e-6,
            output_per_token=10e-6,
            cache_read_per_token=0.25e-6,
            cache_write_per_token=None,
        ),
    )

    # Cache-heavy turn: 100K cumulative input, 90K of them cache reads.
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100_000,
            "cumulative_cache_read_input_tokens": 90_000,
            "cumulative_output_tokens": 200,
            "model": "gpt-5.6",
        },
        store,
    )
    # Cache-miss turn: the cumulative cached count is unchanged, and the
    # report omits it. The persisted split must carry forward rather than
    # re-billing the 90K cache reads at the full input rate.
    _persist_native_cumulative_usage(
        conv.id,
        {"cumulative_input_tokens": 200_000, "cumulative_output_tokens": 400, "model": "gpt-5.6"},
        store,
    )

    usage = _usage(store, conv.id)
    expected_cost = (110_000 * 2.5 + 90_000 * 0.25 + 400 * 10) / 1_000_000
    assert usage["cache_read_input_tokens"] == 90_000
    assert usage["input_tokens"] == 110_000
    assert usage["total_tokens"] == 200_400
    assert usage["total_cost_usd"] == pytest.approx(expected_cost)
    bucket = usage["by_model"]["gpt-5.6"]
    assert bucket["input_tokens"] == 110_000
    assert bucket["cache_read_input_tokens"] == 90_000
    assert bucket["total_cost_usd"] == pytest.approx(expected_cost)


def test_explicit_zero_cache_count_replaces_persisted_split(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="codex-cache-zero", agent_id=_AGENT_ID)

    # Only token splits are asserted here; keep the token-pricing branch off
    # the live catalog so the test is deterministic and offline.
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: None,
    )

    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100_000,
            "cumulative_cache_read_input_tokens": 90_000,
            "cumulative_output_tokens": 200,
            "model": "gpt-5.6",
        },
        store,
    )
    # An explicit zero is a report, not an omission: it is honored as-is.
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 120_000,
            "cumulative_cache_read_input_tokens": 0,
            "cumulative_output_tokens": 300,
            "model": "gpt-5.6",
        },
        store,
    )

    usage = _usage(store, conv.id)
    assert usage["cache_read_input_tokens"] == 0
    assert usage["input_tokens"] == 120_000


def test_carried_cache_count_is_clamped_to_a_lowered_input_total(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="codex-cache-lowered", agent_id=_AGENT_ID)

    # Only token splits are asserted here; keep the token-pricing branch off
    # the live catalog so the test is deterministic and offline.
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: None,
    )

    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100_000,
            "cumulative_cache_read_input_tokens": 90_000,
            "cumulative_output_tokens": 200,
            "model": "gpt-5.6",
        },
        store,
    )
    # A lowered cumulative total without a cache count keeps the carried split
    # within the reported input, and later reports carry that clamped count.
    _persist_native_cumulative_usage(
        conv.id,
        {"cumulative_input_tokens": 50_000, "cumulative_output_tokens": 250, "model": "gpt-5.6"},
        store,
    )
    assert _usage(store, conv.id)["cache_read_input_tokens"] == 50_000
    assert _usage(store, conv.id)["input_tokens"] == 0

    _persist_native_cumulative_usage(
        conv.id,
        {"cumulative_input_tokens": 200_000, "cumulative_output_tokens": 400, "model": "gpt-5.6"},
        store,
    )
    usage = _usage(store, conv.id)
    assert usage["cache_read_input_tokens"] == 50_000
    assert usage["input_tokens"] == 150_000


def _conversation_point_reads(statements: list[str]) -> list[str]:
    """Filter captured SQL to point reads of a single conversation row."""
    matches: list[str] = []
    for raw in statements:
        normalized = " ".join(raw.split())
        if "FROM conversations" not in normalized or " WHERE " not in normalized:
            continue
        where = normalized.split(" WHERE ", 1)[1]
        if "conversations.id = " in where and "root_conversation_id" not in where:
            matches.append(normalized)
    return matches


@pytest.mark.asyncio
async def test_flush_with_stale_row_keeps_monotonic_clamp(db_uri: str) -> None:
    """A stale caller-held row must not weaken the forged-low-report clamp.

    The events route reads the conversation at request start (authorization)
    and hands that row to the usage flush for the read-only tree walks. If a
    concurrent report persists a higher cost AFTER that read, a forged low
    report on the stale-row request must still be a no-op: the own-usage
    persist baselines its monotonic clamp on its own FRESH read, never on the
    caller's row (otherwise the replay could roll the persisted/enforced cost
    back and double-count the daily rollup delta).
    """
    from omnigent.server.routes._sessions.orchestration import (
        _persist_external_session_usage,
    )
    from omnigent.server.schemas import SessionEventInput

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="stale-clamp", agent_id=_AGENT_ID)
    # The route's access-check row, read while the persisted cost was $0.
    stale_row = store.get_conversation(conv.id)
    assert stale_row is not None

    # A concurrent legitimate report lands after the access check.
    _persist_native_cumulative_usage(conv.id, {"cumulative_cost_usd": 10.0, "model": "m1"}, store)

    # The stale-row request then delivers a (forged or delayed) lower report.
    await _persist_external_session_usage(
        conv.id,
        SessionEventInput(
            type="external_session_usage",
            data={"cumulative_cost_usd": 2.0, "model": "m1"},
        ),
        store,
        stale_row,
    )

    assert _usage(store, conv.id)["total_cost_usd"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_flush_with_supplied_row_skips_tree_root_rereads(db_uri: str) -> None:
    """The flush's read-only tree walks must reuse the supplied row's root.

    With the route's row in hand, the only conversation point read a flush
    may issue is the own-usage persist's fresh clamp baseline — the subtree
    roll-up and the ancestor publish take the root from the supplied row.
    """
    from sqlalchemy import event as sa_event

    from omnigent.db.utils import _engine_cache
    from omnigent.server.routes._sessions.orchestration import (
        _persist_external_session_usage,
    )
    from omnigent.server.schemas import SessionEventInput

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="flush-reads", agent_id=_AGENT_ID)
    row = store.get_conversation(conv.id)
    assert row is not None

    engine = _engine_cache[db_uri]
    statements: list[str] = []

    def _on_exec(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa_event.listen(engine, "before_cursor_execute", _on_exec)
    try:
        await _persist_external_session_usage(
            conv.id,
            SessionEventInput(
                type="external_session_usage",
                data={"cumulative_cost_usd": 3.0, "model": "m1"},
            ),
            store,
            row,
        )
    finally:
        sa_event.remove(engine, "before_cursor_execute", _on_exec)

    point_reads = _conversation_point_reads(statements)
    assert len(point_reads) == 1, (
        f"flush issued {len(point_reads)} conversation point reads (expected "
        f"1: the persist's fresh clamp baseline); statements={point_reads}"
    )
    assert _usage(store, conv.id)["total_cost_usd"] == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_flush_telemetry_reports_the_callers_installation_id(db_uri: str) -> None:
    """The usage event names the posting machine from its request header only.

    The header is what makes the attribution free: the server never looks the
    host up, so a flush from a client that sends no header leaves the field
    unset rather than costing a read.
    """
    from unittest.mock import patch

    from omnigent.server.routes._sessions.orchestration import (
        _persist_external_session_usage,
    )
    from omnigent.server.schemas import SessionEventInput

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="flush-telemetry", agent_id=_AGENT_ID)
    row = store.get_conversation(conv.id)
    assert row is not None

    events: list = []
    with patch(
        "omnigent.server.routes._sessions.orchestration._tel_emit",
        side_effect=events.append,
    ):
        await _persist_external_session_usage(
            conv.id,
            SessionEventInput(
                type="external_session_usage",
                data={"cumulative_cost_usd": 3.0, "model": "m1"},
            ),
            store,
            row,
            "alice@example.com",
            "inst-host-9",
        )
        await _persist_external_session_usage(
            conv.id,
            SessionEventInput(
                type="external_session_usage",
                data={"cumulative_cost_usd": 4.0, "model": "m1"},
            ),
            store,
            row,
            "alice@example.com",
        )

    assert [e.host_installation_id for e in events] == ["inst-host-9", None]


def test_claude_native_tokens_split_both_cache_buckets_and_keep_the_reported_cost(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.runtime import usage_metrics

    growths: list[tuple[dict, str | None]] = []
    monkeypatch.setattr(
        usage_metrics,
        "record_usage_growth",
        lambda growth, *, model, **_names: growths.append((dict(growth), model)),
    )
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="claude", agent_id=_AGENT_ID)

    # claude-native's input total includes cache reads AND cache writes.
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_cost_usd": 0.5,
            "model": "claude-opus-4-8",
            "cumulative_input_tokens": 1210,
            "cumulative_cache_read_input_tokens": 1000,
            "cumulative_cache_creation_input_tokens": 200,
            "cumulative_output_tokens": 40,
        },
        store,
    )

    usage = _usage(store, conv.id)
    assert usage["input_tokens"] == 10
    assert usage["cache_read_input_tokens"] == 1000
    assert usage["cache_creation_input_tokens"] == 200
    assert usage["output_tokens"] == 40
    assert usage["total_tokens"] == 1250
    # The reported cost wins; the tokens are not priced into another figure.
    assert usage["total_cost_usd"] == pytest.approx(0.5)
    bucket = usage["by_model"]["claude-opus-4-8"]
    assert bucket["cache_creation_input_tokens"] == 200
    assert bucket["input_tokens"] == 10
    growth, model = growths[-1]
    assert model == "claude-opus-4-8"
    assert growth["cache_creation_input_tokens"] == 200
    assert growth["total_cost_usd"] == pytest.approx(0.5)


def test_rate_limit_windows_merge_sparse_updates_per_limit(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.errors import OmnigentError
    from omnigent.runtime import usage_metrics

    gauges: list[dict] = []
    monkeypatch.setattr(usage_metrics, "record_rate_limits", lambda rl: gauges.append(dict(rl)))
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="codex", agent_id=_AGENT_ID)
    weekly = {"used_percent": 80.0, "window_minutes": 10080, "resets_at": 1792874497}

    _persist_native_cumulative_usage(
        conv.id, {"rate_limits": {"limit_id": "codex", "primary": weekly}}, store
    )
    # A sparse update carrying only the other window keeps the first one.
    _persist_native_cumulative_usage(
        conv.id,
        {"rate_limits": {"limit_id": "codex", "secondary": {"used_percent": 5}}},
        store,
    )

    usage = _usage(store, conv.id)
    limit = usage["rate_limits"]["codex"]
    assert limit["primary"] == weekly
    assert limit["secondary"] == {"used_percent": 5.0}
    assert isinstance(limit["captured_at"], int)
    # A rate-limit-only report touches no token or cost field.
    assert "total_cost_usd" not in usage
    assert "input_tokens" not in usage
    assert gauges[0] == {"limit_id": "codex", "primary": weekly}

    for bad in (
        {"limit_id": "codex"},
        {"limit_id": "", "primary": weekly},
        {"limit_id": "codex", "primary": {"used_percent": float("nan")}},
        {"limit_id": "codex", "primary": {"used_percent": 5, "resets_at": -1}},
        {"limit_id": "codex", "primary": {"used_percent": 10**400}},
        {"limit_id": "codex", "primary": {"used_percent": 5, "resets_at": 10**30}},
    ):
        with pytest.raises(OmnigentError):
            _persist_native_cumulative_usage(conv.id, {"rate_limits": bad}, store)
    with pytest.raises(OmnigentError):
        _persist_native_cumulative_usage(conv.id, {"cumulative_input_tokens": 10**400}, store)

    # The stored ids are capped, so a client can't grow the map or the metric series.
    for n in range(20):
        _persist_native_cumulative_usage(
            conv.id, {"rate_limits": {"limit_id": f"id-{n}", "primary": weekly}}, store
        )
    assert len(_usage(store, conv.id)["rate_limits"]) == 8
    assert len(gauges) == 2 + 7  # the two codex reports above, then id-0..id-6


class _Named:
    def __init__(self, name: str) -> None:
        self.name = name


def _usage_series(
    monkeypatch: pytest.MonkeyPatch, store: SqlAlchemyConversationStore, conv_id: str
) -> dict[str, list[dict]]:
    reader = InMemoryMetricReader()
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
    monkeypatch.setattr(
        usage_metrics.otel_metrics, "get_meter", MeterProvider(metric_readers=[reader]).get_meter
    )
    usage_metrics._instruments.cache_clear()
    try:
        _persist_native_cumulative_usage(
            conv_id, {"cumulative_cost_usd": 2.0, "model": "claude-opus-4-8"}, store
        )
        data = reader.get_metrics_data()
    finally:
        usage_metrics._instruments.cache_clear()
    if data is None:  # nothing recorded
        return {}
    return {
        metric.name: [dict(p.attributes or {}) for p in metric.data.data_points]
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }


def test_usage_metrics_carry_harness_host_agent_project_role_and_user(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(
        agent_id=_AGENT_ID,
        host_id="0123456789abcdef0123456789abcd01",
        workspace="/work",
        project_id="0123456789abcdef0123456789abcd02",
        harness_override="claude-native",
    )
    store.set_labels(conv.id, {"omni.role": "verifier"})
    monkeypatch.setattr(
        _globals, "_host_store", type("H", (), {"get_host": lambda self, i: _Named("laptop")})()
    )
    monkeypatch.setattr(
        _globals,
        "_agent_store",
        type("A", (), {"get_names": lambda self, ids: {ids[0]: "claude"}})(),
    )
    monkeypatch.setattr(
        _globals, "_project_store", type("P", (), {"get_name": lambda self, i: "infra"})()
    )
    # A sub-agent without a grant of its own is attributed to its root's owner;
    # one with its own grant keeps it.
    child = store.create_conversation(agent_id=_AGENT_ID, parent_conversation_id=conv.id)
    granted = store.create_conversation(agent_id=_AGENT_ID, parent_conversation_id=conv.id)
    owners = {conv.id: "alice", granted.id: "bob"}
    monkeypatch.setattr(
        _globals,
        "_conversation_store",
        type("C", (), {"get_session_owner": lambda self, i: owners.get(i)})(),
    )

    cost = _usage_series(monkeypatch, store, conv.id)[usage_metrics.COST_METRIC_NAME]
    child_cost = _usage_series(monkeypatch, store, child.id)[usage_metrics.COST_METRIC_NAME]
    granted_cost = _usage_series(monkeypatch, store, granted.id)[usage_metrics.COST_METRIC_NAME]

    assert cost == [
        {
            "model": "claude-opus-4-8",
            "omnigent.harness": "claude-native",
            "omnigent.host.name": "laptop",
            "omnigent.agent.name": "claude",
            "omnigent.project.name": "infra",
            "omnigent.role": "verifier",
            "omnigent.user": "alice",
        }
    ]
    assert child.root_conversation_id == conv.id
    assert child_cost[0]["omnigent.user"] == "alice"
    assert granted.root_conversation_id == conv.id
    assert granted_cost[0]["omnigent.user"] == "bob"


def test_usage_metrics_for_session_without_host_or_project(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(harness_override="codex-native")
    for name in ("_host_store", "_agent_store", "_project_store", "_conversation_store"):
        monkeypatch.setattr(_globals, name, None)

    cost = _usage_series(monkeypatch, store, conv.id)[usage_metrics.COST_METRIC_NAME]

    assert cost[0]["omnigent.harness"] == "codex-native"
    assert cost[0]["omnigent.host.name"] == "unknown"
    assert cost[0]["omnigent.agent.name"] == "unknown"
    assert cost[0]["omnigent.project.name"] == ""
    assert cost[0]["omnigent.role"] == "other"
    assert cost[0]["omnigent.user"] == "unknown"


def test_usage_metrics_record_no_point_for_a_session_the_harness_telemetry_setting_excludes(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    excluded = store.create_conversation(harness_override="claude-native")
    child = store.create_conversation(parent_conversation_id=excluded.id)
    allowed = store.create_conversation(harness_override="claude-native")
    for name in ("_host_store", "_agent_store", "_project_store", "_conversation_store"):
        monkeypatch.setattr(_globals, name, None)
    config = tmp_path / "server.yaml"
    config.write_text(
        "harness_telemetry:\n"
        "  endpoint: https://otlp.example.test\n"
        f"  exclude:\n    threads: [{excluded.id}]\n"
    )
    monkeypatch.setenv("OMNIGENT_CONFIG", str(config))

    def points(conv_id: str) -> list[dict]:
        return _usage_series(monkeypatch, store, conv_id).get(usage_metrics.COST_METRIC_NAME, [])

    assert points(excluded.id) == []
    assert points(child.id) == []  # a sub-session of an excluded thread
    assert len(points(allowed.id)) == 1


def test_usage_metrics_follow_the_hosts_answer_for_a_repository_excluded_session(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Only the host reads the checkout's origin; its runner's session-init answer decides."""
    import httpx

    from omnigent.server.harness_telemetry import record_host_harness_telemetry

    store = SqlAlchemyConversationStore(db_uri)
    unknown, excluded, allowed = (
        store.create_conversation(harness_override="claude-native") for _ in range(3)
    )
    for name in ("_host_store", "_agent_store", "_project_store", "_conversation_store"):
        monkeypatch.setattr(_globals, name, None)
    config = tmp_path / "server.yaml"
    config.write_text(
        "harness_telemetry:\n"
        "  endpoint: https://otlp.example.test\n"
        "  exclude:\n    repos: [git.example.test/team/private]\n"
    )
    monkeypatch.setenv("OMNIGENT_CONFIG", str(config))
    for conv, enabled in ((excluded, False), (allowed, True)):
        response = httpx.Response(201, json={"harness_telemetry_enabled": enabled})
        record_host_harness_telemetry(conv, response, store)

    def points(conv_id: str) -> list[dict]:
        return _usage_series(monkeypatch, store, conv_id).get(usage_metrics.COST_METRIC_NAME, [])

    assert points(excluded.id) == []
    assert points(unknown.id) == []  # no answer yet: skipped, as the host fails closed
    assert len(points(allowed.id)) == 1
