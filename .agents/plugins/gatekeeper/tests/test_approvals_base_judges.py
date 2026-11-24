"""approvals.py: land and check merge judge with origin/<base>'s code and policy, wherever they run (#926)."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import approvals, git_in, lands  # noqa: F401

LOOP = "docs/agents/loop.md"
REAL_SCRIPTS = Path(approvals.__file__).parent  # before the fixture moves the running script
REAL = approvals.judge_with_base  # the shared fixtures replace it per test


def running_at(monkeypatch: pytest.MonkeyPatch, script: Path) -> None:
    """The script file running here (RUNNING, computed at import) as `script`."""
    monkeypatch.setattr(approvals, "__file__", str(script))
    monkeypatch.setattr(approvals, "RUNNING", (str(script),))


@pytest.fixture(autouse=True)
def judging(monkeypatch: pytest.MonkeyPatch, lands: dict[str, Any], pushed: None) -> dict[str, Any]:
    """The real comparison back (the shared fixtures stub it), in a checkout whose policy files and a copy of the script
    are on origin's main, as a project that adopted the loop has them."""
    monkeypatch.setattr(approvals, "judge_with_base", REAL)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)  # CI sets it; the tests that need it say so
    work = lands["work"]
    script = work / "scripts" / "approvals.py"
    script.parent.mkdir()
    script.write_text(Path(approvals.__file__).read_text())
    skill = (
        Path(approvals.__file__).parent.parent / "SKILL.md"
    )  # which the script reads beside itself
    (work / "SKILL.md").write_text(skill.read_text())
    running_at(monkeypatch, script)
    git_in(work, "add", "-A")
    git_in(work, "commit", "-qm", "adopt the loop")
    git_in(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    git_in(work, "fetch", "-q", "origin")
    return lands


def edit(work: Path, name: str) -> None:
    """The checkout's own copy of `name`, as a PR's branch would carry it."""
    (work / name).write_text((work / name).read_text() + "\n<!-- a PR's own edit -->\n")


def pr_by_number(monkeypatch: pytest.MonkeyPatch, lands: dict[str, Any], number: int) -> None:
    """check merge's view of PR `number`: the fake GitHub's, as the PR it judges."""
    monkeypatch.setattr(approvals, "pull_request", lambda _n: lands["pulls"][number])


def test_the_comparison_is_empty_when_the_checkout_holds_the_bases_files(
    judging: dict[str, Any],
) -> None:
    assert approvals.base_drift("main") == []


@pytest.mark.parametrize(
    "name",
    [LOOP, "docs/agents/issue-tracker.md", "scripts/approvals.py"],
    ids=["loop.md", "the tracker file", "the running approvals.py"],
)
def test_the_comparison_names_each_file_that_differs_from_the_base(
    judging: dict[str, Any], name: str
) -> None:
    edit(judging["work"], name)
    assert approvals.base_drift("main") == [name]


def test_a_land_from_a_checkout_whose_loop_md_differs_refuses_naming_it_and_merges_nothing(
    judging: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    judging["open"](7, {"7.txt": "7\n"})
    edit(judging["work"], LOOP)
    code, lines = judging["land"]("7")
    assert code == 1
    assert any(LOOP in line and "origin/main" in line for line in lines)
    assert judging["merged"]() == []


def test_a_land_from_a_checkout_whose_approvals_py_differs_refuses_naming_it(
    judging: dict[str, Any],
) -> None:
    judging["open"](7, {"7.txt": "7\n"})
    edit(judging["work"], "scripts/approvals.py")
    code, lines = judging["land"]("7")
    assert code == 1
    assert any("scripts/approvals.py" in line for line in lines)
    assert judging["merged"]() == []


def test_a_land_from_the_bases_files_merges(judging: dict[str, Any]) -> None:
    judging["open"](7, {"7.txt": "7\n"})
    code, lines = judging["land"]("7")
    assert code == 0, lines
    assert judging["merged"]() == ["7"]


@pytest.mark.parametrize(
    "name",
    [LOOP, "docs/agents/gatekeeper.md", "scripts/approvals.py"],
    ids=["loop.md", "gatekeeper.md", "approvals.py"],
)
def test_check_merge_refuses_a_differing_file_naming_it(
    judging: dict[str, Any], monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    judging["open"](7, {"7.txt": "7\n"})
    pr_by_number(monkeypatch, judging, 7)
    if (judging["work"] / name).exists():
        edit(judging["work"], name)
    else:  # a policy file the base has none of
        (judging["work"] / name).write_text("a PR's own rules\n")
    with pytest.raises(approvals.Refused, match=name):
        approvals.check("merge", 7)


def test_check_merge_under_actions_refuses_a_head_that_is_not_the_base_tip(
    judging: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    head = judging["open"](7, {"7.txt": "7\n"})
    pr_by_number(monkeypatch, judging, 7)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    git_in(judging["work"], "switch", "-q", "--detach", head)
    with pytest.raises(approvals.Refused, match="not origin/main's tip"):
        approvals.check("merge", 7)


def test_check_merge_under_actions_at_the_base_tip_judges(
    judging: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    judging["open"](7, {"7.txt": "7\n"})
    pr_by_number(monkeypatch, judging, 7)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    git_in(judging["work"], "switch", "-q", "--detach", "origin/main")
    approvals.judge_with_base("main")


def test_outside_actions_a_head_that_is_not_the_base_tip_is_no_refusal(
    judging: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    head = judging["open"](7, {"7.txt": "7\n"})
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    git_in(judging["work"], "switch", "-q", "--detach", head)
    approvals.judge_with_base("main")


def moved_script(judging: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> str:
    """The running script, now at a path inside the checkout that origin's main has no file at (a PR adds or moves it)."""
    moved = judging["work"] / "scripts" / "moved_approvals.py"
    moved.write_text((judging["work"] / "scripts" / "approvals.py").read_text())
    running_at(monkeypatch, moved)
    return "scripts/moved_approvals.py"


def test_a_running_script_inside_the_checkout_that_the_base_lacks_differs(
    judging: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    name = moved_script(judging, monkeypatch)
    assert approvals.base_drift("main") == [name]


def test_land_and_check_merge_refuse_a_script_the_base_lacks_naming_it(
    judging: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    judging["open"](7, {"7.txt": "7\n"})
    pr_by_number(monkeypatch, judging, 7)
    name = moved_script(judging, monkeypatch)
    with pytest.raises(approvals.Refused, match=name):
        approvals.check("merge", 7)
    code, lines = judging["land"]("7")
    assert code == 1
    assert any(name in line for line in lines)
    assert judging["merged"]() == []


def test_a_script_installed_outside_the_checkout_has_nothing_to_compare(
    judging: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    running_at(monkeypatch, tmp_path / "elsewhere" / "approvals.py")
    assert approvals.base_drift("main") == []


def test_check_merge_stops_when_it_cannot_fetch_the_base_naming_the_ref(
    judging: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    judging["open"](7, {"7.txt": "7\n"})
    pr_by_number(monkeypatch, judging, 7)
    git_in(judging["work"], "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    with pytest.raises(approvals.Refused, match="could not fetch origin/main"):
        approvals.check("merge", 7)


def shim_in(work: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """approvals.py loaded by path from a copy of the scripts committed to origin's main, as a checkout runs it."""
    scripts = work / "scripts"
    for source in REAL_SCRIPTS.glob("*.py"):
        shutil.copy(source, scripts / source.name)
    git_in(work, "add", "-A")
    git_in(work, "commit", "-qm", "the scripts")
    git_in(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    git_in(work, "fetch", "-q", "origin")
    monkeypatch.chdir(work)
    spec = importlib.util.spec_from_file_location("approvals_by_path", scripts / "approvals.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_pr_that_edits_gatekeeper_py_is_judged_with_the_bases_copy(
    judging: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """approvals.py runs the gatekeeper's source from the file beside it, so that file is a running file as well."""
    shim = shim_in(judging["work"], monkeypatch)
    assert shim.base_drift("main") == []
    edit(judging["work"], "scripts/gatekeeper.py")
    assert shim.base_drift("main") == ["scripts/gatekeeper.py"]


def stalled_lines(
    judging: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, list[str]]:
    """`check stalled` on a base whose rules require no check and no merge queue: `land` merges it itself (#1746)."""
    monkeypatch.setattr(
        approvals, "required_checks", lambda _base: approvals.github.Required(set())
    )
    capsys.readouterr()
    code = approvals.main(["check", "stalled"])
    return code, capsys.readouterr().out.splitlines()


def test_check_stalled_judges_from_a_checkout_whose_policy_files_differ(
    judging: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A lead's worktree that edits loop.md, or lags a policy change on main: `check stalled` reads the base's policy
    and writes nothing, so the edit judges nothing and every PR is still judged (#1746)."""
    head = judging["open"](7, {"7.txt": "7\n"})
    edit(judging["work"], LOOP)
    assert approvals.base_drift("main") == [LOOP]
    assert approvals.base_drift("main", rules=False) == []
    assert stalled_lines(judging, monkeypatch, capsys) == (
        1,
        [f"PR #7 has its go and a satisfied verdict at {head} and is not landed"],
    )


def test_check_stalled_from_a_checkout_whose_running_script_differs_judges_nothing_and_says_so(
    judging: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    judging["open"](7, {"7.txt": "7\n"})
    edit(judging["work"], "scripts/approvals.py")
    code, lines = stalled_lines(judging, monkeypatch, capsys)
    assert code == 0
    assert lines == [
        (
            "PR #7 into main is not judged here: scripts/approvals.py differs from origin/main's: a PR's own copy would"
            " judge it; run from the base's code (a checkout of origin/main)"
        ),
        "1 of 1 open PRs not judged here",
    ]
