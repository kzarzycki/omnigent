"""A rate-limit refusal GitHub words `already exceeded`, from gh api and gh pr, waits by its bucket (#1266)."""

from __future__ import annotations

import subprocess
import time
import types
from typing import Any

import pytest
from approvals_harness import (
    approvals,
)

github = approvals.github
ALREADY = "gh: API rate limit already exceeded for user ID 1."
GRAPHQL = "GraphQL: API rate limit already exceeded for user ID 1."


def refuse(stderr: str) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(1, ["gh"], "", stderr)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """gh answers `replies` in order; `bucket` is what `gh api rate_limit` says; sleeps are recorded, not taken."""
    seen: dict[str, Any] = {"gh": [], "sleeps": [], "replies": [], "bucket": "0"}

    def run(cmd: list[str], **kw: Any) -> Any:
        seen["gh"].append(cmd)
        if cmd[:3] == ["gh", "api", "rate_limit"]:
            return subprocess.CompletedProcess(
                cmd, 0, f"{seen['bucket']} {int(time.time()) + 120}", ""
            )
        reply = seen["replies"].pop(0)
        if isinstance(reply, Exception):
            raise reply
        return subprocess.CompletedProcess(cmd, 0, reply, "")

    monkeypatch.setattr(github.subprocess, "run", run)
    monkeypatch.setattr(github.time, "sleep", seen["sleeps"].append)
    monkeypatch.setattr(github, "wait_until", time.monotonic() + 3600)
    monkeypatch.setattr(github.random, "randint", lambda low, high: 0)  # no jitter: sums are exact
    return seen


@pytest.mark.parametrize(
    "args,message",
    [
        (("api", "graphql", "-f", "query=x"), ALREADY),
        (("pr", "view", "1"), GRAPHQL),
        (("api", "repos/x"), ALREADY),
    ],
)
def test_already_exceeded_waits_instead_of_raising(
    calls: dict[str, Any], args: tuple[str, ...], message: str
) -> None:
    calls["replies"] = [refuse(message), "ok"]
    assert github.gh(*args) == "ok"
    assert calls["sleeps"]


def test_a_graphql_error_from_gh_pr_reads_the_graphql_bucket(calls: dict[str, Any]) -> None:
    calls["replies"] = [refuse(GRAPHQL), "ok"]
    github.gh("pr", "view", "1")
    assert any(".resources.graphql" in cmd[-1] for cmd in calls["gh"] if "rate_limit" in cmd)


def test_a_rest_refusal_reads_the_core_bucket(calls: dict[str, Any]) -> None:
    calls["replies"] = [refuse(ALREADY), "ok"]
    github.gh("api", "repos/x")
    assert any(".resources.core" in cmd[-1] for cmd in calls["gh"] if "rate_limit" in cmd)


def test_a_bucket_with_calls_left_waits_backoff_doubling(calls: dict[str, Any]) -> None:
    calls["bucket"] = "3600"
    calls["replies"] = [refuse(GRAPHQL), refuse(GRAPHQL), "ok"]
    assert github.gh("pr", "view", "1") == "ok"
    assert sum(calls["sleeps"]) == 60 + 120  # 60 s, then 120 s: it doubles


def test_an_empty_bucket_waits_for_its_reset(calls: dict[str, Any]) -> None:
    calls["bucket"] = "0"
    calls["replies"] = [refuse(GRAPHQL), "ok"]
    github.gh("pr", "view", "1")
    assert 100 < sum(calls["sleeps"]) <= 125


def test_a_refusal_waits_even_where_gh_cannot_read_the_bucket() -> None:
    assert github.rate_limited(ALREADY, 60) is not None


def test_another_error_is_no_rate_limit() -> None:
    assert github.rate_limited("GraphQL: Could not resolve to a PullRequest", 60) is None


def test_a_wait_polls_the_queue_every_two_minutes_not_thirty() -> None:
    assert approvals.QUEUE_POLL == 300 and approvals.POLL == 30


def network(text: str) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(128, ["git"], "", text)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [0.0]
    monkeypatch.setattr(github, "wait_until", None)
    monkeypatch.setattr(approvals.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        approvals.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )

    def unreadable_quota(*args: str, **kwargs: Any) -> str:
        """The wait's quota read (`gh api rate_limit`) is not what these tests check: a failed read keeps the base poll."""
        raise subprocess.CalledProcessError(1, ["gh", *args], "", "quota unread")

    monkeypatch.setattr(github, "gh", unreadable_quota)
    return now


def hold(monkeypatch: pytest.MonkeyPatch, polls: list[Any]) -> list[int]:
    """A land that holds the lock and polls it with `polls` answered in order (an exception is raised)."""
    ran: list[int] = []

    def landing(pr: Any, only: bool = False) -> tuple[int, bool]:
        ran.append(1)
        approvals.held_tip = ("refs/heads/loop-landing/main", "abc")
        return approvals.WAITING, True

    def git(*args: str) -> str:
        reply = polls.pop(0) if polls else "abc\trefs\n"
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(approvals, "landing", landing)
    monkeypatch.setattr(approvals, "git", git)
    return ran


@pytest.mark.parametrize(
    "text",
    [
        "fatal: unable to access 'https://github.com/x/y/': Could not resolve host: github.com",
        "error: RPC failed; curl 56 Recv failure: Operation timed out\nfatal: expected flush after ref listing",
        "error connecting to api.github.com",
        'Post "https://api.github.com/graphql": dial tcp: lookup api.github.com: no such host',
        'Get "https://api.github.com/x": net/http: request canceled (i/o timeout)',
        "read tcp 10.0.0.2:5->140.82.0.6:443: read: connection reset by peer",
    ],
)
def test_a_network_miss_in_the_lock_poll_is_polled_again(
    monkeypatch: pytest.MonkeyPatch,
    clock: list[float],
    capsys: pytest.CaptureFixture[str],
    text: str,
) -> None:
    ran = hold(monkeypatch, [network(text), network(text)])
    assert approvals.land(1, wait=True) == approvals.WAITING  # to the ceiling, not exit 1
    out = capsys.readouterr().out
    assert "network failure 1/3" in out and "network failure 2/3" in out
    assert len(ran) > 2


def test_three_network_misses_in_a_row_fail(
    monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    hold(monkeypatch, [network("Could not resolve host: github.com")] * 3)
    with pytest.raises(subprocess.CalledProcessError):
        approvals.land(1, wait=True)


def test_a_poll_that_answers_resets_the_misses(
    monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    miss = network("Could not resolve host: github.com")
    hold(monkeypatch, [miss, miss, "abc\trefs\n", miss, miss])
    assert approvals.land(1, wait=True) == approvals.WAITING


def test_another_git_failure_still_fails_at_once(
    monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    hold(monkeypatch, [network("fatal: not a git repository")])
    with pytest.raises(subprocess.CalledProcessError):
        approvals.land(1, wait=True)


def test_a_network_miss_in_landing_is_tried_again_at_the_next_poll(
    monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    rounds: list[int] = []

    def landing(pr: Any, only: bool = False) -> tuple[int, bool]:
        rounds.append(1)
        if len(rounds) == 1:
            raise network(
                "fatal: unable to access 'https://github.com/x/y/': Could not resolve host"
            )
        return 0, False

    monkeypatch.setattr(approvals, "landing", landing)
    assert approvals.land(1, wait=True) == 0 and len(rounds) == 2


@pytest.mark.parametrize(
    "text",
    [
        "fatal: unable to access 'https://github.com/x/y/': The requested URL returned error: 403",
        "fatal: unable to access 'https://github.com/x/y/': The requested URL returned error: 404",
        "error: RPC failed; HTTP 403 curl 22 The requested URL returned error: 403",
        "error: RPC failed; HTTP 404",
        "fatal: Authentication failed for 'https://github.com/x/y/'",
    ],
)
def test_a_git_http_refusal_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, clock: list[float], text: str
) -> None:
    ran = hold(monkeypatch, [network(text)])
    with pytest.raises(subprocess.CalledProcessError):
        approvals.land(1, wait=True)
    assert len(ran) == 1


def test_the_batch_wait_polls_every_queue_poll(
    monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(
        approvals.time, "sleep", lambda s: (slept.append(s), clock.__setitem__(0, clock[0] + s))
    )
    monkeypatch.setattr(approvals, "pull_request", lambda number: {"number": number})
    monkeypatch.setattr(approvals, "check_never_comes", lambda *a: False)
    monkeypatch.setattr(approvals, "owner_waits", lambda *a: False)
    monkeypatch.setattr(
        approvals, "landable", lambda *a: types.SimpleNamespace(exit=None, issues=None)
    )
    approvals.batch_wait(1000, {}, {7: []}, [], {7: (approvals.WAITING, True)}, None, None, set())
    assert slept == [approvals.QUEUE_POLL]  # one round, at the base poll, not POLL
