"""approvals.py: `local-ci <pr>` names the head it posted on, and `--head` waits for a just-pushed head (#981)."""

from __future__ import annotations

from typing import Any

import pytest
from approvals_harness import (
    approvals,
    pull,
)
from test_approvals_gate_steps import steps  # noqa: F401

OLD, NEW = "a" * 40, "b" * 40


@pytest.fixture
def lagging(
    steps: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """A PR whose head reads OLD for `lag` reads, then NEW (the checkout's head). `posted` holds the comments,
    `slept` each wait, `reads` the number of head reads."""
    state: dict[str, Any] = {"lag": 0, "reads": 0, "posted": [], "slept": [], "published": []}
    new = steps["head"]

    def read(_n: int) -> dict[str, Any]:
        state["reads"] += 1
        return {**pull(), "headRefOid": OLD if state["reads"] <= state["lag"] else new}

    monkeypatch.setattr(approvals, "pull_request", read)
    # the base is not a head read: policy_from(base_branch(pr)) must not count against the head's lag
    monkeypatch.setattr(approvals, "base_branch", lambda _pr: "main")
    monkeypatch.setattr(approvals, "comment", lambda n, body: state["posted"].append((n, body)))
    monkeypatch.setattr(approvals.time, "sleep", lambda s: state["slept"].append(s))
    monkeypatch.setattr(approvals, "publish", lambda head, *_a: state["published"].append(head))
    state["new"] = new
    return state


def test_a_head_that_catches_up_within_the_wait_posts_on_the_new_head(
    lagging: dict[str, Any],
    steps: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert approvals.main(["local-ci", "--record"]) == 0
    capsys.readouterr()
    lagging["lag"] = 2
    assert approvals.main(["local-ci", "7", "--head", lagging["new"]]) == 0
    ((_, body),) = lagging["posted"]
    assert f"Head: {lagging['new']}" in body and OLD not in body
    assert lagging["slept"].count(approvals.HEAD_WAIT) == 2
    assert capsys.readouterr().out.strip().splitlines()[-1] == (
        f"{approvals.CHECKED} on {lagging['new']}: posted on PR #7"
    )


def test_a_head_that_never_matches_refuses_naming_both_and_posts_nothing(
    lagging: dict[str, Any],
    steps: dict[str, Any],
) -> None:
    assert approvals.main(["local-ci", "--record"]) == 0
    lagging["lag"] = 10**6
    lagging["published"].clear()
    with pytest.raises(approvals.Refused) as refused:
        approvals.local_ci(7, want=lagging["new"])
    assert OLD in str(refused.value) and lagging["new"] in str(refused.value)
    assert lagging["posted"] == [] and lagging["published"] == []
    assert lagging["reads"] == approvals.HEAD_READS
    assert lagging["slept"].count(approvals.HEAD_WAIT) == approvals.HEAD_READS - 1 == 3


def test_without_head_the_line_names_the_posted_head(
    lagging: dict[str, Any],
    steps: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert approvals.main(["local-ci", "--record"]) == 0
    capsys.readouterr()
    assert approvals.main(["local-ci", "7"]) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == (
        f"{approvals.CHECKED} on {lagging['new']}: posted on PR #7"
    )
    # a lagging head with no --head is taken as it is: no wait, and the refusal names the head it read
    lagging["lag"], lagging["reads"] = 1, 0
    with pytest.raises(approvals.Refused, match=OLD):
        approvals.local_ci(7)
    assert lagging["reads"] == 1 and approvals.HEAD_WAIT not in lagging["slept"]


@pytest.mark.parametrize("given", ["abc", "b" * 7, "z" * 40, "HEAD", "b" * 41])
def test_a_short_or_non_hex_head_is_refused_and_never_read_as_a_prefix(
    lagging: dict[str, Any], given: str
) -> None:
    with pytest.raises(approvals.Refused, match="whole commit hash"):
        approvals.local_ci(7, want=given)
    assert lagging["reads"] == 0 and lagging["posted"] == []


def test_head_goes_with_a_pr_not_with_record(
    lagging: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert approvals.main(["local-ci", "--record", "--head", OLD]) != 0
    assert "goes with a PR" in capsys.readouterr().out


def test_a_head_that_shares_only_a_prefix_with_the_pushed_one_is_a_different_head(
    lagging: dict[str, Any], steps: dict[str, Any]
) -> None:
    assert approvals.main(["local-ci", "--record"]) == 0
    other = lagging["new"][:12] + ("0" if lagging["new"][12] != "0" else "1") * 28
    with pytest.raises(approvals.Refused, match=other):
        approvals.local_ci(7, want=other)
    assert lagging["posted"] == []


def test_an_upper_case_head_is_the_same_commit(
    lagging: dict[str, Any], steps: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert approvals.main(["local-ci", "--record"]) == 0
    assert approvals.main(["local-ci", "7", "--head", lagging["new"].upper()]) == 0
    assert len(lagging["posted"]) == 1
