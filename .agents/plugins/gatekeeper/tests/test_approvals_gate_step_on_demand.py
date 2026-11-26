"""approvals.py: a `gate_steps` step marked `on_demand` is keyed, recorded and reused like any step, but the gate skips
it and runs it only when named (#2295)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import approvals, committed, gatekeeper_md, git_in, pull

STEP = """import pathlib, sys
name, runs = sys.argv[1], pathlib.Path(sys.argv[2])
runs.open("a").write(name + "\\n")
sys.exit(1 if (runs.parent / f"fail-{name}").exists() else 0)
"""
POLICY = "docs/agents/gatekeeper.md"


def policy(commands: dict[str, str], extra: str = "") -> str:
    """`lint` reads app.py, `unit` the tree, and `reference`, on demand, reads ref/."""
    return gatekeeper_md(
        "loop:\n  gate_steps:\n"
        f"    - name: lint\n      command: {commands['lint']}\n      where: any\n      paths: [app.py]\n"
        f"    - name: unit\n      command: {commands['unit']}\n      where: any\n"
        f"    - name: reference\n      command: {commands['reference']}\n      where: any\n"
        f"      paths: [ref/**]\n      on_demand: true\n{extra}"
    )


@pytest.fixture
def demand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refs: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """A clean checkout whose committed policy's gate has the steps `lint`, `unit` and the on-demand `reference`, each
    a script that logs its name to `runs`. `commit(files)` commits files and returns the new tree; `ran()` is the names
    run so far. origin's record branches are the `refs` fixture's, read by inputs key as by name."""
    repo, runs, script = tmp_path / "repo", tmp_path / "runs.log", tmp_path / "step.py"
    script.write_text(STEP)
    committed(repo)
    commands = {
        name: f"{sys.executable} {script} {name} {runs}" for name in ("lint", "unit", "reference")
    }

    def commit(files: dict[str, str]) -> str:
        for name, text in files.items():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text(text)
        git_in(repo, "add", "-A")
        git_in(repo, "commit", "-qm", "change")
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
            "ref/run.py": "budget = 1\n",
            "docs/readme.md": "# app\n",
            POLICY: policy(commands),
        }
    )
    return {
        "repo": repo,
        "first": first,
        "commands": commands,
        "commit": commit,
        "head": lambda: git_in(repo, "rev-parse", "HEAD").strip(),
        "ran": lambda: runs.read_text().split() if runs.exists() else [],
        "fail": lambda name: (tmp_path / f"fail-{name}").touch(),
    }


def reference(head: str) -> dict[str, Any]:
    tree = git_in(Path.cwd(), "rev-parse", f"{head}^{{tree}}").strip()
    step = next(one for one in approvals.gate_plan(every=True) if one["name"] == "reference")
    return {"tree": tree, "step": step, "key": approvals.inputs_key(tree, step)}


def test_the_gate_skips_an_on_demand_step_and_says_so(
    demand: dict[str, Any],
    refs: dict[str, dict[str, Any]],
    hub: dict[str, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert approvals.main(["local-ci", "--record"]) == 0
    assert demand["ran"]() == ["lint", "unit"]
    out = capsys.readouterr().out
    said = [line for line in out.splitlines() if line.startswith("reference:")]
    assert said == [
        (
            "reference: skipped, on demand (`on_demand: true`): it runs only when named,"
            " `local-ci --record --step reference`, and the gate's pass does not need it"
        )
    ]
    assert "Local check passed on" in out
    assert "no pass on the whole gate" not in out  # skipped is not left: the gate passed whole
    commands = demand["commands"]
    pushed = {one["command"] for one in refs.values()}
    assert pushed == {commands["lint"], commands["unit"]}
    # the gate as a whole is the other two: its pass, status and run command never name the on-demand step
    whole = approvals.reusable(demand["head"]())
    assert whole is not None and [one["step"] for one in whole["steps"]] == ["lint", "unit"]
    assert commands["reference"] not in approvals.gate()[0]
    assert commands["reference"] not in approvals.RAN["command"]
    assert all(commands["reference"] not in one["description"] for one in hub[demand["head"]()])


def test_a_step_run_by_name_records_its_pass_by_its_inputs_and_writes_no_gate_status(
    demand: dict[str, Any],
    refs: dict[str, dict[str, Any]],
    hub: dict[str, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert approvals.main(["local-ci", "--record", "--step", "reference"]) == 0
    assert demand["ran"]() == ["reference"]
    out = capsys.readouterr().out
    assert "Step reference passed on" in out and "Local check passed" not in out
    found = reference(demand["head"]())
    assert found["key"] is not None
    [record] = refs.values()
    assert (record["command"], record["step"], record["inputs"], record["tree"]) == (
        demand["commands"]["reference"],
        "reference",
        found["key"],
        demand["first"],
    )
    assert demand["head"]() not in hub  # no `gate` status: one step's pass is no pass of the gate
    # the gate's own run after it still skips it, and runs the steps it needs
    assert approvals.main(["local-ci", "--record"]) == 0
    assert demand["ran"]() == ["reference", "lint", "unit"]
    # gate-record finds its pass by its command and key
    command = demand["commands"]["reference"].split()
    assert approvals.main(["gate-record", demand["head"](), "--", *command]) == 0


def test_an_on_demand_pass_is_reused_while_its_inputs_hold_and_missing_once_they_change(
    demand: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert approvals.main(["local-ci", "--record", "--step", "reference"]) == 0
    demand["commit"]({"docs/readme.md": "# app, reworded\n"})
    capsys.readouterr()
    assert approvals.main(["local-ci", "--record", "--step", "reference"]) == 0
    assert demand["ran"]() == ["reference"]  # not run again
    assert f"reference: reused, inputs unchanged since {demand['first']}" in capsys.readouterr().out
    found = reference(demand["head"]())
    assert approvals.step_pass(demand["head"](), found["tree"], found["step"], show=False)
    # a change under ref/ is new inputs: no pass, and gate-record names it missing
    demand["commit"]({"ref/run.py": "budget = 2\n"})
    found = reference(demand["head"]())
    assert approvals.step_pass(demand["head"](), found["tree"], found["step"], show=False) is None
    command = demand["commands"]["reference"].split()
    assert approvals.main(["gate-record", demand["head"](), "--", *command]) == 1
    assert f"no record of `{' '.join(command)}` with inputs {found['key']}" in (
        capsys.readouterr().out
    )


def test_land_never_runs_or_needs_the_on_demand_step(
    demand: dict[str, Any], refs: dict[str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """land reads the head's gate record (head_recorded, gate_record), and `local-ci <pr> --run` runs the gate on it:
    neither runs the on-demand step nor needs a pass of it."""
    head = demand["head"]()
    monkeypatch.setattr(approvals, "pull_request", lambda _n: {**pull(), "headRefOid": head})
    monkeypatch.setattr(approvals, "comment", lambda *_a, **_k: None)
    assert approvals.local_ci(7, run=True) == 0
    assert demand["ran"]() == ["lint", "unit"]
    assert approvals.head_recorded({**pull(), "number": 7, "headRefOid": head})
    assert approvals.main(["gate-record", head]) == 0
    assert "reference" not in approvals.no_pass(head)
    assert approvals.queue_unkeyed() == []


def test_an_unkeyed_on_demand_step_is_never_in_a_batch(demand: dict[str, Any]) -> None:
    text = policy(demand["commands"], 'select:\n  unkeyed: ["reference"]\n')
    demand["commit"]({POLICY: text})
    assert approvals.queue_unkeyed() == []


def test_a_step_named_outside_gate_steps_or_without_record_alone_is_refused(
    demand: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert approvals.main(["local-ci", "--record", "--step", "refernce"]) == 1
    said = capsys.readouterr().out
    assert "local-ci --step refernce: loop.md's `gate_steps` has no step of that name" in said
    assert "(its steps: lint, unit, reference)" in said
    for argv in (["local-ci", "7", "--step", "reference"], ["local-ci", "--record", "--rerun"]):
        argv = argv if "--step" in argv else [*argv, "--step", "reference"]
        assert approvals.main(argv) == 1
        assert "local-ci --step goes with --record alone" in capsys.readouterr().out
    assert demand["ran"]() == []


def test_on_demand_must_be_a_bool_and_a_typo_is_still_refused(demand: dict[str, Any]) -> None:
    for written, key in (("on_demand: yes please", "on_demand"), ("on_demnad: true", "on_demnad")):
        text = gatekeeper_md(
            "loop:\n  gate_steps:\n"
            f"    - name: lint\n      command: echo lint\n      where: any\n      {written}\n"
        )
        (demand["repo"] / POLICY).write_text(text)
        with pytest.raises(approvals.Refused, match=key):
            approvals.gate_plan()
    # each step dict says whether it is on demand; a plain step is not
    (demand["repo"] / POLICY).write_text(policy(demand["commands"]))
    assert [one["on_demand"] for one in approvals.gate_plan(every=True)] == [False, False, True]
    assert json.dumps([one["name"] for one in approvals.gate_plan()]) == '["lint", "unit"]'


def test_a_checkout_with_changes_skips_it_too_unless_named(
    demand: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    (demand["repo"] / "app.py").write_text("value = 5\n")
    assert approvals.main(["local-ci", "--record"]) == 0
    assert demand["ran"]() == ["lint", "unit"]
    assert "reference: skipped, on demand" in capsys.readouterr().out
    assert approvals.main(["local-ci", "--record", "--step", "reference"]) == 0
    assert demand["ran"]() == ["lint", "unit", "reference"]


def test_a_named_step_that_fails_writes_no_gate_failure(
    demand: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    """A failed `--step` run says nothing of the gate: the head's `gate` status is left as the gate's own run set it."""
    demand["fail"]("reference")
    assert approvals.main(["local-ci", "--record", "--step", "reference"]) == 1
    assert demand["head"]() not in hub
    demand["fail"]("lint")
    assert approvals.main(["local-ci", "--record"]) == 1
    assert [one["state"] for one in hub[demand["head"]()]] == ["failure"]


def test_status_passes_a_named_run_reused_by_its_inputs(demand: dict[str, Any]) -> None:
    """`local-ci --status` of a `--step` run on a tree that reused the step's pass by its inputs: passed."""
    assert approvals.main(["local-ci", "--record", "--step", "reference"]) == 0
    demand["commit"]({"docs/readme.md": "# again\n"})
    assert approvals.main(["local-ci", "--record", "--step", "reference"]) == 0
    head = demand["head"]()
    assert approvals.RAN["command"] == demand["commands"]["reference"]
    approvals.run_files(head)[2].parent.mkdir(parents=True, exist_ok=True)
    approvals.run_files(head)[2].write_text("0\n")
    approvals.ran_command_file(head).write_text(approvals.RAN["command"] + "\n")
    assert approvals.local_ci_status(head) == 0


def test_a_named_run_writes_no_gate_event_and_the_gate_does(
    demand: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The branch's `gate` event (events.md) is the gate's: a `--step` run, pass or fail, writes none."""
    events: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        approvals.loop_events,
        "emit",
        lambda kind, branch, state, **_k: events.append((kind, state)),
    )
    assert approvals.main(["local-ci", "--record", "--step", "reference"]) == 0
    demand["fail"]("reference")
    demand["commit"]({"ref/run.py": "budget = 3\n"})
    assert approvals.main(["local-ci", "--record", "--step", "reference"]) == 1
    assert events == []
    assert approvals.main(["local-ci", "--record"]) == 0
    assert events[-1] == ("gate", "ok")


def test_a_gate_whose_every_step_is_on_demand_is_refused(demand: dict[str, Any]) -> None:
    # an empty gate would let `gate-record <head>` and land's record check pass a head with no run at all
    text = gatekeeper_md(
        "loop:\n  gate_steps:\n"
        "    - name: reference\n      command: echo ref\n      where: any\n      on_demand: true\n"
    )
    (demand["repo"] / POLICY).write_text(text)
    for every in (False, True):
        with pytest.raises(approvals.Refused, match="no step"):
            approvals.gate_plan(every=every)
