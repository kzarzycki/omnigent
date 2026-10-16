"""The session subscription tools reach every harness."""

from __future__ import annotations

import pytest

from omnigent.runner.tool_dispatch import build_native_relay_tool_schemas
from omnigent.spec.types import AgentSpec
from omnigent.tools.manager import ToolManager

_TOOLS = {"sys_session_subscribe", "sys_session_unsubscribe", "sys_session_subscriptions"}


@pytest.mark.parametrize("spec", [AgentSpec(spec_version=1), None])
def test_native_harnesses_see_the_subscription_tools(spec: AgentSpec | None) -> None:
    names = {schema["name"] for schema in build_native_relay_tool_schemas(spec)}

    assert names >= _TOOLS


def test_every_agent_gets_the_subscription_tools() -> None:
    names = {
        schema["function"]["name"]
        for schema in ToolManager(AgentSpec(spec_version=1)).get_tool_schemas()
    }

    assert names >= _TOOLS
