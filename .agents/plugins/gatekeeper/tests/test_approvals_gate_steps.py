"""approvals.py: loop.md's gate may name steps, each recorded, published and reused by itself (#940)."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import (
    approvals,
    committed,
    gatekeeper_md,
    git_in,
    heavy_by_run,
    pull,
)

# the real record branch functions, which the `refs` fixture fakes for every test
RECORD_REF, PUSH_RECORD = approvals.record_ref, approvals.push_record

STEP = """import pathlib, subprocess, sys
name, runs = sys.argv[1], pathlib.Path(sys.argv[2])
runs.open("a").write(name + "\\n")
switch = runs.parent / f"switch-{name}"
if switch.exists():
    subprocess.run(["git", "checkout", "-q", switch.read_text().strip()], check=True)
sys.exit(1 if (runs.parent / f"fail-{name}").exists() else 0)
"""


@pytest.fixture
def steps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A clean checkout at a commit whose policy's gate has the steps `lint` and `unit` (`where: any`) and `apple`
    (`where: host`), each a script that logs its name to `runs` and fails while a `fail-<name>` file sits beside it.
    `commands` holds each step's command, `ran()` the names run so far, `set_policy(text)` sets another policy."""
    repo, runs, script = tmp_path / "repo", tmp_path / "runs.log", tmp_path / "step.py"
    script.write_text(STEP)
    committed(repo)
    commands = {
        name: f"{sys.executable} {script} {name} {runs}" for name in ("lint", "unit", "apple")
    }
    where = {"lint": "any", "unit": "any", "apple": "host"}
    written = "".join(
        f"    - name: {name}\n      command: {command}\n      where: {where[name]}\n"
        for name, command in commands.items()
    )
    state: dict[str, Any] = {"policy": gatekeeper_md(f"loop:\n  gate_steps:\n{written}")}
    monkeypatch.setattr(approvals, "project", lambda: (None, approvals.Policy(state["policy"])))
    monkeypatch.setattr(approvals, "one_repo", lambda: None)
    monkeypatch.chdir(repo)
    head = git_in(repo, "rev-parse", "HEAD").strip()
    state.update(
        repo=repo,
        head=head,
        tree=git_in(repo, "rev-parse", "HEAD^{tree}").strip(),
        commands=commands,
        fail=lambda name: (tmp_path / f"fail-{name}").touch(),
        switch=lambda name, commit: (tmp_path / f"switch-{name}").write_text(commit),
        mend=lambda name: (tmp_path / f"fail-{name}").unlink(),
        ran=lambda: runs.read_text().split() if runs.exists() else [],
        set_policy=lambda text: state.update(policy=text),
    )
    monkeypatch.setattr(approvals, "pull_request", lambda _n: {**pull(), "headRefOid": head})
    return state


def passed_by_another_clone(refs: dict[str, dict[str, Any]], tree: str, command: str) -> None:
    """A pass of `command` on `tree` that another clone pushed to origin as its record branch."""
    refs[approvals.record_name(tree, command)] = {
        "head": "c" * 40,
        "tree": tree,
        "command": command,
        "time": "2026-01-01T00:00:00Z",
        "env": {},
        "seconds": 303,
        "slot_wait_seconds": 0,
    }


def local_record(steps: dict[str, Any], name: str) -> dict[str, Any] | None:
    path = approvals.recorded(steps["tree"], steps["commands"][name])
    return json.loads(path.read_text()) if path.exists() else None


def test_gate_steps_are_read_from_the_policy_and_a_malformed_one_is_refused(
    steps: dict[str, Any],
) -> None:
    plan = approvals.gate_plan()
    assert [(step["name"], step["where"]) for step in plan] == [
        ("lint", "any"),
        ("unit", "any"),
        ("apple", "host"),
    ]
    assert approvals.gate()[0] == "; ".join(
        f"{name}: {command}" for name, command in steps["commands"].items()
    )
    bad = {
        "where: cloud": "must be one of any, host, linux, not 'cloud'",
        "where: any\n    - name: a\n      command: echo\n      where: any": "names the name 'a' twice",
    }
    for tail, said in bad.items():
        steps["set_policy"](
            gatekeeper_md(
                f"loop:\n  gate_steps:\n    - name: a\n      command: echo a\n      {tail}\n"
            )
        )
        with pytest.raises(approvals.Refused, match=said):
            approvals.gate_plan()
    steps["set_policy"](
        gatekeeper_md(
            "loop:\n  gate: mise run check\n  gate_steps:\n    - name: a\n      command: echo a\n      where: any\n"
        )
    )
    with pytest.raises(approvals.Refused, match="sets both `gate` and `gate_steps`"):
        approvals.gate_plan()


def test_a_tree_whose_any_steps_have_a_pushed_pass_runs_only_its_host_steps_on_a_host(
    steps: dict[str, Any], refs: dict[str, dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("lint", "unit"):
        passed_by_another_clone(refs, steps["tree"], steps["commands"][name])
    assert approvals.main(["local-ci", "--record"]) == 0
    assert steps["ran"]() == ["apple"]
    out = capsys.readouterr().out
    for name in ("lint", "unit"):
        branch = approvals.RECORD_BRANCH.format(
            approvals.record_name(steps["tree"], steps["commands"][name])
        )
        assert f"{name}: passed on tree {steps['tree']} before, reused from {branch}" in out
    record = local_record(steps, "apple")
    assert record is not None and record["step"] == "apple" and record["head"] == steps["head"]
    assert isinstance(record["seconds"], int) and isinstance(record["slot_wait_seconds"], int)
    assert approvals.record_name(steps["tree"], steps["commands"]["apple"]) in refs
    whole = approvals.reusable(steps["head"])
    assert whole is not None and [one["step"] for one in whole["steps"]] == [
        "lint",
        "unit",
        "apple",
    ]
    assert whole["seconds"] == 303 * 2 + record["seconds"]


def test_a_cloud_session_runs_the_any_steps_and_leaves_the_host_step_with_no_whole_pass(
    steps: dict[str, Any],
    refs: dict[str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    assert approvals.main(["local-ci", "--record"]) == 0
    assert steps["ran"]() == ["lint", "unit"]
    assert "`apple` runs only on a host" in capsys.readouterr().out
    assert local_record(steps, "apple") is None
    assert {
        approvals.record_name(steps["tree"], steps["commands"][n]) for n in ("lint", "unit")
    } == set(refs)
    assert approvals.reusable(steps["head"]) is None
    with pytest.raises(approvals.Refused, match="`apple` runs only on a host"):
        approvals.local_ci(7, run=True)
    # the host then runs the host step alone
    monkeypatch.delenv("CLAUDE_CODE_REMOTE")
    assert approvals.main(["local-ci", "--record"]) == 0
    assert steps["ran"]() == ["lint", "unit", "apple"]
    assert approvals.reusable(steps["head"]) is not None


def test_a_step_that_failed_is_never_reused(
    steps: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    steps["fail"]("unit")
    assert approvals.main(["local-ci", "--record"]) == 1
    assert (
        f"{steps['commands']['unit']} exited 1: nothing recorded for `unit`"
        in capsys.readouterr().out
    )
    assert steps["ran"]() == ["lint", "unit"]
    assert local_record(steps, "lint") is not None and local_record(steps, "unit") is None
    assert approvals.reusable(steps["head"]) is None
    steps["mend"]("unit")
    assert approvals.main(["local-ci", "--record"]) == 0
    # lint passed and is reused; unit failed, so it runs again, and apple, never reached, runs now
    assert steps["ran"]() == ["lint", "unit", "unit", "apple"]


def test_a_single_command_gate_records_its_seconds_and_its_wait_for_the_host_gate_slot(
    steps: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """A gate without `gate_steps` keeps one record, which gains `seconds` and `slot_wait_seconds`: the run waits
    while another gate holds the host slot, and says so."""
    command = steps["commands"]["unit"]
    steps["set_policy"](gatekeeper_md(f"loop:\n  gate: {command}\n"))
    slot = Path(approvals.slot_path())
    slot.parent.mkdir(parents=True, exist_ok=True)
    held = os.open(slot, os.O_WRONLY | os.O_CREAT)
    fcntl.flock(held, fcntl.LOCK_EX)
    threading.Timer(1.5, os.close, (held,)).start()
    began = time.monotonic()
    assert approvals.main(["local-ci", "--record"]) == 0
    assert time.monotonic() - began >= 1.5
    assert "local-ci: gate: waiting for the host gate slot" in capsys.readouterr().err
    record = json.loads(approvals.recorded(steps["tree"], command).read_text())
    assert record["slot_wait_seconds"] >= 1 and isinstance(record["seconds"], int)
    assert set(record) == {
        "head", "tree", "command", "exit", "env", "time", "host", "seconds", "slot_wait_seconds",
        "load_1m", "swap_used_mb",
    }  # fmt: skip


def test_a_step_run_under_a_held_slot_takes_none_and_tells_its_command(
    steps: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run already under the slot (mutate.py's, a gate wrapper's) takes no lock of its own, since it would wait on
    its parent; the step's command is told the slot is held, so the project's wrapper takes none either."""
    seen: list[dict[str, str]] = []

    def run(words: list[str], check: bool, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        seen.append(env)
        return subprocess.CompletedProcess(words, 0)

    slot = approvals.slot_path()
    Path(slot).parent.mkdir(parents=True, exist_ok=True)
    held = os.open(slot, os.O_WRONLY | os.O_CREAT)
    fcntl.flock(held, fcntl.LOCK_EX)
    monkeypatch.setenv(approvals.SLOT_HELD, slot)
    monkeypatch.setattr(approvals.subprocess, "run", run)
    monkeypatch.setattr(approvals, "run_heavy", heavy_by_run)
    step = threading.Thread(
        target=approvals.ran_step, args=(approvals.gate_plan()[0],), daemon=True
    )
    try:
        step.start()
        step.join(5)
        assert not step.is_alive(), "the step waited on the slot its own parent holds"
    finally:
        os.close(held)
        step.join(5)
    assert seen[0][approvals.SLOT_HELD] == slot


def test_a_cloud_run_names_only_the_missing_step(
    steps: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With `lint` and `unit` passed and `apple` left, `local-ci <pr>`'s refusal names `apple` alone."""
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    assert approvals.main(["local-ci", "--record"]) == 0
    with pytest.raises(approvals.Refused) as refused:
        approvals.local_ci(7)
    said = str(refused.value)
    assert f"step `apple`: no pass for the head {steps['head']}" in said
    assert "step `lint`" not in said and "step `unit`" not in said
    assert f"the gate step `apple` ({steps['commands']['apple']}) has no pass" in said


def test_status_passes_only_when_every_step_the_run_had_to_pass_is_recorded(
    steps: dict[str, Any], refs: dict[str, dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    """A step's pass counts from this clone's record or origin's branch; with neither, the run passed nothing."""
    assert approvals.main(["local-ci", "--record"]) == 0
    _, _, exit_file = approvals.run_files(steps["head"])
    exit_file.write_text("0\n")
    approvals.ran_command_file(steps["head"]).write_text(
        "\n".join(steps["commands"].values()) + "\n"
    )
    assert approvals.local_ci_status(steps["head"]) == 0
    approvals.recorded(steps["tree"], steps["commands"]["unit"]).unlink()
    assert approvals.local_ci_status(steps["head"]) == 0  # origin's branch still holds it
    del refs[approvals.record_name(steps["tree"], steps["commands"]["unit"])]
    assert approvals.local_ci_status(steps["head"]) == 1
    assert "exited 0 without recording a pass" in capsys.readouterr().out


def test_a_step_run_takes_the_slot_and_tells_its_command_it_is_held(
    steps: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, str]] = []

    def run(words: list[str], check: bool, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        seen.append(env)
        return subprocess.CompletedProcess(words, 0)

    monkeypatch.setattr(approvals.subprocess, "run", run)
    monkeypatch.setattr(approvals, "run_heavy", heavy_by_run)
    assert approvals.ran_step(approvals.gate_plan()[0]) == (0, 0, 0)
    assert seen[0][approvals.SLOT_HELD] == approvals.slot_path()


def test_a_record_of_a_failed_step_is_never_reused(steps: dict[str, Any]) -> None:
    """A failed step writes no record; one found anyway (hand-written, another version's) holding a non-zero exit
    counts for nothing, and the step runs."""
    for name in ("lint", "unit", "apple"):
        record = {
            "head": steps["head"],
            "tree": steps["tree"],
            "command": steps["commands"][name],
            "exit": 1 if name == "unit" else 0,
            "env": {},
            "time": "2026-01-01T00:00:00Z",
        }
        path = approvals.recorded(steps["tree"], steps["commands"][name])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))
    assert approvals.reusable(steps["head"]) is None
    assert approvals.main(["local-ci", "--record"]) == 0
    assert steps["ran"]() == ["unit"]


def test_a_pushed_record_carries_its_seconds_and_a_record_pushed_before_them_still_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record branch's message holds the seconds lines, which another clone reads back; a branch pushed before
    #940, with none, is still the pass, its seconds unknown."""
    monkeypatch.setattr(approvals, "record_ref", RECORD_REF)
    monkeypatch.setattr(approvals, "push_record", PUSH_RECORD)
    monkeypatch.setattr(approvals, "project", lambda: (None, None))
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    git_in(tmp_path, "init", "-q", "--bare", str(origin))
    work.mkdir()
    git_in(work, "init", "-q")
    git_in(
        work,
        "-c",
        "user.name=l",
        "-c",
        "user.email=l@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "base",
    )
    git_in(work, "remote", "add", "origin", str(origin))
    git_in(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    monkeypatch.chdir(work)
    head, tree = (git_in(work, "rev-parse", ref).strip() for ref in ("HEAD", "HEAD^{tree}"))
    record = {"head": head, "tree": tree, "env": {}, "time": "2026-01-01T00:00:00Z"}
    approvals.push_record(record | {"command": "old"})
    old = approvals.record_ref(tree, "old", {})
    assert old is not None and "seconds" not in old
    approvals.push_record(record | {"command": "true", "seconds": 1008, "slot_wait_seconds": 1688})
    new = approvals.record_ref(tree, "true", {})
    assert new is not None and (new["seconds"], new["slot_wait_seconds"]) == (1008, 1688)


def stepped(*rows: tuple[str, str]) -> str:
    """A policy whose `gate_steps` are these (name, where) rows, each running the fixture's step script as `lint`."""
    return gatekeeper_md(
        "loop:\n  gate_steps:\n"
        + "".join(
            f"    - name: {name}\n      command: {{lint}} {name}\n      where: {where}\n"
            for name, where in rows
        )
    )


def test_a_lone_host_step_named_gate_is_a_step_left_in_a_cloud_session(
    steps: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """S1: a step named `gate` is a step, not the legacy gate: alone and `where: host`, a cloud `--record` leaves it
    and says so, rather than reading the missing pass as the one gate's."""
    steps["set_policy"](stepped(("gate", "host")).replace("{lint}", steps["commands"]["lint"]))
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    assert approvals.main(["local-ci", "--record"]) == 0
    assert steps["ran"]() == []
    assert "`gate` runs only on a host" in capsys.readouterr().out


def test_steps_named_gate_or_alone_record_and_print_as_steps(
    steps: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """S1: with several steps, one named `gate` records its `step` and prints its line like the others, and a reused
    pass says each step; a stepped plan of one step does too."""
    posted: list[str] = []
    monkeypatch.setattr(approvals, "comment", lambda _n, body: posted.append(body))
    lint = steps["commands"]["lint"]
    for rows in ((("gate", "any"), ("unit", "any")), (("only", "any"),)):
        steps["set_policy"](stepped(*rows).replace("{lint}", lint))
        capsys.readouterr()
        assert approvals.main(["local-ci", "--record"]) == 0
        out = capsys.readouterr().out
        for name, _ in rows:
            assert f"{name}: passed in " in out
            record = json.loads(approvals.recorded(steps["tree"], f"{lint} {name}").read_text())
            assert record["step"] == name
        assert approvals.main(["local-ci", "7"]) == 0
        assert all(f"`{name}` from the pass recorded on" in posted[-1] for name, _ in rows)


def other_commit(steps: dict[str, Any]) -> str:
    """A commit of the same tree as the head, beside it: a checkout switched to it is clean and holds the tree, so
    only HEAD tells the switch."""
    repo = steps["repo"]
    git_in(repo, "commit", "-q", "--allow-empty", "-m", "beside")
    other = git_in(repo, "rev-parse", "HEAD").strip()
    git_in(repo, "checkout", "-q", steps["head"])
    return other


def test_a_clean_head_switch_between_steps_fails_local_ci_and_records_nothing_after_it(
    steps: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """C1: `lint` checks out another commit, clean, of the same tree: the step fails saying the checkout changed, and
    neither it nor `unit`, which would run on the other commit, is recorded for the head's tree."""
    other = other_commit(steps)
    steps["switch"]("lint", other)
    assert approvals.main(["local-ci", "--record"]) == 1
    out = capsys.readouterr().out
    assert "changed the checkout" in out and f"HEAD moved during the gate: it is {other}" in out
    assert steps["ran"]() == ["lint"]
    assert local_record(steps, "lint") is None and local_record(steps, "unit") is None


def test_a_step_moved_to_another_tree_is_a_move_whatever_its_head_says() -> None:
    """The run checks the tree as well as HEAD: either one changed is the checkout moving."""
    assert approvals.head_moved("a" * 40, "t" * 40, "a" * 40, "t" * 40) == ""
    assert approvals.head_moved("a" * 40, "t" * 40, "a" * 40, "u" * 40)
    assert approvals.head_moved("a" * 40, "t" * 40, "b" * 40, "t" * 40)
