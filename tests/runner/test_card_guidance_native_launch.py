"""Question-card guidance reaches each native harness's real instruction channel.

Each test drives the runner-side launch (or the executor's turn delivery) and
reads what the agent receives: the terminal args, developer instructions, rule
file or per-turn ``system`` field. The guidance must be present when
``offers_ask_user_question`` is true and absent when it is not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

import omnigent.runtime.prompt as prompt_module
from omnigent.entities.session_resources import SessionResourceView
from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.codex_native import bridge as codex_native_bridge
from omnigent.harnesses.opencode_native.bridge import OPENCODE_NATIVE_REQUEST_SESSION_ID_ENV_VAR
from omnigent.inner.opencode_native_executor import OpenCodeNativeExecutor
from omnigent.runner.app import (
    _auto_create_claude_terminal,
    _auto_create_codex_terminal,
    _auto_create_devin_terminal,
    _PiNativeLaunchConfig,
)
from omnigent.runner.session_init_protocol import (
    RunnerSessionInitEnvelope,
    RunnerSessionInitSnapshot,
)
from omnigent.runtime.prompt import ASK_USER_QUESTION_INSTRUCTION, build_instructions_nullable
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.inner.test_opencode_native_executor import _FakeServer, _seed_state
from tests.runner.test_app_sessions_native_terminals_autocreate import (
    _launch_pi_terminal_args,
    _no_op_forwarder,
    _stub_devin_launch,
)

_AUTHOR_TEXT = "Be a concise, careful coding assistant."
_OFFERED = pytest.mark.parametrize("offered", [True, False], ids=["offered", "not-offered"])


def _set_offered(monkeypatch: pytest.MonkeyPatch, offered: bool) -> None:
    """Make the single guidance decision answer ``offered`` for every caller."""
    monkeypatch.setattr(
        prompt_module, "offers_ask_user_question", lambda *_args, **_kwargs: offered
    )


def _spec(harness: str, instructions: str | None) -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name=harness,
        instructions=instructions,
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
    )


def _assert_guidance(text: str, offered: bool) -> None:
    assert (ASK_USER_QUESTION_INSTRUCTION in text) is offered, text


@_OFFERED
@pytest.mark.asyncio
async def test_devin_rule_file_carries_guidance_only_when_offered(
    offered: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spec with no instructions still gets the paragraph in the always-on rule."""
    _set_offered(monkeypatch, offered)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda **_kwargs: None)
    _stub_devin_launch(tmp_path, monkeypatch)

    async def _launch_config(**_kwargs: Any) -> _PiNativeLaunchConfig:
        return _PiNativeLaunchConfig(
            workspace=workspace,
            server_url="http://127.0.0.1:8000",
            terminal_launch_args=None,
            external_session_id=None,
        )

    monkeypatch.setattr("omnigent.runner.app._pi_native_launch_config", _launch_config)

    class _Stop(Exception):
        pass

    class _Registry:
        terminal_registry = None

        async def launch_required_terminal(self, **_kwargs: Any) -> SessionResourceView:
            raise _Stop

    with pytest.raises(_Stop):
        await _auto_create_devin_terminal(
            "5f0c2d7e9a8b4c6d8e1f2a3b4c5d6e7f",
            _Registry(),  # type: ignore[arg-type]
            lambda _sid, _evt: None,
            server_client=None,
            agent_spec=_spec("devin-native", None),
        )

    rule_path = workspace / ".windsurf" / "rules" / "omnigent-agent-instructions.md"
    if offered:
        assert rule_path.is_file()
        _assert_guidance(rule_path.read_text(encoding="utf-8"), True)
    else:
        assert not rule_path.exists()


@_OFFERED
@pytest.mark.asyncio
async def test_opencode_prompt_system_field_carries_guidance_only_when_offered(
    offered: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner's composed instructions reach opencode as the prompt's ``system`` field."""
    import omnigent.harnesses.opencode_native.http_transport as transport_mod
    from omnigent.harnesses.opencode_native.client import OpenCodeClient

    _set_offered(monkeypatch, offered)
    server = _FakeServer()

    def _client_for_state(**_kwargs: Any) -> OpenCodeClient:
        mock = httpx.AsyncClient(
            base_url="http://opencode.test", transport=httpx.MockTransport(server.handler)
        )
        return OpenCodeClient("http://opencode.test", client=mock)

    monkeypatch.setattr(transport_mod, "client_for_state", _client_for_state)
    _seed_state(tmp_path)
    monkeypatch.setenv(OPENCODE_NATIVE_REQUEST_SESSION_ID_ENV_VAR, "conv_1")
    executor = OpenCodeNativeExecutor(bridge_dir=tmp_path)
    executor._boot_poll_attempts = 1
    executor._boot_poll_delay = 0.0

    # The gated composition the runner performs for opencode-native turns.
    composed = build_instructions_nullable(_spec("opencode-native", None), None, [])
    async for _event in executor.run_turn(
        [{"role": "user", "content": "hello"}], [], composed or ""
    ):
        pass

    [system] = [
        body.get("system", "")
        for _m, path, body in server.requests
        if path.endswith("/prompt_async")
    ]
    _assert_guidance(system, offered)


@_OFFERED
@pytest.mark.asyncio
async def test_claude_append_system_prompt_carries_guidance_only_when_offered(
    offered: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_offered(monkeypatch, offered)
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder", _no_op_forwarder
    )
    captured: dict[str, Any] = {}

    class _Registry:
        terminal_registry = None

        async def launch_required_terminal(
            self, *, session_id: str, spec: Any, **_kwargs: Any
        ) -> SessionResourceView:
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name="claude:main",
                metadata={"terminal_name": "claude", "session_key": "main", "running": True},
            )

    client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"labels": {}})),
    )
    await _auto_create_claude_terminal(
        "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
        _Registry(),  # type: ignore[arg-type]
        lambda _sid, _evt: None,
        server_client=client,
        agent_spec=_spec("claude-native", _AUTHOR_TEXT),
    )

    args = captured["spec"].args
    appended = args[args.index("--append-system-prompt") + 1]
    assert appended.startswith(_AUTHOR_TEXT)
    _assert_guidance(appended, offered)
    _assert_guidance("\n".join(args), offered)


@_OFFERED
@pytest.mark.asyncio
async def test_codex_developer_instructions_carry_guidance_only_when_offered(
    offered: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnigent.harnesses.codex_native.app_server as codex_app_mod

    _set_offered(monkeypatch, offered)
    session_id = "76cbdcbbf84d4149b2a7d7441b6966c1"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)

    class _Stop(Exception):
        pass

    captured: dict[str, Any] = {}

    def _build_server(**kwargs: Any) -> Any:
        captured.update(kwargs)
        raise _Stop

    monkeypatch.setattr(codex_app_mod, "build_codex_native_server", _build_server)
    session_init = RunnerSessionInitEnvelope.model_validate(
        {
            "protocol_version": 2,
            "server_version": "test",
            "session_id": session_id,
            "agent_id": "agent_codex",
            "snapshot": RunnerSessionInitSnapshot(
                created_at=10, updated_at=11, model_override="gpt-5.4-mini"
            ).model_dump(mode="json"),
        }
    )

    class _Registry:
        terminal_registry = None

    with pytest.raises(_Stop):
        await _auto_create_codex_terminal(
            session_id,
            _Registry(),  # type: ignore[arg-type]
            lambda _sid, _evt: None,
            agent_spec=_spec("codex-native", _AUTHOR_TEXT),
            server_client=None,
            session_init=session_init,
        )

    developer_instructions = captured["developer_instructions"]
    assert developer_instructions.startswith(_AUTHOR_TEXT)
    _assert_guidance(developer_instructions, offered)


@_OFFERED
@pytest.mark.asyncio
async def test_pi_instructions_file_carries_guidance_only_when_offered(
    offered: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_offered(monkeypatch, offered)
    args = await _launch_pi_terminal_args(
        tmp_path,
        monkeypatch,
        model_override=None,
        agent_spec=_spec("pi-native", _AUTHOR_TEXT),
    )

    instructions_path = Path(args[args.index("--append-system-prompt") + 1])
    text = instructions_path.read_text(encoding="utf-8")
    assert text.startswith(_AUTHOR_TEXT)
    _assert_guidance(text, offered)
    _assert_guidance("\n".join(args), False)
