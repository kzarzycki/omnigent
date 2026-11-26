"""approvals.py: a verdict and a go carry to a head that only takes the base in with no hand edit (#1314), by one
mechanical proof, `catch_up`: `git range-diff` for a rebase, `git show --remerge-diff` for a merge of the base."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from approvals_harness import approvals

BODY = "".join(f"line {n}\n" for n in range(1, 11))
Repo = Callable[..., str]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repo:
    """`repo(*git args)` runs git in a fresh repo whose `main` holds app.py, and returns its output."""
    monkeypatch.chdir(tmp_path)
    who = ("-c", "commit.gpgsign=false", "-c", "user.name=T", "-c", "user.email=t@example.com")

    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *who, *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()

    run("init", "-q", "-b", "main")
    (tmp_path / "app.py").write_text(BODY)
    run("add", "-A")
    run("commit", "-qm", "base")

    def write(name: str, text: str, message: str) -> str:
        (tmp_path / name).write_text(text)
        run("add", "-A")
        run("commit", "-qm", message)
        return run("rev-parse", "HEAD")

    run.write = write  # type: ignore[attr-defined]
    return run


def pr(repo: Repo, head: str, tip: str | None = None) -> dict[str, str]:
    return {
        "headRefOid": head,
        "baseRefOid": tip or repo("rev-parse", "main"),
        "baseRefName": "main",
    }


def feature(repo: Repo) -> str:
    """The PR's two commits on a branch off main: the verified head."""
    repo("switch", "-qc", "feature")
    repo.write("app.py", BODY.replace("line 10\n", "line 10, feature\n"), "one")  # type: ignore[attr-defined]
    return repo.write("b.txt", "b\n", "two")  # type: ignore[attr-defined]


def moved(repo: Repo, name: str = "other.txt", text: str = "main moved\n") -> str:
    repo("switch", "-q", "main")
    return repo.write(name, text, f"main moves {name}")  # type: ignore[attr-defined]


def test_a_clean_rebase_carries(repo: Repo) -> None:
    old = feature(repo)
    tip = moved(repo)
    repo("switch", "-q", "feature")
    repo("rebase", "-q", "main")
    proof = approvals.catch_up(pr(repo, repo("rev-parse", "HEAD"), tip), old)
    assert proof and "git range-diff" in proof


def test_a_clean_merge_of_main_carries(repo: Repo) -> None:
    old = feature(repo)
    tip = moved(repo)
    repo("switch", "-q", "feature")
    repo("merge", "-q", "--no-ff", "-m", "take main in", "main")
    proof = approvals.catch_up(pr(repo, repo("rev-parse", "HEAD"), tip), old)
    assert proof and "--remerge-diff" in proof


def test_two_merges_of_main_carry(repo: Repo) -> None:
    """A PR branch updated twice by merging its base in, as the loop updates one, keeps the old head's verdict."""
    old = feature(repo)
    moved(repo)
    repo("switch", "-q", "feature")
    repo("merge", "-q", "--no-ff", "-m", "take main in", "main")
    tip = moved(repo, "third.txt", "main moved again\n")
    repo("switch", "-q", "feature")
    repo("merge", "-q", "--no-ff", "-m", "take main in again", "main")
    proof = approvals.catch_up(pr(repo, repo("rev-parse", "HEAD"), tip), old)
    assert proof and "--remerge-diff" in proof


def test_a_merge_with_a_hand_resolved_conflict_carries_nothing(repo: Repo) -> None:
    old = feature(repo)
    tip = moved(repo, "app.py", BODY.replace("line 10\n", "line 10, main\n"))
    repo("switch", "-q", "feature")
    subprocess.run(["git", "merge", "--no-ff", "-q", "main"], capture_output=True, check=False)
    (Path("app.py")).write_text(BODY.replace("line 10\n", "line 10, both\n"))
    repo("add", "-A")
    repo("commit", "-qm", "take main in, by hand")
    assert approvals.catch_up(pr(repo, repo("rev-parse", "HEAD"), tip), old) is None


def test_a_rebase_that_changed_a_commit_carries_nothing(repo: Repo) -> None:
    old = feature(repo)
    tip = moved(repo)
    repo("switch", "-q", "feature")
    repo("rebase", "-q", "main")
    (Path("b.txt")).write_text("b, edited\n")
    repo("commit", "-qa", "--amend", "-m", "two")
    head = repo("rev-parse", "HEAD")
    assert approvals.catch_up(pr(repo, head, tip), old) is None


def test_a_new_commit_carries_nothing(repo: Repo) -> None:
    old = feature(repo)
    tip = moved(repo)
    repo("switch", "-q", "feature")
    repo("merge", "-q", "--no-ff", "-m", "take main in", "main")
    head = repo.write("c.txt", "c\n", "three")  # type: ignore[attr-defined]
    assert approvals.catch_up(pr(repo, head, tip), old) is None
    # a commit dropped: the first one alone, on the new base
    repo("switch", "-qc", "dropped", "main")
    repo("cherry-pick", old + "~1")
    assert approvals.catch_up(pr(repo, repo("rev-parse", "HEAD"), tip), old) is None


def long_rebase(repo: Repo, change: str | None) -> tuple[str, str, str]:
    """A 12-commit PR rebased onto a moved main: (old head, new head, tip). `change` edits commit 2 by hand
    ("changed"), leaves it out ("dropped") or adds a commit after it ("added"); git range-diff pads commit numbers
    of 10+ commits, so lines for the first nine start with a space."""
    repo("switch", "-qc", "feature")
    for n in range(1, 13):
        repo.write(f"f{n}.txt", f"{n}\n", f"c{n}")  # type: ignore[attr-defined]
    old = repo("rev-parse", "HEAD")
    tip = moved(repo)
    repo("switch", "-q", "feature")
    repo("rebase", "-q", "main")
    if change:
        base = repo("rev-parse", "main")
        picks = repo("rev-list", "--reverse", f"{base}..HEAD").split()
        repo("reset", "-q", "--hard", base)
        for n, commit in enumerate(picks, 1):
            if n == 2 and change == "dropped":
                continue
            repo("cherry-pick", commit)
            if n == 2 and change == "changed":
                Path("f2.txt").write_text("edited by hand\n")
                repo("commit", "-qa", "--amend", "-m", "c2")
            if n == 2 and change == "added":
                repo.write("extra.txt", "x\n", "extra")  # type: ignore[attr-defined]
    return old, repo("rev-parse", "HEAD"), tip


def test_a_rebase_of_ten_or_more_commits_carries_when_clean(repo: Repo) -> None:
    old, head, tip = long_rebase(repo, None)
    assert approvals.catch_up(pr(repo, head, tip), old)


@pytest.mark.parametrize("change", ["changed", "dropped", "added"])
def test_a_rebase_of_ten_or_more_commits_carries_nothing_for_a_change_among_the_first_nine(
    repo: Repo, change: str
) -> None:
    old, head, tip = long_rebase(repo, change)
    assert approvals.catch_up(pr(repo, head, tip), old) is None
