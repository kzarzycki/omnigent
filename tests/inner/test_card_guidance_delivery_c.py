"""The question-card guidance reaches the vendor prompt of cursor, goose, qwen and hermes.

Each test composes the real prompt, hands it to the real executor's ``run_turn``
as ``system_prompt``, and inspects what the vendor SDK / CLI / ACP agent receives.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnigent.inner.cursor_executor import CursorExecutor
from omnigent.inner.goose_executor import GooseExecutor
from omnigent.inner.hermes_executor import HermesExecutor
from omnigent.inner.qwen_executor import QwenExecutor
from omnigent.runtime import prompt
from omnigent.runtime.prompt import ASK_USER_QUESTION_INSTRUCTION, build_instructions
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.inner.test_cursor_executor import _assistant, _install_fake_sdk

_USER_TEXT = "please plan the migration"
_AUTHOR_TEXT = "Agent prompt"


_SERVED_TOOLS = [{"name": "sys_ask_user_question"}]


def _composed(monkeypatch: pytest.MonkeyPatch, *, offered: bool) -> str:
    """Compose a prompt for a plain agent, with the card tool offered or not."""
    monkeypatch.setattr(prompt, "offers_ask_user_question", lambda *_a, **_k: offered)
    spec = AgentSpec(
        spec_version=1, name="agent", instructions=_AUTHOR_TEXT, executor=ExecutorSpec()
    )
    return build_instructions(spec, None, [])


async def _cursor_prompt(monkeypatch: pytest.MonkeyPatch, system_prompt: str) -> str:
    """Run a Cursor SDK turn and return the text passed to ``agent.send``."""
    state = _install_fake_sdk(monkeypatch, [{"messages": [_assistant("ok")], "result": "ok"}])
    executor = CursorExecutor(api_key="crsr_x")
    messages = [{"role": "user", "content": _USER_TEXT, "session_id": "conv1"}]
    try:
        _ = [event async for event in executor.run_turn(messages, _SERVED_TOOLS, system_prompt)]
    finally:
        await executor.close()
    return state["sent"][0]


async def _acp_prompt(
    executor: GooseExecutor | QwenExecutor,
    system_prompt: str,
    served_tools: list[dict[str, str]] = _SERVED_TOOLS,
) -> str:
    """Run a fresh-session ACP turn and return the ``session/prompt`` text block."""
    executor._initialized = True
    executor._session_id = "sess-card"
    executor._proc = MagicMock(returncode=None)
    loop = asyncio.get_event_loop()
    sent_prompts: list[str] = []

    async def fake_send(msg: dict) -> None:
        if msg.get("method") != "session/prompt":
            return
        sent_prompts.append(msg["params"]["prompt"][0]["text"])
        request_id = msg["id"]

        def _resolve() -> None:
            future = executor._pending.get(request_id)
            if future and not future.done():
                future.set_result(
                    {"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "end_turn"}}
                )

        loop.call_soon(_resolve)

    executor._send = fake_send  # type: ignore[method-assign]
    # The real relay start (or its failure), as ``_ensure_session`` would run it.
    executor._mcp.session_new_servers(
        tools=served_tools, tool_executor=lambda name, args: None, loop=loop
    )
    messages = [{"role": "user", "content": _USER_TEXT}]
    try:
        async for _ in executor.run_turn(messages, served_tools, system_prompt):
            pass
    finally:
        executor._mcp.close()
    return sent_prompts[0]


async def _goose_prompt(monkeypatch: pytest.MonkeyPatch, system_prompt: str) -> str:
    """Run a Goose turn and return the prompt text sent over ACP."""
    return await _acp_prompt(GooseExecutor(), system_prompt)


async def _qwen_prompt(monkeypatch: pytest.MonkeyPatch, system_prompt: str) -> str:
    """Run a Qwen turn and return the prompt text sent over ACP."""
    return await _acp_prompt(QwenExecutor(), system_prompt)


async def _hermes_prompt(monkeypatch: pytest.MonkeyPatch, system_prompt: str) -> str:
    """Run a Hermes turn and return the ``-q`` message passed to the CLI."""
    executor = HermesExecutor(hermes_path="/usr/bin/hermes-fake", cwd="/tmp")
    process = MagicMock(returncode=0)
    process.communicate = AsyncMock(
        return_value=(b"session_id: 20260620_card_sid\nHi there!", b"")
    )
    messages = [{"role": "user", "content": _USER_TEXT, "session_id": "card-key"}]
    with patch.object(
        asyncio, "create_subprocess_exec", new=AsyncMock(return_value=process)
    ) as create_subprocess:
        _ = [event async for event in executor.run_turn(messages, _SERVED_TOOLS, system_prompt)]
    argv = list(create_subprocess.call_args.args)
    return argv[argv.index("-q") + 1]


_HARNESS_DRIVERS: dict[str, Callable[[pytest.MonkeyPatch, str], Awaitable[str]]] = {
    "cursor": _cursor_prompt,
    "goose": _goose_prompt,
    "qwen": _qwen_prompt,
    "hermes": _hermes_prompt,
}


@pytest.mark.parametrize("harness", _HARNESS_DRIVERS)
async def test_card_guidance_reaches_the_vendor_prompt(
    harness: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the card tool offered, the vendor prompt carries the guidance before the user turn."""
    composed = _composed(monkeypatch, offered=True)
    sent = await _HARNESS_DRIVERS[harness](monkeypatch, composed)
    assert ASK_USER_QUESTION_INSTRUCTION in sent
    assert sent.startswith(_AUTHOR_TEXT)
    assert sent.endswith(f"\n\n{_USER_TEXT}")


@pytest.mark.parametrize("harness", _HARNESS_DRIVERS)
async def test_composed_away_card_guidance_is_absent_from_the_vendor_prompt(
    harness: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the card tool not offered, the vendor still gets the prompt but no guidance."""
    composed = _composed(monkeypatch, offered=False)
    sent = await _HARNESS_DRIVERS[harness](monkeypatch, composed)
    assert ASK_USER_QUESTION_INSTRUCTION not in sent
    assert sent.startswith(_AUTHOR_TEXT)
    assert sent.endswith(f"\n\n{_USER_TEXT}")


@pytest.mark.parametrize("executor_class", [GooseExecutor, QwenExecutor])
@pytest.mark.parametrize("failure", ["no-card-tool", "kill-switch", "relay-setup-fails"])
async def test_card_guidance_is_dropped_when_the_agent_gets_no_card_tool(
    executor_class: type[GooseExecutor | QwenExecutor],
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A composed prompt with the guidance loses it when no card tool is really served."""
    served_tools = _SERVED_TOOLS
    if failure == "no-card-tool":
        served_tools = [{"name": "other_tool"}]
    elif failure == "kill-switch":
        monkeypatch.setenv("OMNIGENT_ACP_MCP", "0")
    else:

        def _fail_bridge_dir() -> Path:
            raise OSError("bridge directory is not writable")

        monkeypatch.setattr(
            "omnigent.harnesses.claude_native.bridge.prepare_acp_mcp_bridge_dir",
            _fail_bridge_dir,
        )
    composed = _composed(monkeypatch, offered=True)
    sent = await _acp_prompt(executor_class(), composed, served_tools=served_tools)
    assert ASK_USER_QUESTION_INSTRUCTION not in sent
    assert sent.startswith(_AUTHOR_TEXT)
