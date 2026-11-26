"""approvals.py: `approve merge` and `record verdict` hand a PR to Mergify once `check merge` passes (#1741).

Under `land.queue: mergify` the step that makes a PR ready also queues it, so no separate `land` run has to be started
and kept alive; `check stalled` lists a PR that holds a go and a satisfied verdict but was never queued. Driven through
main() against the fixtures of test_approvals_mergify_queue.py, on a PR touching `.github/**`, which the fixture's risk
rule makes wait for the owner's go on its head.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from approvals_harness import approvals, lands, verdict_on  # noqa: F401
from test_approvals_cloud_queue import cloud_queue  # noqa: F401
from test_approvals_github_queue import labels_of, queue, run  # noqa: F401
from test_approvals_mergify_queue import land_statuses, mergify  # noqa: F401


def risky(state: dict[str, Any], number: int, **fields: Any) -> str:
    """A PR touching `.github/**`, which the fixture's risk rule makes wait for the owner's go on its head."""
    head: str = state["opened"](number, {".github/x.txt": "x\n"}, **fields)
    state["pulls"][number]["files"] = {"nodes": [{"path": ".github/x.txt"}], "totalCount": 1}
    return head


def go(number: int, head: str) -> int:
    return approvals.main(["approve", "merge", str(number), "--head", head, "--by", "owner"])


def report(tmp_path: Path, head: str) -> Path:
    path = tmp_path / "report.md"
    path.write_text(
        f"# Verifier report: PR #7, pass 1\n\nVerifier: claude, same-family, pass 1\n\n## Criteria\n\n- It works.\n\n"
        f"Head: {head}\nVERDICT: 0 blockers, 0 majors, 0 minors\nSATISFIED: yes\nRisk: high — a risk path\n"
    )
    return path


@pytest.fixture
def reviewed(mergify: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """`record verdict` against the fake: the review it posts lands on the PR, as GitHub keeps it."""
    monkeypatch.setattr(approvals, "diff_lines", lambda _base, _head: {})

    def post(pr: int, review: dict[str, Any]) -> None:
        mergify["pulls"][pr]["reviews"]["nodes"].append(verdict_on(review["commit_id"]))

    monkeypatch.setattr(approvals, "post_review", post)
    return mergify


def test_a_go_recorded_after_the_verdict_queues_the_pr(
    mergify: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    head = risky(mergify, 7)
    assert land_statuses(hub, head) == []
    assert go(7, head) == 0
    assert land_statuses(hub, head) == [("success", f"Queued for Mergify at {head}")]
    assert "loop:queued" in labels_of(mergify, 7)
    assert f"PR #7 queued for Mergify at {head}" in capsys.readouterr().out


def test_a_verdict_recorded_after_the_go_queues_the_pr(
    reviewed: dict[str, Any], hub: dict[str, list[dict[str, Any]]], tmp_path: Path
) -> None:
    head = risky(reviewed, 7, reviews=())
    assert go(7, head) == 0
    assert land_statuses(hub, head) == [] and "loop:queued" not in labels_of(reviewed, 7)
    assert approvals.main(["record", "verdict", "7", str(report(tmp_path, head))]) == 0
    assert land_statuses(hub, head) == [("success", f"Queued for Mergify at {head}")]
    assert "loop:queued" in labels_of(reviewed, 7)


def test_a_pr_missing_the_go_or_the_verdict_is_not_queued_and_says_what(
    reviewed: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    no_go = risky(reviewed, 7)
    no_verdict = risky(reviewed, 8, reviews=())
    # a verdict with no go
    capsys.readouterr()
    assert approvals.main(["record", "verdict", "7", str(report(tmp_path, no_go))]) == 0
    out = capsys.readouterr().out
    assert "waits for the owner's go" in out and "PR #7 is not queued" in out
    # a go with no verdict
    assert go(8, no_verdict) == 0
    out = capsys.readouterr().out
    assert "PR #8 is not queued" in out
    for number, head in ((7, no_go), (8, no_verdict)):
        assert land_statuses(hub, head) == []
        assert "loop:queued" not in labels_of(reviewed, number)


def test_a_withdrawn_go_queues_nothing(
    mergify: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    head = risky(mergify, 7)
    code = approvals.main(["approve", "merge", "7", "--head", head, "--by", "owner", "--withdraw"])
    assert code == 0
    assert land_statuses(hub, head) == []
    assert "loop:queued" not in labels_of(mergify, 7)


def test_stalled_lists_a_pr_whose_land_was_stopped(
    mergify: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A go and a satisfied verdict with no hand-over (the `land` that would have made it never ran): listed, until it
    is queued; a PR already queued is not, and one that waits for its go is listed for that (#1746)."""
    stuck = risky(mergify, 7)
    waiting = risky(mergify, 8)
    queued = mergify["opened"](9, {"9.txt": "9\n"})
    # the go lands as a status and a record with the hand-over turned off, as before this change
    with monkeypatch.context() as stopped:
        stopped.setattr(approvals, "hand_over", lambda _number: None)
        assert go(7, stuck) == 0
    assert mergify["land"]("9", "--only")[0] == 0 and land_statuses(hub, queued)
    capsys.readouterr()
    assert approvals.main(["check", "stalled"]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"PR #7 has its go and a satisfied verdict at {stuck} and is not queued"
    # #8 waits for its go, its head pushed long before this run: a hand-back left it (#1746)
    assert lines[1:] == [
        f"PR #8 has a satisfied verdict at {waiting} and no go, gate record or new head for {lines[1].split(' for ')[1]}"
    ]
    assert lines[1].endswith(": missing: go")
    assert land_statuses(hub, waiting) == []
    assert mergify["land"]("7", "--only")[0] == 0
    assert go(8, waiting) == 0 and mergify["land"]("8", "--only")[0] == 0
    assert approvals.main(["check", "stalled"]) == 0


def test_stalled_takes_no_pr(mergify: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert approvals.main(["check", "stalled", "7"]) == 1
    assert "takes no PR" in capsys.readouterr().out
