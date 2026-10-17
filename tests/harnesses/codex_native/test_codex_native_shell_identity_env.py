"""The native Codex app-server, which runs the agent's shell, carries session identity."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest

from omnigent.debug_logging import PRIMARY_SESSION_ID_ENV_VAR
from omnigent.harnesses.codex_native import app_server
from omnigent.harnesses.codex_native import main as codex_native
from omnigent.runner.identity import RUNNER_AUTH_SECRET_ENV_VARS


async def test_app_server_env_carries_session_identity_but_no_runner_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_home = tmp_path / "source-home"
    source_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    monkeypatch.setenv(PRIMARY_SESSION_ID_ENV_VAR, "stale-runner-value")
    monkeypatch.setenv("OMNIGENT_SESSION_ID", "stale-runner-value")
    for name in RUNNER_AUTH_SECRET_ENV_VARS:
        monkeypatch.setenv(name, "runner-secret")
    monkeypatch.setattr(app_server, "_codex_cli_version", AsyncMock(return_value=(0, 152, 1)))
    monkeypatch.setattr(app_server, "acquire_codex_native_process_owner_lock", lambda: None)

    class SpawnObserved(Exception):
        pass

    spawn_env: dict[str, str] = {}

    async def spawn(*args: object, **kwargs: object) -> None:
        assert args[1] == "app-server"
        spawn_env.update(cast("dict[str, str]", kwargs["env"]))
        raise SpawnObserved

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    server = app_server.build_codex_native_server(
        session_id="child-session",
        # A sub-agent turn in its parent's runner: the primary names the parent.
        primary_session_id="parent-session",
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        cwd=tmp_path,
        model=None,
        profile=None,
        bridge_dir=tmp_path / "bridge",
        codex_path="/test/codex",
        reconcile_process_registry=False,
    )

    with pytest.raises(SpawnObserved):
        await server.start()

    assert spawn_env[PRIMARY_SESSION_ID_ENV_VAR] == "parent-session"
    assert spawn_env["OMNIGENT_SESSION_ID"] == "child-session"
    assert not RUNNER_AUTH_SECRET_ENV_VARS & spawn_env.keys()
    assert "runner-secret" not in spawn_env.values()


@pytest.mark.parametrize("inherited_primary", [None, "outer-session"])
async def test_cli_owned_launch_names_its_own_session_as_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inherited_primary: str | None
) -> None:
    # `omnigent codex` with its own server owns a top-level session, even when run
    # from another session's shell that exports that session as the primary.
    if inherited_primary is None:
        monkeypatch.delenv(PRIMARY_SESSION_ID_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(PRIMARY_SESSION_ID_ENV_VAR, inherited_primary)
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.bridge._BRIDGE_ROOT", tmp_path / "codex-bridge"
    )
    monkeypatch.setattr(
        codex_native, "_create_codex_session", AsyncMock(return_value="cli-session")
    )
    monkeypatch.setattr(
        codex_native,
        "resolve_native_codex_launch",
        lambda **_kwargs: app_server.NativeCodexLaunch([], None, None),
    )

    class BuildObserved(Exception):
        pass

    def build(**kwargs: object) -> None:
        raise BuildObserved(app_server.build_codex_native_server(**kwargs))  # type: ignore[arg-type]

    monkeypatch.setattr(codex_native, "build_codex_native_server", build)

    with pytest.raises(BuildObserved) as observed:
        await codex_native._prepare_codex_terminal(
            base_url="http://test",
            headers={},
            session_id=None,
            runner_id=None,
            session_bundle=b"fake-bundle",
            codex_args=(),
            command="/test/codex",
            model=None,
        )

    env = cast("app_server.CodexNativeAppServer", observed.value.args[0]).env
    assert env[PRIMARY_SESSION_ID_ENV_VAR] == "cli-session"
    assert env["OMNIGENT_SESSION_ID"] == "cli-session"
