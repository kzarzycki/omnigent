"""Thinking summaries ask the Codex app-server for detailed reasoning summaries."""

import sys
from pathlib import Path

import pytest

from omnigent.harnesses.codex_native import app_server as codex_native_app_server
from omnigent.harnesses.codex_native.app_server import _build_native_codex_app_server_argv
from omnigent.harnesses.codex_native.launch_args import redact_codex_launch_args

_USER_ARGS = ["-c", 'model_reasoning_effort="high"', "-c", "model_context_window=872000"]


def _overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    thinking_summaries: bool | None,
    terminal_launch_args: list[str],
) -> list[str]:
    monkeypatch.setattr(codex_native_app_server, "_find_codex_cli", lambda: sys.executable)
    flag = {} if thinking_summaries is None else {"thinking_summaries": thinking_summaries}
    server = codex_native_app_server.build_codex_native_server(
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        cwd=tmp_path,
        model="gpt-5.5",
        profile=None,
        bridge_dir=tmp_path / "bridge",
        ap_server_url=None,
        ap_auth_headers={},
        terminal_launch_args=terminal_launch_args,
        **flag,
    )
    return server.config_overrides


@pytest.mark.parametrize("thinking_summaries", [None, False])
def test_off_leaves_the_app_server_overrides_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, thinking_summaries: bool | None
) -> None:
    overrides = _overrides(
        tmp_path,
        monkeypatch,
        thinking_summaries=thinking_summaries,
        terminal_launch_args=[*_USER_ARGS, "-c", 'model_reasoning_summary="concise"'],
    )

    assert overrides == ["model_context_window=872000", 'model="gpt-5.5"']


def test_on_asks_for_detailed_summaries_on_the_app_server_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overrides = _overrides(
        tmp_path, monkeypatch, thinking_summaries=True, terminal_launch_args=_USER_ARGS
    )

    assert overrides == [
        "model_context_window=872000",
        'model_reasoning_summary="detailed"',
        'model="gpt-5.5"',
    ]
    argv = _build_native_codex_app_server_argv(
        tagged_argv0="codex", listen_url="ws://127.0.0.1:1", config_overrides=overrides
    )
    assert argv.count('model_reasoning_summary="detailed"') == 1
    position = argv.index('model_reasoning_summary="detailed"')
    assert argv[position - 1] == "-c"
    assert position < argv.index("app-server")
    # Launch logs show the value rather than masking it.
    assert 'model_reasoning_summary="detailed"' in redact_codex_launch_args(argv)


@pytest.mark.parametrize(
    "user_args",
    [
        ["-c", 'model_reasoning_summary="concise"'],
        ['--config=model_reasoning_summary="concise"'],
        ["-c", 'model_reasoning_summary="none"', "-c", 'model_reasoning_summary="concise"'],
    ],
)
def test_the_users_own_value_follows_and_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user_args: list[str]
) -> None:
    """Codex applies ``-c`` overrides in order, so the user's last value comes after ours."""
    overrides = _overrides(
        tmp_path, monkeypatch, thinking_summaries=True, terminal_launch_args=user_args
    )

    summaries = [o for o in overrides if o.startswith("model_reasoning_summary=")]
    assert summaries == ['model_reasoning_summary="detailed"', 'model_reasoning_summary="concise"']


def test_a_prompt_after_the_separator_is_not_a_user_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overrides = _overrides(
        tmp_path,
        monkeypatch,
        thinking_summaries=True,
        terminal_launch_args=["--", "-c", 'model_reasoning_summary="none"'],
    )

    assert [o for o in overrides if o.startswith("model_reasoning_summary=")] == [
        'model_reasoning_summary="detailed"'
    ]
