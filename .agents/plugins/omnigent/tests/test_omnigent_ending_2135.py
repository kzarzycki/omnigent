"""A manager's status block (a ``STILL WORKING`` block with a ``DONE:`` section) is still working, not a DONE report (#2135):
``ending_of`` takes the first line opening with an ending word, ``USER NEEDED`` anywhere winning, and ``Waits: own
supervisor`` is the footer for a manager whose own supervisor wakes it."""

import pytest
from test_omnigent_agent import omnigent_agent as oa

BLOCK = """STILL WORKING:
- lead A: building the fix for #N.

No USER NEEDED items.

DONE:
- nudged lead A.

Waits: supervisor"""
OWN = BLOCK.replace("Waits: supervisor", "Waits: own supervisor")


def routed(text: str) -> tuple[str, str, str]:
    return oa.route({"kind": "ended", "last": text}, lambda: ("none", ""))


def test_the_issue_block_is_no_done_report() -> None:
    assert oa.ending_of(BLOCK) is None


def test_a_status_block_waiting_on_its_own_supervisor_routes_record() -> None:
    assert routed(OWN)[1] == "record"


def test_the_issue_block_with_the_old_footer_is_no_delivered_done() -> None:
    assert routed(BLOCK)[1] not in ("done",)


def test_a_first_line_done_still_routes_done() -> None:
    text = "DONE: landed the fix. Report: r.md\n\nSTILL WORKING: on nothing"
    assert oa.ending_of(text) == "DONE"
    assert routed(text)[1] == "done"


def test_user_needed_after_done_still_routes_as_a_user_needed() -> None:
    text = "DONE: built it.\n\nUSER NEEDED: which branch?"
    assert oa.ending_of(text) == "USER NEEDED"
    assert routed(text)[1] == "act"  # no card: its manager's (#2170)


def test_user_needed_after_a_still_working_block_wins() -> None:
    assert oa.ending_of("STILL WORKING: a\n\nUSER NEEDED: pick one") == "USER NEEDED"


def test_parked_first_and_a_plain_still_working_are_unchanged() -> None:
    assert oa.ending_of("PARKED: no capacity") == "PARKED"
    assert oa.ending_of("STILL WORKING: waiting on the verifier") is None


def test_own_supervisor_parses_and_wait_on_prints_it() -> None:
    assert oa.wait_line(["own"]) == "Waits: own supervisor"
    with pytest.raises(oa.Fail):
        oa.wait_line(["own", "x"])
    assert not oa.waits_on(OWN, {}, "cos")
    assert oa.waits_on(BLOCK, {}, "cos")
