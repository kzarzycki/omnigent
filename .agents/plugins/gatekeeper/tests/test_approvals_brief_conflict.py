"""`brief` checks the PR's head merges into its own base before writing anything (#1034): a head that conflicts exits 1
naming the files and writes no brief, `--allow-conflict` writes it anyway, and a clean head's brief is unchanged. Each
test runs the real check against real repos: an origin, and a clone the lead works in."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from approvals_harness import LOOP, approvals, pull

ROLES = Path(__file__).resolve().parents[2] / "loop" / "skills" / "engineering-loop"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(cwd: Path, name: str, text: str) -> str:
    (cwd / name).write_text(text)
    git(cwd, "add", name)
    git(cwd, "commit", "-qm", f"{name}: {text.strip()}")
    return git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """origin with `trunk` (the PR's base, not the default branch's name) and a PR head published as refs/pull/7/head,
    and a clone that has fetched nothing of either since the base moved."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(seed))
    git(seed, "checkout", "-q", "-b", "trunk")
    start = commit(seed, "shared.txt", "one\n")
    commit(seed, "other.txt", "x\n")
    git(seed, "push", "-q", "origin", "trunk")
    git(seed, "checkout", "-q", "-b", "feature", start)
    head = commit(seed, "shared.txt", "head edit\n")
    git(seed, "push", "-q", "origin", f"{head}:refs/pull/7/head")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    monkeypatch.chdir(clone)
    monkeypatch.setattr(approvals, "merge_base", lambda base, head: start)
    monkeypatch.setattr(approvals, "project", lambda: (None, LOOP))

    def pr(base: str = "trunk") -> None:
        found = pull(body="Closes #12.\n")
        found["headRefOid"], found["baseRefName"] = head, base
        monkeypatch.setattr(approvals, "pull_request", lambda _n: found)

    pr()
    return {"seed": seed, "head": head, "pr": pr}


def advance(repo: dict[str, object], name: str, text: str) -> None:
    seed = repo["seed"]
    assert isinstance(seed, Path)
    git(seed, "checkout", "-q", "trunk")
    commit(seed, name, text)
    git(seed, "push", "-q", "origin", "trunk")


def test_a_head_that_conflicts_with_its_base_writes_no_brief_and_names_the_file(
    repo: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    advance(repo, "shared.txt", "base edit\n")  # the base moved on the same lines after the clone
    assert approvals.main(["brief", "7"]) == 1
    out = capsys.readouterr().out
    assert "shared.txt" in out
    assert "merge the base in first" in out
    assert "Review PR #7" not in out


def test_every_conflicted_file_is_named_and_a_clean_one_is_not(
    repo: dict[str, object], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = repo["seed"]
    assert isinstance(seed, Path)
    git(seed, "checkout", "-q", "feature")
    head = commit(seed, "other.txt", "head other\n")
    git(seed, "push", "-q", "-f", "origin", f"{head}:refs/pull/7/head")
    found = pull(body="Closes #12.\n")
    found["headRefOid"], found["baseRefName"] = head, "trunk"
    monkeypatch.setattr(approvals, "pull_request", lambda _n: found)
    advance(repo, "shared.txt", "base edit\n")
    advance(repo, "other.txt", "base other\n")
    advance(repo, "third.txt", "unrelated\n")
    assert approvals.main(["brief", "7"]) == 1
    out = capsys.readouterr().out
    assert "shared.txt" in out and "other.txt" in out and "third.txt" not in out


def test_the_check_merges_into_the_prs_own_base(
    repo: dict[str, object], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []
    real = subprocess.run

    def spy(cmd: list[str], *a: object, **k: object) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        return real(cmd, *a, **k)  # type: ignore[call-overload,no-any-return]

    monkeypatch.setattr(approvals.subprocess, "run", spy)
    assert approvals.main(["brief", "7"]) == 0
    tree = next(cmd for cmd in seen if cmd[:2] == ["git", "merge-tree"])
    assert tree[-2:] == ["origin/trunk", repo["head"]]
    assert "--write-tree" in tree


def test_a_clean_head_gets_the_brief_unchanged(
    repo: dict[str, object], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    advance(repo, "third.txt", "unrelated\n")  # the base moved, in a file the head doesn't touch
    assert approvals.main(["brief", "7"]) == 0
    checked = capsys.readouterr().out
    monkeypatch.setattr(approvals, "conflicted", lambda _pr, _base, _head: [])
    assert approvals.main(["brief", "7"]) == 0
    assert checked == capsys.readouterr().out
    assert checked.startswith("Review PR #7 as the verifier, pass 1")


def test_allow_conflict_writes_the_brief_anyway(
    repo: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    advance(repo, "shared.txt", "base edit\n")
    assert approvals.main(["brief", "7", "--allow-conflict"]) == 0
    assert capsys.readouterr().out.startswith("Review PR #7 as the verifier, pass 1")


def test_a_base_that_cannot_be_fetched_is_not_read_as_clean(
    repo: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    pr = repo["pr"]
    assert callable(pr)
    pr("gone")  # no such branch on origin
    assert approvals.main(["brief", "7"]) == 1
    out = capsys.readouterr().out
    assert "cannot fetch origin/gone" in out
    assert "Review PR #7" not in out


def test_a_stale_base_ref_is_fetched_before_the_check(
    repo: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    git(Path.cwd(), "fetch", "-q", "origin")  # origin/trunk is as it was before the base moved
    advance(repo, "shared.txt", "base edit\n")
    assert approvals.main(["brief", "7"]) == 1
    assert "shared.txt" in capsys.readouterr().out


def test_lead_md_says_what_to_do_on_exit_1() -> None:
    lead = " ".join((ROLES / "lead.md").read_text().split())
    assert "exits 1 naming the conflicted files and writes no brief" in lead
    assert "merge the base in first" in lead
    assert "`--allow-conflict`" in lead


def test_a_git_error_in_the_merge_is_not_read_as_clean(
    repo: dict[str, object], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    found = pull(body="Closes #12.\n")
    found["headRefOid"], found["baseRefName"] = "f" * 40, "trunk"  # a commit git doesn't have
    monkeypatch.setattr(approvals, "pull_request", lambda _n: found)
    assert approvals.main(["brief", "7"]) == 1
    out = capsys.readouterr().out
    assert "git merge-tree failed" in out
    assert "Review PR #7" not in out


def test_a_conflict_git_names_no_file_for_still_refuses(
    repo: dict[str, object], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = subprocess.run

    def bare(cmd: list[str], *a: object, **k: object) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["git", "merge-tree"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="a" * 40 + "\n", stderr="")
        return real(cmd, *a, **k)  # type: ignore[call-overload,no-any-return]

    monkeypatch.setattr(approvals.subprocess, "run", bare)
    assert approvals.main(["brief", "7"]) == 1
    out = capsys.readouterr().out
    assert "files not named by git" in out
    assert "Review PR #7" not in out
