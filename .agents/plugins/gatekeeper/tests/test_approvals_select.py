"""approvals.py affected-tests: gatekeeper.md's `select` cuts the narrowed gate by build input (#1631)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import approvals, gatekeeper_md, git_in

# the real read: conftest.py's autouse `_land_heads_recorded` stubs approvals.head_recorded to True in every module but
# test_approvals_land_record, so the name is taken here, at import, before the fixture replaces it
HEAD_RECORDED = approvals.head_recorded

# the project's `Affected tests:` command: it logs that it ran, so a test sees whether the cut let it through
SELECTOR = """import pathlib, sys
(pathlib.Path(__file__).parent / "ran.log").open("a").write("selector\\n")
"""

# a `checks` command: it logs its name, and exits with the code its second argument gives
CHECK = """import pathlib, sys
(pathlib.Path(__file__).parent / "ran.log").open("a").write(sys.argv[1] + "\\n")
sys.exit(int(sys.argv[2]) if len(sys.argv) > 2 else 0)
"""

SELECT = """select:
  build_inputs:
    - src/*
    - package.json
  checks:
    - paths: [apm.yml, apm.lock.yaml, .agents/skills/*, .claude/skills/*]
      command: {check} agent-sync
    - paths: [scripts/release.sh]
      command: {check} release {release_exit}
"""


@pytest.fixture
def worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A clone on a branch of its own whose base holds loop.md's `Affected tests:` line and gatekeeper.md with the
    policy `start(policy)` gives; `commit(path)` commits a file on the branch, `ran()` lists what ran."""
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "selector.py").write_text(SELECTOR)
    (tools / "check.py").write_text(CHECK)
    log = tools / "ran.log"
    check = f"{sys.executable} {tools / 'check.py'}"
    monkeypatch.delenv("GATE_BASE", raising=False)
    monkeypatch.setenv(approvals.WORKING_TREE_POLICY, "1")
    monkeypatch.setattr(approvals, "open_pulls", list)

    (tools / "mise").write_text('#!/bin/sh\necho "mise $*" >> "$(dirname "$0")/ran.log"\n')
    (tools / "mise").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")

    def start(policy: str | None, release_exit: int = 0, selector: bool = True) -> None:
        git_in(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
        git_in(tmp_path, "clone", "-q", str(origin), str(repo))
        git_in(repo, "config", "user.email", "loop@example.com")
        git_in(repo, "config", "user.name", "loop")
        git_in(repo, "checkout", "-q", "-b", "main")
        agents = repo / "docs" / "agents"
        agents.mkdir(parents=True)
        line = f"Affected tests: {sys.executable} {tools / 'selector.py'}\n" if selector else ""
        (agents / "loop.md").write_text(f"# Loop\n\n{line}")
        if policy is not None:
            (agents / "gatekeeper.md").write_text(
                gatekeeper_md(policy.format(check=check, release_exit=release_exit))
            )
        (repo / "src").mkdir()
        (repo / "src" / "app.py").write_text("value = 1\n")
        git_in(repo, "add", "-A")
        git_in(repo, "commit", "-qm", "base")
        git_in(repo, "push", "-q", "-u", "origin", "main")
        git_in(repo, "remote", "set-head", "origin", "main")
        git_in(repo, "checkout", "-q", "-b", "work")
        monkeypatch.chdir(repo)

    def commit(*paths: str) -> None:
        for path in paths:
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            target = (
                repo / path
            )  # appended to, so a changed loop.md keeps its `Affected tests:` line
            target.write_text((target.read_text() if target.exists() else "") + f"changed {path}\n")
        git_in(repo, "add", "-A")
        git_in(repo, "commit", "-qm", "change")

    return {
        "start": start,
        "commit": commit,
        "ran": lambda: log.read_text().splitlines() if log.exists() else [],
    }


def test_a_docs_only_change_selects_zero_and_says_so(worker: dict[str, Any], capsys: Any) -> None:
    worker["start"](SELECT)
    worker["commit"]("docs/guide.md", "README.md")
    assert approvals.main(["affected-tests"]) == 0
    assert worker["ran"]() == []
    assert "affected-tests: 0 selected: non-product" in capsys.readouterr().err


def test_a_pack_bump_runs_only_its_check(worker: dict[str, Any]) -> None:
    worker["start"](SELECT)
    worker["commit"](
        "apm.yml", "apm.lock.yaml", ".claude/skills/demo/SKILL.md", ".agents/skills/x/a.py"
    )
    assert approvals.main(["affected-tests"]) == 0
    assert worker["ran"]() == ["agent-sync"]


def test_a_failing_check_fails_the_run(worker: dict[str, Any]) -> None:
    worker["start"](SELECT, release_exit=3)
    worker["commit"]("scripts/release.sh", "apm.yml")
    assert approvals.main(["affected-tests"]) == 3
    assert worker["ran"]() == ["agent-sync", "release"]  # in the order of the files git lists


@pytest.mark.parametrize(
    "path",
    [
        "src/app.py",  # a declared build input
        "package.json",
        "Makefile",  # in neither list: unclear, so it tests
        ".github/workflows/ci.yml",
        "docs/agents/gatekeeper.md",  # the selection's own policy
        "docs/agents/loop.md",
    ],
)
def test_a_build_input_or_an_unclear_path_runs_the_selector(
    worker: dict[str, Any], path: str
) -> None:
    worker["start"](SELECT)
    worker["commit"]("docs/guide.md", path)
    assert approvals.main(["affected-tests"]) == 0
    assert worker["ran"]() == ["selector"]


def test_a_build_input_under_a_non_product_glob_still_tests(worker: dict[str, Any]) -> None:
    """build_inputs win over the pack's non-product set: a project whose product is markdown declares it."""
    worker["start"](SELECT.replace("    - package.json\n", "    - package.json\n    - content/*\n"))
    worker["commit"]("content/post.md")
    assert approvals.main(["affected-tests"]) == 0
    assert worker["ran"]() == ["selector"]


def test_without_select_a_docs_change_runs_the_selector_as_before(worker: dict[str, Any]) -> None:
    worker["start"](None)
    worker["commit"]("docs/guide.md")
    assert approvals.main(["affected-tests"]) == 0
    assert worker["ran"]() == ["selector"]


def test_classify_names_inputs_and_dedupes_checks() -> None:
    select = {
        "build_inputs": ["src/*"],
        "checks": [{"paths": ["apm.yml", ".claude/*"], "command": "mise run agent-sync --frozen"}],
    }
    assert approvals.classify(["apm.yml", ".claude/skills/a/SKILL.md", "docs/x.md"], select) == (
        [],
        ["mise run agent-sync --frozen"],
    )
    assert approvals.classify(["src/a.py", "tools/x.py"], select) == (
        ["src/a.py", "tools/x.py"],
        [],
    )
    assert approvals.classify(["docs/x.md"], {}) == (["docs/x.md"], [])


@pytest.mark.parametrize(
    "path",
    [
        "docs/guide.md",
        "docs/diagram.png",  # docs/* alone
        "README.md",
        "pkg/README.md",  # *.md alone
        ".agents/skills/x/a.py",  # .agents/* alone
        ".claude/settings.json",  # .claude/* alone
        "apm.yml",
        "apm.lock.yaml",
    ],
)
def test_each_non_product_entry_is_no_build_input_without_checks(path: str) -> None:
    """A project that declares no `checks`: each NON_PRODUCT entry still makes its path non-product (a pack bump)."""
    assert approvals.classify([path], {"build_inputs": ["src/*"]}) == ([], [])


@pytest.mark.parametrize(
    "path",
    ["mise.toml", "uv.lock", "tools/affected_tests.py", "src/README.md", "apm.yml.bak"],
)
def test_a_path_in_neither_list_or_a_build_input_tests(path: str) -> None:
    assert approvals.classify([path], {"build_inputs": ["src/*"]}) == ([path], [])


def test_two_checks_sharing_a_command_are_refused() -> None:
    """One command per `checks` row: a second row with the same command is refused, so the policy says it once."""
    policy = approvals.Policy(
        gatekeeper_md(
            "select:\n  build_inputs: [src/*]\n  checks:\n    - paths: [apm.yml]\n      command: sync\n"
            "    - paths: [.claude/*]\n      command: sync\n"
        )
    )
    with pytest.raises(approvals.Refused, match="names the command 'sync' twice"):
        approvals.settings(policy)


def test_select_without_build_inputs_cuts_nothing_and_keeps_unkeyed() -> None:
    """`select.unkeyed` (#2183) shares the section: without `build_inputs` every file is a build input, as before."""
    policy = approvals.Policy(gatekeeper_md("select:\n  unkeyed: [lint]\n"))
    select = approvals.settings(policy)["select"]
    assert select == {"unkeyed": ["lint"]}
    assert approvals.classify(["docs/x.md", "apm.yml"], select) == (["docs/x.md", "apm.yml"], [])
    both = approvals.Policy(gatekeeper_md("select:\n  build_inputs: [src/*]\n  unkeyed: [lint]\n"))
    assert approvals.settings(both)["select"] == {"build_inputs": ["src/*"], "unkeyed": ["lint"]}


def base_of_work() -> str:
    return git_in(Path.cwd(), "merge-base", "origin/main", "HEAD").strip()


def test_the_narrowed_gate_of_a_pack_bump_runs_and_records_only_its_check(
    worker: dict[str, Any],
) -> None:
    worker["start"](SELECT)
    worker["commit"]("apm.yml", ".claude/skills/demo/SKILL.md")
    base = base_of_work()
    with approvals.narrowed_to(base):
        command = approvals.narrowed_gate()[0]
    assert command.endswith(" agent-sync' (narrowed)") and "select.checks:" in command
    assert approvals.narrowed_base(command) == base
    assert approvals.main(["local-ci", "--record", "--base", base]) == 0
    assert worker["ran"]() == ["agent-sync"]
    # the pass is reused by tree: a second record runs nothing
    assert approvals.main(["local-ci", "--record", "--base", base]) == 0
    assert worker["ran"]() == ["agent-sync"]


def test_the_narrowed_gate_of_a_docs_change_runs_nothing_and_says_none(
    worker: dict[str, Any],
) -> None:
    worker["start"](SELECT)
    worker["commit"]("docs/guide.md")
    base = base_of_work()
    with approvals.narrowed_to(base):
        assert approvals.narrowed_gate()[0] == f"GATE_BASE={base} select.checks: none (narrowed)"
    assert approvals.main(["local-ci", "--record", "--base", base]) == 0
    assert worker["ran"]() == []


def test_the_narrowed_gate_of_a_build_input_runs_the_selector(worker: dict[str, Any]) -> None:
    worker["start"](SELECT)
    worker["commit"]("docs/guide.md", "src/app.py")
    base = base_of_work()
    with approvals.narrowed_to(base):
        assert "select.checks" not in approvals.narrowed_gate()[0]
    assert approvals.main(["local-ci", "--record", "--base", base]) == 0
    assert worker["ran"]() == ["selector"]


def test_a_moved_build_input_still_tests(worker: dict[str, Any]) -> None:
    """`git mv src/app.py docs/app.md`: the old name is a build input, though the new one is prose (--no-renames)."""
    worker["start"](SELECT)
    git_in(Path.cwd(), "mv", "src/app.py", "docs/app.md")
    git_in(Path.cwd(), "commit", "-qm", "move")
    assert approvals.main(["affected-tests"]) == 0
    assert worker["ran"]() == ["selector"]


def test_a_diff_git_cannot_list_runs_the_selector(worker: dict[str, Any]) -> None:
    """A base with no merge base with HEAD: `git diff <base>...HEAD` fails, so the cut can't tell."""
    worker["start"](SELECT)
    worker["commit"]("docs/guide.md")
    orphan = subprocess.run(
        ["git", "commit-tree", "HEAD^{tree}", "-m", "orphan"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert approvals.changed_since(orphan) is None
    assert approvals.main(["affected-tests", "--base", orphan]) == 0
    assert worker["ran"]() == ["selector"]


# uncommitted work: the cut can't tell what the run is on, so the `Affected tests:` command runs


def test_an_uncommitted_build_input_on_a_docs_head_runs_the_selector(
    worker: dict[str, Any],
) -> None:
    worker["start"](SELECT)
    worker["commit"]("docs/guide.md")
    (Path.cwd() / "src" / "app.py").write_text("value = 2\n")
    assert approvals.main(["affected-tests"]) == 0
    assert worker["ran"]() == ["selector"]


def test_an_uncommitted_build_input_before_the_first_commit_runs_the_selector(
    worker: dict[str, Any],
) -> None:
    worker["start"](SELECT)
    (Path.cwd() / "src" / "app.py").write_text("value = 2\n")
    assert approvals.main(["affected-tests"]) == 0
    assert worker["ran"]() == ["selector"]


def test_an_untracked_build_input_runs_the_selector(worker: dict[str, Any]) -> None:
    worker["start"](SELECT)
    worker["commit"]("docs/guide.md")
    (Path.cwd() / "src" / "new.py").write_text("value = 3\n")
    assert approvals.main(["affected-tests"]) == 0
    assert worker["ran"]() == ["selector"]


def test_without_select_uncommitted_work_runs_the_selector(worker: dict[str, Any]) -> None:
    worker["start"](None)
    (Path.cwd() / "src" / "app.py").write_text("value = 2\n")
    assert approvals.main(["affected-tests"]) == 0
    assert worker["ran"]() == ["selector"]


def test_a_dirty_narrowed_record_runs_the_selector_and_records_nothing(
    worker: dict[str, Any], capsys: Any
) -> None:
    """local-ci on a checkout with changes "runs the check and records nothing" (local_ci's docstring)."""
    worker["start"](SELECT)
    worker["commit"]("docs/guide.md")
    (Path.cwd() / "src" / "app.py").write_text("value = 2\n")
    assert approvals.main(["local-ci", "--record", "--base", base_of_work()]) == 0
    assert worker["ran"]() == ["selector"]
    out = capsys.readouterr().out
    assert "nothing is recorded" in out and "select.checks" not in out


# `select` without `build_inputs` cuts nothing, on any diff


def test_select_with_only_unkeyed_runs_the_selector_on_an_empty_diff(
    worker: dict[str, Any],
) -> None:
    """HEAD is the base here, so the diff is empty."""
    worker["start"]("select:\n  unkeyed: [lint]\n")
    assert approvals.main(["affected-tests"]) == 0
    assert worker["ran"]() == ["selector"]


def test_select_with_only_unkeyed_and_uncommitted_work_runs_the_selector(
    worker: dict[str, Any],
) -> None:
    worker["start"]("select:\n  unkeyed: [lint]\n")
    (Path.cwd() / "src" / "app.py").write_text("value = 2\n")
    assert approvals.main(["affected-tests"]) == 0
    assert worker["ran"]() == ["selector"]


def test_without_select_an_empty_diff_runs_the_selector(worker: dict[str, Any]) -> None:
    worker["start"](None)
    assert approvals.main(["affected-tests"]) == 0
    assert worker["ran"]() == ["selector"]


# `local-ci <pr>` from a checkout not on the PR's head: the cut is of the PR's head, not the checkout's

NO_CI = "loop:\n  ci: false\n"


def as_pr(monkeypatch: pytest.MonkeyPatch, head: str) -> list[str]:
    """`local-ci 7` against a stand-in GitHub: the PR's head and base, and the comments it posts."""
    monkeypatch.setattr(approvals, "leads_its_session", lambda: True)
    monkeypatch.setattr(approvals, "settled_head", lambda pr, want: head)
    monkeypatch.setattr(approvals, "pull_status", lambda pr: {"baseRefName": "main"})
    monkeypatch.setattr(approvals, "base_branch", lambda pr: "main")
    posted: list[str] = []
    monkeypatch.setattr(approvals, "comment", lambda pr, body: posted.append(body))
    return posted


def head_of() -> str:
    return git_in(Path.cwd(), "rev-parse", "HEAD").strip()


@pytest.mark.parametrize(
    "policy",
    [None, SELECT, "select:\n  unkeyed: [lint]\n"],
    ids=["without-select", "select", "unkeyed-only"],
)
def test_a_build_input_prs_recorded_pass_posts_from_main(
    worker: dict[str, Any], monkeypatch: pytest.MonkeyPatch, policy: str | None
) -> None:
    worker["start"]((policy or "") + NO_CI)
    worker["commit"]("src/app.py")
    assert approvals.main(["local-ci", "--record"]) == 0
    assert worker["ran"]() == ["selector"]
    posted = as_pr(monkeypatch, head_of())
    git_in(Path.cwd(), "checkout", "-q", "main")
    assert approvals.main(["local-ci", "7"]) == 0
    assert "(narrowed)" in posted[0] and "select.checks" not in posted[0]
    assert worker["ran"]() == ["selector"]


def test_a_docs_prs_recorded_pass_posts_from_a_checkout_on_a_code_branch(
    worker: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    worker["start"](SELECT + NO_CI)
    worker["commit"]("docs/guide.md")
    assert approvals.main(["local-ci", "--record"]) == 0
    assert worker["ran"]() == []
    posted = as_pr(monkeypatch, head_of())
    git_in(Path.cwd(), "checkout", "-q", "-b", "other", "main")
    worker["commit"]("src/app.py")
    assert approvals.main(["local-ci", "7"]) == 0
    assert "select.checks: none" in posted[0]
    assert worker["ran"]() == []


def test_on_the_prs_head_a_docs_prs_recorded_pass_posts(
    worker: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    worker["start"](SELECT + NO_CI)
    worker["commit"]("docs/guide.md")
    assert approvals.main(["local-ci", "--record"]) == 0
    posted = as_pr(monkeypatch, head_of())
    assert approvals.main(["local-ci", "7"]) == 0
    assert "select.checks: none" in posted[0]
    assert worker["ran"]() == []


@pytest.mark.parametrize(
    ("paths", "key"),
    [(["docs/guide.md"], "select.checks: none"), (["apm.yml", "apm.lock.yaml"], "agent-sync")],
)
def test_a_cut_prs_recorded_pass_posts_from_a_dirty_checkout_on_main(
    worker: dict[str, Any], monkeypatch: pytest.MonkeyPatch, paths: list[str], key: str
) -> None:
    """A lead's checkout with an uncommitted edit: the cut is of the PR's head, so a docs or pack-bump PR's record is
    still found (a named head is exempt from the dirty-checkout rule)."""
    worker["start"](SELECT + NO_CI)
    worker["commit"](*paths)
    assert approvals.main(["local-ci", "--record"]) == 0
    posted = as_pr(monkeypatch, head_of())
    git_in(Path.cwd(), "checkout", "-q", "main")
    (Path.cwd() / "src" / "app.py").write_text("value = 2\n")
    assert approvals.main(["local-ci", "7"]) == 0
    assert "select.checks:" in posted[0] and key in posted[0]


def test_a_move_inside_docs_runs_nothing(worker: dict[str, Any]) -> None:
    """A moved file counts under both its names, each classified like any path: two non-product names test nothing."""
    worker["start"](SELECT)
    worker["commit"]("docs/guide.md")
    base = head_of()
    git_in(Path.cwd(), "mv", "docs/guide.md", "docs/manual.md")
    git_in(Path.cwd(), "commit", "-qm", "move")
    assert approvals.changed_since(base) == ["docs/guide.md", "docs/manual.md"]
    assert approvals.main(["affected-tests", "--base", base]) == 0
    assert worker["ran"]() == []


def test_a_docs_heads_record_is_found_by_gate_record(worker: dict[str, Any]) -> None:
    """The cut's record carries its key's `GATE_BASE` as its env, so `gate-record` (which reads the env back from the
    key) finds it, and a second record on the tree reuses it."""
    worker["start"](SELECT)
    worker["commit"]("docs/guide.md")
    base = base_of_work()
    assert approvals.main(["affected-tests", "--record"]) == 0
    words = approvals.tests_record_words(base, head_of())
    assert words == [f"GATE_BASE={base}", "select.checks:", "none", "(narrowed)"]
    assert approvals.main(["gate-record", head_of(), "--", *words]) == 0
    assert approvals.main(["affected-tests", "--record"]) == 0
    assert worker["ran"]() == []


def lands_from(where: str) -> None:
    """Move the checkout to where a lead lands from: the PR's own head, a clean or dirty main, or a code branch."""
    if where == "own-head":
        return
    git_in(Path.cwd(), "checkout", "-q", "main")
    if where == "dirty-main":
        (Path.cwd() / "notes.txt").write_text("note\n")
    elif where == "code-branch":
        git_in(Path.cwd(), "checkout", "-q", "-b", "other")
        (Path.cwd() / "src" / "other.py").write_text("value = 4\n")
        git_in(Path.cwd(), "add", "-A")
        git_in(Path.cwd(), "commit", "-qm", "other")


@pytest.mark.parametrize("where", ["own-head", "clean-main", "dirty-main", "code-branch"])
@pytest.mark.parametrize(
    "paths",
    [["docs/guide.md"], ["apm.yml", "apm.lock.yaml"], ["src/app.py"]],
    ids=["docs", "pack-bump", "build-input"],
)
def test_land_finds_the_prs_record_from_any_checkout(
    worker: dict[str, Any], paths: list[str], where: str
) -> None:
    """`land`'s head_recorded keys the record by the PR's head, not the checkout it runs in."""
    worker["start"](SELECT)
    worker["commit"](*paths)
    assert approvals.main(["affected-tests", "--record"]) == 0
    head = head_of()
    lands_from(where)
    assert HEAD_RECORDED({"number": 7, "headRefOid": head, "baseRefName": "main"})


# --- #2217: the narrowest set affected-tests can name, with no `Affected tests:` line or no base ---


@pytest.mark.parametrize(
    ("paths", "ran"),
    [
        (["docs/guide.md", "README.md"], []),  # every path non-product: zero
        (["apm.yml", ".claude/skills/demo/SKILL.md"], ["agent-sync"]),  # only the check it touches
        (["docs/guide.md", "src/app.py"], ["mise run check"]),  # a build input: the full check
        (
            ["tests/conftest.py"],
            ["mise run check"],
        ),  # test setup, which no check names: unclear, so it tests
        (
            ["tools/lonely.sh"],
            ["mise run check"],
        ),  # a file nothing names: the full check, never nothing
    ],
)
def test_without_the_line_the_select_cut_runs_then_the_full_check(
    worker: dict[str, Any], paths: list[str], ran: list[str]
) -> None:
    worker["start"](SELECT, selector=False)
    worker["commit"](*paths)
    assert approvals.main(["affected-tests"]) == 0
    assert worker["ran"]() == ran


def test_a_build_input_without_the_line_says_the_full_check_runs(
    worker: dict[str, Any], capsys: Any
) -> None:
    """With no `Affected tests:` line, the build input's line names no command that isn't there."""
    worker["start"](SELECT, selector=False)
    worker["commit"]("src/app.py")
    assert approvals.main(["affected-tests"]) == 0
    said = capsys.readouterr().err
    assert (
        "build input src/app.py: the cut can't settle it" in said
        and "the project's full check runs" in said
    )
    assert "the `Affected tests:` command runs" not in said


def test_a_non_product_change_without_the_line_selects_zero_and_says_so(
    worker: dict[str, Any], capsys: Any
) -> None:
    worker["start"](SELECT, selector=False)
    worker["commit"]("docs/guide.md")
    assert approvals.main(["affected-tests"]) == 0
    said = capsys.readouterr().err
    assert "affected-tests: 0 selected: non-product" in said and "full check" not in said


def test_a_base_git_cant_resolve_cuts_the_change_since_the_merge_base(
    worker: dict[str, Any], capsys: Any
) -> None:
    """#2217: an all-zeros base falls back to the merge base, so `select` still cuts a docs change to zero."""
    worker["start"](SELECT)
    worker["commit"]("docs/guide.md")
    assert approvals.main(["affected-tests", "--base", "0" * 40]) == 0
    assert worker["ran"]() == []
    said = capsys.readouterr().err
    assert "it selects against the merge base" in said and "0 selected: non-product" in said
