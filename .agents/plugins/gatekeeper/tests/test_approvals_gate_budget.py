"""#2445: every gate limit follows `timeouts.run`, and a selection over it fails fast."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import approvals
from test_approvals_affected_tests import worker  # noqa: F401
from test_approvals_timeouts import checkout, policy_md, verb  # noqa: F401

RUN = "timeouts:\n  run: 10m\n"  # a 600 second budget: 80% is 480 seconds


@pytest.fixture
def budget(
    worker: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """A worker whose policy sets a 10 minute `timeouts.run` and whose loop.md says `Gate: full`."""
    (worker["repo"] / "docs" / "agents" / "gatekeeper.md").write_text(policy_md(RUN))
    worker["set_loop"]("# Loop\n\nAffected tests: true\nGate: full\n")
    worker["commit"]("app.py", "x = 2\n")
    durations = tmp_path / "durations.json"
    monkeypatch.setenv("GATE_DURATIONS", str(durations))
    monkeypatch.delenv("GATE_SETUP_SECONDS", raising=False)
    monkeypatch.delenv("GATE_WORKERS", raising=False)

    def record(**seconds: float) -> None:
        durations.write_text(json.dumps({f"tests/{name}.py": s for name, s in seconds.items()}))
        for name in seconds:
            if name != "gone":
                (worker["repo"] / "tests").mkdir(exist_ok=True)
                (worker["repo"] / "tests" / f"{name}.py").write_text("")

    return {**worker, "record": record}


def test_an_estimate_over_80_percent_fails_fast_naming_estimate_budget_and_cause(
    budget: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    budget["record"](a=400, b=100)  # 500 of 600 seconds: 83%
    started = time.monotonic()
    code = approvals.main(["affected-tests"])
    assert time.monotonic() - started < 1
    err = capsys.readouterr().err
    assert code == approvals.TIMED_OUT and budget["ran"]() == []
    assert "estimate 8 minutes 20 seconds" in err and "`timeouts.run` (10 minutes)" in err
    assert "`Gate: full` selects the whole suite" in err


def test_an_estimate_at_70_percent_runs(
    budget: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    budget["record"](a=300, b=120)  # 420 of 600 seconds
    assert approvals.main(["affected-tests"]) == 0
    assert budget["ran"]() == ["mise run check base=unset"]
    assert "estimate 7 minutes" in capsys.readouterr().err


def test_setup_and_workers_enter_the_estimate(
    budget: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    budget["record"](a=800, b=100)  # 900 seconds of tests: two workers make 450
    monkeypatch.setenv("GATE_WORKERS", "2")
    assert approvals.main(["affected-tests"]) == 0
    monkeypatch.setenv("GATE_SETUP_SECONDS", "60")  # 450 + 60 = 510 > 480
    assert approvals.main(["affected-tests"]) == approvals.TIMED_OUT


def test_the_estimate_is_per_shard_where_shards_are_on(
    budget: dict[str, Any],
) -> None:
    (budget["repo"] / "docs" / "agents" / "gatekeeper.md").write_text(
        policy_md(RUN).replace("spec:", "select:\n  shards: 2\nspec:")
    )
    budget["record"](a=300, b=300, c=300, d=300)  # 1200 seconds: 600 a shard, over 480
    assert approvals.main(["affected-tests"]) == approvals.TIMED_OUT
    budget["record"](a=200, b=200, c=200, d=200)  # 400 a shard
    assert approvals.main(["affected-tests"]) == 0


def test_without_durations_the_selection_runs_and_says_no_estimate(
    budget: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert approvals.main(["affected-tests"]) == 0
    assert budget["ran"]() == ["mise run check base=unset"]
    assert "no estimate available" in capsys.readouterr().err


def test_the_nightly_recording_turns_junit_or_durations_output_into_the_shards_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite><testcase classname="tests.test_a.TestK" name="t1" time="1.5"/>'
        '<testcase classname="tests.test_a" name="t2" time="0.5"/>'
        '<testcase classname="gatekeeper.tests.test_b" name="t3" time="3"/></testsuite></testsuites>'
    )
    text = tmp_path / "out.txt"
    text.write_text(
        "12.50s call     tests/test_a.py::test_x\n0.50s setup    tests/test_a.py::test_x\n"
        "3.00s call     tests/test_b.py::test_y\n"
    )
    monkeypatch.chdir(tmp_path)
    expected = {"tests/test_a.py": 2.0, "gatekeeper/tests/test_b.py": 3.0}
    assert approvals.parse_durations(junit.read_text()) == expected
    assert approvals.parse_durations(text.read_text()) == {
        "tests/test_a.py": 13.0,
        "tests/test_b.py": 3.0,
    }
    out = tmp_path / "durations.json"
    assert approvals.durations_verb(str(junit), str(out), None, False) == 0
    suite = tmp_path / "suite"
    suite.write_text("\n".join(expected))
    assert json.loads(out.read_text()) == expected
    # the file is the one `shards --durations` reads
    assert approvals.main(["shards", "--suite", str(suite), "--durations", str(out)]) == 0


def test_a_gate_run_writes_its_estimate_and_actual_time_and_the_report_flags_drift(
    budget: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    budget["record"](a=300, b=120)
    assert approvals.main(["affected-tests"]) == 0
    rows = [
        json.loads(line)
        for line in (budget["repo"] / ".qualify/gate-runs.jsonl").read_text().splitlines()
    ]
    assert rows[0]["estimate"] == 420 and rows[0]["actual"] >= 0
    capsys.readouterr()
    assert approvals.durations_verb(None, None, None, True) == 0
    assert (
        "drift: a gate run" in capsys.readouterr().out
    )  # a run of well under a second against 7 minutes
    approvals.note_run(budget["repo"], 100, 110, 0)
    runs = (budget["repo"] / ".qualify/gate-runs.jsonl").read_text().splitlines()
    assert len(approvals.drift_lines(budget["repo"])) == len(runs) - 1  # 10% off is no drift


def test_check_reports_a_workflow_timeout_out_of_step(checkout: Path) -> None:
    flows = checkout / ".github" / "workflows"
    flows.mkdir(parents=True)
    (flows / "ci.yml").write_text(
        "jobs:\n  qualify:\n    timeout-minutes: 45 # timeouts.run\n  other:\n    timeout-minutes: 5\n"
    )
    code, out = verb("--check")
    assert code == 1
    assert (
        ".github/workflows/ci.yml: `timeout-minutes` is 45 but `timeouts.run` (1 hour) makes it 60"
        in out
    )
    assert "other" not in out
    (flows / "ci.yml").write_text("jobs:\n  qualify:\n    timeout-minutes: 60 # timeouts.run\n")
    assert verb("--check")[0] == 0


def test_check_reports_a_wall_clock_variable_and_a_hand_set_land_wait(checkout: Path) -> None:
    (checkout / "docs" / "agents" / "loop.md").write_text(
        "# Loop\n\nWall-clock variable: SELECT_SECONDS\n"
    )
    (checkout / "mise.toml").write_text('[env]\nSELECT_SECONDS = "1800"\n')
    (checkout / "docs" / "agents" / "gatekeeper.md").write_text(policy_md("land:\n  wait: 3h\n"))
    code, out = verb("--check")
    assert code == 1
    assert "mise.toml: `SELECT_SECONDS` is 1800 seconds but `timeouts.run` makes it 3600" in out
    assert "`land.wait` is 3 hours but `timeouts.run` makes it 2 hours" in out


def test_check_reports_a_nightly_whose_push_paths_differ_from_build_inputs(checkout: Path) -> None:
    (checkout / "docs" / "agents" / "gatekeeper.md").write_text(
        policy_md("select:\n  build_inputs:\n    - uv.lock\n    - pyproject.toml\n")
    )
    flows = checkout / ".github" / "workflows"
    flows.mkdir(parents=True)
    nightly = "# timeouts: nightly\non:\n  push:\n    branches: [main]\n    paths:\n      - uv.lock\n{more}"
    (flows / "nightly.yml").write_text(nightly.format(more=""))
    code, out = verb("--check")
    assert (
        code == 1
        and "the nightly's push paths ['uv.lock'] differ from `select.build_inputs`" in out
    )
    (flows / "nightly.yml").write_text(nightly.format(more="      - pyproject.toml\n"))
    assert verb("--check")[0] == 0


def test_build_inputs_in_queue_takes_floor_or_full_and_nothing_else() -> None:
    for value in ("floor", "full"):
        approvals.settings(
            approvals.Policy(policy_md(f"select:\n  build_inputs_in_queue: {value}\n"))
        )
    with pytest.raises(approvals.Refused, match="build_inputs_in_queue"):
        approvals.settings(approvals.Policy(policy_md("select:\n  build_inputs_in_queue: some\n")))


def test_a_deleted_test_file_is_no_part_of_the_estimate(budget: dict[str, Any]) -> None:
    budget["record"](a=300, gone=900)  # `gone` no longer exists: 300 seconds, not 1200
    assert approvals.main(["affected-tests"]) == 0


def test_a_nested_runs_line_with_no_file_is_no_recorded_test() -> None:
    assert approvals.parse_durations(
        "0.000s setup ::test_inner\n1.0s call tests/test_a.py::t\n"
    ) == {"tests/test_a.py": 1.0}
