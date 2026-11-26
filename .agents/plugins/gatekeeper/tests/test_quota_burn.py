"""land --wait's REST reads across poll passes: writer, required_checks and a settled head's check runs are read once
and kept, the poll backs off while nothing changes, and gh()'s quota guard waits or stops near an empty bucket (#1771)."""

from __future__ import annotations

import itertools
import json
import subprocess
import time
from typing import Any

import pytest
from approvals_harness import approvals

github = approvals.github
HEAD = "a" * 40


class Hub:
    """The `gh` binary, faked at subprocess.run under the real github.gh() (so its guard runs as in a land): it answers
    the reads one poll pass of land makes on the REST path and counts every call by its route; `runs` is the head's
    check-run status, `bucket` the core calls left that `gh api rate_limit` reports."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.runs = "completed"
        self.bucket = 5000

    def __call__(self, cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        assert cmd[0] == "gh", cmd
        return subprocess.CompletedProcess(cmd, 0, self.answer(tuple(cmd[1:])), "")

    def answer(self, args: tuple[str, ...]) -> str:
        self.calls.append(args)
        path = " ".join(args)
        if "rate_limit" in path:
            return f"{self.bucket} 5000 {int(time.time()) + 120}"
        if "rules/branches/main" in path:
            return "check\n"
        if "branches/main" in path:
            return "qualify\n"
        if "/permission" in path:
            return "write\n"
        if "check-runs" in path:
            return json.dumps(
                {
                    "name": "check",
                    "status": self.runs,
                    "conclusion": "success",
                    "check_suite": {"id": 1},
                }
            )
        if "check-suites" in path:
            return json.dumps({"id": 1, "status": self.runs})
        if "actions/runs" in path:
            return json.dumps({"check_suite_id": 1, "name": "ci", "status": self.runs})
        if path.endswith("/status?per_page=100"):
            return json.dumps({"statuses": [], "total_count": 0})
        if args[:2] == ("api", "repos/{owner}/{repo}/statuses/" + HEAD):  # a status write
            return "{}"
        raise AssertionError(args)

    def api(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if "rate_limit" not in call]


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch) -> Hub:
    fake = Hub()
    monkeypatch.setattr(github.subprocess, "run", fake)
    monkeypatch.setattr(github, "wait_until", None)
    monkeypatch.setattr(github.random, "randint", lambda low, high: 0)
    return fake


def one_pass() -> None:
    """What a pass of land asks, as often as it asks it: the rules several times, a writer per trusted login, the
    head's checks. Passes are POLL apart, past READ_TTL, so the run's read cache (`reads`) never answers one pass
    from another: it starts each pass empty."""
    github.reads.clear()
    for _ in range(3):
        assert github.required_checks("main") == {"check", "qualify"}
    assert github.writer("worker") and github.writer("worker")
    assert github.rest_checks(HEAD)["contexts"]["totalCount"] == 1


def test_five_passes_over_a_settled_head_read_the_rules_writer_and_check_runs_once(
    hub: Hub,
) -> None:
    one_pass()
    first = len(hub.api())
    # rules and protection, the permission, three run reads, the combined status
    assert first == 2 + 1 + 3 + 1
    for _ in range(4):
        one_pass()
    # each later pass reads only the head's commit statuses, which anyone can post at any moment
    assert len(hub.api()) == first + 4
    assert all(call[-1].endswith("/status?per_page=100") for call in hub.api()[first:])


def test_a_head_with_a_check_still_running_is_read_again_each_pass(hub: Hub) -> None:
    hub.runs = "in_progress"
    one_pass()
    one_pass()
    reads = [call for call in hub.api() if "check-runs" in " ".join(call)]
    assert len(reads) == 2


def test_a_kept_answer_is_read_again_once_older_than_kept(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(github.time, "monotonic", lambda: now[0])
    one_pass()
    first = len(hub.api())
    now[0] += github.KEPT
    one_pass()
    assert len(hub.api()) == 2 * first


def test_a_writer_lookup_that_failed_on_the_network_is_asked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers: list[Any] = [
        subprocess.CalledProcessError(1, "gh", stderr="dial tcp: i/o timeout"),
        "write\n",
    ]
    asked: list[tuple[str, ...]] = []

    def gh(*args: str, **_kw: Any) -> str:
        asked.append(args)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return str(answer)

    monkeypatch.setattr(github, "gh", gh)
    assert not github.writer("worker")
    assert github.writer("worker") and github.writer("worker")
    assert len(asked) == 2


def test_a_caller_editing_the_required_checks_leaves_the_kept_answer_alone(hub: Hub) -> None:
    github.required_checks("main").discard("check")
    assert github.required_checks("main") == {"check", "qualify"}


def test_the_poll_backs_off_while_nothing_changes_up_to_the_quiet_cap(hub: Hub) -> None:
    polls = [approvals.poll_seconds(quiet) for quiet in range(6)]
    assert polls == [300, 480, 480, 480, 480, 480]
    # a quiet wait still beats inside the window a `land` event is live in, and a waiter stamps before it goes stale
    assert max(polls) + approvals.POLL_JITTER < 2 * 300 <= approvals.WAITER_STALE


def test_a_low_quota_poll_is_never_shorter_than_low_poll(hub: Hub) -> None:
    hub.bucket = 100
    assert approvals.poll_seconds(0) == approvals.LOW_POLL
    assert approvals.poll_seconds(3) == max(approvals.QUIET_POLL, approvals.LOW_POLL)


def test_the_guard_reads_the_bucket_at_most_once_a_minute_and_only_inside_a_wait(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    one_pass()
    assert not [call for call in hub.calls if "rate_limit" in call]  # no wait: nothing is guarded
    github.forget()
    monkeypatch.setattr(github, "wait_until", time.monotonic() + 3600)
    one_pass()
    one_pass()
    assert len([call for call in hub.calls if "rate_limit" in call]) == 1


def test_a_nearly_spent_bucket_waits_for_its_reset_inside_the_ceiling(
    hub: Hub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        hub.bucket = 5000  # the reset passes during the wait

    monkeypatch.setattr(github.time, "sleep", sleep)
    monkeypatch.setattr(github, "wait_until", time.monotonic() + 3600)
    hub.bucket = github.QUOTA_FLOOR - 1
    github.required_checks("main")
    assert 120 <= sum(slept) <= 122  # one wait, to the reset, then both reads go ahead
    assert len(hub.api()) == 2
    assert "quota nearly spent" in capsys.readouterr().err


def test_a_nearly_spent_bucket_whose_reset_is_past_the_ceiling_stops_naming_it(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github, "wait_until", time.monotonic() + 10)
    hub.bucket = 0
    with pytest.raises(subprocess.CalledProcessError) as stopped:
        github.required_checks("main")
    assert "core quota nearly spent (0 calls left), resets at" in stopped.value.stderr
    assert not hub.api()  # the last calls are not spent


def test_the_real_gh_guards_each_call_only_while_a_wait_runs(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard acts through gh() itself: with an empty bucket and a ceiling it can't wait inside, gh() refuses
    before it runs the call, and without a wait it runs the call regardless."""
    hub.bucket = 0
    assert github.gh("api", "repos/{owner}/{repo}/branches/main") == "qualify\n"
    github.forget()  # drop the bucket reading the unguarded call did not make
    monkeypatch.setattr(github, "wait_until", time.monotonic() + 10)
    with pytest.raises(subprocess.CalledProcessError) as stopped:
        github.gh("api", "repos/{owner}/{repo}/branches/main")
    assert "core quota nearly spent" in stopped.value.stderr
    assert len(hub.api()) == 1  # the guarded call never reached gh


def test_land_wait_naps_back_off_while_the_state_holds_and_reset_when_it_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive land --wait over six rounds (three alike, two of a new state, then an end) and read the gap between
    rounds, which is the nap poll_seconds gave for the quiet count wait_loop passed it."""
    now, ran = [0.0], []
    states = ["checks pending", "checks pending", "checks pending", "lock held", "lock held"]
    monkeypatch.setattr(github, "wait_until", None)
    monkeypatch.setattr(github, "quota", lambda bucket: None)  # no quota news: the quiet poll alone
    monkeypatch.setattr(github.random, "randint", lambda low, high: 0)
    monkeypatch.setattr(approvals.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        approvals.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )

    def landing(pr: Any, only: bool = False) -> tuple[int, bool]:
        ran.append(now[0])
        if len(ran) > len(states):
            return 0, False
        print(states[len(ran) - 1])
        return approvals.WAITING, True

    monkeypatch.setattr(approvals, "landing", landing)
    assert approvals.land(1, wait=True) == 0
    assert (
        github.wait_until is None
    )  # the wait is over: a later gh() in this process is not guarded
    gaps = [b - a for a, b in itertools.pairwise(ran)]
    assert gaps == [300, 480, 480, 300, 480]


def test_a_nearly_spent_bucket_inside_a_tight_ceiling_waits_with_the_jitter_cut_to_fit(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        hub.bucket = 5000

    monkeypatch.setattr(github.time, "sleep", sleep)
    monkeypatch.setattr(github.random, "randint", lambda low, high: high)
    monkeypatch.setattr(github, "wait_until", time.monotonic() + 121 + 50)
    hub.bucket = github.QUOTA_FLOOR - 1
    github.required_checks(
        "main"
    )  # the reset falls inside the ceiling: it waits, never stops on the jitter
    assert 120 <= sum(slept) <= 121 + 50


def test_a_run_reads_a_path_once_until_it_writes(hub: Hub, monkeypatch: pytest.MonkeyPatch) -> None:
    """One run (`approve merge`, `record verdict`) read the same PR three or four times, about 40 REST calls; a repeat of
    a plain `gh api` read within READ_TTL is answered from `reads`, and a write empties it, so the run never reads
    back what it wrote stale."""
    github.reads.clear()
    for _ in range(3):
        github.rest_checks(HEAD)
    status_reads = [call for call in hub.api() if call[-1].endswith("/status?per_page=100")]
    assert len(status_reads) == 1
    github.gh("api", "repos/{owner}/{repo}/statuses/" + HEAD, "-f", "state=success")  # a write
    github.rest_checks(HEAD)
    assert len([call for call in hub.api() if call[-1].endswith("/status?per_page=100")]) == 2


@pytest.mark.parametrize(
    ("args", "kept"),
    [
        (("api", "repos/o/r/pulls/1"), True),
        (("api", "--paginate", "repos/o/r/pulls/1/files?per_page=100", "--jq", ".[]"), True),
        (("api", "-X", "GET", "repos/o/r/pulls"), True),
        (("api", "-X", "POST", "repos/o/r/issues/1/comments"), False),
        (("api", "--method=PATCH", "repos/o/r/pulls/1"), False),
        (("api", "repos/o/r/issues/1/labels", "-f", "labels[]=x"), False),
        (("api", "repos/o/r/issues/1/comments", "-F", "body=@-"), False),
        (("api", "-i", "repos/o/r/pulls/1"), False),
        (("api", "rate_limit"), False),
        (("pr", "view", "1"), False),
    ],
)
def test_only_a_plain_api_get_is_kept(args: tuple[str, ...], kept: bool) -> None:
    assert github.plain_read(args) is kept
