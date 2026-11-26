"""approvals.py: a gate step that names `paths` is keyed on its inputs, so a tree that changes none reuses its pass (#963)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import (
    approvals,
    committed,
    gatekeeper_md,
    git_in,
    pull,
)

# the real record branch functions, which the `refs` fixture fakes for every test
RECORD_REF, PUSH_RECORD = approvals.record_ref, approvals.push_record
INPUT_CANDIDATES = approvals.input_candidates

STEP = """import pathlib, sys
name, runs = sys.argv[1], pathlib.Path(sys.argv[2])
runs.open("a").write(name + "\\n")
"""
POLICY = "docs/agents/gatekeeper.md"
# each step's paths: `lint` reads app.py, `apple` (a host step) ios/, and `unit` names none, so it reads the tree
PATHS: dict[str, list[str] | None] = {"lint": ["app.py"], "unit": None, "apple": ["ios/**"]}
WHERE = {"lint": "any", "unit": "any", "apple": "host"}


def policy(commands: dict[str, str], paths: dict[str, list[str] | None], extra: str = "") -> str:
    rows = "".join(
        f"    - name: {name}\n      command: {command}\n      where: {WHERE[name]}\n"
        + (f"      paths: {json.dumps(paths[name])}\n" if paths[name] is not None else "")
        for name, command in commands.items()
    )
    return gatekeeper_md(f"loop:\n  gate_steps:\n{rows}{extra}")


@pytest.fixture
def inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refs: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """A clean checkout whose committed policy's gate has the steps `lint`, `unit` and `apple` (PATHS, WHERE), each a
    script that logs its name to `runs`. `commit(files)` commits files (text, or None to delete) and returns the new
    tree, `set_paths(paths)` commits a policy with those paths, `ran()` is the names run so far. origin's record
    branches are the `refs` fixture's, read by inputs key as by name."""
    repo, runs, script = tmp_path / "repo", tmp_path / "runs.log", tmp_path / "step.py"
    script.write_text(STEP)
    committed(repo)  # the template's one commit, then `first` below
    commands = {name: f"{sys.executable} {script} {name} {runs}" for name in PATHS}

    def commit(files: dict[str, str | None]) -> str:
        for name, text in files.items():
            where = repo / name
            if text is None:
                where.unlink()
                continue
            where.parent.mkdir(parents=True, exist_ok=True)
            where.write_text(text)
        git_in(repo, "add", "-A")
        git_in(repo, "commit", "-qm", "change")
        return tree()

    def tree() -> str:
        return git_in(repo, "rev-parse", "HEAD^{tree}").strip()

    def candidates(command: str, key: str, _why: list[str] | None = None) -> list[str]:
        return [
            found["tree"]
            for found in refs.values()
            if found["command"] == command and found.get("inputs") == key
        ]

    monkeypatch.setattr(
        approvals, "project", lambda: (None, approvals.Policy((repo / POLICY).read_text()))
    )
    monkeypatch.setattr(approvals, "one_repo", lambda: None)
    monkeypatch.setattr(approvals, "input_candidates", candidates)
    monkeypatch.chdir(repo)
    first = commit(
        {
            "app.py": "value = 1\n",
            "docs/readme.md": "# app\n",
            "ios/App.swift": "let x = 1\n",
            POLICY: policy(commands, PATHS),
        }
    )
    return {
        "repo": repo,
        "first": first,
        "commands": commands,
        "commit": commit,
        "tree": tree,
        "head": lambda: git_in(repo, "rev-parse", "HEAD").strip(),
        "set_paths": lambda paths: commit({POLICY: policy(commands, paths)}),
        "ran": lambda: runs.read_text().split() if runs.exists() else [],
    }


def step(name: str) -> dict[str, Any]:
    return next(one for one in approvals.gate_plan() if one["name"] == name)


def local_record(inputs: dict[str, Any], tree: str, name: str) -> dict[str, Any] | None:
    path = approvals.recorded(tree, inputs["commands"][name])
    return json.loads(path.read_text()) if path.exists() else None


def test_a_diff_outside_a_steps_paths_reuses_its_pass_and_names_the_source_tree(
    inputs: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert approvals.main(["local-ci", "--record"]) == 0
    assert inputs["ran"]() == ["lint", "unit", "apple"]
    capsys.readouterr()
    second = inputs["commit"]({"docs/readme.md": "# app, a comment-only fix\n"})
    assert approvals.main(["local-ci", "--record"]) == 0
    # `unit` names no paths, so it reads the whole tree and runs again; the other two read nothing that changed
    assert inputs["ran"]() == ["lint", "unit", "apple", "unit"]
    out = capsys.readouterr().out
    for name in ("lint", "apple"):
        assert f"{name}: reused, inputs unchanged since {inputs['first']}" in out
    whole = approvals.reusable(inputs["head"]())
    assert whole is not None and whole["tree"] == second
    assert [one["tree"] for one in whole["steps"]] == [inputs["first"], second, inputs["first"]]


def test_a_diff_touching_one_path_inside_runs_the_step(inputs: dict[str, Any]) -> None:
    assert approvals.main(["local-ci", "--record"]) == 0
    inputs["commit"]({"ios/App.swift": "let x = 2\n"})
    assert approvals.main(["local-ci", "--record"]) == 0
    assert inputs["ran"]()[3:] == ["unit", "apple"]
    # a rename inside its paths is a change of what it reads, though no file's content changed
    inputs["commit"]({"ios/App.swift": None, "ios/Main.swift": "let x = 2\n"})
    assert approvals.main(["local-ci", "--record"]) == 0
    assert inputs["ran"]()[5:] == ["unit", "apple"]
    # and so is a file made executable
    (inputs["repo"] / "app.py").chmod(0o755)
    git_in(inputs["repo"], "commit", "-qam", "executable")
    assert approvals.main(["local-ci", "--record"]) == 0
    assert inputs["ran"]()[7:] == ["lint", "unit"]


def test_a_diff_that_changes_gate_steps_runs_every_step(inputs: dict[str, Any]) -> None:
    """Only `apple`'s paths change, and no file `lint` reads: `lint` runs again all the same, since the setting
    is in every step's key."""
    assert approvals.main(["local-ci", "--record"]) == 0
    inputs["set_paths"](PATHS | {"apple": ["ios/**", "Package.swift"]})
    assert approvals.main(["local-ci", "--record"]) == 0
    assert inputs["ran"]() == ["lint", "unit", "apple"] * 2


def test_a_step_without_paths_keeps_the_whole_tree_key(
    inputs: dict[str, Any], refs: dict[str, dict[str, Any]]
) -> None:
    assert approvals.main(["local-ci", "--record"]) == 0
    unit = local_record(inputs, inputs["first"], "unit")
    assert unit is not None and "inputs" not in unit
    assert "inputs" in (local_record(inputs, inputs["first"], "lint") or {})
    assert approvals.record_name(inputs["first"], inputs["commands"]["unit"]) in refs
    assert approvals.inputs_key(inputs["first"], step("unit")) is None
    # paths that match no file, or none, read nothing a key could stand for: the step keeps the tree key and runs
    for paths in (["nothing/**"], []):
        inputs["set_paths"](PATHS | {"lint": paths})
        assert approvals.inputs_key(inputs["tree"](), step("lint")) is None
        assert approvals.main(["local-ci", "--record"]) == 0
        inputs["commit"]({"docs/readme.md": f"# {paths}\n"})
        assert approvals.main(["local-ci", "--record"]) == 0
        assert inputs["ran"]()[-2:] == ["lint", "unit"]
    assert approvals.inputs_key("f" * 40, step("apple")) is None  # a tree git doesn't hold


def test_a_step_without_paths_never_lists_the_tree(
    inputs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A step keyed by the tree reads no listing of it, which on a large tree costs each lookup a walk of it."""
    monkeypatch.setattr(approvals, "git", lambda *args: pytest.fail(f"git {' '.join(args)}"))
    assert approvals.inputs_key(inputs["first"], step("unit")) is None


def test_each_step_record_carries_the_hosts_load_and_swap_at_its_start(
    inputs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals, "host_load", lambda: {"load_1m": 3.5, "swap_used_mb": 812})
    assert approvals.main(["local-ci", "--record"]) == 0
    for name in PATHS:
        record = local_record(inputs, inputs["first"], name)
        assert record is not None and (record["load_1m"], record["swap_used_mb"]) == (3.5, 812)


def test_the_host_load_reads_the_load_average_and_swap_where_the_host_says_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 100 kB\nSwapTotal:  4194304 kB\nSwapFree:   3145728 kB\n")
    monkeypatch.setattr(approvals, "MEMINFO", str(meminfo))
    monkeypatch.setattr(approvals.os, "getloadavg", lambda: (2.345, 1.0, 0.5))
    assert approvals.host_load() == {"load_1m": 2.35, "swap_used_mb": 1024}
    # macOS has no /proc: its sysctl says the swap in use
    monkeypatch.setattr(approvals, "MEMINFO", str(tmp_path / "none"))
    said = "total = 2048.00M  used = 1536.75M  free = 511.25M  (encrypted)\n"
    monkeypatch.setattr(
        approvals.subprocess,
        "run",
        lambda words, **_k: subprocess.CompletedProcess(
            words, 0, said if "sysctl" in words else ""
        ),
    )
    assert approvals.swap_used_mb() == 1536

    def unknown() -> tuple[float, float, float]:
        raise OSError("no load average")

    monkeypatch.setattr(approvals.os, "getloadavg", unknown)
    monkeypatch.setattr(
        approvals.subprocess, "run", lambda words, **_k: subprocess.CompletedProcess(words, 1, "")
    )
    assert approvals.host_load() == {"load_1m": None, "swap_used_mb": None}


def test_a_record_wrong_in_any_one_field_is_never_reused_by_its_inputs(
    inputs: dict[str, Any],
) -> None:
    """This clone's records of `lint`'s command, each sound but for one field, or for its own tree, which holds other
    inputs than the key it says: none is the pass, so `lint` runs on the next tree."""
    lint = step("lint")
    key = approvals.inputs_key(inputs["first"], lint)
    other = inputs["commit"]({"app.py": "value = 9\n"})
    git_in(inputs["repo"], "reset", "-q", "--hard", "HEAD~1")
    sound = {
        "head": inputs["head"](),
        "tree": inputs["first"],
        "command": lint["command"],
        "exit": 0,
        "env": {},
        "time": "2026-01-01T00:00:00Z",
        "step": "lint",
        "inputs": key,
    }
    wrong = [
        {"exit": 1},
        {"env": {"TIER": "1"}},
        {"step": "unit"},
        {"inputs": "0" * 64},
        {"inputs": None},
        {"time": None},
        {"tree": other},  # its tree holds another app.py, whatever key it says
    ]
    directory = approvals.local_ci_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for n, change in enumerate(wrong):
        record = sound | change
        (directory / f"{n:040x}-{approvals.gate_key(lint['command'])}.json").write_text(
            json.dumps(record)
        )
    inputs["commit"]({"docs/readme.md": "# moved on\n"})
    assert approvals.step_pass(inputs["head"](), inputs["tree"](), lint) is None
    assert approvals.main(["local-ci", "--record"]) == 0
    assert inputs["ran"]()[0] == "lint"


def test_status_and_refusals_read_a_step_reused_by_its_inputs(
    inputs: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert approvals.main(["local-ci", "--record"]) == 0
    inputs["commit"]({"docs/readme.md": "# again\n"})
    assert approvals.main(["local-ci", "--record"]) == 0
    head = inputs["head"]()
    _, _, exit_file = approvals.run_files(head)
    exit_file.write_text("0\n")
    approvals.ran_command_file(head).write_text("\n".join(inputs["commands"].values()) + "\n")
    assert approvals.local_ci_status(head) == 0
    capsys.readouterr()
    # a tree whose app.py changed has no `lint` pass: the refusal says so by its inputs, and names no other step
    inputs["commit"]({"app.py": "value = 3\n"})
    monkeypatch.setattr(
        approvals, "pull_request", lambda _n: {**pull(), "headRefOid": inputs["head"]()}
    )
    with pytest.raises(approvals.Refused) as refused:
        approvals.local_ci(7)
    said = str(refused.value)
    assert f"step `lint`: no pass on its inputs in tree {inputs['tree']()}" in said
    assert "step `apple`" not in said


def test_a_pushed_pass_is_reused_by_inputs_only_when_its_own_tree_holds_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real record branches on a bare origin: the branch of tree A, pushed with its `Inputs:` line, load and swap, is
    `lint`'s pass on tree B, which changes only docs; a branch of tree C, whose app.py differs, saying A's key, is
    not, though its message says so."""
    monkeypatch.setattr(approvals, "record_ref", RECORD_REF)
    monkeypatch.setattr(approvals, "push_record", PUSH_RECORD)
    monkeypatch.setattr(approvals, "input_candidates", INPUT_CANDIDATES)
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    git_in(tmp_path, "init", "-q", "--bare", str(origin))
    work.mkdir()
    git_in(work, "init", "-q")
    git_in(work, "config", "user.email", "loop@example.com")
    git_in(work, "config", "user.name", "loop")
    git_in(work, "remote", "add", "origin", str(origin))
    command = "true lint"
    text = gatekeeper_md(
        "loop:\n  gate_steps:\n"
        f"    - name: lint\n      command: {command}\n      where: any\n      paths: [app.py]\n"
    )
    monkeypatch.setattr(approvals, "project", lambda: (None, approvals.Policy(text)))
    monkeypatch.chdir(work)

    def commit(name: str, body: str) -> tuple[str, str]:
        (work / name).write_text(body)
        git_in(work, "add", "-A")
        git_in(work, "commit", "-qm", name)
        return git_in(work, "rev-parse", "HEAD").strip(), git_in(
            work, "rev-parse", "HEAD^{tree}"
        ).strip()

    a_head, a_tree = commit("app.py", "value = 1\n")
    git_in(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    lint = approvals.gate_plan()[0]
    key = approvals.inputs_key(a_tree, lint)
    assert key is not None
    calls: list[tuple[str, ...]] = []
    real_git = approvals.git
    monkeypatch.setattr(approvals, "git", lambda *args: calls.append(args) or real_git(*args))
    assert approvals.input_candidates(command, key) == []
    assert not [args for args in calls if args[0] == "fetch"]  # no branch, nothing fetched
    record = {"head": a_head, "tree": a_tree, "command": command, "env": {}}
    approvals.push_record(
        record
        | {"time": "2026-01-01T00:00:00Z", "inputs": key, "seconds": 5}
        | {"load_1m": 4.25, "swap_used_mb": 640}
    )
    c_head, c_tree = commit("app.py", "value = 2\n")
    approvals.push_record(
        {"head": c_head, "tree": c_tree, "command": command, "env": {}}
        | {"time": "2026-01-02T00:00:00Z", "inputs": key}
    )
    other = approvals.inputs_key(c_tree, lint)
    git_in(work, "reset", "-q", "--hard", a_head)
    b_head, b_tree = commit("readme.md", "# docs\n")
    shutil.rmtree(approvals.local_ci_dir(), ignore_errors=True)
    # both branches say the key; only A's tree holds those inputs
    assert sorted(approvals.input_candidates(command, key)) == sorted([a_tree, c_tree])
    assert approvals.input_candidates(command, str(other)) == []
    found = approvals.step_pass(b_head, b_tree, lint, show=False)
    assert found is not None and (found["tree"], found["head"]) == (a_tree, a_head)
    assert (found["inputs"], found["load_1m"], found["swap_used_mb"]) == (key, 4.25, 640)
    assert isinstance(found["swap_used_mb"], int)
    # with A's branch gone, C's, which lies about its inputs, is no pass
    git_in(
        work,
        "push",
        "-q",
        "origin",
        f":{approvals.RECORD_BRANCH.format(approvals.record_name(a_tree, command))}",
    )
    assert approvals.step_pass(b_head, b_tree, lint, show=False) is None
    # origin unreachable: no candidate, and the reason said
    git_in(work, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    why: list[str] = []
    assert approvals.input_candidates(command, key, why) == []
    assert why and why[0].startswith(f"origin's record branches for `{command}` could not be read")


def test_gate_step_paths_must_be_a_list_of_globs(inputs: dict[str, Any]) -> None:
    for written in ("paths: app.py", "paths: [1]"):
        text = gatekeeper_md(
            "loop:\n  gate_steps:\n"
            f"    - name: lint\n      command: echo lint\n      where: any\n      {written}\n"
        )
        (inputs["repo"] / POLICY).write_text(text)
        with pytest.raises(approvals.Refused, match="gate_steps.paths"):
            approvals.gate_plan()


def test_the_real_host_load_answers_on_this_host() -> None:
    load = approvals.host_load()
    assert set(load) == {"load_1m", "swap_used_mb"}
    if hasattr(os, "getloadavg"):
        assert isinstance(load["load_1m"], float)


def test_gate_record_finds_a_step_by_its_inputs_on_another_tree(
    inputs: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """gate-record reads a step keyed by its inputs as step_pass does: tree A's pushed pass of `lint` and `apple` is
    their pass on tree B, which changes only docs, naming A's tree and the key; `unit`, keyed by the tree, is read on
    B's tree alone. Only origin counts: this clone's own records of B never do. A change to app.py is new inputs."""
    assert approvals.main(["local-ci", "--record"]) == 0
    head_a = inputs["head"]()
    inputs["commit"]({"docs/readme.md": "# app, a comment-only fix\n"})
    head_b = inputs["head"]()
    capsys.readouterr()
    # `unit` reads the whole tree, so B, with no pass of its own, still lacks one
    assert approvals.main(["gate-record", head_b]) == 1
    out = capsys.readouterr().out
    assert f"no record of `{inputs['commands']['unit']}` on tree {inputs['tree']()}" in out
    for name in ("lint", "apple"):
        key = approvals.inputs_key(inputs["tree"](), step(name))
        assert f"Tree {inputs['first']} on {head_a[:12]}: " in out
        assert f"reused on tree {inputs['tree']()}, inputs key {key}" in out
    # a pass of `unit` on B, kept only in this clone, never counts; pushed to origin, B's gate is whole
    approvals.recorded(inputs["tree"](), inputs["commands"]["unit"]).write_text('{"exit": 0}')
    assert approvals.main(["gate-record", head_b]) == 1
    assert approvals.main(["gate-record", head_b, "--", *inputs["commands"]["lint"].split()]) == 0
    assert approvals.main(["local-ci", "--record"]) == 0
    capsys.readouterr()
    assert approvals.main(["gate-record", head_b]) == 0
    # app.py changes `lint`'s inputs: origin holds no pass with the new key
    inputs["commit"]({"app.py": "value = 2\n"})
    lint = inputs["commands"]["lint"]
    key = approvals.inputs_key(inputs["tree"](), step("lint"))
    capsys.readouterr()
    assert approvals.main(["gate-record", inputs["head"](), "--", *lint.split()]) == 1
    assert (
        f"no record of `{lint}` with inputs {key} on tree {inputs['tree']()}"
        in capsys.readouterr().out
    )
    assert approvals.main(["gate-record", inputs["head"]()]) == 1


def test_a_step_select_unkeyed_lists_runs_on_a_tree_with_its_pass(
    inputs: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """`select.unkeyed` names `lint` by name and `apple` by command: both run again on the very tree they passed on,
    while `unit`, unlisted, is reused (#2180)."""
    listed = json.dumps(["lint", inputs["commands"]["apple"]])
    inputs["commit"]({POLICY: policy(inputs["commands"], PATHS, f"select:\n  unkeyed: {listed}\n")})
    assert approvals.main(["local-ci", "--record"]) == 0
    assert approvals.main(["local-ci", "--record"]) == 0
    assert inputs["ran"]() == ["lint", "unit", "apple", "lint", "apple"]
    assert approvals.reusable(inputs["head"]()) is None
    assert "`select.unkeyed` lists it" in approvals.no_pass(inputs["head"]())
