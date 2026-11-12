"""The composed card guidance reaches the model through the Codex, Pi and Claude SDK executors."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

import omnigent.runtime.prompt
from omnigent.inner import pi_executor as pi_mod
from omnigent.inner.codex_executor import _CodexAppServerSession
from omnigent.inner.executor import TurnComplete
from omnigent.inner.pi_executor import PiExecutor
from omnigent.runtime.prompt import ASK_USER_QUESTION_INSTRUCTION, build_instructions
from omnigent.spec import AgentSpec

from .test_codex_executor import _FakeProcess as _FakeCodexProcess
from .test_codex_executor import _run
from .test_pi_executor import _FakeProcess as _FakePiProcess

_AUTHORED = "You are the card guidance fixture agent."


def _spec() -> AgentSpec:
    """Stub only the AgentSpec fields the instruction builders read."""
    return cast(
        AgentSpec,
        SimpleNamespace(
            instructions=_AUTHORED,
            skills=[],
            tools=SimpleNamespace(agents=[], builtins=[]),
            spawn=False,
            tool_groups=SimpleNamespace(browser=True),
        ),
    )


def _composed_prompt(monkeypatch: pytest.MonkeyPatch, *, offered: bool) -> str:
    """Compose the prompt with the card paragraph on or composed away."""
    if not offered:
        monkeypatch.setattr(
            omnigent.runtime.prompt, "offers_ask_user_question", lambda *args, **kwargs: False
        )
    prompt = build_instructions(_spec(), None, [])
    assert (ASK_USER_QUESTION_INSTRUCTION in prompt) is offered
    assert _AUTHORED in prompt
    return prompt


# --- Codex: developerInstructions on thread/start ---


def _codex_thread_start_params(system_prompt: str) -> dict[str, Any]:
    """Run one real Codex app-server turn and return the ``thread/start`` params."""
    requests: list[tuple[str, dict[str, Any]]] = []
    session = _CodexAppServerSession(
        codex_path="/bin/echo", cwd="/tmp/workspace", env={}, tool_executor=None
    )
    session.start = AsyncMock()
    session._proc = _FakeCodexProcess()

    async def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        requests.append((method, params))
        if method == "thread/start":
            return {
                "result": {"thread": {"id": "thread-1"}, "model": "m", "reasoningEffort": "low"}
            }
        if method == "turn/start":
            turn = {"id": "turn-1"}
            asyncio.get_running_loop().call_soon(
                session._events.put_nowait, {"method": "turn/completed", "params": {"turn": turn}}
            )
            return {"result": {"turn": turn}}
        return {"result": {}}

    session._request = AsyncMock(side_effect=request)

    async def drive() -> None:
        async for _ in session.run_turn(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            system_prompt=system_prompt,
            model=None,
            cwd=".",
            sandbox="workspace-write",
        ):
            pass

    _run(drive())
    return next(params for method, params in requests if method == "thread/start")


def test_codex_developer_instructions_include_card_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex's ``thread/start`` developer instructions carry the card paragraph."""
    params = _codex_thread_start_params(_composed_prompt(monkeypatch, offered=True))
    assert ASK_USER_QUESTION_INSTRUCTION in params["developerInstructions"]


def test_codex_developer_instructions_omit_card_guidance_when_not_offered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the paragraph composed away, Codex's developer instructions lack it."""
    params = _codex_thread_start_params(_composed_prompt(monkeypatch, offered=False))
    assert _AUTHORED in params["developerInstructions"]
    assert ASK_USER_QUESTION_INSTRUCTION not in params["developerInstructions"]


# --- Pi: --append-system-prompt / --system-prompt CLI argument ---


def _pi_spawn_argv(
    monkeypatch: pytest.MonkeyPatch, system_prompt: str, system_prompt_mode: str
) -> list[str]:
    """Run one real Pi turn with only subprocess creation stubbed; return the argv."""
    captured: dict[str, list[str]] = {}

    async def fake_spawn(*args: str, **kwargs: object) -> _FakePiProcess:
        captured["argv"] = list(args)
        return _FakePiProcess(
            stdout_lines=[
                '{"type": "response", "success": true}',
                '{"type": "message_update", "assistantMessageEvent":'
                ' {"type": "text_delta", "delta": "hi"}}',
                '{"type": "agent_end", "messages": []}',
            ],
            stderr_lines=[],
        )

    monkeypatch.setattr(pi_mod, "_create_subprocess_exec", fake_spawn)

    async def drive() -> list[object]:
        executor = PiExecutor(pi_path="/usr/bin/pi", system_prompt_mode=system_prompt_mode)
        try:
            return [
                event
                async for event in executor.run_turn(
                    [{"role": "user", "content": "hello"}], [], system_prompt
                )
            ]
        finally:
            await executor.close()

    events = _run(drive())
    assert any(isinstance(event, TurnComplete) for event in events)
    return captured["argv"]


@pytest.mark.parametrize("system_prompt_mode", ["append", "replace"])
def test_pi_prompt_argument_includes_card_guidance(
    monkeypatch: pytest.MonkeyPatch, system_prompt_mode: str
) -> None:
    """Pi receives the card paragraph in its system-prompt CLI argument."""
    argv = _pi_spawn_argv(
        monkeypatch, _composed_prompt(monkeypatch, offered=True), system_prompt_mode
    )
    flag = "--system-prompt" if system_prompt_mode == "replace" else "--append-system-prompt"
    assert ASK_USER_QUESTION_INSTRUCTION in argv[argv.index(flag) + 1]


@pytest.mark.parametrize("system_prompt_mode", ["append", "replace"])
def test_pi_prompt_argument_omits_card_guidance_when_not_offered(
    monkeypatch: pytest.MonkeyPatch, system_prompt_mode: str
) -> None:
    """With the paragraph composed away, no Pi argument carries it."""
    argv = _pi_spawn_argv(
        monkeypatch, _composed_prompt(monkeypatch, offered=False), system_prompt_mode
    )
    flag = "--system-prompt" if system_prompt_mode == "replace" else "--append-system-prompt"
    assert _AUTHORED in argv[argv.index(flag) + 1]
    assert not any(ASK_USER_QUESTION_INSTRUCTION in arg for arg in argv)


# --- Claude SDK: ClaudeAgentOptions.system_prompt ---


async def _claude_sdk_system_prompt_option(system_prompt: str, preset: str | None) -> object:
    """Build one real turn's ``ClaudeAgentOptions`` and return its ``system_prompt``."""
    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

    executor = ClaudeSDKExecutor(system_prompt_preset=preset)
    captured: dict[str, Any] = {}

    async def capture_options(sdk: object, *, session_key: str, options: object, model: object):
        captured["options"] = options
        raise RuntimeError("stop after options built")

    with patch.object(executor, "_get_or_create_client", side_effect=capture_options):
        with pytest.raises(RuntimeError, match="stop after options built"):
            async for _ in executor.run_turn(
                [{"role": "user", "content": "hi"}], [], system_prompt
            ):
                pass
    return captured["options"].system_prompt


def _claude_sdk_prompt_text(option: object) -> str:
    """The instruction text in the option: the string itself, or a preset's ``append``."""
    return option["append"] if isinstance(option, dict) else str(option)


@pytest.mark.parametrize("preset", [None, "claude_code"])
def test_claude_sdk_system_prompt_option_includes_card_guidance(
    monkeypatch: pytest.MonkeyPatch, preset: str | None
) -> None:
    """The Claude SDK options' system prompt carries the card paragraph."""
    option = _run(
        _claude_sdk_system_prompt_option(_composed_prompt(monkeypatch, offered=True), preset)
    )
    assert ASK_USER_QUESTION_INSTRUCTION in _claude_sdk_prompt_text(option)


@pytest.mark.parametrize("preset", [None, "claude_code"])
def test_claude_sdk_system_prompt_option_omits_card_guidance_when_not_offered(
    monkeypatch: pytest.MonkeyPatch, preset: str | None
) -> None:
    """With the paragraph composed away, the Claude SDK system prompt lacks it."""
    option = _run(
        _claude_sdk_system_prompt_option(_composed_prompt(monkeypatch, offered=False), preset)
    )
    text = _claude_sdk_prompt_text(option)
    assert _AUTHORED in text
    assert ASK_USER_QUESTION_INSTRUCTION not in text
