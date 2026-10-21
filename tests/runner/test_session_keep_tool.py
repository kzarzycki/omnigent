"""sys_session_keep reaches every harness and labels the calling session."""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.harnesses.claude_native.bridge import augment_claude_args
from omnigent.harnesses.codex_native.app_server import framework_approved_tools
from omnigent.runner.tool_dispatch import build_native_relay_tool_schemas, execute_tool
from omnigent.spec.types import AgentSpec
from omnigent.tools.manager import ToolManager


@pytest.mark.parametrize("spec", [AgentSpec(spec_version=1), None])
def test_native_harnesses_see_the_keep_tool(spec: AgentSpec | None) -> None:
    assert "sys_session_keep" in {s["name"] for s in build_native_relay_tool_schemas(spec)}


def test_every_agent_gets_the_keep_tool() -> None:
    schemas = ToolManager(AgentSpec(spec_version=1)).get_tool_schemas()
    assert "sys_session_keep" in {s["function"]["name"] for s in schemas}


def test_the_keep_tool_never_prompts_on_a_native_harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    assert "sys_session_keep" in framework_approved_tools(routed_spawns=False)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path)
    args = augment_claude_args((), bridge_dir=tmp_path, python_executable="/venv/bin/python")
    allowed = args[args.index("--allowedTools") + 1].split(",")
    assert "mcp__omnigent__sys_session_keep" in allowed


async def test_the_keep_tool_patches_the_calling_sessions_label() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "conv_1"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        output = await execute_tool(
            tool_name="sys_session_keep",
            arguments=json.dumps({"reason": "  a buyer\nreplied "}),
            server_client=client,
            conversation_id="conv_1",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert json.loads(output) == {"kept": True, "reason": "a buyer replied"}
    assert [(r.method, r.url.path) for r in requests] == [("PATCH", "/v1/sessions/conv_1")]
    assert json.loads(requests[0].content) == {"labels": {"omnigent.keep": "a buyer replied"}}
