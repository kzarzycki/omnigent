"""A private repository owned by an organization: GraphQL's FORBIDDEN falls back to REST, and the base fetch uses gh's credentials (#1215)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import (
    approvals,
)

REAL = approvals.judge_with_base  # the shared fixtures replace it per test


def test_a_graphql_forbidden_error_falls_back_to_rest_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(approvals.github, "_rest_only", False)

    def graphql() -> str:
        calls.append("graphql")
        raise subprocess.CalledProcessError(
            1, ["gh"], stderr="gh: Resource not accessible by integration"
        )

    assert approvals.github.fast(graphql, lambda: "rest") == "rest"
    assert approvals.github.fast(graphql, lambda: "rest") == "rest"
    assert calls == ["graphql"]


def test_an_unrelated_graphql_error_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(approvals.github, "_rest_only", False)

    def graphql() -> str:
        raise subprocess.CalledProcessError(
            1, ["gh"], stderr="gh: Could not resolve to a Repository"
        )

    with pytest.raises(subprocess.CalledProcessError):
        approvals.github.fast(graphql, lambda: "rest")
    assert approvals.github._rest_only is False


def test_the_base_fetch_authenticates_through_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def run(cmd: list[str], *args: Any, **kwargs: Any) -> Any:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(approvals.subprocess, "run", run)
    monkeypatch.setattr(approvals, "toplevel", lambda: Path("."))
    monkeypatch.setattr(approvals, "base_drift", lambda _base, rules=True: [])
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    REAL("main", fetch=True)
    (fetch,) = seen
    at = fetch.index("fetch")
    assert fetch[at - 4 : at] == [
        "-c",
        "credential.helper=",
        "-c",
        "credential.helper=!gh auth git-credential",
    ]


def test_the_running_account_is_a_writer_by_its_own_permissions_where_graphql_forbidden_refuses_the_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def gh(*args: str, **kwargs: Any) -> str:
        raise subprocess.CalledProcessError(
            1, ["gh"], stderr="gh: Resource not accessible by integration"
        )

    monkeypatch.setattr(approvals.github, "gh", gh)
    monkeypatch.setattr(approvals.github, "viewer", lambda: "loop")
    monkeypatch.setattr(approvals.github, "rest", lambda _path: {"permissions": {"push": True}})
    assert approvals.github.writer("loop")
    assert not approvals.github.writer("drive-by")
