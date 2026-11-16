"""A spec's disallowed tools are enforced by a server policy, whatever the harness flag does."""

from __future__ import annotations

import pytest

from omnigent.policies.builtins.safety import deny_tools
from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies.builder import any_policies_apply, build_policy_engine
from omnigent.spec.types import AgentSpec, ExecutorSpec, Phase, PolicyAction
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def _spec(disallowed: list[str]) -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="reviewer",
        executor=ExecutorSpec(config={"disallowed_tools": disallowed}),
    )


def _tool_call(name: str) -> EvaluationContext:
    return EvaluationContext(
        phase=Phase.TOOL_CALL, content={"name": name, "arguments": {}}, tool_name=name
    )


@pytest.mark.asyncio
async def test_the_server_refuses_a_tool_the_spec_forbids_with_no_harness_flag(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Only the spec reaches the engine (no --disallowedTools anywhere), and Edit is denied."""
    conversation = conversation_store.create_conversation()
    engine = build_policy_engine(
        spec=_spec(["Edit", "Bash(git push:*)"]),
        conversation_id=conversation.id,
        conversation_store=conversation_store,
    )

    denied = await engine.evaluate(_tool_call("Edit"))
    allowed = await engine.evaluate(_tool_call("Read"))
    # An argument pattern is the harness's to enforce; the plain name is not widened.
    pattern = await engine.evaluate(_tool_call("Bash"))

    assert denied.action == PolicyAction.DENY
    assert "Edit" in (denied.reason or "")
    assert allowed.action == PolicyAction.ALLOW
    assert pattern.action == PolicyAction.ALLOW


def test_the_policy_fast_path_does_not_skip_a_spec_that_forbids_tools(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conversation = conversation_store.create_conversation()

    def applies(spec: AgentSpec) -> bool:
        return any_policies_apply(
            spec=spec,
            conversation_id=conversation.id,
            default_policies=None,
            policy_store=None,
            phase=Phase.TOOL_CALL,
            tool_name="Edit",
            conversation=conversation,
        )

    assert applies(_spec(["Edit"]))
    assert not applies(_spec([]))


@pytest.mark.parametrize("event_type", ["request", "response", "tool_result"])
def test_only_a_tool_call_event_is_denied_by_the_tool_denylist(event_type: str) -> None:
    evaluate = deny_tools(["Edit"])

    assert evaluate({"type": event_type, "data": {"name": "Edit"}})["result"] == "ALLOW"
    assert evaluate({"type": "tool_call", "data": {"name": "Edit"}})["result"] == "DENY"
