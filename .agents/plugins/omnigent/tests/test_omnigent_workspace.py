"""omnigent_agent.py start refuses a role child a workspace a live session uses on its host (#750)."""

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from test_omnigent_agent import (  # noqa: F401  (the fixtures apply here by name)
    FakeOmnigent,
    no_model,
    no_sleep,
    omnigent_agent,
    this_repo,
    trusted_folders,
)

WORKER = "loop/skills/engineering-loop/worker.md"


def lead_in(og: FakeOmnigent, place: Path, **fields: object) -> None:
    """A live session this script did not start, using ``place`` on this host."""
    og.others.append(
        {"id": "lead", "title": "s750-lead", "host_id": og.host_id, "workspace": str(place)}
        | fields
    )


def start(og: FakeOmnigent, workspace: Path, **kwargs: object) -> str:
    kwargs.setdefault("role", WORKER)
    return omnigent_agent.start(
        og,
        str(kwargs.pop("agent", "claude")),
        str(kwargs.pop("title", "s750-worker")),
        "do X",
        workspace / "tmp" / "loop" / f"{kwargs.pop('report', 'r')}.md",
        confirm_seconds=0,
        workspace=str(workspace),
        **kwargs,
    )[0]


@pytest.mark.parametrize("top_level", [False, True])
def test_a_role_start_on_a_workspace_a_live_session_uses_is_refused_naming_it(
    tmp_path: Path, top_level: bool
) -> None:
    og = FakeOmnigent()
    first = start(og, tmp_path, title="s750-gate-a", report="a")
    with pytest.raises(omnigent_agent.Fail) as refused:
        start(og, tmp_path, title="s750-gate-b", report="b", top_level=top_level)
    assert f"live session {first} s750-gate-a" in str(refused.value)
    assert "its own worktree" in str(refused.value)
    assert list(og.sessions) == [first]  # refused before any session exists


def test_a_child_is_refused_the_workspace_its_own_parent_uses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "lead")
    og = FakeOmnigent()
    lead_in(og, tmp_path)
    with pytest.raises(omnigent_agent.Fail, match="live session lead s750-lead"):
        start(og, tmp_path)


def test_the_cli_exits_non_zero_on_a_used_workspace(
    tmp_path: Path,
    role_file: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("do X")
    og = FakeOmnigent()
    lead_in(og, tmp_path)
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    argv = ["start", "claude", "s750-worker", "b.md", "r.md", "--role", role_file("worker.md")]
    assert omnigent_agent.main([*argv, "--top-level", "--confirm-seconds", "0"]) != 0
    assert "live session lead s750-lead" in capsys.readouterr().err


def test_the_same_start_succeeds_once_that_session_is_archived(tmp_path: Path) -> None:
    og = FakeOmnigent()
    first = start(og, tmp_path, title="s750-gate-a", report="a")
    og.archive(first)
    assert start(og, tmp_path, title="s750-gate-b", report="b") != first


@pytest.mark.parametrize(
    "fields",
    [{"status": "failed"}, {"archived": True}, {"host_id": "other"}, {"workspace": "/elsewhere"}],
)
def test_a_failed_or_archived_session_or_one_elsewhere_holds_no_workspace(
    tmp_path: Path, fields: dict
) -> None:
    og = FakeOmnigent()
    lead_in(og, tmp_path, **fields)
    assert start(og, tmp_path) == "s0"


def test_a_session_on_another_host_holds_that_host_s_workspace_not_this_one_s(
    tmp_path: Path,
) -> None:
    og = FakeOmnigent()
    lead_in(og, tmp_path, host_id="other")
    with pytest.raises(omnigent_agent.Fail, match="on host other by live session lead"):
        start(og, tmp_path, host_id="other", top_level=True)


def test_a_path_through_a_symlink_on_this_host_is_the_same_workspace(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    og = FakeOmnigent()
    lead_in(og, tmp_path / "link")
    with pytest.raises(omnigent_agent.Fail, match="live session lead"):
        start(og, tmp_path / "real")


def test_a_start_without_a_role_shares_a_used_workspace(tmp_path: Path) -> None:
    og = FakeOmnigent()
    lead_in(og, tmp_path)
    assert start(og, tmp_path, role=None, top_level=True) == "s0"


def test_a_cloud_agent_s_role_start_shares_a_used_workspace(tmp_path: Path) -> None:
    og = FakeOmnigent()
    og.harnesses["claude-cloud"] = omnigent_agent.CLOUD_HARNESS
    lead_in(og, tmp_path)
    first = start(og, tmp_path, agent="claude-cloud", title="s750-cloud-a", report="a")
    assert start(og, tmp_path, agent="claude-cloud", title="s750-cloud-b", report="b") != first


def test_a_session_that_names_no_workspace_holds_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # an empty path resolves to the current directory
    og = FakeOmnigent()
    lead_in(og, tmp_path, workspace="")
    assert start(og, tmp_path) == "s0"


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def bare(tmp_path: Path) -> Path:
    """A bare remote holding main and a PR branch, made from a throwaway clone."""
    seed = tmp_path / "seed"
    git("init", "-q", "-b", "main", str(seed), cwd=tmp_path)
    git("config", "user.email", "t@example.com", cwd=seed)
    git("config", "user.name", "t", cwd=seed)
    (seed / "a.txt").write_text("a")
    git("add", ".", cwd=seed)
    git("commit", "-qm", "one", cwd=seed)
    git("checkout", "-qb", "pr-branch", cwd=seed)
    (seed / "b.txt").write_text("b")
    git("add", ".", cwd=seed)
    git("commit", "-qm", "two", cwd=seed)
    git("checkout", "-q", "main", cwd=seed)
    remote = tmp_path / "remote.git"
    git("clone", "-q", "--bare", str(seed), str(remote), cwd=tmp_path)
    git("update-ref", "refs/pull/7/head", "pr-branch", cwd=remote)  # as GitHub keeps it, forks too
    git("update-ref", "refs/heads/main", "main", cwd=remote)
    return remote


def test_a_launch_dir_holds_no_files_but_the_pin_and_a_loop_folder(
    tmp_path: Path, bare: Path
) -> None:
    config, dest = tmp_path / "claude.json", tmp_path / "launch"
    omnigent_agent.launch_dir(f"file://{bare}", "pr-branch", "env_1", dest, config=config)
    assert git("symbolic-ref", "HEAD", cwd=dest) == "refs/heads/pr-branch"
    assert not (dest / "a.txt").exists() and not (dest / ".git" / "index").exists()
    pin = json.loads((dest / ".claude" / "settings.local.json").read_text())
    assert pin == {"remote": {"defaultEnvironmentId": "env_1"}}
    assert (dest / "tmp" / "loop").is_dir()
    assert not config.exists()  # trust is written only on request
    omnigent_agent.pin_environment(dest)  # a clone with its own pin passes

    other = tmp_path / "trusted"
    omnigent_agent.launch_dir(f"file://{bare}", "main", "env_1", other, trust=True, config=config)
    assert json.loads(config.read_text())["projects"][str(other.resolve())] == {
        "hasTrustDialogAccepted": True
    }


def test_a_clone_without_a_pin_is_refused_naming_the_helper(
    tmp_path: Path, bare: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(omnigent_agent, "cloud_environments", list)  # nothing to resolve
    dest = tmp_path / "clone"
    git("clone", "-q", f"file://{bare}", str(dest), cwd=tmp_path)
    with pytest.raises(omnigent_agent.Fail, match="launch-dir"):
        omnigent_agent.pin_environment(dest)


def test_start_for_a_pr_moves_a_launch_dirs_head_but_never_a_checkouts(
    tmp_path: Path, bare: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        omnigent_agent, "pr_branch", lambda pr, workspace: "pr-branch"
    )  # PR 7's head
    dest = tmp_path / "launch"
    omnigent_agent.launch_dir(f"file://{bare}", "main", "env_1", dest)
    omnigent_agent.move_head(dest, 7)
    assert git("symbolic-ref", "HEAD", cwd=dest) == "refs/heads/pr-branch"
    assert not (dest / "b.txt").exists()  # nothing was checked out

    full = tmp_path / "full"
    git("clone", "-q", f"file://{bare}", str(full), cwd=tmp_path)
    omnigent_agent.move_head(full, 7)
    assert git("symbolic-ref", "--short", "HEAD", cwd=full) == "main"  # it has files: left alone


def worktrees(tree: Path) -> int:
    return git("worktree", "list", "--porcelain", cwd=tree).count("worktree ")


def test_start_pr_on_a_cloud_agent_reads_the_pin_and_checks_out_nothing_on_the_host(
    tmp_path: Path, bare: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host keeps only the launch dir (#917): `start --pr` moves its HEAD, reads its pin, and makes no worktree
    and checks out no file, there or in a checkout given as the workspace, which it leaves as it was."""
    og = FakeOmnigent()
    og.harnesses = {"claude-cloud": "acp:claude-cloud"}
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.setattr(
        omnigent_agent, "pr_branch", lambda pr, workspace: "pr-branch"
    )  # PR 7's head
    dest, full = tmp_path / "launch", tmp_path / "full"
    omnigent_agent.launch_dir(f"file://{bare}", "main", "env_1", dest)
    git("clone", "-q", f"file://{bare}", str(full), cwd=tmp_path)
    (full / ".claude").mkdir()
    (full / ".claude" / "settings.local.json").write_text(
        '{"remote": {"defaultEnvironmentId": "env_1"}}'
    )
    pin = (dest / ".claude" / "settings.local.json").read_text()
    for tree in (dest, full):
        argv = [
            "start",
            "claude-cloud",
            f"t-{tree.name}",
            "do X",
            str(tree / "tmp" / "loop" / "r.md"),
        ]
        argv += ["--pr", "7", "--confirm-seconds", "0", "--workspace", str(tree)]
        (tree / "tmp" / "loop").mkdir(parents=True, exist_ok=True)
        assert omnigent_agent.main(argv) == 0
    assert git("symbolic-ref", "HEAD", cwd=dest) == "refs/heads/pr-branch"
    assert (dest / ".claude" / "settings.local.json").read_text() == pin  # its own pin, read
    assert not (dest / ".git" / "index").exists()
    assert sorted(p.name for p in dest.iterdir()) == [".claude", ".git", "tmp"]
    assert git("symbolic-ref", "HEAD", cwd=full) == "refs/heads/main"
    assert git("status", "--porcelain", "--untracked-files=no", cwd=full) == ""
    assert not (full / "b.txt").exists()
    assert worktrees(dest) == worktrees(full) == 1


def test_a_fork_pr_head_moves_a_launch_dir_through_the_pr_ref_not_a_same_named_origin_branch(
    tmp_path: Path, bare: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git("update-ref", "refs/pull/8/head", "pr-branch", cwd=bare)  # a fork's head: no origin branch
    git("update-ref", "refs/heads/fork-branch", "main", cwd=bare)  # an unrelated same-named one
    monkeypatch.setattr(omnigent_agent, "pr_branch", lambda pr, workspace: "fork-branch")
    dest = tmp_path / "launch"
    omnigent_agent.launch_dir(f"file://{bare}", "main", "env_1", dest)
    omnigent_agent.move_head(dest, 8)
    assert git("symbolic-ref", "HEAD", cwd=dest) == "refs/heads/fork-branch"
    assert git("rev-parse", "HEAD", cwd=dest) == git("rev-parse", "pr-branch", cwd=bare)


def test_the_pr_is_looked_up_in_the_launch_dirs_repository_not_the_callers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stand-in `gh` answers with the name of the directory it runs in."""
    bin_dir, other, launch = tmp_path / "bin", tmp_path / "caller", tmp_path / "launch"
    for d in (bin_dir, other, launch):
        d.mkdir()
    gh = bin_dir / "gh"
    gh.write_text('#!/bin/sh\necho "branch-of-$(basename "$PWD")"\n')
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.chdir(other)
    assert omnigent_agent.pr_branch(7, launch) == "branch-of-launch"
