"""A ``session_ended`` event reaches the executor; an interrupt does not end the session."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from omnigent.inner import acp_executor as acp
from omnigent.inner.executor import Executor, ExecutorConfig, ExecutorEvent, Message, ToolSpec
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.runtime.harnesses._scaffold import SessionEndedEvent


class _Executor(Executor):
    def __init__(self) -> None:
        self.ended: list[str] = []

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        return
        yield

    async def end_session(self, session_key: str) -> None:
        self.ended.append(session_key)


@pytest.mark.asyncio
async def test_the_adapter_tells_its_executor_the_session_ended() -> None:
    executor = _Executor()
    adapter = ExecutorAdapter(lambda: executor, session_key="key")
    await adapter.on_session_ended()  # no executor built yet: nothing to tell
    adapter._executor = executor
    response = await adapter._post_session_event(
        "c" * 32,
        SessionEndedEvent(type="session_ended"),
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(conversation_id="c" * 32))),  # type: ignore[arg-type]
    )
    assert response.status_code == 204
    assert executor.ended == ["key"]


def _acp_executor_with_agent(method: str | None, replies: list[dict]) -> acp.AcpExecutor:
    executor = acp.AcpExecutor(acp.AcpAgentConfig(command="x", name="bridge"))
    executor._proc = SimpleNamespace(returncode=None)  # type: ignore[assignment]
    executor._session_ended_method = method

    async def rpc(rpc_method: str, params: dict, timeout: float = 30.0) -> dict:
        replies.append({"method": rpc_method, "params": params, "timeout": timeout})
        return {}

    executor._rpc = rpc  # type: ignore[assignment]
    return executor


@pytest.mark.asyncio
async def test_the_acp_executor_sends_the_method_the_agent_named() -> None:
    sent: list[dict] = []
    executor = _acp_executor_with_agent("_omnigent/session_ended", sent)
    await executor.end_session("key")
    assert [call["method"] for call in sent] == ["_omnigent/session_ended"]


@pytest.mark.asyncio
async def test_an_agent_that_did_not_ask_is_left_alone() -> None:
    sent: list[dict] = []
    await _acp_executor_with_agent(None, sent).end_session("key")
    assert sent == []


@pytest.mark.asyncio
async def test_a_failed_notice_is_logged_and_not_raised(caplog) -> None:
    executor = _acp_executor_with_agent("_omnigent/session_ended", [])

    async def failing(*_args: object, **_kwargs: object) -> dict:
        raise TimeoutError("no answer")

    executor._rpc = failing  # type: ignore[assignment]
    with caplog.at_level(logging.WARNING):
        await executor.end_session("key")
    assert "session-ended notice failed" in caplog.text


@pytest.mark.asyncio
async def test_an_agent_that_already_left_is_logged(caplog) -> None:
    executor = _acp_executor_with_agent("_omnigent/session_ended", [])
    executor._proc = None
    with caplog.at_level(logging.WARNING):
        await executor.end_session("key")
    assert "no live agent to tell" in caplog.text


@pytest.mark.asyncio
async def test_the_handshake_records_the_method_the_agent_advertises() -> None:
    executor = acp.AcpExecutor(acp.AcpAgentConfig(command="x", name="bridge"))
    capabilities = {"omnigentSessionEnded": {"method": "_omnigent/session_ended"}}

    async def rpc(method: str, params: dict, timeout: float = 30.0) -> dict:
        return {"result": {"agentCapabilities": capabilities}}

    executor._rpc = rpc  # type: ignore[assignment]
    await executor._ensure_initialized()
    assert executor._session_ended_method == "_omnigent/session_ended"
