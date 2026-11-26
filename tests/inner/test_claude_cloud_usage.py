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
from tests.inner.claude_cloud_stream import STREAM

_BRIDGE_PATH = Path(__file__).parents[2] / "integrations/claude-cloud-acp/claude_cloud_acp.py"

# A cloud turn as the stream delivers it (sanitized): the result's usage is cumulative and
# its modelUsage names a helper model beside the session's own.
_STREAM = STREAM
_MODEL = "claude-opus-4-8[1m]"
_TURN_STREAM = _STREAM[:3]


@pytest.fixture
def bridge(tmp_path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "claude_cloud_acp_usage_under_test", _BRIDGE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STATE_FILE = tmp_path / "state.json"
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


def test_a_helper_model_does_not_take_over_the_session_model_or_window(bridge: ModuleType) -> None:
    """The init model and its window stay, though a helper wrote more of the output."""
    tracker = bridge.UsageTracker()
    for payload in _TURN_STREAM:
        tracker.note(payload)
    turn = tracker.take()
    assert turn is not None and turn["model"] == _MODEL
    assert tracker.context_window == 1_000_000


def test_the_baseline_survives_a_bridge_respawn(
    bridge: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A respawned bridge reports a turn's growth, not the cloud session's whole total again."""
    first = bridge.Session("/", "acp-usage")
    first.cloud_id = "session_u"
    first.usage.load("session_u")
    for payload in _STREAM[:3]:
        first.usage.note(payload)
    first.usage.take()
    bridge.remember("usage:session_u", json.dumps(first.usage.seen))
    respawned = bridge.Session("/", "acp-usage")
    respawned.usage.load("session_u")
    respawned.usage.note(_STREAM[3])
    turn = respawned.usage.take()
    assert turn is not None
    assert (turn["inputTokens"], turn["outputTokens"], turn["costUsd"]) == (10, 30, 0.25)


def test_a_cloud_session_adopted_mid_turn_loads_its_saved_baseline(
    bridge: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh ACP session that learns its cloud id from the replay still reports only growth."""
    bridge.remember("usage:session_u", json.dumps({"input_tokens": 30, "output_tokens": 120}))
    adopted = bridge.Session("/", "acp-fresh")
    adopted.reader = threading.current_thread()
    adopted.cloud_id = "session_u"  # set by the replay, after the turn started
    adopted.events.put({"sequence_num": "9", "payload": _STREAM[3]})
    monkeypatch.setattr(bridge, "update", lambda *_: None)
    assert bridge._mirror("acp-fresh", adopted, 0) == "end_turn"
    turn = adopted.usage.take()
    assert turn is not None
    assert (turn["inputTokens"], turn["outputTokens"]) == (10, 30)


def _failing_turn(bridge: ModuleType, monkeypatch: pytest.MonkeyPatch, stop: str | None) -> dict:
    """Run a prompt whose stream gave one assistant message and no result; return the reply.

    *stop* is the stop reason the turn ends with (a cancel), or ``None`` for a turn that raises.
    """
    out = io.StringIO()
    session = bridge.Session("/", "acp-usage")
    session.cloud_id = "session_u"
    session.usage.loaded = True
    bridge.SESSIONS["acp-usage"] = session

    def mirror(_acp_id: str, s: object, _text: str) -> str:
        for payload in _TURN_STREAM[:2]:  # init and the assistant message, never the result
            session.usage.note(payload)
        if stop is None:
            raise RuntimeError("the sandbox went away")
        return stop

    monkeypatch.setattr(bridge, "_mirror_turn", mirror)
    monkeypatch.setattr(bridge, "_send", lambda *_: (0, False))
    monkeypatch.setattr(bridge, "remember", lambda *_: None)
    monkeypatch.setattr(bridge, "touch", lambda: None)
    monkeypatch.setattr(bridge, "prompt_done", lambda: None)
    monkeypatch.setattr(bridge, "keepalive", lambda *_: None)
    monkeypatch.setattr(bridge.sys, "stdout", out)
    bridge.handle_prompt(
        {"id": 7, "params": {"sessionId": "acp-usage", "prompt": [{"type": "text", "text": "go"}]}}
    )
    return next(
        line for line in map(json.loads, out.getvalue().splitlines()) if line.get("id") == 7
    )


_SPENT = {
    "input_tokens": 10,
    "output_tokens": 20,
    "total_tokens": 30,
    "cache_read_input_tokens": 4000,
    "cache_creation_input_tokens": 500,
    "context_tokens": 4510,
    "model": _MODEL,
}


def test_a_failed_turn_reports_the_tokens_it_spent(
    bridge: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn that raises before its result still carries its tokens on the error."""
    reply = _failing_turn(bridge, monkeypatch, None)
    assert reply["error"]["message"].startswith("the sandbox went away")
    assert AcpExecutor._usage_from_result(reply["error"]["data"]) == _SPENT


def test_a_cancelled_turn_reports_the_tokens_it_spent(
    bridge: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel that ends with no result still reports the turn's tokens."""
    reply = _failing_turn(bridge, monkeypatch, "cancelled")
    assert reply["result"]["stopReason"] == "cancelled"
    assert AcpExecutor._usage_from_result(reply["result"]) == _SPENT


def test_partial_then_final_counts_each_token_once(bridge: ModuleType) -> None:
    """A failed turn's tokens are not counted again when a later result's totals include them."""
    tracker = bridge.UsageTracker()
    tracker.begin_turn()
    for payload in _TURN_STREAM[:2]:
        tracker.note(payload)
    partial = tracker.take_partial()
    assert partial is not None and (partial["inputTokens"], partial["outputTokens"]) == (10, 20)
    assert tracker.take_partial() is None  # reported once
    tracker.begin_turn()
    tracker.note(_TURN_STREAM[2])  # cumulative: 30 in, 120 out
    final = tracker.take()
    assert final is not None
    assert partial["inputTokens"] + final["inputTokens"] == 30
    assert partial["outputTokens"] + final["outputTokens"] == 120


def test_a_result_replaces_the_running_tokens(bridge: ModuleType) -> None:
    """A turn that ends with its result reports that only, never also its partial tokens."""
    tracker = bridge.UsageTracker()
    tracker.begin_turn()
    for payload in _TURN_STREAM:
        tracker.note(payload)
    assert tracker.take() is not None
    assert tracker.take_partial() is None


_FAILING_AGENT = r"""
import sys, json

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

usage = {"inputTokens": 10, "outputTokens": 20, "totalTokens": 30, "model": "claude-opus-4-8[1m]"}
for line in sys.stdin:
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "s1"}})
    elif method == "session/prompt":
        error = {"code": -32000, "message": "boom", "data": {"usage": usage}}
        send({"jsonrpc": "2.0", "id": mid, "error": error})
"""


@pytest.mark.asyncio
async def test_a_failed_turn_s_usage_reaches_the_session_record(tmp_path: Path) -> None:
    """An agent's error carrying usage becomes an ExecutorError that the relay books."""
    import shlex
    import sys
    from unittest.mock import MagicMock, patch

    from omnigent.inner.acp_executor import AcpAgentConfig
    from omnigent.inner.executor import ExecutorError
    from omnigent.server.routes._sessions.orchestration import _accumulate_session_usage

    agent = tmp_path / "failing_agent.py"
    agent.write_text(_FAILING_AGENT)
    executor = AcpExecutor(
        AcpAgentConfig(command=shlex.join([sys.executable, str(agent)]), name="claude-cloud")
    )
    try:
        events = [
            e async for e in executor.run_turn([{"role": "user", "content": "go"}], [], "", None)
        ]
    finally:
        await executor.close()
    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert len(errors) == 1 and errors[0].usage is not None
    assert errors[0].usage["output_tokens"] == 20
    store = MagicMock()
    store.get_conversation.return_value = None
    with patch("omnigent.llms.context_window.fetch_model_pricing", return_value=None):
        _accumulate_session_usage({"usage": errors[0].usage}, "conv_x", store)
    delta = store.increment_session_usage.call_args.args[1]
    assert delta["by_model"][_MODEL]["output_tokens"] == 20
