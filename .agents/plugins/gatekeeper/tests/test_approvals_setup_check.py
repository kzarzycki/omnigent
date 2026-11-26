"""approvals.py check setup: a consumer's gate fails where a loop step would exit non-zero after a pack upgrade."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from approvals_harness import approvals, gatekeeper_md, git_in

BULLETS = "# Gatekeeper\n\n## Policy\n\n- spec: auto unless risk\n- merge: auto unless risk\n"
FENCED = gatekeeper_md("spec:\n  auto: true\n")
UV_TASK = '[tasks."loop:approvals"]\nrun = "uv run .agents/skills/engineering-loop/scripts/approvals.py check merge"\n'
BARE_TASK = UV_TASK.replace("uv run", "python3")
WORKFLOW = """name: approvals
on: pull_request
jobs:
  approvals:
    runs-on: ubuntu-latest
    steps:
      - uses: jdx/mise-action@v4
        with:
          %s
      - run: mise run loop:approvals merge 1
"""
FLOW = ".github/workflows/approvals.yml"
TABLE_TASK = (
    '[tasks]\nlogin = "python3 .agents/skills/engineering-loop/scripts/approvals.py check merge"\n'
)
VIA_TASK = UV_TASK + '[tasks.check]\nrun = "mise run loop:approvals merge 1"\n'
PACK_TASK_FLOW = WORKFLOW.replace("loop:approvals", "check")


@pytest.fixture(autouse=True)
def fresh() -> None:
    for cached in (approvals.default_base, approvals.unbased, approvals.differs):
        if clear := getattr(cached, "cache_clear", None):
            clear()


def repo_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str]) -> Path:
    """A checkout with no origin (so its working tree is read) holding the files, by path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git_in(repo, "init", "-q", "-b", "main")
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    monkeypatch.chdir(repo)
    return repo


POLICY = "docs/agents/gatekeeper.md"


def run_setup(capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    """`check setup` through the command line: its exit code and what it printed."""
    code = approvals.main(["check", "setup"])
    return code, capsys.readouterr().out


def test_a_bullet_policy_fails_the_check_and_names_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = repo_with(tmp_path, monkeypatch, {POLICY: BULLETS, "mise.toml": UV_TASK})
    code, out = run_setup(capsys)
    assert code == 1
    assert "docs/agents/gatekeeper.md" in out and "fenced yaml block under ## Policy" in out
    assert "before the pin bump" in out
    (repo / POLICY).write_text(FENCED)
    assert run_setup(capsys)[0] == 0


def test_a_bullet_policy_does_not_hide_a_bare_python_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_with(tmp_path, monkeypatch, {POLICY: BULLETS, "mise.toml": BARE_TASK})
    code, out = run_setup(capsys)
    assert code == 1 and "fenced yaml block" in out and "mise.toml:2" in out


def test_a_bare_python_task_fails_the_check_until_it_runs_with_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = repo_with(tmp_path, monkeypatch, {POLICY: FENCED, "mise.toml": BARE_TASK})
    code, out = run_setup(capsys)
    assert code == 1 and "mise.toml:2" in out
    (repo / "mise.toml").write_text(UV_TASK)
    assert run_setup(capsys)[0] == 0


@pytest.mark.parametrize(
    "path, text",
    [
        ("mise.toml", TABLE_TASK),
        ("mise.local.toml", BARE_TASK),
        (
            ".mise/tasks/approvals",
            "#!/bin/sh\npython3 .agents/skills/engineering-loop/scripts/approvals.py check merge\n",
        ),
    ],
)
def test_a_bare_python_step_in_any_mise_form_fails_the_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    path: str,
    text: str,
) -> None:
    repo_with(tmp_path, monkeypatch, {POLICY: FENCED, path: text})
    code, out = run_setup(capsys)
    assert code == 1 and path in out


@pytest.mark.parametrize("setting", ["install: false", "install_args: node", "install_args: ''"])
def test_a_workflow_that_installs_no_uv_fails_the_check_until_it_does(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    setting: str,
) -> None:
    repo = repo_with(
        tmp_path, monkeypatch, {POLICY: FENCED, "mise.toml": UV_TASK, FLOW: WORKFLOW % setting}
    )
    code, out = run_setup(capsys)
    assert code == 1 and "job `approvals`" in out
    (repo / FLOW).write_text(WORKFLOW % "install_args: uv")
    assert run_setup(capsys)[0] == 0


def test_a_task_that_reaches_a_pack_script_through_another_task_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_with(
        tmp_path,
        monkeypatch,
        {POLICY: FENCED, "mise.toml": VIA_TASK, FLOW: PACK_TASK_FLOW % "install: false"},
    )
    code, out = run_setup(capsys)
    assert code == 1 and "job `approvals`" in out


@pytest.mark.parametrize("step", ["- run: mise install", "- uses: astral-sh/setup-uv@v6"])
def test_a_job_that_installs_uv_another_way_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], step: str
) -> None:
    flow = (WORKFLOW % "install: false").replace(
        "      - run: mise run", f"      {step}\n      - run: mise run"
    )
    repo_with(tmp_path, monkeypatch, {POLICY: FENCED, "mise.toml": UV_TASK, FLOW: flow})
    assert run_setup(capsys)[0] == 0


def test_a_job_that_only_says_check_is_not_a_pack_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    flow = (WORKFLOW % "install: false").replace("mise run loop:approvals merge 1", "cargo check")
    repo_with(tmp_path, monkeypatch, {POLICY: FENCED, "mise.toml": VIA_TASK, FLOW: flow})
    assert run_setup(capsys)[0] == 0


def test_a_file_task_is_named_by_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    body = "#!/bin/sh\nuv run .agents/skills/engineering-loop/scripts/approvals.py check merge\n"
    flow = (WORKFLOW % "install: false").replace("loop:approvals", "ci:approve")
    repo_with(tmp_path, monkeypatch, {POLICY: FENCED, ".mise/tasks/ci/approve": body, FLOW: flow})
    code, out = run_setup(capsys)
    assert code == 1 and "job `approvals`" in out


def test_a_mise_run_of_another_task_does_not_make_a_later_step_a_pack_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    flow = (WORKFLOW % "install: false").replace(
        "mise run loop:approvals merge 1", "mise run lint\n      - run: cargo check"
    )
    repo_with(tmp_path, monkeypatch, {POLICY: FENCED, "mise.toml": VIA_TASK, FLOW: flow})
    assert run_setup(capsys)[0] == 0


def test_a_mise_run_of_another_task_before_a_shell_operator_is_not_a_pack_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`mise run lint && cargo check` runs the task `lint`; `check` there is cargo's, in a step or in a task body."""
    flow = (WORKFLOW % "install: false").replace(
        "mise run loop:approvals merge 1", "mise run lint && cargo check"
    )
    ci = '[tasks.ci]\nrun = "mise run lint && cargo check"\n'
    repo = repo_with(tmp_path, monkeypatch, {POLICY: FENCED, "mise.toml": VIA_TASK, FLOW: flow})
    assert run_setup(capsys)[0] == 0
    (repo / "mise.toml").write_text(VIA_TASK + ci)
    (repo / FLOW).write_text((WORKFLOW % "install: false").replace("loop:approvals merge 1", "ci"))
    assert run_setup(capsys)[0] == 0


def test_check_setup_refuses_on_a_python_without_tomllib_and_names_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "tomllib", None)  # `import tomllib` then raises ImportError
    repo_with(tmp_path, monkeypatch, {POLICY: FENCED, "mise.toml": UV_TASK})
    code, out = run_setup(capsys)
    assert code == 1 and "Python 3.11" in out and "uv run --python '>=3.11'" in out


def test_a_task_whose_run_is_a_list_is_read_line_by_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ci = '[tasks.ci]\nrun = ["mise run fmt", "cargo check"]\n'
    flow = (WORKFLOW % "install: false").replace("loop:approvals merge 1", "ci")
    repo_with(tmp_path, monkeypatch, {POLICY: FENCED, "mise.toml": VIA_TASK + ci, FLOW: flow})
    assert run_setup(capsys)[0] == 0
