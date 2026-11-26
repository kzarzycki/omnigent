"""`land --wait` waits out a GitHub rate limit and polls a held lock without GitHub calls (#1209)."""

from __future__ import annotations

import ast
import inspect
import itertools
import json
import subprocess
import textwrap
import time
from typing import Any

import pytest
from approvals_harness import approvals

github = approvals.github
real_poll_seconds = approvals.poll_seconds


def refuse(stderr: str) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(1, ["gh"], "", stderr)


@pytest.fixture(autouse=True)
def poll_unstretched(monkeypatch: pytest.MonkeyPatch) -> None:
    """The quota read is a `gh` call: tests of the wait loop poll at POLL without it."""
    monkeypatch.setattr(approvals, "poll_seconds", lambda *_: approvals.POLL)
    monkeypatch.setattr(
        approvals, "project", lambda: (None, None)
    )  # the wait's length is the default


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """gh answers the scripted `replies` in order (an exception is raised); sleeps are recorded, not taken."""
    seen: dict[str, Any] = {"gh": [], "sleeps": [], "replies": []}

    def run(cmd: list[str], **kw: Any) -> Any:
        seen["gh"].append(cmd)
        if cmd[:3] == ["gh", "api", "rate_limit"]:
            return subprocess.CompletedProcess(cmd, 0, f"0 {int(time.time()) + 120}", "")
        reply = seen["replies"].pop(0)
        if isinstance(reply, Exception):
            raise reply
        return subprocess.CompletedProcess(cmd, 0, reply, "")

    monkeypatch.setattr(github.subprocess, "run", run)
    monkeypatch.setattr(github.time, "sleep", seen["sleeps"].append)
    monkeypatch.setattr(github.random, "randint", lambda low, high: 0)
    monkeypatch.setattr(github, "wait_until", time.monotonic() + 3600)
    return seen


def test_a_primary_limit_waits_for_the_reset_then_carries_on(calls: dict[str, Any]) -> None:
    calls["replies"] = [refuse("gh: API rate limit exceeded (HTTP 403)"), "ok"]
    assert github.gh("api", "x") == "ok"
    assert 100 < sum(calls["sleeps"]) <= 125 and max(calls["sleeps"]) <= 60


def test_a_secondary_limit_waits_for_retry_after(calls: dict[str, Any]) -> None:
    calls["replies"] = [
        refuse("You have exceeded a secondary rate limit (HTTP 403) Retry-After: 17"),
        "ok",
    ]
    assert github.gh("api", "x") == "ok"
    assert calls["sleeps"] == [17]


def test_a_secondary_limit_without_retry_after_backs_off_from_60(calls: dict[str, Any]) -> None:
    calls["replies"] = [refuse("secondary rate limit (HTTP 403)")] * 2 + ["ok"]
    assert github.gh("api", "x") == "ok"
    assert calls["sleeps"] == [60, 60, 60]


def test_another_403_fails_at_once(calls: dict[str, Any]) -> None:
    calls["replies"] = [refuse("Resource not accessible by integration (HTTP 403)")]
    with pytest.raises(subprocess.CalledProcessError):
        github.gh("api", "x")
    assert calls["sleeps"] == []


def test_without_wait_a_limit_fails_naming_the_reset(
    calls: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github, "wait_until", None)
    calls["replies"] = [refuse("API rate limit exceeded (HTTP 403)")]
    with pytest.raises(subprocess.CalledProcessError) as failed:
        github.gh("api", "x")
    assert "rate limit, resets at" in failed.value.stderr
    assert calls["sleeps"] == []


def test_a_limit_past_the_ceiling_fails(
    calls: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github, "wait_until", time.monotonic() + 5)
    calls["replies"] = [refuse("API rate limit exceeded (HTTP 403)")]
    with pytest.raises(subprocess.CalledProcessError):
        github.gh("api", "x")


def test_a_held_lock_at_an_unchanged_tip_is_polled_without_gh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now, ran, listed = [0.0], [], []  # a fake clock, so the counts do not depend on machine load

    def landing(pr: Any, only: bool = False) -> tuple[int, bool]:
        ran.append(1)
        approvals.held_tip = ("refs/heads/loop-landing/main", "abc")
        return approvals.WAITING, True

    def git(*args: str) -> str:
        listed.append(args)
        return "abc\trefs/heads/loop-landing/main\n"

    monkeypatch.setattr(github, "wait_until", None)
    monkeypatch.setattr(approvals.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        approvals.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    monkeypatch.setattr(approvals, "landing", landing)
    monkeypatch.setattr(approvals, "git", git)
    assert approvals.land(1, wait=True) == approvals.WAITING
    # a lock that never moves runs landing() again only once its land may be dead (STALE), and POLL lock reads (which
    # cost no API call) go between two of them (#1915)
    assert abs(len(ran) - approvals.CEILING // approvals.STALE) <= 1
    assert len(listed) > len(ran) * 3


def test_land_wait_turns_the_rate_limit_wait_on_and_sleeps_in_printing_slices(
    calls: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(github, "wait_until", None)  # land() alone sets it
    calls["replies"] = [refuse("secondary rate limit (HTTP 403) Retry-After: 150"), "ok"]
    monkeypatch.setattr(
        approvals, "landing", lambda pr, only=False: (int(github.gh("api", "x") != "ok"), False)
    )
    assert approvals.land(1, wait=True) == 0
    assert calls["sleeps"] == [60, 60, 30]  # none of them silent past wait-for's 300 s
    assert capsys.readouterr().err.count("GitHub rate limit, waiting until") == 3


@pytest.mark.parametrize(
    "message", ["API rate limit exceeded (HTTP 403)", "gh: API rate limit exceeded for user ID 1."]
)
def test_a_graphql_limit_waits_for_the_graphql_reset(calls: dict[str, Any], message: str) -> None:
    calls["replies"] = [refuse(message), "ok"]
    github.gh("api", "graphql", "-f", "query=x")
    assert [
        "gh",
        "api",
        "rate_limit",
        "--jq",
        '.resources.graphql | "\\(.remaining) \\(.reset)"',
    ] in calls["gh"]


def test_a_held_lock_is_asked_of_github_again_once_stale_and_said_each_minute(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now, ran = [0.0], []
    monkeypatch.setattr(github, "wait_until", None)
    monkeypatch.setattr(approvals.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        approvals.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )

    def landing(pr: Any, only: bool = False) -> tuple[int, bool]:
        ran.append(now[0])
        approvals.held_tip = ("refs/heads/loop-landing/main", "abc")
        print("another land holds the landing lock")
        return approvals.WAITING, True

    monkeypatch.setattr(approvals, "landing", landing)
    monkeypatch.setattr(approvals, "git", lambda *a: "abc\trefs/heads/loop-landing/main\n")
    assert approvals.land(1, wait=True) == approvals.WAITING
    assert all(b - a == approvals.STALE for a, b in itertools.pairwise(ran))
    assert len(ran) > 2
    # the log is never silent for wait-for's 300 s: the held state is said again every SLOT_SAY_SECONDS
    assert capsys.readouterr().out.count("another land holds") >= approvals.CEILING // 120


def test_a_round_that_does_not_hold_the_lock_polls_no_stale_tip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rounds, listed = [], []
    monkeypatch.setattr(github, "wait_until", None)
    monkeypatch.setattr(approvals, "POLL", 0.01)
    monkeypatch.setattr(approvals, "STALE", 0.05)
    monkeypatch.setattr(approvals, "QUEUE_POLL", 0.03)

    def landing(pr: Any, only: bool = False) -> tuple[int, bool]:
        rounds.append(len(listed))
        if len(rounds) == 1:
            approvals.held_tip = (
                "refs/heads/loop-landing/main",
                "abc",
            )  # held; the next round is not
        return (approvals.WAITING, len(rounds) < 3)

    monkeypatch.setattr(approvals, "landing", landing)
    monkeypatch.setattr(approvals, "git", lambda *a: listed.append(a) or "abc\trefs\n")
    approvals.land(1, wait=True)
    assert rounds[2] == rounds[1]  # round 2 held no lock, so nothing was listed after it


def test_a_rest_limit_message_without_a_status_is_a_limit(calls: dict[str, Any]) -> None:
    # GitHub's refusal of `gh api` carries no HTTP status (#1266); the bucket read decides how long it waits
    calls["replies"] = [refuse("gh: API rate limit exceeded for user ID 1."), "ok"]
    assert github.gh("api", "repos/x") == "ok"
    assert calls["sleeps"]


def test_a_rate_limit_wait_adds_bounded_jitter(
    calls: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = []
    monkeypatch.setattr(
        github.random, "randint", lambda low, high: seen.append((low, high)) or high
    )
    calls["replies"] = [refuse("secondary rate limit (HTTP 403) Retry-After: 30"), "ok"]
    github.gh("api", "x")
    assert seen == [(0, github.JITTER)]
    assert sum(calls["sleeps"]) == 30 + github.JITTER


@pytest.mark.parametrize(("answer", "poll"), [("400 5000 0", 600), ("500 5000 0", 300), ("x", 300)])
def test_a_low_quota_stretches_the_poll(
    monkeypatch: pytest.MonkeyPatch, answer: str, poll: int
) -> None:
    monkeypatch.setattr(github, "gh", lambda *args: answer)
    monkeypatch.setattr(github.random, "randint", lambda low, high: 0)
    assert real_poll_seconds() == poll


def test_the_poll_adds_bounded_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[int, int]] = []
    monkeypatch.setattr(github, "gh", lambda *args: "5000 5000 0")
    monkeypatch.setattr(
        github.random, "randint", lambda low, high: seen.append((low, high)) or high
    )
    assert real_poll_seconds() == approvals.QUEUE_POLL + approvals.POLL_JITTER
    assert seen == [(0, approvals.POLL_JITTER)]


def test_the_waits_status_reads_go_through_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[str, ...]] = []
    found = {
        "number": 7,
        "state": "closed",
        "merged": True,
        "mergeable": None,
        "base": {"ref": "main"},
        "head": {"sha": "abc"},
    }
    monkeypatch.setattr(github, "gh", lambda *args, **_k: asked.append(args) or json.dumps(found))
    assert github.pull_status(7) == {
        "number": 7,
        "state": "MERGED",
        "baseRefName": "main",
        "headRefOid": "abc",
        "mergeable": "UNKNOWN",
    }
    assert asked == [("api", "repos/{owner}/{repo}/pulls/7")]  # no GraphQL


def test_a_low_quota_poll_still_says_the_wait_each_minute_and_asks_github_each_poll(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now, ran = [0.0], []
    monkeypatch.setattr(github, "wait_until", None)
    monkeypatch.setattr(approvals, "poll_seconds", lambda *_: approvals.LOW_POLL)
    monkeypatch.setattr(approvals.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        approvals.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )

    def landing(pr: Any, only: bool = False) -> tuple[int, bool]:
        ran.append(now[0])
        print("checks pending")
        return approvals.WAITING, True

    monkeypatch.setattr(approvals, "landing", landing)
    approvals.land(1, wait=True)
    assert all(b - a == approvals.LOW_POLL for a, b in itertools.pairwise(ran))
    # said again each SLOT_SAY_SECONDS, so wait-for's 300 s stall check never fires
    assert capsys.readouterr().out.count("checks pending") >= approvals.CEILING // 120


def test_a_failed_quota_read_keeps_the_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(*args: str) -> str:
        raise subprocess.CalledProcessError(1, ["gh"], "", "HTTP 502")

    monkeypatch.setattr(github.random, "randint", lambda low, high: 0)

    monkeypatch.setattr(github, "gh", failing)
    assert real_poll_seconds() == max(approvals.POLL, approvals.QUEUE_POLL)


@pytest.mark.parametrize("site", ["merges", "base_branch", "settled_head"])
def test_a_status_only_read_asks_pull_status_not_the_whole_pull(site: str) -> None:
    """The conftest fake answers pull_status from pull_request, so no behaviour test tells the two apart; the
    function's own calls do (#1271)."""
    calls = {
        node.func.id
        for node in ast.walk(
            ast.parse(textwrap.dedent(inspect.getsource(getattr(approvals, site))))
        )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "pull_status" in calls
    assert "pull_request" not in calls


def test_a_reset_inside_a_tight_ceiling_waits_with_the_jitter_cut_to_fit(
    calls: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # the bucket resets in 120 s and the ceiling has 50 s beyond it: the most jitter (JITTER) would pass the ceiling
    monkeypatch.setattr(github.random, "randint", lambda low, high: high)
    monkeypatch.setattr(github, "wait_until", time.monotonic() + 121 + 50)
    calls["replies"] = [refuse("gh: API rate limit exceeded (HTTP 403)"), "ok"]
    assert github.gh("api", "x") == "ok"  # it waits, never stops on the jitter alone
    assert 121 <= sum(calls["sleeps"]) <= 121 + 50
