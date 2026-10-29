"""sys_ask_user_question reaches every harness, never prompts, and returns without waiting."""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.harnesses.antigravity_native.bridge import _AGY_ENABLED_TOOLS
from omnigent.harnesses.claude_native.bridge import augment_claude_args
from omnigent.harnesses.codex_native.app_server import framework_approved_tools
from omnigent.harnesses.cursor_native.bridge import _CURSOR_AUTO_APPROVE_TOOLS
from omnigent.runner.tool_dispatch import build_native_relay_tool_schemas, execute_tool
from omnigent.spec.types import AgentSpec
from omnigent.tools.manager import ToolManager

_QUESTIONS = [{"question": "Ship it?", "options": [{"label": "Yes"}, {"label": "No"}]}]


@pytest.mark.parametrize("spec", [AgentSpec(spec_version=1), None])
def test_native_harnesses_see_the_question_tool(spec: AgentSpec | None) -> None:
    assert "sys_ask_user_question" in {s["name"] for s in build_native_relay_tool_schemas(spec)}


def test_every_agent_gets_the_question_tool() -> None:
    schemas = ToolManager(AgentSpec(spec_version=1)).get_tool_schemas()
    assert "sys_ask_user_question" in {s["function"]["name"] for s in schemas}


def test_asking_never_prompts_on_a_native_harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    assert "sys_ask_user_question" in framework_approved_tools(routed_spawns=False)
    assert "sys_ask_user_question" in _CURSOR_AUTO_APPROVE_TOOLS
    assert "sys_ask_user_question" in _AGY_ENABLED_TOOLS
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path)
    args = augment_claude_args((), bridge_dir=tmp_path, python_executable="/venv/bin/python")
    allowed = args[args.index("--allowedTools") + 1].split(",")
    assert "mcp__omnigent__sys_ask_user_question" in allowed


async def test_the_question_tool_returns_the_servers_question_id_at_once() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"question_id": "question_1", "status": "waiting"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        output = await execute_tool(
            tool_name="sys_ask_user_question",
            arguments=json.dumps({"questions": _QUESTIONS}),
            server_client=client,
            conversation_id="conv_1",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert json.loads(output) == {"question_id": "question_1", "status": "waiting"}
    assert [(r.method, r.url.path) for r in requests] == [
        ("POST", "/v1/sessions/conv_1/questions")
    ]
    assert json.loads(requests[0].content) == {"questions": _QUESTIONS}


async def test_a_refused_question_comes_back_as_a_tool_error() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(400, text="no options")),
        base_url="http://test",
    ) as client:
        output = await execute_tool(
            tool_name="sys_ask_user_question",
            arguments=json.dumps({"questions": [{"question": "Why?"}]}),
            server_client=client,
            conversation_id="conv_1",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert json.loads(output) == {"error": "sys_ask_user_question returned 400: no options"}
