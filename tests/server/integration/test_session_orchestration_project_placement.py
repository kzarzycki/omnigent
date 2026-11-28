"""An orchestrated create can start on the host and workspace a project pins."""

from uuid import uuid4

import pytest
from fastapi import FastAPI

from omnigent.host.frames import HostLaunchRunnerFrame, HostStatFrame, decode_host_frame
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server.feature_flags import (
    FEATURES_ENV_VAR,
    Feature,
    FeatureFlags,
    resolve_feature_flags,
)
from omnigent.server.scripted_host_policy import load_scripted_host_policy
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
# Every frame the fake host received in the current test.
frames: list = []


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
    # Scripted sessions are on and cross-host placements allowed by default; the policy
    # tests narrow the policy, and the flag-off test turns the flag off.
    flags = auth_app.state.feature_flags
    auth_app.state.feature_flags = FeatureFlags(
        flags.enabled_features | {Feature.SCRIPTED_SESSIONS}
    )
    auth_app.state.scripted_host_policy = _policy(pinned_host)
    auth_client.headers.update(_bind_runner(db_uri, source["id"]))
    _stub_late_runner_bind(monkeypatch)

    frames.clear()

    def answer_host(connection, payload):
        frame = decode_host_frame(payload)
        frames.append(frame)
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


def _policy(*host_ids, agents=("*",)):
    return load_scripted_host_policy(
        {
            "scripted_host_policy": {
                "rules": [{"host_id": host_id, "agents": list(agents)} for host_id in host_ids]
            }
        }
    )


async def test_create_takes_host_and_workspace_from_the_named_environment(
    auth_client, auth_app, stack
):
    source, pinned_host, store = stack
    auth_app.state.scripted_host_policy = _policy(pinned_host)
    project_id = await _project(
        auth_client,
        "Cloud",
        {"host_id": pinned_host, "workspace": PINNED_WORKSPACE, "environment_id": ENVIRONMENT_ID},
    )

    response = await _orchestrate(auth_client, source, environment_id=ENVIRONMENT_ID)

    assert response.status_code == 201, response.text
    created = store.get_conversation(response.json()["id"])
    assert (created.host_id, created.workspace) == (pinned_host, PINNED_WORKSPACE)
    assert created.project_id == project_id


@pytest.mark.parametrize("policy", ["none", "other_host", "cloud_bridge_only"])
async def test_a_cross_host_environment_pin_needs_the_placement_policy(
    auth_client, auth_app, stack, policy
):
    """An environment's pinned host is a placement off the sender's host: policy applies."""
    source, pinned_host, store = stack
    auth_app.state.scripted_host_policy = {
        "none": None,
        "other_host": _policy(uuid4().hex),
        "cloud_bridge_only": _policy(pinned_host, agents=("@cloud-bridge",)),
    }[policy]
    await _project(
        auth_client,
        "Cloud",
        {"host_id": pinned_host, "workspace": PINNED_WORKSPACE, "environment_id": ENVIRONMENT_ID},
    )
    before = {c.id for c in store.list_conversations().data}
    frames.clear()

    response = await _orchestrate(auth_client, source, environment_id=ENVIRONMENT_ID)

    assert response.status_code == 403, response.text
    assert "placement policy refused" in response.text
    assert {c.id for c in store.list_conversations().data} == before
    assert not frames


async def test_a_pin_on_the_senders_own_host_needs_no_policy(auth_client, auth_app, stack):
    source, _, store = stack
    sender_host = store.get_conversation(source["id"]).host_id
    auth_app.state.scripted_host_policy = None
    await _project(
        auth_client,
        "Own",
        {"host_id": sender_host, "workspace": PINNED_WORKSPACE, "environment_id": ENVIRONMENT_ID},
    )

    response = await _orchestrate(auth_client, source, environment_id=ENVIRONMENT_ID)

    assert response.status_code == 201, response.text


async def _managed_deployment(auth_app, monkeypatch):
    from omnigent.server.managed_hosts import parse_sandbox_config
    from omnigent.server.routes.sessions import routes_core

    deployment = parse_sandbox_config(
        {"server_url": "https://managed-test.example.com", "providers": [{"provider": "modal"}]}
    )
    assert deployment is not None
    auth_app.state.sandbox_config = deployment

    async def launch(**kwargs):
        return None

    monkeypatch.setattr(routes_core, "_run_managed_launch", launch)


@pytest.mark.parametrize(
    "shape",
    [
        "child",
        "same_host",
        "environment_pin_other_host",
        "managed",
        "cloud_bridge_on_other_host",
        "plain_host_bound_create",
    ],
)
async def test_with_the_flag_off_and_no_policy_nothing_that_works_today_is_refused(
    auth_client, auth_app, stack, monkeypatch, shape
):
    """`scripted_sessions` off: the create path behaves as it did before the policy."""
    import json as _json

    from tests.server.helpers import build_agent_bundle

    source, pinned_host, _ = stack
    auth_app.state.feature_flags = FeatureFlags(
        auth_app.state.feature_flags.enabled_features - {Feature.SCRIPTED_SESSIONS}
    )
    auth_app.state.scripted_host_policy = None
    agent_id = source["agent_id"]
    if shape == "cloud_bridge_on_other_host":
        bundle = build_agent_bundle(
            name="cloud-agent",
            executor={"type": "omnigent", "config": {"harness": "acp:claude-cloud"}},
        )
        uploaded = await auth_client.post(
            "/v1/sessions",
            data={"metadata": _json.dumps({})},
            files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        )
        assert uploaded.status_code == 201, uploaded.text
        agent_id = (await auth_client.get(f"/v1/sessions/{uploaded.json()['session_id']}")).json()[
            "agent_id"
        ]
    if shape in ("environment_pin_other_host", "cloud_bridge_on_other_host"):
        await _project(
            auth_client,
            "Cloud",
            {
                "host_id": pinned_host,
                "workspace": PINNED_WORKSPACE,
                "environment_id": ENVIRONMENT_ID,
            },
        )
    if shape == "managed":
        await _managed_deployment(auth_app, monkeypatch)
    source_header = {ORCHESTRATION_SOURCE_HEADER: source["id"]}
    request = {
        "child": ("/v1/sessions", {}, {"parent_session_id": source["id"]}),
        "same_host": ("/v1/sessions/orchestrated", source_header, {}),
        "environment_pin_other_host": (
            "/v1/sessions/orchestrated",
            source_header,
            {"environment_id": ENVIRONMENT_ID},
        ),
        "managed": ("/v1/sessions/orchestrated", source_header, {"host_type": "managed"}),
        "cloud_bridge_on_other_host": (
            "/v1/sessions/orchestrated",
            source_header,
            {"environment_id": ENVIRONMENT_ID},
        ),
        "plain_host_bound_create": (
            "/v1/sessions",
            {},
            {"host_id": pinned_host, "workspace": PINNED_WORKSPACE},
        ),
    }[shape]
    path, headers, fields = request

    response = await auth_client.post(path, headers=headers, json={"agent_id": agent_id, **fields})

    assert response.status_code == 201, response.text


@pytest.mark.parametrize(
    "scenario",
    [
        "allowed",
        "no_policy",
        "host_not_in_rules",
        "local_agent_on_cloud_bridge_only_host",
        "user_agent_with_an_operator_agents_name",
    ],
)
async def test_explicit_host_route_enforces_the_placement_policy(
    auth_client, auth_app, stack, scenario
):
    """Reaches the server route directly, so the runner's /v1/info check cannot mask it."""
    source, pinned_host, store = stack
    auth_app.state.feature_flags = resolve_feature_flags({FEATURES_ENV_VAR: "scripted_sessions"})
    agent_name = auth_app.state.agent_store.get(source["agent_id"]).name
    assert agent_name == "test-agent"
    auth_app.state.scripted_host_policy = {
        "allowed": _policy(pinned_host),
        "no_policy": None,
        "host_not_in_rules": _policy(uuid4().hex),
        "local_agent_on_cloud_bridge_only_host": _policy(pinned_host, agents=("@cloud-bridge",)),
        # The fixture agent is user-uploaded: a name rule must not match it.
        "user_agent_with_an_operator_agents_name": _policy(pinned_host, agents=(agent_name,)),
    }[scenario]
    before = {c.id for c in store.list_conversations().data}
    frames.clear()

    response = await auth_client.post(
        "/v1/sessions/orchestrated/host",
        headers={ORCHESTRATION_SOURCE_HEADER: source["id"]},
        json={
            "agent_id": source["agent_id"],
            "host_id": pinned_host,
            "host_type": "external",
            "workspace": PINNED_WORKSPACE,
        },
    )

    if scenario == "allowed":
        assert response.status_code == 201, response.text
        return
    assert response.status_code == 403, response.text
    message = response.json()["error"]["message"]
    assert "placement policy refused" in message
    assert pinned_host in message and agent_name in message
    assert {c.id for c in store.list_conversations().data} == before
    assert not frames


@pytest.mark.parametrize(
    ("fields", "status"),
    [
        ({"environment_id": "env_unknown"}, 404),
        ({"project_id": "x", "environment_id": ENVIRONMENT_ID}, 400),
    ],
)
async def test_unknown_environment_never_falls_back_to_the_sender(
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


async def test_project_id_files_the_session_on_the_sender_placement(auth_client, stack):
    """``project_id`` keeps upstream's meaning: the project's pinned host does not apply."""
    source, pinned_host, store = stack
    project_id = await _project(
        auth_client, "Cloud", {"host_id": pinned_host, "workspace": PINNED_WORKSPACE}
    )

    response = await _orchestrate(auth_client, source, project_id=project_id)

    assert response.status_code == 201, response.text
    created = store.get_conversation(response.json()["id"])
    assert created.workspace == "/sender/checkout"
    assert created.project_id == project_id


async def test_environment_without_a_host_and_workspace_is_refused(auth_client, stack):
    source, _, _ = stack
    await _project(auth_client, "Bare", {"environment_id": ENVIRONMENT_ID})

    response = await _orchestrate(auth_client, source, environment_id=ENVIRONMENT_ID)

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


@pytest.mark.parametrize("source_project", ["unfiled", "same", "other"])
async def test_named_environment_wins_over_the_source_project(
    auth_client, db_uri, stack, source_project
):
    source, pinned_host, store = stack
    project_id = await _project(
        auth_client,
        "Cloud",
        {"host_id": pinned_host, "workspace": PINNED_WORKSPACE, "environment_id": ENVIRONMENT_ID},
    )
    if source_project != "unfiled":
        filed_in = project_id if source_project == "same" else await _project(auth_client, "Other")
        response = await auth_client.patch(
            f"/v1/sessions/{source['id']}", json={"project_id": filed_in}
        )
        assert response.status_code == 200, response.text

    response = await _orchestrate(auth_client, source, environment_id=ENVIRONMENT_ID)

    assert response.status_code == 201, response.text
    created = store.get_conversation(response.json()["id"])
    assert (created.project_id, created.workspace) == (project_id, PINNED_WORKSPACE)
    assert created.family_id is None


async def test_sys_session_create_schema_offers_the_project_and_environment():
    from omnigent.tools.builtins.spawn import SysSessionCreateTool

    properties = SysSessionCreateTool().get_schema()["function"]["parameters"]["properties"]

    assert {"project_id", "environment_id"} <= properties.keys()
