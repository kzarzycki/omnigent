"""An orchestrated create can start on the host and workspace a project pins."""

from uuid import uuid4

import pytest
from fastapi import FastAPI

from omnigent.host.frames import HostLaunchRunnerFrame, HostStatFrame, decode_host_frame
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.integration.test_session_orchestration_projects import _project, auth_app
from tests.server.integration.test_sessions_attribution import _bind_runner
from tests.server.integration.test_sessions_harness_override import _stub_late_runner_bind
from tests.server.integration.test_sessions_permissions import (
    _create_session_as,
    _register_online_host,
)

pytestmark = pytest.mark.asyncio

__all__ = ["auth_app"]

PINNED_WORKSPACE = "/pinned/launch"
ENVIRONMENT_ID = "env_fixture"


@pytest.fixture(autouse=True, params=[False, True], ids=["standalone", "nested"])
def orchestration_environment(monkeypatch, request):
    monkeypatch.setenv(
        "OMNIGENT_FEATURES", "sibling_sessions" if request.param else "-sibling_sessions"
    )
    return request.param


@pytest.fixture
async def stack(auth_client, auth_app: FastAPI, db_uri, monkeypatch):
    """A sender on one host and a second, online host a project can pin."""
    auth_client.headers["X-Forwarded-Email"] = "alice"
    source = await _create_session_as(auth_client, "", "alice")
    sender_host, pinned_host = uuid4().hex, uuid4().hex
    for host_id in (sender_host, pinned_host):
        _register_online_host(auth_app, host_id, "alice")
        auth_app.state.host_store.upsert_on_connect(host_id, host_id, "alice")
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(source["id"], sender_host, workspace="/sender/checkout")
    auth_client.headers.update(_bind_runner(db_uri, source["id"]))
    _stub_late_runner_bind(monkeypatch)

    def answer_host(connection, payload):
        frame = decode_host_frame(payload)
        if isinstance(frame, HostStatFrame):
            connection.pending_stats[frame.request_id].set_result(
                {"status": "ok", "exists": True, "type": "directory", "canonical_path": frame.path}
            )
        elif isinstance(frame, HostLaunchRunnerFrame):
            connection.pending_launches[frame.request_id].set_result({"status": "launched"})

    monkeypatch.setattr(auth_app.state.host_registry, "send_text", answer_host)
    return source, pinned_host, store


async def _orchestrate(auth_client, source, **fields):
    return await auth_client.post(
        "/v1/sessions/orchestrated",
        headers={ORCHESTRATION_SOURCE_HEADER: source["id"]},
        json={"agent_id": source["agent_id"], **fields},
    )


@pytest.mark.parametrize("named_by", ["project_id", "environment_id"])
async def test_create_takes_host_and_workspace_from_the_named_project(
    auth_client, stack, named_by
):
    source, pinned_host, store = stack
    project_id = await _project(
        auth_client,
        "Cloud",
        {"host_id": pinned_host, "workspace": PINNED_WORKSPACE, "environment_id": ENVIRONMENT_ID},
    )
    named = (
        {"project_id": project_id}
        if named_by == "project_id"
        else {"environment_id": ENVIRONMENT_ID}
    )

    response = await _orchestrate(auth_client, source, **named)

    assert response.status_code == 201, response.text
    created = store.get_conversation(response.json()["id"])
    assert (created.host_id, created.workspace) == (pinned_host, PINNED_WORKSPACE)
    assert created.project_id == project_id


@pytest.mark.parametrize(
    ("fields", "status"),
    [
        ({"project_id": "missing"}, 404),
        ({"environment_id": "env_unknown"}, 404),
        ({"project_id": "x", "environment_id": ENVIRONMENT_ID}, 400),
    ],
)
async def test_unknown_project_or_environment_never_falls_back_to_the_sender(
    auth_client, stack, db_uri, fields, status
):
    source, pinned_host, store = stack
    await _project(
        auth_client,
        "Cloud",
        {"host_id": pinned_host, "workspace": PINNED_WORKSPACE, "environment_id": ENVIRONMENT_ID},
    )
    before = {c.id for c in store.list_conversations().data}

    response = await _orchestrate(auth_client, source, **fields)

    assert response.status_code == status, response.text
    assert {c.id for c in store.list_conversations().data} == before


async def test_project_without_a_host_and_workspace_is_refused(auth_client, stack):
    source, _, _ = stack
    project_id = await _project(auth_client, "Bare", {"environment_id": ENVIRONMENT_ID})

    response = await _orchestrate(auth_client, source, project_id=project_id)

    assert response.status_code == 400
    assert "no host_id and workspace" in response.json()["error"]["message"]


async def test_workspace_path_create_still_uses_the_sender_placement(auth_client, stack):
    source, _, store = stack

    response = await _orchestrate(auth_client, source)

    assert response.status_code == 201, response.text
    created = store.get_conversation(response.json()["id"])
    assert created.workspace == "/sender/checkout"


async def test_environment_id_is_refused_outside_an_orchestrated_create(auth_client, stack):
    source, _, _ = stack

    response = await auth_client.post(
        "/v1/sessions", json={"agent_id": source["agent_id"], "environment_id": ENVIRONMENT_ID}
    )

    assert response.status_code == 400


async def test_sys_session_create_passes_the_environment_to_the_server(auth_client, stack):
    import json

    from omnigent.runner.tool_dispatch import execute_tool

    source, pinned_host, store = stack
    await _project(
        auth_client,
        "Cloud",
        {"host_id": pinned_host, "workspace": PINNED_WORKSPACE, "environment_id": ENVIRONMENT_ID},
    )

    result = json.loads(
        await execute_tool(
            tool_name="sys_session_create",
            arguments=json.dumps(
                {
                    "agent_id": source["agent_id"],
                    "placement": "same_host",
                    "environment_id": ENVIRONMENT_ID,
                }
            ),
            server_client=auth_client,
            conversation_id=source["id"],
        )
    )

    assert "error" not in result, result
    assert store.get_conversation(result["conversation_id"]).workspace == PINNED_WORKSPACE


@pytest.mark.parametrize("source_filed", [False, True])
async def test_named_project_wins_over_the_source_project(
    auth_client, db_uri, stack, source_filed
):
    source, pinned_host, store = stack
    project_id = await _project(
        auth_client, "Cloud", {"host_id": pinned_host, "workspace": PINNED_WORKSPACE}
    )
    if source_filed:
        other = await _project(auth_client, "Elsewhere")
        response = await auth_client.patch(f"/v1/sessions/{source['id']}", json={"project_id": other})
        assert response.status_code == 200, response.text

    response = await _orchestrate(auth_client, source, project_id=project_id)

    assert response.status_code == 201, response.text
    created = store.get_conversation(response.json()["id"])
    assert (created.project_id, created.workspace) == (project_id, PINNED_WORKSPACE)
    assert created.family_id is None


async def test_sys_session_create_schema_offers_the_project_and_environment():
    from omnigent.tools.builtins.spawn import SysSessionCreateTool

    properties = SysSessionCreateTool().get_schema()["function"]["parameters"]["properties"]

    assert {"project_id", "environment_id"} <= properties.keys()
