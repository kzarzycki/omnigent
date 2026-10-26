"""Each session in a runner applies its own server harness telemetry decision."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import omnigent.config as config
from omnigent.config import HARNESS_TELEMETRY_ENV_VAR, harness_telemetry_for
from omnigent.entities import Conversation
from omnigent.runner import create_runner_app
from omnigent.server.harness_telemetry import harness_telemetry_decision
from omnigent.server.runner_session_init import RunnerSessionInitializer
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient


class _NoTunnels:
    def get(self, _runner_id: str) -> None:
        return None


def _session(session_id: str, **labels: str) -> Conversation:
    return Conversation(
        id=session_id,
        created_at=1,
        updated_at=1,
        agent_id="agent_telemetry",
        runner_id="runner_parent",
        root_conversation_id="conv_parent",
        parent_conversation_id=None if session_id == "conv_parent" else "conv_parent",
        labels=labels,
    )


@pytest.mark.asyncio
async def test_a_co_located_sub_session_applies_its_own_decision_not_its_parents_runner_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child sharing its parent's runner gets no launch frame; its session-init decides."""
    server_config = tmp_path / "server.yaml"

    def exclude(**lists: list[str]) -> None:
        server_config.write_text(
            json.dumps(
                {
                    "harness_telemetry": {
                        "endpoint": "https://otlp.example.test",
                        "exclude": lists,
                    }
                }
            )
        )

    exclude(labels=["privacy=private"])
    monkeypatch.setenv("OMNIGENT_CONFIG", str(server_config))
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config, "_session_harness_telemetry", {})
    store = SimpleNamespace(
        get_session_owner=lambda _session_id: "member@example.test",
        set_labels=lambda _session_id, _labels: None,
    )
    parent = _session("conv_parent")
    # The host wrote the parent's launch-frame decision for the whole runner.
    runner_decision = tmp_path / "runner-decision.json"
    runner_decision.write_text(json.dumps(harness_telemetry_decision(parent, store)))
    monkeypatch.setenv(HARNESS_TELEMETRY_ENV_VAR, str(runner_decision))

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    initializer = RunnerSessionInitializer(
        _NoTunnels(),  # type: ignore[arg-type]
        server_version="0.6.0",
        conversation_store=store,  # type: ignore[arg-type]
    )

    async def initialize(session: Conversation, *, resume: bool = False) -> None:
        response = await initializer.initialize(
            session, client, timeout=10, resume_interrupted_turn=resume
        )
        assert response.status_code == 201, response.text

    def exports(session_id: str) -> bool:
        telemetry = harness_telemetry_for(tmp_path, session_id)
        return telemetry is not None and telemetry.enabled

    async with _runner_client(app) as client:
        await initialize(parent)
        await initialize(_session("conv_private", privacy="private"))
        await initialize(_session("conv_allowed"))
        assert exports("conv_parent") and exports("conv_allowed")
        assert not exports("conv_private")
        assert harness_telemetry_for(tmp_path, "conv_allowed").resource_attributes == {
            "omnigent.session.id": "conv_allowed",
            "omnigent.root_session.id": "conv_parent",
        }

        # A thread exclusion added since applies to the next child and a resumed one.
        exclude(threads=["conv_parent"])
        await initialize(_session("conv_later"))
        await initialize(_session("conv_allowed"), resume=True)
        assert not exports("conv_later") and not exports("conv_allowed")

    # A session this runner has no decision for never inherits the runner's.
    assert exports("conv_parent") and not exports("conv_unknown")


@pytest.mark.asyncio
async def test_the_runner_tells_the_server_whether_a_sessions_repository_is_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the host reads the checkout's origin; the server saves its answer per session."""
    import subprocess

    def checkout(name: str, origin: str) -> Path:
        repo = tmp_path / name
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", origin], check=True)
        return repo

    server_config = tmp_path / "server.yaml"
    server_config.write_text(
        "harness_telemetry:\n"
        "  endpoint: https://otlp.example.test\n"
        "  exclude:\n    repos: [git.example.test/team/private]\n"
    )
    monkeypatch.setenv("OMNIGENT_CONFIG", str(server_config))
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(HARNESS_TELEMETRY_ENV_VAR, raising=False)
    monkeypatch.setattr(config, "_session_harness_telemetry", {})
    saved: dict[str, dict[str, str]] = {}
    store = SimpleNamespace(
        get_session_owner=lambda _session_id: None,
        set_labels=lambda session_id, labels: saved.setdefault(session_id, {}).update(labels),
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    initializer = RunnerSessionInitializer(
        _NoTunnels(),  # type: ignore[arg-type]
        server_version="0.6.0",
        conversation_store=store,  # type: ignore[arg-type]
    )
    private = checkout("private", "git@git.example.test:team/private.git")
    public = checkout("public", "https://git.example.test/team/public.git")

    async with _runner_client(app) as client:
        for session_id, workspace in (("conv_private", private), ("conv_public", public)):
            session = _session(session_id)
            session.workspace = str(workspace)
            response = await initializer.initialize(session, client, timeout=10)
            assert response.status_code == 201, response.text

    assert saved == {
        "conv_private": {"omnigent.harness_telemetry": "off"},
        "conv_public": {"omnigent.harness_telemetry": "on"},
    }
