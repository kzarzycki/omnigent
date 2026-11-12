"""Card-guidance delivery at each harness's real instruction channel.

The composed system prompt (with or without the ``sys_ask_user_question``
paragraph) is passed to the real executor's ``run_turn``; each test asserts what
the vendor SDK, client or session receives. Fakes come from the executors' own
test files.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from omnigent.inner.antigravity_executor import AntigravityExecutor
from omnigent.inner.copilot_executor import CopilotExecutor
from omnigent.inner.executor import ExecutorConfig
from omnigent.inner.open_responses_sdk import OpenResponsesExecutor
from omnigent.inner.openai_agents_sdk_executor import OpenAIAgentsSDKExecutor
from omnigent.runtime import prompt as prompt_module
from omnigent.runtime.prompt import ASK_USER_QUESTION_INSTRUCTION, build_instructions
from tests.inner.test_antigravity_executor import (
    _drain,
    _install_fake_sdk,
    _text_step,
)
from tests.inner.test_copilot_executor import _ev, _install_fake_copilot, _user
from tests.inner.test_open_responses_sdk import FakeClient, FakeResponse
from tests.inner.test_openai_agents_sdk_executor import (
    _collect,
    _fake_agents_sdk,
    _FakeResult,
    _FakeRunner,
)
from tests.runtime.test_prompt import _spec


@pytest.fixture(params=[True, False], ids=["guidance_composed", "guidance_composed_away"])
def composed_prompt(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Any]:
    """Yield ``(system_prompt, offered)`` composed by the real prompt builder."""
    offered: bool = request.param
    if not offered:
        monkeypatch.setattr(prompt_module, "offers_ask_user_question", lambda *_a, **_k: False)
    system_prompt = build_instructions(_spec("Agent prompt"), None, [])
    assert (ASK_USER_QUESTION_INSTRUCTION in system_prompt) is offered
    yield system_prompt, offered


def _assert_reaches(received: str | None, offered: bool, where: str) -> None:
    """The paragraph is in ``received`` exactly when it was composed in."""
    assert "Agent prompt" in (received or ""), f"authored prompt missing from {where}"
    assert (ASK_USER_QUESTION_INSTRUCTION in (received or "")) is offered, where


@pytest.mark.asyncio
async def test_openai_agents_delivers_guidance_as_agent_instructions(composed_prompt: Any) -> None:
    """The Agents SDK ``Agent`` is built with the composed prompt as its instructions."""
    system_prompt, offered = composed_prompt
    _FakeRunner.last_calls = []
    _FakeRunner.next_results = []
    _FakeRunner.next_result = _FakeResult(events=[], final_output="done")
    executor = OpenAIAgentsSDKExecutor(client=object(), model="gpt-test")
    with patch(
        "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
        return_value=_fake_agents_sdk(),
    ):
        await _collect(
            executor.run_turn(
                [{"role": "user", "content": "hi"}],
                [],
                system_prompt,
                ExecutorConfig(),
            )
        )
    _assert_reaches(_FakeRunner.last_calls[0]["agent"].instructions, offered, "Agent.instructions")


@pytest.mark.asyncio
async def test_open_responses_delivers_guidance_as_request_instructions(
    composed_prompt: Any,
) -> None:
    """The Responses API request carries the composed prompt as ``instructions``."""
    system_prompt, offered = composed_prompt

    @dataclass
    class _Completed:
        response: Any
        type: str = "response.completed"

    executor = OpenResponsesExecutor(client=FakeClient(FakeResponse()))
    executor._client = FakeClient([_Completed(response=FakeResponse(output_text="ok"))])
    _ = [
        event
        async for event in executor.run_turn(
            [{"role": "user", "content": "hi"}],
            [],
            system_prompt,
            ExecutorConfig(model="gpt-5.3-codex"),
        )
    ]
    _assert_reaches(
        executor._client.responses.last_kwargs.get("instructions"), offered, "request instructions"
    )


@pytest.mark.asyncio
async def test_antigravity_delivers_guidance_as_system_instructions(
    composed_prompt: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK agent config carries the composed prompt as ``system_instructions``."""
    system_prompt, offered = composed_prompt
    captured = _install_fake_sdk(monkeypatch, scripts=[[_text_step("ok")]])
    await _drain(
        AntigravityExecutor(),
        [{"role": "user", "content": "hi", "session_id": "s1"}],
        system_prompt=system_prompt,
    )
    _assert_reaches(captured["configs"][0].system_instructions, offered, "system_instructions")


@pytest.mark.asyncio
async def test_copilot_delivers_guidance_as_appended_system_message(
    composed_prompt: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Copilot session is created with the composed prompt as an append-mode system message."""
    system_prompt, offered = composed_prompt
    state = _install_fake_copilot(monkeypatch, [[_ev("ASSISTANT_MESSAGE", content="ok")]])
    executor = CopilotExecutor(github_token="gho_x")
    _ = [event async for event in executor.run_turn([_user("hi")], [], system_prompt)]
    system_message = state["create_kwargs"][0]["system_message"]
    assert system_message["mode"] == "append"
    _assert_reaches(system_message["content"], offered, "system_message")
    await executor.close()
