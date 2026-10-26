"""The runner closes each id-less text segment of a turn with a message item."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app
from omnigent.runner.app import _text_segment_event, _TextSegment
from omnigent.runner.transports.ws_tunnel.event_delivery import RunnerEventDispatcher
from omnigent.runner.transports.ws_tunnel.frames import TEXT_SEGMENT_CAPABILITY
from omnigent.runner.transports.ws_tunnel.serve import _handle_tunnel_frame
from tests.runner.helpers import NullServerClient


def _closed_segments(*events: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    """Feed *events* through one segment; return (boundary, text, agent) per closed segment."""
    segment = _TextSegment()
    closed = []
    for event in events:
        segment_event = _text_segment_event(segment, event)
        if segment_event is not None:
            item = segment_event["item"]
            assert isinstance(item, dict)
            closed.append(
                (segment_event["text_segment"], item["content"][0]["text"], item["agent"])
            )
    return closed


def _delta(text: str, **extra: Any) -> dict[str, Any]:
    return {"type": "response.output_text.delta", "delta": text, **extra}


_FUNCTION_CALL = {
    "type": "response.output_item.done",
    "item": {"type": "function_call", "status": "completed", "call_id": "call_1"},
}


def test_text_segments_close_at_tool_calls_child_sessions_and_the_turn_end() -> None:
    closed = _closed_segments(
        {"type": "response.in_progress", "response": {"id": "resp_1", "model": "turn-model"}},
        _delta("looking "),
        _delta("around"),
        _FUNCTION_CALL,
        _delta("delegating"),
        {"type": "session.created", "child_session_id": "conv_child"},
        _delta("done"),
        {"type": "response.completed", "response": {"id": "resp_1", "model": "final-model"}},
    )

    assert closed == [
        ("function_call", "looking around", "turn-model"),
        ("session.created", "delegating", "turn-model"),
        ("response.completed", "done", "final-model"),
    ]


def test_text_segments_skip_message_scoped_text_and_restart_with_each_turn() -> None:
    closed = _closed_segments(
        _delta("stale text of an abandoned turn"),
        {"type": "session.status", "status": "running"},
        _delta("native text", message_id="msg_native"),
        {
            "type": "response.output_item.done",
            "item": {"type": "function_call", "status": "in_progress", "call_id": "call_1"},
        },
        _FUNCTION_CALL,
        _delta("answer"),
        {"type": "response.failed", "response": {"id": "resp_2"}},
    )

    assert closed == [("response.failed", "answer", None)]


async def _streamed_texts(ready_frame: str) -> list[str]:
    """Publish one SDK text segment and return the texts ``/stream`` serves the relay."""
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    dispatcher = RunnerEventDispatcher()
    app.state.runner_event_dispatcher = dispatcher

    async def send_text(_text: str) -> None:
        pass

    dispatcher.connected(send_text)
    await _handle_tunnel_frame(app, ready_frame, send_text, {}, {}, event_dispatcher=dispatcher)
    app.state.publish_event("s1", _delta("hello"))
    app.state.publish_event("s1", {"type": "response.completed", "response": {"id": "r"}})
    app.state.session_event_queues["s1"].put_nowait(None)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        response = await client.get("/v1/sessions/s1/stream")
    events = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")
    ]
    return [
        event.get("delta") or event["item"]["content"][0]["text"]
        for event in events
        if event["type"] in {"response.output_text.delta", "response.output_item.done"}
    ]


@pytest.mark.asyncio
async def test_a_server_without_the_text_segment_capability_never_receives_a_segment_item() -> (
    None
):
    # An older server's event.ready carries no capability list.
    assert await _streamed_texts(json.dumps({"kind": "event.ready"})) == ["hello"]


@pytest.mark.asyncio
async def test_a_server_that_accepts_text_segments_receives_the_segment_item() -> None:
    ready = json.dumps({"kind": "event.ready", "capabilities": [TEXT_SEGMENT_CAPABILITY]})
    assert await _streamed_texts(ready) == ["hello", "hello"]


@pytest.mark.asyncio
async def test_a_segment_pulled_while_the_tunnel_is_down_waits_in_order_for_the_next_stream() -> (
    None
):
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    dispatcher = RunnerEventDispatcher()
    app.state.runner_event_dispatcher = dispatcher

    async def send_text(_text: str) -> None:
        pass

    ready = json.dumps({"kind": "event.ready", "capabilities": [TEXT_SEGMENT_CAPABILITY]})
    dispatcher.connected(send_text)
    await _handle_tunnel_frame(app, ready, send_text, {}, {}, event_dispatcher=dispatcher)
    app.state.publish_event("s1", _delta("hello"))
    app.state.publish_event("s1", {"type": "response.completed", "response": {"id": "r"}})
    app.state.session_event_queues["s1"].put_nowait(None)
    # The tunnel drops before the relay's stream pulls the closed segment.
    dispatcher.disconnected()

    async def stream_kinds() -> list[str]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            response = await client.get("/v1/sessions/s1/stream")
        return [
            "segment" if event.get("text_segment") else event["type"]
            for line in response.text.splitlines()
            if line.startswith("data: {")
            and (event := json.loads(line[6:]))["type"] != "session.heartbeat"
        ]

    assert await stream_kinds() == ["response.output_text.delta"]

    dispatcher.connected(send_text)
    await _handle_tunnel_frame(app, ready, send_text, {}, {}, event_dispatcher=dispatcher)
    assert await stream_kinds() == ["segment", "response.completed"]


@pytest.mark.asyncio
async def test_a_frame_whose_send_fails_is_requeued_ahead_of_later_events() -> None:
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    app.state.publish_event("s1", _delta("hello"))
    app.state.publish_event("s1", _FUNCTION_CALL)
    queue = app.state.session_event_queues["s1"]
    endpoint = next(
        route.endpoint  # type: ignore[attr-defined]
        for route in app.routes
        if getattr(route, "path", "") == "/v1/sessions/{session_id}/stream"
    )
    body = (await endpoint("s1")).body_iterator
    await body.__anext__()  # heartbeat
    await body.__anext__()  # the delta, handed to a send that fails
    await body.aclose()  # teardown cancels the stream at that yield

    requeued = [queue.get_nowait() for _ in range(queue.qsize())]
    assert [event["type"] for event in requeued] == [
        "response.output_text.delta",
        "response.output_item.done",
        "response.output_item.done",
    ]
    assert [event.get("text_segment") for event in requeued] == [None, "function_call", None]
