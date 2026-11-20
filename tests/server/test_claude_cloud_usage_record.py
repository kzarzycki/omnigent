"""A recorded claude-cloud turn fills the session record and the usage counters."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from omnigent.llms.context_window import ModelPricing
from omnigent.server.routes._sessions.orchestration import _context_labels_from_turn_usage
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.inner.claude_cloud_stream import STREAM
from tests.server.helpers import create_test_agent


async def test_claude_cloud_turn_fills_the_record_and_counters(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorded cloud stream, through the bridge and the relay's accumulation, fills the record.

    Two turns of the stream persist their growth on the session under the model that ran,
    and the usage counters grow by the same amounts with the cloud harness's labels.
    """
    import importlib.util
    from pathlib import Path

    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from omnigent.inner.acp_executor import AcpExecutor
    from omnigent.runtime import usage_metrics
    from omnigent.server.routes import sessions as sessions_routes

    root = Path(__file__).parents[2]
    spec = importlib.util.spec_from_file_location(
        "claude_cloud_acp_record", root / "integrations/claude-cloud-acp/claude_cloud_acp.py"
    )
    assert spec is not None and spec.loader is not None
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    stream = STREAM
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: ModelPricing(input_per_token=1e-6, output_per_token=2e-6),
    )
    reader = InMemoryMetricReader()
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
    monkeypatch.setattr(
        usage_metrics.otel_metrics, "get_meter", MeterProvider(metric_readers=[reader]).get_meter
    )
    usage_metrics._instruments.cache_clear()
    agent = await create_test_agent(client)
    created = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert created.status_code == 201, created.text
    session = created.json()
    store = SqlAlchemyConversationStore(db_uri)
    tracker = bridge.UsageTracker()
    try:
        for payloads in (stream[:3], stream[3:]):
            for payload in payloads:
                tracker.note(payload)
            usage = AcpExecutor._usage_from_result({"usage": tracker.take()})
            sessions_routes._accumulate_session_usage({"usage": usage}, session["id"], store)
            # The relay persists the window-fill labels beside the accumulation.
            store.set_labels(session["id"], _context_labels_from_turn_usage(usage))
        data = reader.get_metrics_data()
    finally:
        usage_metrics._instruments.cache_clear()
    conv = store.get_conversation(session["id"])
    recorded: dict[str, Any] = dict(conv.session_usage) if conv and conv.session_usage else {}
    # The cloud's cumulative totals (40 in, 150 out, 0.75 USD) arrive once each, not twice.
    bucket = recorded["by_model"]["claude-opus-4-8[1m]"]
    assert (bucket["input_tokens"], bucket["output_tokens"]) == (40, 150)
    assert bucket["total_cost_usd"] == pytest.approx(0.75)
    snapshot = (await client.get(f"/v1/sessions/{session['id']}")).json()
    assert snapshot["last_total_tokens"] == 4510  # the last assistant message's window fill
    assert snapshot["context_window"] == 1_000_000
    assert snapshot["usage_by_model"]["claude-opus-4-8[1m]"]["output_tokens"] == 150
    assert data is not None
    # The relay's own metric emission, not a second call here, produced these series.
    points = {
        (metric.name, dict(p.attributes or {}).get("token.type")): (dict(p.attributes), p.value)
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        for p in metric.data.data_points
    }
    tokens = usage_metrics.TOKENS_METRIC_NAME
    assert points[(tokens, "input")][1] == 40
    assert points[(tokens, "output")][1] == 150
    assert points[(tokens, "cache_read")][1] == 9500
    assert points[(tokens, "cache_creation")][1] == 800
    assert points[(usage_metrics.COST_METRIC_NAME, None)][1] == pytest.approx(0.75)
    assert points[(tokens, "input")][0]["model"] == "claude-opus-4-8[1m]"
