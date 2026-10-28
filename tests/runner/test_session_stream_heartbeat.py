"""The runner's session stream opens with a heartbeat naming the turn in flight."""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnigent.runner import create_runner_app
from omnigent.runner.transports.ws_tunnel.frames import (
    RequestFrame,
    ResponseBodyFrame,
    decode_frame,
)
from omnigent.runner.transports.ws_tunnel.serve import dispatch_via_asgi
from tests.runner.helpers import NullServerClient


async def _tunneled_stream_events(live_response_id: str | None) -> list[dict[str, Any]]:
    """Serve ``GET /stream`` through the WebSocket tunnel; return its events in order."""
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    if live_response_id is not None:
        app.state.live_response_id["s1"] = live_response_id
    app.state.publish_event("s1", {"type": "response.output_text.delta", "delta": "hi"})
    app.state.session_event_queues["s1"].put_nowait(None)
    sent: list[str] = []

    async def send_text(text: str) -> None:
        sent.append(text)

    request = RequestFrame(id="req_stream", method="GET", path="/v1/sessions/s1/stream")
    await dispatch_via_asgi(app, request, send_text)
    body = "".join(
        frame.body for frame in map(decode_frame, sent) if isinstance(frame, ResponseBodyFrame)
    )
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: {")]


@pytest.mark.asyncio
async def test_a_subscriber_during_a_turn_gets_a_first_heartbeat_naming_that_turn() -> None:
    events = await _tunneled_stream_events("resp_1")

    assert events[0] == {"type": "session.heartbeat", "response_id": "resp_1"}
    assert events[1]["type"] == "response.output_text.delta"


@pytest.mark.asyncio
async def test_a_subscriber_between_turns_gets_a_bare_first_heartbeat() -> None:
    events = await _tunneled_stream_events(None)

    assert events[0] == {"type": "session.heartbeat"}
