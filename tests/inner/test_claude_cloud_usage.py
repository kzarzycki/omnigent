"""Usage from a claude.ai cloud session's event stream reaches Omnigent's record and counters."""

from __future__ import annotations

import importlib.util
import io
import json
import threading
from pathlib import Path
from types import ModuleType

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from omnigent.inner.acp_executor import AcpExecutor
from omnigent.llms.context_window import DEFAULT_CONTEXT_WINDOW
from omnigent.runtime import usage_metrics
from omnigent.server.routes._sessions.orchestration import _context_labels_from_turn_usage

_BRIDGE_PATH = Path(__file__).parents[2] / "integrations/claude-cloud-acp/claude_cloud_acp.py"

# One cloud turn as the stream delivers it. The result's usage is cumulative.
_MODEL = "claude-opus-4-8[1m]"
_TURN_STREAM = [
    {"type": "system", "subtype": "init", "model": _MODEL},
    {
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": "done"}],
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 4000,
                "cache_creation_input_tokens": 500,
                "output_tokens": 20,
            },
        },
    },
    {
        "type": "result",
        "total_cost_usd": 0.5,
        "usage": {
            "input_tokens": 30,
            "cache_read_input_tokens": 9000,
            "cache_creation_input_tokens": 800,
            "output_tokens": 120,
        },
        "modelUsage": {_MODEL: {"outputTokens": 120, "contextWindow": 1_000_000}},
    },
]


@pytest.fixture
def bridge() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "claude_cloud_acp_usage_under_test", _BRIDGE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prompt_response(bridge: ModuleType, monkeypatch: pytest.MonkeyPatch, stream: list) -> dict:
    """Run one ``session/prompt`` through the bridge's real turn loop; return its result."""
    out = io.StringIO()
    session = bridge.Session("/", "acp-usage")
    session.cloud_id, session.reader, session.caught_up = (
        "session_u",
        threading.current_thread(),
        True,
    )
    session.marked = True
    bridge.SESSIONS["acp-usage"] = session
    for number, payload in enumerate(stream, start=1):
        session.events.put({"sequence_num": str(number), "payload": payload})
    monkeypatch.setattr(bridge, "_send", lambda *_: (0, False))
    monkeypatch.setattr(bridge, "fetch_events", lambda *_: ([], None))
    monkeypatch.setattr(bridge, "remember", lambda *_: None)
    monkeypatch.setattr(bridge, "touch", lambda: None)
    monkeypatch.setattr(bridge, "prompt_done", lambda: None)
    monkeypatch.setattr(bridge.sys, "stdout", out)
    bridge.handle_prompt(
        {"id": 7, "params": {"sessionId": "acp-usage", "prompt": [{"type": "text", "text": "go"}]}}
    )
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    return next(line["result"] for line in lines if line.get("id") == 7)


def test_cloud_turn_usage_reaches_the_record_and_counters(
    bridge: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The result event's usage becomes the turn's usage, window fill, model and cost."""
    response = _prompt_response(bridge, monkeypatch, _TURN_STREAM)
    assert response["stopReason"] == "end_turn"
    usage = AcpExecutor._usage_from_result(response)
    assert usage == {
        "input_tokens": 30,
        "output_tokens": 120,
        "total_tokens": 150,
        "cache_read_input_tokens": 9000,
        "cache_creation_input_tokens": 800,
        "context_tokens": 4510,
        "cost_usd": 0.5,
        "model": _MODEL,
    }
    # The same dict feeds the session record's context labels ...
    assert _context_labels_from_turn_usage(usage) == {
        "omnigent.last_context_tokens": "4510",
        "omnigent.last_context_window": "1000000",
    }
    # ... and the counters, split by the session's harness and host.
    reader = InMemoryMetricReader()
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
    monkeypatch.setattr(
        usage_metrics.otel_metrics, "get_meter", MeterProvider(metric_readers=[reader]).get_meter
    )
    usage_metrics._instruments.cache_clear()
    try:
        usage_metrics.record_usage_growth(
            {**usage, "total_cost_usd": usage["cost_usd"]},
            model=usage["model"],
            harness="acp:claude-cloud",
            host_name="claude-cloud",
        )
        data = reader.get_metrics_data()
    finally:
        usage_metrics._instruments.cache_clear()
    assert data is not None
    points = {
        metric.name: [dict(p.attributes or {}) for p in metric.data.data_points]
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    for name in (usage_metrics.TOKENS_METRIC_NAME, usage_metrics.COST_METRIC_NAME):
        assert points[name], name
        assert all(
            p["omnigent.harness"] == "acp:claude-cloud" and p["model"] == _MODEL
            for p in points[name]
        )


def test_second_turn_reports_only_its_growth(bridge: ModuleType) -> None:
    """The cloud totals are cumulative, so a turn's share is the growth since the last result."""
    tracker = bridge.UsageTracker()
    for payload in _TURN_STREAM:
        tracker.note(payload)
    tracker.take()
    tracker.note(
        {
            "type": "result",
            "total_cost_usd": 0.75,
            "usage": {"input_tokens": 40, "output_tokens": 150},
        }
    )
    turn = tracker.take()
    assert turn is not None
    assert (turn["inputTokens"], turn["outputTokens"], turn["costUsd"]) == (10, 30, 0.25)


def test_context_window_follows_the_model_not_the_default(bridge: ModuleType) -> None:
    """The window comes from the model the cloud session ran, not the 128000 fallback."""
    tracker = bridge.UsageTracker()
    for payload in _TURN_STREAM:
        tracker.note(payload)
    assert tracker.context_window == 1_000_000
    labels = _context_labels_from_turn_usage({"context_tokens": 1, "model": _MODEL})
    assert labels["omnigent.last_context_window"] == "1000000"
    assert int(labels["omnigent.last_context_window"]) != DEFAULT_CONTEXT_WINDOW


def test_a_result_without_usage_leaves_usage_unknown(
    bridge: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No usage on the stream means no usage on the turn: nothing is made up."""
    response = _prompt_response(
        bridge,
        monkeypatch,
        [{"type": "assistant", "message": {"content": []}}, {"type": "result"}],
    )
    assert "usage" not in response
    assert AcpExecutor._usage_from_result(response) is None
