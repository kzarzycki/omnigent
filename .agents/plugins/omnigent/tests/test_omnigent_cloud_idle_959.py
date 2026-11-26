"""A cloud child idle on its own background run is working, not failed (#959).

Omnigent's record shows none of a cloud session's tasks, and a turn the run wakes never reaches the host,
so ``wait`` reads the run from the history and checks in instead of nudging and failing. The seam: the
fake Omnigent, whose cloud session answers each message with the next scripted turn."""

import json
from pathlib import Path

import pytest
from test_omnigent_agent import (  # noqa: F401 (no_sleep: autouse)
    CloudFake,
    no_sleep,
    omnigent_agent,
)

CHECK_IN = "Host check-in on your background run"


def launch(task: str) -> list[dict]:
    """A Bash ``run_in_background`` call and Claude Code's reply to it, as a cloud session's history holds them."""
    args = {"command": "pytest -q", "run_in_background": True}
    return [
        {
            "type": "function_call",
            "name": "Bash",
            "arguments": json.dumps(args),
            "call_id": f"c-{task}",
        },
        {
            "type": "function_call_output",
            "call_id": f"c-{task}",
            "output": f"Command running in background with ID: {task}. Output is being written to:"
            f" /tmp/tasks/{task}.output\nYou will be notified when it completes.",
        },
    ]


def says(text: str) -> list[dict]:
    return [
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text}],
        }
    ]


class Scripted(CloudFake):
    """A cloud session that answers each message (the brief first) with the next of ``turns``."""

    def __init__(self, turns: list[list[dict]]) -> None:
        super().__init__()
        self.turns = list(turns)
        self.got: list[str] = []

    def land(self, sid: str, text: str) -> None:
        super().land(sid, text)
        self.got.append(text)
        if self.turns:
            self.sessions[sid].extend(self.turns.pop(0))


@pytest.fixture(autouse=True)
def check_in_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(omnigent_agent, "CLOUD_CHECK_IN_SECONDS", 0)
    monkeypatch.delenv("LOOP_DENY_LIST", raising=False)


def started(og: Scripted, report: Path) -> str:
    sid, _ = omnigent_agent.start(og, "claude-cloud", "t", "do X", report, None, 0)
    return sid


def test_idle_on_own_run_is_checked_in_not_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    og = Scripted(
        [
            launch("b0azqqqi9") + says("STILL WORKING: waiting on b0azqqqi9"),
            says("REPORT\nGates: 0"),
        ]
    )
    report = tmp_path / "r.md"
    sid = started(og, report)
    assert omnigent_agent.wait(og, sid, report) == str(report)
    assert report.read_text() == "Gates: 0\n"
    checks = og.got[1:]
    assert len(checks) == 1 and checks[0].startswith(CHECK_IN) and "b0azqqqi9" in checks[0]
    assert omnigent_agent.CLOUD_NUDGE not in og.got
    err = capsys.readouterr().err
    assert (
        f"session {sid} is idle on its own background run b0azqqqi9: waiting for its report" in err
    )
    assert "nudging" not in err


def test_still_waiting_after_check_in_stays_pending(tmp_path: Path) -> None:
    og = Scripted(
        [
            launch("b0azqqqi9") + says("Started the suite; it runs about 20 minutes."),
            says("STILL WORKING: waiting on b0azqqqi9"),
            says("REPORT\nGates: 0"),
        ]
    )
    report = tmp_path / "r.md"
    assert omnigent_agent.wait(og, started(og, report), report) == str(report)
    assert [m.startswith(CHECK_IN) for m in og.got[1:]] == [True, True]


def test_pending_run_fails_at_timeout_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(omnigent_agent, "CLOUD_CHECK_IN_SECONDS", 3600)
    og = Scripted([launch("b1x2y3z4w") + says("STILL WORKING: waiting on b1x2y3z4w")])
    report = tmp_path / "r.md"
    sid = started(og, report)
    with pytest.raises(
        omnigent_agent.Fail,
        match=r"no report after 0s, still idle on its own background run b1x2y3z4w",
    ):
        omnigent_agent.wait(og, sid, report, timeout=0.2)
    assert og.got[1:] == [] and not report.exists()


def test_pr_worker_idle_on_own_run_reports_by_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = "https://github.com/o/r/pull/7#issuecomment-1"
    monkeypatch.setattr(
        omnigent_agent,
        "pr_report",
        lambda said, pr, since=None: "from the PR" if link in said else None,
    )
    og = Scripted(
        [launch("b0azqqqi9") + says("STILL WORKING: waiting on b0azqqqi9"), says(f"Posted: {link}")]
    )
    report = tmp_path / "r.md"
    sid = started(og, report)  # a --pr brief resolves the PR's repository on GitHub
    assert omnigent_agent.wait(og, sid, report, pr=7) == str(report)
    assert report.read_text() == "from the PR"
    assert len(og.got) == 2 and og.got[1].startswith(CHECK_IN)


def test_idle_without_own_run_is_nudged_then_fails(tmp_path: Path) -> None:
    og = Scripted([says("Looking into the failing test."), says("Still on it.")])
    report = tmp_path / "r.md"
    sid = started(og, report)
    with pytest.raises(omnigent_agent.Fail, match=r"ended \(idle\) without"):
        omnigent_agent.wait(og, sid, report)
    assert og.got[1:] == [omnigent_agent.CLOUD_NUDGE]


def test_a_run_from_an_earlier_turn_left_unnamed_is_not_pending() -> None:
    """Only the last turn's launches, or one its last message names, are waited on: an old run is done with."""
    old = launch("b0azqqqi9") + says("REPORT\nGates: 0")
    brief = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "follow-up"}],
        }
    ]
    assert omnigent_agent.pending_runs(old + brief + says("Fixing the lint.")) == []
    assert omnigent_agent.pending_runs(
        old + brief + says("STILL WORKING: waiting on b0azqqqi9")
    ) == ["b0azqqqi9"]


def test_a_lead_s_still_working_on_another_session_is_still_its_report(tmp_path: Path) -> None:
    """A lead's ending line is its report unless it names its own pending run."""
    ending = "STILL WORKING: waiting on verifier 8013eac25f3a. Report: r.md"
    og = Scripted([launch("b0azqqqi9") + says(ending)])
    report = tmp_path / "r.md"
    assert omnigent_agent.wait(og, started(og, report), report) == str(report)
    assert report.read_text() == ending + "\n" and og.got[1:] == []


def test_a_report_sharing_the_bridge_s_opening_message_is_taken(tmp_path: Path) -> None:
    """The bridge opens a turn with ``cloud session: <url>``; a reply that is the turn's first output shares its message."""
    bridge = "cloud session: https://claude.ai/code/session_013ycVZR1fUWbbh7wt1VWhs8\n\n"
    og = Scripted(
        [
            launch("b72l6dhdy") + says(bridge + "STILL WORKING: waiting on b72l6dhdy"),
            says(bridge + "REPORT\nEXIT: 0"),
        ]
    )
    report = tmp_path / "r.md"
    assert omnigent_agent.wait(og, started(og, report), report) == str(report)
    assert report.read_text() == "EXIT: 0\n" and len(og.got) == 2
