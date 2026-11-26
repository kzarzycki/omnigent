"""approvals.py: a failed `local-ci --record` writes a `gate` failure status on the head, the signal a lead's
`wait-for --until gate` reads (#1002)."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from approvals_harness import (
    BARE,
    HEAD,
    TREE,
    add,
    approvals,
    checkout,  # noqa: F401
    github,  # noqa: F401
)


def gate_states(hub: dict[str, list[dict[str, Any]]]) -> list[tuple[str, str]]:
    return [(s["state"], s["description"]) for s in hub.get(HEAD, []) if s["context"] == "gate"]


def test_a_failed_record_run_writes_a_failure_gate_status(
    checkout: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    checkout["exit"] = 2
    assert approvals.main(["local-ci", "--record"]) == 1
    assert gate_states(hub) == [
        ("failure", f"Failed: mise run check:all exited 2 on tree {TREE[:12]} at {HEAD[:12]}")
    ]
    assert f"wrote the `gate` failure status on {HEAD}" in capsys.readouterr().out


def test_a_machine_fault_writes_an_error_gate_status(
    checkout: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    checkout.update({"exit": 78, "loop": add(BARE, "loop", "machine_exit: 78\n")})
    assert approvals.main(["local-ci", "--record"]) == 1
    assert [state for state, _ in gate_states(hub)] == ["error"]


def test_a_passing_record_run_writes_no_failure(
    checkout: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    assert approvals.main(["local-ci", "--record"]) == 0
    assert [state for state, _ in gate_states(hub)] == ["success"]


def test_a_refused_failure_status_is_said_and_is_no_error(
    checkout: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refused(*_a: Any, **_k: Any) -> None:
        raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 403: Resource not accessible")

    monkeypatch.setattr(approvals, "post_status", refused)
    checkout["exit"] = 2
    assert approvals.main(["local-ci", "--record"]) == 1  # the run's own failure, not the status's
    out = capsys.readouterr().out
    assert (
        "the `gate` failure status, which tells a waiting lead the run failed, was not written"
        in out
    )
    assert "HTTP 403" in out
    assert "mise run check:all exited 2: nothing recorded" in out
