"""The runner forwards a ``session_ended`` event to the session's harness, and only that."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.runner.app import create_runner_app
from omnigent.runtime.harnesses.process_manager import NoLiveHarnessError
from omnigent.spec.types import AgentSpec, ExecutorSpec
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


class _IdleExitedProcessManager(_ProcessManager):
    """No live worker: ``"any"`` finds nothing, and the harness family starts one."""

    def __init__(self, client: _HarnessClient) -> None:
        super().__init__(None)
        self.started = client
        self.spawned: list[tuple[str, dict[str, str] | None]] = []

    async def get_client(self, conversation_id: str, harness: str, env: Any = None) -> Any:
        if harness == "any":
            raise NoLiveHarnessError(conversation_id)
        self.spawned.append((harness, env))
        return self.started


@pytest.mark.asyncio
async def test_session_ended_after_an_idle_exit_starts_a_worker_to_tell() -> None:
    """A session whose worker left idle is not left behind: a worker starts to hear it."""
    spec = AgentSpec(
        spec_version=1,
        name="cloud",
        executor=ExecutorSpec(type="omnigent", config={"harness": "acp", "command": "bridge"}),
    )

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return spec

    harness = _HarnessClient()
    manager = _IdleExitedProcessManager(harness)
    app = create_runner_app(
        process_manager=manager,  # type: ignore[arg-type]
        spec_resolver=resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        created = await client.post(
            "/v1/sessions", json={"session_id": CONVERSATION_ID, "agent_id": "agent"}
        )
        assert created.status_code == 201, created.text
        manager.spawned.clear()
        response = await client.post(
            f"/v1/sessions/{CONVERSATION_ID}/events", json={"type": "session_ended"}
        )
    assert response.status_code == 204
    assert [family for family, _ in manager.spawned] == ["acp"]
    assert harness.posts[-1]["json"] == {"type": "session_ended"}


@pytest.mark.asyncio
async def test_session_ended_with_no_worker_and_no_spec_starts_nothing(caplog) -> None:
    manager = _IdleExitedProcessManager(_HarnessClient())
    app = create_runner_app(
        process_manager=manager,  # type: ignore[arg-type]
        spec_resolver=None,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    response = await _send(app, "session_ended")
    assert response.status_code == 204
    assert manager.spawned == []
    assert "ended with no live harness" in caplog.text
