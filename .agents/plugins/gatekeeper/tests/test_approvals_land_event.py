"""approvals.py: `land` writes the `land` event of each PR's branch (#1378): waiting, ok, failed; events.md is the contract."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import threading
import time as real_time
from collections.abc import Iterator
from typing import Any

import pytest
from approvals_harness import (
    Cloud,
    approvals,
    cloud,  # noqa: F401
    git_in,
    lands,  # noqa: F401
)

# origin's pre-receive hook as a host with push access has it: events may be rewritten, the lock only moves forward
PERMIT_EVENTS = """#!/bin/sh
zero=0000000000000000000000000000000000000000
while read old new ref; do
  case "$ref" in refs/heads/loop-landing/*) ;; *) continue ;; esac
  if [ "$new" = "$zero" ]; then echo "refused: delete of $ref" >&2; exit 1; fi
  if [ "$old" != "$zero" ] && ! git merge-base --is-ancestor "$old" "$new"; then
    echo "refused: non-fast-forward of $ref" >&2; exit 1
  fi
done
"""
LOCK = approvals.LOCK.format("main")
COMMENT_URL = "https://example.test/pull/{}#issuecomment-1"


@pytest.fixture
def events_pushed(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    """`lands` on an origin that accepts a rewritten `refs/loop/*`, a comment URL as GitHub returns it, and the grace
    before a merged PR's refs go set to `grace`; `event(branch)` is the event origin holds, None when it holds none.
    A delayed clear land_ended starts and the test outlives is killed at teardown: it is detached, so a test that sets
    EVENT_GRACE to 3600 would otherwise leave it sleeping an hour, to clear a temp repo that is gone by then."""
    hook = lands["origin"] / "hooks" / "pre-receive"
    hook.write_text(PERMIT_EVENTS)
    hook.chmod(0o755)
    posted = approvals.comment

    def comment(number: int, body: str) -> str:
        posted(number, body)
        return COMMENT_URL.format(number)

    monkeypatch.setattr(approvals, "comment", comment)
    monkeypatch.setattr(approvals, "EVENT_GRACE", 0)

    def event(branch: str) -> dict[str, Any] | None:
        ref = f"refs/loop/land/{branch}"
        if not git_in(lands["origin"], "for-each-ref", ref).strip():
            return None
        return json.loads(git_in(lands["origin"], "show", f"{ref}:event.json"))

    lands["event"] = event
    real_popen = approvals.subprocess.Popen
    clears: list[Any] = []

    def popen(args: Any, *a: Any, **kw: Any) -> Any:
        proc = real_popen(args, *a, **kw)
        if kw.get("start_new_session") and "clear" in args:
            clears.append(proc)
        return proc

    monkeypatch.setattr(approvals.subprocess, "Popen", popen)
    yield lands
    for proc in clears:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def refs_gone(lands: dict[str, Any], limit: float = 10) -> bool:
    """Whether no `refs/loop/*` is left on origin or in the clone, within `limit` seconds (the clear is its own process)."""
    end = real_time.monotonic() + limit
    while real_time.monotonic() < end:
        if (
            not git_in(lands["origin"], "for-each-ref", "refs/loop").strip()
            and not git_in(lands["work"], "for-each-ref", "refs/loop").strip()
        ):
            return True
        real_time.sleep(0.1)
    return False


class Clock:
    """A stubbed `time` for approvals: `sleep` moves `monotonic` by what it is asked, then runs `tick(now)`."""

    def __init__(self) -> None:
        self.now, self.tick, self.slept = 0.0, lambda _now: None, 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert self.slept < 10_000, "the wait never ends"
        self.slept += seconds
        self.now += seconds
        self.tick(self.now)

    def __getattr__(self, name: str) -> Any:
        return getattr(real_time, name)


def hold_lock(lands: dict[str, Any]) -> str:
    """Another land's HELD lock, taken now, naming no PR."""
    work = lands["work"]
    empty = git_in(work, "hash-object", "-t", "tree", "-w", "/dev/null").strip()
    commit = git_in(
        work, "commit-tree", "-m", f"{approvals.HELD} [skip ci]\n\npid 1, beats, nonce a", empty
    ).strip()
    git_in(work, "push", "-q", "origin", f"{commit}:{LOCK}")
    return commit


def free_lock(lands: dict[str, Any]) -> None:
    work, tip = lands["work"], git_in(lands["origin"], "rev-parse", LOCK).strip()
    empty = git_in(work, "hash-object", "-t", "tree", "-w", "/dev/null").strip()
    commit = git_in(
        work, "commit-tree", "-p", tip, "-m", f"{approvals.FREE} [skip ci]\n\nnonce b", empty
    ).strip()
    git_in(work, "push", "-q", "origin", f"{commit}:{LOCK}")


def test_land_wait_beats_waiting(
    events_pushed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocked on a held lock, `land --wait` rewrites its `waiting` event at each poll; the lock's own tip stays
    what the holder wrote, and the PR lands once the lock is free."""
    lands = events_pushed
    # the `ok` stays readable: a waiter polling every 0.2 s must not find the ref cleared before it reads the end
    monkeypatch.setattr(approvals, "EVENT_GRACE", 3600)
    clock = Clock()
    monkeypatch.setattr(approvals, "time", clock)
    monkeypatch.setattr(approvals, "QUEUE_POLL", approvals.POLL)
    lands["open"](7, {"7.txt": "7\n"})
    held = hold_lock(lands)
    seen: list[dict[str, Any]] = []

    def tick(now: float) -> None:
        event = lands["event"]("pr7")
        if event and (not seen or seen[-1]["seq"] != event["seq"]):
            seen.append(event)
        # the reference: the lock is still the holder's own tip while the land beats
        if len(seen) < 3:
            assert git_in(lands["origin"], "rev-parse", LOCK).strip() == held
        else:
            free_lock(lands)

    clock.tick = tick
    code, lines = lands["land"]("7", "--wait")
    assert code == 0, lines
    assert [e["state"] for e in seen] == ["waiting"] * len(seen) and len(seen) >= 3
    assert [e["seq"] for e in seen] == list(range(seen[0]["seq"], seen[0]["seq"] + len(seen)))
    assert lands["merged"]() == ["7"]


def test_land_merged_event_and_cleanup(
    events_pushed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merged PR's event ends `ok` (written once the PR is merged), and then its refs are gone, here and on origin."""
    lands = events_pushed
    lands["open"](7, {"7.txt": "7\n"})
    ends: list[tuple[str, dict[str, Any] | None, str]] = []
    emit = approvals.loop_events.emit

    def spy(kind: str, branch: str, state: str, **kw: Any) -> str:
        ends.append((state, None, lands["pulls"][7]["state"]))
        return emit(kind, branch, state, **kw)

    monkeypatch.setattr(approvals.loop_events, "emit", spy)
    code, lines = lands["land"]("7")
    assert code == 0, lines
    # the reference: the PR as GitHub reports it, merged, onto main's squash
    assert lands["pulls"][7]["state"] == "MERGED" and lands["main"]() == lands["squashes"][7]
    # a land without --wait beats nothing: one `ok`, after the merge, and nothing after it
    assert ends == [("ok", None, "MERGED")]
    assert refs_gone(lands)


def test_native_wait_follows_land(
    events_pushed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`wait land <branch>` started before `land` exits 0 once the PR merges: the `ok` stays readable for the grace
    before its refs go, so a poll between the two finds it."""
    lands = events_pushed
    monkeypatch.setattr(approvals, "EVENT_GRACE", 4)
    lands["open"](7, {"7.txt": "7\n"})
    result: dict[str, Any] = {}
    out = io.StringIO()

    def wait() -> None:
        result["code"] = approvals.loop_events.wait(
            "land", "pr7", timeout=30, poll=0.2, cwd=lands["work"], out=out
        )

    waiter = threading.Thread(target=wait)
    waiter.start()
    real_time.sleep(0.5)
    assert waiter.is_alive()  # nothing was written yet: it waits
    code, lines = lands["land"]("7")
    waiter.join(timeout=30)
    assert code == 0, lines
    assert result == {"code": 0}, out.getvalue()
    assert out.getvalue().startswith("land pr7: ok")
    assert refs_gone(lands)


@pytest.mark.parametrize("how", ["push refused", "emit raises"])
def test_land_exit_codes_without_origin_push(
    lands: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    how: str,
) -> None:
    """An origin that refuses the event's push (as a cloud session's proxy does), or an
    emit that raises, changes none of `land`'s exit codes: a merge is 0."""
    if how == "push refused":
        hook = lands["origin"] / "hooks" / "pre-receive"
        hook.write_text(
            '#!/bin/sh\nwhile read old new ref; do case "$ref" in refs/loop/*) echo refused >&2; exit 1;; esac; done\n'
        )
        hook.chmod(0o755)
    else:

        def boom(*_a: Any, **_k: Any) -> str:
            raise OSError("no git")

        monkeypatch.setattr(approvals.loop_events, "emit", boom)
    monkeypatch.setattr(approvals, "EVENT_GRACE", 0)
    lands["open"](7, {"7.txt": "7\n"})
    got, lines = lands["land"]("7")
    assert got == 0, lines
    assert not git_in(lands["origin"], "for-each-ref", "refs/loop").strip()


def test_land_wait_ceiling_asks_the_owner(
    events_pushed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wait that reaches its ceiling ends `needs-owner`, the whole question in `question.md`."""
    lands = events_pushed
    monkeypatch.setattr(approvals, "time", Clock())
    monkeypatch.setattr(approvals, "QUEUE_POLL", approvals.POLL)
    lands["open"](7, {"7.txt": "7\n"})
    hold_lock(lands)
    code, lines = lands["land"]("7", "--wait")
    assert code == approvals.WAITING, lines
    event = lands["event"]("pr7")
    assert event and event["state"] == "needs-owner"
    question = git_in(lands["origin"], "show", "refs/loop/land/pr7:question.md")
    assert "stopped after 2 hours" in question


def test_land_waiting_on_the_owners_go_ends_needs_owner(
    events_pushed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A named PR held for the owner's go ends `needs-owner`, the line `land` prints as the question."""
    lands = events_pushed
    said = "PR #7 waits for the owner's go on its head (`approve merge 7`)"
    monkeypatch.setattr(approvals, "owner_waits", lambda *_a: [said])
    lands["open"](7, {"7.txt": "7\n"})
    code, lines = lands["land"]("7")
    assert code == approvals.WAITING and said in lines, lines
    event = lands["event"]("pr7")
    assert event and event["state"] == "needs-owner"
    assert git_in(lands["origin"], "show", "refs/loop/land/pr7:question.md") == said + "\n"


PULL_URL = "https://example.test/org/repo/pull/{}"


def events_of(lands: dict[str, Any], number: int) -> dict[str, Any] | None:
    return lands["event"](f"pr{number}")


def test_land_ok_event_points_to_the_pr(events_pushed: dict[str, Any]) -> None:
    """`ok` carries the PR's URL, which `read` and `wait` show a lead."""
    lands = events_pushed
    lands["open"](7, {"7.txt": "7\n"})
    lands["pulls"][7]["url"] = PULL_URL.format(7)
    code, lines = lands["land"]("7")
    assert code == 0, lines
    event = events_of(lands, 7)
    assert event and (event["state"], event["url"]) == ("ok", PULL_URL.format(7))


def test_land_merged_outside_land_ends_ok(events_pushed: dict[str, Any]) -> None:
    """A PR the GitHub merge queue merged, or one merged by hand, is read merged by the next `land`, whose
    `queue_landed` is its only end: the event ends `ok` and its refs go."""
    lands = events_pushed
    lands["open"](7, {"7.txt": "7\n"})
    lands["pulls"][7]["state"] = "MERGED"
    code, lines = lands["land"]("7")
    assert code == 0, lines
    # the reference: land merged nothing itself
    assert lands["merged"]() == []
    assert refs_gone(lands)
    assert "PR #7 is merged" in lines


def test_land_merged_outside_land_writes_ok_once(
    events_pushed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The event of a merged PR is written once by a process however many rounds read it merged."""
    lands = events_pushed
    monkeypatch.setattr(approvals, "EVENT_GRACE", 600)  # the refs stay while the rounds run
    lands["open"](7, {"7.txt": "7\n"})
    lands["pulls"][7]["state"] = "MERGED"
    for _ in range(3):
        code, lines = lands["land"]("7")
        assert code == 0, lines
    event = events_of(lands, 7)
    assert event and (event["state"], event["seq"]) == ("ok", 1)


@pytest.fixture(autouse=True)
def fresh_process_state() -> Any:
    """What a land process holds per run (the branches whose end it wrote) starts empty in each test."""
    approvals.landed_ok.clear()
    approvals.ended.clear()
    yield
    approvals.landed_ok.clear()
    approvals.ended.clear()


def batch_wait(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch, round_of: Any
) -> list[tuple[int, str | None]]:
    """`land --wait 7 8` where `landing_here` is `round_of(named)` (7 ends, 8 waits again); the states of 7's event
    seen at each sleep, as (seq, state)."""
    clock = Clock()
    monkeypatch.setattr(approvals, "time", clock)
    monkeypatch.setattr(approvals, "QUEUE_POLL", approvals.POLL)
    # a CI wait whose checks never move runs its round again only after FULL (#1915): a round each poll, as before
    monkeypatch.setattr(approvals, "FULL", approvals.POLL)
    # a few rounds, not the 2 hours the policy's `timeouts` derive (#2228)
    monkeypatch.setattr(approvals, "land_wait", lambda: 6 * approvals.POLL)
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](8, {"8.txt": "8\n"})
    monkeypatch.setattr(approvals, "landing_here", lambda named, *_a, **_k: round_of(named))
    seen: list[tuple[int, str | None]] = []

    def tick(_now: float) -> None:
        event = events_of(lands, 7)
        if event and (not seen or seen[-1][0] != event["seq"]):
            seen.append((event["seq"], event["state"]))

    clock.tick = tick
    lands["land"]("7", "8", "--wait")
    return seen


def test_land_batch_wait_keeps_the_owners_question_of_a_pr_that_ended(
    events_pushed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With PR 7 held for the owner's go and PR 8 waiting on CI, the wait's beat rewrites 8 only: 7's event stays
    `needs-owner` at every sleep (the ceiling leaves 8 `waiting`, a PR still waiting on CI, and 7's question)."""
    lands = events_pushed

    def one_round(named: list[dict[str, Any]]) -> tuple[int, bool]:
        approvals.asks_owner(named[0], ["PR #7 waits for the owner's go"], True)
        return approvals.WAITING, True

    seen = batch_wait(lands, monkeypatch, one_round)
    assert seen and {state for _, state in seen} == {"needs-owner"}, seen
    # the ceiling leaves 8, still waiting on CI, `waiting`, and 7's question as it was
    assert (events_of(lands, 8) or {}).get("state") == "waiting"
    assert (events_of(lands, 7) or {}).get("state") == "needs-owner"


def test_land_batch_wait_keeps_the_ok_of_a_pr_that_merged(
    events_pushed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With PR 7 merged in the first round and PR 8 still waiting, 7's event stays `ok` at seq 1: the beat skips it,
    later rounds read it merged without writing it again, and the ceiling leaves it."""
    lands = events_pushed
    monkeypatch.setattr(approvals, "EVENT_GRACE", 3600)
    calls: list[int] = []

    def one_round(named: list[dict[str, Any]]) -> tuple[int, bool]:
        calls.append(len(named))
        if not calls[1:]:
            lands["pulls"][7]["state"] = "MERGED"
            approvals.land_ended(named[0])
        return approvals.WAITING, True

    seen = batch_wait(lands, monkeypatch, one_round)
    assert seen == [(1, "ok")], seen
    assert len(calls) > 2  # the rounds went on, with 8 alone once 7 was merged
    assert (events_of(lands, 8) or {}).get(
        "state"
    ) == "waiting"  # the ceiling leaves the PR still open, waiting on CI, `waiting`
    event = events_of(lands, 7)
    assert event and (event["state"], event["seq"]) == ("ok", 1)  # and leaves the one that merged


def test_land_beats_waiting_while_it_gates_under_the_lock(
    events_pushed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A round that holds the lock and runs a long gate beats `waiting` meanwhile from the lock's heartbeat, so a
    native `wait` does not read the land as silent."""
    lands = events_pushed
    monkeypatch.setattr(approvals, "BEAT", 0.2)
    lands["open"](7, {"7.txt": "7\n"})
    seen: list[int] = []

    def gating(*_a: Any, **_k: Any) -> tuple[int, bool]:
        end = (
            real_time.monotonic() + 60
        )  # a deadline for a loaded host; two beats end the gate at once
        while real_time.monotonic() < end and len(seen) < 2:
            event = events_of(lands, 7)
            if event and event["state"] == "waiting" and event["seq"] not in seen:
                seen.append(event["seq"])
            real_time.sleep(0.05)
        return 0, False

    monkeypatch.setattr(approvals, "local_queue", gating)
    code, lines = lands["land"]("7", "--wait")
    assert code == 0, lines
    assert len(seen) >= 2, seen


def test_land_draft_held_for_the_owners_go_ends_needs_owner(
    events_pushed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A draft is held before it is readied, so CI never starts ahead of the go: its event ends `needs-owner` and
    the draft stays a draft."""
    lands = events_pushed
    said = "PR #7 waits for the owner's go on its head (`approve merge 7`)"
    monkeypatch.setattr(approvals, "owner_waits", lambda *_a: [said])
    lands["open"](7, {"7.txt": "7\n"})
    lands["pulls"][7].update(isDraft=True, url=PULL_URL.format(7))
    code, lines = lands["land"]("7")
    assert code == approvals.WAITING and said in lines, lines
    event = events_of(lands, 7)
    assert event and (event["state"], event["url"]) == ("needs-owner", PULL_URL.format(7))
    assert lands["pulls"][7]["isDraft"] and lands["merged"]() == []
    assert git_in(lands["origin"], "show", "refs/loop/land/pr7:question.md") == said + "\n"


def test_land_waiting_and_ceiling_events_point_to_the_pr(
    events_pushed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`waiting` and `needs-owner` carry the PR's URL too."""
    lands = events_pushed
    clock = Clock()
    monkeypatch.setattr(approvals, "time", clock)
    monkeypatch.setattr(approvals, "QUEUE_POLL", approvals.POLL)
    lands["open"](7, {"7.txt": "7\n"})
    lands["pulls"][7]["url"] = PULL_URL.format(7)
    hold_lock(lands)
    urls: list[tuple[str, str]] = []

    def tick(_now: float) -> None:
        event = events_of(lands, 7)
        if event and (not urls or urls[-1] != (event["state"], event["url"])):
            urls.append((event["state"], event["url"]))

    clock.tick = tick
    lands["land"]("7", "--wait")
    assert urls == [("waiting", PULL_URL.format(7))]
    event = events_of(lands, 7)  # the ceiling comes after the last sleep
    assert event and (event["state"], event["url"]) == ("needs-owner", PULL_URL.format(7))


def test_the_clear_of_a_merged_pr_runs_after_its_worktree_is_gone(
    events_pushed: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A land run again in a fresh worktree (caught_up) ends and the worktree is removed; the refs of the PR it merged
    still go, since the clear runs from the repository's git dir."""
    lands = events_pushed
    work = lands["work"]
    fresh = tmp_path / "fresh"
    git_in(work, "worktree", "add", "-q", "--detach", str(fresh), "HEAD")
    lands["open"](7, {"7.txt": "7\n"})
    git_in(work, "switch", "-q", "--detach", "origin/main")
    monkeypatch.chdir(fresh)
    monkeypatch.setattr(approvals, "EVENT_GRACE", 2)
    code, lines = lands["land"]("7")
    assert code == 0, lines
    git_in(work, "worktree", "remove", "--force", str(fresh))  # reap, as the land exits
    assert not fresh.exists()
    # the clear starts after the grace and pushes twice: a loaded host needs more than the default window
    assert refs_gone(lands, 60)


def test_comment_returns_the_url_of_the_comment_it_posts(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`comment` asks `gh` for the new comment's `html_url`, and gives back what it prints."""
    fake = tmp_path / "bin" / "gh"
    fake.parent.mkdir()
    fake.write_text(
        "#!/bin/sh\n"
        'case "$*" in *"--jq .html_url"*) echo https://example.test/pull/7#issuecomment-9;; esac\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    assert approvals.github.comment(7, "a note") == "https://example.test/pull/7#issuecomment-9"


def test_rest_pull_and_the_graphql_query_carry_the_pr_url(cloud: Cloud) -> None:
    """Both ways a PR is read give its `url`, which every `land` event points to."""
    assert "number url title" in approvals.github.PULL
    cloud.routes.update(
        {
            "repos/{owner}/{repo}/issues/7/timeline?per_page=100": [],
            "repos/{owner}/{repo}/git/matching-refs/heads/gh-readonly-queue/main/pr-7-": [],
        }
    )
    cloud.routes["repos/{owner}/{repo}/pulls/7"]["html_url"] = PULL_URL.format(7)
    assert approvals.github.rest_pull(7)["url"] == PULL_URL.format(7)


def test_the_heartbeat_beats_the_pr_still_gating_not_the_one_that_ended(
    events_pushed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the lock with 7 and 8, 7 is evicted while 8 still gates: the heartbeat beats 8 and leaves 7's `failed`."""
    lands = events_pushed
    monkeypatch.setattr(approvals, "BEAT", 0.2)
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](8, {"8.txt": "8\n"})
    seen: list[int] = []

    def gating(*_a: Any, **_k: Any) -> tuple[int, bool]:
        approvals.land_event(approvals.beating_for[0], "failed")
        end = real_time.monotonic() + 60
        while real_time.monotonic() < end and len(seen) < 3:
            event = events_of(lands, 8)
            if event and event["seq"] not in seen:
                seen.append(event["seq"])
            real_time.sleep(0.05)
        return 0, False

    monkeypatch.setattr(approvals, "local_queue", gating)
    code, lines = lands["land"]("7", "8", "--wait")
    assert code == 0, lines
    assert len(seen) >= 3, seen
    assert (events_of(lands, 7) or {}).get("state") == "failed"


def test_a_new_round_beats_waiting_for_the_pr_that_asked_the_owner_last_round(
    events_pushed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """7 ended `needs-owner` in an earlier round of this process; the owner's go came, and the next round waits on its
    CI: that round starts with no ends, so its beat rewrites the event to `waiting`."""
    lands = events_pushed
    monkeypatch.setattr(approvals, "time", Clock())
    monkeypatch.setattr(approvals, "QUEUE_POLL", approvals.POLL)
    lands["open"](7, {"7.txt": "7\n"})
    approvals.land_event(lands["pulls"][7], "needs-owner", question="Go?")
    rounds: list[int] = []

    def one_round(named: list[dict[str, Any]], *_a: Any, **_k: Any) -> tuple[int, bool]:
        rounds.append(1)
        return (approvals.WAITING, True) if len(rounds) < 2 else (0, False)

    monkeypatch.setattr(approvals, "landing_here", one_round)
    lands["land"]("7", "--wait")
    event = events_of(lands, 7)
    assert event and event["state"] == "waiting", event


def gated_wait(
    lands: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    beat: float,
    gate: float,
    nap: float,
    ci_first: bool,
) -> dict[str, Any]:
    """Scaled: the land beat (events' beat_s) and the lock heartbeat (BEAT) are both `beat` real seconds, standing for
    300 s; the gate takes `gate` s. Round 1 waits (the lock held, or with `ci_first` PR 7's CI pending) and beats
    `waiting`; a `wait land pr7` starts on that beat; round 2 starts `nap` real seconds later, takes the lock and gates.
    Returns the land's exit, what merged and the wait's exit."""
    events = approvals.loop_events
    monkeypatch.setitem(events.BEATS, "land", beat)
    monkeypatch.setattr(approvals, "BEAT", beat)
    # the `ok` stays readable: a waiter polling every 0.2 s must not find the ref cleared before it reads the end
    monkeypatch.setattr(approvals, "EVENT_GRACE", 3600)
    monkeypatch.setattr(
        approvals, "LOCAL_CI", ["sh", "-c", f'sleep {gate}; exec "$0" "$@"', *approvals.LOCAL_CI]
    )
    clock = Clock()
    monkeypatch.setattr(approvals, "time", clock)
    monkeypatch.setattr(approvals, "QUEUE_POLL", approvals.POLL)
    lands["open"](7, {"7.txt": "7\n"})
    if ci_first:
        lands["pulls"][7]["waiting"] = (approvals.WAITING, True)
    else:
        hold_lock(lands)
    result: dict[str, Any] = {}

    def wait() -> None:
        result["code"] = events.wait(
            "land", "pr7", timeout=120, poll=0.2, cwd=lands["work"], out=sys.stderr
        )

    waiter = threading.Thread(target=wait)

    def tick(_now: float) -> None:
        if lands["event"]("pr7") and "started" not in result:
            result["started"] = True
            waiter.start()
            if ci_first:
                lands["pulls"][7].pop("waiting")
            else:
                free_lock(lands)
            real_time.sleep(nap)  # the real time the wait loop's nap takes before round 2

    clock.tick = tick
    code, _lines = lands["land"]("7", "--wait")
    waiter.join(timeout=120)
    result.update(land=code, merged=lands["merged"]())
    return result


@pytest.mark.slow  # real seconds on purpose: 6 s beats and a 20 s gate (#1610)
@pytest.mark.timeout(
    240
)  # scaled real seconds: beats of 6 s and a 20 s gate, longer on a loaded host
def test_a_wait_on_a_land_whose_round_starts_late_and_gates_long_never_goes_silent(
    events_pushed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 1 waits on PR 7's CI and beats; the next round starts a low-quota nap later (300 s, scaled), takes the lock
    and gates for longer than a beat. The heartbeat beats at the lock, so the silence stays under two beats and a
    native `wait land` ends 0 instead of 4."""
    result = gated_wait(events_pushed, monkeypatch, beat=6, gate=20, nap=7.2, ci_first=True)
    assert result["land"] == 0 and result["merged"] == ["7"]
    assert result.get("code") == 0, result


@pytest.mark.slow  # a real heartbeat thread and a 1 s gate (#1610)
def test_v2_heartbeat_beat_in_flight_does_not_replace_ok(
    events_pushed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heartbeat checks `ended`, then emits; an `ok` written by the main thread between the two is replaced by
    the beat's `waiting`. Forced here: the heartbeat's emit for pr7 is held until the `ok` is written, as a beat whose
    ls-remote and push (up to 60 s each) are in flight when the merge lands."""
    lands = events_pushed
    events = approvals.loop_events
    monkeypatch.setattr(approvals, "BEAT", 0.3)
    monkeypatch.setattr(approvals, "EVENT_GRACE", 3600)
    monkeypatch.setattr(
        approvals, "LOCAL_CI", ["sh", "-c", 'sleep 1; exec "$0" "$@"', *approvals.LOCAL_CI]
    )
    clock = Clock()
    monkeypatch.setattr(approvals, "time", clock)
    lands["open"](7, {"7.txt": "7\n"})
    real_emit = events.emit
    ok_written = threading.Event()
    held = {"n": 0}

    def emit(kind: str, branch: str, state: str, **kw: Any) -> str:
        if state == "waiting" and threading.current_thread() is not threading.main_thread():
            held["n"] += 1
            ok_written.wait(timeout=10)
        sha = real_emit(kind, branch, state, **kw)
        if state == "ok":
            ok_written.set()
        return sha

    monkeypatch.setattr(events, "emit", emit)
    code, _lines = lands["land"]("7", "--wait")
    real_time.sleep(1)  # the heartbeat thread's held emit finishes
    event = lands["event"]("pr7")
    assert code == 0 and lands["merged"]() == ["7"] and held["n"] >= 1
    assert event and event["state"] == "ok", event
