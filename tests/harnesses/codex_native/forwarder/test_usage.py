"""Usage tests for Codex forwarder."""

from __future__ import annotations

import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from tests.harnesses.codex_native.forwarder._support import (
    _RecordingClient,
)

# ── sub-agent usage pricing: seed the child coalescer's model ──────────


@pytest.mark.asyncio
async def test_usage_coalescer_seeded_model_rides_along_so_child_usage_prices() -> None:
    """A coalescer seeded with a model attaches it to its token post.

    Codex sub-agent (child-thread) usage is recorded on a coalescer created on
    the child-event path, where ``forwarder_state`` (the usual model source) is
    intentionally ``None`` — so ``record()`` receives no model. Without the
    constructor seed the token post carries no ``model``, the server leaves the
    child's ``total_cost_usd`` unpriced (``None``), and the sub-agent's spend
    drops out of the parent's subtree cost — letting it run past the budget.
    The seeded model must ride along on every token post so the server can
    price the cumulative tokens.
    """
    client = _RecordingClient()
    coalescer = fwd._SessionUsageCoalescer(client, "conv_child", model="gpt-5.5")
    # Mirror the child path exactly: a usage frame with NO model in record().
    coalescer.record({"tokenUsage": {"total": {"inputTokens": 46003, "outputTokens": 4141}}})
    await coalescer.flush()

    assert len(client.posts) == 1  # one external_session_usage post
    url, body = client.posts[0]
    assert url == "/v1/sessions/conv_child/events"
    assert body["type"] == "external_session_usage"
    data = body["data"]
    # The seeded model rides along — this is what lets the server price the
    # tokens into the child's total_cost_usd (the whole point of the fix).
    assert data["model"] == "gpt-5.5"
    # The cumulative token counts the server prices from are present.
    assert data["cumulative_input_tokens"] == 46003
    assert data["cumulative_output_tokens"] == 4141


@pytest.mark.asyncio
async def test_usage_coalescer_unseeded_omits_model() -> None:
    """Without a seed (and no model via record), the post carries no model.

    This is the pre-fix behavior that left a sub-agent's cost unpriced; the
    test pins the contrast so a regression dropping the seed is caught (the
    post would silently go back to model-less and the budget gap would return).
    """
    client = _RecordingClient()
    coalescer = fwd._SessionUsageCoalescer(client, "conv_child")  # no model seed
    coalescer.record({"tokenUsage": {"total": {"inputTokens": 100, "outputTokens": 5}}})
    await coalescer.flush()

    assert len(client.posts) == 1
    _url, body = client.posts[0]
    assert "model" not in body["data"]


@pytest.mark.asyncio
async def test_turn_boundary_posts_the_rollouts_latest_rate_limit_windows(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The newest windowed rollout snapshot reaches the server stripped to its windows.

    A later snapshot with no window (Codex's ``premium`` bucket) does not hide
    it; plan, credits and spend-control state stay on the host; an unchanged
    snapshot at the next turn boundary posts nothing.
    """
    import json
    import types

    thread_id = "01900000-0000-7000-8000-000000000001"
    codex_home = tmp_path / "codex-home"
    rollout = codex_home / "sessions/2026/10/19" / f"rollout-2026-10-19T06-46-30-{thread_id}.jsonl"
    rollout.parent.mkdir(parents=True)

    def token_count(rate_limits: dict) -> str:
        payload = {"type": "token_count", "info": None, "rate_limits": rate_limits}
        return json.dumps({"type": "event_msg", "payload": payload}) + "\n"

    weekly = {"used_percent": 80.0, "window_minutes": 10080, "resets_at": 1792874497}
    rollout.write_text(
        token_count({"limit_id": "codex", "primary": {**weekly, "used_percent": 66.0}})
        + token_count(
            {
                "limit_id": "codex",
                "limit_name": None,
                "primary": weekly,
                "secondary": None,
                "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                "plan_type": "prolite",
                "spend_control_reached": None,
            }
        )
        + token_count({"limit_id": "premium", "primary": None, "secondary": None})
        + json.dumps({"type": "response_item", "payload": {"type": "message"}})
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        fwd,
        "read_bridge_state",
        lambda _dir: types.SimpleNamespace(thread_id=thread_id, codex_home=str(codex_home)),
    )

    assert fwd._rollout_rate_limits(tmp_path) == {"limit_id": "codex", "primary": weekly}

    client = _RecordingClient()
    coalescer = fwd._SessionUsageCoalescer(client, "conv_parent")
    for turn in ("turn_1", "turn_2"):
        await fwd._handle_terminal_turn_boundary_inner(
            client,
            session_id="conv_parent",
            bridge_dir=tmp_path,
            method="turn/completed",
            params={"threadId": thread_id, "turn": {"id": turn, "status": "completed"}},
            usage_coalescer=coalescer,
            delta_coalescer=None,
            elicitation_tracker=fwd._CodexElicitationTaskTracker(),
            codex_client=None,
            forwarder_state=None,
        )

    usage_posts = [
        body["data"] for _url, body in client.posts if body["type"] == "external_session_usage"
    ]
    assert usage_posts == [{"rate_limits": {"limit_id": "codex", "primary": weekly}}]
