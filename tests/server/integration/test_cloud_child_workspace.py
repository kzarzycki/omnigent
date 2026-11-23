"""A child created with its parent's workspace keeps it, so its runner starts there."""

import httpx
import pytest

from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def test_a_child_create_keeps_the_workspace_it_names(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    agent = await create_test_agent(client)
    parent = (await client.post("/v1/sessions", json={"agent_id": agent["id"]})).json()
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(parent["id"], "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", workspace="/work/parent")
    store.set_runner_id(parent["id"], "runner_test")

    response = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": parent["id"],
            "workspace": "/work/parent",
        },
    )

    assert response.status_code == 201, response.text
    child = store.get_conversation(response.json()["id"])
    assert child is not None
    assert child.workspace == "/work/parent"
