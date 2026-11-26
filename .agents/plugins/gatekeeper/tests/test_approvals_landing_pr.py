"""approvals.py: a landing lock whose PR already merged is released, not held for 2 hours (#901)."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from typing import Any

import pytest
from approvals_harness import (
    approvals,
    git_in,
    lands,  # noqa: F401
)

REF, NAME = approvals.LOCK.format("main"), "loop-landing/main"


def held_by(lands: dict[str, Any], body: str, seconds_ago: int = 10 * 60) -> str:
    """Another land's `held` lock commit with `body`, taken `seconds_ago`, pushed onto the lock branch's tip."""
    when = f"@{int(datetime.now(UTC).timestamp()) - seconds_ago} +0000"
    tip = git_in(lands["origin"], "for-each-ref", "--format=%(objectname)", REF).strip()
    tree = git_in(lands["work"], "hash-object", "-t", "tree", "-w", os.devnull).strip()
    held = subprocess.run(
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
    git_in(lands["work"], "push", "-q", "origin", f"{held}:{REF}")
    return held


def test_a_held_lock_naming_a_pr_merged_into_the_base_is_taken_at_once(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #831 case: its land merged it and died before it freed the lock, minutes ago."""
    lands["open"](7, {"7.txt": "7\n"})
    # PR #5's merge, made on origin after this checkout last fetched main: the land fetches before it looks
    origin, tip = lands["origin"], lands["main"]()
    done = git_in(
        origin,
        *("-c", "user.name=GitHub", "-c", "user.email=gh@example.com", "commit-tree"),
        *(f"{tip}^{{tree}}", "-p", tip, "-m", "PR 5 (#5)"),
    ).strip()
    git_in(origin, "update-ref", "refs/heads/main", done, tip)
    monkeypatch.setattr(
        approvals,
        "merged_pull",
        lambda n: {"merged": True, "merge_commit_sha": done} if n == 5 else None,
    )
    dead = held_by(lands, "pid 1, PR #5, nonce 00")
    code, lines = lands["land"]("7")
    assert code == 0, lines
    assert lines[0] == (
        f"the landing lock {NAME} (pid 1, PR #5) is released: PR #5 merged into main"
        f" as {done}: broken"
    )
    assert (lands["lock"](), lands["merged"]()) == ("", ["7"])
    # broken as a stale one is: taken in a push of a held child of the dead land's commit
    assert dead in git_in(lands["origin"], "rev-list", REF).split()


def test_a_held_lock_naming_an_open_pr_is_waited_on(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    asked: list[int] = []
    monkeypatch.setattr(approvals, "merged_pull", lambda n: asked.append(n))
    held = held_by(lands, "pid 1, PR #5, nonce 00")
    code, lines = lands["land"]("7")
    assert (code, asked) == (approvals.WAITING, [5])
    assert lines[0].startswith(
        f"another land holds the landing lock {NAME} (pid 1, PR #5), taken 10 minutes ago ("
    )
    assert (lands["lock"](), lands["merged"]()) == (held, [])


def test_a_held_lock_naming_a_merged_pr_the_base_lacks_is_waited_on(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub says merged, but the base origin has holds no such commit (a merge queue's, or one rewritten away)."""
    elsewhere = lands["open"](8, {"8.txt": "8\n"})
    lands["open"](7, {"7.txt": "7\n"})
    monkeypatch.setattr(
        approvals, "merged_pull", lambda _n: {"merged": True, "merge_commit_sha": elsewhere}
    )
    held = held_by(lands, "pid 1, PR #5, nonce 00")
    code, lines = lands["land"]("7", "--only")
    assert code == approvals.WAITING, lines
    assert (lands["lock"](), lands["merged"]()) == (held, [])


def test_a_held_lock_whose_pr_github_cannot_read_is_waited_on(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    lands["open"](7, {"7.txt": "7\n"})

    def failing(_n: int) -> None:
        raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502")

    monkeypatch.setattr(approvals, "merged_pull", failing)
    held = held_by(lands, "pid 1, PR #5, nonce 00")
    assert lands["land"]("7")[0] == approvals.WAITING
    assert lands["lock"]() == held


def test_a_lock_written_before_this_change_keeps_the_two_hour_rule(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No PR named: no GitHub read, waited on under 2 hours, broken as stale past them."""
    lands["open"](7, {"7.txt": "7\n"})
    asked: list[int] = []
    monkeypatch.setattr(approvals, "merged_pull", lambda n: asked.append(n))
    held = held_by(lands, "pid 1, nonce 00", 119 * 60)
    code, lines = lands["land"]("7")
    assert code == approvals.WAITING
    assert lines[0].startswith(
        f"another land holds the landing lock {NAME} (pid 1), taken 119 minutes"
    )
    assert lands["lock"]() == held
    held_by(lands, "pid 1, nonce 00", 3 * 60 * 60)
    code, lines = lands["land"]("7")
    assert (code, lines[0]) == (
        0,
        f"the landing lock {NAME} is 3 hours old, past 2 hours: broken as stale",
    )
    assert (asked, lands["merged"]()) == ([], ["7"])


def test_a_named_pr_past_two_hours_is_broken_as_stale_whatever_github_says(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ceiling stays for a holder that died before its merge."""
    lands["open"](7, {"7.txt": "7\n"})
    monkeypatch.setattr(approvals, "merged_pull", lambda _n: None)
    held_by(lands, "pid 1, PR #5, nonce 00", 3 * 60 * 60)
    code, lines = lands["land"]("7")
    assert (code, lines[0]) == (
        0,
        f"the landing lock {NAME} is 3 hours old, past 2 hours: broken as stale",
    )


def lock_bodies(lands: dict[str, Any]) -> list[str]:
    """Each commit of the lock branch, oldest first: its title's state, then its body less the nonce."""
    log = git_in(lands["origin"], "log", "--reverse", "--format=%s%x00%b%x01", REF)
    return [
        f"{title.split(' [')[0].removeprefix('landing lock: ')}: {body.strip().split(', nonce ')[0]}"
        for title, body in (entry.strip().split("\0") for entry in log.split("\1") if entry.strip())
    ]


def test_a_land_names_its_pr_and_moves_the_lock_to_its_batchs_last_pr_before_merging(
    lands: dict[str, Any],
) -> None:
    """A batch merges oldest first: the lock names its last PR, so it reads released only once all of them merged."""
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](8, {"8.txt": "8\n"})
    pid = os.getpid()
    assert lands["land"]("7")[0] == 0
    assert lands["merged"]() == ["7", "8"]
    assert lock_bodies(lands) == [
        f"held: pid {pid}, PR #7, beats",
        f"held: pid {pid}, PR #8, beats",
        f"free: pid {pid}, beats",
    ]
    # a land naming no PR takes the lock naming none, the old format, until its batch is known
    lands["open"](9, {"9.txt": "9\n"})
    assert lands["land"]()[0] == 0
    assert lock_bodies(lands)[3:] == [
        f"held: pid {pid}, beats",
        f"held: pid {pid}, PR #9, beats",
        f"free: pid {pid}, beats",
    ]


def test_a_land_whose_lock_was_broken_before_its_merges_merges_nothing_and_leaves_their_lock(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another land took the lock meanwhile (it read this one's PR as merged by hand): the move is refused."""
    lands["open"](7, {"7.txt": "7\n"})
    theirs: list[str] = []
    batch = approvals.merge_batch

    def taken_meanwhile(*args: Any) -> Any:
        landed = batch(*args)
        theirs.append(held_by(lands, "pid 2, nonce 00", 0))
        return landed

    monkeypatch.setattr(approvals, "merge_batch", taken_meanwhile)
    code, lines = lands["land"]("7")
    assert code == 1
    assert lines[-1].startswith(
        f"another land took the landing lock {NAME} before this one's merges"
    )
    assert not any("was not released" in line for line in lines)
    assert (lands["lock"](), lands["merged"]()) == (theirs[-1], [])
