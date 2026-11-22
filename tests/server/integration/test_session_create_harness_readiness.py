"""Session creation refuses unavailable harnesses before persisting a child."""

import json

import httpx
import pytest
from fastapi import FastAPI

from tests.server.helpers import build_agent_bundle, create_test_agent

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("bundle", [False, True])
@pytest.mark.parametrize("readiness", [False, "binary-missing", "version-too-low", "absent"])
async def test_child_create_rejects_unavailable_harness(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    bundle: bool,
    readiness: bool | str,
) -> None:
    agent = await create_test_agent(client)
    parent = (await client.post("/v1/sessions", json={"agent_id": agent["id"]})).json()
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(parent["id"], "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", workspace="/tmp/workspace")
    store.set_runner_id(parent["id"], "runner_test")
    from omnigent.harness_availability import HarnessAvailability
    from omnigent.stores.host_store import HostStore

    report: dict[str, HarnessAvailability] = {"claude-sdk": True}
    if readiness != "absent":
        report["jcode"] = readiness
    host_store = HostStore(db_uri)
    host_store.upsert_on_connect(
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "test-host", "local", configured_harnesses=report
    )
    app.state.host_store = host_store
    before = store.list_conversations(limit=100).data
    if bundle:
        response = await client.post(
            "/v1/sessions",
            data={"metadata": json.dumps({"parent_session_id": parent["id"]})},
            files={
                "bundle": (
                    "agent.tar.gz",
                    build_agent_bundle(
                        "jcode-child",
                        executor={"type": "omnigent", "config": {"harness": "jcode"}},
                    ),
                    "application/gzip",
                )
            },
        )
    else:
        response = await client.post(
            "/v1/sessions",
            json={
                "agent_id": agent["id"],
                "parent_session_id": parent["id"],
                "harness_override": "jcode",
            },
        )
    assert response.status_code == 412, response.text
    assert response.json()["error"]["code"] == "harness_not_configured"
    assert "jcode" in response.json()["error"]["message"]
    assert len(store.list_conversations(limit=100).data) == len(before)


@pytest.mark.parametrize(
    "readiness",
    [
        None,
        {},
        {"jcode": True},
        {"jcode": "needs-auth"},
        {"jcode": "cli-unresponsive", "claude-sdk": True},
    ],
)
async def test_child_create_preserves_ready_and_unknown_hosts(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    readiness: dict | None,
) -> None:
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.host_store import HostStore

    agent = await create_test_agent(client)
    parent = (await client.post("/v1/sessions", json={"agent_id": agent["id"]})).json()
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(parent["id"], "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", workspace="/tmp/workspace")
    store.set_runner_id(parent["id"], "runner_test")
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "test-host", "local", configured_harnesses=readiness
    )
    app.state.host_store = hosts
    response = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": parent["id"],
            "harness_override": "jcode",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["harness"] == "jcode"


@pytest.mark.parametrize(
    ("harness", "expected_status"),
    [("devin-native", 412), ("jcode", 201)],
)
async def test_child_create_gates_only_the_tmux_backed_harness(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    harness: str,
    expected_status: int,
) -> None:
    """A host without usable tmux blocks its native harness with tmux repair steps."""
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.host_store import HostStore

    agent = await create_test_agent(client)
    parent = (await client.post("/v1/sessions", json={"agent_id": agent["id"]})).json()
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(parent["id"], "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", workspace="/tmp/workspace")
    store.set_runner_id(parent["id"], "runner_test")
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "test-host",
        "local",
        configured_harnesses={"devin-native": "tmux-unavailable", "jcode": True},
    )
    app.state.host_store = hosts
    response = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": parent["id"],
            "harness_override": harness,
        },
    )
    assert response.status_code == expected_status, response.text
    if expected_status == 412:
        error = response.json()["error"]
        assert error["code"] == "harness_not_configured"
        assert "tmux 3.3 or newer" in error["message"]
        assert "quit and reopen the Omnigent app" in error["message"]


@pytest.mark.parametrize("bundle", [False, True])
async def test_top_level_create_rejects_unavailable_harness(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    bundle: bool,
) -> None:
    from unittest.mock import AsyncMock

    from omnigent.server.routes import _session_create_validation
    from omnigent.server.routes._sessions import orchestration
    from omnigent.stores.host_store import HostStore

    monkeypatch.setattr(
        orchestration, "_validate_session_workspace", AsyncMock(return_value="/tmp/workspace")
    )

    monkeypatch.setattr(
        _session_create_validation,
        "validate_uploaded_bundle_host_workspace",
        AsyncMock(return_value="/tmp/workspace"),
    )
    agent = await create_test_agent(client)
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "test-host",
        "local",
        configured_harnesses={"jcode": False},
    )
    app.state.host_store = hosts
    metadata = {
        "host_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "workspace": "/tmp/workspace",
    }
    if bundle:
        response = await client.post(
            "/v1/sessions",
            data={"metadata": json.dumps(metadata)},
            files={
                "bundle": (
                    "agent.tar.gz",
                    build_agent_bundle(
                        "jcode-top", executor={"type": "omnigent", "config": {"harness": "jcode"}}
                    ),
                    "application/gzip",
                )
            },
        )
    else:
        response = await client.post(
            "/v1/sessions",
            json={**metadata, "agent_id": agent["id"], "harness_override": "jcode"},
        )
    assert response.status_code == 412, response.text
    assert response.json()["error"]["code"] == "harness_not_configured"


@pytest.mark.parametrize(
    ("named_host_ready", "expected_status"),
    [(False, 412), (True, 409)],
)
async def test_child_with_explicit_host_is_placed_and_checked_on_that_host(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    named_host_ready: bool,
    expected_status: int,
) -> None:
    from unittest.mock import AsyncMock

    from omnigent.server.routes._sessions import orchestration
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.host_store import HostStore

    monkeypatch.setattr(
        orchestration, "_validate_session_workspace", AsyncMock(return_value="/tmp/workspace")
    )
    parent_agent = await create_test_agent(client)
    jcode = await create_test_agent(
        client,
        name="jcode-worker",
        executor={"type": "omnigent", "config": {"harness": "jcode"}},
    )
    parent = (await client.post("/v1/sessions", json={"agent_id": parent_agent["id"]})).json()
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(parent["id"], "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", workspace="/tmp/workspace")
    store.set_runner_id(parent["id"], "runner_test")
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "parent-host",
        "local",
        configured_harnesses={"jcode": not named_host_ready},
    )
    hosts.upsert_on_connect(
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "other-host",
        "local",
        configured_harnesses={"jcode": named_host_ready},
    )
    app.state.host_store = hosts
    response = await client.post(
        "/v1/sessions",
        json={
            "agent_id": jcode["id"],
            "parent_session_id": parent["id"],
            "host_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "workspace": "/tmp/workspace",
        },
    )
    assert response.status_code == expected_status, response.text
    if expected_status == 412:
        assert response.json()["error"]["code"] == "harness_not_configured"
        return
    # Readiness passed on the named host; placement then tries it (not the parent's
    # runner) and finds it offline in this harness-less test app.
    assert response.json()["error"]["message"] == "host is offline"


async def test_child_naming_parent_host_keeps_parent_runner(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from omnigent.server.routes._sessions import orchestration
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    monkeypatch.setattr(
        orchestration, "_validate_session_workspace", AsyncMock(return_value="/tmp/workspace")
    )
    agent = await create_test_agent(client)
    parent = (await client.post("/v1/sessions", json={"agent_id": agent["id"]})).json()
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(parent["id"], "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", workspace="/tmp/workspace")
    store.set_runner_id(parent["id"], "runner_test")
    response = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": parent["id"],
            "host_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "workspace": "/tmp/workspace",
        },
    )
    assert response.status_code == 201, response.text
    assert store.get_conversation(response.json()["id"]).runner_id == "runner_test"


async def test_child_without_host_inherits_parent_runner(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from omnigent.server.routes._sessions import orchestration
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    monkeypatch.setattr(
        orchestration, "_validate_session_workspace", AsyncMock(return_value="/tmp/workspace")
    )
    agent = await create_test_agent(client)
    parent = (await client.post("/v1/sessions", json={"agent_id": agent["id"]})).json()
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(parent["id"], "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", workspace="/tmp/workspace")
    store.set_runner_id(parent["id"], "runner_test")
    response = await client.post(
        "/v1/sessions", json={"agent_id": agent["id"], "parent_session_id": parent["id"]}
    )
    assert response.status_code == 201, response.text
    assert store.get_conversation(response.json()["id"]).runner_id == "runner_test"


async def test_loaded_parent_does_not_require_another_session_read(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server.routes._session_harness_readiness import validate_create_harness_readiness
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.host_store import HostStore

    agent = await create_test_agent(client)
    parent_id = (await client.post("/v1/sessions", json={"agent_id": agent["id"]})).json()["id"]
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(parent_id, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", workspace="/tmp/workspace")
    parent = store.get_conversation(parent_id)
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "test-host",
        "local",
        configured_harnesses={"jcode": True},
    )

    def unexpected_read(session_id: str) -> None:
        raise AssertionError("Already-loaded parent must be reused")

    monkeypatch.setattr(store, "get_conversation", unexpected_read)
    await validate_create_harness_readiness(
        harness="jcode",
        host_id=None,
        parent_session_id=parent_id,
        inherited_runner_id="runner_test",
        user_id=None,
        conversation_store=store,
        host_store=hosts,
        parent=parent,
    )


@pytest.mark.parametrize("depth", [1, 16, 17, 32])
async def test_nested_parent_readiness_has_bounded_reads(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    depth: int,
) -> None:
    from unittest.mock import Mock

    from omnigent.errors import OmnigentError
    from omnigent.server.routes._session_harness_readiness import validate_create_harness_readiness
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.host_store import HostStore

    agent = await create_test_agent(client)
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation(
        agent_id=agent["id"],
        host_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        workspace="/tmp/workspace",
    )
    for _ in range(depth):
        parent = store.create_conversation(
            kind="sub_agent",
            parent_conversation_id=parent.id,
            agent_id=agent["id"],
            runner_id="runner_test",
        )
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "test-host",
        "local",
        configured_harnesses={"jcode": False},
    )
    read = Mock(wraps=store.get_conversation)
    monkeypatch.setattr(store, "get_conversation", read)
    validation = validate_create_harness_readiness(
        harness="jcode",
        host_id=None,
        parent_session_id=parent.id,
        inherited_runner_id="runner_test",
        user_id=None,
        conversation_store=store,
        host_store=hosts,
        parent=parent,
    )
    with pytest.raises(OmnigentError, match="not configured"):
        await validation
    assert read.call_count == min(depth, 16)


async def test_acp_agent_missing_from_host_report_is_refused_by_name(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """A host with other ACP agents but no ``claude-cloud`` entry refuses the create."""
    from omnigent.errors import OmnigentError
    from omnigent.server.routes._session_harness_readiness import validate_create_harness_readiness
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.host_store import HostStore

    host_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(
        host_id,
        "test-host",
        "local",
        configured_harnesses={"acp": True, "acp:other": True},
    )
    arguments = {
        "harness": "acp:claude-cloud",
        "host_id": host_id,
        "parent_session_id": None,
        "inherited_runner_id": None,
        "user_id": None,
        "conversation_store": SqlAlchemyConversationStore(db_uri),
        "host_store": hosts,
    }

    with pytest.raises(OmnigentError, match="claude-cloud"):
        await validate_create_harness_readiness(**arguments)

    hosts.upsert_on_connect(
        host_id,
        "test-host",
        "local",
        configured_harnesses={"acp": True, "acp:other": True, "acp:claude-cloud": True},
    )
    await validate_create_harness_readiness(**arguments)
