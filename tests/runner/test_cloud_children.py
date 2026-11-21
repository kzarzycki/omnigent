"""Where a claude-cloud session's ``sys_session_create`` children run, and its agent listing."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from omnigent.runner.tool_dispatch import execute_tool
from omnigent.spec.types import AgentSpec, ExecutorSpec

CLOUD_SPEC = AgentSpec(
    spec_version=1,
    name="claude-cloud",
    spawn=True,
    executor=ExecutorSpec(type="omnigent", config={"harness": "acp:claude-cloud"}),
)
LOCAL_SPEC = AgentSpec(
    spec_version=1,
    name="claude-native-ui",
    spawn=True,
    executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
)
HARNESSES = {
    "ag_cloud": "acp:claude-cloud",
    "ag_cloud_copy": "acp:claude-cloud",
    "ag_local": "claude-native",
}


def _server(requests: list[httpx.Request], *, scripted: bool = True):
    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/v1/info":
            return httpx.Response(200, json={"scripted_sessions": scripted})
        if path.startswith("/v1/agents/"):
            agent_id = path.rsplit("/", 1)[1]
            if agent_id not in HARNESSES:
                return httpx.Response(404)
            return httpx.Response(200, json={"id": agent_id, "harness": HARNESSES[agent_id]})
        if path == "/v1/agents":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "ag_local", "name": "claude-native-ui", "harness": "claude-native"}
                    ]
                },
            )
        if path.endswith("/events"):
            return httpx.Response(202, json={"queued": True})
        if request.method == "GET" and path == "/v1/sessions":
            return httpx.Response(200, json={"data": []})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": path.rsplit("/", 1)[1],
                    "agent_id": "ag_cloud",
                    "parent_session_id": "lead",
                },
            )
        if request.method == "PATCH":
            return httpx.Response(200, json={})
        body = json.loads(request.content)
        return httpx.Response(201, json={"id": "conv_child", "agent_id": body.get("agent_id")})

    return respond


@pytest.fixture(autouse=True)
def _no_work_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.runner import subagent_work

    monkeypatch.setattr(subagent_work, "register_child_session", Mock())
    monkeypatch.setattr(subagent_work, "register_subagent_work", Mock())


async def _create(
    arguments: dict, spec: AgentSpec, requests: list, *, scripted: bool = True
) -> dict:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server(requests, scripted=scripted)),
        base_url="http://server",
    ) as client:
        return json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps(arguments),
                server_client=client,
                conversation_id="lead",
                agent_spec=spec,
            )
        )


def _creates(requests: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in requests if r.method == "POST" and not r.url.path.endswith("/events")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments", [{}, {"agent_id": "ag_cloud_copy"}], ids=["no-agent", "cloud-agent"]
)
async def test_a_default_create_starts_a_cloud_child(arguments: dict) -> None:
    requests: list[httpx.Request] = []

    result = await _create({"title": "helper", **arguments}, CLOUD_SPEC, requests)

    assert result["conversation_id"] == "conv_child", result
    (create,) = _creates(requests)
    body = json.loads(create.content)
    assert create.url.path == "/v1/sessions"
    # A child shares its parent's runner and workspace, so the bridge starts it in the
    # cloud environment the parent's workspace pins.
    assert body["parent_session_id"] == "lead"
    assert body["agent_id"] == arguments.get("agent_id", "ag_cloud")


@pytest.mark.asyncio
async def test_a_create_naming_a_host_places_a_local_agent_there() -> None:
    requests: list[httpx.Request] = []

    result = await _create(
        {
            "agent_id": "ag_local",
            "placement": "host",
            "host_id": "host_b",
            "workspace": "/work/repo",
        },
        CLOUD_SPEC,
        requests,
    )

    assert "error" not in result, result
    (create,) = _creates(requests)
    body = json.loads(create.content)
    assert create.url.path == "/v1/sessions/orchestrated/host"
    assert (body["host_id"], body["workspace"]) == ("host_b", "/work/repo")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"agent_id": "ag_local"},
        {"agent_id": "ag_unknown"},
        {"agent_id": "ag_local", "placement": "same_host"},
        {"agent_id": "ag_cloud", "placement": "same_host"},
    ],
    ids=["local-child", "unknown-agent-child", "local-same-host", "cloud-same-host"],
)
async def test_a_local_session_on_the_bridge_host_is_refused(arguments: dict) -> None:
    requests: list[httpx.Request] = []

    result = await _create(arguments, CLOUD_SPEC, requests)

    assert "placement='host'" in result["error"], result
    assert "host_id" in result["error"] and "no session was created" in result["error"]
    assert _creates(requests) == []


@pytest.mark.asyncio
async def test_a_config_path_create_is_refused_from_the_cloud() -> None:
    requests: list[httpx.Request] = []

    result = await _create({"config_path": "agent.yaml"}, CLOUD_SPEC, requests)

    assert "agent_id" in result["error"], result
    assert _creates(requests) == []


@pytest.mark.asyncio
async def test_a_host_session_keeps_its_default_local_child() -> None:
    requests: list[httpx.Request] = []

    result = await _create({"agent_id": "ag_local"}, LOCAL_SPEC, requests)

    assert result["conversation_id"] == "conv_child", result
    assert not any(r.url.path.startswith("/v1/agents/") for r in requests)


@pytest.mark.asyncio
async def test_a_host_create_names_the_flag_that_enables_it() -> None:
    requests: list[httpx.Request] = []

    result = await _create(
        {
            "agent_id": "ag_local",
            "placement": "host",
            "host_id": "host_b",
            "workspace": "/work/repo",
        },
        CLOUD_SPEC,
        requests,
        scripted=False,
    )

    assert "scripted_sessions" in result["error"], result


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ["{}", '{"limit": 1}'], ids=["complete", "paged"])
@pytest.mark.parametrize(("spec", "listed"), [(CLOUD_SPEC, True), (LOCAL_SPEC, False)])
async def test_the_agent_list_names_a_cloud_sessions_own_agent(
    spec: AgentSpec, listed: bool, arguments: str, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server(requests)), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_agent_list",
                arguments=arguments,
                server_client=client,
                conversation_id="lead",
                agent_spec=spec,
                runner_workspace=tmp_path,
            )
        )

    assert [row["agent_id"] for row in result["builtins"]] == ["ag_local"]
    if listed:
        (own,) = result["caller_agent"]
        assert (own["agent_id"], own["name"], own["harness"]) == (
            "ag_cloud",
            "claude-cloud",
            "acp:claude-cloud",
        )
    else:
        assert "caller_agent" not in result
