"""The usage counters and rate-limit gauges reach a real OpenTelemetry reader."""

from __future__ import annotations

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from omnigent.runtime import usage_metrics

_UNSPLIT = {
    "model": "m",
    "omnigent.harness": "unknown",
    "omnigent.host.name": "unknown",
    "omnigent.agent.name": "unknown",
    "omnigent.project.name": "",
    "omnigent.role": "other",
}


def _points(reader: InMemoryMetricReader) -> dict[str, list[tuple[dict, float]]]:
    data = reader.get_metrics_data()
    assert data is not None
    return {
        metric.name: [(dict(p.attributes or {}), p.value) for p in metric.data.data_points]
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }


def test_usage_growth_and_rate_limits_export(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
    monkeypatch.setattr(usage_metrics.otel_metrics, "get_meter", provider.get_meter)
    usage_metrics._instruments.cache_clear()
    try:
        usage_metrics.record_usage_growth(
            {"input_tokens": 10, "output_tokens": 0, "total_tokens": 99, "total_cost_usd": 0.5},
            model="m",
        )
        usage_metrics.record_usage_growth({"input_tokens": 5, "total_cost_usd": -1.0}, model="m")
        usage_metrics.record_rate_limits(
            {
                "limit_id": "codex",
                "primary": {"used_percent": 80.0, "window_minutes": 10080, "resets_at": 17},
            }
        )
        points = _points(reader)
    finally:
        usage_metrics._instruments.cache_clear()

    # Zero, negative and ``total_tokens`` entries add nothing.
    assert points[usage_metrics.TOKENS_METRIC_NAME] == [({**_UNSPLIT, "token.type": "input"}, 15)]
    assert points[usage_metrics.COST_METRIC_NAME] == [(_UNSPLIT, pytest.approx(0.5))]
    window = {"limit_id": "codex", "window": "primary", "window_minutes": 10080}
    assert points[usage_metrics.RATE_LIMIT_USED_METRIC_NAME] == [(window, 80.0)]
    assert points[usage_metrics.RATE_LIMIT_RESETS_AT_METRIC_NAME] == [(window, 17)]


def test_usage_growth_carries_harness_host_agent_project_and_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = InMemoryMetricReader()
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
    monkeypatch.setattr(
        usage_metrics.otel_metrics, "get_meter", MeterProvider(metric_readers=[reader]).get_meter
    )
    usage_metrics._instruments.cache_clear()
    try:
        usage_metrics.record_usage_growth(
            {"output_tokens": 3, "total_cost_usd": 0.25},
            model="m",
            harness="claude-native",
            host_name="macbook",
            agent_name="claude",
            project_name="homelab",
            role="verifier",
        )
        points = _points(reader)
    finally:
        usage_metrics._instruments.cache_clear()

    split = {
        "model": "m",
        "omnigent.harness": "claude-native",
        "omnigent.host.name": "macbook",
        "omnigent.agent.name": "claude",
        "omnigent.project.name": "homelab",
        "omnigent.role": "verifier",
    }
    assert points[usage_metrics.TOKENS_METRIC_NAME] == [({**split, "token.type": "output"}, 3)]
    assert points[usage_metrics.COST_METRIC_NAME] == [(split, pytest.approx(0.25))]
