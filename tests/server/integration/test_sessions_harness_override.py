"""Integration tests for the session-scoped ``harness_override`` column.

Mirrors ``test_sessions_model_override.py``: create-time persistence,
snapshot read-back, runner-body forwarding (via a ``_get_runner_client``
capture stub), and fail-loud validation. ``harness_override`` is
create-time only — there is intentionally no PATCH path, since the
harness process spawns on the session's first turn.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


class _CaptureClient:
    """Runner-client stub that records the POSTed path + body.

    :param captured: Dict the test inspects after the route fires.
    """

    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    async def post(self, path: str, *, json: dict[str, Any], **_: Any) -> Any:
        """Record the path + body and return a fake 202 response."""
        self._captured["path"] = path
        self._captured["body"] = json
        self._captured.setdefault("posts", []).append((path, json))

        class _Resp:
            status_code = 202
            headers: dict[str, str] = {}
            text = ""

        return _Resp()

    async def get(self, *_: Any, **__: Any) -> Any:
        raise NotImplementedError


def _stub_runner_client(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``_get_runner_client`` to return a capturing stub.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: A dict the test inspects after the runner POST runs;
        contains ``path`` and ``body`` for the last POST plus a ``posts``
        list of every ``(path, body)`` pair, once the route fires.
    """
    from omnigent.server.routes import sessions as sessions_mod

    captured: dict[str, Any] = {}

    async def _stub(*_: Any, **__: Any) -> _CaptureClient:
        return _CaptureClient(captured)

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _stub)
    return captured


def _stub_late_runner_bind(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Accept any ``PATCH {runner_id}`` bind and capture the runner POSTs.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: The :func:`_stub_runner_client` capture dict.
    """
    from omnigent.server.routes import sessions as sessions_mod

    async def _skip_relay_readiness(*_: Any, **__: Any) -> None:
        return None

    def _accept_runner_id(_router: Any, raw: str, *, user_id: Any = None) -> str:
        del user_id
        return raw.strip()

    monkeypatch.setattr(sessions_mod, "_ensure_runner_relay_ready", _skip_relay_readiness)
    monkeypatch.setattr(sessions_mod, "_registered_runner_id", _accept_runner_id)
    return _stub_runner_client(monkeypatch)


@pytest.mark.parametrize("override", ["pi", "acp:goose"])
async def test_create_with_harness_override_persists_and_snapshot_reflects(
    client: httpx.AsyncClient,
    override: str,
) -> None:
    """Create-time ``harness_override`` makes the snapshot report it.

    The test bundle declares ``executor.config.harness: claude-sdk``;
    after creating with an override the snapshot's
    ``harness`` must match it — what the runner will actually spawn —
    not the spec's declared value. This is the contract the new-chat
    composer and the attach banner rely on.
    """
    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "initial_items": [], "harness_override": override},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body.get("harness") == override, (
        f"Create response harness is {body.get('harness')!r}, expected {override!r}. "
        f"If this is 'claude-sdk', _resolve_harness ignored the persisted "
        f"override and reported the spec default."
    )

    get = await client.get(f"/v1/sessions/{body['id']}")
    assert get.status_code == 200
    assert get.json().get("harness") == override, (
        "GET snapshot lost the harness override — the column did not "
        "persist or _resolve_harness stopped preferring it."
    )
    listed = (await client.get("/v1/sessions")).json()["data"]
    assert next(item for item in listed if item["id"] == body["id"])["harness"] == override


async def test_create_without_harness_override_reports_spec_default(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No override → the snapshot reports the spec's declared harness.

    Guards the NULL-means-track-the-spec semantics: an un-overridden
    session must keep following the agent bundle's declared harness.
    """
    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "initial_items": []},
    )
    assert resp.status_code == 201, resp.text
    # The test bundle declares claude-sdk (see build_agent_bundle).
    assert resp.json().get("harness") == "claude-sdk"
    override = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "initial_items": [], "harness_override": "pi"},
    )
    assert override.status_code == 201, override.text
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore

    def reject_single_agent_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("The session list must batch its agent metadata reads")

    monkeypatch.setattr(SqlAlchemyAgentStore, "get", reject_single_agent_read)
    listed = (await client.get("/v1/sessions")).json()["data"]
    assert next(item for item in listed if item["id"] == resp.json()["id"])["harness"] == (
        "claude-sdk"
    )

    def unavailable_bundle(*args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("Artifact service unavailable")

    monkeypatch.setattr(AgentCache, "load", unavailable_bundle)
    response = await client.get("/v1/sessions")
    assert response.status_code == 200
    listed_by_id = {item["id"]: item for item in response.json()["data"]}
    assert "harness" not in listed_by_id[resp.json()["id"]]
    assert listed_by_id[override.json()["id"]]["harness"] == "pi"


async def test_create_harness_override_alias_canonicalizes(
    client: httpx.AsyncClient,
) -> None:
    """The ``openai-agents-sdk`` alias persists as canonical ``openai-agents``.

    A raw alias on the row would miss the runner's harness-module
    registry at dispatch (it keys on canonical names).
    """
    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "initial_items": [],
            "harness_override": "openai-agents-sdk",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json().get("harness") == "openai-agents"


async def test_create_rejects_unknown_harness_override(
    client: httpx.AsyncClient,
) -> None:
    """An unknown harness fails loud at create — no orphan session row."""
    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "initial_items": [], "harness_override": "bogus"},
    )
    assert resp.status_code == 400, (
        f"Unknown harness_override should 400, got {resp.status_code}: {resp.text}"
    )
    assert "bogus" in resp.text


@pytest.mark.parametrize("override", ["pi", "acp:goose"])
async def test_create_rejects_harness_override_for_non_omnigent_agent(
    client: httpx.AsyncClient,
    override: str,
) -> None:
    """Non-omnigent executor types reject the override instead of no-opping.

    Mirrors the CLI's ``--harness`` rule: those executors have no
    ``config.harness``, so accepting the value would silently launch the
    spec's own executor while the snapshot claimed otherwise.
    """
    agent = await create_test_agent(
        client,
        name="sdk-typed-agent",
        # A non-omnigent executor type (claude_sdk rejects the helper's
        # executor.connection, so agents_sdk is the representative here);
        # the helper still injects config.harness, which this executor
        # type simply ignores.
        executor={"type": "agents_sdk", "model": "databricks-gpt-5-4"},
    )
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "initial_items": [], "harness_override": override},
    )
    assert resp.status_code == 400, (
        f"harness_override on an agents_sdk-typed agent should 400, got "
        f"{resp.status_code}: {resp.text}"
    )
    assert "agents_sdk" in resp.text


@pytest.mark.parametrize("override", ["pi", "acp:goose"])
async def test_runner_first_event_forwards_harness_override(
    client: httpx.AsyncClient,
    override: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A create-time override reaches the runner body on the first event.

    The runner reads ``harness_override`` from the message body in
    ``_resolve_harness_config`` / the TurnDispatch builder — if the
    server drops it here, the session silently runs the spec's declared
    harness while the snapshot claims the override.
    """
    captured = _stub_runner_client(monkeypatch)

    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "initial_items": [], "harness_override": override},
    )
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]

    event = await client.post(
        f"/v1/sessions/{sid}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "first turn"}],
            },
        },
    )
    assert event.status_code == 202, event.text

    assert captured.get("body") is not None, (
        "Runner client was never POSTed to — _forward_event_to_runner "
        "did not run. Check the runner-stub wiring."
    )
    assert captured["body"].get("harness_override") == override, (
        f"First-event runner body missing the create-time harness "
        f"override; got {captured['body'].get('harness_override')!r}. The "
        f"create route did not persist harness_override before the first "
        f"turn, or the forwarding site dropped it."
    )


@pytest.mark.parametrize("override", ["pi", "acp:goose"])
async def test_create_session_init_carries_harness_override(
    client: httpx.AsyncClient,
    override: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The create route's runner notification must carry the override.

    With ``initial_items`` the kickoff turn is forwarded before this
    notification lands. A notification that omits the session-init envelope
    leaves the runner resolving the harness from the spec, so init evicts the
    override harness the kickoff turn is already streaming through.
    """
    from omnigent.server.routes import sessions as sessions_mod

    captured = _stub_runner_client(monkeypatch)

    async def _skip_relay_readiness(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(sessions_mod, "_ensure_runner_relay_ready", _skip_relay_readiness)

    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "harness_override": override,
            "initial_items": [
                {
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "kickoff"}],
                    },
                }
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    init_posts = [body for path, body in captured.get("posts", []) if path == "/v1/sessions"]
    assert init_posts, (
        "The create route never notified the runner about the new session — "
        "check the runner-stub wiring."
    )
    snapshot = init_posts[-1].get("session_init", {}).get("snapshot", {})
    assert snapshot.get("harness_override") == override, (
        f"Session-init notification lost the create-time harness override; "
        f"got {init_posts[-1]!r}. The runner then resolves the harness from "
        f"the spec and spawns that one instead."
    )
    assert init_posts[-1].get("session_init", {}).get("suppress_recovery_turn") is True, (
        f"The create-time init must suppress the runner's recovery turn — the "
        f"kickoff was already forwarded, so recovery would run it twice; got "
        f"{init_posts[-1]!r}."
    )


@pytest.mark.parametrize("override", ["pi", "acp:goose"])
async def test_patch_rebind_init_carries_harness_override(
    client: httpx.AsyncClient,
    override: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PATCH rebind's runner notification must carry the override.

    The external-host launch binds a runner after create via
    ``PATCH {runner_id}``; the recovery turn that then executes any seeded
    ``initial_items`` resolves its harness from session init. A bare legacy
    body loses the override, so the kickoff runs on the spec's harness.
    """
    captured = _stub_late_runner_bind(monkeypatch)

    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "harness_override": override},
    )
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]
    captured.pop("posts", None)

    patch = await client.patch(f"/v1/sessions/{sid}", json={"runner_id": "runner_bound_late"})
    assert patch.status_code == 200, patch.text

    init_posts = [body for path, body in captured.get("posts", []) if path == "/v1/sessions"]
    assert init_posts, (
        "The PATCH rebind never notified the runner about the session — "
        "check the runner-stub wiring."
    )
    snapshot = init_posts[-1].get("session_init", {}).get("snapshot", {})
    assert snapshot.get("harness_override") == override, (
        f"Rebind session-init notification lost the harness override; got "
        f"{init_posts[-1]!r}. The runner then resolves the harness from the "
        f"spec, and the kickoff recovery turn runs on the wrong harness."
    )
    assert not init_posts[-1].get("session_init", {}).get("suppress_recovery_turn"), (
        f"The rebind init must keep recovery enabled — on rebind the recovery "
        f"turn is what runs the pending seeded kickoff; got {init_posts[-1]!r}."
    )


async def test_patch_bind_side_chat_init_suppresses_recovery_turn(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copied sub-agent completion must not start a side-chat recovery turn."""
    captured = _stub_late_runner_bind(monkeypatch)

    agent = await create_test_agent(client)
    source = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert source.status_code == 201, source.text
    notice = "[System: sub-agent worker finished. Summarize its result.]"
    seeded = await client.post(
        f"/v1/sessions/{source.json()['id']}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": notice}],
                },
            },
        },
    )
    assert seeded.status_code in (200, 202), seeded.text
    fork = await client.post(
        f"/v1/sessions/{source.json()['id']}/fork",
        json={"title": "Side chat", "side_chat": True},
    )
    assert fork.status_code == 201, fork.text
    items = await client.get(f"/v1/sessions/{fork.json()['id']}/items")
    assert items.status_code == 200, items.text
    assert notice in items.text
    captured.pop("posts", None)

    patch = await client.patch(
        f"/v1/sessions/{fork.json()['id']}", json={"runner_id": "runner_bound_late"}
    )
    assert patch.status_code == 200, patch.text

    init_posts = [body for path, body in captured.get("posts", []) if path == "/v1/sessions"]
    assert init_posts, "The side-chat bind never notified the runner about the session."
    assert init_posts[-1].get("session_init", {}).get("suppress_recovery_turn") is True, (
        f"The side-chat bind init must suppress the recovery turn; got {init_posts[-1]!r}."
    )


async def test_runner_body_omits_harness_override_when_unset(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No override → the runner body carries no ``harness_override`` key.

    The runner treats a present key as an explicit override; sending the
    spec default redundantly would defeat NULL-means-track-the-spec when
    bundles update between turns.
    """
    captured = _stub_runner_client(monkeypatch)

    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "initial_items": []},
    )
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]

    event = await client.post(
        f"/v1/sessions/{sid}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        },
    )
    assert event.status_code == 202, event.text
    assert captured.get("body") is not None
    assert "harness_override" not in captured["body"]


async def test_acp_selection_does_not_require_server_configuration(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("{}\n")
    agent = await create_test_agent(client)
    response = await client.post(
        "/v1/sessions", json={"agent_id": agent["id"], "harness_override": "acp:runner-only"}
    )
    assert response.status_code == 201, response.text
    assert response.json()["harness"] == "acp:runner-only"


@pytest.mark.parametrize("override", ["acp:", "acp:Goose", "acp:goose/other", "acp: goose"])
async def test_rejects_malformed_acp_selection(client: httpx.AsyncClient, override: str) -> None:
    agent = await create_test_agent(client)
    response = await client.post(
        "/v1/sessions", json={"agent_id": agent["id"], "harness_override": override}
    )
    assert response.status_code == 400, response.text
    assert "invalid ACP agent identifier" in response.text


@pytest.mark.parametrize("extra", [0, 1, 100])
@pytest.mark.parametrize("with_other_overrides", [False, True])
@pytest.mark.parametrize("seed_effort", [False, True])
async def test_override_storage_limit_is_checked_before_create(
    client: httpx.AsyncClient, extra: int, with_other_overrides: bool, seed_effort: bool
) -> None:
    import json

    from omnigent.db.db_models import SqlConversation
    from omnigent.runtime import get_conversation_store

    executor = {"type": "omnigent", "config": {"harness": "claude-sdk"}}
    if seed_effort:
        executor["reasoning_effort"] = "high"
    agent = await create_test_agent(client, executor=executor)
    overrides = {"harness_override": "acp:"}
    if seed_effort:
        overrides["reasoning_effort"] = "high"
    if with_other_overrides:
        overrides.update(
            {
                "model_override": "model-" + "a" * 120,
                "reasoning_effort": "high",
                "cost_control_mode_override": "off",
                "subagent_routing_override": "on",
            }
        )
    limit = SqlConversation.__table__.c.session_overrides.type.length
    overhead = len(json.dumps(overrides, separators=(",", ":")))
    overrides["harness_override"] += "a" * (limit - overhead + extra)
    store = get_conversation_store()
    before = {conv.id for conv in store.list_conversations().data}
    requested = dict(overrides)
    if seed_effort:
        requested.pop("reasoning_effort", None)
    response = await client.post("/v1/sessions", json={"agent_id": agent["id"], **requested})
    after = {conv.id for conv in store.list_conversations().data}
    if extra:
        assert after == before, "Oversized input left a persisted session"
        assert response.status_code == 400, response.text
        assert "session overrides" in response.text.lower()
    else:
        assert response.status_code == 201, response.text
        assert after - before == {response.json()["id"]}
        assert response.json()["harness"] == overrides["harness_override"]


async def test_override_growth_rejects_patch_without_mutation(client: httpx.AsyncClient) -> None:
    import json

    from omnigent.db.db_models import SqlConversation
    from omnigent.runtime import get_conversation_store

    agent = await create_test_agent(client)
    limit = SqlConversation.__table__.c.session_overrides.type.length
    harness = "acp:" + "a" * (
        limit - len(json.dumps({"harness_override": "acp:"}, separators=(",", ":")))
    )
    created = await client.post(
        "/v1/sessions", json={"agent_id": agent["id"], "harness_override": harness}
    )
    assert created.status_code == 201, created.text
    session = created.json()["id"]
    response = await client.patch(
        f"/v1/sessions/{session}",
        json={"model_override": "another-model", "title": "must not persist"},
    )
    assert response.status_code == 400, response.text
    assert "session overrides" in response.text.lower()
    conv = get_conversation_store().get_conversation(session)
    assert conv.harness_override == harness
    assert conv.model_override is None
    assert conv.title != "must not persist"


async def _install_unpinned_agent(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    executor_config: dict[str, str] | None = None,
) -> str:
    """Install an agent whose spec names no model and, by default, no harness.

    :param client: Test HTTP client.
    :param monkeypatch: Clears the single-user marker: this unauthenticated app
        stamps no owner on installs, and the marker maps the creator to ``local``.
    :param executor_config: Extra ``executor.config`` keys, e.g. ``{"permission_mode": "plan"}``.
    :returns: The installed agent's id.
    """
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER", raising=False)
    import yaml

    from tests._helpers.session import bundle_files

    config = {
        "spec_version": 1,
        "name": "portable-reviewer",
        "instructions": "Review the change.",
        "executor": {"type": "omnigent", "config": dict(executor_config or {})},
    }
    bundle = bundle_files({"config.yaml": yaml.safe_dump(config).encode()})
    installed = await client.post(
        "/v1/agents", files={"bundle": ("agent.tar.gz", bundle, "application/gzip")}
    )
    assert installed.status_code in (200, 201), installed.text
    return installed.json()["id"]


async def test_unpinned_spec_starts_on_the_requested_harness_and_model(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spec without harness or model runs on what the create request names."""
    agent_id = await _install_unpinned_agent(client, monkeypatch)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent_id, "harness_override": "pi", "model_override": "model-a"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["harness"] == "pi"
    session = (await client.get(f"/v1/sessions/{resp.json()['id']}")).json()
    assert session["model_override"] == "model-a"
    # Both agent views report no harness for the portable spec, not "omnigent".
    catalog = (await client.get("/v1/agents", params={"scope": "user"})).json()["data"]
    assert next(item for item in catalog if item["id"] == agent_id)["harness"] is None
    bound = (await client.get(f"/v1/sessions/{resp.json()['id']}/agent")).json()
    assert bound["harness"] is None


@pytest.mark.parametrize(
    ("user_config", "request_model", "expected"),
    [
        ({"harness": {"default": "pi"}, "model": "model-b"}, None, ("pi", "model-b")),
        ({"harness": "codex-native"}, None, ("codex-native", None)),
        # The request's model outranks the user's default model.
        ({"harness": "pi", "model": "model-b"}, "model-req", ("pi", "model-req")),
    ],
)
async def test_unpinned_spec_falls_back_to_the_users_configured_defaults(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    user_config: dict[str, Any],
    request_model: str | None,
    expected: tuple[str, str | None],
) -> None:
    """With no override, a single-user server starts on the user's config defaults.

    Every create-time check sees that default, as if the request had named it.
    """
    import yaml

    from omnigent.runtime import get_conversation_store
    from omnigent.server.routes._sessions import orchestration

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(user_config))
    agent_id = await _install_unpinned_agent(client, monkeypatch)
    checked: list[str | None] = []
    first_check = orchestration._reject_ungatewayed_model_routing

    async def _record_first_check(body: Any, *args: Any) -> None:
        checked.append(body.harness_override)
        await first_check(body, *args)

    monkeypatch.setattr(orchestration, "_reject_ungatewayed_model_routing", _record_first_check)
    requested = {"agent_id": agent_id}
    if request_model is not None:
        requested["model_override"] = request_model
    resp = await client.post("/v1/sessions", json=requested)
    assert resp.status_code == 201, resp.text
    conv = get_conversation_store().get_conversation(resp.json()["id"])
    assert (conv.harness_override, conv.model_override) == expected
    assert checked == [expected[0]]


async def test_unpinned_spec_uses_the_configuration_saved_to_the_server(
    app: Any,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A saved configuration reaches session create through the app's preference store."""
    import yaml

    from omnigent.configuration_profile import ConfigurationSnapshot, PortableConfiguration
    from omnigent.preference_sync import PreferenceNamespace
    from omnigent.runtime import get_conversation_store

    class _SavedConfiguration:
        def read_configuration(self, *, user_id: str | None) -> ConfigurationSnapshot:
            return ConfigurationSnapshot(
                identity=PreferenceNamespace(authority_id="a", user_id="u", workspace_id="1"),
                configuration=PortableConfiguration(default_harness="codex-native", model="m"),
            )

    # The server's own config names another harness; the saved one wins.
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"harness": "pi"}))
    monkeypatch.setattr(app.state, "preference_store", _SavedConfiguration())
    agent_id = await _install_unpinned_agent(client, monkeypatch)
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id})
    assert resp.status_code == 201, resp.text
    conv = get_conversation_store().get_conversation(resp.json()["id"])
    assert (conv.harness_override, conv.model_override) == ("codex-native", "m")


async def test_create_app_hands_session_create_its_preference_store(
    runtime_init: None, db_uri: str, tmp_path: Path
) -> None:
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.server.preference_requests import PreferencePolicy
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    store, policy = object(), PreferencePolicy()
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        preference_store=store,  # type: ignore[arg-type]
        preference_policy=policy,
    )
    assert (app.state.preference_store, app.state.preference_policy) == (store, policy)


async def test_unpinned_named_sub_agent_takes_the_session_harness(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A harness-less sub-agent started on claude-native gets the native terminal labels."""
    import yaml

    from omnigent.runtime import get_conversation_store
    from tests._helpers.session import bundle_files

    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER", raising=False)
    parent = {
        "spec_version": 1,
        "name": "lead",
        "instructions": "Lead.",
        "executor": {"config": {"harness": "claude-sdk"}},
        "tools": {"agents": ["worker"]},
    }
    worker = {"spec_version": 1, "name": "worker", "executor": {"type": "omnigent", "config": {}}}
    bundle = bundle_files(
        {
            "config.yaml": yaml.safe_dump(parent).encode(),
            "agents/worker/config.yaml": yaml.safe_dump(worker).encode(),
        }
    )
    installed = await client.post(
        "/v1/agents", files={"bundle": ("agent.tar.gz", bundle, "application/gzip")}
    )
    assert installed.status_code == 200, installed.text
    agent_id = installed.json()["id"]
    lead = await client.post("/v1/sessions", json={"agent_id": agent_id})
    assert lead.status_code == 201, lead.text
    child = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_id,
            "parent_session_id": lead.json()["id"],
            "sub_agent_name": "worker",
            "harness_override": "claude-native",
        },
    )
    assert child.status_code == 201, child.text
    conv = get_conversation_store().get_conversation(child.json()["id"])
    assert conv.harness_override == "claude-native"
    assert conv.labels.get("omnigent.ui") == "terminal"


async def test_uploaded_bundle_without_harness_is_refused(client: httpx.AsyncClient) -> None:
    """An upload has no override field, so a harness-less bundle is refused up front."""
    import json

    import yaml

    from omnigent.runtime import get_conversation_store
    from tests._helpers.session import bundle_files

    config = {"spec_version": 1, "name": "portable", "executor": {"config": {}}}
    bundle = bundle_files({"config.yaml": yaml.safe_dump(config).encode()})
    store = get_conversation_store()
    before = {conv.id for conv in store.list_conversations().data}
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    assert resp.status_code == 400, resp.text
    assert "names no harness" in resp.text
    assert {conv.id for conv in store.list_conversations().data} == before


async def test_unpinned_spec_without_any_default_is_refused(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Nothing names a harness: the create fails before any session exists."""
    from omnigent.runtime import get_conversation_store

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    agent_id = await _install_unpinned_agent(client, monkeypatch)
    store = get_conversation_store()
    before = {conv.id for conv in store.list_conversations().data}
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id})
    assert resp.status_code == 400, resp.text
    assert "names no harness" in resp.text
    assert {conv.id for conv in store.list_conversations().data} == before


@pytest.mark.parametrize(
    ("spec_harness", "harness", "expected_args"),
    [
        (None, "claude-native", ["--permission-mode", "plan"]),
        # A Claude-only key must not reach codex: ``--permission-mode`` is not a
        # codex flag, and codex would exit before its thread starts.
        (None, "codex-native", None),
        ("claude-native", "codex-native", None),
    ],
)
async def test_spec_claude_keys_follow_the_session_harness(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    spec_harness: str | None,
    harness: str,
    expected_args: list[str] | None,
) -> None:
    """``permission_mode`` translates for the harness the session actually runs."""
    from omnigent.runtime import get_conversation_store

    executor_config = {"permission_mode": "plan"}
    if spec_harness is not None:
        executor_config["harness"] = spec_harness
    agent_id = await _install_unpinned_agent(client, monkeypatch, executor_config)
    resp = await client.post(
        "/v1/sessions", json={"agent_id": agent_id, "harness_override": harness}
    )
    assert resp.status_code == 201, resp.text
    conv = get_conversation_store().get_conversation(resp.json()["id"])
    assert conv.terminal_launch_args == expected_args


async def test_routing_guards_see_the_users_default_harness_for_an_unpinned_spec(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Smart Routing's create-time guards run on the harness the session will run on."""
    import yaml

    from omnigent.server.routes._sessions import orchestration

    seen: list[str | None] = []
    real = orchestration._fixed_native_routing_harness

    def spy(body: Any, agent: Any, agent_cache: Any) -> str | None:
        seen.append(body.harness_override)
        return real(body, agent, agent_cache)

    monkeypatch.setattr(orchestration, "_fixed_native_routing_harness", spy)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"harness": "codex-native"}))
    agent_id = await _install_unpinned_agent(client, monkeypatch)
    await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_id,
            "cost_control_mode_override": "on",
            "smart_routing_message": "hello",
        },
    )
    assert seen == ["codex-native"]


async def test_default_model_yields_to_the_spec_and_the_request(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The user's default model fills a gap only; it never replaces a named model."""
    import yaml

    from omnigent.runtime import get_conversation_store

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"harness": "pi", "model": "model-default"})
    )
    agent_id = await _install_unpinned_agent(client, monkeypatch)
    resp = await client.post(
        "/v1/sessions", json={"agent_id": agent_id, "model_override": "model-request"}
    )
    assert resp.status_code == 201, resp.text
    conv = get_conversation_store().get_conversation(resp.json()["id"])
    assert (conv.harness_override, conv.model_override) == ("pi", "model-request")
