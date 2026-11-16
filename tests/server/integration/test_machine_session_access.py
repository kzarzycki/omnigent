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
from omnigent.errors import OmnigentError
from omnigent.host.frames import HostHelloFrame, HostLaunchRunnerFrame, encode_host_frame
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.machine_access import MachineSessionAccess
from omnigent.server.routes._machine_start import install_machine_launch_guard
from omnigent.spec import extract_safe
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.budgets import budget
from tests.server.helpers import build_agent_bundle
from tests.server.helpers import websocket_scope as _websocket_scope
from tests.server.integration.test_session_host_launch import _serve_one_launch
from tests.server.integration.test_sessions_permissions import build_host_perm_app
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
        "codex": _register_agent(
            host_perm_app,
            tmp_path,
            "codex-agent",
            {"type": "omnigent", "config": {"harness": "codex-native"}},
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
        allowed_agents=frozenset({"listed-agent", "bypass-agent", "codex-agent"}),
        allow_cloud_sessions=cloud,
    )
    install_machine_launch_guard(app)


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


async def _start_host_session(
    app: FastAPI, client: httpx.AsyncClient, agent_id: str
) -> tuple[str, object]:
    """Start a listed session on the named user's host; return its id and the host tunnel."""
    host = await _connect_host(app, _NAMED_HOST, _NAMED_USER)
    request = asyncio.create_task(_create_as_machine(client, agent_id))
    await _serve_one_launch(host, launch_status="launched")
    response = await request
    assert response.status_code == 201, response.text
    return response.json()["id"], host


@pytest.mark.parametrize(
    ("event_type", "data"),
    [
        (
            "external_permission_mode_change",
            {"permission_mode": "bypassPermissions", "initial_observation": False},
        ),
        ("external_codex_approval_mode_change", {"approval_mode": "never", "sandbox": "danger"}),
    ],
)
async def test_machine_cannot_arm_bypass_through_native_setting_events(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
    event_type: str,
    data: dict[str, object],
) -> None:
    """The events that rewrite stored launch args are refused for the machine principal."""
    _grant(host_perm_app)
    session_id, _host = await _start_host_session(
        host_perm_app, host_perm_client, agents["listed"]
    )
    before = host_perm_app.state.conversation_store.get_conversation(session_id)
    response = await host_perm_client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": event_type, "data": data},
        headers={"X-Forwarded-Email": _MACHINE},
    )
    assert response.status_code == 403, response.text
    after = host_perm_app.state.conversation_store.get_conversation(session_id)
    assert after.terminal_launch_args == before.terminal_launch_args


async def test_runner_launch_refuses_a_machine_session_with_bypass_args_stored(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
) -> None:
    """Whatever wrote bypass into the stored row, a relaunch of a machine session refuses it."""
    from omnigent.server.routes._sessions.orchestration import ensure_runner_connected

    _grant(host_perm_app)
    session_id, _host = await _start_host_session(
        host_perm_app, host_perm_client, agents["listed"]
    )
    store = host_perm_app.state.conversation_store
    store.update_conversation(
        session_id, terminal_launch_args=["--permission-mode", "bypassPermissions"]
    )
    queue = host_perm_app.state.host_registry.get(_NAMED_HOST).outbound_queue
    while not queue.empty():
        queue.get_nowait()
    with pytest.raises(OmnigentError, match="skips permission prompts"):
        await ensure_runner_connected(
            session_id=session_id,
            conv=store.get_conversation(session_id),
            app_state=host_perm_app.state,
            conversation_store=store,
            runner_router=None,
            raise_host_refusal=True,
        )
    assert queue.empty()


async def test_runner_launch_refuses_a_machine_session_whose_agent_is_no_longer_listed(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
) -> None:
    """The relaunch re-checks the agent against the grant, not just the launch args."""
    from omnigent.server.routes._sessions.orchestration import ensure_runner_connected

    _grant(host_perm_app)
    session_id, _host = await _start_host_session(
        host_perm_app, host_perm_client, agents["listed"]
    )
    host_perm_app.state.machine_session_access = MachineSessionAccess(
        _MACHINE, _NAMED_USER, frozenset({"something-else"}), False
    )
    install_machine_launch_guard(host_perm_app)
    store = host_perm_app.state.conversation_store
    with pytest.raises(OmnigentError, match="start this agent"):
        await ensure_runner_connected(
            session_id=session_id,
            conv=store.get_conversation(session_id),
            app_state=host_perm_app.state,
            conversation_store=store,
            runner_router=None,
            raise_host_refusal=True,
        )


async def _refused_quickly(call) -> httpx.Response:
    """Await *call*; a request that gets past the guard hangs at the runner, so cap it."""
    try:
        return await asyncio.wait_for(call, timeout=budget(5.0))
    except TimeoutError:
        pytest.fail("request was not refused for the machine principal (reached the runner)")


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        (
            "POST",
            "/resources/terminals",
            {"json": {"terminal": "claude", "session_key": "main", "bridge_inject_dir": True}},
        ),
        ("POST", "/resources/terminals/terminal_claude_main/transfer", {"json": {}}),
        ("POST", "/resources/environments/env_1/shell", {"json": {"command": "id"}}),
        (
            "PUT",
            "/agent",
            {"files": {"bundle": ("a.tar.gz", b"x", "application/gzip")}},
        ),
        (
            "POST",
            "/agent/mcp-servers",
            {"json": {"name": "x", "transport": "stdio", "command": "id"}},
        ),
        (
            "PUT",
            "/agent/mcp-servers/x",
            {"json": {"name": "x", "transport": "stdio", "command": "id"}},
        ),
        ("DELETE", "/agent/mcp-servers/x", {}),
    ],
    ids=[
        "terminal-create",
        "terminal-transfer",
        "env-shell",
        "agent-bundle",
        "mcp-add",
        "mcp-put",
        "mcp-delete",
    ],
)
async def test_machine_refused_for_routes_that_run_or_type_on_the_host(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
    method: str,
    path: str,
    kwargs: dict[str, object],
) -> None:
    """Owner-level routes that start a process on the host, or alter what starts one."""
    _grant(host_perm_app)
    created = await _create_as_machine(host_perm_client, agents["listed"], None)
    assert created.status_code == 201, created.text
    response = await _refused_quickly(
        host_perm_client.request(
            method,
            f"/v1/sessions/{created.json()['id']}{path}",
            headers={"X-Forwarded-Email": _MACHINE},
            **kwargs,
        )
    )
    assert response.status_code == 403, response.text
    assert "machine client may not" in response.text


_OWNER_ONLY_REQUESTS = [
    (
        "post",
        "/v1/sessions/{sid}/events",
        {
            "type": "external_permission_mode_change",
            "data": {"permission_mode": "bypassPermissions", "initial_observation": False},
        },
    ),
    ("post", "/v1/sessions/{sid}/events", {"type": "slash_command", "data": {}}),
    (
        "post",
        "/v1/sessions/{sid}/resources/terminals",
        {"terminal": "bash", "session_key": "main"},
    ),
]


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    _OWNER_ONLY_REQUESTS,
    ids=["permission-mode-event", "slash-command", "terminal"],
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
    """The machine principal owns its session but cannot open terminals or change modes."""
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


_CODEX_BYPASS_LABEL = "omnigent.codex_native.bypass_sandbox"


async def test_machine_runner_launch_refuses_the_codex_bypass_label(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
) -> None:
    """Codex full bypass is a label with empty launch args; the launch route still refuses it."""
    created = await _create_as_machine(host_perm_client, agents["listed"], None)
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    host_perm_app.state.conversation_store.update_conversation_with_changes(
        session_id, label_updates={_CODEX_BYPASS_LABEL: "1"}
    )
    _grant(host_perm_app)
    await _connect_host(host_perm_app, _NAMED_HOST, _NAMED_USER)
    response = await host_perm_client.post(
        f"/v1/hosts/{_NAMED_HOST}/runners",
        json={"session_id": session_id, "workspace": _WORKSPACE},
        headers={"X-Forwarded-Email": _MACHINE},
    )
    assert response.status_code == 403, response.text
    assert "skips permission prompts" in response.text
    assert host_perm_app.state.host_registry.get(_NAMED_HOST).outbound_queue.empty()


async def test_runner_launch_guard_refuses_a_machine_session_with_the_codex_bypass_label(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
) -> None:
    """Whatever stamped the label, a relaunch of a machine-owned session refuses it."""
    from omnigent.server.routes._sessions.orchestration import ensure_runner_connected

    _grant(host_perm_app)
    session_id, _host = await _start_host_session(
        host_perm_app, host_perm_client, agents["listed"]
    )
    store = host_perm_app.state.conversation_store
    store.update_conversation_with_changes(session_id, label_updates={_CODEX_BYPASS_LABEL: "1"})
    with pytest.raises(OmnigentError, match="skips permission prompts"):
        await ensure_runner_connected(
            session_id=session_id,
            conv=store.get_conversation(session_id),
            app_state=host_perm_app.state,
            conversation_store=store,
            runner_router=None,
            raise_host_refusal=True,
        )


async def test_machine_cannot_fork_with_codex_bypass(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
) -> None:
    """A fork's codex bypass opt-in is refused for the machine principal before any stamp."""
    _grant(host_perm_app)
    created = await _create_as_machine(host_perm_client, agents["listed"], None)
    assert created.status_code == 201, created.text
    response = await host_perm_client.post(
        f"/v1/sessions/{created.json()['id']}/fork",
        json={"codex_bypass_sandbox": True},
        headers={"X-Forwarded-Email": _MACHINE},
    )
    assert response.status_code == 403, response.text
    assert "machine client may not" in response.text


@pytest.fixture()
def host_perm_app(db_uri: str, tmp_path: Path) -> FastAPI:
    """The host-enabled app, with the scheduled-task routes mounted by ``create_app``."""
    from omnigent.server.auth import create_auth_provider
    from omnigent.server.routes.scheduled_tasks import create_scheduled_tasks_router
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
    from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    router = create_scheduled_tasks_router(
        SqlAlchemyScheduledTaskStore(db_uri),
        agent_store=SqlAlchemyAgentStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        auth_provider=create_auth_provider(),
    )
    return build_host_perm_app(db_uri, tmp_path, extra_routers=[(router, "/v1", [])])


@pytest.mark.parametrize("mode", ["bypass", "bypassPermissions"])
async def test_machine_cannot_schedule_a_bypass_permission_mode(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
    mode: str,
) -> None:
    """A scheduled task's bypass mode, which stamps the codex label on fire, is refused."""
    _grant(host_perm_app)
    response = await host_perm_client.post(
        "/v1/scheduled-tasks",
        json={
            "name": "nightly",
            "prompt": "run",
            "rrule": "FREQ=DAILY",
            "agent_id": agents["listed"],
            "permission_mode": mode,
        },
        headers={"X-Forwarded-Email": _MACHINE},
    )
    assert response.status_code == 403, response.text
    assert "machine client may not" in response.text


async def _machine_session_in_native_harness(
    app: FastAPI, client: httpx.AsyncClient, agent_id: str, harness: str
) -> str:
    """Create a machine session and pin its terminal harness, as a runner-reported one is."""
    _grant(app)
    created = await _create_as_machine(client, agent_id, None)
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    app.state.conversation_store.update_conversation(session_id, harness_override=harness)
    return session_id


@pytest.mark.parametrize(
    ("harness", "content"),
    [
        ("codex-native", "!id"),
        ("claude-native", "!id"),
        ("claude-native", "  /compact"),
        ("claude-native", [{"type": "input_text", "text": "/login"}]),
        (
            "codex-native",
            [{"type": "message", "role": "user", "content": "!printf from-machine"}],
        ),
        (
            "claude-native",
            [{"type": "output_text", "text": "hi"}, {"type": "input_text", "text": "/login"}],
        ),
        (
            "claude-native",
            [{"type": "input_text", "text": ""}, {"type": "input_text", "text": "!id"}],
        ),
    ],
    ids=[
        "codex-shell",
        "claude-bash-mode",
        "claude-slash-command",
        "claude-slash-block",
        "codex-role-keyed-item",
        "claude-output-text-block",
        "claude-empty-first-block",
    ],
)
async def test_machine_message_that_a_native_terminal_would_run_is_refused(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
    harness: str,
    content: object,
) -> None:
    """A leading ! or / is a shell escape or slash command in a native TUI, so it is refused."""
    session_id = await _machine_session_in_native_harness(
        host_perm_app, host_perm_client, agents["listed"], harness
    )
    response = await _refused_quickly(
        host_perm_client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "message", "data": {"content": content}},
            headers={"X-Forwarded-Email": _MACHINE},
        )
    )
    assert response.status_code == 403, response.text
    assert "machine client may not" in response.text


async def test_machine_startup_input_with_a_shell_escape_is_refused(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
) -> None:
    """The refusal keys on the identity, so a startup-input dispatch is refused too."""
    session_id = await _machine_session_in_native_harness(
        host_perm_app, host_perm_client, agents["listed"], "codex-native"
    )
    with pytest.raises(OmnigentError, match="machine client may not"):
        await host_perm_app.state.post_server_message(session_id, _MACHINE, "!id")


async def test_machine_startup_input_with_leading_newline_shell_escape_is_refused(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
) -> None:
    """The startup dispatch applies the same text rule, ignoring leading newlines."""
    session_id = await _machine_session_in_native_harness(
        host_perm_app, host_perm_client, agents["listed"], "claude-native"
    )
    with pytest.raises(OmnigentError, match="machine client may not"):
        await host_perm_app.state.post_server_message(session_id, _MACHINE, "\n\n!id")


async def test_machine_cannot_patch_a_native_scheduled_task_to_bypass(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
) -> None:
    """A task created without a mode must not be switched to bypass by a later PATCH."""
    _grant(host_perm_app)
    headers = {"X-Forwarded-Email": _MACHINE}
    created = await host_perm_client.post(
        "/v1/scheduled-tasks",
        json={
            "name": "nightly",
            "prompt": "run",
            "rrule": "FREQ=DAILY",
            "agent_id": agents["codex"],
        },
        headers=headers,
    )
    assert created.status_code in (200, 201), created.text
    response = await host_perm_client.patch(
        f"/v1/scheduled-tasks/{created.json()['id']}",
        json={"permission_mode": "bypass"},
        headers=headers,
    )
    assert response.status_code == 403, response.text
    assert "machine client may not" in response.text


async def test_machine_cannot_pick_or_unpick_a_registry_mcp_service(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
) -> None:
    """The registry select and un-select paths return before the stdio guard, so they check too."""
    _grant(host_perm_app)
    created = await _create_as_machine(host_perm_client, agents["listed"], None)
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    headers = {"X-Forwarded-Email": _MACHINE}
    registry_body = {"name": "svc", "transport": "registry"}
    for method, path, kwargs in (
        ("POST", "/agent/mcp-servers", {"json": registry_body}),
        ("PUT", "/agent/mcp-servers/svc", {"json": registry_body}),
    ):
        response = await _refused_quickly(
            host_perm_client.request(
                method, f"/v1/sessions/{session_id}{path}", headers=headers, **kwargs
            )
        )
        assert response.status_code == 403, response.text
        assert "machine client may not" in response.text
    host_perm_app.state.conversation_store.update_conversation_with_changes(
        session_id, label_updates={"omnigent.mcp_service.svc": "1"}
    )
    response = await _refused_quickly(
        host_perm_client.delete(
            f"/v1/sessions/{session_id}/agent/mcp-servers/svc", headers=headers
        )
    )
    assert response.status_code == 403, response.text
    assert "machine client may not" in response.text


async def test_machine_can_answer_approvals_and_elicitations_of_its_session(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
) -> None:
    """The machine principal may answer prompts in a session it started."""
    _grant(host_perm_app)
    created = await _create_as_machine(host_perm_client, agents["listed"], None)
    assert created.status_code == 201, created.text
    base = f"/v1/sessions/{created.json()['id']}"
    headers = {"X-Forwarded-Email": _MACHINE}
    resolved = await host_perm_client.post(
        f"{base}/elicitations/abc/resolve", json={"action": "accept"}, headers=headers
    )
    assert resolved.status_code == 202, resolved.text
    fetched = await host_perm_client.get(f"{base}/elicitations/abc", headers=headers)
    assert fetched.status_code == 200, fetched.text
    approval = await host_perm_client.post(
        f"{base}/events", json={"type": "approval", "data": {}}, headers=headers
    )
    assert approval.status_code == 202, approval.text
    assert approval.json() == {"queued": False}


async def test_machine_gets_not_found_for_prompts_in_another_users_session(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    agents: dict[str, str],
) -> None:
    """The prompt routes stay scoped to sessions the machine principal can access."""
    _grant(host_perm_app)
    created = await host_perm_client.post(
        "/v1/sessions",
        json={"agent_id": agents["listed"]},
        headers={"X-Forwarded-Email": _OTHER_USER},
    )
    assert created.status_code == 201, created.text
    base = f"/v1/sessions/{created.json()['id']}"
    headers = {"X-Forwarded-Email": _MACHINE}
    responses = [
        await host_perm_client.post(
            f"{base}/events", json={"type": "approval", "data": {}}, headers=headers
        ),
        await host_perm_client.post(
            f"{base}/elicitations/abc/resolve", json={"action": "accept"}, headers=headers
        ),
        await host_perm_client.get(f"{base}/elicitations/abc", headers=headers),
    ]
    assert [response.status_code for response in responses] == [404, 404, 404], [
        response.text for response in responses
    ]
