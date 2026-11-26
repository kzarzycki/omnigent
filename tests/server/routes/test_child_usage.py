"""Tests for ``POST /v1/sessions/{id}/child-usage`` and its ``GET /v1/usage`` entries."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.llms.context_window import ModelPricing
from omnigent.runner.identity import (
    ORCHESTRATION_SOURCE_HEADER,
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.server.app import create_app
from omnigent.server.routes import usage as usage_routes
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

_AGENT = "1" * 32
_BUNDLE_HASH = "c" * 64
_TOKEN = "runner-binding-token"


class _AgentCacheStub:
    """The usage routes never load a bundle; creating the app only needs a cache."""

    def load(self, agent_id: str, bundle_location: str, **_: Any) -> SimpleNamespace:
        raise AssertionError("child usage must not load an agent bundle")


@pytest.fixture()
def stores(db_uri: str) -> SimpleNamespace:
    agents = SqlAlchemyAgentStore(db_uri)
    agents.create(_AGENT, "agent-1", f"{_AGENT}/{_BUNDLE_HASH}")
    return SimpleNamespace(agents=agents, conversations=SqlAlchemyConversationStore(db_uri))


def _app(stores: SimpleNamespace, tmp_path: Path) -> FastAPI:
    return create_app(
        agent_store=stores.agents,
        file_store=SqlAlchemyFileStore(stores.conversations.storage_location),
        conversation_store=stores.conversations,
        artifact_store=LocalArtifactStore(str(tmp_path / "artifacts")),
        agent_cache=_AgentCacheStub(),  # type: ignore[arg-type]
    )


@pytest_asyncio.fixture()
async def client(stores: SimpleNamespace, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=_app(stores, tmp_path))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _session(stores: SimpleNamespace) -> str:
    return stores.conversations.create_conversation(
        agent_id=_AGENT, runner_id=token_bound_runner_id(_TOKEN)
    ).id


def _from(session_id: str, token: str = _TOKEN) -> dict[str, str]:
    return {ORCHESTRATION_SOURCE_HEADER: session_id, RUNNER_TUNNEL_TOKEN_HEADER: token}


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "key": "verify-1",
        "harness": "codex",
        "model": "gpt-5.5",
        "input_tokens": 1000,
        "output_tokens": 200,
        "cost_usd": 0.5,
    }
    body.update(overrides)
    return body


async def _report_sessions(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    resp = await client.get("/v1/usage")
    assert resp.status_code == 200, resp.text
    return resp.json()["sessions"]


async def test_post_shows_as_child_entry_and_leaves_parent_totals(
    client: httpx.AsyncClient, stores: SimpleNamespace
) -> None:
    session_id = _session(stores)
    stores.conversations.set_session_usage(
        session_id,
        {"total_cost_usd": 2.0, "by_model": {"claude-opus": {"total_cost_usd": 2.0}}},
    )

    resp = await client.post(
        f"/v1/sessions/{session_id}/child-usage", json=_body(), headers=_from(session_id)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "object": "child_usage",
        "session_id": session_id,
        "child": {
            "key": "verify-1",
            "harness": "codex",
            "model": "gpt-5.5",
            "input_tokens": 1000,
            "output_tokens": 200,
            "total_tokens": 1200,
            "cost_usd": 0.5,
            "session_id": None,
        },
        "replaced": False,
    }
    [row] = await _report_sessions(client)
    assert row["id"] == session_id
    assert [(c["harness"], c["model"], c["cost_usd"]) for c in row["child_usage"]] == [
        ("codex", "gpt-5.5", 0.5)
    ]
    # The parent's own cost and model rows are unchanged.
    assert row["cost_usd"] == 2.0
    assert row["models"] == {"claude-opus": 2.0}


async def test_post_for_a_session_with_no_other_usage_leaves_it_unpriced(
    client: httpx.AsyncClient, stores: SimpleNamespace
) -> None:
    session_id = _session(stores)

    await client.post(
        f"/v1/sessions/{session_id}/child-usage", json=_body(), headers=_from(session_id)
    )

    [row] = await _report_sessions(client)
    assert row["cost_usd"] is None
    assert row["models"] == {}
    assert len(row["child_usage"]) == 1


async def test_post_for_another_session_is_refused(
    client: httpx.AsyncClient, stores: SimpleNamespace
) -> None:
    mine, other = _session(stores), _session(stores)

    resp = await client.post(
        f"/v1/sessions/{other}/child-usage", json=_body(), headers=_from(mine)
    )

    assert resp.status_code == 403, resp.text
    assert stores.conversations.get_conversation(other).session_usage == {}


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no-headers"),
        pytest.param({RUNNER_TUNNEL_TOKEN_HEADER: _TOKEN}, id="no-source-session"),
        pytest.param("wrong-token", id="wrong-token"),
    ],
)
async def test_post_without_a_valid_token_is_refused(
    client: httpx.AsyncClient, stores: SimpleNamespace, headers: Any
) -> None:
    session_id = _session(stores)
    if headers == "wrong-token":
        headers = _from(session_id, "someone-elses-token")

    resp = await client.post(
        f"/v1/sessions/{session_id}/child-usage", json=_body(), headers=headers
    )

    assert resp.status_code == 403, resp.text
    assert stores.conversations.get_conversation(session_id).session_usage == {}


async def test_repeated_key_counts_once(
    client: httpx.AsyncClient, stores: SimpleNamespace
) -> None:
    session_id = _session(stores)
    url, headers = f"/v1/sessions/{session_id}/child-usage", _from(session_id)

    first = await client.post(url, json=_body(), headers=headers)
    again = await client.post(url, json=_body(), headers=headers)
    revised = await client.post(url, json=_body(output_tokens=300, cost_usd=0.7), headers=headers)
    second_run = await client.post(url, json=_body(key="verify-2"), headers=headers)

    assert [r.json()["replaced"] for r in (first, again, revised, second_run)] == [
        False,
        True,
        True,
        False,
    ]
    [row] = await _report_sessions(client)
    by_key = {c["key"]: c for c in row["child_usage"]}
    assert set(by_key) == {"verify-1", "verify-2"}
    # The repeat replaced the entry; it did not add to it.
    assert by_key["verify-1"]["output_tokens"] == 300
    assert by_key["verify-1"]["cost_usd"] == 0.7


async def test_unpriced_total_only_report(
    client: httpx.AsyncClient, stores: SimpleNamespace
) -> None:
    session_id = _session(stores)

    resp = await client.post(
        f"/v1/sessions/{session_id}/child-usage",
        json={"key": "k", "harness": "agy", "total_tokens": 5000},
        headers=_from(session_id),
    )

    assert resp.status_code == 200, resp.text
    child = resp.json()["child"]
    assert (child["total_tokens"], child["cost_usd"], child["model"]) == (5000, None, None)


async def test_cost_is_priced_server_side_when_the_cli_gives_none(
    client: httpx.AsyncClient, stores: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnigent.llms.context_window as context_window

    monkeypatch.setattr(
        context_window,
        "fetch_model_pricing_with_provider",
        lambda model, provider_config=None, harness=None: ModelPricing(
            input_per_token=0.001, output_per_token=0.002
        ),
    )
    session_id = _session(stores)

    resp = await client.post(
        f"/v1/sessions/{session_id}/child-usage",
        json=_body(cost_usd=None),
        headers=_from(session_id),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["child"]["cost_usd"] == pytest.approx(1000 * 0.001 + 200 * 0.002)


@pytest.mark.parametrize(
    "overrides",
    [
        {"input_tokens": -1},
        {"cost_usd": -0.1},
        {"key": "has space"},
        {"key": ""},
        {"harness": ""},
        {"surprise": 1},
        {"input_tokens": None, "output_tokens": None, "cost_usd": None},
    ],
    ids=[
        "negative-tokens",
        "negative-cost",
        "bad-key",
        "empty-key",
        "no-harness",
        "extra",
        "empty",
    ],
)
async def test_invalid_body_is_rejected(
    client: httpx.AsyncClient, stores: SimpleNamespace, overrides: dict[str, Any]
) -> None:
    session_id = _session(stores)

    resp = await client.post(
        f"/v1/sessions/{session_id}/child-usage",
        json=_body(**overrides),
        headers=_from(session_id),
    )

    assert resp.status_code == 422, resp.text


async def test_child_run_cap(
    client: httpx.AsyncClient, stores: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(usage_routes, "MAX_CHILD_USAGE_PER_SESSION", 2)
    session_id = _session(stores)
    url, headers = f"/v1/sessions/{session_id}/child-usage", _from(session_id)

    codes = [
        (await client.post(url, json=_body(key=key), headers=headers)).status_code
        for key in ("a", "b", "c", "a")
    ]

    assert codes == [200, 200, 409, 200]
