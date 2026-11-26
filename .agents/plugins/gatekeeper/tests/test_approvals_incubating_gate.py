"""approvals.py: an eligible incubating PR's gate run takes `GATE_INCUBATING` and the waived names (#1288)."""

from __future__ import annotations

import os
from typing import Any

import pytest
from approvals_harness import approvals, loop_md, pull

BODY = "Incubates: #9\n"
NAMES = ["GATE_INCUBATING", "GATE_INCUBATING_WAIVES"]


@pytest.fixture(autouse=True)
def setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Epic #9 open and labelled; no env left over from or into another test."""
    node = {"number": 9, "state": "OPEN", "labels": {"nodes": [{"name": "incubating"}]}}
    monkeypatch.setattr(approvals, "issue_node", lambda _n: node)
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)


def made(body: str = BODY, path: str = "new.py") -> dict[str, Any]:
    pr = pull(body=body, labels=("incubating",))
    pr["files"] = {"nodes": [{"path": path, "changeType": "ADDED", "additions": 1, "deletions": 0}]}
    return pr


def test_an_eligible_pr_exports_the_flag_and_the_waived_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = loop_md("incubating:\n  waive: [full_gate, coverage]\n")
    assert approvals.tier(made(), policy) == NAMES
    assert os.environ["GATE_INCUBATING"] == "1"
    assert os.environ["GATE_INCUBATING_WAIVES"] == "coverage,full_gate"
    monkeypatch.delenv("GATE_INCUBATING")
    monkeypatch.delenv("GATE_INCUBATING_WAIVES")


def test_an_ineligible_pr_exports_nothing() -> None:
    policy = loop_md("incubating:\n  waive: [full_gate]\n")
    assert approvals.tier(made(body="no epic line"), policy) == []
    assert "GATE_INCUBATING" not in os.environ


def test_an_ambient_flag_never_reaches_a_gate_step(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in NAMES:
        monkeypatch.setenv(name, "1")
    step = {"sets": {}}
    assert not set(NAMES) & set(approvals.gate_env(step))
    assert approvals.gate_env({"sets": {"GATE_INCUBATING": "1"}})["GATE_INCUBATING"] == "1"


def test_local_ci_of_a_pr_runs_the_gate_with_the_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(approvals, "settled_head", lambda *_a: "h")
    monkeypatch.setattr(approvals, "project", lambda: (None, "loop"))
    monkeypatch.setattr(approvals, "settings", lambda _l: {"incubating": {}})
    monkeypatch.setattr(approvals, "pull_request", lambda _n: made())
    monkeypatch.setattr(approvals, "closing_issues", lambda _p: [])
    monkeypatch.setattr(
        approvals, "reusable", lambda head, passed=(), why=None: seen.update(reuse=list(passed))
    )
    monkeypatch.setattr(
        approvals,
        "run_local_ci",
        lambda pr, head, passed=(): (
            seen.update(ran=list(passed)) or {"command": "c", "env": {}, "published": True}
        ),
    )
    monkeypatch.setattr(approvals, "comment", lambda *_a: None)
    approvals.pr_local_ci(7, True)
    assert seen["reuse"] == NAMES and seen["ran"] == NAMES


def test_local_ci_of_a_pr_reads_no_pr_without_an_incubating_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(approvals, "settled_head", lambda *_a: "h")
    monkeypatch.setattr(approvals, "project", lambda: (None, "loop"))
    monkeypatch.setattr(approvals, "settings", lambda _l: {})
    monkeypatch.setattr(approvals, "pull_request", lambda _n: pytest.fail("the PR was read"))
    monkeypatch.setattr(
        approvals,
        "reusable",
        lambda head, passed=(), why=None: {
            "head": "h",
            "time": "t",
            "command": "c",
            "env": {},
            "published": True,
        },
    )
    monkeypatch.setattr(approvals, "comment", lambda *_a: None)
    assert approvals.pr_local_ci(7, False) == 0
