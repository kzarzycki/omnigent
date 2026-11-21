"""Where a claude-cloud session's child sessions run, and its agent listing."""

from __future__ import annotations

import asyncio
import dataclasses
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
# The caller's own agent is session-bound, so the agent catalog doesn't know it.
OWN_AGENT = "ag_session_cloud"
PARENT_WORKSPACE = "/work/parent"
HARNESSES = {"ag_cloud_copy": "acp:claude-cloud", "ag_local": "claude-native"}


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
            row = {"id": "ag_local", "name": "claude-native-ui", "harness": "claude-native"}
            return httpx.Response(
                200, json={"data": [] if "after" in request.url.params else [row]}
            )
        if path.endswith("/child_sessions"):
            return httpx.Response(200, json={"data": []})
        if path.endswith("/events"):
            return httpx.Response(202, json={"queued": True})
        if request.method == "GET" and path == "/v1/sessions":
            return httpx.Response(200, json={"data": []})
        if request.method == "GET":
            session_id = path.rsplit("/", 1)[1]
            return httpx.Response(
                200,
                json={
                    "id": session_id,
                    "agent_id": OWN_AGENT,
                    "workspace": PARENT_WORKSPACE if session_id == "lead" else None,
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


async def _call(
    tool: str,
    arguments: dict,
    spec: AgentSpec,
    requests: list,
    *,
    scripted: bool = True,
    **options: object,
) -> dict:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server(requests, scripted=scripted)),
        base_url="http://server",
    ) as client:
        output = await execute_tool(
            tool_name=tool,
            arguments=json.dumps(arguments),
            server_client=client,
            conversation_id="lead",
            agent_spec=spec,
            **options,  # type: ignore[arg-type]
        )
    return json.loads(output) if output.startswith("{") else {"error": output}


def _creates(requests: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in requests if r.method == "POST" and not r.url.path.endswith("/events")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [{}, {"agent_id": OWN_AGENT}, {"agent_id": "ag_cloud_copy"}],
    ids=["no-agent", "own-session-bound-agent", "other-cloud-agent"],
)
async def test_a_default_create_starts_a_cloud_child_in_the_parents_workspace(
    arguments: dict,
) -> None:
    requests: list[httpx.Request] = []

    result = await _call(
        "sys_session_create", {"title": "helper", **arguments}, CLOUD_SPEC, requests
    )

    assert result["conversation_id"] == "conv_child", result
    (create,) = _creates(requests)
    body = json.loads(create.content)
    assert create.url.path == "/v1/sessions"
    assert body["parent_session_id"] == "lead"
    assert body["agent_id"] == arguments.get("agent_id", OWN_AGENT)
    # The bridge starts the child from this workspace, whose pin names the cloud environment.
    assert body["workspace"] == PARENT_WORKSPACE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "path"),
    [
        (
            {"placement": "host", "host_id": "host_b", "workspace": "/work/repo"},
            "/v1/sessions/orchestrated/host",
        ),
        ({"placement": "managed"}, "/v1/sessions/orchestrated"),
        ({"placement": "same_host", "environment_id": "env_pinned"}, "/v1/sessions/orchestrated"),
    ],
    ids=["named-host", "managed", "project-pinned-host"],
)
async def test_a_create_naming_where_it_runs_places_a_local_agent(
    arguments: dict, path: str
) -> None:
    requests: list[httpx.Request] = []

    result = await _call(
        "sys_session_create", {"agent_id": "ag_local", **arguments}, CLOUD_SPEC, requests
    )

    assert "error" not in result, result
    (create,) = _creates(requests)
    body = json.loads(create.content)
    assert create.url.path == path
    assert "parent_session_id" not in body
    if "host_id" in arguments:
        assert (body["host_id"], body["workspace"]) == ("host_b", "/work/repo")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "options"),
    [
        ({"agent_id": "ag_local"}, {}),
        ({"agent_id": "ag_unknown"}, {}),
        ({"agent_id": "ag_local", "placement": "same_host"}, {}),
        ({"agent_id": OWN_AGENT, "placement": "same_host"}, {}),
        ({"config_path": "agent.yaml"}, {}),
        ({"agent_id": "ag_local"}, {"spec": LOCAL_SPEC, "effective_harness": "acp:claude-cloud"}),
    ],
    ids=[
        "local-child",
        "unknown-agent-child",
        "local-same-host",
        "cloud-same-host",
        "config-path",
        "cloud-harness-override",
    ],
)
async def test_a_local_session_on_the_bridge_host_is_refused(
    arguments: dict, options: dict
) -> None:
    requests: list[httpx.Request] = []
    spec = options.pop("spec", CLOUD_SPEC)

    result = await _call("sys_session_create", arguments, spec, requests, **options)

    assert "placement='host' with host_id and workspace" in result["error"], result
    assert "no session was created" in result["error"]
    assert _creates(requests) == []


@pytest.mark.asyncio
async def test_a_named_send_does_not_start_a_local_child_on_the_bridge_host() -> None:
    helper = AgentSpec(
        spec_version=1,
        name="local-helper",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )
    requests: list[httpx.Request] = []

    result = await _call(
        "sys_session_send",
        {"agent": "local-helper", "args": "Report done"},
        dataclasses.replace(CLOUD_SPEC, sub_agents=[helper]),
        requests,
        session_inbox=asyncio.Queue(),
    )

    assert "placement='host' with host_id and workspace" in result["error"], result
    assert _creates(requests) == []


@pytest.mark.asyncio
async def test_a_host_session_keeps_its_default_local_child() -> None:
    requests: list[httpx.Request] = []

    result = await _call("sys_session_create", {"agent_id": "ag_local"}, LOCAL_SPEC, requests)

    assert result["conversation_id"] == "conv_child", result
    (create,) = _creates(requests)
    assert "workspace" not in json.loads(create.content)
    assert not any(r.url.path.startswith("/v1/agents/") for r in requests)


@pytest.mark.asyncio
async def test_a_host_create_names_the_flag_that_enables_it() -> None:
    requests: list[httpx.Request] = []

    result = await _call(
        "sys_session_create",
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

    assert "OMNIGENT_FEATURES=scripted_sessions" in result["error"], result


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{}, {"limit": 1}], ids=["complete", "paged"])
@pytest.mark.parametrize(("spec", "listed"), [(CLOUD_SPEC, True), (LOCAL_SPEC, False)])
async def test_the_agent_list_names_a_cloud_sessions_own_agent(
    spec: AgentSpec, listed: bool, arguments: dict, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []

    result = await _call("sys_agent_list", arguments, spec, requests, runner_workspace=tmp_path)

    assert [row["agent_id"] for row in result["builtins"]] == ["ag_local"]
    if not listed:
        assert "caller_agent" not in result
        return
    (own,) = result["caller_agent"]
    assert (own["agent_id"], own["name"], own["harness"]) == (
        OWN_AGENT,
        "claude-cloud",
        "acp:claude-cloud",
    )
    cursor = result.get("page", {}).get("next_cursor")
    if cursor:
        later = await _call(
            "sys_agent_list",
            {**arguments, "cursor": cursor},
            spec,
            requests,
            runner_workspace=tmp_path,
        )
        # Listed once, on the first page.
        assert "caller_agent" not in later
