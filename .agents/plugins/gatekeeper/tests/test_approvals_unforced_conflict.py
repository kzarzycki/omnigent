"""approvals.py: a merge of the base whose parents conflict is forced, with or without land's CONFLICTS comment (#1203)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from approvals_harness import (
    approvals,
    pull,
    verdict_on,
)

WHO = ("-c", "commit.gpgsign=false", "-c", "user.name=Lead", "-c", "user.email=l@example.com")
LINE = "unforced merge of main: land merges heads itself (the lead role, step 8); this pass is avoidable"


def git(where: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *WHO, *args], cwd=where, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The PR's verified head `head` and main's tip `base` both changed app.py; `merge` resolves that conflict by
    hand, and `clean` is a second merge of main on top of it that conflicts with nothing."""
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
    (tmp_path / "app.py").write_text("one, on main\n")
    git(tmp_path, "commit", "-qam", "main changed the same line")
    git(tmp_path, "switch", "-q", "feature")
    # stops on the conflict, which the next commit resolves
    assert (
        subprocess.run(
            ["git", *WHO, "merge", "-q", "main"], cwd=tmp_path, capture_output=True, check=False
        ).returncode
        == 1
    )
    (tmp_path / "app.py").write_text("one, on main, the feature\n")
    git(tmp_path, "add", "app.py")
    git(tmp_path, "commit", "-qm", "Merge main, conflict resolved")
    merge = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "switch", "-q", "main")
    (tmp_path / "other.txt").write_text("main moved again\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "main moved again")
    base = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "switch", "-q", "feature")
    git(tmp_path, "merge", "-q", "--no-ff", "-m", "Merge main again", "main")
    return {"head": head, "merge": merge, "base": base, "clean": git(tmp_path, "rev-parse", "HEAD")}


@pytest.mark.parametrize(
    "at", ["merge", "clean"], ids=["the conflicted merge", "a clean merge after it"]
)
def test_a_hand_resolved_conflict_with_main_is_not_unforced(repo: dict[str, str], at: str) -> None:
    pr = pull(reviews=(verdict_on(repo["head"]),))
    pr.update(headRefOid=repo[at], baseRefOid=repo["base"])
    assert approvals.unforced(pr) is None


def test_a_clean_merge_of_main_after_a_verdict_on_the_resolution_is_still_unforced(
    repo: dict[str, str],
) -> None:
    pr = pull(reviews=(verdict_on(repo["merge"]),))
    pr.update(headRefOid=repo["clean"], baseRefOid=repo["base"])
    assert approvals.unforced(pr) == LINE
