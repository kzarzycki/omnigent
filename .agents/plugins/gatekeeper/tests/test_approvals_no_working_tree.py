"""approvals.py from a clone with no working tree reads the policy from origin's default branch, and land computes the
batch in objects, reusing a recorded pass on its tree or refusing, naming it, with no file checked out (#917)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import (
    approvals,
    git_in,
    lands,  # noqa: F401
)

LOOP = "docs/agents/loop.md"
REAL = approvals.judge_with_base  # the shared fixtures replace it per test
MARK = "<!-- origin's main -->"


@pytest.fixture
def launch(
    monkeypatch: pytest.MonkeyPatch, lands: dict[str, Any], pushed: None, tmp_path: Path
) -> dict[str, Any]:
    """The policy files on origin's main, and a `--no-checkout` clone of origin to run from, as a host keeps to launch
    cloud sessions; the real base comparison of #926 back, and the script installed outside the clone."""
    monkeypatch.setattr(approvals, "judge_with_base", REAL)
    monkeypatch.delenv(
        approvals.WORKING_TREE_POLICY
    )  # `lands` reads the policy from the work tree; these read it from origin
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    work = lands["work"]
    (work / LOOP).write_text((work / LOOP).read_text() + f"\n{MARK}\n")
    git_in(work, "add", "-A")
    git_in(work, "commit", "-qm", "adopt the loop")
    git_in(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    git_in(work, "fetch", "-q", "origin")
    here = tmp_path / "launch"
    git_in(tmp_path, "clone", "-q", "--no-checkout", str(lands["origin"]), str(here))
    git_in(here, "config", "user.email", "loop@example.com")
    git_in(here, "config", "user.name", "loop")
    monkeypatch.chdir(here)
    approvals.policy_ref.cache_clear()
    return lands | {"here": here}


def untouched(here: Path) -> None:
    """Nothing checked out in the clone: no index, no file but git's own, no worktree added."""
    assert not (here / ".git" / "index").exists()
    assert sorted(os.listdir(here)) == [".git"]
    assert len(git_in(here, "worktree", "list", "--porcelain").split("\nworktree ")) == 1


def test_a_no_checkout_clone_has_no_working_tree_and_a_checkout_has_one(
    launch: dict[str, Any], tmp_path: Path
) -> None:
    assert not approvals.working_tree(launch["here"])
    assert approvals.working_tree(launch["work"])
    fresh = tmp_path / "fresh"
    git_in(tmp_path, "init", "-q", str(fresh))
    assert approvals.working_tree(fresh)  # no commit yet: nothing to read from origin either


def test_the_policy_is_read_from_origins_default_branch(launch: dict[str, Any]) -> None:
    tracker, loop = approvals.project()
    assert tracker is not None and MARK in (loop or "")
    untouched(launch["here"])


def test_check_merge_judges_with_origins_policy_and_finds_no_drift(
    launch: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    launch["open"](7, {"7.txt": "7\n"})
    monkeypatch.setattr(approvals, "pull_request", lambda _n: launch["pulls"][7])
    monkeypatch.chdir(launch["here"])
    from_launch = approvals.check("merge", 7), capsys.readouterr().out
    monkeypatch.chdir(launch["work"])
    git_in(launch["work"], "switch", "-q", "--detach", "origin/main")
    assert (approvals.check("merge", 7), capsys.readouterr().out) == from_launch
    untouched(launch["here"])


def test_the_base_comparison_reads_the_policy_as_the_run_does(
    launch: dict[str, Any],
) -> None:
    """Into the default branch, no drift by construction; into another base, its own files still judge."""
    assert approvals.base_drift("main") == []
    work = launch["work"]
    (work / LOOP).write_text((work / LOOP).read_text() + "\na release branch's own rule\n")
    git_in(work, "commit", "-qam", "release rules")
    git_in(work, "push", "-q", "origin", "HEAD:refs/heads/release")
    git_in(
        launch["here"], "fetch", "-q", "origin", "+refs/heads/release:refs/remotes/origin/release"
    )
    assert approvals.base_drift("release") == [LOOP]


def test_land_with_no_pass_on_the_batch_tree_merges_runs_no_gate_and_checks_out_nothing(
    launch: dict[str, Any],
) -> None:
    """#1872: the batch tree needs no recorded pass; the verifier's record on the head is the proof."""
    launch["open"](7, {"7.txt": "7\n"})
    code, lines = launch["land"]("7")
    assert code == 0, lines
    assert launch["merged"]() == ["7"] and launch["runs"]() == 0
    untouched(launch["here"])


def passed(launch: dict[str, Any], monkeypatch: pytest.MonkeyPatch, files: dict[str, str]) -> str:
    """A pass recorded on main with `files` written, from the checkout, as a session with the files would; its tree."""
    work = launch["work"]
    monkeypatch.chdir(work)
    git_in(work, "switch", "-q", "--detach", "origin/main")
    for name, text in files.items():
        (work / name).write_text(text)
    git_in(work, "add", *files)
    git_in(work, "commit", "-qm", "the batch, as merged")
    assert approvals.main(["local-ci", "--record"]) == 0
    monkeypatch.chdir(launch["here"])
    return git_in(work, "rev-parse", "HEAD^{tree}").strip()


def test_a_head_that_conflicts_with_the_batch_is_skipped_and_the_rest_lands(
    launch: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    launch["open"](7, {"same.txt": "7\n"})
    launch["open"](8, {"same.txt": "8\n"})
    passed(launch, monkeypatch, {"same.txt": "7\n"})
    code, lines = launch["land"]()
    assert code == 1, lines
    assert any("PR #8 conflicts with main plus #7" in line for line in lines), lines
    assert launch["merged"]() == ["7"]
    assert any(n == 8 and body.startswith(approvals.CONFLICTS) for n, body in launch["comments"])
    untouched(launch["here"])


def test_a_batch_github_merges_only_in_part_gates_nothing(
    launch: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    launch["open"](7, {"7.txt": "7\n"})
    launch["open"](8, {"8.txt": "8\n"})
    passed(launch, monkeypatch, {"7.txt": "7\n", "8.txt": "8\n"})
    fake = approvals.github.gh

    def refuses_8(*args: str, **kwargs: Any) -> str:
        if args[:3] == ("pr", "merge", "8"):
            raise approvals.subprocess.CalledProcessError(1, "gh", stderr="refused")
        return fake(*args, **kwargs)

    monkeypatch.setattr(approvals.github, "gh", refuses_8)
    runs = launch["runs"]()
    code, lines = launch["land"]()
    assert code == 1, lines  # PR #8 was refused
    assert launch["merged"]() == ["7"], lines
    assert launch["runs"]() == runs
    untouched(launch["here"])


def test_land_with_a_pre_land_check_and_no_working_tree_refuses(
    launch: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    launch["open"](7, {"7.txt": "7\n"})
    real = approvals.settings
    monkeypatch.setattr(approvals, "settings", lambda text: real(text) | {"pre_land": "true"})
    code, lines = launch["land"]("7")
    assert code == 1
    assert any("`pre_land`" in line and "no working tree" in line for line in lines), lines
    assert launch["merged"]() == []
    untouched(launch["here"])


def push_policy_change(launch: dict[str, Any], text: str) -> None:
    """A rule added on origin's main after the launch clone was made, which the clone holds nowhere until it fetches."""
    work = launch["work"]
    git_in(work, "switch", "-q", "--detach", "origin/main")
    (work / LOOP).write_text((work / LOOP).read_text() + f"\n{text}\n")
    git_in(work, "commit", "-qam", "change the policy")
    git_in(work, "push", "-q", "origin", "HEAD:refs/heads/main")


def test_the_policy_is_origins_now_not_the_clones_last_fetched_copy(
    launch: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    push_policy_change(launch, "<!-- changed after the clone -->")
    monkeypatch.chdir(launch["here"])
    approvals.policy_ref.cache_clear()
    assert "changed after the clone" in (approvals.project()[1] or "")
    assert (
        "changed after the clone" in approvals.policy_bytes(launch["here"], Path(LOOP)).decode()
    )  # base_drift and check merge read it through here
    untouched(launch["here"])


def test_land_judges_by_the_policy_pushed_since_the_clone(
    launch: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    launch["open"](7, {"7.txt": "7\n"})
    push_policy_change(launch, "<!-- pre_land added -->")
    monkeypatch.chdir(launch["here"])
    approvals.policy_ref.cache_clear()
    real = approvals.settings
    monkeypatch.setattr(
        approvals,
        "settings",
        lambda text: (
            real(text) | ({"pre_land": "true"} if "pre_land added" in (text or "") else {})
        ),
    )
    code, lines = launch["land"]("7")
    assert code == 1 and launch["merged"]() == []
    assert any("`pre_land`" in line and "no working tree" in line for line in lines), lines


def test_a_failed_fetch_is_refused_not_read_as_the_last_fetched_ref(
    launch: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    git_in(launch["here"], "remote", "set-url", "origin", str(launch["here"] / "gone"))
    approvals.policy_ref.cache_clear()
    with pytest.raises(approvals.Refused, match="could not be fetched"):
        approvals.project()


def test_the_default_branch_is_asked_of_origin_where_the_clone_recorded_none(
    tmp_path: Path, launch: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Origin's default branch is not `main` here, and the clone has no refs/remotes/origin/HEAD."""
    work = launch["work"]
    git_in(work, "switch", "-q", "--detach", "origin/main")
    git_in(work, "push", "-q", "origin", "HEAD:refs/heads/trunk")
    git_in(launch["origin"], "symbolic-ref", "HEAD", "refs/heads/trunk")
    bare = tmp_path / "bare"
    git_in(tmp_path, "clone", "-q", "--no-checkout", str(launch["origin"]), str(bare))
    git_in(bare, "remote", "set-head", "origin", "-d")
    assert not (bare / ".git" / "refs" / "remotes" / "origin" / "HEAD").exists()
    approvals.policy_ref.cache_clear()
    assert approvals.policy_ref(bare) == "refs/remotes/origin/trunk"


def push_base(launch: dict[str, Any], name: str) -> None:
    """A stacked PR's base branch, created on origin after the launch clone was made, with a rule of its own."""
    work = launch["work"]
    git_in(work, "switch", "-q", "--detach", "origin/main")
    (work / LOOP).write_text((work / LOOP).read_text() + f"\n<!-- {name}'s own rule -->\n")
    git_in(work, "commit", "-qam", f"{name} rules")
    git_in(work, "push", "-q", "origin", f"HEAD:refs/heads/{name}")


def test_a_base_pushed_since_the_clone_is_refused_not_judged_by_empty_policy(
    launch: dict[str, Any],
) -> None:
    """With no working tree there are no files to fall back to: the unfetched base's policy is not read as none."""
    push_base(launch, "stack")
    with pytest.raises(approvals.Refused, match="no origin/stack ref"):
        approvals.project(base="stack")
    git_in(launch["here"], "fetch", "-q", "origin", "+refs/heads/stack:refs/remotes/origin/stack")
    assert "stack's own rule" in (approvals.project(base="stack")[1] or "")
    untouched(launch["here"])


def test_land_into_an_unfetched_base_whose_fetch_fails_is_refused_not_judged_by_defaults(
    launch: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """origin has no such branch, so land's own fetch of the base fails and project() has no ref to read."""
    pull = launch["open"](7, {"7.txt": "7\n"}) and launch["pulls"][7]
    monkeypatch.setattr(approvals, "land_base", lambda _pr: ([pull], "stack", 0))
    with pytest.raises(approvals.Refused, match="no origin/stack ref"):
        approvals.landing(7)
    assert launch["merged"]() == []
    untouched(launch["here"])
