"""approvals.py: a step the host's policy file forbids is refused before it spawns, on every path that runs one."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from approvals_harness import (
    SCRIPTS,
    approvals,
)

POLICY = "# heavy here\nmise run check  # whole suite\napprovals.py local-ci\nswift build\n"


@pytest.fixture
def policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "host-commands"
    path.write_text(POLICY)
    monkeypatch.setenv("AGENT_HOST_COMMANDS", str(path))
    return path


@pytest.mark.parametrize(
    "words",
    [
        ["mise", "run", "check"],
        ["uv", "run", "/x/y/approvals.py", "local-ci", "--run"],
        ["python3", "approvals.py", "local-ci"],
        ["swift", "build", "-c", "release"],
        ["uv", "run", "python3", "approvals.py", "local-ci"],
        ["/usr/bin/python3", "/x/approvals.py", "local-ci"],
        ["swiftly", "run", "swift", "build"],
        ["/bin/sh", "-c", "swift build -c release"],
        ["bash", "-lc", "cd x && swift build"],
        ["sh", "-c", "X=1 swift build"],
        ["sh", "-c", "timeout 300 mise run check"],
        ["timeout", "300", "mise", "run", "check"],
        ["env", "A=1", "swift", "build"],
        ["dash", "-c", "swift build", "name"],
    ],
)
def test_a_listed_command_is_refused(policy: Path, words: list[str]) -> None:
    with pytest.raises(
        approvals.Refused, match="no gate record.*gate it on an allowed host or in claude-cloud"
    ):
        approvals.run_heavy(words)


def test_the_refusal_spawns_nothing(
    policy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("spawned")

    monkeypatch.setattr(approvals.subprocess, "Popen", boom)
    with pytest.raises(approvals.Refused):
        approvals.run_heavy(["mise", "run", "check"])
    with pytest.raises(approvals.Refused):
        approvals.heavy(["mise", "run", "check"])


def test_a_command_not_listed_runs(policy: Path) -> None:
    assert approvals.run_heavy(["true"])[0] == 0
    assert approvals.host_forbids(["mise", "run", "test"]) is None


def test_a_host_without_a_policy_runs_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_HOST_COMMANDS", str(tmp_path / "none"))
    assert approvals.host_forbids(["mise", "run", "check"]) is None
    assert approvals.run_heavy(["true"])[0] == 0


def test_mutate_refuses_a_forbidden_test_command(
    policy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sys.path.insert(0, str(SCRIPTS.parents[3] / "loop" / "skills" / "engineering-loop" / "scripts"))
    import mutate

    with pytest.raises(mutate.Refused, match="gate it on an allowed host"):
        mutate.run_command("swift build", "/dev/null")
