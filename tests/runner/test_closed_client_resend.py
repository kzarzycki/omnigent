"""A send holding a client that a concurrent cleanup closed is resent exactly once."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omnigent.runtime.harnesses.process_manager import post_event, stream_event

URL = "/v1/sessions/c/events"


class _Manager:
    """Hands out a fresh client per ``get_client``; each counts the requests it received."""

    def __init__(self) -> None:
        self.delivered = 0
        self.get_calls = 0

    def _client(self) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            self.delivered += 1
            return httpx.Response(200, json={})

        return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://worker")

    async def get_client(self, conversation_id: str, harness: str, env: Any = None) -> Any:
        self.get_calls += 1
        return self._client()


async def _held_then_closed(manager: _Manager) -> httpx.AsyncClient:
    """The sender holds a client; cleanup closes it (ordered with events, no sleeps)."""
    held = await manager.get_client("c", "acp")
    holding, closed = asyncio.Event(), asyncio.Event()

    async def cleanup() -> None:
        await holding.wait()
        await held.aclose()
        closed.set()

    task = asyncio.ensure_future(cleanup())
    holding.set()
    await closed.wait()
    await task
    manager.get_calls = 0
    return held


@pytest.mark.asyncio
async def test_post_on_a_client_closed_by_cleanup_is_resent_exactly_once() -> None:
    manager = _Manager()
    held = await _held_then_closed(manager)
    resp = await post_event(manager, "c", "acp", held, URL, json={"type": "message"}, timeout=5)  # type: ignore[arg-type]
    assert resp.status_code == 200
    assert manager.delivered == 1
    assert manager.get_calls == 1


@pytest.mark.asyncio
async def test_stream_on_a_client_closed_by_cleanup_is_resent_exactly_once() -> None:
    manager = _Manager()
    held = await _held_then_closed(manager)
    async with stream_event(manager, "c", "acp", held, URL, json={"type": "message"}) as resp:  # type: ignore[arg-type]
        assert resp.status_code == 200
    assert manager.delivered == 1
    assert manager.get_calls == 1


@pytest.mark.asyncio
async def test_a_runtime_error_from_an_open_client_is_not_resent() -> None:
    manager = _Manager()

    def boom(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("other")

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom), base_url="http://w")
    with pytest.raises(RuntimeError):
        await post_event(manager, "c", "acp", client, URL, json={}, timeout=5)  # type: ignore[arg-type]
    assert manager.delivered == 0
    assert manager.get_calls == 0
