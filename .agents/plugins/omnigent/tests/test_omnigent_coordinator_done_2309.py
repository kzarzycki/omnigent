"""A followed coordinator (a manager with its own leads registry) whose turn closes on a bare ``DONE`` reports no
result: it wakes no one but the leads waiting on it (#2309). A lead's ``DONE: <result>`` and a ``USER
NEEDED`` route as before."""

from test_omnigent_agent import omnigent_agent as oa

NOTHING = """Nothing to do on [#667](https://github.com/example/repo/pull/667). Someone put `loop:hold` on it, and the queue drops any PR with that label, using the rule that came in with [#789](https://github.com/example/repo/pull/789).

It isn't my PR. Its branch `loop/gh-quota-665` belongs to the GitHub API quota work on [#665](https://github.com/example/repo/issues/665), which no lead in my registry runs. Whoever added the hold also owns removing it and running `land 667` again.

DONE"""


def routed(text: str, **event: object) -> tuple[str, str, str]:
    return oa.route({"kind": "ended", "last": text, **event}, lambda: ("none", ""))


def test_a_coordinators_bare_done_is_told_only_to_its_waiters() -> None:
    assert routed(NOTHING, coordinator=True)[1:] == (
        "done",
        "a coordinator's DONE with no result on its line: told only to its waiters",
    )


def test_a_bare_done_with_only_a_report_path_has_no_result() -> None:
    assert "waiters" in routed("DONE. Report: /tmp/r.md", coordinator=True)[2]


def test_a_coordinators_done_with_a_result_routes_done() -> None:
    assert routed("DONE: PR #5 merged as abc1234", coordinator=True)[2].startswith("done: its")


def test_a_leads_done_with_a_result_still_routes_done() -> None:
    assert routed("DONE: PR #5 merged as abc1234")[1] == "done"


def test_user_needed_after_done_still_goes_to_the_owner() -> None:
    text = NOTHING + "\n\nUSER NEEDED: which branch?"
    assert routed(text, coordinator=True)[1] == "act"
