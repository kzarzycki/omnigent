"""E2E journey test: skill loading and execution (mock LLM).

Verifies the full user journey of loading a bundled skill and
using its content in a follow-up turn, driven by a mock LLM:

1. Create session with an inline agent.
2. Mock LLM returns load_skill + read_skill_file tool calls.
3. Verify the tools were called and returned skill content.
4. Mock LLM returns text referencing the skill knowledge.
5. Verify the response references the skill's content.

load_skill and read_skill_file are always auto-registered by the
runner's ToolManager regardless of spec tools.builtins declaration.

Usage::

    pytest tests/e2e/test_journey_skill_loading.py -v
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.helpers import final_assistant_text


def _extract_tool_names(body: dict[str, Any]) -> list[str]:
    """Extract all function_call tool names from a response body."""
    return [
        item.get("name", "")
        for item in body.get("output", [])
        if item.get("type") == "function_call"
    ]


def _extract_tool_results(body: dict[str, Any]) -> list[str]:
    """Extract all function_call_output strings from a response body."""
    return [
        item.get("output", "")
        for item in body.get("output", [])
        if item.get("type") == "function_call_output"
    ]


def test_skill_loading_journey(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """Full journey: load a skill, read its reference file, use its content.

    :param http_client: HTTP client pointed at the live e2e server.
    :param live_runner_id: Runner id bound to the session.
    :param mock_llm_server_url: Mock LLM server URL.
    """
    model = f"mock-skill-{uuid.uuid4().hex[:6]}"

    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        http_client,
        name=f"skill-journey-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt=(
            "You are a research assistant with skill loading capability. "
            "When asked, call load_skill and read_skill_file tools."
        ),
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_ls1",
                        "name": "load_skill",
                        "arguments": '{"name": "deep-research"}',
                    }
                ],
            },
            {
                "tool_calls": [
                    {
                        "call_id": "call_rsf1",
                        "name": "read_skill_file",
                        "arguments": (
                            '{"skill_name": "deep-research", '
                            '"path": "references/research-checklist.md"}'
                        ),
                    }
                ],
            },
            {
                "text": (
                    "I've loaded the deep-research skill and read the checklist. "
                    "The research checklist requires verifying claims against "
                    "3 independent sources before presenting conclusions."
                ),
            },
        ],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )

    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Call load_skill with name=deep-research. "
            "Then call read_skill_file with "
            "skill_name=deep-research and "
            "path=references/research-checklist.md. "
            "Tell me what the checklist says."
        ),
    )

    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=60,
    )

    assert body["status"] == "completed", (
        f"Expected completed, got {body['status']}. Error: {body.get('error')}"
    )

    tool_names = _extract_tool_names(body)
    assert "load_skill" in tool_names, (
        f"Expected load_skill tool call. Tool calls: {tool_names}."
    )
    assert "read_skill_file" in tool_names, (
        f"Expected read_skill_file tool call. Tool calls: {tool_names}."
    )

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "text": (
                    "Based on the research checklist from the deep-research "
                    "skill, the key steps before presenting a conclusion are: "
                    "1. Verify claims against 3 independent sources. "
                    "2. Prefer primary sources over secondary ones. "
                    "3. Cross-check for consistency across sources."
                ),
            },
        ],
        key=model,
    )

    followup_response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Using the research checklist from the deep-research skill "
            "you just loaded, what are the key steps before presenting "
            "a conclusion?"
        ),
    )

    followup_body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=followup_response_id,
        timeout=60,
    )

    assert followup_body["status"] == "completed", (
        f"Follow-up failed: {followup_body['status']}. Error: {followup_body.get('error')}"
    )

    followup_text = final_assistant_text(followup_body)
    text_lower = followup_text.lower()
    assert "source" in text_lower or "verify" in text_lower, (
        f"Expected the agent to reference the research checklist content "
        f"(sources, verification). Got: {followup_text[:500]}"
    )
