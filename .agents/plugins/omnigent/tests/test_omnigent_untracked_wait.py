"""omnigent_agent.py supervise: a lead whose turn ends on a wait nothing wakes it for (a detached pid
or its log, a background subagent in a cloud session) is nudged with why, then escalated; a hand-back
to the manager inside a STILL WORKING goes to the session the supervisor notifies (#1753)."""

from pathlib import Path

import pytest
import test_omnigent_agent as base  # the faked server, the supervisor loop
from test_omnigent_agent import (  # noqa: F401  (the fixtures apply here by name)
    no_model,
    no_sleep,
    this_repo,
    trusted_folders,
)

omnigent_agent = base.omnigent_agent

OBSERVED = "STILL WORKING: waiting on pid 4242 (the build, started with nohup)"
SUBAGENT = "STILL WORKING: waiting on the background subagent reviewing the diff"
HANDED = "STILL WORKING: #1755 READY, gate record is yours"
NUDGE = ("rule", "nudge", omnigent_agent.UNTRACKED_WHY)
ESCALATED = ("rule", "act", f"{omnigent_agent.UNTRACKED_WHY} again after its nudge")


def route(text: str, **event: object) -> tuple[str, str, str]:
    return omnigent_agent.route({"kind": "ended", "last": text, **event}, lambda: ("none", "fake"))


@pytest.mark.parametrize(
    "text",
    [
        OBSERVED,
        "STILL WORKING: waiting on tmp/loop/gate.log",
        "STILL WORKING: waiting on the build, pid 4242",
        "STILL WORKING: build started.\nWaiting on its pid file before the push.",
        "STILL WORKING: waiting on gate; its pid is 4242",
        "STILL WORKING: waiting on nohup build, output in build.out",
    ],
)
def test_a_wait_on_a_pid_or_a_log_is_nudged_once_then_escalated(text: str) -> None:
    assert route(text) == NUDGE
    assert route(text, nudged=True) == ESCALATED
    # the same words again after the nudge escalate rather than read as a repeat
    assert route(text, nudged=True, repeat=True)[1] == "act"


@pytest.mark.parametrize(
    ("text", "wait"),
    [
        ("STILL WORKING: waiting on pid 4242, started with LOOP_NOTIFY set", "pid"),
        ("STILL WORKING: waiting on the gate (wait-for --notify on gate.log)", None),
        ("STILL WORKING: waiting on the verifier (its log is tmp/loop/v.log)", "verifier"),
        ("STILL WORKING: waiting on CI; gate.log was clean", "CI"),
        ("STILL WORKING: waiting on pid 4242\nWaits: CI", "CI"),
        ("STILL WORKING: waiting on deployment", "deployment"),
    ],
)
def test_a_wait_that_wakes_the_lead_by_itself_keeps_its_route(text: str, wait: str | None) -> None:
    assert not omnigent_agent.untracked_wait(text)
    if wait:
        assert route(text) == ("rule", "record", f"names its wait: {wait}")


def test_a_background_subagent_is_an_untracked_wait_only_in_a_cloud_session() -> None:
    assert route(SUBAGENT, cloud=True) == NUDGE
    assert route(SUBAGENT, cloud=True, nudged=True) == ESCALATED
    assert route("STILL WORKING: waiting on the verifier subagent", cloud=True) == NUDGE
    # on a host, a native subagent's end re-invokes the session: today's route
    assert route(SUBAGENT)[1] == "record"
    assert route(f"{SUBAGENT}\nWaits: #12", cloud=True)[1] == "record"


@pytest.mark.parametrize(
    ("text", "what"),
    [
        (HANDED, HANDED),
        ("STILL WORKING: verifier next.\nHandback: #1755 gate record", "#1755 gate record"),
        (
            "STILL WORKING: READY FOR VERIFY #1755 abc1234",
            "STILL WORKING: READY FOR VERIFY #1755 abc1234",
        ),
        (
            "STILL WORKING: #1755: the land is over to you",
            "STILL WORKING: #1755: the land is over to you",
        ),
    ],
)
def test_a_hand_back_inside_still_working_goes_to_the_manager(text: str, what: str) -> None:
    assert route(text) == ("rule", "act", f"hands back to its manager: {what}")
    assert route(text, repeat=True) == ("rule", "record", "an exact repeat of its hand-back")


@pytest.mark.parametrize(
    "text",
    [
        "STILL WORKING: waiting on CI for #12",
        "STILL WORKING: the lead reads its own report; ready soon",
        "STILL WORKING: waiting on the gate; the choice is yours",
        "STILL WORKING: hand it over to you once CI is in",
        "DONE: #12 READY",
    ],
)
def test_no_hand_back_without_its_words(text: str) -> None:
    assert not route(text)[2].startswith("hands back")


def run(og: base.Lead, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    registry = base.following(tmp_path)
    offset = base.skewed(monkeypatch)
    base.supervised(
        og,
        registry,
        monkeypatch,
        2,
        lambda p: offset.__setitem__(0, 301.0),
        notify="cos",
        console=True,
    )
    return registry


def test_the_supervisor_sends_the_untracked_wait_nudge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = base.Lead(base.told("brief", 60), base.ending(OBSERVED, 0))
    registry = run(og, tmp_path, monkeypatch)
    assert [t for s, t in og.inbox if s == "a"] == [omnigent_agent.LEAD_UNTRACKED]
    assert base.events_of(registry)[-1]["action"] == "nudge"
    # the lead fixes its own wait: nothing goes to the chief of staff yet
    assert base.posted(og) == []


def test_the_supervisor_nudges_a_cloud_lead_waiting_on_its_subagent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = base.Lead(base.told("brief", 60), base.ending(SUBAGENT, 0))
    og.snap = dict(base.CLOUD_RECORD)
    og.harnesses = {"claude-cloud": omnigent_agent.CLOUD_HARNESS}
    registry = run(og, tmp_path, monkeypatch)
    assert [t for s, t in og.inbox if s == "a"] == [omnigent_agent.LEAD_UNTRACKED]
    assert base.events_of(registry)[-1]["action"] == "nudge"


def test_the_supervisor_reports_a_hand_back_to_the_session_it_notifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = base.Lead(base.told("brief", 60), base.ending(HANDED, 0))
    registry = run(og, tmp_path, monkeypatch)
    assert [t for s, t in og.inbox if s == "a"] == []  # the lead is not nudged
    assert base.events_of(registry)[-1]["action"] == "act"
    assert any("#1755 READY" in t for t in base.posted(og))
