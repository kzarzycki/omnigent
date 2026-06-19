"""E2E test: "first session to working code" user journey (mock LLM).

Exercises the full developer workflow end-to-end with a mock LLM:
create session -> chat with agent -> agent writes code -> add review
comment -> agent addresses comment.

The mock LLM returns pre-scripted tool calls (sys_os_write for the
coding turn, list_comments + update_comment for the review turn)
so the test validates the session/tool dispatch pipeline without
requiring a real LLM.

list_comments and update_comment are always auto-registered by the
runner's ToolManager. sys_os_write is dispatched by the runner via
a per-conversation tmpdir fallback when the spec declares no os_env.

Usage::

    pytest tests/e2e/test_journey_first_session_to_code.py -v
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


def _tool_names_in_output(body: dict[str, Any]) -> list[str]:
    """Collect every function_call tool name from a response body."""
    return [
        item["name"]
        for item in body.get("output", [])
        if item.get("type") == "function_call" and item.get("name")
    ]


def _extract_all_text(body: dict[str, Any]) -> str:
    """Concatenate all assistant output_text blocks."""
    parts: list[str] = []
    for item in body.get("output", []):
        if item.get("type") == "message":
            for block in item.get("content", []):
                text = block.get("text")
                if text:
                    parts.append(text)
    return "\n".join(parts)


def test_first_session_to_working_code_journey(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """Full developer journey: create session, code, review comment, address it.

    :param http_client: HTTP client pointed at the live server.
    :param live_runner_id: Runner id the session is bound to.
    :param mock_llm_server_url: Mock LLM server URL.
    """
    model = f"mock-code-journey-{uuid.uuid4().hex[:6]}"

    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        http_client,
        name=f"code-journey-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt=(
            "You are a coding assistant. When asked to write code, use "
            "the sys_os_write tool to create files. When asked to address "
            "review comments, call list_comments then update_comment."
        ),
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_sow1",
                        "name": "sys_os_write",
                        "arguments": (
                            '{"path": "palindrome.py", '
                            '"content": "def is_palindrome(s: str) -> bool:\\n'
                            "    s = s.lower()\\n    return s == s[::-1]\\n"
                            '"}'
                        ),
                    }
                ],
            },
            {"text": "I've created palindrome.py with the is_palindrome function."},
        ],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )

    session_resp = http_client.get(f"/v1/sessions/{session_id}")
    session_resp.raise_for_status()
    assert session_resp.json()["id"] == session_id

    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Write a Python function called `is_palindrome` that checks "
            "if a string is a palindrome. Save it to `palindrome.py`."
        ),
    )

    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=60,
    )
    assert body["status"] == "completed", (
        f"Agent coding turn failed. error={body.get('error')!r}. output={body.get('output', [])}"
    )

    tool_calls = _tool_names_in_output(body)
    text_output = _extract_all_text(body).lower()
    has_write_evidence = (
        any("write" in t.lower() or "shell" in t.lower() for t in tool_calls)
        or "is_palindrome" in text_output
        or "palindrome" in text_output
    )
    assert has_write_evidence, (
        f"Agent completed but produced no evidence of writing the function. "
        f"Tool calls: {tool_calls}. Text (first 500 chars): {text_output[:500]}"
    )

    comment_resp = http_client.post(
        f"/v1/sessions/{session_id}/comments",
        json={
            "path": "palindrome.py",
            "body": (
                "Add error handling for non-string inputs. The function "
                "should raise a TypeError if the input is not a string."
            ),
            "start_index": 0,
            "end_index": 30,
            "anchor_content": "def is_palindrome",
        },
    )
    comment_resp.raise_for_status()
    comment_id: str = comment_resp.json()["id"]

    comments_resp = http_client.get(f"/v1/sessions/{session_id}/comments")
    comments_resp.raise_for_status()
    comment_statuses = {c["id"]: c["status"] for c in comments_resp.json()}
    assert comment_statuses.get(comment_id) == "draft", (
        f"Expected comment to start as 'draft', got {comment_statuses.get(comment_id)!r}"
    )

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_lc1",
                        "name": "list_comments",
                        "arguments": "{}",
                    }
                ],
            },
            {
                "tool_calls": [
                    {
                        "call_id": "call_uc1",
                        "name": "update_comment",
                        "arguments": f'{{"comment_id": "{comment_id}", "status": "addressed"}}',
                    }
                ],
            },
            {"text": "I've addressed the review comment."},
        ],
        key=model,
    )

    address_response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "I left a review comment on palindrome.py. "
            "Please list_comments and then update_comment to mark it addressed."
        ),
    )

    address_body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=address_response_id,
        timeout=60,
    )
    assert address_body["status"] == "completed", (
        f"Agent address-comment turn failed. "
        f"error={address_body.get('error')!r}. "
        f"output={address_body.get('output', [])}"
    )

    address_calls = _tool_names_in_output(address_body)
    assert "list_comments" in address_calls, (
        f"Agent did not call list_comments. Tool calls seen: {address_calls}. "
        f"Output: {address_body.get('output', [])}"
    )
    assert "update_comment" in address_calls, (
        f"Agent did not call update_comment. Tool calls seen: {address_calls}. "
        f"Output: {address_body.get('output', [])}"
    )

    post_resp = http_client.get(f"/v1/sessions/{session_id}/comments")
    post_resp.raise_for_status()
    post_statuses = {c["id"]: c["status"] for c in post_resp.json()}
    assert post_statuses.get(comment_id) == "addressed", (
        f"Comment still has status {post_statuses.get(comment_id)!r} "
        f"after the agent turn; expected 'addressed'."
    )
