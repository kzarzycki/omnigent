"""Parsing and applying ``os_env.sandbox.resources`` limits."""

from __future__ import annotations

import os
import subprocess
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from omnigent.errors import OmnigentError
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, SandboxResourcesSpec
from omnigent.inner.loader import _parse_os_env_sandbox_spec
from omnigent.inner.os_env_serialization import decode_sandbox_spec, encode_sandbox_spec
from omnigent.runner import session_limits
from omnigent.spec.parser import _parse_os_env_sandbox
from omnigent.spec.types import AgentSpec
from omnigent.util import user_systemd

_PARSERS = [_parse_os_env_sandbox_spec, _parse_os_env_sandbox]


@pytest.mark.parametrize("backend", ["linux_bwrap", "none"])
@pytest.mark.parametrize("parser", _PARSERS)
def test_resources_parse_with_and_without_bwrap(parser, backend: str) -> None:
    spec = parser(
        {
            "type": backend,
            "resources": {
                "memory_high": "3G",
                "memory_max": 4294967296,
                "memory_swap_max": "0",
                "cpu_weight": 50,
            },
        }
    )
    assert spec.resources == SandboxResourcesSpec(
        memory_high="3G", memory_max="4294967296", memory_swap_max="0", cpu_weight=50
    )
    assert spec.resources.systemd_properties() == {
        "MemoryHigh": "3G",
        "MemoryMax": "4294967296",
        "MemorySwapMax": "0",
        "CPUWeight": "50",
    }
    assert parser(asdict(spec)).resources == spec.resources
    assert decode_sandbox_spec(encode_sandbox_spec(spec)).resources == spec.resources


@pytest.mark.parametrize("parser", _PARSERS)
def test_resources_absent_by_default(parser) -> None:
    assert parser({"type": "linux_bwrap"}).resources is None


@pytest.mark.parametrize("backend", ["darwin_seatbelt", "windows_jobobject"])
@pytest.mark.parametrize("parser", _PARSERS)
def test_resources_refused_off_linux(parser, backend: str) -> None:
    with pytest.raises((ValueError, OmnigentError), match=rf"resources.*{backend}"):
        parser({"type": backend, "resources": {"memory_max": "4G"}})


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("4G", "must be a mapping"),
        ({"memory": "4G"}, "does not accept memory"),
        ({"memory_max": "4 GB"}, r"resources\.memory_max"),
        ({"memory_high": True}, r"resources\.memory_high"),
        ({"cpu_weight": 0}, r"resources\.cpu_weight"),
        ({"cpu_weight": "50"}, r"resources\.cpu_weight"),
    ],
)
@pytest.mark.parametrize("parser", _PARSERS)
def test_malformed_resources_fail_loud(parser, raw: object, message: str) -> None:
    with pytest.raises((ValueError, OmnigentError), match=message):
        parser({"type": "linux_bwrap", "resources": raw})


@pytest.fixture
def fresh_runner(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """A runner process that has not applied limits yet, with systemd faked."""
    monkeypatch.setattr(session_limits, "_applied", None)
    monkeypatch.setattr(session_limits, "_child_sessions", set())
    monkeypatch.setattr(session_limits, "IS_LINUX", True)
    monkeypatch.setenv("OMNIGENT_SESSION_SLICE", "agents.slice")
    monkeypatch.setattr(session_limits, "missing_user_manager_controllers", lambda needed: [])
    moves: list[dict[str, object]] = []

    def fake_move(pids: list[int], **kwargs: object) -> None:
        moves.append({"pids": pids, **kwargs})

    monkeypatch.setattr(session_limits, "move_into_new_slice", fake_move)
    return moves


def _spec(resources: SandboxResourcesSpec | None) -> SimpleNamespace:
    return SimpleNamespace(os_env=SimpleNamespace(sandbox=SimpleNamespace(resources=resources)))


def test_apply_moves_the_runner_into_a_slice_under_the_host_slice(
    fresh_runner: list[dict[str, object]],
) -> None:
    resources = SandboxResourcesSpec(memory_max="2G", cpu_weight=50)
    session_limits.apply_spec_session_limits("conv_primary", _spec(resources))
    session_limits.apply_spec_session_limits("conv_primary", _spec(resources))
    (move,) = fresh_runner
    assert str(move["slice_name"]).startswith("agents-convprimaryp")
    assert move["slice_properties"] == {"MemoryMax": "2G", "CPUWeight": "50"}


def test_apply_moves_children_started_before_the_limits(
    fresh_runner: list[dict[str, object]],
) -> None:
    """An MCP server discovered before init must not stay outside the slice."""
    child = subprocess.Popen(["sleep", "30"])
    try:
        session_limits.apply_session_limits("conv_primary", SandboxResourcesSpec(memory_max="2G"))
    finally:
        child.kill()
        child.wait()
    (move,) = fresh_runner
    assert move["pids"][0] == os.getpid()
    assert child.pid in move["pids"]


def test_apply_leaves_sub_agents_to_their_parents_limits(
    fresh_runner: list[dict[str, object]],
) -> None:
    session_limits.note_child_session("conv_child")
    session_limits.apply_session_limits("conv_child", SandboxResourcesSpec(memory_max="1G"))
    assert fresh_runner == []
    session_limits.apply_session_limits("conv_parent", SandboxResourcesSpec(memory_max="4G"))
    session_limits.apply_session_limits("conv_child", SandboxResourcesSpec(memory_max="1G"))
    assert len(fresh_runner) == 1


def test_apply_without_resources_does_nothing(fresh_runner: list[dict[str, object]]) -> None:
    session_limits.apply_session_limits("conv_primary", None)
    session_limits.apply_session_limits("conv_primary", SandboxResourcesSpec())
    session_limits.apply_spec_session_limits("conv_primary", SimpleNamespace(os_env=None))
    assert fresh_runner == []
    assert not session_limits.runner_has_session_slice()


def test_reused_runner_applies_a_switched_sessions_limits(
    fresh_runner: list[dict[str, object]],
) -> None:
    """``/switch`` from an unlimited session to a limited one limits the runner."""
    session_limits.apply_session_limits("conv_original", None)
    session_limits.apply_session_limits("conv_target", SandboxResourcesSpec(memory_max="500M"))
    (move,) = fresh_runner
    assert move["slice_properties"] == {"MemoryMax": "500M"}


def test_reused_runner_refuses_different_limits(fresh_runner: list[dict[str, object]]) -> None:
    """One runner carries one budget: a conflicting session is refused, not ignored."""
    session_limits.apply_session_limits("conv_original", SandboxResourcesSpec(memory_max="4G"))
    session_limits.apply_session_limits("conv_same", SandboxResourcesSpec(memory_max="4G"))
    session_limits.apply_session_limits("conv_unlimited", None)
    with pytest.raises(OmnigentError, match=r"MemoryMax=500M.*conv_original.*MemoryMax=4G"):
        session_limits.apply_session_limits("conv_target", SandboxResourcesSpec(memory_max="500M"))
    assert len(fresh_runner) == 1


def test_apply_refuses_on_macos(
    fresh_runner: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``type: none`` agent on macOS is refused, never run unlimited."""
    monkeypatch.setattr(session_limits, "IS_LINUX", False)
    with pytest.raises(OmnigentError, match="need Linux with a user systemd manager"):
        session_limits.apply_session_limits("conv_primary", SandboxResourcesSpec(memory_max="2G"))
    assert fresh_runner == []


@pytest.mark.parametrize(
    ("missing", "message"),
    [(None, "no user systemd manager"), (["memory"], "does not delegate the memory")],
)
def test_apply_refuses_without_a_capable_user_manager(
    fresh_runner: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    missing: list[str] | None,
    message: str,
) -> None:
    monkeypatch.setattr(session_limits, "missing_user_manager_controllers", lambda needed: missing)
    with pytest.raises(OmnigentError, match=message):
        session_limits.apply_session_limits("conv_primary", SandboxResourcesSpec(memory_max="2G"))


def test_apply_reports_a_refused_move(
    fresh_runner: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(pids: list[int], **kwargs: object) -> None:
        raise user_systemd.UserSystemdError("Access denied")

    monkeypatch.setattr(session_limits, "move_into_new_slice", refuse)
    with pytest.raises(OmnigentError, match="Access denied"):
        session_limits.apply_session_limits("conv_primary", SandboxResourcesSpec(memory_max="2G"))
    assert not session_limits.runner_has_session_slice()


def test_limited_runner_reports_its_slice_so_harnesses_skip_the_zygote(
    fresh_runner: list[dict[str, object]],
) -> None:
    """Zygote-forked harnesses would sit outside the session's slice."""
    assert not session_limits.runner_has_session_slice()
    session_limits.apply_session_limits("conv_primary", SandboxResourcesSpec(memory_max="2G"))
    assert session_limits.runner_has_session_slice()


class _ForbiddenZygote:
    async def fork_harness(self, argv: list[str], env: dict[str, str]) -> str:
        return "forked"


@pytest.mark.parametrize(("limited", "expected"), [(True, "exec"), (False, "forked")])
async def test_limited_runner_execs_its_harness_instead_of_forking(
    fresh_runner: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    limited: bool,
    expected: str,
) -> None:
    """A zygote-forked harness would live in the zygote's cgroup, outside the slice."""
    import asyncio

    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    if limited:
        session_limits.apply_session_limits("conv_primary", SandboxResourcesSpec(memory_max="2G"))
    manager = HarnessProcessManager()
    manager._harness_zygote = _ForbiddenZygote()

    async def fake_exec(*argv: str, **kwargs: object) -> str:
        return "exec"

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    assert await manager._spawn_harness_process(["--harness", "x"], {}) == expected


def _limited_spec(name: str, memory_max: str, **fields: object) -> AgentSpec:
    sandbox = OSEnvSandboxSpec(type="none", resources=SandboxResourcesSpec(memory_max=memory_max))
    return AgentSpec(
        spec_version=1,
        name=name,
        os_env=OSEnvSpec(type="caller_process", sandbox=sandbox),
        **fields,
    )


@pytest.mark.parametrize(
    ("envelope", "snapshot"),
    [({}, {"parent_session_id": "conv_parent"}), ({"sub_agent_name": "helper"}, {})],
    ids=["parent-only", "named-sub-agent"],
)
async def test_session_init_registers_a_child_under_its_parents_limits(
    fresh_runner: list[dict[str, object]],
    envelope: dict[str, object],
    snapshot: dict[str, object],
) -> None:
    """A child declaring its own budget joins the parent's runner without a conflict."""
    from tests.runner.conftest import _build_app_for_spec, _runner_client

    session_limits.apply_session_limits("conv_parent", SandboxResourcesSpec(memory_max="4G"))
    app, _ = await _build_app_for_spec(_limited_spec("child", "1G"))
    session_id, agent_id = "conv_child", "ag_child"
    payload = {
        "session_id": session_id,
        "agent_id": agent_id,
        # The runner requires the body to name the same sub-agent as the envelope.
        **envelope,
        "session_init": {
            "protocol_version": 2,
            "server_version": "0.6.0.dev0",
            "session_id": session_id,
            "agent_id": agent_id,
            **envelope,
            "snapshot": {"created_at": 1234, "updated_at": 1234, "labels": {}, **snapshot},
        },
    }
    async with _runner_client(app) as client:
        response = await client.post("/v1/sessions", json=payload)
    assert response.status_code == 201, response.text
    assert len(fresh_runner) == 1


@pytest.mark.parametrize(
    "snapshot",
    [{"sub_agent_name": "helper"}, {"parent_session_id": "conv_parent"}],
    ids=["named-sub-agent", "parent-only"],
)
async def test_first_turn_without_init_registers_a_child(
    fresh_runner: list[dict[str, object]], snapshot: dict[str, object]
) -> None:
    """After a reconnect a child's first turn arrives with no init; the server snapshot says
    it is a child, so it runs under the parent's limits instead of conflicting with them."""
    from omnigent.runner import create_runner_app
    from tests.runner.conftest import (
        _FakeProcessManager,
        _runner_client,
        _ScriptedHarnessClient,
        _sse,
    )
    from tests.runner.helpers import NullServerClient

    class _ChildSnapshotServer(NullServerClient):
        class _Resp:
            status_code = 200

            def __init__(self, payload: dict[str, object]) -> None:
                self._payload = payload

            def json(self) -> dict[str, object]:
                return self._payload

            def raise_for_status(self) -> None:
                return None

        async def get(self, url: str, **kwargs: object) -> object:
            if url.rstrip("/").endswith("conv_child"):
                return self._Resp({"agent_id": "ag_parent", "created_at": 0, **snapshot})
            if url.rstrip("/").endswith("/items"):
                return self._Resp({"data": [], "has_more": False})
            return super()._Response()

    child = _limited_spec("helper", "1G")
    parent = _limited_spec("parent", "4G", sub_agents=[child])

    async def _resolve(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return parent

    session_limits.apply_session_limits("conv_parent", SandboxResourcesSpec(memory_max="4G"))
    harness = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "r1"}}),
            _sse({"type": "response.completed", "response": {"id": "r1"}}),
        ]
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        spec_resolver=_resolve,
        server_client=_ChildSnapshotServer(),  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        response = await client.post(
            "/v1/sessions/conv_child/events",
            params={"stream": "true"},
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_parent",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        )
        body = response.text
    assert response.status_code == 200, body
    assert "differ from the limits" not in body
    assert "conv_child" in session_limits._child_sessions
    assert len(fresh_runner) == 1


async def test_harness_resolution_for_a_named_sub_agent_keeps_the_parents_limits(
    fresh_runner: list[dict[str, object]],
) -> None:
    """A caller that already knows the sub-agent's name (a background title request)
    resolves its harness without a recovery lookup, so the resolver registers it."""
    from omnigent.runner.app import _resolve_harness_config

    child = _limited_spec("helper", "1G")
    parent = _limited_spec("parent", "4G", sub_agents=[child])

    async def _resolve(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return parent

    session_limits.apply_session_limits("conv_parent", SandboxResourcesSpec(memory_max="4G"))
    await _resolve_harness_config(
        agent_id="ag_parent",
        spec_resolver=_resolve,
        session_id="conv_child",
        sub_agent_name="helper",
    )
    assert "conv_child" in session_limits._child_sessions
    assert len(fresh_runner) == 1


@pytest.mark.parametrize(
    ("sub_agent_name", "parent_session_id", "registered"),
    [
        ("helper", None, True),
        (None, "conv_parent", True),
        (None, None, False),
        (None, "conv_child", False),
    ],
    ids=["named-only", "parent-only", "top-level", "own-parent"],
)
async def test_snapshot_recovery_registers_children_by_name_or_parent(
    fresh_runner: list[dict[str, object]],
    sub_agent_name: str | None,
    parent_session_id: str | None,
    registered: bool,
) -> None:
    """Recovery registers the child itself, before any harness resolution could."""
    import httpx
    from fastapi import FastAPI

    from omnigent.runner.subagent_recovery import build_subagent_recovery

    async def _snapshot(session_id: str) -> SimpleNamespace:
        return SimpleNamespace(sub_agent_name=sub_agent_name, parent_session_id=parent_session_id)

    async with httpx.AsyncClient(base_url="http://server") as server_client:
        recovery = build_subagent_recovery(
            FastAPI(),
            _background_tasks=set(),
            _schedule_subagent_wake=lambda entry, *, is_rewake=False: None,
            _session_inboxes={},
            _session_snapshot=_snapshot,
            _session_sub_agent_names={},
            _subagent_recovery_tasks={},
            server_client=server_client,
        )
        assert await recovery.recover_sub_agent_name("conv_child") == sub_agent_name
    assert ("conv_child" in session_limits._child_sessions) is registered
