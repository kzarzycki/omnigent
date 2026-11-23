"""Completed Codex reasoning items saved as ``reasoning`` conversation items."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from tests.harnesses.codex_native.forwarder._support import _RecordingClient


def _completed_reasoning(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "threadId": "thread_1",
        "turnId": "turn_1",
        "item": {"type": "reasoning", "id": "rs_1", **item},
    }


async def _deliver(item: dict[str, Any], *, persist_reasoning: bool) -> list[tuple[str, dict]]:
    client = _RecordingClient()
    state = fwd._CodexForwarderState(persist_reasoning=persist_reasoning)
    await fwd._handle_completed_item(
        client, "conv_x", _completed_reasoning(item), forwarder_state=state
    )
    return client.posts


@pytest.mark.asyncio
async def test_reasoning_with_text_posts_one_reasoning_item() -> None:
    """Summary text lands in ``summary`` and raw text in ``content``, in ReasoningData's shape."""
    posts = await _deliver(
        {"summary": ["Reading the config", "  "], "content": ["raw step"]},
        persist_reasoning=True,
    )

    assert len(posts) == 1
    url, body = posts[0]
    assert url == "/v1/sessions/conv_x/events"
    assert body["type"] == "external_conversation_item"
    assert body["data"]["item_type"] == "reasoning"
    assert body["data"]["item_data"] == {
        "agent": "codex-native-ui",
        "summary": [{"type": "summary_text", "text": "Reading the config"}],
        "content": [{"type": "reasoning_text", "text": "raw step"}],
    }
    assert body["data"]["source_id"]


@pytest.mark.asyncio
async def test_summary_only_reasoning_has_no_content() -> None:
    posts = await _deliver({"summary": ["Planning"]}, persist_reasoning=True)

    assert [body["data"]["item_data"]["content"] for _, body in posts] == [None]


@pytest.mark.parametrize(
    "item",
    [
        pytest.param({}, id="no-fields"),
        pytest.param({"summary": [], "content": []}, id="empty-lists"),
        pytest.param({"summary": ["", " \n"], "content": [""]}, id="blank-text"),
    ],
)
@pytest.mark.asyncio
async def test_reasoning_without_text_posts_nothing(item: dict[str, Any]) -> None:
    assert await _deliver(item, persist_reasoning=True) == []


@pytest.mark.asyncio
async def test_reasoning_is_not_saved_when_the_setting_is_off() -> None:
    assert await _deliver({"summary": ["Reading the config"]}, persist_reasoning=False) == []


def test_child_streams_inherit_the_setting() -> None:
    """A Codex sub-agent's thread runs on the same app-server, so it saves reasoning too."""
    state = fwd._CodexForwarderState(persist_reasoning=True)

    stream = state.child_stream(_RecordingClient(), "thread_child", "conv_child")

    assert stream.state.persist_reasoning is True
