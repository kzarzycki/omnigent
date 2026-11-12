"""Blocked models and efforts are refused at every session start and switch."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from omnigent.server import model_block_list
from tests.server.helpers import build_agent_bundle, create_test_agent

BLOCKED_MODEL = "fixture-model-blocked"
ALLOWED_MODEL = "fixture-model-allowed"


@pytest.fixture()
def block(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Install a server block list for one test, e.g. ``block(blocked_efforts=["max"])``."""

    def _block(**config: Any) -> None:
        monkeypatch.setattr(
            model_block_list, "_current", model_block_list.parse_model_block_list(config)
        )

    return _block


def _assert_refused(resp: httpx.Response, entry: str, setting: str) -> None:
    assert resp.status_code == 400, resp.text
    message = resp.json()["error"]["message"]
    assert repr(entry) in message
    assert repr(setting) in message


@pytest.mark.parametrize(
    ("field", "value", "setting"),
    [
        ("model_override", BLOCKED_MODEL, "blocked_models"),
        ("model_override", BLOCKED_MODEL.upper() + "[1m]", "blocked_models"),
        ("reasoning_effort", "max", "blocked_efforts"),
    ],
)
@pytest.mark.parametrize("child", [False, True], ids=["api", "sub-agent"])
async def test_create_refuses_a_blocked_model_or_effort(
    client: httpx.AsyncClient,
    block: Callable[..., None],
    field: str,
    value: str,
    setting: str,
    child: bool,
) -> None:
    """
    The web picker, the API, ``sys_session_create`` and sub-agent spawns all
    create through ``POST /v1/sessions``; a child names its parent session.
    """
    agent = await create_test_agent(client)
    block(blocked_models=[BLOCKED_MODEL], blocked_efforts=["max"])
    body: dict[str, Any] = {"agent_id": agent["id"], field: value}
    if child:
        body["parent_session_id"] = agent["_session_id"]

    _assert_refused(await client.post("/v1/sessions", json=body), value, setting)

    allowed = {"model_override": ALLOWED_MODEL, "reasoning_effort": "high"}[field]
    resp = await client.post("/v1/sessions", json={**body, field: allowed})
    assert resp.status_code == 201, resp.text


@pytest.mark.parametrize(
    ("executor", "entry", "setting"),
    [
        ({"model": BLOCKED_MODEL}, BLOCKED_MODEL, "blocked_models"),
        ({"reasoning_effort": "max"}, "max", "blocked_efforts"),
    ],
)
async def test_create_refuses_a_blocked_default(
    client: httpx.AsyncClient,
    block: Callable[..., None],
    executor: dict[str, str],
    entry: str,
    setting: str,
) -> None:
    """A start that names nothing is refused rather than run on a blocked default."""
    agent = await create_test_agent(client, executor={"type": "omnigent", **executor})
    block(blocked_models=[BLOCKED_MODEL], blocked_efforts=["max"])

    resp = await client.post("/v1/sessions", json={"agent_id": agent["id"]})

    _assert_refused(resp, entry, setting)


async def test_bundle_upload_refuses_a_blocked_default(
    client: httpx.AsyncClient,
    block: Callable[..., None],
) -> None:
    """The CLI and bundled sub-agents create by upload; a blocked spec model is refused."""
    block(blocked_models=[BLOCKED_MODEL])

    resp = await client.post(
        "/v1/sessions",
        data={"metadata": "{}"},
        files={
            "bundle": (
                "agent.tar.gz",
                build_agent_bundle(
                    "bundled", executor={"type": "omnigent", "model": BLOCKED_MODEL}
                ),
                "application/gzip",
            )
        },
    )

    _assert_refused(resp, BLOCKED_MODEL, "blocked_models")


@pytest.mark.parametrize(
    ("field", "value", "setting"),
    [
        ("model_override", BLOCKED_MODEL, "blocked_models"),
        ("reasoning_effort", "max", "blocked_efforts"),
    ],
)
async def test_switch_refuses_a_blocked_model_or_effort(
    client: httpx.AsyncClient,
    block: Callable[..., None],
    field: str,
    value: str,
    setting: str,
) -> None:
    """The in-session picker and the API switch through the same PATCH."""
    agent = await create_test_agent(client)
    created = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "model_override": ALLOWED_MODEL, "reasoning_effort": "low"},
    )
    session_id = created.json()["id"]
    block(blocked_models=[BLOCKED_MODEL], blocked_efforts=["max"])

    resp = await client.patch(f"/v1/sessions/{session_id}", json={field: value})

    _assert_refused(resp, value, setting)
    snapshot = (await client.get(f"/v1/sessions/{session_id}")).json()
    assert (snapshot["model_override"], snapshot["reasoning_effort"]) == (ALLOWED_MODEL, "low")


async def test_a_running_session_keeps_an_entry_blocked_later(
    client: httpx.AsyncClient,
    block: Callable[..., None],
) -> None:
    """A block applies to the next start or switch, never to a running session."""
    agent = await create_test_agent(client)
    created = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "model_override": BLOCKED_MODEL, "reasoning_effort": "max"},
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    block(blocked_models=[BLOCKED_MODEL], blocked_efforts=["max"])

    renamed = await client.patch(f"/v1/sessions/{session_id}", json={"title": "still running"})

    assert renamed.status_code == 200, renamed.text
    snapshot = (await client.get(f"/v1/sessions/{session_id}")).json()
    assert (snapshot["model_override"], snapshot["reasoning_effort"]) == (BLOCKED_MODEL, "max")
