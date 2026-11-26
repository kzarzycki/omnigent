"""approvals.py: a PR that closes no open issue is refused at `check build`, since GitHub lists a PR on an issue's
Development panel only for a closing keyword (`Part of` alone shows on no issue)."""

from __future__ import annotations

import pytest
from approvals_harness import LOOP, TRACKER, add, approvals, approved, authored, issue, pull

# a consumer's release workflow: its App opens the `cz bump` PR, which names the standing release epic with `Part of`
RELEASE_APP = "release-app[bot]"
RELEASES = add(
    LOOP,
    "bot",
    f'"{RELEASE_APP}":\n  paths:\n    - CHANGELOG.md\n    - pyproject.toml\n    - uv.lock\n',
)


def refusal(body: str, state: str = "OPEN") -> list[str]:
    issue = approved(1, "kind:bug")
    issue["state"] = state
    pr = pull(body=body)
    pr["closingIssuesReferences"]["nodes"] = [issue] if body.startswith("Closes") else []
    return approvals.closes_nothing(pr, [issue], LOOP)


@pytest.mark.parametrize("body", ["Part of #1", "Implements #1"])
def test_a_pr_with_only_a_link_is_refused(body: str) -> None:
    (line,) = refusal(body)
    assert line.startswith("the PR closes no open issue") and "own sub-issue" in line


def test_a_pr_closing_an_open_issue_passes() -> None:
    assert refusal("Closes #1") == []


def test_a_pr_closing_a_closed_issue_is_refused() -> None:
    assert refusal("Closes #1", "CLOSED")


def test_a_pr_naming_no_issue_is_left_to_the_no_issue_refusal() -> None:
    assert approvals.closes_nothing(pull(body="x"), [], LOOP) == []


def test_a_verified_release_pin_bump_is_exempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(approvals, "pin_target", lambda _pull: "engineering-v1.0.0")
    assert refusal("Part of #1") == []


@pytest.mark.parametrize(("pusher", "refused"), [(RELEASE_APP, False), ("worker", True)])
def test_the_release_workflows_part_of_pr_is_exempt(pusher: str, refused: bool) -> None:
    """The release fold a `bot` section covers finishes no issue of its own; the same `Part of` PR with a person's
    commit on it is no bot's PR, so it is refused like any other."""
    issue = approved(1, "kind:bug")
    issue["state"] = "OPEN"
    pr = {
        **pull(body="Part of #1", files=("CHANGELOG.md", "pyproject.toml", "uv.lock")),
        "author": {"__typename": "Bot", "login": RELEASE_APP},
        "headRefName": "release/next",
    }
    pr["authored"] = {"nodes": [authored(RELEASE_APP), authored(pusher)]}
    assert bool(approvals.closes_nothing(pr, [issue], RELEASES)) is refused


@pytest.mark.parametrize(("body", "refused"), [("Part of #1", True), ("Closes #1", False)])
def test_check_build_refuses_a_part_of_only_pr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], body: str, refused: bool
) -> None:
    """The refusal sits in gated(), which check build, check merge and land share: dropping that call must fail here."""
    monkeypatch.setattr(approvals, "project", lambda: (TRACKER, LOOP))
    issue = approved(1, "kind:bug")
    issue["state"] = "OPEN"
    pr = pull(body=body)
    pr["closingIssuesReferences"]["nodes"] = [issue] if not refused else []
    monkeypatch.setattr(approvals, "pull_request", lambda _pr: pr)
    monkeypatch.setattr(approvals, "issues_of", lambda numbers: [issue])
    approvals.main(["check", "build", "7"])
    out = capsys.readouterr().out
    assert ("the PR closes no open issue" in out) is refused


def test_a_closing_link_to_another_repos_issue_is_judged_as_no_issue_of_the_pr() -> None:
    """GitHub links `closes <url of a sibling repo's issue>` as a closing reference. The gates work on bare numbers in
    the PR's own repo, so the sibling's #1, with no spec record, must not stand in for this repo's approved #1."""
    own = approved(1, "kind:bug")
    own["state"] = "OPEN"
    own["repository"] = {"nameWithOwner": "KZARZYCKI/Scratch-Gate-Revert"}  # spelled either way
    other = issue(1, "kind:bug")
    other["state"] = "OPEN"
    other["repository"] = {"nameWithOwner": "Kzarzycki/Sibling"}
    pr = pull(body="Closes #1\n\ncloses https://github.com/kzarzycki/sibling/issues/1")
    pr["closingIssuesReferences"]["nodes"] = [other, own]
    assert approvals.closing_issues(pr) == [own]
    assert approvals.closed(pr, "kzarzycki/scratch-gate-revert", LOOP) == {1}
    tracker = TRACKER
    assert approvals.gated("merge", pr, tracker, LOOP)[1] == [own]
