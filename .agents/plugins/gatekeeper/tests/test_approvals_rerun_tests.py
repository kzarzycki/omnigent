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
        # git drops the folder on a checkout without loop.md
        (repo / "docs" / "agents").mkdir(parents=True, exist_ok=True)
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
    red_head(worker)
    worker["set_loop"]("# Loop\n\nAffected tests: false\n")
    git_in(worker["repo"], "commit", "-qam", "a red selection")  # the red run's own selection fails
    red = git_in(worker["repo"], "rev-parse", "HEAD").strip()
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


# a stand-in `mise` that defines no task: `run` and `tasks info` fail as mise does for an unknown task (exit 1)
NO_TASKS = """#!/bin/sh
echo "mise $*" >> "$(dirname "$0")/ran.log"
echo "mise ERROR no task $3 found" >&2
exit 1
"""


def red_with_loop(worker: dict[str, Any], loop: str) -> str:
    """A red head whose own loop.md is `loop`, committed with it, so the red run's selection is that line's."""
    red_head(worker)
    worker["set_loop"](loop)
    git_in(worker["repo"], "commit", "-qam", "the red run's loop.md")
    return git_in(worker["repo"], "rev-parse", "HEAD").strip()


def test_a_rerun_runs_the_red_heads_selection_never_the_bases(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The base's loop.md names a task the red head lacks (a head behind its base): the rerun is the red run's own
    selection, so it runs the red head's command and never the base's (#2275)."""
    repo = worker["repo"]
    git_in(repo, "checkout", "-q", "main")
    worker["commit"]("docs/agents/loop.md", "# Loop\n\nAffected tests: mise run check-affected\n")
    git_in(repo, "push", "-q", "origin", "main")
    git_in(repo, "checkout", "-q", "work")
    # the policy is origin/main's, as on a real PR
    monkeypatch.delenv(approvals.WORKING_TREE_POLICY)
    red = red_head(worker)
    code, err = rerun(worker, red, capsys)
    assert code == 0
    assert worker["ran"]() == ["selected app.py other.py"]
    assert "check-affected" not in err


def test_a_rerun_whose_task_mise_lacks_did_not_run(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """mise exits 1 for an unknown task, as for a red test: the rerun ran no test, so it is never red again (#2275)."""
    (worker["repo"].parent / "tools" / "mise").write_text(NO_TASKS)
    red = red_with_loop(worker, "# Loop\n\nAffected tests: mise run check-affected\n")
    code, err = rerun(worker, red, capsys)
    assert code == approvals.DID_NOT_RUN
    assert (
        "rerun did not run" in err
        and "`mise run check-affected`, a task mise doesn't define here" in err
    )
    assert "the change caused it" not in err


def test_a_selection_that_cannot_start_did_not_run(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    red = red_with_loop(worker, "# Loop\n\nAffected tests: /no/such/selector\n")
    code, err = rerun(worker, red, capsys)
    assert code == approvals.DID_NOT_RUN
    assert "rerun did not run" in err and "the change caused it" not in err


def test_a_red_head_with_no_selector_did_not_run(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """With no `Affected tests:` line the selection runs nothing and exits 0, which a rerun would read as a flake
    that passed: it ran no test, so it says so instead (#2275)."""
    red = red_with_loop(worker, "# Loop\n")
    code, err = rerun(worker, red, capsys)
    assert code == approvals.DID_NOT_RUN
    assert "names no `Affected tests:` selector" in err and "Flaky" not in err
    assert worker["ran"]() == []


# a selection that writes a JUnit report of `count` test cases to `report.xml`, as a runner given a junit flag does,
# and exits `code`; a `count` of `none` writes no report, as a step that fails before the runner starts
REPORTER = """import pathlib, sys
count, code = sys.argv[1], int(sys.argv[2])
if count != "none":
    cases = "".join(f'<testcase classname="app.py" name="t{i}"/>' for i in range(int(count)))
    (pathlib.Path.cwd() / "report.xml").write_text(f'<testsuite tests="{count}">{cases}</testsuite>')
sys.exit(code)
"""


def red_with_report(worker: dict[str, Any], count: int | str, code: int = 0) -> str:
    """A red head whose loop.md's selection writes a report of `count` tests to the `Test report:` path and exits
    `code`."""
    reporter = worker["repo"].parent / "tools" / "reporter.py"
    reporter.write_text(REPORTER)
    return red_with_loop(
        worker,
        f"# Loop\n\nAffected tests: {sys.executable} {reporter} {count} {code}\n\nTest report: report.xml\n",
    )


def red_ran(worker: dict[str, Any], failed: list[str], selected: list[str]) -> None:
    """The red run's failed-tests file holds `failed` and `selected`."""
    Path(worker["failed"]).write_text(json.dumps({"failed": failed, "selected": selected}))


def test_a_rerun_that_collected_zero_tests_did_not_run(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The selection exits 0 having run no test, where the red ran some: that is no flake that passed, since the
    rerun says nothing about the change (#2277)."""
    red = red_with_report(worker, 0)
    code, err = rerun(worker, red, capsys)
    assert code == approvals.DID_NOT_RUN
    assert "rerun did not run: zero tests collected" in err and "Flaky" not in err


def test_a_rerun_whose_report_shows_tests_is_read_by_its_exit(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    red = red_with_report(worker, 2)
    code, err = rerun(worker, red, capsys)
    assert code == 0
    assert "a flake: open or update its `Flaky:` issue" in err and "did not run" not in err


def test_a_report_an_earlier_run_left_is_never_counted(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """A report from before the rerun shows tests this rerun never ran: it is removed first (#2277)."""
    red = red_with_loop(
        worker, f"# Loop\n\nAffected tests: {worker['selector']}\n\nTest report: report.xml\n"
    )
    (worker["repo"] / "report.xml").write_text(
        '<testsuite><testcase classname="a" name="b"/></testsuite>'
    )
    code, err = rerun(worker, red, capsys)
    assert code == approvals.DID_NOT_RUN
    assert "zero tests collected" in err


def test_a_rerun_red_before_its_tests_is_red_again(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """A step ahead of the runner fails (a lint step, an unkeyed check), so no report is written: that is red, as any
    red rerun is, never `did not run` (#2277)."""
    red = red_with_report(worker, "none", 1)
    code, err = rerun(worker, red, capsys)
    assert code == 1
    assert "red again" in err and "did not run" not in err


def test_a_runner_that_collected_nothing_and_said_so_did_not_run(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """A runner that exits nonzero for an empty collection still writes its report, showing zero tests (#2277)."""
    red = red_with_report(worker, 0, 5)
    code, err = rerun(worker, red, capsys)
    assert code == approvals.DID_NOT_RUN
    assert "zero tests collected" in err


def test_a_red_that_listed_tests_none_of_them_failed_counts_as_having_run(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The red run's report listed tests though none failed (a red step after them): it ran some (#2277)."""
    red = red_with_report(worker, 0)
    red_ran(worker, [], ["app.py::test_value"])
    code, err = rerun(worker, red, capsys)
    assert code == approvals.DID_NOT_RUN
    assert "zero tests collected" in err


def test_a_red_that_ran_no_tests_keeps_its_reruns_exit(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """A red run with neither failed nor listed tests ran none, so a rerun that runs none is read by its exit (#2277)."""
    red = red_with_report(worker, 0)
    red_ran(worker, [], [])
    code, err = rerun(worker, red, capsys)
    assert code == 0
    assert "did not run" not in err


def test_a_fix_rerun_that_collected_zero_tests_did_not_run(
    worker: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The fix path counts the report too: a fix since the red whose rerun runs nothing is no pass (#2277)."""
    red = red_with_report(worker, 0)
    worker["commit"]("app.py", "value = 3\n")  # the fix, in the failed test's own file
    code, err = rerun(worker, red, capsys)
    assert code == approvals.DID_NOT_RUN
    assert "so those and what it reaches run" in err and "zero tests collected" in err
