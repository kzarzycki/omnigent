"""approvals.py affected-tests: loop.md's `Affected tests:` line is the worker's gate on a draft head (#1174)."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import (
    approvals,
    gatekeeper_md,
    git_in,
)

# a selector: the tests a change affects since the base it reads from GATE_BASE (or the variable `Gate base:` names), none
# for a docs-only change; it logs what it selected and runs
SELECTOR = """import os, pathlib, subprocess, sys
var = sys.argv[1] if len(sys.argv) > 1 else "GATE_BASE"
changed = subprocess.run(["git", "diff", "--name-only", os.environ[var], "HEAD"], capture_output=True, text=True, check=True).stdout.split()
chosen = [f for f in changed if f.endswith(".py")]
log = pathlib.Path(__file__).parent / "ran.log"
log.open("a").write("selected " + (" ".join(chosen) or "nothing") + "\\n")
sys.exit(0)
"""

# a stand-in `mise` that logs its arguments and whether the base variable reached it
MISE = """#!/bin/sh
echo "mise $* base=${GATE_BASE:-unset}" >> "$(dirname "$0")/ran.log"
"""


@pytest.fixture
def worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A clone of an origin holding `main`, on a branch of its own, with loop.md's text settable and a fake `mise`
    on the path. `ran()` lists what the selector and `mise` logged; `commit(path, text)` commits a file."""
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    git_in(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git_in(tmp_path, "clone", "-q", str(origin), str(repo))
    git_in(repo, "config", "user.email", "loop@example.com")
    git_in(repo, "config", "user.name", "loop")
    git_in(repo, "checkout", "-q", "-b", "main")
    (repo / "docs" / "agents").mkdir(parents=True)
    (repo / "app.py").write_text("value = 1\n")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-qm", "base")
    git_in(repo, "push", "-q", "-u", "origin", "main")
    git_in(repo, "remote", "set-head", "origin", "main")
    git_in(repo, "checkout", "-q", "-b", "work")
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "selector.py").write_text(SELECTOR)
    (tools / "mise").write_text(MISE)
    (tools / "mise").chmod((tools / "mise").stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("GATE_BASE", raising=False)
    monkeypatch.delenv("MY_BASE", raising=False)
    monkeypatch.setenv(approvals.WORKING_TREE_POLICY, "1")
    monkeypatch.setattr(
        approvals, "open_pulls", list
    )  # no PR for the branch: the default branch is the base
    monkeypatch.chdir(repo)
    log = tools / "ran.log"

    def commit(path: str, text: str) -> None:
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
        git_in(repo, "add", "-A")
        git_in(repo, "commit", "-qm", path)

    def set_loop(text: str) -> None:
        (repo / "docs" / "agents" / "loop.md").write_text(text)

    return {
        "commit": commit,
        "repo": repo,
        "set_loop": set_loop,
        "ran": lambda: log.read_text().splitlines() if log.exists() else [],
        "selector": f"{sys.executable} {tools / 'selector.py'}",
    }


def test_a_docs_only_head_runs_no_tests_under_a_selector_that_returns_none(
    worker: dict[str, Any],
) -> None:
    worker["set_loop"](f"# Loop\n\nAffected tests: {worker['selector']}\n")
    worker["commit"]("docs/guide.md", "words\n")
    assert approvals.main(["affected-tests"]) == 0
    assert worker["ran"]() == ["selected nothing"]


def test_a_code_head_runs_the_selection_against_the_merge_base(worker: dict[str, Any]) -> None:
    worker["set_loop"](f"# Loop\n\nAffected tests: {worker['selector']}\n")
    worker["commit"]("app.py", "value = 2\n")
    assert approvals.main(["affected-tests"]) == 0
    assert worker["ran"]() == ["selected app.py"]


def test_the_base_goes_in_the_variable_the_gate_base_line_names(
    worker: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    worker["set_loop"](
        f"# Loop\n\nGate base: MY_BASE\n\nAffected tests: MY_BASE_SET=1 {worker['selector']} MY_BASE\n"
    )
    worker["commit"]("app.py", "value = 2\n")
    monkeypatch.setenv("GATE_BASE", "stale")  # not the project's variable: left as it is
    assert approvals.main(["affected-tests"]) == 0
    assert worker["ran"]() == ["selected app.py"]


def test_without_the_line_the_full_check_runs(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """#2217: no selector, and no `select` cut to narrow by: the project's full check runs, never nothing."""
    worker["set_loop"]("# Loop\n\nNo selector here.\n")
    worker["commit"]("app.py", "x = 1\n")
    assert approvals.main(["affected-tests"]) == 0
    assert worker["ran"]() == ["mise run check base=unset"]
    assert (
        "affected-tests: loop.md names no `Affected tests:` selector, so it can't narrow the change: the"
        " project's full check runs" in capsys.readouterr().err
    )


def test_a_base_git_cant_resolve_selects_against_the_merge_base(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """#2217: a push of a new branch (an all-zeros base) selects against the merge base with the default branch."""
    worker["set_loop"](f"# Loop\n\nAffected tests: {worker['selector']}\n")
    worker["commit"]("app.py", "value = 2\n")
    assert approvals.main(["affected-tests", "--base", "0" * 40]) == 0
    assert worker["ran"]() == ["selected app.py"]
    assert "it selects against the merge base" in capsys.readouterr().err


def test_the_opt_out_runs_check_with_no_base_set(
    worker: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Gate: full` is the project's own choice of the full check, so it still runs it."""
    worker["set_loop"](f"# Loop\n\nAffected tests: {sys.executable} x.py\nGate: full\n")
    worker["commit"]("docs/guide.md", "words\n")
    monkeypatch.setenv("GATE_BASE", "abc")  # a caller's selection never leaks into the full run
    assert approvals.main(["affected-tests"]) == 0
    assert worker["ran"]() == ["mise run check base=unset"]


def test_a_selector_that_fails_fails_the_gate(worker: dict[str, Any]) -> None:
    worker["set_loop"](f"# Loop\n\nAffected tests: {sys.executable} -c 'raise SystemExit(3)'\n")
    assert approvals.main(["affected-tests"]) == 3


def test_land_and_local_ci_never_set_the_base_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate of `land` and `local-ci` runs the full selection: a base in the caller's environment is cleared when
    the project names it in `local_ci_env`, and never set by the gate itself; only a head proof's `env` takes it."""
    policy = approvals.Policy(gatekeeper_md("loop:\n  local_ci_env:\n    - GATE_BASE\n"))
    monkeypatch.setattr(approvals, "project", lambda: (None, policy))
    monkeypatch.setenv("GATE_BASE", "abc")
    command, _, sets = approvals.gate()
    assert "GATE_BASE" not in command and sets == {}
    assert "GATE_BASE" not in approvals.gate_env({"sets": sets})
    _, _, with_proof = approvals.gate(["GATE_BASE"])
    assert with_proof == {"GATE_BASE": "abc"}


def test_the_selection_uses_the_merge_base_not_the_tip_of_origins_base(
    worker: dict[str, Any],
) -> None:
    """Main moves on after `work` branched: its new file is no part of the change."""
    repo = worker["repo"]
    worker["set_loop"](f"# Loop\n\nAffected tests: {worker['selector']}\n")
    worker["commit"]("app.py", "value = 2\n")
    git_in(repo, "checkout", "-q", "main")
    worker["commit"]("later.py", "later = 1\n")
    git_in(repo, "push", "-q", "origin", "main")
    git_in(repo, "checkout", "-q", "work")
    assert approvals.main(["affected-tests"]) == 0
    assert worker["ran"]() == ["selected app.py"]


def test_a_gate_base_line_that_names_no_variable_is_refused(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    worker["set_loop"](f"# Loop\n\nGate base: not-a-name\n\nAffected tests: {worker['selector']}\n")
    assert approvals.main(["affected-tests"]) == 1
    assert "no variable name" in capsys.readouterr().out
    assert worker["ran"]() == []


def test_a_stacked_pr_selects_against_its_own_base(
    worker: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = worker["repo"]
    worker["set_loop"](f"# Loop\n\nAffected tests: {worker['selector']}\n")
    git_in(repo, "checkout", "-q", "-b", "stack", "main")
    worker["commit"]("parent.py", "parent = 1\n")
    git_in(repo, "push", "-q", "origin", "stack")
    git_in(repo, "checkout", "-q", "-B", "work", "stack")
    worker["commit"]("app.py", "value = 2\n")
    monkeypatch.setattr(
        approvals,
        "open_pulls",
        lambda: [
            {"headRefName": "child", "baseRefName": "work"},  # stacked on this branch, listed first
            {"headRefName": "work", "baseRefName": "stack"},
        ],
    )
    assert approvals.main(["affected-tests"]) == 0
    assert worker["ran"]() == ["selected app.py"]


def test_a_shallow_clone_is_deepened_before_the_merge_base(
    worker: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    worker["set_loop"](f"# Loop\n\nAffected tests: {worker['selector']}\n")
    worker["commit"]("app.py", "value = 2\n")
    deepened: list[Any] = []
    monkeypatch.setattr(approvals, "deepened", lambda pr: deepened.append(pr))
    assert approvals.main(["affected-tests"]) == 0
    assert deepened == [None]


def test_the_selection_and_the_fallback_run_under_the_host_gate_slot(
    worker: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probe = f"{sys.executable} -c 'import os, sys; sys.exit(0 if os.environ.get(\"LOOP_GATE_SLOT_HELD\") else 9)'"
    worker["set_loop"](f"# Loop\n\nAffected tests: {probe}\n")
    assert approvals.main(["affected-tests"]) == 0
    worker["set_loop"](f"# Loop\n\nAffected tests: {probe}\nGate: full\n")
    held: list[str | None] = []
    real = approvals.run_heavy
    monkeypatch.setattr(
        approvals,
        "run_heavy",
        lambda words, env=None, **kw: (
            held.append((env or {}).get("LOOP_GATE_SLOT_HELD")),
            real(words, env=env, **kw),
        )[1],
    )
    assert approvals.main(["affected-tests"]) == 0
    assert held and held[0]


def test_a_callers_base_never_narrows_a_plain_pass_recorded_under_the_full_gate(
    worker: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`gate_env` clears the `Gate base:` variable (else `GATE_BASE`) even when `local_ci_env` doesn't list it, and a
    head proof's `env` naming it is the one way it gets through."""
    policy = approvals.Policy(
        gatekeeper_md("loop:\n  head_proof:\n    status: gate-sel\n    env: [GATE_BASE]\n")
    )
    monkeypatch.setattr(approvals, "project", lambda: (None, policy))
    monkeypatch.setenv("GATE_BASE", "abc")
    _, _, sets = approvals.gate()
    assert "GATE_BASE" not in approvals.gate_env({"sets": sets})
    worker["set_loop"]("# Loop\n\nGate base: MY_BASE\n")
    monkeypatch.setenv("MY_BASE", "abc")
    assert "MY_BASE" not in approvals.gate_env({"sets": {}})
    _, _, with_proof = approvals.gate(["GATE_BASE"])
    assert approvals.gate_env({"sets": with_proof})["GATE_BASE"] == "abc"
