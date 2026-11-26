"""approvals.py: a base that requires `land` makes no land wait for it, since only the merge step of that land posts it (#1323)."""

from __future__ import annotations

from typing import Any

import pytest
from approvals_harness import (
    approvals,
    lands,  # noqa: F401
)


def settings_md(block: str) -> str:
    return f"# Loop\n\n## Settings\n\n```yaml\n{block}```\n"


@pytest.fixture
def land_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """The base's rules require `land`, and the checks `loop:approvals` and `gatekeeper` the loop posts too."""
    monkeypatch.setattr(
        approvals, "required_checks", lambda _base: {"land", "loop:approvals", "gatekeeper"}
    )


def merged_after_land_status(lands: dict[str, Any]) -> bool:
    return lands["merged"]() == ["7"]


def test_a_required_land_does_not_hold_the_land_that_posts_it(
    lands: dict[str, Any], land_required: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    posted: list[str] = []
    monkeypatch.setattr(approvals, "post_status", lambda _head, context, *_: posted.append(context))
    code, lines = lands["land"]()
    assert code == 0, lines
    assert merged_after_land_status(lands)
    assert posted[0] == "land"  # before the merge call; the gate may post its own after


def test_a_required_land_does_not_hold_the_ci_wait(
    lands: dict[str, Any], land_required: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    # CI is on for this project; its head has no check of any name `land` could satisfy
    monkeypatch.setattr(approvals, "head_checks", lambda _pull, required: ([], None, set()))
    waits = approvals.ran(lands["pulls"][7])
    assert not any("`land`" in line for line in waits), waits


def test_a_required_land_does_not_keep_a_draft_from_being_readied(
    lands: dict[str, Any], land_required: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    # the policy changes while the land waits for the lock, so `unreadied` judges the readied draft again
    project, changed = approvals.project, []
    monkeypatch.setattr(
        approvals,
        "judge_with_base",
        lambda _base, fetch=False: changed.append(1) if not fetch else None,
    )
    monkeypatch.setattr(
        approvals,
        "project",
        lambda **kw: (
            (project(**kw)[0], settings_md("loop:\n  gate: echo ok\n"))
            if changed
            else project(**kw)
        ),
    )
    lands["pulls"][7]["isDraft"] = True
    # CI is on and its head has no check of any name `land` could satisfy, as in the CI wait's test
    monkeypatch.setattr(approvals, "head_checks", lambda _pull, required: ([], None, set()))
    unreadied, ran = approvals.unreadied, []
    monkeypatch.setattr(approvals, "unreadied", lambda *a: ran.append(1) or unreadied(*a))
    code, lines = lands["land"]("7")
    assert code == 0, lines
    assert ran, "the policy change did not reach unreadied"
    assert merged_after_land_status(lands)
    assert not any(call[:3] == ("pr", "ready", "7") and "--undo" in call for call in lands["calls"])


def test_unreadied_judges_a_draft_without_the_statuses_the_loop_posts(
    lands: dict[str, Any], land_required: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    asked: list[set[str]] = []

    def landable(_pull: Any, _tracker: Any, _loop: Any, required: set[str], named: bool) -> Any:
        asked.append(required)
        return type("Verdict", (), {"draft": True})()

    monkeypatch.setattr(approvals, "landable", landable)
    pull = {**lands["pulls"][7], "readied": True, "waiting": []}
    assert approvals.unreadied([pull], "main", None, None) == [pull]
    # the same set readied() was given, so a draft it readied is not sent back for a status only land posts
    assert asked == [set()]


def test_head_proof_refuses_land_as_its_status() -> None:
    loop = settings_md("loop:\n  head_proof:\n    status: land\n    env: []\n")
    with pytest.raises(approvals.Refused, match="`head_proof.status` is 'land'"):
        approvals.head_proof(loop)
