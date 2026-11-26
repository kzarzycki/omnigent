"""approvals.py: a free landing lock goes to the land that began waiting first (#1216)."""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any

import pytest
from approvals_harness import (
    approvals,
    git_in,
    lands,  # noqa: F401
)


def waiter(
    lands: dict[str, Any],
    who: str,
    since: float,
    aged: int = 0,
    title: str = "waiting",
    pr: int = 99,
) -> None:
    """Another land's waiting ref: began at `since`, last stamped `aged` seconds ago."""
    when = f"@{int(time.time()) - aged} +0000"
    tree = git_in(lands["work"], "hash-object", "-t", "tree", "-w", os.devnull).strip()
    commit = subprocess.run(
        [
            "git",
            "commit-tree",
            "-m",
            f"{title} [skip ci]\n\nsince {since:.3f}, PR #{pr}, pid 1, nonce 0",
            tree,
        ],
        cwd=lands["work"],
        env={**os.environ, "GIT_COMMITTER_DATE": when},
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    git_in(
        lands["work"],
        "push",
        "-q",
        "-f",
        "origin",
        f"{commit}:{approvals.WAIT_REF.format('main', who)}",
    )


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(approvals, "waiting", True)
    monkeypatch.setattr(
        approvals,
        "waited",
        {"since": 0.0, "id": "", "base": "", "tip": "", "took": False, "idle": False},
    )


def refs(lands: dict[str, Any]) -> list[str]:
    out = git_in(lands["origin"], "for-each-ref", "--format=%(refname)", "refs/heads/loop-waiting")
    return out.split()


def test_a_land_that_began_waiting_later_leaves_the_free_lock_to_the_older_waiter(
    lands: dict[str, Any], waits: None, capsys: pytest.CaptureFixture[str]
) -> None:
    waiter(lands, "aaaa", time.time() - 600)
    held: list[str] = []
    assert approvals.locked("main", held, 7) is False and held == []
    assert "PR #99 began waiting for the landing lock" in capsys.readouterr().out
    assert len(refs(lands)) == 2  # its own place is kept while it waits


def test_a_waiter_that_stopped_stamping_does_not_hold_up_the_next(
    lands: dict[str, Any], waits: None
) -> None:
    waiter(lands, "aaaa", time.time() - 600, aged=approvals.WAITER_STALE + 60)
    assert approvals.locked("main", [], 7) is True


def test_a_younger_waiter_does_not_block_the_older(lands: dict[str, Any], waits: None) -> None:
    waiter(lands, "zzzz", time.time() + 600)
    assert approvals.locked("main", [], 7) is True


def test_taking_the_lock_deletes_the_lands_own_waiting_ref(
    lands: dict[str, Any], waits: None
) -> None:
    assert approvals.locked("main", [], 7) is True
    assert refs(lands) == []


def test_a_land_without_wait_ignores_waiters_and_leaves_no_ref(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals, "waiting", False)
    waiter(lands, "aaaa", time.time() - 600)
    assert approvals.locked("main", [], 7) is True
    assert refs(lands) == [approvals.WAIT_REF.format("main", "aaaa")]


def test_unwait_deletes_the_ref_and_forget_drops_the_place(
    lands: dict[str, Any], waits: None
) -> None:
    approvals.stamp_wait("main", 7)
    assert len(refs(lands)) == 1
    approvals.unwait()
    assert refs(lands) == [] and approvals.waited["since"] > 0
    approvals.unwait(forget=True)
    assert approvals.waited["id"] == "" and approvals.waited["since"] == 0.0


def test_two_lands_that_began_in_the_same_second_do_not_both_yield(
    lands: dict[str, Any], waits: None
) -> None:
    """The other's since reads 1000.300; this land's is 1000.400: only one of the two yields."""
    waiter(lands, "aaaa", 1000.3)
    approvals.waited.update(since=1000.4, id="zzzz")
    assert approvals.waiter_ahead("main") == "PR #99"
    approvals.waited.update(since=1000.2, id="zzzz")
    assert approvals.waiter_ahead("main") is None


def test_the_oldest_of_several_waiters_is_the_one_named(lands: dict[str, Any], waits: None) -> None:
    waiter(lands, "bbbb", time.time() - 100, pr=2)
    waiter(lands, "aaaa", time.time() - 300, pr=1)
    approvals.waited["since"] = time.time()
    assert approvals.waiter_ahead("main") == "PR #1"


def test_a_done_ref_does_not_hold_the_lock(lands: dict[str, Any], waits: None) -> None:
    waiter(lands, "aaaa", time.time() - 600, title="done")
    assert approvals.locked("main", [], 7) is True


def test_a_land_that_took_the_lock_keeps_its_place(lands: dict[str, Any], waits: None) -> None:
    assert approvals.locked("main", [], 7) is True
    began = approvals.waited["since"]
    assert began > 0 and approvals.waited["id"]
    approvals.stamp_wait("main", 7)  # its next poll
    assert approvals.waited["since"] == began


def test_stamps_chain_as_fast_forwards(lands: dict[str, Any], waits: None) -> None:
    approvals.stamp_wait("main", 7)
    first = approvals.waited["tip"]
    approvals.stamp_wait("main", 7)
    ref = approvals.WAIT_REF.format("main", approvals.waited["id"])
    assert git_in(lands["origin"], "rev-parse", f"{ref}~1").strip() == first


@pytest.mark.parametrize("ending", [3, RuntimeError("boom")])
def test_a_wait_that_ends_without_taking_the_lock_leaves_no_ref(
    lands: dict[str, Any], waits: None, monkeypatch: pytest.MonkeyPatch, ending: Any
) -> None:
    def stamps_then_ends(pr: Any, only: bool) -> int:
        approvals.stamp_wait("main", 7)
        assert len(refs(lands)) == 1
        if isinstance(ending, Exception):
            raise ending
        return ending

    monkeypatch.setattr(approvals, "wait_loop", stamps_then_ends)
    try:
        approvals.land(7, wait=True)
    except RuntimeError:
        pass
    assert refs(lands) == [] and approvals.waited["id"] == ""


def test_a_waiter_whose_last_round_landed_nothing_does_not_hold_the_queue(
    lands: dict[str, Any], waits: None
) -> None:
    """A took the lock, found nothing landable and stamped idle: B, who began later, may take the free lock."""
    approvals.waited.update(since=time.time() - 600, id="aaaa")
    assert approvals.locked("main", [], 7) is True  # A's take of the free lock
    approvals.waited["idle"] = True  # what wait_loop sets when the round landed nothing
    approvals.stamp_wait("main", 7)
    approvals.waited.update(since=time.time(), id="bbbb", tip="", took=False, idle=False)
    assert approvals.waiter_ahead("main") is None
