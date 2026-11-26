"""approvals.py: catch_up's range-diff proof refuses a range that holds a merge commit, since `git range-diff` leaves
merges out and a merge whose tree adds an edit still shows every commit `=` (#1497). A plain rebase still carries:
test_approvals_catch_up.test_a_clean_rebase_carries."""

from __future__ import annotations

from pathlib import Path

from approvals_harness import approvals
from test_approvals_catch_up import (  # noqa: F401  (repo is a fixture)
    Repo,
    feature,
    moved,
    pr,
    repo,
)


def test_a_rebase_with_a_merge_that_adds_a_file_carries_nothing(repo: Repo) -> None:
    old = feature(repo)
    old_base = repo("rev-parse", "main")
    tip = moved(repo)
    repo("switch", "-qc", "pr2", "main")
    repo("cherry-pick", f"{old}~1", old)
    Path("extra.txt").write_text("nobody reviewed this\n")
    repo("add", "-A")
    tree = repo("write-tree")
    merge = repo("commit-tree", tree, "-p", "HEAD", "-p", old_base, "-m", "sneak")
    shown = repo("range-diff", "--no-color", f"{old_base}..{old}", f"{tip}..{merge}")
    # the blind spot: range-diff shows every commit `=`
    assert [line.split()[2] for line in shown.splitlines()] == ["=", "="]
    assert approvals.catch_up(pr(repo, merge, tip), old) is None


def test_a_rebase_that_drops_a_merge_from_the_reviewed_head_carries_nothing(repo: Repo) -> None:
    reviewed = feature(repo)
    old_base = repo("rev-parse", "main")
    Path("extra.txt").write_text("reviewed only inside the merge\n")
    repo("add", "-A")
    tree = repo("write-tree")
    old = repo("commit-tree", tree, "-p", reviewed, "-p", old_base, "-m", "merge with an edit")
    repo("reset", "-q", "--hard", reviewed)
    tip = moved(repo)
    repo("switch", "-qc", "pr2", "main")
    repo("cherry-pick", f"{reviewed}~1", reviewed)
    head = repo("rev-parse", "HEAD")
    # the blind spot from the other side: the reviewed head's merge is left out, so every commit shows `=`
    shown = repo("range-diff", "--no-color", f"{old_base}..{old}", f"{tip}..{head}")
    assert [line.split()[2] for line in shown.splitlines()] == ["=", "="]
    assert approvals.catch_up(pr(repo, head, tip), old) is None
