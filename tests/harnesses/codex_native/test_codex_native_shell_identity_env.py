"""The native Codex app-server, which runs the agent's shell, carries session identity."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest

from omnigent.debug_logging import PRIMARY_SESSION_ID_ENV_VAR
from omnigent.harnesses.codex_native import app_server
from omnigent.runner.identity import RUNNER_AUTH_SECRET_ENV_VARS


async def test_app_server_env_carries_session_identity_but_no_runner_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_home = tmp_path / "source-home"
    source_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    # A sub-agent turn in its parent's runner: the primary names the parent.
    monkeypatch.setenv(PRIMARY_SESSION_ID_ENV_VAR, "parent-session")
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
