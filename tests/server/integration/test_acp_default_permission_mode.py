"""An ACP agent's default permission mode applies on every start path and caps requests."""

# ruff: noqa: F811  (pytest fixtures imported by name)

import pytest

from omnigent.acp_permission_mode import ACP_PERMISSION_MODE_LABEL_KEY
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runtime import get_conversation_store
from tests.server.helpers import create_test_agent
from tests.server.integration.test_acp_model_selection import (
    _CHILD_DEFAULT,
    _executor,
    _isolated_provider_config,  # noqa: F401  (autouse fixture)
)
from tests.server.integration.test_session_orchestration import (  # noqa: F401
    auth_app,
    orchestration_environment,
    peers,
    startup_permissions,
)

pytestmark = pytest.mark.asyncio

_PLACEMENTS = ["top-level", "child", "independent"]


async def _agent(auth_client, default_mode):
    executor = _executor()
    if default_mode is not None:
        executor["config"]["permission_mode"] = default_mode
    return await create_test_agent(auth_client, executor=executor, include_llm=False)


async def _create(auth_client, peers, monkeypatch, agent, placement, mode):
    from omnigent.server.routes.sessions import routes_core

    source, _, _ = peers

    async def skip_launch(**kwargs):
        pass

    monkeypatch.setattr(routes_core, "_run_managed_launch", skip_launch)
    body = {"agent_id": agent["id"], "initial_items": []}
    headers = {}
    path = "/v1/sessions"
    if mode is not None:
        body["permission_mode"] = mode
    if placement == "child":
        body["parent_session_id"] = source["id"]
    elif placement == "independent":
        path = "/v1/sessions/orchestrated"
        body["host_type"] = "managed"
        headers[ORCHESTRATION_SOURCE_HEADER] = source["id"]
    return await auth_client.post(path, json=body, headers=headers)


@pytest.mark.parametrize("placement", _PLACEMENTS)
@pytest.mark.parametrize("default_mode", [None, "auto", "bypassPermissions"])
async def test_a_session_without_a_mode_gets_the_agent_default(
    auth_client, peers, monkeypatch, placement, default_mode
):
    agent = await _agent(auth_client, default_mode)

    response = await _create(auth_client, peers, monkeypatch, agent, placement, None)

    assert response.status_code == 201, response.text
    created = get_conversation_store().get_conversation(response.json()["id"])
    assert created.labels.get(ACP_PERMISSION_MODE_LABEL_KEY) == default_mode


@pytest.mark.parametrize("default_mode", [None, "auto", "bypassPermissions"])
async def test_a_resumed_session_keeps_the_agent_default(
    auth_client, peers, monkeypatch, default_mode
):
    agent = await _agent(auth_client, default_mode)
    created = await _create(auth_client, peers, monkeypatch, agent, "top-level", None)
    assert created.status_code == 201, created.text

    forked = await auth_client.post(f"/v1/sessions/{created.json()['id']}/fork", json={})

    assert forked.status_code == 201, forked.text
    resumed = get_conversation_store().get_conversation(forked.json()["id"])
    assert resumed.labels.get(ACP_PERMISSION_MODE_LABEL_KEY) == default_mode


@pytest.mark.parametrize("default_mode", [None, "auto", "bypassPermissions"])
async def test_a_relaunched_runner_resolves_the_agent_default(
    auth_client, peers, monkeypatch, tmp_path, default_mode
):
    """A runner that comes back for an existing session reads the same default."""
    from omnigent.runner._entry import _AgentSpecCache, _resolve_agent_spec_from_server
    from omnigent.runner.app import _build_spawn_env_from_spec

    agent = await _agent(auth_client, default_mode)
    created = await _create(auth_client, peers, monkeypatch, agent, "top-level", None)
    assert created.status_code == 201, created.text

    resolved = await _resolve_agent_spec_from_server(
        auth_client, _AgentSpecCache(tmp_path), agent["id"], session_id=created.json()["id"]
    )

    assert resolved is not None
    env = _build_spawn_env_from_spec(resolved.spec, "acp:synthetic")
    assert env is not None
    assert env.get("HARNESS_ACP_PERMISSION_MODE") == default_mode


async def test_a_named_child_ceiling_is_its_own_default(client, monkeypatch):
    from omnigent.server.routes.sessions import routes_core

    sub_executor = {**_executor("worker"), "model": _CHILD_DEFAULT}
    sub_executor["config"] = {**sub_executor["config"], "permission_mode": "bypassPermissions"}
    agent = await create_test_agent(
        client,
        executor=_executor(),
        include_llm=False,
        sub_agents=[{"name": "worker", "executor": sub_executor}],
    )

    async def skip_launch(**kwargs):
        pass

    monkeypatch.setattr(routes_core, "_run_managed_launch", skip_launch)
    response = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "initial_items": [],
            "parent_session_id": agent["_session_id"],
            "sub_agent_name": "worker",
            "permission_mode": "bypassPermissions",
        },
    )

    assert response.status_code == 201, response.text
    created = get_conversation_store().get_conversation(response.json()["id"])
    assert created.labels[ACP_PERMISSION_MODE_LABEL_KEY] == "bypassPermissions"


@pytest.mark.parametrize("placement", _PLACEMENTS)
@pytest.mark.parametrize(
    ("default_mode", "requested", "accepted"),
    [
        (None, "bypassPermissions", False),
        (None, "auto", True),
        ("auto", "bypassPermissions", False),
        ("auto", "auto", True),
        ("bypassPermissions", "bypassPermissions", True),
        ("bypassPermissions", "auto", True),
    ],
)
async def test_a_session_can_only_tighten_the_agent_default(
    auth_client, peers, monkeypatch, placement, default_mode, requested, accepted
):
    agent = await _agent(auth_client, default_mode)

    response = await _create(auth_client, peers, monkeypatch, agent, placement, requested)

    if accepted:
        assert response.status_code == 201, response.text
        created = get_conversation_store().get_conversation(response.json()["id"])
        assert created.labels[ACP_PERMISSION_MODE_LABEL_KEY] == requested
    else:
        assert response.status_code == 400, response.text
        assert "bypassPermissions" in response.text
        assert "looser" in response.text or "skips approval" in response.text
