"""`committed` copies the session's template repo: it neither builds a repo itself nor shares the template (#1441)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from approvals_harness import committed, git_in, repo_template  # noqa: F401
from approvals_repos import TEMPLATE

STAMP = "2001-02-03T04:05:06 +0000"


@pytest.fixture
def marked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The template pointed at a repo no `git init` + commit of `committed` could reproduce: a marker file, and a
    commit back-dated to 2001, so only a copy of it has them."""
    repo = tmp_path / "marked-template"
    repo.mkdir()
    git_in(repo, "init", "-q")
    git_in(repo, "config", "user.email", "marker@example.com")
    git_in(repo, "config", "user.name", "marker")
    (repo / "marker.txt").write_text("from the template\n")
    git_in(repo, "add", "-A")
    os.environ["GIT_AUTHOR_DATE"] = os.environ["GIT_COMMITTER_DATE"] = STAMP
    try:
        git_in(repo, "commit", "-qm", "marked")
    finally:
        del os.environ["GIT_AUTHOR_DATE"], os.environ["GIT_COMMITTER_DATE"]
    monkeypatch.setitem(TEMPLATE, "repo", repo)
    return repo


def test_committed_is_a_copy_of_the_template_not_a_new_repo(marked: Path, tmp_path: Path) -> None:
    head = committed(tmp_path / "copy")
    assert head == git_in(marked, "rev-parse", "HEAD").strip()  # a fresh build has another hash
    assert (tmp_path / "copy" / "marker.txt").read_text() == "from the template\n"
    assert git_in(
        tmp_path / "copy", "log", "-1", "--format=%an %ad", "--date=iso-strict"
    ).startswith("marker 2001-02-03T04:05:06")
    assert git_in(tmp_path / "copy", "status", "--porcelain") == ""


@pytest.mark.parametrize("round", [1, 2])
def test_a_test_changing_its_copy_leaks_nothing_into_the_next(
    marked: Path, tmp_path: Path, round: int
) -> None:
    """Runs twice in one session: the second copy is as clean as the first, whatever the first left behind."""
    repo = tmp_path / "copy"
    head = committed(repo)
    assert not (repo / "leak.txt").exists()
    assert git_in(repo, "rev-list", "--count", "HEAD").strip() == "1"
    (repo / "leak.txt").write_text(f"round {round}")
    (repo / "marker.txt").write_text("changed\n")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-qm", "changed")
    assert git_in(repo, "rev-parse", "HEAD").strip() != head
    # the template itself is untouched
    assert git_in(marked, "rev-parse", "HEAD").strip() == head
    assert not (marked / "leak.txt").exists()
    assert (marked / "marker.txt").read_text() == "from the template\n"


@pytest.mark.usefixtures("repo_template")
def test_the_session_template_is_one_committed_repo(tmp_path: Path) -> None:
    """The real template, built by the `repo_template` fixture the harness turns on for every approvals test."""
    assert TEMPLATE["repo"].name == "repo"
    assert git_in(TEMPLATE["repo"], "rev-list", "--count", "HEAD").strip() == "1"
    assert committed(tmp_path / "c") == git_in(TEMPLATE["repo"], "rev-parse", "HEAD").strip()
