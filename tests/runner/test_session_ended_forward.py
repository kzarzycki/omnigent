"""The runner forwards a ``session_ended`` event to the session's harness, and only that."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.runner.app import create_runner_app
from omnigent.runtime.harnesses.process_manager import NoLiveHarnessError
from omnigent.spec.types import AgentSpec
from tests.runner.conftest import _runner_client
from tests.runner.helpers import NullServerClient

CONVERSATION_ID = "a136ad3e8265e86eba8564d6cda81a14"


class _HarnessClient:
    def __init__(self, status_code: int = 204) -> None:
        self.posts: list[dict[str, Any]] = []
        self.status_code = status_code

    async def post(self, url: str, *, json: dict[str, Any], timeout: float) -> Any:
        self.posts.append({"url": url, "json": json})
        return SimpleNamespace(status_code=self.status_code)


class _ProcessManager:
    def __init__(self, client: _HarnessClient | None) -> None:
        self.client = client

    async def get_client(self, conversation_id: str, harness: str, env: Any = None) -> Any:
        if self.client is None:
            raise NoLiveHarnessError(conversation_id)
        return self.client

    def has_session(self, conversation_id: str) -> bool:
        return self.client is not None


def _app(client: _HarnessClient | None):
    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return AgentSpec(spec_version=1, name="t")

    return create_runner_app(
        process_manager=_ProcessManager(client),  # type: ignore[arg-type]
        spec_resolver=resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )


async def _send(app, event_type: str):
    async with _runner_client(app) as client:
        return await client.post(
            f"/v1/sessions/{CONVERSATION_ID}/events", json={"type": event_type}
        )


@pytest.mark.asyncio
async def test_session_ended_reaches_the_harness() -> None:
    harness = _HarnessClient()
    response = await _send(_app(harness), "session_ended")
    assert response.status_code == 204
    assert harness.posts == [
        {"url": f"/v1/sessions/{CONVERSATION_ID}/events", "json": {"type": "session_ended"}}
    ]


@pytest.mark.asyncio
async def test_session_ended_without_a_live_harness_is_logged_not_failed(caplog) -> None:
    response = await _send(_app(None), "session_ended")
    assert response.status_code == 204
    assert "ended with no live harness" in caplog.text


@pytest.mark.asyncio
async def test_a_refusing_harness_is_logged_not_failed(caplog) -> None:
    harness = _HarnessClient(status_code=500)
    response = await _send(_app(harness), "session_ended")
    assert response.status_code == 204
    assert "refused session_ended" in caplog.text
