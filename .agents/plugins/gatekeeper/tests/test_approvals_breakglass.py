"""approvals.py: the owner's break-glass record, its proof comment, and what `check merge` makes of a red required check
under it (#1722 sections 1 and 5). The run-data proof is test_approvals_breakglass_runs.py's."""

from __future__ import annotations

import contextlib
from typing import Any

import pytest
from approvals_harness import HEAD, add, approvals, loop_md, note, pull

OWNER = "octocat"
SHA = "c" * 40
LOOP = add(loop_md("loop:\n  owner: octocat\n"), "breakglass", "max_commits: 20\n")
RED = "PR #7: `qualify` is FAILURE on the head commit"


@pytest.fixture(autouse=True)
def unproved(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run-data proof is test_approvals_breakglass_runs.py's: here every item holds."""
    monkeypatch.setattr(approvals, "breakglass_verify", lambda *_a: approvals.Proved([], []))
    monkeypatch.setattr(approvals, "project", lambda **_k: (None, LOOP))
    monkeypatch.setattr(approvals, "policy_from", lambda _base: contextlib.nullcontext())


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A PR #7 whose comments grow as approvals.py posts them, as the login `as_login` names; a head the test moves."""
    state: dict[str, Any] = {"as": OWNER, "pull": pull(checks=(("qualify", "FAILURE"),))}

    def comment(_number: int, body: str) -> str:
        state["pull"]["comments"]["nodes"].append(note(body, author={"login": state["as"]}))
        return f"https://example.test/7#c{len(state['pull']['comments']['nodes'])}"

    monkeypatch.setattr(approvals, "pull_request", lambda _n: state["pull"])
    monkeypatch.setattr(approvals, "comment", comment)
    return state


def proven(state: dict[str, Any]) -> None:
    approvals.breakglass_proof(7, HEAD, 11, 12, None, [21, 22])


def test_the_owners_record_pins_the_head_the_proof_hash_and_the_expiry(
    posted: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    proven(posted)
    approvals.breakglass(7, HEAD, "suite is broken", "2999-01-01T00:00:00Z")
    [mark] = hub[HEAD]
    assert (mark["context"], mark["state"]) == ("breakglass", "success")
    assert mark["description"] == f"The owner's break-glass on {HEAD[:12]}"
    record = posted["pull"]["comments"]["nodes"][-1]["body"]
    assert record.startswith("Approved by the owner.\n")
    machine = approvals.machine(record)
    assert "Approved: breakglass\nBy: owner\nHead: " + HEAD in machine
    proof = posted["pull"]["comments"]["nodes"][0]
    assert f"Proof: {approvals.proof_hash(proof)}" in machine
    assert "Expires: 2999-01-01T00:00:00Z\nReason: suite is broken" in machine


def test_the_proof_comment_names_run_ids_only(posted: dict[str, Any]) -> None:
    proven(posted)
    body = approvals.machine(posted["pull"]["comments"]["nodes"][0]["body"])
    assert body.splitlines()[:5] == [
        "Break-glass proof",
        f"Head: {HEAD}",
        "Failed-run: 11",
        "Rerun: 12",
        "Earlier-runs: 21, 22",
    ]


def test_a_delegate_cannot_approve_break_glass_with_or_without_delegate_all(
    posted: dict[str, Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals, "one_repo", lambda: None)
    for argv in (
        ["approve", "breakglass", "7", "--head", HEAD, "--by", "delegate", "--reason", "x"],
        ["approve", "breakglass", "7", "--head", HEAD, "--by", "delegate", "--withdraw"],
    ):
        assert approvals.main(argv) == 1
        assert "the owner's alone" in capsys.readouterr().out
    assert posted["pull"]["comments"]["nodes"] == []


def test_approval_needs_a_proof_a_future_expiry_and_the_pulls_current_head(
    posted: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    with pytest.raises(approvals.Refused, match="has no `Break-glass proof` comment"):
        approvals.breakglass(7, HEAD, "why")
    proven(posted)
    with pytest.raises(approvals.Refused, match="no future ISO 8601 time"):
        approvals.breakglass(7, HEAD, "why", "2001-01-01T00:00:00Z")
    with pytest.raises(approvals.Refused, match="ask about the head it has now"):
        approvals.breakglass(7, SHA, "why")
    assert hub == {}


def standing(state: dict[str, Any], loop: str = LOOP) -> Any:
    return approvals.standing(state["pull"], loop)


def approved(state: dict[str, Any], hub: dict[str, list[dict[str, Any]]]) -> None:
    proven(state)
    approvals.breakglass(7, HEAD, "why", "2999-01-01T00:00:00Z")
    hub[HEAD][0]["creator"] = {"login": OWNER}


def test_a_current_record_is_the_owners_and_nothing_is_held_without_one(
    posted: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    assert standing(posted).held is False
    approved(posted, hub)
    assert standing(posted).why == []


def test_a_status_or_record_by_another_login_is_no_approval(
    posted: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    approved(posted, hub)
    hub[HEAD][0]["creator"] = {"login": "agent"}
    assert "no `breakglass` status on" in standing(posted).why[0]
    assert "(the one there is agent's)" in standing(posted).why[0]
    hub[HEAD][0]["creator"] = {"login": OWNER}
    posted["pull"]["comments"]["nodes"][-1]["author"] = {"login": "agent"}
    assert "is not the owner's (By: owner, comment by agent)" in standing(posted).why[0]


def test_a_record_that_says_delegate_is_no_approval(
    posted: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    approved(posted, hub)
    body = posted["pull"]["comments"]["nodes"][-1]
    body["body"] = body["body"].replace("By: owner", "By: delegate")
    assert "(By: delegate, comment by octocat)" in standing(posted).why[0]


def test_a_push_voids_the_record_since_it_names_the_old_head(
    posted: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    approved(posted, hub)
    posted["pull"]["headRefOid"] = SHA
    hub[SHA] = []
    why = standing(posted).why
    assert any(f"names {HEAD}, not the head {SHA}: a push voids it" in line for line in why)


def test_a_lapsed_record_and_a_withdrawn_one_hold(
    posted: dict[str, Any], hub: dict[str, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    approved(posted, hub)
    now = approvals.utcnow
    monkeypatch.setattr(approvals, "utcnow", lambda: approvals.parse_time("3000-01-01T00:00:00Z"))
    assert "lapsed at 2999-01-01T00:00:00Z" in standing(posted).why[0]
    monkeypatch.setattr(approvals, "utcnow", now)
    approvals.breakglass(7, HEAD, "", withdraw=True)
    hub[HEAD][0]["creator"] = {"login": OWNER}
    assert hub[HEAD][0]["state"] == "failure"
    assert "is withdrawn" in standing(posted).why[0]
    assert "Withdrawn: breakglass" in posted["pull"]["comments"]["nodes"][-1]["body"]


def test_a_proof_comment_edited_after_approval_voids_it(
    posted: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    approved(posted, hub)
    posted["pull"]["comments"]["nodes"][0]["body"] += "\nEarlier-runs: 99\n"
    assert "was edited after the owner approved it" in standing(posted).why[0]


def test_without_loop_owner_no_login_is_the_owner(
    posted: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    approved(posted, hub)
    assert "sets no `loop.owner`" in standing(posted, loop_md()).why[0]


def test_the_policy_names_the_windows_and_refuses_a_bad_age() -> None:
    got = approvals.breakglass_settings(add(LOOP, "breakglass", "max_age: 12h\nearlier_runs: 3\n"))
    assert (got["max_age"].total_seconds(), got["earlier_runs"], got["smoke"]) == (
        12 * 3600,
        3,
        "check-land",
    )
    with pytest.raises(approvals.Refused, match="write it as `7d` or `12h`"):
        approvals.breakglass_settings(add(LOOP, "breakglass", "max_age: week\n"))


def cover(state: dict[str, Any], *lines: str) -> Any:
    return approvals.breakglass_cover(state["pull"], LOOP, list(lines))


def test_check_merge_with_no_record_is_unchanged(posted: dict[str, Any]) -> None:
    assert cover(posted, RED) == ([RED], [])


def test_a_current_record_clears_a_red_or_missing_required_check_and_says_so(
    posted: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    approved(posted, hub)
    assert cover(posted, RED) == (
        [],
        [f"PR #7: required check `qualify` red; break-glass approved by the owner on {HEAD}"],
    )
    missing = "PR #7: no `qualify` on the head commit yet"
    assert cover(posted, missing).said[0].startswith("PR #7: required check `qualify` missing;")


def test_a_pending_check_is_no_red_one_so_it_stays_a_wait(
    posted: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    approved(posted, hub)
    pending = "PR #7: `qualify` is IN_PROGRESS on the head commit"
    assert cover(posted, RED, pending) == ([RED, pending], [])


def test_a_refused_record_keeps_the_red_line_and_names_the_gap(
    posted: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    approved(posted, hub)
    hub[HEAD][0]["creator"] = {"login": "agent"}
    got = cover(posted, RED)
    assert got.lines[0] == RED and "no `breakglass` status" in got.lines[1] and got.said == []


def test_waits_leave_only_what_the_cover_leaves(
    posted: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    """check merge reads the cover's lines in place of ran()'s, so a covered check is no wait."""
    approved(posted, hub)
    left = cover(posted, RED).lines
    assert approvals.waits(posted["pull"], [], LOOP, checks=left) == approvals.owner_waits(
        posted["pull"], [], LOOP
    )


NO_CI = add(LOOP, "loop", "ci: false\n")
RED_CHECK = "PR #7: `check` is FAILURE on the head commit"


def test_under_ci_false_a_break_glass_covers_no_red_required_check(
    posted: dict[str, Any], hub: dict[str, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1872 changes no break-glass rule: under `ci: false`, on a base requiring `check` that is red on the head, a
    current owner's break-glass clears nothing, so `check merge` (ran, the cover, then waits) and `land` (landable)
    both still hold the PR; without `ci: false` the same record covers it."""
    monkeypatch.setattr(approvals, "required_checks", lambda _base: {"check"})
    posted["pull"] = pull(checks=(("check", "FAILURE"),))
    approved(posted, hub)
    pr = posted["pull"]
    assert approvals.ran(pr) == [RED_CHECK]
    assert approvals.breakglass_cover(pr, LOOP, [RED_CHECK]).lines == []  # the record holds
    covered = approvals.breakglass_cover(pr, NO_CI, approvals.ran(pr))
    assert covered == ([RED_CHECK], [])
    assert RED_CHECK in approvals.waits(pr, [], NO_CI, checks=covered.lines)
    monkeypatch.setattr(approvals, "proof_gaps", lambda *_a: ([], []))
    monkeypatch.setattr(approvals, "flagged_as", lambda p, _i, _l: p)
    monkeypatch.setattr(
        approvals, "breakglass_held", lambda *_a: pytest.fail("land took the break-glass")
    )
    assert approvals.landable(pr, None, NO_CI, {"check"}, named=True).exit == (1, False)
