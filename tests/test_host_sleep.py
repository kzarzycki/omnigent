"""A host's sleep does not count toward a turn's timers.

The stdlib loop's clock stops in a sleep, so these tests jump it forward by
patching ``loop.time``: that is what uvloop's clock does on a sleeping Mac,
while ``time.monotonic`` stays behind.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from omnigent import host_sleep
from omnigent.host_sleep import HostSleepWatch, push_back
from omnigent.inner import acp_executor, goose_executor, qwen_executor
from omnigent.inner.executor import ExecutorError, TurnComplete
from omnigent.runtime.harnesses import _scaffold
from omnigent.server.schemas import OutputTextDeltaEvent


class _Clock:
    """The running loop's clock, which a test can jump forward as a sleeping host does."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loop = asyncio.get_running_loop()
        real = loop.time
        self.ahead = 0.0
        monkeypatch.setattr(loop, "time", lambda: real() + self.ahead, raising=False)

    def sleep_host(self, seconds: float) -> None:
        self.ahead += seconds


@pytest.fixture
def short_step(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_sleep, "_STEP_S", 0.05)


async def test_a_timeout_outlives_a_host_sleep_and_still_fires_on_a_stall(
    monkeypatch: pytest.MonkeyPatch,
    short_step: None,
) -> None:
    clock = _Clock(monkeypatch)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5) as turn:
            watch = HostSleepWatch(lambda: [turn])
            try:
                await asyncio.sleep(0.1)
                clock.sleep_host(600)  # far past the deadline
                await asyncio.sleep(0.1)  # the turn goes on
                woke = time.monotonic()
                await asyncio.sleep(5)  # a stall with the host awake
            finally:
                watch.stop()
    # 0.5 s in total, less the time before the stall: no second full window.
    assert 0.2 < time.monotonic() - woke < 0.45


async def test_without_the_watch_the_same_sleep_fires_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(monkeypatch)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5):
            await asyncio.sleep(0.1)
            clock.sleep_host(600)
            await asyncio.sleep(0.1)


async def test_a_short_sleep_is_credited_in_full(
    monkeypatch: pytest.MonkeyPatch, short_step: None
) -> None:
    clock = _Clock(monkeypatch)
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(6) as turn:
        watch = HostSleepWatch(lambda: [turn])
        clock.sleep_host(8)  # longer than the time left, shorter than any guess at "long"
        await asyncio.sleep(0.2)  # custom-lint: disable=sleep-then-assert -- watch ticks pass
        watch.stop()
        assert turn.when() - loop.time() == pytest.approx(5.8, abs=0.1)


async def test_a_reset_before_the_tick_does_not_credit_the_sleep_twice(
    monkeypatch: pytest.MonkeyPatch, short_step: None
) -> None:
    clock = _Clock(monkeypatch)
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(10) as idle, asyncio.timeout(120) as absolute:
        watch = HostSleepWatch(lambda: [idle, absolute])
        clock.sleep_host(600)
        watch.check()  # what a watchdog reset does first
        idle.reschedule(loop.time() + 10)
        await asyncio.sleep(0.2)  # custom-lint: disable=sleep-then-assert -- watch ticks pass
        watch.stop()
        assert idle.when() - loop.time() == pytest.approx(9.8, abs=0.1)
        assert absolute.when() - loop.time() == pytest.approx(119.8, abs=0.1)


async def test_push_back_leaves_a_finished_timeout_alone() -> None:
    async with asyncio.timeout(None) as unbounded:
        push_back(unbounded, 5)
    done = asyncio.timeout(1)
    push_back(done, 5)  # never entered
    push_back(None, 5)


# -- uvloop, the loop the harness runner uses ---------------------------------


def _on_uvloop(scenario: Callable[[], Any]) -> None:
    uvloop = pytest.importorskip("uvloop")
    loop = uvloop.new_event_loop()
    try:
        loop.run_until_complete(scenario())
    finally:
        loop.close()


def test_on_uvloop_a_busy_loop_is_not_a_sleep(short_step: None) -> None:
    async def scenario() -> None:
        async with asyncio.timeout(30) as turn:
            watch = HostSleepWatch(lambda: [turn])
            before = turn.when()
            for _ in range(3):
                time.sleep(0.7)  # the loop is stuck, the host is awake
                await asyncio.sleep(0.1)
            watch.stop()
            assert turn.when() == before

    _on_uvloop(scenario)


def test_on_uvloop_the_tick_runs_before_a_deadline_that_is_also_due(
    monkeypatch: pytest.MonkeyPatch, short_step: None
) -> None:
    """After a wake the tick and the deadline are both due: the tick must win."""
    slept = [0.0]
    real_ahead = host_sleep._ahead
    monkeypatch.setattr(host_sleep, "_ahead", lambda loop: real_ahead(loop) + slept[0])

    async def scenario() -> None:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.3) as turn:
                watch = HostSleepWatch(lambda: [turn])
                try:
                    time.sleep(0.6)  # both timers are now overdue ...
                    slept[0] += 0.6  # ... and the clocks say the host slept
                    await asyncio.sleep(0.1)  # the turn goes on
                    woke = time.monotonic()
                    await asyncio.sleep(5)  # a stall with the host awake
                finally:
                    watch.stop()
        assert time.monotonic() - woke < 0.4

    _on_uvloop(scenario)


# -- the turn watchdogs (omnigent.runtime.harnesses._scaffold) ----------------


class _Turn(_scaffold.HarnessApp):
    def __init__(self, body: Callable[[_scaffold.TurnContext], Any]) -> None:
        super().__init__()
        self._body = body

    async def run_turn(self, request: Any, ctx: _scaffold.TurnContext) -> None:
        await self._body(ctx)


def _ctx() -> _scaffold.TurnContext:
    return _scaffold.TurnContext(
        response_id="sleep", event_queue=asyncio.Queue(), cancelled=asyncio.Event()
    )


async def test_turn_watchdogs_outlive_a_host_sleep(
    monkeypatch: pytest.MonkeyPatch, short_step: None
) -> None:
    monkeypatch.setattr(_scaffold, "_TURN_IDLE_TIMEOUT_S", 0.5)
    monkeypatch.setattr(_scaffold, "_TURN_ABSOLUTE_TIMEOUT_S", 1.0)
    clock = _Clock(monkeypatch)

    async def body(ctx: _scaffold.TurnContext) -> None:
        await asyncio.sleep(0.1)
        clock.sleep_host(600)
        await asyncio.sleep(0.1)

    await _Turn(body)._guarded_run_turn(None, _ctx())


async def test_progress_right_after_a_wake_leaves_one_idle_window(
    monkeypatch: pytest.MonkeyPatch, short_step: None
) -> None:
    """A stall after the wake fails after the idle timeout, not after the sleep's length."""
    monkeypatch.setattr(_scaffold, "_TURN_IDLE_TIMEOUT_S", 0.3)
    monkeypatch.setattr(_scaffold, "_TURN_ABSOLUTE_TIMEOUT_S", 5.0)
    clock = _Clock(monkeypatch)
    woke = [0.0]

    async def body(ctx: _scaffold.TurnContext) -> None:
        clock.sleep_host(600)
        ctx.emit(OutputTextDeltaEvent(type="response.output_text.delta", delta="awake"))
        woke[0] = time.monotonic()
        await asyncio.sleep(3)  # a stall with the host awake

    with pytest.raises(RuntimeError, match="idle watchdog"):
        await _Turn(body)._guarded_run_turn(None, _ctx())
    assert time.monotonic() - woke[0] < 1


# -- the executors' idle deadline ---------------------------------------------

_EXECUTORS = [
    pytest.param(qwen_executor, qwen_executor.QwenExecutor, id="qwen"),
    pytest.param(goose_executor, goose_executor.GooseExecutor, id="goose"),
    pytest.param(
        acp_executor,
        lambda: acp_executor.AcpExecutor(acp_executor.AcpAgentConfig(command="fake")),
        id="acp",
    ),
]


async def _run_turn(
    module: Any, factory: Callable[[], Any], answer: Callable[[Any, int], Any]
) -> list[Any]:
    executor = factory()
    executor._initialized = True
    executor._session_id = "sess-sleep"
    executor._proc = MagicMock()
    executor._proc.returncode = None
    tasks: list[asyncio.Task[None]] = []

    async def fake_send(msg: dict[str, Any]) -> None:
        if msg.get("method") == "session/prompt":
            tasks.append(asyncio.create_task(answer(executor, msg["id"])))

    executor._send = fake_send
    try:
        return [e async for e in executor.run_turn([{"role": "user", "content": "hi"}], [], "")]
    finally:
        for task in tasks:
            task.cancel()


@pytest.mark.parametrize(("module", "factory"), _EXECUTORS)
async def test_executor_idle_timeout_does_not_count_a_host_sleep(
    module: Any, factory: Callable[[], Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host sleeps ten minutes in the turn's last wait; the answer comes right after."""
    monkeypatch.setattr(module, "_PROMPT_TIMEOUT_SECONDS", 1.0)
    clock = _Clock(monkeypatch)

    async def answer(executor: Any, rid: int) -> None:
        await asyncio.sleep(0.2)
        clock.sleep_host(600)
        await asyncio.sleep(0.05)
        executor._pending[rid].set_result(
            {"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}}
        )

    events = await _run_turn(module, factory, answer)
    assert any(isinstance(e, TurnComplete) for e in events), events


@pytest.mark.parametrize(("module", "factory"), _EXECUTORS)
async def test_executor_idle_timeout_still_fires_on_a_stall(
    module: Any, factory: Callable[[], Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "_PROMPT_TIMEOUT_SECONDS", 0.3)

    async def never(executor: Any, rid: int) -> None:
        await asyncio.sleep(60)

    started = time.monotonic()
    events = await _run_turn(module, factory, never)
    assert [type(e) for e in events] == [ExecutorError], events
    assert 0.25 < time.monotonic() - started < 1
