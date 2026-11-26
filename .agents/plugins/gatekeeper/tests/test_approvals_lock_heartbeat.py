"""approvals.py: a land that holds the landing lock re-stamps it, and a lock no land stamps any more is broken (#1260)."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from approvals_harness import (
    approvals,
    git_in,
    lands,  # noqa: F401
)

REF = approvals.LOCK.format("main")


def lock_aged(lands: dict[str, Any], minutes: int, body: str) -> str:
    """Another land's HELD lock commit, made `minutes` ago, pushed onto the lock branch."""
    when = f"@{int(datetime.now(UTC).timestamp()) - minutes * 60} +0000"
    tip = git_in(lands["origin"], "for-each-ref", "--format=%(objectname)", REF).strip()
    tree = git_in(lands["work"], "hash-object", "-t", "tree", "-w", os.devnull).strip()
    commit = subprocess.run(
        [
            "git",
            "commit-tree",
            *(["-p", tip] if tip else []),
            "-m",
            f"{approvals.HELD} [skip ci]\n\n{body}",
            tree,
        ],
        cwd=lands["work"],
        env={**os.environ, "GIT_COMMITTER_DATE": when},
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    git_in(lands["work"], "push", "-q", "origin", f"{commit}:{REF}")
    return commit


def origin_tip(lands: dict[str, Any]) -> str:
    return git_in(lands["origin"], "for-each-ref", "--format=%(objectname)", REF).strip()


def test_a_lock_that_beats_and_is_past_the_stale_limit_is_broken_naming_its_age(
    lands: dict[str, Any],
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    lock_aged(lands, 20, "pid 1, PR #99, beats, nonce a")
    code, lines = lands["land"]("7")
    assert code == 0, lines
    assert any(
        "was last stamped 20 minutes ago, past 15: its land is gone, broken as stale" in x
        for x in lines
    )


def test_a_lock_without_the_beat_word_is_an_earlier_versions_and_waits_for_the_ceiling(
    lands: dict[str, Any],
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    lock_aged(lands, 20, "pid 1, nonce a")
    code, lines = lands["land"]("7")
    assert code == approvals.WAITING
    assert any("another land holds the landing lock" in x for x in lines)


def test_a_young_beating_lock_is_waited_on(lands: dict[str, Any]) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    lock_aged(lands, 5, "pid 1, beats, nonce a")
    assert lands["land"]("7")[0] == approvals.WAITING


def beating(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> tuple[list[str], threading.Event, threading.Thread]:
    monkeypatch.setattr(approvals, "BEAT", 0.02)
    held: list[str] = []
    assert approvals.locked("main", held, 7)
    stop = threading.Event()
    thread = threading.Thread(target=approvals.heartbeat, args=("main", held, stop), daemon=True)
    thread.start()
    return held, stop, thread


def test_a_live_holders_heartbeat_moves_the_lock_and_keeps_the_pr_it_names(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    held, stop, thread = beating(lands, monkeypatch)
    first = held[0]
    deadline = time.monotonic() + 20
    while len(held) < 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    stop.set()
    thread.join(5)
    assert len(held) >= 3
    assert origin_tip(lands) == held[-1]
    parents = git_in(lands["origin"], "rev-list", "--first-parent", held[-1]).split()
    assert parents[: len(held)] == held[::-1]  # a chain of our own commits from the first
    body = git_in(lands["origin"], "log", "-1", "--format=%b", held[-1])
    assert "PR #7" in body and f", {approvals.BEATS}," in body
    assert first in parents


def test_a_holder_whose_lock_was_taken_stops_beating_and_refuses_to_merge(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals, "BEAT", 3600)  # no beat until the test moves the lock
    held: list[str] = []
    assert approvals.locked("main", held, 7)
    lock_aged(lands, 0, "pid 2, PR #8, beats, nonce b")  # another land took it
    monkeypatch.setattr(approvals, "BEAT", 0.02)
    stop = threading.Event()
    thread = threading.Thread(target=approvals.heartbeat, args=("main", held, stop), daemon=True)
    thread.start()
    thread.join(20)
    assert not thread.is_alive() and held == []
    with pytest.raises(approvals.Refused, match="took the landing lock"):
        approvals.repointed("main", held, 7)


def run_beat(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failing: Any
) -> tuple[list[str], threading.Thread]:
    monkeypatch.setattr(approvals, "BEAT", 3600)
    held: list[str] = []
    assert approvals.locked("main", held, 7)
    monkeypatch.setattr(approvals, "BEAT", 0.02)
    monkeypatch.setattr(approvals, "leased", failing)
    thread = threading.Thread(
        target=approvals.heartbeat, args=("main", held, threading.Event()), daemon=True
    )
    thread.start()
    return held, thread


def test_a_beat_lost_to_the_network_keeps_the_lock_and_tries_again(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_leased, real_git, calls = approvals.leased, approvals.git, []

    def flaky(base: str, new: str, old: str, **kw: Any) -> Any:
        calls.append(new)
        return (
            subprocess.CompletedProcess([], 1, "", "net")
            if len(calls) == 1
            else real_leased(base, new, old)
        )

    def down(*args: str) -> str:
        if args[:2] == ("ls-remote", "origin") and len(calls) == 1:
            raise subprocess.CalledProcessError(128, ["git", *args])
        return real_git(*args)

    monkeypatch.setattr(approvals, "git", down)
    held, thread = run_beat(lands, monkeypatch, flaky)
    deadline = time.monotonic() + 20
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert thread.is_alive() and held and len(calls) >= 2


def test_a_beat_that_reached_origin_but_reported_failure_stays_in_held(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_leased = approvals.leased
    seen: list[str] = []

    def lost_answer(base: str, new: str, old: str, **kw: Any) -> Any:
        real_leased(base, new, old)
        seen.append(new)
        return subprocess.CompletedProcess([], 1, "", "reset")

    held, _ = run_beat(lands, monkeypatch, lost_answer)
    deadline = time.monotonic() + 20
    while not seen and time.monotonic() < deadline:
        time.sleep(0.02)
    assert seen and seen[0] in held and held[-1] != held[0]


def test_a_lost_answer_then_an_unreadable_tip_keeps_the_land_beating_and_its_lock_its_own(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_leased, real_git, seen = approvals.leased, approvals.git, []

    def lost_answer(base: str, new: str, old: str, **kw: Any) -> Any:
        if not seen:  # the first beat reaches origin, its answer and the read after it are lost
            real_leased(base, new, old)
            seen.append(new)
            return subprocess.CompletedProcess([], 1, "", "reset")
        return real_leased(base, new, old)

    def down(*args: str) -> str:
        if args[:2] == ("ls-remote", "origin") and len(seen) == 1 and not down.once:
            down.once = True
            raise subprocess.CalledProcessError(128, ["git", *args])
        return real_git(*args)

    down.once = False  # type: ignore[attr-defined]
    monkeypatch.setattr(approvals, "git", down)
    held, thread = run_beat(lands, monkeypatch, lost_answer)
    deadline = time.monotonic() + 20
    while len(held) < 4 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert thread.is_alive() and origin_tip(lands) in held and len(held) >= 4


def test_a_beat_that_hangs_is_timed_out_and_gives_beating_back(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []

    def hangs(base: str, new: str, old: str, **kw: Any) -> Any:
        seen.append(kw.get("timeout"))
        raise subprocess.TimeoutExpired("git", kw["timeout"])

    _, thread = run_beat(lands, monkeypatch, hangs)
    deadline = time.monotonic() + 20
    while not seen and time.monotonic() < deadline:
        time.sleep(0.02)
    assert seen[0] == approvals.BEAT_PUSH and thread.is_alive()
    assert approvals.beating.acquire(timeout=5)
    approvals.beating.release()


def test_the_beat_thread_blocks_the_stop_signals(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal

    masks: list[set[int]] = []

    def peek(base: str, new: str, old: str, **kw: Any) -> Any:
        masks.append(signal.pthread_sigmask(signal.SIG_BLOCK, []))
        return subprocess.CompletedProcess([], 1)

    run_beat(lands, monkeypatch, peek)
    deadline = time.monotonic() + 20
    while not masks and time.monotonic() < deadline:
        time.sleep(0.02)
    assert set(approvals.STOPS) <= masks[0]
