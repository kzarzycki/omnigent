"""`land <pr> --wait` on a PR a merge queue holds: what it asks GitHub, through the real github.gh() (#2101).

A lead's `land 266 --wait` was the account's biggest GitHub caller: 41 rounds about 45 s apart in 31 minutes, each the
same 9 calls (the PR, a permission, `api user` three times, the merged high-risk PRs twice, the rules and the branch
protection). Reproduced here, it had two causes, both in the old code:

- `land --wait` on a PR in the queue exited 0 at once (the queue was "GitHub's to watch"), so a lead waiting for the
  merge ran `land` again and again, each a fresh process: no cache the adapter keeps (the login, the permission KEPT
  900 s, the rules) outlived its round;
- inside a round, a refused `api user` read was never kept, so each viewer() asked it again: four times a round here,
  each also reading the viewer's own permission.

The wait now waits until the queue merges the PR or takes it out, reading between rounds only its queue state
(queue_watch, one call a QUEUE_WATCH); a refused login is kept KEPT seconds, and a read refused or rate-limited is not
asked again inside its round. Driven on the `queue` fake GitHub (test_approvals_github_queue.py), with `gh` faked at
subprocess.run under the real github.gh(), its quota guard included, and a fake clock.
"""

from __future__ import annotations

import collections
import itertools
import json
import re
import subprocess
from typing import Any

import pytest
from approvals_harness import AUTOUSE, add, approvals, lands  # noqa: F401
from test_approvals_github_queue import notes_on, queue, red_group  # noqa: F401

github = approvals.github
# the adapter's functions as approvals.py binds them, before the harness's fixtures stub them
REAL = {
    name: getattr(approvals, name)
    for name in ("viewer", "writer", "merged_high", "required_checks", "pull_request")
}
REAL_GH = github.gh
EPOCH = 1_800_000_000  # the fake clock's time.time() at 0
WAIT = 30 * 60  # the wait's length: the queue merges the PR then
# a poll round's budget (gh calls) and the calls a wait may spend beside its polls: its first round, a FULL round each
# 900 s and the round that reads the merge
PER_POLL, CONSTANT = 2, 45
LIMITED = "gh: API rate limit exceeded for user ID 1. (HTTP 403)"
REFUSED = "gh: Resource not accessible by integration (HTTP 403)"


def route(args: tuple[str, ...]) -> str:
    """A gh call by what it reads, its values folded: `api user`, `api .../permission`, `graphql pullRequest`..."""
    text = " ".join(args)
    if args[:2] == ("api", "graphql"):
        if ": pullRequest(number:" in text:
            return "graphql queue_watch"
        return "graphql " + ("pullRequest" if "pullRequest(number" in text else "other")
    if args[:1] == ("api",):
        words = [a for a in args[1:] if not a.startswith(("-", ".", "If-None-Match"))]
        return "api " + re.sub(
            r"/\d+(?=/|$)", "/N", re.sub(r"\?.*", "", words[0] if words else text)
        )
    return " ".join(args[:2])


@pytest.fixture
def wait(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The `queue` world under a fake clock (`now`, which time.sleep advances, running each due `ticks` action), with
    the adapter's own viewer, writer, merged_high, required_checks and pull_request on the real github.gh(), and `gh`
    answered at subprocess.run: the login (`user`: a login, or the stderr of its refusal), the permission
    (`permission`: one, or the stderr of its refusal), the rules
    (a SQUASH merge queue requiring `qualify`), the PR's GraphQL read and the free `api rate_limit`; the rest,
    queue_watch's poll too, by the `queue` fake. `limit_after`: the REST bucket empties after that many REST answers, for 600 s, during
    which each REST read is refused with GitHub's rate-limit error and `api rate_limit` says so. Each call is kept in
    `gh_calls` as (now, args, answered); `rounds` holds when each landing() round ran."""
    for name in AUTOUSE:
        request.getfixturevalue(name)
    state: dict[str, Any] = request.getfixturevalue("queue")
    fake, pulls = github.gh, state["pulls"]
    state.update(
        now=0.0,
        ticks=[],
        gh_calls=[],
        rounds=[],
        user="loop",
        permission="write",
        limit_after=None,
        reset=None,
        rest=0,
    )
    real_run, real_sleep = subprocess.run, approvals.time.sleep

    def rate_limit(args: tuple[str, ...]) -> str:
        text = " ".join(args)
        limited = state["reset"] is not None and state["now"] < state["reset"] - EPOCH
        remaining, reset = (
            (0, state["reset"]) if limited else (5000, EPOCH + int(state["now"]) + 3600)
        )
        if ".resources.graphql" in text:
            remaining = 5000
        return f"{remaining} 5000 {reset}" if "(.limit)" in text else f"{remaining} {reset}"

    def rest_refused(args: tuple[str, ...]) -> bool:
        if args[:1] != ("api",) or args[1:2] == ("graphql",):
            return False
        if state["reset"] is not None:
            return state["now"] < state["reset"] - EPOCH
        if state["limit_after"] is not None and state["rest"] >= state["limit_after"]:
            state["reset"] = EPOCH + int(state["now"]) + 600
            return True
        state["rest"] += 1
        return False

    def answer(args: tuple[str, ...], data: str | None) -> str:
        text = " ".join(args)
        if args[:2] == ("api", "user"):
            if state["user"].startswith("gh:"):
                raise subprocess.CalledProcessError(1, ["gh", *args], "", state["user"])
            return state["user"] + "\n"
        if args[0] == "api" and any(a.endswith("/permission") for a in args):
            if state["permission"].startswith("gh:"):
                raise subprocess.CalledProcessError(1, ["gh", *args], "", state["permission"])
            return state["permission"] + "\n"
        if args[0] == "api" and "/rules/branches/" in text:
            return ":merge_queue:SQUASH\nqualify\n"
        if args[0] == "api" and any(a.startswith("repos/{owner}/{repo}/branches/") for a in args):
            return ""
        if args[:2] == ("api", "graphql") and "pullRequest(number: $n)" in text:
            number = int(next(a for a in args if a.startswith("n="))[2:])
            return json.dumps({"data": {"repository": {"pullRequest": pulls[number]}}})
        return fake(*args, data=data)

    def run(
        cmd: Any, *a: Any, input: str | None = None, **kw: Any
    ) -> subprocess.CompletedProcess[str]:
        if not isinstance(cmd, list) or cmd[:1] != ["gh"]:
            return real_run(cmd, *a, input=input, **kw)  # git, and the gate
        args = tuple(cmd[1:])
        if args[:2] == ("api", "rate_limit"):  # free, so never counted
            return subprocess.CompletedProcess(cmd, 0, rate_limit(args), "")
        if rest_refused(args):
            state["gh_calls"].append((state["now"], args, False))
            raise subprocess.CalledProcessError(1, cmd, "", LIMITED)
        try:
            out = answer(args, input)
        except subprocess.CalledProcessError:
            state["gh_calls"].append((state["now"], args, False))
            raise
        state["gh_calls"].append((state["now"], args, True))
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def sleep(seconds: float) -> None:
        if seconds < 1:  # subprocess.wait's own few-ms sleeps
            real_sleep(seconds)
            return
        state["now"] += seconds
        for at, act in sorted(state["ticks"], key=lambda tick: tick[0]):
            if state["now"] >= at:
                state["ticks"].remove((at, act))
                act()

    landing = approvals.landing

    def counted(*args: Any, **kwargs: Any) -> tuple[int, bool]:
        state["rounds"].append(state["now"])
        return landing(*args, **kwargs)

    for name, function in REAL.items():
        monkeypatch.setattr(approvals, name, function)
    monkeypatch.setattr(github, "gh", REAL_GH)
    monkeypatch.setattr(github.subprocess, "run", run)
    monkeypatch.setattr(github.random, "randint", lambda low, high: 0)
    monkeypatch.setattr(approvals.time, "monotonic", lambda: state["now"])
    monkeypatch.setattr(approvals.time, "time", lambda: EPOCH + state["now"])
    monkeypatch.setattr(approvals.time, "sleep", sleep)
    monkeypatch.setattr(approvals, "landing", counted)
    monkeypatch.setattr(approvals, "CEILING", 2 * WAIT)
    github.forget()

    def queued(number: int = 7) -> str:
        """PR #number opened and enqueued by a land without --wait, its calls and rounds forgotten, as a new process."""
        head = state["opened"](number)
        assert state["land"](str(number))[0] == 0
        assert pulls[number]["mergeQueueEntry"]
        github.forget()
        state["gh_calls"].clear()
        state["rounds"].clear()
        return head

    def at(seconds: float, act: Any) -> None:
        state["ticks"].append((seconds, act))

    state.update(queued=queued, at=at)
    return state


def spent(state: dict[str, Any]) -> collections.Counter[str]:
    return collections.Counter(route(args) for _, args, _ in state["gh_calls"])


def polls(state: dict[str, Any]) -> list[float]:
    return [now for now, args, _ in state["gh_calls"] if route(args) == "graphql queue_watch"]


def within_a_poll(state: dict[str, Any], since: float) -> bool:
    """Whether a landing() round ran within one poll of `since`: QUEUE_WATCH, read on a POLL step."""
    return any(
        since <= at <= since + approvals.QUEUE_WATCH + approvals.POLL for at in state["rounds"]
    )


@pytest.mark.parametrize("user", ["loop", REFUSED])
def test_a_30_minute_queue_wait_reads_the_login_once_and_spends_two_calls_a_poll(
    wait: dict[str, Any], user: str
) -> None:
    wait["user"] = user
    wait["queued"]()
    wait["at"](WAIT, lambda: wait["queue_merge"](7))
    code, lines = wait["land"]("7", "--wait")
    # the old code exited 0 at once, the PR still queued: its caller then polled with whole rounds
    assert code == 0, lines
    assert wait["now"] >= WAIT and "PR #7 is merged" in lines
    calls, rounds = spent(wait), polls(wait)
    assert calls["api user"] <= 1, calls  # a refused one too: the old code asked it 4 times a round
    permission = calls["api repos/{owner}/{repo}/collaborators/loop/permission"]
    assert permission <= 1 + int(wait["now"] // github.KEPT), calls
    assert len(rounds) >= WAIT // approvals.QUEUE_WATCH - 2
    assert len(wait["gh_calls"]) <= PER_POLL * len(rounds) + CONSTANT, calls


def test_poll_rounds_and_landing_rounds_are_at_least_60_s_apart(wait: dict[str, Any]) -> None:
    wait["queued"]()
    wait["at"](WAIT, lambda: wait["queue_merge"](7))
    assert wait["land"]("7", "--wait")[0] == 0
    for times in (polls(wait), wait["rounds"]):
        assert all(later - earlier >= 60 for earlier, later in itertools.pairwise(times)), times
    assert len(wait["rounds"]) < len(
        polls(wait)
    )  # a round runs on a move, or each FULL, never each poll


def test_under_a_rest_rate_limit_no_failed_read_is_asked_again_before_the_reset(
    wait: dict[str, Any],
) -> None:
    """The REST bucket empties mid-round, after the rules read, with the guard's reading still fresh: the next REST
    read is refused once, and gh() waits to the reset before it asks again; past it the wait goes on to the merge."""
    wait["queued"]()
    wait["limit_after"] = 1
    wait["at"](WAIT, lambda: wait["queue_merge"](7))
    code, lines = wait["land"]("7", "--wait")
    assert code == 0, lines
    refused = [(now, args) for now, args, answered in wait["gh_calls"] if not answered]
    assert refused, "the bucket never emptied"
    reset = wait["reset"] - EPOCH
    for now, args in refused:
        assert now < reset
        again = [later for later, same, _ in wait["gh_calls"] if same == args and later > now]
        assert all(later >= reset for later in again), (args, again)
    assert collections.Counter(args for _, args in refused).most_common(1)[0][1] == 1


def test_outside_a_wait_a_rate_limited_or_refused_read_is_asked_once_a_round(
    wait: dict[str, Any],
) -> None:
    """viewer() three times in a round, GitHub refusing `api user` for the rate limit: one call, until read_afresh()
    (a wait's next poll) lets it ask again."""
    wait["user"] = LIMITED
    assert [github.viewer() for _ in range(3)] == [None] * 3
    assert spent(wait)["api user"] == 1
    github.read_afresh()
    wait["user"] = "loop"
    assert github.viewer() == "loop"
    assert spent(wait)["api user"] == 2


def test_a_refused_login_is_kept_for_the_process_and_a_later_grant_is_seen_within_kept(
    wait: dict[str, Any],
) -> None:
    """A token whose `api user` GitHub refuses is refused for its whole process, since the process never changes its
    token: it is not asked again, though rounds read afresh. What a wait of hours may see change is a permission:
    writer() keeps its answer KEPT seconds, a refusal too, so a grant is seen after KEPT."""
    wait["user"] = REFUSED
    assert github.viewer() is None
    wait["now"] = 2 * github.KEPT
    github.read_afresh()
    assert github.viewer() is None and spent(wait)["api user"] == 1
    permission = {"login": "read"}

    def answer(args: tuple[str, ...], data: str | None = None) -> str:
        assert args[1] == "repos/{owner}/{repo}/collaborators/worker/permission", args
        return permission["login"] + "\n"

    wait["now"] = 0.0
    real_gh = github.gh
    calls: list[float] = []

    def counted(*args: str, data: str | None = None) -> str:
        calls.append(wait["now"])
        return answer(args, data)

    github.gh = counted
    try:
        assert github.writer("worker") is False
        permission["login"] = "write"
        wait["now"] = github.KEPT - 1
        assert github.writer("worker") is False and len(calls) == 1
        wait["now"] = github.KEPT + 1
        assert github.writer("worker") is True and len(calls) == 2
    finally:
        github.gh = real_gh


def test_a_merge_ends_the_wait_within_one_poll(wait: dict[str, Any]) -> None:
    wait["queued"]()
    wait["at"](300, lambda: wait["queue_merge"](7))
    code, lines = wait["land"]("7", "--wait")
    assert code == 0, lines
    assert within_a_poll(wait, 300) and wait["now"] <= 300 + approvals.QUEUE_WATCH + approvals.POLL
    assert len(notes_on(wait, 7, approvals.LANDED)) == 1


def test_a_removal_runs_the_round_within_one_poll_and_the_wait_goes_on(
    wait: dict[str, Any],
) -> None:
    """A removal for no reason that counts (`manual`) is enqueued again by the round, and the wait goes on."""
    head = wait["queued"]()
    wait["at"](300, lambda: wait["remove"](7, "MANUAL", at="2026-10-02T12:00:00Z"))
    wait["at"](900, lambda: wait["queue_merge"](7))
    code, lines = wait["land"]("7", "--wait")
    assert code == 0, lines
    assert within_a_poll(wait, 300)
    assert wait["enqueued"][-1] == (7, head) and len(wait["enqueued"]) == 2


def test_a_red_group_runs_the_round_within_one_poll(wait: dict[str, Any]) -> None:
    """GitHub takes the PR out for a red required check on its group: the round comments the red and enqueues it again."""
    wait["queued"]()

    def red() -> None:
        red_group(wait, "g1", 440)
        wait["remove"](7, approvals.FAILED_CHECKS, at="2026-10-02T12:00:00Z")

    wait["at"](300, red)
    wait["at"](900, lambda: wait["queue_merge"](7))
    code, lines = wait["land"]("7", "--wait")
    assert code == 0, lines
    assert within_a_poll(wait, 300)
    assert len(notes_on(wait, 7, approvals.QUEUE_RED)) == 1


def test_a_push_to_the_head_runs_the_round_within_one_poll(wait: dict[str, Any]) -> None:
    """A push takes the PR out of the queue; the round reads the new head, which has no verdict of its own: exit 1."""
    wait["queued"]()

    def push() -> None:
        wait["pulls"][7].update(headRefOid="c" * 40, mergeQueueEntry=None)

    wait["at"](300, push)
    code, lines = wait["land"]("7", "--wait")
    assert code == 1, "\n".join(dict.fromkeys(lines))
    assert any("not on the head " + "c" * 40 in line for line in lines)
    assert within_a_poll(wait, 300) and wait["now"] <= 300 + approvals.QUEUE_WATCH + approvals.POLL


@pytest.fixture
def mergify_wait(wait: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """`wait` on a base whose rules require `qualify` and no GitHub merge queue, the policy naming Mergify."""
    rules = approvals.github.Required({"qualify"})
    monkeypatch.setattr(approvals, "required_checks", lambda _base: rules)
    loop = wait["work"] / "docs" / "agents" / "loop.md"
    loop.write_text(add(loop.read_text(), "land", "queue: mergify\n"))
    return wait


def test_a_mergify_wait_ends_when_mergify_merges_the_pr(mergify_wait: dict[str, Any]) -> None:
    """Under `land.queue: mergify`, `land --wait` exited 0 once the PR was queued; it now returns on the merge."""
    mergify_wait["opened"](7)
    mergify_wait["at"](600, lambda: mergify_wait["queue_merge"](7))
    code, lines = mergify_wait["land"]("7", "--wait")
    assert code == 0, lines
    assert "PR #7 is merged" in lines
    # the merge leaves its labels as they were: the PR's state alone says it, read within one poll
    assert 600 <= mergify_wait["now"] <= 600 + approvals.QUEUE_WATCH + approvals.POLL
    assert len(polls(mergify_wait)) >= 600 // approvals.QUEUE_WATCH - 2


def test_a_mergify_wait_ends_when_mergify_dequeues_the_pr(mergify_wait: dict[str, Any]) -> None:
    head = mergify_wait["opened"](7)

    def dequeued() -> None:
        mergify_wait["pulls"][7]["labels"]["nodes"].append({"name": approvals.DEQUEUED})

    mergify_wait["at"](300, dequeued)
    code, lines = mergify_wait["land"]("7", "--wait")
    assert code == 1, lines
    assert f"PR #7 was dequeued by Mergify at {head}: evicted" in lines
    assert 300 <= mergify_wait["now"] <= 300 + approvals.QUEUE_WATCH + approvals.POLL


def test_a_push_to_a_head_queued_for_mergify_runs_the_round_within_one_poll(
    mergify_wait: dict[str, Any],
) -> None:
    """A push leaves Mergify's labels as they were: the new head alone says it, and its round refuses it (exit 1)."""
    mergify_wait["opened"](7)
    mergify_wait["at"](300, lambda: mergify_wait["pulls"][7].update(headRefOid="c" * 40))
    code, lines = mergify_wait["land"]("7", "--wait")
    assert code == 1, "\n".join(dict.fromkeys(lines))
    assert 300 <= mergify_wait["now"] <= 300 + approvals.QUEUE_WATCH + approvals.POLL


def test_a_wait_that_enqueues_the_pr_waits_on_for_the_merge(wait: dict[str, Any]) -> None:
    head = wait["opened"](7)
    wait["at"](300, lambda: wait["queue_merge"](7))
    code, lines = wait["land"]("7", "--wait")
    assert code == 0, lines
    assert wait["enqueued"] == [(7, head)] and within_a_poll(wait, 300)
    assert "PR #7 is merged" in lines


def test_a_rate_limited_permission_read_is_no_answer_and_is_asked_again_next_round(
    wait: dict[str, Any],
) -> None:
    """A permission read GitHub refuses for its rate limit (an HTTP 403 too) says nothing about the account: it is not
    kept as a refusal, so the next round, past read_afresh(), reads the permission again."""
    wait["permission"] = LIMITED
    assert github.writer("worker") is False
    github.read_afresh()
    wait["permission"] = "write"
    assert github.writer("worker") is True


def test_a_refused_permission_read_for_another_account_is_kept_as_an_answer(
    wait: dict[str, Any],
) -> None:
    """Where the proxy refuses the permission path and GitHub refuses `api user`, another account is no writer, and
    that answer is kept KEPT seconds: later rounds, past read_afresh(), do not read it again."""
    wait["user"], wait["permission"] = REFUSED, REFUSED
    assert github.writer("worker") is False
    github.read_afresh()
    assert github.writer("worker") is False
    assert spent(wait)["api repos/{owner}/{repo}/collaborators/worker/permission"] == 1
