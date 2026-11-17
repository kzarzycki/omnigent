"""A ``session_ended`` event reaches the executor; an interrupt does not end the session."""

from __future__ import annotations

import asyncio
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
async def test_the_adapter_tells_its_executor_the_session_ended(caplog) -> None:
    executor = _Executor()
    adapter = ExecutorAdapter(lambda: executor, session_key="key")
    with caplog.at_level(logging.WARNING):
        await adapter.on_session_ended()  # no executor built yet: nothing to tell
    assert "no executor in this harness to tell" in caplog.text
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
    executor._session_id = "acp-1"

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
    assert [(call["method"], call["params"]) for call in sent] == [
        ("_omnigent/session_ended", {"sessionId": "acp-1"})
    ]


@pytest.mark.asyncio
async def test_an_agent_that_did_not_ask_is_left_alone() -> None:
    sent: list[dict] = []
    await _acp_executor_with_agent(None, sent).end_session("key")
    assert sent == []


@pytest.mark.asyncio
async def test_a_respawned_agent_with_no_session_is_logged(caplog) -> None:
    sent: list[dict] = []
    executor = _acp_executor_with_agent("_omnigent/session_ended", sent)
    executor._advertised_session_ended = True
    executor._session_ended_method = None  # a respawn clears both until the next turn
    executor._session_id = None
    with caplog.at_level(logging.WARNING):
        await executor.end_session("key")
    assert sent == [] and "no session to tell" in caplog.text


@pytest.mark.asyncio
async def test_an_agent_that_never_asked_is_not_logged(caplog) -> None:
    executor = _acp_executor_with_agent(None, [])
    executor._session_id = None
    with caplog.at_level(logging.WARNING):
        await executor.end_session("key")
    assert "no session to tell" not in caplog.text


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
    assert executor._advertised_session_ended


@pytest.mark.asyncio
async def test_the_advertised_flag_survives_a_respawn_that_omits_the_capability(caplog) -> None:
    executor = acp.AcpExecutor(acp.AcpAgentConfig(command="x", name="bridge"))
    capabilities: dict = {"omnigentSessionEnded": {"method": "_omnigent/session_ended"}}

    async def rpc(method: str, params: dict, timeout: float = 30.0) -> dict:
        return {"result": {"agentCapabilities": capabilities}}

    executor._rpc = rpc  # type: ignore[assignment]
    await executor._ensure_initialized()
    assert executor._advertised_session_ended

    # What _start_process resets on a respawn, then a handshake without the capability.
    executor._initialized = False
    executor._session_ended_method = None
    executor._reset_session_state()
    capabilities.clear()
    await executor._ensure_initialized()
    assert executor._session_ended_method is None
    assert executor._advertised_session_ended

    with caplog.at_level(logging.WARNING):
        await executor.end_session("key")
    assert "no session to tell" in caplog.text


def _executor_after_exit(method: str | None, status: int) -> tuple[acp.AcpExecutor, list]:
    """An executor whose agent has left with *status*; idle exits use status 75."""
    steps: list = []
    executor = _acp_executor_with_agent(method, steps)
    executor._proc = SimpleNamespace(returncode=status)  # type: ignore[assignment]
    executor._idle_exit_status = 75

    async def start(cwd: str | None = None) -> None:
        steps.append("spawn")
        executor._reset_session_state()  # a respawn forgets the old process's session

    async def initialize() -> None:
        steps.append("initialize")
        executor._session_ended_method = "_omnigent/session_ended"

    executor._start_process = start  # type: ignore[method-assign]
    executor._ensure_initialized = initialize  # type: ignore[method-assign]
    return executor, steps


@pytest.mark.asyncio
async def test_an_agent_that_left_idle_is_started_again_to_be_told() -> None:
    executor, steps = _executor_after_exit("_omnigent/session_ended", 75)
    await executor.end_session("key")
    assert [s if isinstance(s, str) else s["method"] for s in steps] == [
        "spawn",
        "initialize",
        "_omnigent/session_ended",
    ]
    assert steps[-1]["params"] == {"sessionId": "acp-1"}


@pytest.mark.asyncio
async def test_an_agent_that_never_advertised_the_notice_is_not_started() -> None:
    executor, steps = _executor_after_exit(None, 75)
    await executor.end_session("key")
    assert steps == []


@pytest.mark.asyncio
async def test_an_agent_that_exited_for_another_reason_is_not_started(caplog) -> None:
    executor, steps = _executor_after_exit("_omnigent/session_ended", 1)
    with caplog.at_level(logging.WARNING):
        await executor.end_session("key")
    assert steps == [] and "exited (status 1)" in caplog.text


@pytest.mark.asyncio
async def test_a_failed_start_after_an_idle_exit_is_logged_not_raised(caplog) -> None:
    executor, _ = _executor_after_exit("_omnigent/session_ended", 75)

    async def broken() -> None:
        raise OSError("no such file")

    executor._start_process = broken  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        await executor.end_session("key")
    assert "session-ended notice failed" in caplog.text


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, asyncio.subprocess.Process]]:
    """Real stand-in agent processes, recorded with the cwd each was spawned in."""
    spawns: list[tuple[str, asyncio.subprocess.Process]] = []
    real_spawn = asyncio.create_subprocess_exec

    async def spawn(*_args: object, cwd: str, **kwargs: object) -> asyncio.subprocess.Process:
        proc = await real_spawn(
            "sleep",
            "60",
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        spawns.append((cwd, proc))
        return proc

    monkeypatch.setattr(acp.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(acp.AcpExecutor, "_sandbox_launch", lambda self, keys: ("sleep", []))
    monkeypatch.setattr(acp.AcpExecutor, "_start_reader_tasks", lambda self: None, raising=False)
    return spawns


async def _second_turn(executor: acp.AcpExecutor) -> list[object]:
    async def initialized() -> None:
        executor._initialized = True

    async def no_session() -> str:
        raise RuntimeError("stop after spawn")

    executor._ensure_initialized = initialized  # type: ignore[method-assign]
    executor._ensure_session = no_session  # type: ignore[method-assign]
    return [event async for event in executor.run_turn([], [], "")]


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace_removed", [True, False])
async def test_the_agent_started_for_the_notice_is_stopped_before_the_next_turn(
    tmp_path, spawned, workspace_removed
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = acp.AcpExecutor(acp.AcpAgentConfig(command="x", name="bridge"), cwd=str(workspace))
    executor._proc = SimpleNamespace(returncode=75)  # type: ignore[assignment]
    executor._idle_exit_status = 75
    executor._session_ended_method = "_omnigent/session_ended"
    executor._session_id = "acp-1"

    async def initialize() -> None:
        executor._session_ended_method = "_omnigent/session_ended"

    async def rpc(*_args: object, **_kwargs: object) -> dict:
        return {}

    async def _no_op(*_args: object, **_kwargs: object) -> None:
        return None

    executor._ensure_initialized = initialize  # type: ignore[method-assign]
    executor._rpc = rpc  # type: ignore[assignment]
    executor._session_id = "acp-1"
    if workspace_removed:
        workspace.rmdir()

    await executor.end_session("key")
    assert len(spawned) == 1
    assert spawned[0][1].returncode is not None  # the notice's agent did not stay up

    events = await _second_turn(executor)
    if workspace_removed:
        assert len(spawned) == 1  # refused by the workspace check, nothing spawned
        assert "does not exist" in events[0].message
    else:
        assert [cwd for cwd, _ in spawned] == [str(workspace), str(workspace)]
    for _, proc in spawned:
        if proc.returncode is None:
            proc.kill()
