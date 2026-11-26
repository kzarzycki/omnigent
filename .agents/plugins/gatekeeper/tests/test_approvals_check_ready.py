"""approvals.py `check ready`: the PR body's `## Evidence` alone, by the code `check merge` runs (#1577)."""

from __future__ import annotations

import contextlib

import pytest
from approvals_harness import approvals, approved, issue, loop_md, pull


@pytest.fixture(autouse=False)
def project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(approvals, "project", lambda: (None, loop_md("")))
    monkeypatch.setattr(approvals, "adopted", lambda issues, loop: issues)
    monkeypatch.setattr(approvals, "closing_issues", lambda pr: [approved(1)])


def test_ready_refuses_a_body_with_no_evidence_and_says_what_merge_says(
    project: None, capsys: pytest.CaptureFixture[str]
) -> None:
    pr = pull(body="Closes #1\n")
    assert approvals.checked("ready", pr) == 1
    assert capsys.readouterr().out.strip() in approvals.proofs(
        "merge", pr, [approved(1)], loop_md("")
    )


def test_ready_takes_a_body_with_evidence_and_checks_nothing_else(
    project: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        approvals.checked("ready", pull(body="## Evidence\n\nran it\n")) == 0
    )  # no verdict, no gate owed
    assert "evidence proof" in capsys.readouterr().out


def test_ready_refuses_a_demo_that_only_links_its_media(
    project: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(approvals, "writer", lambda login: login == "loop")
    linked = "## Demo\n\n- [check 1](https://example.com/check-1.mp4)\n"
    assert approvals.checked("ready", pull(body="## Evidence\n\nran\n", comments=(linked,))) == 1
    assert "demo.sh" in capsys.readouterr().out


def test_ready_honours_an_incubating_waiver_of_evidence_and_no_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epic = {**issue(9, "incubating", "epic"), "state": "OPEN"}
    monkeypatch.setattr(approvals, "issue_node", lambda _n: epic)
    monkeypatch.setattr(approvals, "adopted", lambda issues, loop: issues)
    monkeypatch.setattr(approvals, "closing_issues", lambda pr: [approved(1)])
    pr = pull(body="Incubates: #9\n", labels=("incubating",))
    pr["files"] = {
        "nodes": [{"path": "x.md", "changeType": "ADDED", "additions": 5, "deletions": 0}]
    }
    monkeypatch.setattr(
        approvals, "project", lambda: (None, loop_md("incubating:\n  waive: [evidence]\n"))
    )
    assert approvals.checked("ready", pr) == 0
    monkeypatch.setattr(
        approvals, "project", lambda: (None, loop_md("incubating:\n  waive: [verifier]\n"))
    )
    assert approvals.checked("ready", pr) == 1  # nothing waived: Evidence still owed


def test_the_ready_point_is_a_cli_choice(
    project: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(approvals, "toplevel", lambda: None)
    monkeypatch.setattr(approvals, "pull_request", lambda number: pull(body="Closes #1\n"))
    monkeypatch.setattr(approvals, "policy_from", lambda base: contextlib.nullcontext())
    assert approvals.main(["check", "ready", "7"]) == 1
    assert "## Evidence" in capsys.readouterr().out
