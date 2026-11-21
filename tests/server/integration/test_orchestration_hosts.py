"""A host marked orchestration-only refuses local work sessions, except allowed agents."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from omnigent.errors import OmnigentError
from omnigent.server.orchestration_hosts import OrchestrationHosts, OrchestrationLaunchGuard
from tests.server.helpers import build_agent_bundle
from tests.server.integration.test_machine_session_access import (
    _NAMED_HOST,
    _NAMED_USER,
    _OTHER_HOST,
    _WORKSPACE,
    _connect_host,
)
from tests.server.integration.test_machine_session_access import agents as agents
from tests.server.integration.test_session_host_launch import _serve_one_launch
from tests.server.integration.test_sessions_permissions import (
    host_perm_app as host_perm_app,
)
from tests.server.integration.test_sessions_permissions import (
    host_perm_client as host_perm_client,
)

pytestmark = pytest.mark.asyncio


def _mark(app: FastAPI, hosts: set[str]) -> None:
    marks = OrchestrationHosts(hosts=frozenset(hosts), allowed_agents=frozenset({"listed-agent"}))
    app.state.orchestration_hosts = marks
    guard = OrchestrationLaunchGuard(
        marks, app.state.host_store, app.state.agent_store, app.state.agent_cache
    )
    app.state.host_registry.orchestration_launch_guard = guard
    app.state.host_registry.orchestration_host_check = guard.check


async def _create(client: httpx.AsyncClient, agent_id: str, host_id: str) -> httpx.Response:
    body = {"agent_id": agent_id, "host_id": host_id, "workspace": _WORKSPACE}
    return await client.post("/v1/sessions", json=body, headers={"X-Forwarded-Email": _NAMED_USER})


async def test_a_work_agent_is_refused_on_a_marked_host(
    host_perm_app: FastAPI, host_perm_client: httpx.AsyncClient, agents: dict[str, str]
) -> None:
    _mark(host_perm_app, {_NAMED_HOST})
    await _connect_host(host_perm_app, _NAMED_HOST, _NAMED_USER)

    response = await _create(host_perm_client, agents["unlisted"], _NAMED_HOST)

    assert response.status_code == 403, response.text
    assert "orchestration-only" in response.text and "cloud" in response.text
    assert host_perm_app.state.host_registry.get(_NAMED_HOST).outbound_queue.empty()


@pytest.mark.parametrize("agent_key", ["listed", "cloud"])
async def test_a_listed_or_cloud_agent_starts_on_a_marked_host(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
    agent_key: str,
) -> None:
    _mark(host_perm_app, {_NAMED_HOST})
    host = await _connect_host(host_perm_app, _NAMED_HOST, _NAMED_USER)
    request = asyncio.create_task(_create(host_perm_client, agents[agent_key], _NAMED_HOST))
    served = asyncio.create_task(_serve_one_launch(host, launch_status="launched"))
    await asyncio.wait({request, served}, return_when=asyncio.FIRST_COMPLETED)
    if request.done() and not served.done():
        served.cancel()
        pytest.fail(f"start was refused: {request.result().text}")
    await served

    assert (await request).status_code == 201


async def test_an_unmarked_host_behaves_as_before(
    host_perm_app: FastAPI, host_perm_client: httpx.AsyncClient, agents: dict[str, str]
) -> None:
    _mark(host_perm_app, {_OTHER_HOST})
    host = await _connect_host(host_perm_app, _NAMED_HOST, _NAMED_USER)
    request = asyncio.create_task(_create(host_perm_client, agents["unlisted"], _NAMED_HOST))
    served = asyncio.create_task(_serve_one_launch(host, launch_status="launched"))
    await asyncio.wait({request, served}, return_when=asyncio.FIRST_COMPLETED)
    if request.done() and not served.done():
        served.cancel()
        pytest.fail(f"start was refused: {request.result().text}")
    await served

    assert (await request).status_code == 201


def test_marks_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_ORCHESTRATION_HOSTS", raising=False)
    monkeypatch.delenv("OMNIGENT_ORCHESTRATION_ALLOWED_AGENTS", raising=False)
    assert OrchestrationHosts.from_env() is None
    monkeypatch.setenv("OMNIGENT_ORCHESTRATION_ALLOWED_AGENTS", "a")
    with pytest.raises(RuntimeError):
        OrchestrationHosts.from_env()
    monkeypatch.setenv("OMNIGENT_ORCHESTRATION_HOSTS", "host-a, laptop")
    monkeypatch.setenv("OMNIGENT_ORCHESTRATION_ALLOWED_AGENTS", "a, b")
    marks = OrchestrationHosts.from_env()
    assert marks == OrchestrationHosts(frozenset({"host-a", "laptop"}), frozenset({"a", "b"}))


async def test_binding_an_existing_work_session_to_a_marked_host_is_refused(
    host_perm_app: FastAPI, host_perm_client: httpx.AsyncClient, agents: dict[str, str]
) -> None:
    """The runner-launch route, which the CLI and web UI use, meets the mark too."""
    _mark(host_perm_app, {_NAMED_HOST})
    await _connect_host(host_perm_app, _NAMED_HOST, _NAMED_USER)
    headers = {"X-Forwarded-Email": _NAMED_USER}
    created = await host_perm_client.post(
        "/v1/sessions", json={"agent_id": agents["unlisted"]}, headers=headers
    )
    assert created.status_code == 201, created.text

    response = await host_perm_client.post(
        f"/v1/hosts/{_NAMED_HOST}/runners",
        json={"session_id": created.json()["id"], "workspace": _WORKSPACE},
        headers=headers,
    )

    assert response.status_code == 403, response.text
    assert "orchestration-only" in response.text
    assert host_perm_app.state.host_registry.get(_NAMED_HOST).outbound_queue.empty()


async def test_a_relaunch_on_a_marked_host_is_refused(
    host_perm_app: FastAPI, host_perm_client: httpx.AsyncClient, agents: dict[str, str]
) -> None:
    """A runner relaunched from the stored session row meets the mark."""
    from omnigent.server.routes._sessions.helpers import _launch_runner_on_host_impl

    _mark(host_perm_app, {_NAMED_HOST})
    await _connect_host(host_perm_app, _NAMED_HOST, _NAMED_USER)
    created = await host_perm_client.post(
        "/v1/sessions",
        json={"agent_id": agents["unlisted"]},
        headers={"X-Forwarded-Email": _NAMED_USER},
    )
    store = host_perm_app.state.conversation_store
    conv = store.get_conversation(created.json()["id"])
    registry = host_perm_app.state.host_registry

    with pytest.raises(OmnigentError, match="orchestration-only"):
        await _launch_runner_on_host_impl(conv, store, registry, registry.get(_NAMED_HOST))

    assert registry.get(_NAMED_HOST).outbound_queue.empty()


async def test_an_uploaded_agent_is_refused_on_a_marked_host(
    host_perm_app: FastAPI, host_perm_client: httpx.AsyncClient
) -> None:
    """An uploaded bundle is never an allow-listed agent, so its create is refused."""
    _mark(host_perm_app, {_NAMED_HOST})
    await _connect_host(host_perm_app, _NAMED_HOST, _NAMED_USER)
    metadata = {"host_id": _NAMED_HOST, "workspace": _WORKSPACE}
    bundle = build_agent_bundle(name="listed-agent")

    response = await host_perm_client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={
            "bundle": ("agent.tar.gz", build_agent_bundle(name="listed-agent"), "application/gzip")
        },
        headers={"X-Forwarded-Email": _NAMED_USER},
    )

    assert response.status_code == 403, response.text
    assert "orchestration-only" in response.text
    assert host_perm_app.state.host_registry.get(_NAMED_HOST).outbound_queue.empty()
