"""Config parsing and bypass detection for the machine principal's session grant."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from omnigent.server.machine_access import (
    ACTS_FOR_ENV,
    ALLOW_CLOUD_SESSIONS_ENV,
    ALLOWED_AGENTS_ENV,
    MachineSessionAccess,
    launch_args_bypass_permissions,
)
from omnigent.server.routes._host_launch import resolve_host_owner
from tests.server.routes.test_host_launch import _FakeHost, _FakeHostStore

_MACHINE = "machine@example.test"


def test_unset_leaves_the_grant_off(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (ACTS_FOR_ENV, ALLOWED_AGENTS_ENV, ALLOW_CLOUD_SESSIONS_ENV):
        monkeypatch.delenv(name, raising=False)
    assert MachineSessionAccess.from_env(_MACHINE) is None


def test_reads_user_agents_and_cloud_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ACTS_FOR_ENV, "alice@example.test")
    monkeypatch.setenv(ALLOWED_AGENTS_ENV, " alpha , beta,,")
    monkeypatch.setenv(ALLOW_CLOUD_SESSIONS_ENV, "1")
    access = MachineSessionAccess.from_env(_MACHINE)
    assert access == MachineSessionAccess(
        machine_sub=_MACHINE,
        acts_for="alice@example.test",
        allowed_agents=frozenset({"alpha", "beta"}),
        allow_cloud_sessions=True,
    )


@pytest.mark.parametrize(
    "env",
    [
        {ALLOWED_AGENTS_ENV: "alpha"},
        {ALLOW_CLOUD_SESSIONS_ENV: "1"},
        {ACTS_FOR_ENV: "alice@example.test"},
        {ACTS_FOR_ENV: _MACHINE, ALLOWED_AGENTS_ENV: "alpha"},
        {ACTS_FOR_ENV: "local", ALLOWED_AGENTS_ENV: "alpha"},
    ],
    ids=["agents-no-user", "cloud-no-user", "user-grants-nothing", "acts-for-itself", "reserved"],
)
def test_misconfiguration_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    for name in (ACTS_FOR_ENV, ALLOWED_AGENTS_ENV, ALLOW_CLOUD_SESSIONS_ENV):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError):
        MachineSessionAccess.from_env(_MACHINE)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--dangerously-skip-permissions"], True),
        (["--dangerously-bypass-approvals-and-sandbox"], True),
        (["--yolo"], True),
        (["--permission-mode", "bypassPermissions"], True),
        (["--permission-mode=bypassPermissions"], True),
        (["--sandbox", "danger-full-access"], True),
        (["--permission-mode", "plan"], False),
        (["--sandbox", "workspace-write"], False),
        ([], False),
        (None, False),
    ],
)
def test_launch_args_bypass_detection(args: list[str] | None, expected: bool) -> None:
    assert launch_args_bypass_permissions(args) is expected


def test_host_owner_check_only_widens_when_the_grant_is_passed() -> None:
    access = MachineSessionAccess(_MACHINE, "alice@example.test", frozenset({"alpha"}), False)
    store = _FakeHostStore(
        hosts={
            "mine": _FakeHost(host_id="mine", user_id="alice@example.test"),
            "other": _FakeHost(host_id="other", user_id="bob@example.test"),
        }
    )
    assert (
        resolve_host_owner(
            user_id=_MACHINE, host_id="mine", host_store=store, machine_access=access
        ).host_id
        == "mine"
    )
    with pytest.raises(HTTPException) as no_grant:
        resolve_host_owner(user_id=_MACHINE, host_id="mine", host_store=store)
    assert no_grant.value.status_code == 403
    with pytest.raises(HTTPException) as other_host:
        resolve_host_owner(
            user_id=_MACHINE, host_id="other", host_store=store, machine_access=access
        )
    assert other_host.value.status_code == 403
    with pytest.raises(HTTPException) as other_caller:
        resolve_host_owner(
            user_id="carol@example.test", host_id="mine", host_store=store, machine_access=access
        )
    assert other_caller.value.status_code == 403


def test_initial_input_is_not_a_machine_create_field() -> None:
    """A durable first input needs a managed host, which the machine principal cannot set."""
    from omnigent.server.machine_access import MACHINE_CREATE_FIELDS

    assert "initial_input" not in MACHINE_CREATE_FIELDS
