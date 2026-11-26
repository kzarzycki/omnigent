"""approvals.py: `loop.batch_wait_seconds` makes `land` wait for a PR held only by its required checks (#1032)."""

from __future__ import annotations

import time as real_time
from typing import Any

import pytest
from approvals_harness import (
    GITHUB_RULE,
    approvals,
    git_in,
    lands,  # noqa: F401
    loop_md,
)


class Clock:
    """A stubbed `time` for approvals: `sleep` moves `monotonic` by what it is asked, then runs `tick(now)`."""

    def __init__(self) -> None:
        self.now, self.slept, self.tick = 0.0, [], lambda _now: None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert sum(self.slept) < 10_000, "the wait never ends"
        self.slept.append(seconds)
        self.now += seconds
        self.tick(self.now)

    def __getattr__(self, name: str) -> Any:
        return getattr(real_time, name)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    stub = Clock()
    monkeypatch.setattr(approvals, "time", stub)
    monkeypatch.setattr(approvals, "QUEUE_POLL", approvals.POLL)  # these tests count 30 s rounds
    return stub


@pytest.fixture(autouse=True)
def checks_required(monkeypatch: pytest.MonkeyPatch, required: list[str]) -> None:
    monkeypatch.setattr(approvals, "required_checks", lambda _base: {"check"})


def policy(lands: dict[str, Any], setting: str) -> None:
    """The base's loop.md with `setting` (a section), committed to origin's main before any PR is opened."""
    work = lands["work"]
    (work / "docs" / "agents" / "loop.md").write_text(
        GITHUB_RULE.replace("```yaml\n", f"```yaml\n{setting}\n", 1)
    )
    git_in(work, "add", "docs")
    git_in(work, "commit", "-q", "-m", "policy")
    git_in(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    git_in(work, "fetch", "-q", "origin")


def check(lands: dict[str, Any], number: int, state: str) -> None:
    """PR `number`'s required `check` is now `state` (SUCCESS, or PENDING while it runs)."""
    nodes = lands["pulls"][number]["commits"]["nodes"][0]["commit"]["statusCheckRollup"]
    nodes["contexts"]["nodes"] = [
        {"__typename": "CheckRun", "name": "check", "status": "COMPLETED", "conclusion": state}
    ]


def batch_line(lands: dict[str, Any], number: int) -> str:
    [body] = [b for n, b in lands["comments"] if n == number and b.startswith("Landed\n")]
    return next(line for line in body.splitlines() if line.startswith("Batch:"))


def test_a_pr_whose_checks_turn_green_during_the_wait_joins_the_batch(
    lands: dict[str, Any], clock: Clock
) -> None:
    policy(lands, "land:\n  batch_wait_seconds: 600")
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](8, {"8.txt": "8\n"})
    check(lands, 8, "PENDING")
    clock.tick = lambda now: check(lands, 8, "SUCCESS") if now >= 300 else None
    code, lines = lands["land"]()
    assert code == 0, lines
    assert lands["merged"]() == ["7", "8"] and lands["runs"]() == 0
    assert sum(clock.slept) == 300
    assert any("waits up to 600 s" in line and "#8" in line for line in lines), lines
    assert any("PR #8 turned landable" in line for line in lines), lines
    assert any(line == "land waited 300 s for the batch; joined: #8" for line in lines), lines
    assert (
        batch_line(lands, 7) == batch_line(lands, 8) == "Batch: #7, #8 (waited 300 s, joined: #8)"
    )


def test_the_wait_ends_at_the_deadline_and_the_pr_that_is_still_pending_stays_out(
    lands: dict[str, Any], clock: Clock
) -> None:
    policy(lands, "land:\n  batch_wait_seconds: 100")
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](8, {"8.txt": "8\n"})
    check(lands, 8, "PENDING")
    code, _ = lands["land"]()
    assert (code, lands["merged"](), lands["runs"]()) == (approvals.WAITING, ["7"], 0)
    assert sum(clock.slept) == 100
    assert batch_line(lands, 7) == "Batch: #7 (waited 100 s, joined: none)"


def test_a_pr_that_turns_red_during_the_wait_ends_it(lands: dict[str, Any], clock: Clock) -> None:
    policy(lands, "land:\n  batch_wait_seconds: 600")
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](8, {"8.txt": "8\n"})
    check(lands, 8, "PENDING")
    clock.tick = lambda _now: check(lands, 8, "FAILURE")
    code, lines = lands["land"]()
    assert (code, lands["merged"](), len(clock.slept)) == (0, ["7"], 1)
    assert any("left out of the wait" in line for line in lines), lines


@pytest.mark.parametrize("other", ["red", "owner", "owner and pending", "gap"])
def test_no_pr_held_by_its_checks_alone_lands_at_once(
    lands: dict[str, Any], clock: Clock, other: str
) -> None:
    """A red check, an owner wait (with or without pending checks behind it) and a proof gap are no wait."""
    policy(lands, "land:\n  batch_wait_seconds: 600")
    lands["open"](7, {"7.txt": "7\n"})
    if other.startswith("owner"):
        lands["open"](8, {".github/ci.yml": "on: push\n"})
        lands["pulls"][8]["files"] = {"nodes": [{"path": ".github/ci.yml"}]}
    else:
        lands["open"](8, {"8.txt": "8\n"}, **({"reviews": ()} if other == "gap" else {}))
    if other == "red":
        check(lands, 8, "FAILURE")
    if other == "owner and pending":
        check(lands, 8, "PENDING")
    _, _ = lands["land"]()
    assert (lands["merged"](), lands["runs"](), clock.slept) == (["7"], 0, [])


def test_a_lone_pr_lands_at_once(lands: dict[str, Any], clock: Clock) -> None:
    policy(lands, "land:\n  batch_wait_seconds: 600")
    lands["open"](7, {"7.txt": "7\n"})
    assert lands["land"]()[0] == 0
    assert (lands["merged"](), clock.slept, batch_line(lands, 7)) == (["7"], [], "Batch: #7")


def test_unset_keeps_todays_call_order(
    lands: dict[str, Any], clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](8, {"8.txt": "8\n"})
    check(lands, 8, "PENDING")
    calls: list[str] = []
    monkeypatch.setattr(approvals, "batch_wait", lambda *_a: calls.append("wait") or "")
    judged, land = approvals.landable, approvals.landable
    monkeypatch.setattr(
        approvals,
        "landable",
        lambda pull, *rest: calls.append(f"landable {pull['number']}") or judged(pull, *rest),
    )
    code, _ = lands["land"]()
    assert (code, calls, lands["merged"](), clock.slept) == (
        approvals.WAITING,
        ["landable 7", "landable 8"],
        ["7"],
        [],
    )
    assert land is judged


def test_only_never_reads_or_waits_on_other_prs(
    lands: dict[str, Any], clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy(lands, "land:\n  batch_wait_seconds: 600")
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](8, {"8.txt": "8\n"})
    check(lands, 8, "PENDING")
    read: list[int] = []
    monkeypatch.setattr(approvals, "pull_request", lambda n: read.append(n) or lands["pulls"][n])
    assert lands["land"]("7", "--only")[0] == 0
    assert (set(read), lands["merged"](), clock.slept) == ({7}, ["7"], [])


@pytest.mark.parametrize("bad", ["-1", "'600'", "1.5", "true"])
def test_a_setting_that_is_no_non_negative_whole_number_is_refused_naming_the_key(bad: str) -> None:
    with pytest.raises(approvals.Refused, match="land.batch_wait_seconds"):
        approvals.settings(loop_md(f"land:\n  batch_wait_seconds: {bad}\n"))


def test_the_setting_alone_asks_for_no_signing_keys() -> None:
    loop = loop_md("land:\n  batch_wait_seconds: 600\n")
    assert approvals.settings(loop) == {"land": {"batch_wait_seconds": 600}}


def test_a_pr_joins_in_number_order_whatever_order_its_checks_finish_in(
    lands: dict[str, Any], clock: Clock
) -> None:
    policy(lands, "land:\n  batch_wait_seconds: 600")
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](6, {"6.txt": "6\n"})
    check(lands, 6, "PENDING")
    clock.tick = lambda _now: check(lands, 6, "SUCCESS")
    assert lands["land"]()[0] == 0
    assert batch_line(lands, 7) == "Batch: #6, #7 (waited 30 s, joined: #6)"


def test_a_pr_that_asks_the_owner_during_the_wait_ends_it(
    lands: dict[str, Any], clock: Clock
) -> None:
    policy(lands, "land:\n  batch_wait_seconds: 600")
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](8, {".github/ci.yml": "on: push\n"})
    lands["pulls"][8]["files"] = {"nodes": []}
    check(lands, 8, "PENDING")
    clock.tick = lambda _now: lands["pulls"][8].update(
        files={"nodes": [{"path": ".github/ci.yml"}]}
    )
    lands["land"]()
    assert (lands["merged"](), len(clock.slept)) == (["7"], 1)


def test_only_waits_for_no_named_pr_either(lands: dict[str, Any], clock: Clock) -> None:
    policy(lands, "land:\n  batch_wait_seconds: 600")
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](8, {"8.txt": "8\n"})
    check(lands, 8, "PENDING")
    code, _ = lands["land"]("7", "8", "--only")
    assert (code, lands["merged"](), clock.slept) == (approvals.WAITING, ["7"], [])


def test_a_verdict_that_is_no_wait_on_checks_is_no_wait_even_with_no_owner_line(
    lands: dict[str, Any], clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The (WAITING, False) of landable is the owner's wait: land waits only on its (WAITING, True)."""
    policy(lands, "land:\n  batch_wait_seconds: 600")
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](8, {"8.txt": "8\n"})
    judged = approvals.landable
    monkeypatch.setattr(approvals, "owner_waits", lambda *_a: [])
    monkeypatch.setattr(
        approvals,
        "landable",
        lambda pull, *rest: (
            approvals.Verdict((approvals.WAITING, False))
            if pull["number"] == 8
            else judged(pull, *rest)
        ),
    )
    lands["land"]()
    assert (lands["merged"](), clock.slept) == (["7"], [])


def never_comes(lands: dict[str, Any], kind: str) -> None:
    """PR 8's required `check` is not on its head and no workflow will run it: `kind` says why."""
    pull = lands["pulls"][8]
    if kind == "conflicting":
        pull["mergeable"] = "CONFLICTING"
        check(lands, 8, "PENDING")
        pull["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"] = []
    else:  # readied after the draft's skipped run, which no later run replaced
        pull["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"] = [
            {
                "__typename": "CheckRun",
                "name": "check",
                "status": "COMPLETED",
                "conclusion": "SKIPPED",
                "completedAt": "2026-01-01T00:00:00Z",
            }
        ]
        pull["timelineItems"]["nodes"] = [
            {"__typename": "ReadyForReviewEvent", "createdAt": "2026-01-02T00:00:00Z"}
        ]


@pytest.mark.parametrize("kind", ["conflicting", "readied draft"])
def test_a_pr_whose_required_check_never_comes_holds_no_lock(
    lands: dict[str, Any], clock: Clock, kind: str
) -> None:
    policy(lands, "land:\n  batch_wait_seconds: 600")
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](8, {"8.txt": "8\n"})
    never_comes(lands, kind)
    code, _ = lands["land"]()
    assert (code, lands["merged"](), lands["runs"](), clock.slept) == (
        approvals.WAITING,
        ["7"],
        0,
        [],
    )


def test_a_conflicting_pr_whose_check_is_there_and_pending_is_still_waited_for(
    lands: dict[str, Any], clock: Clock
) -> None:
    policy(lands, "land:\n  batch_wait_seconds: 600")
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](8, {"8.txt": "8\n"})
    lands["pulls"][8]["mergeable"] = "CONFLICTING"
    check(lands, 8, "PENDING")
    lands["land"]()
    assert sum(clock.slept) == 600


def test_a_pr_not_reported_conflicting_and_missing_its_check_is_still_waited_for(
    lands: dict[str, Any], clock: Clock
) -> None:
    policy(lands, "land:\n  batch_wait_seconds: 600")
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](8, {"8.txt": "8\n"})
    lands["pulls"][8]["mergeable"] = "MERGEABLE"
    lands["pulls"][8]["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"][
        "nodes"
    ] = []
    lands["land"]()
    assert sum(clock.slept) == 600


def test_a_wait_of_zero_is_no_refusal() -> None:
    assert approvals.settings(loop_md("land:\n  batch_wait_seconds: 0\n")) == {
        "land": {"batch_wait_seconds": 0}
    }


def test_a_pr_whose_check_can_no_longer_come_during_the_wait_ends_it(
    lands: dict[str, Any], clock: Clock
) -> None:
    policy(lands, "land:\n  batch_wait_seconds: 600")
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](8, {"8.txt": "8\n"})
    check(lands, 8, "PENDING")
    clock.tick = lambda _now: never_comes(lands, "conflicting")
    lands["land"]()
    assert (lands["merged"](), len(clock.slept)) == (["7"], 1)


def test_a_land_with_no_working_tree_names_the_wait_in_its_landed_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The batch computed in objects lands through `merges` too, so its Landed comment says what was waited for."""
    pull = {"number": 7, "headRefOid": "a" * 40}
    seen: list[str] = []
    monkeypatch.setattr(
        approvals, "candidates_of", lambda *_a: ([pull], {8: (approvals.WAITING, True)}, {8: []})
    )
    monkeypatch.setattr(approvals, "settings", lambda _loop: {"land": {"batch_wait_seconds": 60}})
    monkeypatch.setattr(approvals, "batch_wait", lambda *_a: " (waited 60 s, joined: none)")
    monkeypatch.setattr(approvals, "policy_ref", lambda _top: "refs/remotes/origin/main")
    monkeypatch.setattr(approvals, "toplevel", lambda: None)
    monkeypatch.setattr(approvals, "git", lambda *_a: "b" * 40)
    monkeypatch.setattr(approvals, "merged_trees", lambda *_a: [(pull, "t")])
    monkeypatch.setattr(approvals, "merges", lambda *a: seen.append(a[5]))
    approvals.local_queue([pull], "main", None, None, {"check"})
    assert seen == [" (waited 60 s, joined: none)"]


def test_each_poll_of_the_wait_reads_github_not_the_runs_memory(
    lands: dict[str, Any], clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll can be shorter than the run's read memory (READ_TTL, #2016): `batch_wait_seconds` under it, or the last
    poll's remainder. After each sleep the wait empties that memory (read_afresh), so a PR whose checks just turned
    green is read from GitHub and joins the batch."""
    polls: list[float] = []
    monkeypatch.setattr(approvals.github, "read_afresh", lambda: polls.append(clock.now))
    policy(lands, "land:\n  batch_wait_seconds: 15")
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](8, {"8.txt": "8\n"})
    check(lands, 8, "PENDING")
    clock.tick = lambda now: check(lands, 8, "SUCCESS") if now >= 15 else None
    code, lines = lands["land"]()
    assert code == 0, lines
    assert clock.slept == [15] and polls == [
        15.0
    ]  # the one short poll emptied the memory before it read
