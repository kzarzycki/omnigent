"""E2E: open sys_ask_user_question cards survive a real server process restart.

The in-process tests rebuild the app on one database; this one stops the server
process and starts a new one, so nothing in memory carries over. The runner is
down while the new server answers its first read, so no runner reconnect
restores the cards before a reader does.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests._helpers.live_server import terminate_process
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HEALTH_TIMEOUT_S = 90.0
_ANSWER_TIMEOUT_S = 30.0
# The model endpoint is never reached: the questions are asked through the API.
_AGENT_YAML = b"""\
spec_version: 1
name: question_restart_probe
prompt: Probe for question cards across a restart.
executor:
  model: probe-model
  config:
    harness: openai-agents
  auth:
    type: api_key
    api_key: probe-key
    base_url: http://127.0.0.1:9/v1
"""

_client = httpx.Client(trust_env=False, timeout=30.0)
for _var in ("NO_PROXY", "no_proxy"):
    os.environ[_var] = ",".join(filter(None, [os.environ.get(_var, ""), "127.0.0.1,localhost"]))


def _create_session(base_url: str, **metadata: str) -> str:
    response = post_session_bundle(
        _client.post,
        f"{base_url}/v1/sessions",
        bundle_files({"config.yaml": _AGENT_YAML}),
        metadata=metadata,
    )
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def _ask(base_url: str, session_id: str, text: str) -> str:
    response = _client.post(
        f"{base_url}/v1/sessions/{session_id}/questions",
        json={"questions": [{"question": text, "options": [{"label": "Yes"}, {"label": "No"}]}]},
    )
    assert response.status_code == 200, response.text
    return response.json()["question_id"]


def _answer(base_url: str, session_id: str, question_id: str, content: dict[str, Any]) -> None:
    response = _client.post(
        f"{base_url}/v1/sessions/{session_id}/elicitations/{question_id}/resolve",
        json={"action": "accept", "content": content},
    )
    assert response.status_code == 202, response.text


def _snapshot(base_url: str, session_id: str) -> dict[str, Any]:
    response = _client.get(f"{base_url}/v1/sessions/{session_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _visible_cards(base_url: str, session_id: str) -> set[tuple[str, str | None]]:
    return {
        (event["elicitation_id"], event["params"].get("target_session_id"))
        for event in _snapshot(base_url, session_id)["pending_elicitations"]
    }


def _answers_received(base_url: str, session_id: str) -> list[str]:
    response = _client.get(f"{base_url}/v1/sessions/{session_id}/items", params={"limit": 200})
    assert response.status_code == 200, response.text
    return [
        block["text"]
        for item in response.json()["data"]
        if item.get("type") == "message"
        for block in (item.get("data") or item).get("content") or []
        if str(block.get("text", "")).startswith("[answer to ")
    ]


@pytest.mark.timeout(300)
def test_open_questions_survive_a_server_process_restart(tmp_path: Path) -> None:
    with server_runner(
        tmp_path,
        server_cwd=_REPO_ROOT,
        health_timeout=_HEALTH_TIMEOUT_S,
        poll_interval=0.5,
    ) as stack:
        stack.start_runner(cwd=_REPO_ROOT)
        base_url = stack.base_url
        parent = _create_session(base_url)
        bind_session_runner(_client.patch, base_url, parent, stack.runner_id)
        child = _create_session(base_url, parent_session_id=parent)
        child_question = _ask(base_url, child, "Child asks?")
        open_question = _ask(base_url, parent, "Ship it?")
        answered_question = _ask(base_url, parent, "Tag it?")
        _answer(base_url, parent, answered_question, {"Tag it?": "No"})
        first_server = stack.server

        terminate_process(stack.runner)
        stack.runner = None
        terminate_process(stack.server)
        stack.start_server()
        assert stack.server is not first_server

        # The parent is the first session the new process reads.
        assert _visible_cards(base_url, parent) == {
            (open_question, None),
            (child_question, child),
        }
        stack.start_runner(cwd=_REPO_ROOT)
        listed = _client.get(f"{base_url}/v1/sessions", params={"visibility": "all"}).json()[
            "data"
        ]
        assert {row["id"]: row["pending_elicitations_count"] for row in listed}[parent] == 2
        assert [q["question_id"] for q in _snapshot(base_url, parent)["pending_questions"]] == [
            open_question
        ]

        _answer(base_url, parent, open_question, {"Ship it?": "Yes"})
        deadline = time.monotonic() + _ANSWER_TIMEOUT_S
        while time.monotonic() < deadline:
            if f"[answer to {open_question}] Ship it?: Yes" in _answers_received(base_url, parent):
                break
            time.sleep(0.5)
        assert _answers_received(base_url, parent) == [
            f"[answer to {answered_question}] Tag it?: No",
            f"[answer to {open_question}] Ship it?: Yes",
        ]
        assert _snapshot(base_url, parent)["pending_questions"] == []
