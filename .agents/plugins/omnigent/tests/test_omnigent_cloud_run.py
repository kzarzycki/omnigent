"""A cloud session's run must finish inside its turn (#1002): its VM may not outlive the turn."""

import subprocess

import pytest
from test_omnigent_agent import (
    CLOUD_RECORD,
    CloudFake,
    Lead,
    ending,
    following,
    omnigent_agent,
    skewed,
    supervised,
    told,
)

DETACH_LINE = "A long or locked run is detached"
IN_TURN = "A run may not outlive the turn that started it"


@pytest.mark.parametrize(("top_level", "pr"), [(True, None), (False, None)])
def test_a_cloud_brief_carries_the_in_turn_rule_and_not_the_detach_line(
    tmp_path, monkeypatch, top_level, pr
) -> None:
    monkeypatch.delenv("LOOP_DENY_LIST", raising=False)
    og = CloudFake()
    sid, _ = omnigent_agent.start(
        og, "claude-cloud", "t", "do X", tmp_path / "r.md", None, 0, top_level=top_level, pr=pr
    )
    brief = omnigent_agent.text_of(og.sessions[sid][1])
    assert IN_TURN in brief and "end a turn only on that tracked wait, naming its task id" in brief
    # one wake per run (finding 1 of the waste audit): one tracked wait as long as the run, no poll loop
    assert "--pid <pid> --budget <the run's expected length>` started as a background call" in brief
    assert "run_in_background" in brief and "never a short `wait-for`" in brief
    assert "--budget 480" not in brief and "calling it again" not in brief
    assert "Monitor" not in brief and "in the foreground" not in brief
    assert "`STILL WORKING` that names only your own detached run is not a valid ending" in brief
    assert DETACH_LINE not in brief


def test_the_pr_cloud_preamble_carries_the_in_turn_rule() -> None:
    brief = omnigent_agent.cloud_preamble(omnigent_agent.PR_PREAMBLE, 7)
    assert IN_TURN in brief and DETACH_LINE not in brief


@pytest.mark.parametrize("preamble", ["PREAMBLE", "PR_PREAMBLE", "LEAD_PREAMBLE"])
def test_a_host_brief_keeps_the_detach_line(preamble: str) -> None:
    text = getattr(omnigent_agent, preamble)
    assert DETACH_LINE in text and IN_TURN not in text
    assert omnigent_agent.DETACH_LINE in text  # the line a cloud brief replaces


def test_the_cloud_nudges_name_the_restart_and_the_host_nudge_keeps_polling() -> None:
    for nudge in (omnigent_agent.CLOUD_NUDGE, omnigent_agent.PR_NUDGE, omnigent_agent.HELD_NUDGE):
        assert "restart it only if it did not" in nudge and "detached" not in nudge
        assert "was lost" not in nudge
    assert "Poll any long run you started detached" in omnigent_agent.NUDGE


def test_the_supervisor_nudge_of_a_cloud_lead_names_the_restart(tmp_path, monkeypatch) -> None:
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    monkeypatch.setattr(omnigent_agent, "triage", lambda event, decisions: ("act", "stalled"))
    og = Lead(told("brief", 60), ending("STILL WORKING: waiting on my background run", 0))
    og.snap = dict(CLOUD_RECORD)
    og.harnesses = {"claude-cloud": omnigent_agent.CLOUD_HARNESS}
    at = {1: 301.0, 2: 302.0}
    supervised(
        og,
        registry,
        monkeypatch,
        3,
        lambda p: offset.__setitem__(0, at[p]),
        notify="cos",
        console=True,
    )
    assert [t for s, t in og.inbox if s == "a"] == [omnigent_agent.LEAD_NUDGE_CLOUD]
    assert "restart it only if it did not" in omnigent_agent.LEAD_NUDGE_CLOUD


@pytest.mark.parametrize(
    ("ending", "own"),
    [
        ("STILL WORKING: waiting on my gate. Report: r.md", True),
        ("STILL WORKING: waiting on my background run. Report: r.md", True),
        ("STILL WORKING: waiting on the verifier background session. Report: r.md", False),
        ("STILL WORKING: waiting on the verifier and my background gate. Report: r.md", False),
        ("STILL WORKING: waiting on the agent and my gate. Report: r.md", False),
        ("STILL WORKING: waiting on my agent and gate. Report: r.md", False),
        ("STILL WORKING: waiting on my verifier and gate. Report: r.md", False),
        ("STILL WORKING: waiting on my tests and gate. Report: r.md", False),
        ("STILL WORKING: waiting on my deployment and gate. Report: r.md", False),
        ("STILL WORKING: waiting on my gate, the verifier. Report: r.md", False),
        ("STILL WORKING: waiting on my gate (the verifier). Report: r.md", False),
        ("STILL WORKING: waiting on my tests and my gate. Report: r.md", False),
        ("STILL WORKING: waiting on the deployment and my gate. Report: r.md", False),
        ("STILL WORKING: waiting on my own test run (14 min). Report: r.md", True),
        ("STILL WORKING: waiting on my own gate run (14 min). Report: r.md", True),
        ("STILL WORKING: waiting on my tests. Report: r.md", True),
        ("STILL WORKING: waiting on my background tests. Report: r.md", True),
        ("STILL WORKING: waiting on the background run. Report: r.md", True),
        ("STILL WORKING: waiting on the verifier. Report: r.md", False),
    ],
)
def test_only_a_wait_naming_nothing_but_its_own_run_counts_as_lost(ending: str, own: bool) -> None:
    assert omnigent_agent.own_run_only(ending) is own


def test_wait_for_prints_the_last_log_lines_with_still_running(tmp_path, capsys) -> None:
    run = subprocess.Popen(["sleep", "30"])  # a live run of the test's own, stopped below
    try:
        log, pid = tmp_path / "gate.log", tmp_path / "gate.pid"
        pid.write_text(f"{run.pid}\n")
        log.write_text(f"run {run.pid}\nlock x\nlocked\nstep 1\nstep 2\nstep 3\nstep 4\n")
        argv = ["wait-for", "--log", str(log), "--pid", str(pid), "--budget", "0.05"]
        assert omnigent_agent.main(argv) == omnigent_agent.LIMIT
        assert capsys.readouterr().out == "still running\nstep 2\nstep 3\nstep 4\n"
    finally:
        run.kill()
        run.wait()


def test_no_nudge_or_help_teaches_a_loop_of_short_waits() -> None:
    """Every text the tool sends or prints names one tracked wait as long as the run (waste audit, finding 1)."""
    from pathlib import Path

    texts = {
        name: getattr(omnigent_agent, name)
        for name in ("CLOUD_RUN_NUDGE", "LEAD_NUDGE_CLOUD", "LEAD_UNTRACKED", "CLOUD_RUN_LINE")
    }
    for name, text in texts.items():
        assert "looping on" not in text and "called again" not in text, name
        assert (
            "--budget <its expected length>" in text
            or "--budget <the run's expected length>" in text
        ), name
    usage = subprocess.run(
        ["python3", omnigent_agent.__file__, "wait-for", "--help"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "the run's expected length for a background call" in " ".join(usage.split())
    skill = " ".join((Path(omnigent_agent.__file__).parents[1] / "SKILL.md").read_text().split())
    assert (
        "pass the run's expected length when the harness tracks the call in the background" in skill
    )
