"""launch-dir makes a worktree of a same-repository checkout, so it inherits the checkout's folder
trust, and `start` refuses a cloud launch from an untrusted folder before any session exists (#980)."""

import json
import subprocess
from pathlib import Path

import pytest
from test_omnigent_agent import (  # noqa: F401  (the fixtures apply here by name)
    REAL_TRUSTED,
    FakeOmnigent,
    no_model,
    no_sleep,
    omnigent_agent,
    this_repo,
    trusted_folders,
)


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def bare(tmp_path: Path) -> Path:
    """A bare remote that serves partial clones, holding main and a PR branch."""
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
    git("config", "uploadpack.allowFilter", "true", cwd=remote)
    return remote


@pytest.fixture
def real_trust(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The real trust check, reading a config of the test's own."""
    config = tmp_path / "claude.json"
    monkeypatch.setattr(omnigent_agent, "folder_trusted", REAL_TRUSTED)
    monkeypatch.setattr(omnigent_agent, "claude_config", lambda: config)
    return config


def missing(repo: Path, rev: str) -> list[str]:
    """The objects ``rev`` reaches that ``repo`` lacks, listed without fetching them."""
    out = git("rev-list", "--objects", "--missing=print", rev, cwd=repo)
    return [line for line in out.splitlines() if line.startswith("?")]


def checkout(bare: Path, dest: Path, *flags: str) -> Path:
    git("clone", "-q", *flags, f"file://{bare}", str(dest), cwd=dest.parent)
    git("config", "user.email", "t@example.com", cwd=dest)
    return dest


def test_a_clone_launch_dir_is_blobless(tmp_path: Path, bare: Path) -> None:
    dest = tmp_path / "launch"
    omnigent_agent.launch_dir(f"file://{bare}", "pr-branch", "env_1", dest, here=tmp_path)
    assert len(missing(dest, "HEAD")) == 2  # a.txt and b.txt, never fetched


def test_a_launch_dir_inside_a_same_repo_checkout_is_a_worktree_without_blobs(
    tmp_path: Path, bare: Path, real_trust: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main = checkout(bare, tmp_path / "main", "--no-checkout", "--filter=blob:none")
    before = missing(main, "origin/pr-branch")
    dest = tmp_path / "launch"
    omnigent_agent.launch_dir(f"file://{bare}", "pr-branch", "env_1", dest, here=main)
    assert git("rev-parse", "--git-common-dir", cwd=dest) == str(main / ".git")  # a linked worktree
    assert git("rev-parse", "HEAD", cwd=dest) == git("rev-parse", "origin/pr-branch", cwd=main)
    assert not (dest / "a.txt").exists() and not (dest / "b.txt").exists()
    assert not Path(
        git("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=dest)
    ).exists()
    assert missing(dest, "HEAD") == before and len(before) == 2  # no blob came with it
    pin = json.loads((dest / ".claude" / "settings.local.json").read_text())
    assert pin == {"remote": {"defaultEnvironmentId": "env_1"}}
    assert (dest / "tmp" / "loop").is_dir()
    assert not real_trust.exists()  # no trust entry written
    assert "NOT trusted" in capsys.readouterr().out  # the checkout is not trusted here


def test_the_worktree_is_trusted_when_its_checkout_is_and_no_entry_is_written(
    tmp_path: Path, bare: Path, real_trust: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main = checkout(bare, tmp_path / "main")
    entry = {"projects": {str(main.resolve()): {"hasTrustDialogAccepted": True}}}
    real_trust.write_text(json.dumps(entry))
    dest = tmp_path / "launch"
    omnigent_agent.launch_dir(f"file://{bare}", "main", "env_1", dest, here=main)
    assert json.loads(real_trust.read_text()) == entry
    assert omnigent_agent.folder_trusted(dest)
    assert ", trusted" in capsys.readouterr().out
    # the checkout's own branch is untouched by the worktree
    assert git("symbolic-ref", "--short", "HEAD", cwd=main) == "main"


def test_a_checkout_of_another_repository_gets_a_clone(
    tmp_path: Path, bare: Path, real_trust: Path
) -> None:
    other = tmp_path / "other"
    git("init", "-q", str(other), cwd=tmp_path)
    git("remote", "add", "origin", "https://example.com/else/where.git", cwd=other)
    dest = tmp_path / "launch"
    omnigent_agent.launch_dir(f"file://{bare}", "main", "env_1", dest, here=other)
    assert git("rev-parse", "--git-common-dir", cwd=dest) == ".git"  # its own repository


def test_trust_is_by_path_so_a_clone_inside_a_trusted_folder_is_untrusted(
    tmp_path: Path, bare: Path, real_trust: Path
) -> None:
    real_trust.write_text(
        json.dumps({"projects": {str(tmp_path.resolve()): {"hasTrustDialogAccepted": True}}})
    )
    dest = tmp_path / "launch"
    omnigent_agent.launch_dir(f"file://{bare}", "main", "env_1", dest, here=tmp_path)
    assert not omnigent_agent.folder_trusted(dest)


def test_trust_flag_writes_the_entry_as_before(
    tmp_path: Path, bare: Path, real_trust: Path
) -> None:
    main = checkout(bare, tmp_path / "main")
    dest = tmp_path / "launch"
    omnigent_agent.launch_dir(f"file://{bare}", "main", "env_1", dest, trust=True, here=main)
    assert json.loads(real_trust.read_text())["projects"][str(dest.resolve())] == {
        "hasTrustDialogAccepted": True
    }


def start_argv(tree: Path) -> list[str]:
    (tree / "tmp" / "loop").mkdir(parents=True, exist_ok=True)
    return [
        "start",
        "claude-cloud",
        "t-launch",
        "do X",
        str(tree / "tmp" / "loop" / "r.md"),
        "--confirm-seconds",
        "0",
        "--workspace",
        str(tree),
    ]


def test_start_from_an_untrusted_launch_dir_refuses_before_a_session_exists(
    tmp_path: Path,
    bare: Path,
    real_trust: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    og = FakeOmnigent()
    og.harnesses = {"claude-cloud": "acp:claude-cloud"}
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    dest = tmp_path / "launch"
    omnigent_agent.launch_dir(f"file://{bare}", "main", "env_1", dest, here=tmp_path)
    assert omnigent_agent.main(start_argv(dest)) != 0
    err = capsys.readouterr().err
    assert str(dest) in err and "launch-dir --trust" in err and "trusted checkout" in err
    assert not og.sessions  # nothing was created


def test_start_from_a_worktree_of_a_trusted_checkout_creates_the_session_and_writes_no_trust(
    tmp_path: Path, bare: Path, real_trust: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent()
    og.harnesses = {"claude-cloud": "acp:claude-cloud"}
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    main = checkout(bare, tmp_path / "main")
    entry = json.dumps({"projects": {str(main.resolve()): {"hasTrustDialogAccepted": True}}})
    real_trust.write_text(entry)
    dest = tmp_path / "launch"
    omnigent_agent.launch_dir(f"file://{bare}", "main", "env_1", dest, here=main)
    assert omnigent_agent.main(start_argv(dest)) == 0
    assert len(og.sessions) == 1
    assert real_trust.read_text() == entry


def test_start_pr_moves_a_worktree_launch_dirs_own_branch_not_the_checkouts(
    tmp_path: Path, bare: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(omnigent_agent, "pr_branch", lambda pr, workspace: "pr-branch")
    git("update-ref", "refs/pull/7/head", "pr-branch", cwd=bare)
    main = checkout(bare, tmp_path / "main")
    dest = tmp_path / "launch"
    omnigent_agent.launch_dir(f"file://{bare}", "main", "env_1", dest, here=main)
    omnigent_agent.move_head(dest, 7)
    assert git("rev-parse", "HEAD", cwd=dest) == git("rev-parse", "origin/pr/7", cwd=dest)
    assert git("rev-parse", "main", cwd=main) == git("rev-parse", "origin/main", cwd=main)
    assert git("symbolic-ref", "--short", "HEAD", cwd=main) == "main"


@pytest.mark.parametrize(
    ("ours", "theirs", "same"),
    [
        ("https://example.com/Team/Repo.git", "https://example.com/team/repo.git", False),
        ("https://EXAMPLE.com/Team/Repo.git/", "git@example.com:Team/Repo", True),
        ("ssh://git@example.com:22/Team/Repo.git", "https://Example.COM/Team/Repo", False),
    ],
)
def test_the_same_repo_match_is_case_blind_in_the_host_only(
    ours: str, theirs: str, same: bool
) -> None:
    # a port keeps the host apart (the third case); the path's case is the repository's own
    assert (omnigent_agent.repo_id(ours) == omnigent_agent.repo_id(theirs)) is same


def test_a_checkout_whose_remote_differs_only_in_path_case_is_not_the_same_repo(
    tmp_path: Path,
) -> None:
    here = tmp_path / "here"
    git("init", "-q", str(here), cwd=tmp_path)
    git("remote", "add", "origin", "https://example.com/Team/Repo.git", cwd=here)
    assert omnigent_agent.same_repo_checkout("https://example.com/team/repo.git", here) is None
    found = omnigent_agent.same_repo_checkout("https://example.com/Team/Repo", here)
    assert found == (here.resolve(), "origin")


def separate_checkout(bare: Path, tmp_path: Path) -> tuple[Path, Path]:
    """A checkout whose git dir lives apart from it (``clone --separate-git-dir``), and that git dir."""
    gitdir = tmp_path / "gitdirs" / "repo.git"
    gitdir.parent.mkdir()
    main = checkout(bare, tmp_path / "main", f"--separate-git-dir={gitdir}")
    return main, gitdir


def trusting(config: Path, *folders: Path) -> None:
    config.write_text(
        json.dumps(
            {"projects": {str(f.resolve()): {"hasTrustDialogAccepted": True} for f in folders}}
        )
    )


def test_a_worktree_takes_the_trust_of_the_main_worktree_git_names(
    tmp_path: Path, bare: Path, real_trust: Path
) -> None:
    # after --separate-git-dir that is the git dir itself, which is where Claude Code reads it
    main, gitdir = separate_checkout(bare, tmp_path)
    dest = tmp_path / "launch"
    omnigent_agent.launch_dir(f"file://{bare}", "main", "env_1", dest, here=main)
    assert omnigent_agent.main_worktree(dest) == gitdir.resolve()
    trusting(real_trust, gitdir)
    assert omnigent_agent.folder_trusted(dest)


def test_a_trusted_folder_that_only_holds_the_git_dir_gives_no_trust(
    tmp_path: Path, bare: Path, real_trust: Path
) -> None:
    main, gitdir = separate_checkout(bare, tmp_path)
    trusting(real_trust, gitdir.parent)
    dest = tmp_path / "launch"
    omnigent_agent.launch_dir(f"file://{bare}", "main", "env_1", dest, here=main)
    assert not omnigent_agent.folder_trusted(dest)


def test_a_worktree_of_a_worktree_takes_the_main_checkouts_trust(
    tmp_path: Path, bare: Path, real_trust: Path
) -> None:
    main = checkout(bare, tmp_path / "main")
    side = tmp_path / "side"
    git("worktree", "add", "-q", "-b", "side", str(side), cwd=main)
    trusting(real_trust, main)
    dest = tmp_path / "launch"
    omnigent_agent.launch_dir(f"file://{bare}", "main", "env_1", dest, here=side)
    assert omnigent_agent.main_worktree(dest) == main.resolve()
    assert omnigent_agent.folder_trusted(dest)


@pytest.mark.parametrize("elsewhere", [False, True], ids=["branch-only", "checked-out-elsewhere"])
def test_start_pr_leaves_the_pr_branch_and_any_checkout_of_it_alone(
    tmp_path: Path, bare: Path, monkeypatch: pytest.MonkeyPatch, elsewhere: bool
) -> None:
    monkeypatch.setattr(omnigent_agent, "pr_branch", lambda pr, workspace: "pr-branch")
    # the PR's head is ahead of the branch the checkout holds, so moving that branch shows
    tree = git("rev-parse", "pr-branch^{tree}", cwd=bare)
    who = ("-c", "user.name=t", "-c", "user.email=t@example.com")
    ahead = git(*who, "commit-tree", tree, "-p", "pr-branch", "-m", "three", cwd=bare)
    git("update-ref", "refs/pull/7/head", ahead, cwd=bare)
    main = checkout(bare, tmp_path / "main")
    other = tmp_path / "other"
    if elsewhere:
        git("worktree", "add", "-q", str(other), "pr-branch", cwd=main)
    else:
        git("branch", "-q", "pr-branch", "origin/pr-branch", cwd=main)
    held = git("rev-parse", "refs/heads/pr-branch", cwd=main)
    dest = tmp_path / "launch"
    omnigent_agent.launch_dir(f"file://{bare}", "main", "env_1", dest, here=main)
    omnigent_agent.move_head(dest, 7)
    assert git("rev-parse", "HEAD", cwd=dest) == ahead
    assert git("symbolic-ref", "HEAD", cwd=dest) == "refs/heads/launch/launch"
    assert git("rev-parse", "refs/heads/pr-branch", cwd=main) == held
    assert git("symbolic-ref", "--short", "HEAD", cwd=main) == "main"
    if elsewhere:
        assert git("symbolic-ref", "--short", "HEAD", cwd=other) == "pr-branch"
        assert git("rev-parse", "HEAD", cwd=other) == held
        assert git("status", "--porcelain", cwd=other) == ""


def pin(folder: Path, env: str) -> None:
    (folder / ".claude").mkdir(exist_ok=True)
    (folder / ".claude" / "settings.local.json").write_text(
        json.dumps({"remote": {"defaultEnvironmentId": env}})
    )


def test_a_worktree_pin_its_main_checkouts_pin_shadows_is_refused_naming_both(
    tmp_path: Path, bare: Path, real_trust: Path
) -> None:
    main = checkout(bare, tmp_path / "main")
    pin(main, "env_A")
    with pytest.raises(omnigent_agent.Fail, match="env_B.*env_A"):
        omnigent_agent.launch_dir(f"file://{bare}", "main", "env_B", tmp_path / "b", here=main)
    assert not (tmp_path / "b").exists()  # refused before git made anything
    dest = tmp_path / "a"
    omnigent_agent.launch_dir(f"file://{bare}", "main", "env_A", dest, here=main)
    omnigent_agent.pin_environment(dest)  # the same pin as its checkout's passes
    pin(dest, "env_B")
    with pytest.raises(omnigent_agent.Fail, match="env_B.*env_A"):
        omnigent_agent.pin_environment(dest)
    (main / ".claude" / "settings.local.json").unlink()
    omnigent_agent.pin_environment(dest)  # an unpinned checkout leaves the worktree's own pin


def test_launch_dir_run_from_a_linked_worktree_reads_the_main_checkouts_pin(
    tmp_path: Path, bare: Path, real_trust: Path
) -> None:
    main = checkout(bare, tmp_path / "main")
    pin(main, "env_A")
    tree = tmp_path / "tree"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-q", "-b", "w", str(tree)], check=True
    )
    with pytest.raises(omnigent_agent.Fail, match="env_B.*env_A"):
        omnigent_agent.launch_dir(f"file://{bare}", "main", "env_B", tmp_path / "b", here=tree)
    assert not (tmp_path / "b").exists()


def test_launch_dir_clone_is_the_way_out_of_a_shadowing_pin(
    tmp_path: Path, bare: Path, real_trust: Path
) -> None:
    main = checkout(bare, tmp_path / "main")
    pin(main, "env_A")
    dest = tmp_path / "b"
    omnigent_agent.launch_dir(f"file://{bare}", "main", "env_B", dest, here=main, clone=True)
    assert omnigent_agent.main_worktree(dest) is None
    omnigent_agent.pin_environment(dest)  # a clone's own pin is the one that applies


def test_launch_dir_and_start_read_the_same_pin_in_a_separate_git_dir_checkout(
    tmp_path: Path, bare: Path, real_trust: Path
) -> None:
    main, _ = separate_checkout(bare, tmp_path)
    pin(main, "env_A")  # the checkout root's pin, which the worktree's start does not read
    dest = tmp_path / "b"
    omnigent_agent.launch_dir(f"file://{bare}", "main", "env_B", dest, here=main)
    omnigent_agent.pin_environment(dest)  # the two agree: both accept


def test_launch_dir_clone_flag_reaches_the_command(
    tmp_path: Path, bare: Path, real_trust: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = checkout(bare, tmp_path / "main")
    pin(main, "env_A")
    monkeypatch.chdir(main)
    dest = tmp_path / "b"
    argv = ["launch-dir", f"file://{bare}", "main", "env_B", str(dest), "--clone"]
    assert omnigent_agent.main(argv) == 0
    assert omnigent_agent.main_worktree(dest) is None


def test_launch_dir_takes_the_main_checkouts_pin_when_no_environment_is_named(
    tmp_path: Path, bare: Path, real_trust: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A main checkout with a pin, no `env` on the Cloud worker line and no environment named after
    the repo: the run worktree takes the checkout's pin, not the account's Default."""
    monkeypatch.setattr(
        omnigent_agent,
        "cloud_environments",
        lambda: [{"environment_id": "env_default", "name": "Default"}],
    )
    main = checkout(bare, tmp_path / "main")
    pin(main, "env_main")
    dest = main / ".worktrees" / "claude-cloud-run-main"
    omnigent_agent.launch_dir(f"file://{bare}", "main", None, dest, here=main)
    assert omnigent_agent.environment_pin(omnigent_agent.own_settings(dest)) == "env_main"
    omnigent_agent.pin_environment(dest)  # and start's own check agrees
