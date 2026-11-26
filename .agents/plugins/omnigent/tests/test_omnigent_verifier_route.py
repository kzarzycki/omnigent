"""omnigent_agent.py: the verifier's fallback routes, one switch on the start (#890)."""

from __future__ import annotations

import argparse
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from test_omnigent_agent import FakeOmnigent, omnigent_agent


def route(name: str, **given: str | bool | None) -> argparse.Namespace:
    args = argparse.Namespace(
        verifier_route=name,
        agent="codex",
        host=None,
        model=None,
        author_model=None,
        permission_mode=None,
        same_family=False,
    )
    for key, value in given.items():
        setattr(args, key, value)
    return args


def test_gemini_route_runs_agy_unattended_on_a_named_host() -> None:
    args = route("gemini", host="kris")
    omnigent_agent.apply_route(args)
    assert (args.agent, args.permission_mode) == ("antigravity-native-ui", "bypassPermissions")
    with pytest.raises(omnigent_agent.Fail, match="pass --host"):
        omnigent_agent.apply_route(route("gemini"))


def test_claude_route_is_same_family_on_any_model_the_author_s_included() -> None:
    for given in (
        {"model": "claude-opus-5-5", "author_model": "claude-opus-5-5"},
        {"model": "claude-opus-5-5"},
        {},
    ):
        args = route("claude", **given)
        omnigent_agent.apply_route(args)
        assert (args.agent, args.same_family) == ("claude", True), given


def test_claude_route_start_on_the_author_s_own_model_starts_a_child(
    tmp_path: Path, role_file: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("review")
    argv = [
        "start",
        "codex",
        "t",
        "b.md",
        str(tmp_path / "r.md"),
        "--role",
        role_file("verifier.md"),
    ]
    argv += [
        "--verifier-route",
        "claude",
        "--model",
        "claude-opus-5-5",
        "--author-model",
        "claude-opus-5-5",
    ]
    assert omnigent_agent.main(argv) == 0
    assert list(og.sessions) == ["s0"]


def test_signature_names_agy() -> None:
    assert (
        omnigent_agent.signature("antigravity-native", "gemini-4-hydrogen-medium", None)
        == "agy/gemini-4-hydrogen-medium/?"
    )


def test_gemini_route_without_a_host_is_refused_by_the_start(
    tmp_path: Path,
    role_file: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief = tmp_path / "b.md"
    brief.write_text("review")
    monkeypatch.setattr(omnigent_agent, "Omnigent", FakeOmnigent)
    monkeypatch.chdir(tmp_path)
    argv = [
        "run",
        "codex",
        "t",
        str(brief),
        str(tmp_path / "r.md"),
        "--role",
        role_file("verifier.md"),
    ]
    assert omnigent_agent.main([*argv, "--verifier-route", "gemini"]) == 1
    assert "pass --host" in capsys.readouterr().err


def test_gemini_route_on_the_callers_own_host_starts_a_child(
    tmp_path: Path, role_file: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent()
    og.harnesses = {"antigravity-native-ui": "antigravity-native"}
    og.host = lambda n: {"host": og.host_id}[n]  # type: ignore[method-assign]
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("review")
    argv = ["start", "codex", "t", "b.md", str(tmp_path / "r.md")]
    argv += ["--role", role_file("verifier.md"), "--verifier-route", "gemini", "--host", "host"]
    assert omnigent_agent.main(argv) == 0
    assert list(og.sessions) == ["s0"]


def test_agy_on_another_host_reads_its_brief_from_a_file_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent()
    og.host = lambda n: {"kris": "far"}[n]  # type: ignore[method-assign]
    og.host_names = {"far": "kris"}
    og.harnesses = {"antigravity-native-ui": "antigravity-native"}
    written: dict[str, str] = {}
    og.filesystem = lambda sid, path, method, body=None: written.update({path: body["content"]})  # type: ignore[attr-defined]
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("review the head " * 400)
    argv = ["start", "antigravity-native-ui", "t", "b.md", "/there/r.md", "--top-level"]
    argv += ["--host", "kris", "--workspace", "/there", "--permission-mode", "bypassPermissions"]
    assert omnigent_agent.main(argv) == 0
    sent = [omnigent_agent.text_of(i) for i in og.sessions["s0"] if i.get("role") == "user"]
    assert sent == ["Work on t: read /there/r.md.brief and do what it says."], sent
    assert "review the head" in written["/there/r.md.brief"]


def test_verifier_start_refuses_a_workspace_not_at_the_briefs_reviewed_head(
    tmp_path: Path,
    role_file: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    shas = []
    for _ in range(2):
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "c"], check=True)
        shas.append(
            subprocess.run(
                [*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
            ).stdout.strip()
        )
    og = FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)

    def start(line: str, name: str) -> int:
        (tmp_path / "b.md").write_text(f"Review it.\n{line}\n")
        argv = [
            "start",
            "codex",
            name,
            "b.md",
            str(tmp_path / f"{name}.md"),
            "--role",
            role_file("verifier.md"),
        ]
        return omnigent_agent.main(argv)

    assert start(f"- Reviewed head: {shas[0]}", "stale") == 1
    err = capsys.readouterr().err
    assert shas[0][:12] in err and shas[1][:12] in err and "checkout --detach" in err
    assert og.sessions == {}
    assert start(f"- Reviewed head: {shas[1]}", "same") == 0
    og.archived.extend(og.sessions)
    og.sessions.clear()
    assert start("no head line", "none") == 0
    # a --far brief makes its own worktree, so the launch workspace is at another commit by design
    og.sessions.clear()
    assert start(f"- Reviewed head: {shas[0]}\n- Worktree: first, in /x, run git", "far") == 0
    # a workspace that is no checkout is never a silent pass: one line says the check was skipped
    plain = tmp_path.parent / f"{tmp_path.name}-plain"  # outside the repo made above
    plain.mkdir()
    og.sessions.clear()
    capsys.readouterr()
    (tmp_path / "b.md").write_text(f"- Reviewed head: {shas[0]}\n")
    argv = [
        "start",
        "codex",
        "plain",
        "b.md",
        str(plain / "r.md"),
        "--role",
        role_file("verifier.md"),
    ]
    assert omnigent_agent.main([*argv, "--workspace", str(plain)]) == 0
    assert "HEAD not read" in capsys.readouterr().err


def test_a_start_with_a_pr_whose_head_moved_asks_for_a_new_brief(
    tmp_path: Path,
    role_file: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "c"], check=True)
    old = subprocess.run(
        [*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    # the PR got a push after the brief: the start's own checkout moves the workspace past it
    monkeypatch.setattr(
        omnigent_agent,
        "check_out_head",
        lambda ws, pr: subprocess.run(
            [*git, "commit", "-q", "--allow-empty", "-m", "d"], check=True
        ),
    )
    og = FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text(f"- Reviewed head: {old}\n")
    argv = [
        "start",
        "codex",
        "t",
        "b.md",
        str(tmp_path / "r.md"),
        "--role",
        role_file("verifier.md"),
    ]
    assert omnigent_agent.main([*argv, "--pr", "7"]) == 1
    err = capsys.readouterr().err
    assert "`approvals.py brief 7`" in err and "checkout --detach" not in err
    assert og.sessions == {}


def test_verifier_start_reads_a_remote_workspace_head_through_a_session_there(
    tmp_path: Path,
    role_file: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    old, new = "a" * 40, "b" * 40
    og = FakeOmnigent()
    og.host = lambda n: {"kris": "far"}[n]  # type: ignore[method-assign]
    og.host_names = {"far": "kris"}
    # a worktree: .git names its gitdir, whose HEAD is a branch held by the common dir
    og.procs["far"] = {
        "/there/.git": "gitdir: /clone/.git/worktrees/w\n",
        "/clone/.git/worktrees/w/HEAD": "ref: refs/heads/x\n",
        "/clone/.git/worktrees/w/commondir": "../..\n",
        "/clone/.git/refs/heads/x": new + "\n",
    }
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)

    def start(head: str, name: str) -> int:
        (tmp_path / "b.md").write_text(f"- Reviewed head: {head}\n")
        argv = ["start", "codex", name, "b.md", "/there/r.md", "--role", role_file("verifier.md")]
        return omnigent_agent.main(
            [*argv, "--top-level", "--host", "kris", "--workspace", "/there"]
        )

    assert start(old, "no-session") == 0  # no live session there: skipped, said once
    assert capsys.readouterr().err.count("HEAD not read") == 1
    og.sessions.clear()
    og.others = [{"id": "x", "host_id": "far", "status": "idle"}]
    assert start(old, "stale") == 1
    assert "bbbbbbbbbbbb" in capsys.readouterr().err and og.sessions == {}
    assert start(new, "same") == 0


@pytest.mark.parametrize("name", ["gemini", "claude"])
def test_route_flag_without_the_verifier_role_is_refused_before_a_session(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("review")
    argv = ["start", "codex", "t", "b.md", "r.md", "--verifier-route", name, "--host", "h"]
    assert omnigent_agent.main(argv) == 1
    err = capsys.readouterr().err
    assert "--role" in err and "verifier.md" in err
    assert not og.sessions
    # the variable alone is a host-wide default, not a request: a non-verifier start ignores it
    monkeypatch.setenv("OMNIGENT_VERIFIER_ROUTE", name)
    assert omnigent_agent.main(["start", "codex", "t", "b.md", "r.md"]) == 0
    assert og.sessions


def test_env_route_on_a_verifier_start_routes_to_agy(
    tmp_path: Path, role_file: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent()
    og.host = lambda n: {"h": "far"}[n]  # type: ignore[method-assign]
    og.host_names = {"far": "h"}
    og.harnesses = {"antigravity-native-ui": "antigravity-native"}
    og.filesystem = lambda sid, path, method, body=None: None  # type: ignore[attr-defined]
    agents: list[str] = []
    create = og.create
    og.create = lambda agent, *a, **k: (agents.append(agent), create(agent, *a, **k))[1]  # type: ignore[method-assign]
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.setenv("OMNIGENT_VERIFIER_ROUTE", "gemini")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("review")
    argv = [
        "start",
        "codex",
        "t",
        "b.md",
        "/w/r.md",
        "--role",
        role_file("x/verifier.md"),
        "--top-level",
        "--host",
        "h",
        "--workspace",
        "/w",
    ]
    assert omnigent_agent.main(argv) == 0
    assert agents == ["antigravity-native-ui"]


def test_a_start_here_sees_a_host_with_room() -> None:
    """This module imports none of test_omnigent_agent's fixtures: a gate's own load once made its starts wait 600 s
    on the real check and fail."""
    assert omnigent_agent.capacity() == (True, "")
