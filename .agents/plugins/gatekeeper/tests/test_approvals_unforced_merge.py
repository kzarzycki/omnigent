"""approvals.py: check merge and the verifier brief flag a merge of the base nothing forced (#939)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import (
    LOOP,
    approvals,
    note,
    pull,
    verdict_on,
)

LINE = "unforced merge of main: land merges heads itself (the lead role, step 8); this pass is avoidable"


def git(where: Path, *args: str) -> str:
    who = ("-c", "commit.gpgsign=false", "-c", "user.name=Lead", "-c", "user.email=l@example.com")
    return subprocess.run(
        ["git", *who, *args], cwd=where, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A checkout whose main moved after the PR's head `head` left it: `merge` is a clean merge of main's new tip
    `base` into `head`, `then` a PR commit on top of that merge, `again` a second clean merge of main into `merge`."""
    monkeypatch.chdir(tmp_path)
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "app.py").write_text("one\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    git(tmp_path, "switch", "-qc", "feature")
    (tmp_path / "app.py").write_text("one, the feature\n")
    git(tmp_path, "commit", "-qam", "feature")
    head = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "switch", "-q", "main")
    (tmp_path / "other.txt").write_text("main moved\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "main moved")
    first = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "switch", "-q", "feature")
    git(tmp_path, "merge", "-q", "--no-ff", "-m", "Merge main", "main")
    merge = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "switch", "-q", "main")
    (tmp_path / "more.txt").write_text("main moved again\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "main moved again")
    base = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "switch", "-q", "feature")
    git(tmp_path, "merge", "-q", "--no-ff", "-m", "Merge main again", "main")
    again = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "switch", "-qc", "after", merge)
    (tmp_path / "app.py").write_text("one, the feature, after\n")
    git(tmp_path, "commit", "-qam", "after the merge")
    then = git(tmp_path, "rev-parse", "HEAD")
    return {
        "head": head,
        "first": first,
        "merge": merge,
        "base": base,
        "again": again,
        "then": then,
    }


def merged_pull(
    case: dict[str, str], at: str = "merge", on: str = "head", *comments: str | dict[str, Any]
) -> dict[str, Any]:
    """The PR with its head at `case[at]`, main's tip at the newest main commit, a satisfied verdict on `case[on]`."""
    pr = pull(reviews=(verdict_on(case[on]),), comments=comments)
    pr.update(headRefOid=case[at], baseRefOid=case["base"])
    return pr


def conflicts(head: str, author: str = "loop") -> dict[str, Any]:
    """land's comment on a head that conflicted in its batch, as merged_onto() writes it."""
    body = f"{approvals.CONFLICTS}\nHead: {head}\n\nIt conflicts with main: merge origin/main in, rerun the gates.\n"
    return note(body, author={"login": author})


def test_a_clean_merge_of_main_with_no_conflict_comment_is_unforced(repo: dict[str, str]) -> None:
    assert approvals.unforced(merged_pull(repo)) == LINE
    # two merges in a row, neither forced
    assert approvals.unforced(merged_pull(repo, "again")) == LINE


def test_a_merge_after_land_named_the_earlier_head_in_a_conflict_comment_is_not_flagged(
    repo: dict[str, str],
) -> None:
    assert approvals.unforced(merged_pull(repo, "merge", "head", conflicts(repo["head"]))) is None
    # a conflict on the head between two merges forced the second one
    assert approvals.unforced(merged_pull(repo, "again", "head", conflicts(repo["merge"]))) is None


def test_a_conflict_comment_on_another_head_or_by_a_stranger_forces_nothing(
    repo: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals, "writer", lambda login: False)
    assert approvals.unforced(merged_pull(repo, "merge", "head", conflicts(repo["first"]))) == LINE
    stranger = conflicts(repo["head"], author="passer-by")
    assert approvals.unforced(merged_pull(repo, "merge", "head", stranger)) == LINE


@pytest.mark.parametrize(
    ("at", "on"),
    [("merge", "merge"), ("then", "head"), ("head", "head")],
    ids=[
        "the verdict is on the merge",
        "a PR commit after the merge",
        "no commit since the verdict",
    ],
)
def test_no_line_unless_every_commit_since_the_verdict_is_a_merge_of_the_base(
    repo: dict[str, str], at: str, on: str
) -> None:
    assert approvals.unforced(merged_pull(repo, at, on)) is None


def test_no_line_without_a_verdict(repo: dict[str, str]) -> None:
    pr = merged_pull(repo)
    pr["reviews"] = {"nodes": []}
    assert approvals.unforced(pr) is None


def test_a_clone_behind_origin_fetches_the_head_and_base_tip_it_lacks(
    repo: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lead's clone that holds the verdict's head but not the merge or main's new tip reads the line once a fetch
    brings them; a head no fetch can bring gives no line, and no error."""
    origin, behind = tmp_path / "origin.git", tmp_path / "behind"
    git(tmp_path, "clone", "-q", "--bare", str(tmp_path), str(origin))
    git(origin, "branch", "old", repo["head"])
    git(
        tmp_path,
        "clone",
        "-q",
        "--no-local",
        "--single-branch",
        "-b",
        "old",
        str(origin),
        str(behind),
    )
    git(behind, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    monkeypatch.chdir(behind)
    for sha in (repo["merge"], repo["base"]):
        missing = subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], check=False)
        assert missing.returncode, f"{sha} is already here"
    assert approvals.unforced(merged_pull(repo)) == LINE
    for key in ("headRefOid", "baseRefOid"):
        pr = merged_pull(repo)
        pr[key] = "c" * 40
        assert approvals.unforced(pr) is None


def test_check_merge_and_the_brief_print_the_line(
    repo: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pr = merged_pull(repo)
    monkeypatch.setattr(approvals, "pull_request", lambda _n: pr)
    monkeypatch.setattr(approvals, "project", lambda: (None, LOOP))
    monkeypatch.setattr(approvals, "merge_base", lambda base, head: repo["head"])
    approvals.main(["check", "merge", "7"])
    assert f"PR #7: {LINE}" in capsys.readouterr().out.splitlines()
    assert approvals.main(["brief", "7"]) == 0
    assert f"- Note: {LINE}" in capsys.readouterr().out.splitlines()
    pr["comments"]["nodes"].append(conflicts(repo["head"]))
    approvals.main(["check", "merge", "7"])
    assert LINE not in capsys.readouterr().out
    assert approvals.main(["brief", "7"]) == 0
    assert LINE not in capsys.readouterr().out
