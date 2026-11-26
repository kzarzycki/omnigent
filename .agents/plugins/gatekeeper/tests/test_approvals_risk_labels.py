"""approvals.py: adding `risk:high` takes off the PR's other risk labels and comments why (#921)."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from approvals_harness import (
    approvals,
    approved,
    issue,
    pull,
)


def test_an_inherited_risk_high_replaces_risk_low_and_names_the_issue(
    monkeypatch: pytest.MonkeyPatch, added: list[tuple[int, str, bool]]
) -> None:
    comments: list[tuple[int, str]] = []
    monkeypatch.setattr(approvals, "comment", lambda number, body: comments.append((number, body)))
    pr: dict[str, Any] = pull(labels=("risk:low", "area:web"))
    named = issue(3, "risk:high")
    assert approvals.flag(pr, [approved(1), named], None) == []
    labels = {"risk:low", "area:web"}
    for _, name, add in added:
        (labels.add if add else labels.discard)(name)
    assert labels == {"risk:high", "area:web"}
    assert len(comments) == 1
    assert comments[0][0] == pr["number"]
    assert "#3" in comments[0][1]
    assert not comments[0][1].startswith(approvals.FLAGGED)


def test_a_refused_label_removal_or_comment_is_a_line_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refused(*_args: Any) -> None:
        raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 403\n")

    monkeypatch.setattr(approvals, "label", lambda number, name, add: None if add else refused())
    monkeypatch.setattr(approvals, "comment", lambda number, body: refused())
    pr: dict[str, Any] = pull(labels=("risk:low",))
    assert approvals.flag(pr, [issue(3, "risk:high")], None) == [
        "PR #7: could not remove `risk:low` beside `risk:high` (HTTP 403): remove it by hand",
        (
            "PR #7: could not comment why `risk:high` was added (`risk:high` is on #3)"
            " (Command 'gh' returned non-zero exit status 1.)"
        ),
    ]
