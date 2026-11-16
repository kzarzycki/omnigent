"""A codex-native session whose spec forbids edits starts read-only."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.harnesses.codex_native import app_server as codex_app
from omnigent.runner.native import orchestration
from omnigent.runner.native.orchestration import (
    _auto_create_codex_terminal,
    _codex_launch_config_for_spec,
    _CodexNativeLaunchConfig,
)
from omnigent.runner.session_init_protocol import (
    RunnerSessionInitEnvelope,
    RunnerSessionInitSnapshot,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec


def _launch_config(args: list[str] | None, *, bypass: bool = False) -> _CodexNativeLaunchConfig:
    return _CodexNativeLaunchConfig(
        workspace=Path("/work"),
        policy_server_url="http://127.0.0.1:1",
        terminal_launch_args=args,
        model_override=None,
        external_session_id=None,
        fork_source_id=None,
        fork_source_external_id=None,
        fork_carry_history=False,
        bypass_sandbox=bypass,
    )


def _spec(disallowed: list[str]) -> AgentSpec:
    return AgentSpec(
        spec_version=1, executor=ExecutorSpec(config={"disallowed_tools": disallowed})
    )


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "apply_patch"])
def test_a_spec_forbidding_edits_starts_codex_read_only(tool: str) -> None:
    config = _codex_launch_config_for_spec(_launch_config(["--model", "m"]), _spec([tool]))

    assert config.terminal_launch_args == ["--model", "m", "--sandbox", "read-only"]


def test_the_spec_wins_over_a_wider_sandbox_and_the_bypass_opt_in() -> None:
    config = _codex_launch_config_for_spec(
        _launch_config(["--sandbox", "workspace-write"], bypass=True), _spec(["Edit"])
    )

    assert config.terminal_launch_args == ["--sandbox", "read-only"]
    assert config.bypass_sandbox is False


@pytest.mark.parametrize("disallowed", [[], ["Bash"], ["Bash(git push:*)"]])
def test_a_spec_that_leaves_edits_alone_keeps_the_launch_config(disallowed: list[str]) -> None:
    config = _launch_config(["--model", "m"])

    assert _codex_launch_config_for_spec(config, _spec(disallowed)) is config
    assert _codex_launch_config_for_spec(config, None) is config


_WIDENING_OVERRIDES = [
    ["-c", 'sandbox_mode="danger-full-access"'],
    ["--config", 'sandbox_mode="workspace-write"'],
    ["--config=sandbox_mode=danger-full-access"],
    ["-c", 'default_permissions=":danger-full-access"'],
    ["--config=default_permissions=:workspace"],
    ["-c", 'approval_policy="never"'],
    ["--config=approval_policy=never"],
    ["-c", 'permissions.custom.filesystem={":root"="write"}'],
    ["--full-auto"],
]


@pytest.mark.parametrize("override", _WIDENING_OVERRIDES)
def test_a_client_config_override_cannot_widen_a_read_only_spec(override: list[str]) -> None:
    config = _codex_launch_config_for_spec(
        _launch_config([*override, "--model", "m"]), _spec(["Edit"])
    )

    assert config.terminal_launch_args == ["--model", "m", "--sandbox", "read-only"]


def test_unrelated_config_overrides_survive_the_read_only_pin() -> None:
    config = _codex_launch_config_for_spec(
        _launch_config(["-c", 'model_reasoning_effort="low"']), _spec(["Edit"])
    )

    assert config.terminal_launch_args == [
        "-c",
        'model_reasoning_effort="low"',
        "--sandbox",
        "read-only",
    ]


@pytest.mark.parametrize("override", _WIDENING_OVERRIDES)
def test_a_resumed_read_only_spec_session_resumes_read_only(override: list[str]) -> None:
    config = _codex_launch_config_for_spec(_launch_config(override), _spec(["Edit"]))

    params = codex_app._codex_resume_permission_params(config.terminal_launch_args)

    assert params == {"sandbox": "read-only"}


async def test_the_codex_terminal_launch_carries_the_read_only_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Stop(Exception):
        pass

    captured: list[list[str]] = []

    def capture(**kwargs: object) -> None:
        captured.append(list(kwargs["terminal_launch_args"]))  # type: ignore[call-overload]
        raise _Stop

    monkeypatch.setattr(codex_app, "resolve_native_codex_launch", capture)
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://runner.test")
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    session_id = "76cbdcbbf84d4149b2a7d7441b6966c1"
    session_init = RunnerSessionInitEnvelope.model_validate(
        {
            "protocol_version": 2,
            "server_version": "test",
            "session_id": session_id,
            "agent_id": "agent_codex",
            "snapshot": RunnerSessionInitSnapshot(
                created_at=10,
                updated_at=11,
                terminal_launch_args=["-c", 'sandbox_mode="danger-full-access"'],
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
            agent_spec=_spec(["Edit"]),
            server_client=None,
            session_init=session_init,
        )

    assert captured == [["--sandbox", "read-only"]]
