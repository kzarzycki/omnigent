"""omnigent_agent.py: a lead marked ``direct`` posts nothing to the notify session but a failed
turn that lost its runner, which the owner can't see in its session (#1163)."""

from pathlib import Path

import pytest
import test_omnigent_agent as base  # the faked server, the supervisor loop
from test_omnigent_agent import no_model, no_sleep  # noqa: F401  (the fixtures apply here by name)

omnigent_agent = base.omnigent_agent


def route(**event: object) -> str:
    return omnigent_agent.route({"direct": True, **event}, lambda: pytest.fail("no model"))[1]


def test_a_direct_lead_s_done_posts_nothing_and_tells_the_lead_waiting_on_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = base.follows(tmp_path, "a", "b", after={"b": "a"})
    assert omnigent_agent.mark_direct(registry, "a")
    og = base.Leads(
        a=[base.told("brief", 60)],
        b=[base.told("brief", 60), base.ending("STILL WORKING: waiting on lead a", 5)],
    )

    def done(poll: int) -> None:
        if poll == 1:
            og.histories["a"].append(base.ending("DONE: PR #7 merged", 0))

    base.supervised(og, registry, monkeypatch, 3, done, notify="cos", console=True)
    assert base.posted(og) == []
    told_b = "Supervisor: a t-a, the lead you wait on, ended: DONE: PR #7 merged"
    assert [(s, t) for s, t in og.inbox] == [("b", told_b)]


def test_a_direct_lead_s_prompt_and_plain_failure_are_only_recorded() -> None:
    assert route(kind="blocked") == "record"
    failed = "a t-a: failed: claude exited: too many open files\nlast message: the runner lost it"
    assert route(kind="failed", line=failed) == "record"  # the lead's own words name no failure
    assert route(kind="failed", line="a t-a: failed: HTTP 529 overloaded", transient=True) == (
        "record"
    )


def test_a_direct_lead_s_failure_that_lost_its_runner_is_still_routed() -> None:
    lost = "a t-a: failed: runner lost\nlast message: building"
    assert route(kind="failed", line=lost, transient=True) == "retry"
    assert route(kind="failed", line="a t-a: failed: exited 137", offline=True) == "revive"
    assert route(kind="failed", line="a t-a: failed: exited 137", offline=True, revived=True) == (
        "act"
    )


def test_a_lead_not_direct_still_posts_its_prompt_and_failure() -> None:
    assert route(kind="blocked", direct=False) == "owner"
    assert route(kind="failed", line="a t-a: failed: exited 1", direct=False) == "revive"
