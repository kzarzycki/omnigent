"""The executor adapter hands the composed prompt, card guidance included, to the executor."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorEvent,
    Message,
    ToolSpec,
    TurnComplete,
)
from omnigent.runtime import prompt
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.runtime.harnesses._scaffold import TurnContext
from omnigent.runtime.prompt import ASK_USER_QUESTION_INSTRUCTION, build_instructions
from omnigent.server.schemas import CreateResponseRequest
from omnigent.spec.types import AgentSpec, ExecutorSpec


class _RecordingExecutor(Executor):
    """Executor that records the system prompt of each turn."""

    def __init__(self) -> None:
        self.system_prompts: list[str] = []

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        self.system_prompts.append(system_prompt)
        yield TurnComplete(response="ok")

    async def close(self) -> None:
        """Nothing to release."""


async def _system_prompt_seen_by_executor(instructions: str) -> str:
    """Run one turn through the real adapter and return what the executor received."""
    executor = _RecordingExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    ctx = TurnContext(
        response_id="resp_card", event_queue=asyncio.Queue(), cancelled=asyncio.Event()
    )
    request = CreateResponseRequest(model="test-agent", input="hello", instructions=instructions)
    await adapter.run_turn(request, ctx)
    return executor.system_prompts[0]


def _composed(monkeypatch: pytest.MonkeyPatch, *, offered: bool) -> str:
    """Compose a prompt for a plain agent, with the card tool offered or not."""
    monkeypatch.setattr(prompt, "offers_ask_user_question", lambda *_a, **_k: offered)
    spec = AgentSpec(
        spec_version=1, name="agent", instructions="Agent prompt", executor=ExecutorSpec()
    )
    return build_instructions(spec, None, [])


@pytest.mark.asyncio
async def test_adapter_delivers_card_guidance_to_the_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every non-native harness runs behind this adapter, so the paragraph must arrive."""
    seen = await _system_prompt_seen_by_executor(_composed(monkeypatch, offered=True))
    assert ASK_USER_QUESTION_INSTRUCTION in seen


@pytest.mark.asyncio
async def test_adapter_delivers_no_card_guidance_when_not_offered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the tool not offered, nothing about cards reaches the executor."""
    seen = await _system_prompt_seen_by_executor(_composed(monkeypatch, offered=False))
    assert ASK_USER_QUESTION_INSTRUCTION not in seen
    assert "Agent prompt" in seen
