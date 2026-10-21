"""OpenTelemetry metrics for the LLM usage Omnigent persists in ``session_usage``.

The counters take each write's growth, so a metrics backend can chart tokens
and USD over time without reading the database. Labels stay low-cardinality:
model, token type, rate-limit window and the session's harness, host, agent and
project names, never a session or user id.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from opentelemetry import metrics as otel_metrics

from omnigent.runtime.telemetry import telemetry_guarded

_OTEL_METER_NAME = "omnigent.usage"
TOKENS_METRIC_NAME = "omnigent.usage.tokens"
COST_METRIC_NAME = "omnigent.usage.cost"
RATE_LIMIT_USED_METRIC_NAME = "omnigent.rate_limit.used_percent"
RATE_LIMIT_RESETS_AT_METRIC_NAME = "omnigent.rate_limit.resets_at"

# ``session_usage`` token keys, mapped to their ``token.type`` label.
_TOKEN_TYPES = {
    "input_tokens": "input",
    "cache_read_input_tokens": "cache_read",
    "cache_creation_input_tokens": "cache_creation",
    "output_tokens": "output",
}


@lru_cache(maxsize=1)
def _instruments() -> tuple[Any, Any, Any, Any]:
    """Create the process-wide usage instruments on first use."""
    meter = otel_metrics.get_meter(_OTEL_METER_NAME)
    return (
        meter.create_counter(
            TOKENS_METRIC_NAME, unit="{token}", description="LLM tokens, by token type."
        ),
        meter.create_counter(COST_METRIC_NAME, unit="USD", description="Priced LLM spend."),
        meter.create_gauge(
            RATE_LIMIT_USED_METRIC_NAME,
            unit="%",
            description="Share of a subscription rate-limit window used, as last reported.",
        ),
        meter.create_gauge(
            RATE_LIMIT_RESETS_AT_METRIC_NAME,
            unit="s",
            description="Unix time a subscription rate-limit window resets, as last reported.",
        ),
    )


@telemetry_guarded
def record_usage_growth(
    growth: Mapping[str, Any],
    *,
    model: str | None,
    harness: str | None = None,
    host_name: str | None = None,
    agent_name: str | None = None,
    project_name: str | None = None,
    role: str | None = None,
) -> None:
    """
    Add one ``session_usage`` write's growth to the token and cost counters.

    :param growth: Increase per ``session_usage`` key, e.g.
        ``{"input_tokens": 12, "output_tokens": 350, "total_cost_usd": 0.02}``.
        Missing, zero and negative entries are skipped.
    :param model: Model the growth is attributed to, e.g. ``"claude-opus-4-8"``.
    :param harness: The session's canonical harness, e.g. ``"claude-native"``.
    :param host_name: Name of the host the session runs on, e.g. ``"macbook"``.
    :param agent_name: Name of the session's agent, e.g. ``"claude"``.
    :param project_name: Name of the session's project; empty when it has none.
    :param role: The session's ``omni.role`` label, e.g. ``"verifier"``; ``other``
        when it has none.
    """
    tokens, cost, _, _ = _instruments()
    labels = {
        "model": model or "unknown",
        "omnigent.harness": harness or "unknown",
        "omnigent.host.name": host_name or "unknown",
        "omnigent.agent.name": agent_name or "unknown",
        "omnigent.project.name": project_name or "",
        "omnigent.role": role or "other",
    }
    for key, token_type in _TOKEN_TYPES.items():
        amount = growth.get(key)
        if isinstance(amount, (int, float)) and amount > 0:
            tokens.add(int(amount), {**labels, "token.type": token_type})
    amount = growth.get("total_cost_usd")
    if isinstance(amount, (int, float)) and amount > 0:
        cost.add(float(amount), labels)


@telemetry_guarded
def record_rate_limits(rate_limits: Mapping[str, Any]) -> None:
    """
    Set the rate-limit gauges from one validated snapshot.

    :param rate_limits: ``{"limit_id": "codex", "primary": {"used_percent":
        80.0, "window_minutes": 10080, "resets_at": 1792874497}}``, as
        ``_coerce_rate_limits`` returns it.
    """
    _, _, used, resets_at = _instruments()
    for window in ("primary", "secondary"):
        snapshot = rate_limits.get(window)
        if not isinstance(snapshot, Mapping):
            continue
        labels = {
            "limit_id": str(rate_limits.get("limit_id")),
            "window": window,
            "window_minutes": int(snapshot.get("window_minutes") or 0),
        }
        used.set(float(snapshot["used_percent"]), labels)
        if snapshot.get("resets_at") is not None:
            resets_at.set(int(snapshot["resets_at"]), labels)
