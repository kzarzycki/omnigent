"""End-to-end happy path for session subscriptions.

A watcher session subscribes to a worker session; the worker completes a turn;
the watcher is woken by the notice and reads the worker's history. Uses the mock
LLM with a live server and runner::

    pytest tests/e2e/test_session_subscriptions_e2e.py -v --timeout=600
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    _flatten_session_item,
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    send_user_message_to_session,
)
from tests.e2e.helpers import POLL_INTERVAL_S

pytestmark = [
    pytest.mark.timeout(600, method="signal"),
    pytest.mark.min_server_version("0.3.0"),
    pytest.mark.min_runner_version("0.9.0"),
]

_WATCHER_PROMPT = (
    "When the user gives you a session id, subscribe to it with sys_session_subscribe. "
    "When you receive a [System: session ... ended its turn ...] notice, read that "
    "session with sys_session_get_history, then reply with the last text you read."
)


def _tool_call(name: str, arguments: dict[str, object], call_id: str) -> dict[str, str]:
    return {"call_id": call_id, "name": name, "arguments": json.dumps(arguments)}


def _wait_for_history_read(
    http_client: httpx.Client, session_id: str, token: str, *, timeout: float = 180
) -> list[dict[str, Any]]:
    """Poll until *session_id* holds a history read whose output carries *token*."""
    deadline = time.monotonic() + timeout
    items: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        response = http_client.get(
            f"/v1/sessions/{session_id}/items", params={"limit": 100, "order": "asc"}
        )
        response.raise_for_status()
        items = [_flatten_session_item(item) for item in response.json()["data"]]
        reads = {
            item.get("call_id")
            for item in items
            if item.get("type") == "function_call"
            and item.get("name") == "sys_session_get_history"
        }
        if any(
            item.get("type") == "function_call_output"
            and item.get("call_id") in reads
            and token in json.dumps(item.get("output"))
            for item in items
        ):
            return items
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"session {session_id} did not read {token!r} within {timeout}s; last items={items!r}"
    )


def test_a_subscriber_is_woken_by_a_turn_end_and_reads_the_history_e2e(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    uid = uuid.uuid4().hex[:6]
    watcher_model = f"mock-subscription-watcher-{uid}"
    worker_model = f"mock-subscription-worker-{uid}"
    mock_base = f"{mock_llm_server_url}/v1"
    token = f"WORKER_DONE_{uid}"

    watcher_agent = register_inline_agent(
        http_client,
        name=f"subscription-watcher-{uid}",
        harness="openai-agents",
        model=watcher_model,
        profile="",
        prompt=_WATCHER_PROMPT,
        mock_llm_base_url=mock_base,
    )
    worker_agent = register_inline_agent(
        http_client,
        name=f"subscription-worker-{uid}",
        harness="openai-agents",
        model=worker_model,
        profile="",
        prompt="Reply with exactly the text the user asks you to say, nothing else.",
        mock_llm_base_url=mock_base,
    )
    watcher = create_runner_bound_session(
        http_client, agent_name=watcher_agent, runner_id=live_runner_id
    )
    worker = create_runner_bound_session(
        http_client, agent_name=worker_agent, runner_id=live_runner_id
    )
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"tool_calls": [_tool_call("sys_session_subscribe", {"session_id": worker}, "sub")]},
            {"text": "SUBSCRIBED"},
            {
                "tool_calls": [
                    _tool_call(
                        "sys_session_get_history", {"conversation_id": worker}, "read_worker"
                    )
                ]
            },
            {"text": token},
        ],
        key=watcher_model,
    )
    configure_mock_llm(mock_llm_server_url, [{"text": token}], key=worker_model)

    response_id = send_user_message_to_session(
        http_client, session_id=watcher, content=f"Subscribe to session {worker}."
    )
    body = poll_session_until_terminal(
        http_client, session_id=watcher, response_id=response_id, timeout=120
    )
    assert body["status"] == "completed", body.get("error")
    listing = http_client.get(f"/v1/sessions/{watcher}/subscriptions")
    listing.raise_for_status()
    assert [row["session_id"] for row in listing.json()["data"]] == [worker]

    response_id = send_user_message_to_session(
        http_client, session_id=worker, content=f"Say {token}."
    )
    body = poll_session_until_terminal(
        http_client, session_id=worker, response_id=response_id, timeout=120
    )
    assert body["status"] == "completed", body.get("error")

    items = _wait_for_history_read(http_client, watcher, token)
    notices = [
        item
        for item in items
        if item.get("type") == "message"
        and item.get("role") == "user"
        and f"({worker}) ended its turn (completed)" in json.dumps(item.get("content"))
    ]
    assert len(notices) == 1, items
