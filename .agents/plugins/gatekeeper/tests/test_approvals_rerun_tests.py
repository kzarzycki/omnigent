"""approvals.py rerun-tests: a red run reruns once, by the rule (R5, #2177): the failed tests plus what the fix reaches,
else the red run's whole selection, in its order, never the failed tests alone."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import (
    approvals,
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
    (repo / "other.py").write_text("other = 1\n")
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


def red_head(worker: dict[str, Any]) -> str:
    """A PR head that changed two files, and the failed-tests file a red run on it wrote: app.py's test failed."""
    worker["commit"]("app.py", "value = 2\n")
    worker["commit"]("other.py", "other = 2\n")
    worker["set_loop"](f"# Loop\n\nAffected tests: {worker['selector']}\n")
    git_in(worker["repo"], "add", "-A")
    git_in(worker["repo"], "commit", "-qm", "loop")
    path = worker["repo"].parent / "failed-tests.json"
    path.write_text(json.dumps({"failed": ["app.py::test_value"], "selected": []}))
    worker["failed"] = str(path)
    return git_in(worker["repo"], "rev-parse", "HEAD").strip()


def rerun(worker: dict[str, Any], red: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = approvals.main(["rerun-tests", worker["failed"], "--red", red])
    return code, capsys.readouterr().err


def test_a_fix_since_the_red_reruns_the_failed_tests_plus_what_the_fix_reaches(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    red = red_head(worker)
    worker["commit"]("app.py", "value = 3\n")  # the fix, in the failed test's own file
    code, err = rerun(worker, red, capsys)
    assert code == 0
    assert worker["ran"]() == [
        "selected app.py"
    ]  # what the fix reaches, not other.py, which the red ran too
    assert "reselects 1 failed test(s), so those and what it reaches run" in err


def test_no_change_since_the_red_reruns_the_red_runs_whole_selection_in_its_order(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    red = red_head(worker)
    code, err = rerun(worker, red, capsys)
    assert code == 0
    assert worker["ran"]() == ["selected app.py other.py"]  # never the failed test alone
    assert "flake candidate: app.py::test_value" in err
    assert (
        "nothing changed reselects 1 failed test(s), so the red run's whole selection runs" in err
    )
    assert "a flake: open or update its `Flaky:` issue and count the run green" in err


def test_a_change_that_misses_the_failed_test_reruns_the_whole_selection(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    red = red_head(worker)
    worker["commit"]("other.py", "other = 3\n")
    code, _ = rerun(worker, red, capsys)
    assert code == 0 and worker["ran"]() == ["selected app.py other.py"]


@pytest.mark.parametrize("given", ["main", "newer"])
def test_a_base_moved_since_the_red_is_no_change(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str], given: str
) -> None:
    """A requeue onto a newer base: the file the base moved is not the PR's fix, so the whole selection runs, whether
    `--base` names the red's base (`main`) or the base the PR was rebased onto (`newer`)."""
    red = red_head(worker)
    repo = worker["repo"]
    Path(worker["failed"]).write_text(
        json.dumps({"failed": ["extra.py::test_extra"], "selected": []})
    )
    git_in(repo, "checkout", "-q", "-b", "newer", "main")
    worker["commit"]("extra.py", "extra = 1\n")
    git_in(repo, "push", "-q", "origin", "newer:main")
    git_in(repo, "checkout", "-q", "work")
    git_in(repo, "rebase", "-q", "newer")
    code = approvals.main(["rerun-tests", worker["failed"], "--red", red, "--base", given])
    assert code == 0
    assert "nothing changed reselects" in capsys.readouterr().err
    # the PR's selection, not extra.py, which the base moved
    assert worker["ran"]() == ["selected app.py other.py"]


def test_a_red_with_no_history_shared_with_the_base_refuses(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """A red fetched without its history: refuse, naming the fetch, never a traceback and never a guess."""
    red_head(worker)
    repo = worker["repo"]
    git_in(repo, "checkout", "-q", "--orphan", "lone")
    git_in(repo, "commit", "-qm", "lone")
    lone = git_in(repo, "rev-parse", "HEAD").strip()
    git_in(repo, "checkout", "-q", "work")
    code = approvals.main(["rerun-tests", worker["failed"], "--red", lone, "--base", "main"])
    out = capsys.readouterr()
    assert code != 0 and "share no history here" in out.out + out.err
    assert worker["ran"]() == []


def test_a_rerun_that_is_red_again_stays_red(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    red = red_head(worker)
    worker["set_loop"]("# Loop\n\nAffected tests: false\n")
    git_in(
        worker["repo"], "commit", "-qam", "red again"
    )  # a loop.md change is no fix of app.py's test
    code, err = rerun(worker, red, capsys)
    assert code != 0 and "red again on the same selection: the change caused it" in err


def test_a_file_that_holds_no_failed_tests_is_never_rerun_on(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    red = red_head(worker)
    Path(worker["failed"]).write_text("not json")
    assert approvals.main(["rerun-tests", worker["failed"], "--red", red]) != 0
    assert "holds no red run's failed tests" in capsys.readouterr().out
    assert worker["ran"]() == []


def test_a_class_name_that_names_the_changed_file_is_reselected(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    red = red_head(worker)
    Path(worker["failed"]).write_text(
        json.dumps({"failed": ["app.Case::test_value"], "selected": []})
    )
    worker["commit"]("app.py", "value = 3\n")
    code, _ = rerun(worker, red, capsys)
    assert code == 0 and worker["ran"]() == ["selected app.py"]


def test_a_red_with_no_test_id_reruns_the_whole_selection(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """Its tests never started (an infrastructure fault): nothing is reselected, so the whole selection runs."""
    red = red_head(worker)
    Path(worker["failed"]).write_text(json.dumps({"failed": [], "selected": []}))
    worker["commit"]("app.py", "value = 3\n")
    code, _ = rerun(worker, red, capsys)
    assert code == 0 and worker["ran"]() == ["selected app.py other.py"]


def test_a_fix_that_misses_one_of_two_failed_tests_reruns_the_whole_selection(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    red = red_head(worker)
    failed = ["app.py::test_value", "other.py::test_other"]
    Path(worker["failed"]).write_text(json.dumps({"failed": failed, "selected": []}))
    worker["commit"](
        "app.py", "value = 3\n"
    )  # fixes the first, not the second: it is the candidate
    code, err = rerun(worker, red, capsys)
    assert code == 0 and worker["ran"]() == ["selected app.py other.py"]
    assert "flake candidate: other.py::test_other" in err and "flake candidate: app.py" not in err
