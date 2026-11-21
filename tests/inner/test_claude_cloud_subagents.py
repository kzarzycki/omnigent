"""A Claude cloud session's sub-agents reach Omnigent as child sessions.

The cloud bridge tags the cloud CLI's sub-agent frames; the generic ACP harness's
default extension reads them into the executor's sub-agent events, which the runner
mints into child sessions (covered by ``tests/runner/test_acp_subagent_forwarding.py``).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from omnigent.inner.acp_executor import AcpAgentConfig, AcpExecutor
from omnigent.inner.acp_extension import generic_acp_extension
from omnigent.inner.executor import (
    SubAgentCompleted,
    SubAgentStarted,
    SubAgentToolCall,
    ToolCallComplete,
    ToolCallRequest,
)

_BRIDGE_PATH = Path(__file__).parents[2] / "integrations/claude-cloud-acp/claude_cloud_acp.py"

_SPAWN = {"description": "find callers", "prompt": "grep for foo", "subagent_type": "Explore"}
# A cloud turn that runs one sub-agent: its start, a tool it ran, that tool's
# result, and the sub-agent's own result.
_STREAM = [
    {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "id": "a1", "name": "Agent", "input": _SPAWN}]
        },
    },
    {
        "type": "assistant",
        "parent_tool_use_id": "a1",
        "message": {
            "content": [
                {"type": "tool_use", "id": "t1", "name": "Grep", "input": {"pattern": "foo"}}
            ]
        },
    },
    {
        "type": "user",
        "parent_tool_use_id": "a1",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a.py"}]},
    },
    {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "a1",
                    "content": [{"type": "text", "text": "a.py"}],
                }
            ]
        },
    },
]


@pytest.fixture
def bridge() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "claude_cloud_acp_subagents_under_test", _BRIDGE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _events(bridge: ModuleType, stream: list[dict], executor: AcpExecutor) -> list[object]:
    events: list[object] = []
    for payload in stream:
        for update in bridge.to_updates(payload)[0]:
            events.extend(executor._handle_session_update(update))
    return events


def test_a_cloud_sub_agent_becomes_a_child_with_its_tool_calls(bridge: ModuleType) -> None:
    executor = AcpExecutor(AcpAgentConfig(command="x"), extension=generic_acp_extension())

    events = _events(bridge, _STREAM, executor)

    assert events == [
        SubAgentStarted(child_key="a1", title="find callers", task="grep for foo"),
        SubAgentToolCall(child_key="a1", call_id="t1", name="Grep", args={"pattern": "foo"}),
        SubAgentCompleted(child_key="a1", ok=True, summary="a.py"),
    ]


def test_a_failed_sub_agent_completes_as_failed(bridge: ModuleType) -> None:
    executor = AcpExecutor(AcpAgentConfig(command="x"), extension=generic_acp_extension())
    failed = {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "a1", "is_error": True, "content": "boom"}
            ]
        },
    }

    events = _events(bridge, [_STREAM[0], failed], executor)

    assert events[-1] == SubAgentCompleted(child_key="a1", ok=False, summary="boom")


def test_other_tool_calls_stay_cards_in_the_parent(bridge: ModuleType) -> None:
    executor = AcpExecutor(AcpAgentConfig(command="x"), extension=generic_acp_extension())
    stream = [
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "id": "b1", "name": "Bash", "input": {}}]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "b1", "content": "ok"}]
            },
        },
    ]

    events = _events(bridge, stream, executor)

    assert [type(event) for event in events] == [ToolCallRequest, ToolCallComplete]
