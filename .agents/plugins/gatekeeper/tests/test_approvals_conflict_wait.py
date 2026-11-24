"""approvals.py: `land --wait` stops at once on a conflicting PR its required checks never reach (#998, #1023)."""

from __future__ import annotations

from typing import Any

import pytest
from approvals_harness import (
    approvals,
    lands,  # noqa: F401
)

NO_QUALIFY = "PR #7: no `qualify` on the head commit yet"


@pytest.fixture
def unchecked(lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """PR #7 open with no check on its head, on a base that requires `qualify`."""
    monkeypatch.setattr(approvals, "required_checks", lambda _base: {"qualify"})
    lands["open"](7, {"7.txt": "7\n"}, checks=())
    return lands


def test_a_conflicting_pr_missing_its_required_check_stops_the_wait_with_the_fix(
    unchecked: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    unchecked["pulls"][7]["mergeable"] = "CONFLICTING"

    def slept(_seconds: float) -> None:
        raise AssertionError("land --wait waited on a check no workflow runs")

    monkeypatch.setattr(approvals.time, "sleep", slept)
    code, lines = unchecked["land"]("7", "--wait")
    assert code == 1, lines
    assert lines == [NO_QUALIFY, "PR #7 conflicts with main: merge `origin/main` in"]
    assert unchecked["merged"]() == []


@pytest.mark.parametrize("mergeable", ["MERGEABLE", "UNKNOWN"])
def test_a_pr_not_reported_conflicting_still_waits_for_its_required_check(
    unchecked: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mergeable: str
) -> None:
    unchecked["pulls"][7]["mergeable"] = mergeable
    monkeypatch.setattr(approvals, "CEILING", 0)  # one look, then the ceiling
    code, lines = unchecked["land"]("7", "--wait")
    assert code == approvals.WAITING, lines
    assert lines == [NO_QUALIFY, "land --wait stopped after 0 hours, still waiting"]


def test_a_land_naming_no_pr_still_waits_on_a_conflicting_one(
    unchecked: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop is the named PR's: a batch land with nothing named keeps the wait it has today."""
    unchecked["pulls"][7]["mergeable"] = "CONFLICTING"
    monkeypatch.setattr(approvals, "CEILING", 0)
    code, lines = unchecked["land"]("--wait")
    assert code == approvals.WAITING, lines
    assert lines == [NO_QUALIFY, "land --wait stopped after 0 hours, still waiting"]


def test_a_conflicting_pr_with_its_required_check_red_says_it_conflicts(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The red run is from before the PR conflicted and no new one comes, so the CI line alone sends the lead
    nowhere: it is followed by the fix (#1023)."""
    monkeypatch.setattr(approvals, "required_checks", lambda _base: {"qualify"})
    lands["open"](7, {"7.txt": "7\n"}, checks=(("qualify", "FAILURE"),))
    lands["pulls"][7]["mergeable"] = "CONFLICTING"
    code, lines = lands["land"]("7")
    assert code == 1, lines
    assert lines == [
        "PR #7: `qualify` is FAILURE on the head commit",
        "PR #7 conflicts with main: merge `origin/main` in",
    ]


def test_a_conflicting_pr_with_its_required_check_pending_stops_the_wait_with_the_fix(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals, "required_checks", lambda _base: {"qualify"})
    lands["open"](7, {"7.txt": "7\n"}, checks=(("qualify", "IN_PROGRESS"),))
    lands["pulls"][7]["mergeable"] = "CONFLICTING"

    def slept(_seconds: float) -> None:
        raise AssertionError("land --wait waited on a check of a PR that conflicts")

    monkeypatch.setattr(approvals.time, "sleep", slept)
    code, lines = lands["land"]("7", "--wait")
    assert code == 1, lines
    assert lines == [
        "PR #7: `qualify` is IN_PROGRESS on the head commit",
        "PR #7 conflicts with main: merge `origin/main` in",
    ]
    assert lands["merged"]() == []


def test_a_mergeable_pr_with_its_required_check_red_prints_only_the_ci_line(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals, "required_checks", lambda _base: {"qualify"})
    lands["open"](7, {"7.txt": "7\n"}, checks=(("qualify", "FAILURE"),))
    lands["pulls"][7]["mergeable"] = "MERGEABLE"
    code, lines = lands["land"]("7")
    assert code == 1, lines
    assert lines == ["PR #7: `qualify` is FAILURE on the head commit"]


@pytest.mark.parametrize(
    ("rest", "graphql"), [(True, "MERGEABLE"), (False, "CONFLICTING"), (None, "UNKNOWN")]
)
def test_rest_reads_mergeable_as_graphql_names_it(
    monkeypatch: pytest.MonkeyPatch, rest: bool | None, graphql: str
) -> None:
    """A cloud session reads the PR over REST, whose `mergeable` is a boolean, or null while GitHub computes it."""
    repo = {"full_name": "owner/repo", "default_branch": "main"}
    found = {
        "node_id": "PR_1",
        "number": 7,
        "title": "Feature",
        "state": "open",
        "mergeable": rest,
        "base": {"ref": "main", "repo": repo},
        "head": {"ref": "feature", "sha": "h", "repo": repo},
    }

    def read(path: str) -> Any:
        return found if path.endswith("/pulls/7") else {"commit": {"sha": "b"}}

    monkeypatch.setattr(approvals.github, "rest", read)
    for name in ("pr_changes", "rest_pages", "rest_latest"):
        monkeypatch.setattr(approvals.github, name, lambda *_args: [])
    for name in ("rest_notes", "rest_threads", "rest_authored", "rest_timeline", "rest_checks"):
        monkeypatch.setattr(approvals.github, name, lambda *_args: {})
    assert approvals.github.rest_pull(7)["mergeable"] == graphql


def test_graphql_reads_whether_the_pr_conflicts() -> None:
    """The host reads the PR over GraphQL, whose `mergeable` the stop reads."""
    assert " mergeable " in approvals.github.PULL


@pytest.fixture
def draft(lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """PR #7 a draft on a base that requires `qualify`, whose only run is the draft's skip: green to GitHub."""
    monkeypatch.setattr(approvals, "required_checks", lambda _base: {"qualify"})
    lands["open"](7, {"7.txt": "7\n"}, checks=(("qualify", "SKIPPED"),))
    lands["pulls"][7]["isDraft"] = True
    return lands


@pytest.mark.parametrize("argv", [("7",), ("7", "--wait")])
def test_a_conflicting_named_draft_stops_with_the_fix_and_is_not_marked_ready(
    draft: dict[str, Any], monkeypatch: pytest.MonkeyPatch, argv: tuple[str, ...]
) -> None:
    """Ready starts no workflow on a conflicting PR either, so its checks would never come."""
    draft["pulls"][7]["mergeable"] = "CONFLICTING"

    def slept(_seconds: float) -> None:
        raise AssertionError("land --wait waited on a draft no workflow runs on")

    monkeypatch.setattr(approvals.time, "sleep", slept)
    code, lines = draft["land"](*argv)
    assert code == 1, lines
    assert lines == ["PR #7 conflicts with main: merge `origin/main` in"]
    assert draft["calls"] == [] and draft["pulls"][7]["isDraft"]


@pytest.mark.parametrize("mergeable", ["MERGEABLE", "UNKNOWN"])
def test_a_named_draft_not_reported_conflicting_is_marked_ready_and_waits(
    draft: dict[str, Any], mergeable: str
) -> None:
    draft["pulls"][7]["mergeable"] = mergeable
    code, lines = draft["land"]("7")
    assert code == approvals.WAITING, lines
    assert lines == [
        "PR #7 is ready now",
        "PR #7: CI starts on ready, so its required checks are pending",
    ]
    assert draft["calls"] == [("pr", "ready", "7")]


def test_a_conflicting_named_draft_on_a_base_requiring_no_check_is_marked_ready_as_today(
    lands: dict[str, Any],
) -> None:
    """No check is awaited there, so nothing waits for good: the batch's own merge finds any conflict."""
    lands["open"](7, {"7.txt": "7\n"}, checks=())
    lands["pulls"][7].update(isDraft=True, mergeable="CONFLICTING")
    code, lines = lands["land"]("7")
    assert ("pr", "ready", "7") in lands["calls"], lines
    assert not any("merge `origin/main` in" in line for line in lines)
    assert (code, lands["merged"]()) == (0, ["7"])
