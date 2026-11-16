"""The machine principal's grant to start sessions on one user's hosts.

Every case goes through ``POST /v1/sessions`` (and the runner-launch route)
with a distinct machine identity, the named user's host connected over a real
tunnel, and operator-registered agents.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from omnigent.cli import _preregister_agent
from omnigent.host.frames import HostHelloFrame, HostLaunchRunnerFrame, encode_host_frame
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.machine_access import MachineSessionAccess
from omnigent.spec import extract_safe
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.budgets import budget
from tests.server.helpers import build_agent_bundle
from tests.server.helpers import websocket_scope as _websocket_scope
from tests.server.integration.test_session_host_launch import _serve_one_launch
from tests.server.integration.test_sessions_permissions import host_perm_app as host_perm_app
from tests.server.integration.test_sessions_permissions import (
    host_perm_client as host_perm_client,
)

pytestmark = pytest.mark.asyncio

_MACHINE = "machine@example.test"
_NAMED_USER = "alice@example.test"
_OTHER_USER = "bob@example.test"
_NAMED_HOST = "f54bb9272002938a3a934bfcb6bb228a"
_OTHER_HOST = "a54bb9272002938a3a934bfcb6bb228b"
_WORKSPACE = "/work/repo"


def _register_agent(app: FastAPI, tmp_path: Path, name: str, executor: dict | None) -> str:
    """Register an operator-authored agent, as ``server --agent`` does."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    cache = AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache")
    with tempfile.TemporaryDirectory() as workdir:
        root = extract_safe(build_agent_bundle(name=name, executor=executor), Path(workdir))
        agent_id = _preregister_agent(root, app.state.agent_store, artifact_store, cache)
    assert agent_id is not None
    return agent_id


@pytest.fixture()
def agents(host_perm_app: FastAPI, tmp_path: Path) -> dict[str, str]:
    """Operator agents: a listed one, an unlisted one, a cloud one, a bypass one."""
    return {
        "listed": _register_agent(host_perm_app, tmp_path, "listed-agent", None),
        "unlisted": _register_agent(host_perm_app, tmp_path, "unlisted-agent", None),
        "cloud": _register_agent(
            host_perm_app,
            tmp_path,
            "cloud-agent",
            {"type": "omnigent", "config": {"harness": "acp:claude-cloud"}},
        ),
        "bypass": _register_agent(
            host_perm_app,
            tmp_path,
            "bypass-agent",
            {
                "type": "omnigent",
                "config": {"harness": "claude-native", "permission_mode": "bypassPermissions"},
            },
        ),
    }


def _grant(app: FastAPI, *, cloud: bool = True) -> None:
    app.state.machine_session_access = MachineSessionAccess(
        machine_sub=_MACHINE,
        acts_for=_NAMED_USER,
        allowed_agents=frozenset({"listed-agent", "bypass-agent"}),
        allow_cloud_sessions=cloud,
    )


async def _connect_host(app: FastAPI, host_id: str, owner: str):
    """Connect a host over the tunnel as *owner* and wait for registration."""
    from asgiref.testing import ApplicationCommunicator

    scope = _websocket_scope(f"/v1/hosts/{host_id}/tunnel")
    scope["headers"] = [(b"x-forwarded-email", owner.encode())]
    comm = ApplicationCommunicator(app, scope)
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=budget(1.0))
    assert accepted["type"] == "websocket.accept"
    hello = encode_host_frame(
        HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="laptop", runners=[])
    )
    await comm.send_input({"type": "websocket.receive", "text": hello})
    # The tunnel ends when its communicator is collected, so the app holds it.
    app.state.test_host_tunnels = [*getattr(app.state, "test_host_tunnels", []), comm]
    while app.state.host_registry.get(host_id) is None:
        await asyncio.sleep(0.01)
    return comm


async def _create_as_machine(
    client: httpx.AsyncClient, agent_id: str, host_id: str | None = _NAMED_HOST, **extra: object
) -> httpx.Response:
    body: dict[str, object] = {"agent_id": agent_id, **extra}
    if host_id is not None:
        body.update(host_id=host_id, workspace=_WORKSPACE)
    return await client.post("/v1/sessions", json=body, headers={"X-Forwarded-Email": _MACHINE})


@pytest.mark.parametrize("agent_key", ["listed", "cloud"])
async def test_machine_starts_listed_and_cloud_sessions_on_named_users_host(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
    agent_key: str,
) -> None:
    """The grant lets the machine principal launch on the named user's host."""
    _grant(host_perm_app)
    host = await _connect_host(host_perm_app, _NAMED_HOST, _NAMED_USER)
    request = asyncio.create_task(_create_as_machine(host_perm_client, agents[agent_key]))
    served = asyncio.create_task(_serve_one_launch(host, launch_status="launched"))
    # The request ends first when it is refused, so a refusal fails here with its text.
    await asyncio.wait({request, served}, return_when=asyncio.FIRST_COMPLETED)
    if request.done() and not served.done():
        served.cancel()
        pytest.fail(f"start was refused: {request.result().text}")
    launch = await served
    response = await request
    assert response.status_code == 201, response.text
    assert isinstance(launch, HostLaunchRunnerFrame)
    assert response.json()["host_id"] == _NAMED_HOST


@pytest.mark.parametrize(
    ("agent_key", "detail"),
    [
        ("unlisted", "start this agent"),
        ("bypass", "skips permission prompts"),
    ],
)
async def test_machine_refused_for_unlisted_agent_and_bypass_terminal(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
    agent_key: str,
    detail: str,
) -> None:
    """An unlisted agent, and a listed one that starts in bypass, never reach the host."""
    _grant(host_perm_app)
    await _connect_host(host_perm_app, _NAMED_HOST, _NAMED_USER)
    response = await _create_as_machine(host_perm_client, agents[agent_key])
    assert response.status_code == 403, response.text
    assert detail in json.dumps(response.json())
    assert host_perm_app.state.host_registry.get(_NAMED_HOST).outbound_queue.empty()


async def test_machine_refused_for_another_users_host(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
) -> None:
    """The grant covers only the named user's hosts."""
    _grant(host_perm_app)
    await _connect_host(host_perm_app, _OTHER_HOST, _OTHER_USER)
    response = await _create_as_machine(host_perm_client, agents["listed"], _OTHER_HOST)
    assert response.status_code == 403, response.text
    assert response.json().get("detail") == "not your host"
    assert host_perm_app.state.host_registry.get(_OTHER_HOST).outbound_queue.empty()


async def test_machine_without_the_setting_still_gets_not_your_host(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
) -> None:
    """Unset, behaviour is today's: the machine principal owns no host."""
    assert getattr(host_perm_app.state, "machine_session_access", None) is None
    await _connect_host(host_perm_app, _NAMED_HOST, _NAMED_USER)
    response = await _create_as_machine(host_perm_client, agents["listed"])
    assert response.status_code == 403, response.text
    assert response.json().get("detail") == "not your host"


@pytest.mark.parametrize(
    "extra",
    [
        {"terminal_launch_args": ["--dangerously-skip-permissions"]},
        {"permission_mode": "bypassPermissions"},
        {"harness_override": "claude-native"},
        {"labels": {"x": "y"}},
    ],
    ids=["launch-args", "permission-mode", "harness-override", "labels"],
)
async def test_machine_refused_for_fields_beyond_a_plain_start(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
    extra: dict[str, object],
) -> None:
    """Launch wiring a plain start does not need is refused, not interpreted."""
    _grant(host_perm_app)
    await _connect_host(host_perm_app, _NAMED_HOST, _NAMED_USER)
    response = await _create_as_machine(host_perm_client, agents["listed"], **extra)
    assert response.status_code == 403, response.text
    assert host_perm_app.state.host_registry.get(_NAMED_HOST).outbound_queue.empty()


async def test_machine_refused_for_an_uploaded_agent_bundle(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
) -> None:
    """An uploaded bundle is an arbitrary agent, so it is never a listed one."""
    _grant(host_perm_app)
    response = await host_perm_client.post(
        "/v1/sessions",
        data={"metadata": "{}"},
        files={
            "bundle": (
                "agent.tar.gz",
                build_agent_bundle(name="listed-agent"),
                "application/gzip",
            )
        },
        headers={"X-Forwarded-Email": _MACHINE},
    )
    assert response.status_code == 403, response.text


async def test_machine_refused_for_a_user_agent_that_takes_a_listed_name(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
) -> None:
    """Only operator-registered agents match the list, not ones a user owns."""
    _grant(host_perm_app)
    listed = host_perm_app.state.agent_store.get(agents["listed"])
    host_perm_app.state.agent_store.create_user_agent(
        "e1d81b1a6812ae1cf32221c5a2a70ba0", "bypass-agent-copy", listed.bundle_location, _MACHINE
    )
    host_perm_app.state.machine_session_access = MachineSessionAccess(
        _MACHINE, _NAMED_USER, frozenset({"bypass-agent-copy"}), False
    )
    response = await _create_as_machine(host_perm_client, "e1d81b1a6812ae1cf32221c5a2a70ba0", None)
    assert response.status_code == 403, response.text


async def test_machine_runner_launch_refuses_stored_bypass_launch_args(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
) -> None:
    """A session made before the grant with bypass launch args is not bound to the host."""
    created = await _create_as_machine(
        host_perm_client,
        agents["listed"],
        None,
        terminal_launch_args=["--dangerously-skip-permissions"],
    )
    assert created.status_code == 201, created.text
    _grant(host_perm_app)
    await _connect_host(host_perm_app, _NAMED_HOST, _NAMED_USER)
    response = await host_perm_client.post(
        f"/v1/hosts/{_NAMED_HOST}/runners",
        json={"session_id": created.json()["id"], "workspace": _WORKSPACE},
        headers={"X-Forwarded-Email": _MACHINE},
    )
    assert response.status_code == 403, response.text
    assert "skips permission prompts" in response.text
    assert host_perm_app.state.host_registry.get(_NAMED_HOST).outbound_queue.empty()


async def test_machine_cannot_switch_its_session_to_bypass_afterwards(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
) -> None:
    """A session the machine owns keeps its launch args: PATCH cannot arm bypass."""
    _grant(host_perm_app)
    created = await _create_as_machine(host_perm_client, agents["listed"], None)
    assert created.status_code == 201, created.text
    patched = await host_perm_client.patch(
        f"/v1/sessions/{created.json()['id']}",
        json={"terminal_launch_args": ["--dangerously-skip-permissions"]},
        headers={"X-Forwarded-Email": _MACHINE},
    )
    assert patched.status_code == 403, patched.text


async def test_machine_runner_launch_rechecks_the_sessions_agent(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
) -> None:
    """A session made before the grant cannot be bound to the host through the runners route."""
    created = await _create_as_machine(host_perm_client, agents["unlisted"], None)
    assert created.status_code == 201, created.text
    _grant(host_perm_app)
    await _connect_host(host_perm_app, _NAMED_HOST, _NAMED_USER)
    response = await host_perm_client.post(
        f"/v1/hosts/{_NAMED_HOST}/runners",
        json={"session_id": created.json()["id"], "workspace": _WORKSPACE},
        headers={"X-Forwarded-Email": _MACHINE},
    )
    assert response.status_code == 403, response.text
    assert host_perm_app.state.host_registry.get(_NAMED_HOST).outbound_queue.empty()


_OWNER_ONLY_REQUESTS = [
    (
        "post",
        "/v1/sessions/{sid}/events",
        {
            "type": "external_permission_mode_change",
            "data": {"permission_mode": "bypassPermissions", "initial_observation": False},
        },
    ),
    ("post", "/v1/sessions/{sid}/events", {"type": "approval", "data": {}}),
    ("post", "/v1/sessions/{sid}/events", {"type": "slash_command", "data": {}}),
    ("post", "/v1/sessions/{sid}/elicitations/abc/resolve", {"action": "accept"}),
    (
        "post",
        "/v1/sessions/{sid}/resources/terminals",
        {"terminal": "bash", "session_key": "main"},
    ),
]


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    _OWNER_ONLY_REQUESTS,
    ids=["permission-mode-event", "approval-event", "slash-command", "elicitation", "terminal"],
)
async def test_machine_cannot_drive_approvals_modes_or_terminals_of_its_session(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
    db_uri: str,
    method: str,
    path: str,
    payload: dict[str, object],
) -> None:
    """The machine principal owns its session but cannot answer approvals or open terminals."""
    _grant(host_perm_app)
    created = await _create_as_machine(host_perm_client, agents["listed"], None)
    assert created.status_code == 201, created.text
    response = await host_perm_client.request(
        method,
        path.format(sid=created.json()["id"]),
        json=payload,
        headers={"X-Forwarded-Email": _MACHINE},
    )
    assert response.status_code == 403, response.text
    assert "machine client may not" in response.text
    stored = SqlAlchemyConversationStore(db_uri).get_conversation(created.json()["id"])
    assert stored is not None and not stored.terminal_launch_args


async def test_machine_cannot_attach_to_a_terminal_for_writing(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
) -> None:
    """An interactive attach is refused for the machine principal before it is accepted."""
    from asgiref.testing import ApplicationCommunicator

    _grant(host_perm_app)
    created = await _create_as_machine(host_perm_client, agents["listed"], None)
    sid = created.json()["id"]
    scope = _websocket_scope(f"/v1/sessions/{sid}/resources/terminals/terminal_x/attach")
    scope["headers"] = [(b"x-forwarded-email", _MACHINE.encode())]
    comm = ApplicationCommunicator(host_perm_app, scope)
    await comm.send_input({"type": "websocket.connect"})
    output = await comm.receive_output(timeout=budget(2.0))
    assert output["type"] == "websocket.close"
