"""sys_ask_user_question: a question waits as a card while the session keeps taking input."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runtime import pending_elicitations
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import open_questions
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store import MAX_OPEN_QUESTIONS
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
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
    open_questions.reset_for_tests()


@contextlib.asynccontextmanager
async def _restarted_server(db_uri: str, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    """Serve a new app on the same database, with the old process's memory gone."""
    pending_elicitations.reset_for_tests()
    open_questions.reset_for_tests()
    artifact_store = LocalArtifactStore(str(tmp_path / "restarted-artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store, cache_dir=tmp_path / "restarted-cache"
        ),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as restarted:
        yield restarted


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


async def test_an_open_question_survives_a_server_restart_and_its_answer_arrives(
    client: httpx.AsyncClient, runner: _Runner, db_uri: str, tmp_path: Path
) -> None:
    session = (await create_test_session(client))["id"]
    question_id = await _ask(client, session, _question("Ship it?", "Yes", "No"))

    async with _restarted_server(db_uri, tmp_path) as restarted:
        listed = (await restarted.get("/v1/sessions")).json()["data"]
        assert [q["question_id"] for q in await _pending_questions(restarted, session)] == [
            question_id
        ]
        answered = await restarted.post(
            f"/v1/sessions/{session}/elicitations/{question_id}/resolve",
            json={"action": "accept", "content": {"Ship it?": "Yes"}},
        )
        assert answered.status_code == 202, answered.text
        assert runner.notices(session) == [f"[answer to {question_id}] Ship it?: Yes"]
        assert await _pending_questions(restarted, session) == []

    # The Inbox finds waiting sessions by their pending count in the session list.
    assert [row["pending_elicitations_count"] for row in listed if row["id"] == session] == [1]
    async with _restarted_server(db_uri, tmp_path) as restarted_again:
        assert await _pending_questions(restarted_again, session) == []


async def test_a_question_answered_before_a_restart_is_not_listed_after_it(
    client: httpx.AsyncClient, runner: _Runner, db_uri: str, tmp_path: Path
) -> None:
    session = (await create_test_session(client))["id"]
    answered = await _ask(client, session, _question("Ship it?", "Yes", "No"))
    still_open = await _ask(client, session, _question("Tag it?", "v1", "v2"))
    await client.post(
        f"/v1/sessions/{session}/events",
        json={"type": "approval", "data": {"elicitation_id": answered, "action": "decline"}},
    )

    async with _restarted_server(db_uri, tmp_path) as restarted:
        assert [q["question_id"] for q in await _pending_questions(restarted, session)] == [
            still_open
        ]
        again = await restarted.post(
            f"/v1/sessions/{session}/elicitations/{answered}/resolve",
            json={"action": "accept", "content": {"Ship it?": "Yes"}},
        )
    assert again.status_code == 202, again.text
    assert runner.notices(session) == [
        f"[answer to {answered}] Ship it?: (dismissed without an answer)"
    ]


async def test_a_session_holds_a_bounded_number_of_open_questions(
    client: httpx.AsyncClient,
) -> None:
    session = (await create_test_session(client))["id"]
    for index in range(MAX_OPEN_QUESTIONS):
        await _ask(client, session, _question(f"Question {index}?", "Yes", "No"))

    response = await client.post(
        f"/v1/sessions/{session}/questions",
        json={"questions": [_question("One more?", "Yes", "No")]},
    )

    assert response.status_code == 400, response.text
    assert len(await _pending_questions(client, session)) == MAX_OPEN_QUESTIONS


def _child_of(db_uri: str, parent: dict[str, Any]) -> str:
    return (
        SqlAlchemyConversationStore(db_uri)
        .create_conversation(
            kind="sub_agent",
            title="test-agent:helper",
            parent_conversation_id=parent["id"],
            agent_id=parent["agent_id"],
        )
        .id
    )


def _cards(snapshot: dict[str, Any]) -> list[tuple[str, str | None]]:
    return [
        (event["elicitation_id"], event["params"].get("target_session_id"))
        for event in snapshot["pending_elicitations"]
    ]


async def test_a_sub_agent_question_still_flags_its_parent_after_a_restart(
    client: httpx.AsyncClient, runner: _Runner, db_uri: str, tmp_path: Path
) -> None:
    parent = await create_test_session(client)
    child = _child_of(db_uri, parent)
    question_id = await _ask(client, child, _question("Child asks?", "Yes", "No"))

    async with _restarted_server(db_uri, tmp_path) as restarted:
        listed = (await restarted.get("/v1/sessions")).json()["data"]
        snapshot = (await restarted.get(f"/v1/sessions/{parent['id']}")).json()
        answered = await restarted.post(
            f"/v1/sessions/{child}/elicitations/{question_id}/resolve",
            json={"action": "accept", "content": {"Child asks?": "Yes"}},
        )

    assert [row["pending_elicitations_count"] for row in listed if row["id"] == parent["id"]] == [
        1
    ]
    assert _cards(snapshot) == [(question_id, child)]
    assert snapshot["pending_questions"] == []
    assert answered.status_code == 202, answered.text
    assert runner.notices(child) == [f"[answer to {question_id}] Child asks?: Yes"]
    async with _restarted_server(db_uri, tmp_path) as restarted_again:
        listed = (await restarted_again.get("/v1/sessions")).json()["data"]
        assert _cards((await restarted_again.get(f"/v1/sessions/{parent['id']}")).json()) == []
    assert [row["pending_elicitations_count"] for row in listed if row["id"] == parent["id"]] == [
        0
    ]


async def test_a_full_parent_listed_after_a_restart_still_shows_its_sub_agent_question(
    client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    parent = await create_test_session(client)
    child = _child_of(db_uri, parent)
    for index in range(MAX_OPEN_QUESTIONS):
        await _ask(client, parent["id"], _question(f"Question {index}?", "Yes", "No"))
    # The parent's own limit is reached; the copy of its child's card is still stored.
    question_id = await _ask(client, child, _question("Child asks?", "Yes", "No"))

    async with _restarted_server(db_uri, tmp_path) as restarted:
        # The Inbox lists sessions before any parent is opened.
        listed = (await restarted.get("/v1/sessions")).json()["data"]
        snapshot = (await restarted.get(f"/v1/sessions/{parent['id']}")).json()

    assert [row["pending_elicitations_count"] for row in listed if row["id"] == parent["id"]] == [
        MAX_OPEN_QUESTIONS + 1
    ]
    assert (question_id, child) in _cards(snapshot)
    assert len(snapshot["pending_questions"]) == MAX_OPEN_QUESTIONS
