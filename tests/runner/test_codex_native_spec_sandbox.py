"""A codex-native session whose spec forbids edits starts read-only."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.runner.native.orchestration import (
    _codex_launch_config_for_spec,
    _CodexNativeLaunchConfig,
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
