"""sys_ask_user_question: a question waits as a card while the session keeps taking input."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from omnigent.runtime import pending_elicitations
from tests.server.helpers import create_test_session
from tests.server.integration import test_session_subscriptions as _subscriptions

# The subscription scenario's fake runner records every message a session is sent.
_Runner = _subscriptions._Runner
runner = _subscriptions.runner

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean_pending_index() -> Any:
    yield
    pending_elicitations.reset_for_tests()


async def _ask(client: httpx.AsyncClient, session_id: str, *questions: dict[str, Any]) -> str:
    response = await client.post(
        f"/v1/sessions/{session_id}/questions", json={"questions": list(questions)}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "waiting"
    return body["question_id"]


async def _pending_questions(client: httpx.AsyncClient, session_id: str) -> list[dict[str, Any]]:
    response = await client.get(f"/v1/sessions/{session_id}")
    assert response.status_code == 200, response.text
    return response.json()["pending_questions"]


def _question(text: str, *labels: str, multi: bool = False) -> dict[str, Any]:
    return {
        "question": text,
        "options": [{"label": label} for label in labels],
        "multiSelect": multi,
    }


async def test_a_message_sent_while_a_question_waits_is_delivered_at_once(
    client: httpx.AsyncClient, runner: _Runner
) -> None:
    session = (await create_test_session(client))["id"]
    question_id = await _ask(client, session, _question("Ship it?", "Yes", "No"))

    sent = await client.post(
        f"/v1/sessions/{session}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "relay: CI"}]},
        },
    )

    assert sent.status_code == 202, sent.text
    assert runner.notices(session) == ["relay: CI"]
    snapshot = (await client.get(f"/v1/sessions/{session}")).json()
    assert [event["elicitation_id"] for event in snapshot["pending_elicitations"]] == [question_id]
    assert snapshot["pending_questions"] == [
        {
            "question_id": question_id,
            "questions": [
                {
                    "question": "Ship it?",
                    "options": [{"label": "Yes"}, {"label": "No"}],
                    "multiSelect": False,
                }
            ],
        }
    ]


async def test_an_answer_arrives_as_a_user_message_and_leaves_pending_questions(
    client: httpx.AsyncClient, runner: _Runner
) -> None:
    session = (await create_test_session(client))["id"]
    question_id = await _ask(
        client,
        session,
        _question("Ship it?", "Yes", "No"),
        _question("Which envs?", "dev", "prod", multi=True),
    )
    verdict = {
        "type": "approval",
        "data": {
            "elicitation_id": question_id,
            "action": "accept",
            "content": {"Ship it?": "Yes", "Which envs?": ["dev", "prod"]},
        },
    }

    first = await client.post(f"/v1/sessions/{session}/events", json=verdict)
    second = await client.post(f"/v1/sessions/{session}/events", json=verdict)

    assert (first.status_code, second.status_code) == (202, 202)
    assert runner.notices(session) == [
        f"[answer to {question_id}] Ship it?: Yes\n"
        f"[answer to {question_id}] Which envs?: dev, prod"
    ]
    assert [body["type"] for _sid, body in runner.events] == ["message"]
    assert await _pending_questions(client, session) == []


async def test_two_questions_wait_at_once_and_each_answer_names_its_own_id(
    client: httpx.AsyncClient, runner: _Runner
) -> None:
    session = (await create_test_session(client))["id"]
    first = await _ask(client, session, _question("Ship it?", "Yes", "No"))
    second = await _ask(client, session, _question("Tag it?", "v1", "v2"))
    assert [q["question_id"] for q in await _pending_questions(client, session)] == [
        first,
        second,
    ]

    resolved = await client.post(
        f"/v1/sessions/{session}/elicitations/{second}/resolve",
        json={"action": "accept", "content": {"Tag it?": "my own tag"}},
    )
    await client.post(
        f"/v1/sessions/{session}/events",
        json={"type": "approval", "data": {"elicitation_id": first, "action": "decline"}},
    )

    assert resolved.status_code == 202, resolved.text
    assert runner.notices(session) == [
        f"[answer to {second}] Tag it?: my own tag",
        f"[answer to {first}] Ship it?: (dismissed without an answer)",
    ]
    assert await _pending_questions(client, session) == []


async def test_a_question_without_labelled_options_is_rejected(client: httpx.AsyncClient) -> None:
    session = (await create_test_session(client))["id"]

    response = await client.post(
        f"/v1/sessions/{session}/questions", json={"questions": [{"question": "Why?"}]}
    )

    assert response.status_code == 400, response.text
    assert pending_elicitations.snapshot_for(session) == []
