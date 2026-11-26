"""`start --cloud-env <name>` (#2246): one cloud start in an environment the project does not pin, from a launch
dir of its own, with the environments API faked. The Omnigent bridge reads a workspace's pin each time it creates
the session's cloud session (the first prompt, and again after a failed setup or an unstarted stall), so the
start never repins a shared workspace for a while and puts it back: the launch dir it runs from holds that pin."""

from __future__ import annotations

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
from test_omnigent_cloud_env import git, pin, repo

ENVS = [
    {"environment_id": "env_x", "name": "widget", "kind": "anthropic_cloud"},
    {"environment_id": "env_xcm", "name": "widget-code-mode", "kind": "anthropic_cloud"},
    {"environment_id": "env_default", "name": "Default", "kind": "anthropic_cloud"},
]


@pytest.fixture
def og(monkeypatch: pytest.MonkeyPatch) -> FakeOmnigent:
    class Hosted(FakeOmnigent):
        host = omnigent_agent.Omnigent.host  # the real lookup, over the hosts the fake lists

    fake = Hosted()
    fake.harnesses = {"claude-cloud": "acp:claude-cloud"}
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: fake)
    monkeypatch.setattr(omnigent_agent, "cloud_environments", lambda: list(ENVS))
    return fake


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, Path]:
    """A checkout whose loop.md names `env widget`, and a launch dir of its own (a clone of it, so no main
    checkout's pin shadows it) pinned to `widget-code-mode`."""
    work = repo(tmp_path, "Cloud worker: claude-cloud, gate test, env widget", name="work")
    remote = tmp_path / "remote.git"
    git("clone", "-q", "--bare", str(work), str(remote), cwd=tmp_path)
    launch = tmp_path / "launch-code-mode"
    omnigent_agent.launch_dir(f"file://{remote}", "main", "env_xcm", launch, clone=True)
    return work, launch


def start(workspace: Path, title: str, *more: str, agent: str = "claude-cloud") -> int:
    report = workspace / "tmp" / "loop" / f"{title}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    argv = ["start", agent, title, "do X", str(report), "--workspace", str(workspace)]
    return omnigent_agent.main([*argv, "--confirm-seconds", "0", *more])


def test_a_cloud_env_start_runs_in_that_environment_and_the_next_plain_start_gets_the_projects(
    og: FakeOmnigent, project: tuple[Path, Path]
) -> None:
    work, launch = project
    assert start(launch, "code-mode", "--cloud-env", "widget-code-mode") == 0
    assert len(og.sessions) == 1 and og.workspaces[-1] == str(launch.resolve())
    # the launch dir keeps its own pin: loop.md's `env widget` does not replace it for this start
    assert pin(launch) == "env_xcm"
    # the workspace the project pins was never touched, so a plain start from it gets widget
    assert start(work, "plain") == 0
    assert pin(work) == "env_x"
    # and a plain start from the launch dir follows the project again
    assert start(launch, "plain-launch") == 0
    assert pin(launch) == "env_x"


def test_a_cloud_env_start_from_a_workspace_pinned_to_another_environment_is_refused_naming_launch_dir(
    og: FakeOmnigent, project: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    work, _ = project
    assert start(work, "plain") == 0 and pin(work) == "env_x"
    assert start(work, "code-mode", "--cloud-env", "widget-code-mode") != 0
    err = capsys.readouterr().err
    assert "launch-dir" in err and "env_xcm" in err
    assert len(og.sessions) == 1  # the plain start's only
    assert pin(work) == "env_x"


def test_an_unknown_cloud_env_fails_before_any_session_naming_the_environments_there(
    og: FakeOmnigent, project: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _, launch = project
    assert start(launch, "nope", "--cloud-env", "widget-nope") != 0
    err = capsys.readouterr().err
    assert "widget-nope" in err
    assert all(e["name"] in err for e in ENVS)
    assert "claude-code-mode --cloud-env" in err
    assert og.sessions == {}
    assert pin(launch) == "env_xcm"


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_cloud_env_on_a_host_start_is_refused_before_any_session(
    og: FakeOmnigent,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    agent: str,
) -> None:
    listed: list[int] = []
    monkeypatch.setattr(omnigent_agent, "cloud_environments", lambda: listed.append(1) or ENVS)
    work = repo(tmp_path, None, name="host")
    git("worktree", "add", "-q", str(tmp_path / "wt"), cwd=work)
    assert start(tmp_path / "wt", "host", "--cloud-env", "widget", agent=agent) != 0
    assert "--cloud-env" in capsys.readouterr().err
    assert og.sessions == {} and listed == []  # refused before the login is asked


def test_cloud_env_on_another_host_is_refused_before_any_session(
    og: FakeOmnigent, project: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """The launch dir's pin is read on this host; one on another host is that host's to check."""
    _, launch = project
    og.host_names = {"h2": "other"}
    assert start(launch, "far", "--cloud-env", "widget-code-mode", "--host", "other") != 0
    assert "--cloud-env" in capsys.readouterr().err
    assert og.sessions == {}


def test_a_cloud_env_worktree_whose_main_checkout_pins_another_environment_is_refused(
    og: FakeOmnigent, project: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`claude --cloud` in a linked worktree takes its main checkout's pin, so a worktree pinned to the variant
    under a main checkout pinned to the project's environment would run the project's."""
    work, _ = project
    assert start(work, "plain") == 0 and pin(work) == "env_x"
    tree = work.parent / "tree"
    git("worktree", "add", "-q", str(tree), cwd=work)
    (tree / ".claude").mkdir(exist_ok=True)
    (tree / ".claude" / "settings.local.json").write_text(
        '{"remote": {"defaultEnvironmentId": "env_xcm"}}'
    )
    assert start(tree, "code-mode", "--cloud-env", "widget-code-mode") != 0
    assert "launch-dir --clone" in capsys.readouterr().err
    assert len(og.sessions) == 1  # the plain start's only


def test_a_cloud_env_start_stays_on_this_host_with_its_launch_dir(
    og: FakeOmnigent, project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cloud start with --workspace goes to the host with the most memory (#838), in a workspace of that host's;
    the launch dir --cloud-env checked is here, so the start stays here."""
    _, launch = project
    og.host_names = {"h2": "other"}
    monkeypatch.setattr(
        omnigent_agent, "pick_cloud_host", lambda og, workspace: ("h2", Path("/far/ws"))
    )
    assert start(launch, "code-mode", "--cloud-env", "widget-code-mode") == 0
    assert og.workspaces[-1] == str(launch.resolve())
    assert og.hosts_of[next(iter(og.sessions))] == og.host_id
