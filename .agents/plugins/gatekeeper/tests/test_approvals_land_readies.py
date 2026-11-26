"""approvals.py: `land` readies each named draft before it waits for the lock, and takes several PRs (#969)."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from typing import Any

import pytest
from approvals_harness import (
    approvals,
    git_in,
    lands,  # noqa: F401
    note,
    record,
    status,
)

REF = approvals.LOCK.format("main")


def lock(lands: dict[str, Any], title: str) -> None:
    """Another land's lock commit (`held` or `free`), taken ten minutes ago, pushed onto the lock branch's tip."""
    when = f"@{int(datetime.now(UTC).timestamp()) - 600} +0000"
    tip = git_in(lands["origin"], "for-each-ref", "--format=%(objectname)", REF).strip()
    tree = git_in(lands["work"], "hash-object", "-t", "tree", "-w", os.devnull).strip()
    commit = subprocess.run(
        [
            "git",
            "commit-tree",
            *(["-p", tip] if tip else []),
            "-m",
            f"{title} [skip ci]\n\npid 1, nonce {len(tip)}{title[-4:] == 'free'}",
            tree,
        ],
        cwd=lands["work"],
        env={**os.environ, "GIT_COMMITTER_DATE": when},
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    git_in(lands["work"], "push", "-q", "origin", f"{commit}:{REF}")


@pytest.fixture
def one_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--wait` ends after its first try, so a test reads what a land did before its first wait."""
    monkeypatch.setattr(approvals, "POLL", approvals.CEILING + 1)


def draft(lands: dict[str, Any], number: int, **fields: Any) -> None:
    lands["open"](number, {f"{number}.txt": f"{number}\n"}, **fields)
    lands["pulls"][number]["isDraft"] = True


def readied(lands: dict[str, Any]) -> list[str]:
    return [call[-1] for call in lands["calls"] if call[:2] == ("pr", "ready")]


def test_a_draft_is_readied_before_the_first_wait_for_the_lock(
    lands: dict[str, Any], one_wait: None
) -> None:
    draft(lands, 7)
    lock(lands, approvals.HELD)
    code, lines = lands["land"]("7", "--wait")
    assert code == approvals.WAITING
    assert any(line.startswith("land --wait stopped after") for line in lines), (
        lines
    )  # it kept waiting
    assert (readied(lands), lands["pulls"][7]["isDraft"], lands["merged"]()) == (["7"], False, [])


def test_a_draft_missing_its_proof_is_not_readied_while_it_waits_and_keeps_its_message(
    lands: dict[str, Any], one_wait: None
) -> None:
    draft(lands, 7, reviews=())
    lock(lands, approvals.HELD)
    assert lands["land"]("7", "--wait")[0] == approvals.WAITING
    assert readied(lands) == []
    lock(lands, approvals.FREE)
    code, lines = lands["land"]("7")
    assert (code, readied(lands), lands["merged"]()) == (1, [], [])
    assert lines and all("is ready now" not in line for line in lines), lines


def test_a_batch_started_while_a_draft_waits_lists_it_among_its_candidates(
    lands: dict[str, Any], one_wait: None
) -> None:
    draft(lands, 7)
    lock(lands, approvals.HELD)
    lands["land"]("7", "--wait")
    lock(lands, approvals.FREE)
    # the holder's next batch is a bare land: it never names #7
    assert lands["land"]()[0] == 0
    assert lands["merged"]() == ["7"]


def test_several_named_drafts_are_readied_and_land_in_one_batch(lands: dict[str, Any]) -> None:
    draft(lands, 7)
    draft(lands, 8)
    assert lands["land"]("7", "8")[0] == 0
    assert (readied(lands), lands["merged"](), lands["runs"]()) == (["7", "8"], ["7", "8"], 0)


def test_a_named_pr_missing_its_proof_is_left_out_with_its_reason_and_the_rest_land(
    lands: dict[str, Any],
) -> None:
    draft(lands, 7)
    draft(lands, 8, reviews=())
    code, lines = lands["land"]("7", "8")
    assert (code, readied(lands), lands["merged"]()) == (1, ["7"], ["7"])
    assert any(line.startswith("PR #8") for line in lines), lines


def test_only_limits_the_batch_to_the_named_prs(lands: dict[str, Any]) -> None:
    lands["open"](5, {"5.txt": "5\n"})
    draft(lands, 7)
    draft(lands, 8)
    assert lands["land"]("7", "8", "--only")[0] == 0
    assert lands["merged"]() == ["7", "8"]


def test_the_exit_is_the_worst_of_the_named_prs_one_over_three_over_zero(
    lands: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](8, {".github/ci.yml": "on: push\n"})
    lands["pulls"][8]["files"] = {"nodes": [{"path": ".github/ci.yml"}]}
    assert lands["land"]("7", "8")[0] == approvals.WAITING  # #8 waits for the owner, #7 lands
    assert lands["merged"]() == ["7"]
    lands["open"](9, {"9.txt": "9\n"}, reviews=())
    assert lands["land"]("8", "9")[0] == 1


def test_a_named_pr_of_another_base_or_closed_is_left_out_and_exits_one(
    lands: dict[str, Any],
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](8, {"8.txt": "8\n"})
    lands["pulls"][8]["state"] = "CLOSED"
    code, lines = lands["land"]("7", "8")
    assert (code, lands["merged"]()) == (1, ["7"])
    assert "PR #8 is closed: nothing to land" in lines


def test_a_cloud_session_readies_a_named_draft_as_a_host_does(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cloud land with no `signed_squash_to` merges over REST (#1712), so a draft is readied and landed as on a host."""
    monkeypatch.setattr(approvals, "in_cloud", lambda: True)
    draft(lands, 7)
    assert lands["land"]("7")[0] == 0
    assert (readied(lands), lands["merged"]()) == (["7"], ["7"])


def test_a_pr_named_twice_is_readied_once(lands: dict[str, Any]) -> None:
    draft(lands, 7)
    assert lands["land"]("7", "7")[0] == 0
    assert (readied(lands), lands["merged"]()) == (["7"], ["7"])


def test_a_named_pr_into_another_base_is_left_out_and_exits_one(lands: dict[str, Any]) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](8, {"8.txt": "8\n"})
    lands["pulls"][8]["baseRefName"] = "release"
    code, lines = lands["land"]("7", "8")
    assert (code, lands["merged"]()) == (1, ["7"])
    assert any(line.startswith("PR #8 targets release, not main like #7") for line in lines), lines


def test_a_readied_draft_whose_required_checks_have_just_started_keeps_the_wait_going(
    lands: dict[str, Any], one_wait: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals, "required_checks", lambda _base: {"check"})
    draft(lands, 7)
    code, lines = lands["land"]("7", "--wait")
    assert code == approvals.WAITING
    assert "PR #7: CI starts on ready, so its required checks are pending" in lines
    assert any(line.startswith("land --wait stopped after") for line in lines), lines


def high_risk_draft(lands: dict[str, Any]) -> None:
    lands["open"](7, {".github/ci.yml": "on: push\n"})
    lands["pulls"][7]["isDraft"] = True
    lands["pulls"][7]["files"] = {"nodes": [{"path": ".github/ci.yml"}]}


def go(lands: dict[str, Any], hub: dict[str, list[dict[str, Any]]], head: str) -> None:
    """The owner's go on `head`, as `approve merge` leaves it: the record on the PR, the status in `hub`."""
    lands["pulls"][7]["comments"]["nodes"].append(note(record("merge", "owner", head=head)))
    hub.setdefault(head, []).insert(0, status("owner", f"The owner's go on {head[:12]}"))


def test_a_high_risk_draft_with_no_go_is_not_readied_and_waits_for_the_owner(
    lands: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    high_risk_draft(lands)
    code, lines = lands["land"]("7")
    assert (code, readied(lands), lands["pulls"][7]["isDraft"]) == (approvals.WAITING, [], True)
    # said once: the pre-lock check's verdict is the batch's, not asked again
    assert sum(line.startswith("PR #7 waits for the owner's go on its head") for line in lines) == 1


def test_a_draft_with_a_go_on_an_earlier_head_is_not_readied(
    lands: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    high_risk_draft(lands)
    go(lands, hub, "c" * 40)
    assert lands["land"]("7")[0] == approvals.WAITING
    assert (readied(lands), lands["merged"]()) == ([], [])


def test_a_high_risk_draft_with_a_go_on_its_head_is_readied_and_lands(
    lands: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    high_risk_draft(lands)
    go(lands, hub, lands["pulls"][7]["headRefOid"])
    assert lands["land"]("7")[0] == 0
    assert (readied(lands), lands["merged"]()) == (["7"], ["7"])


def test_a_refused_pr_named_while_the_lock_is_held_exits_one_over_the_wait(
    lands: dict[str, Any],
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](8, {"8.txt": "8\n"})
    lands["pulls"][8]["state"] = "CLOSED"
    lock(lands, approvals.HELD)
    code, lines = lands["land"]("7", "8")
    assert code == 1
    assert "PR #8 is closed: nothing to land" in lines
    assert any(line.startswith("another land holds the landing lock") for line in lines), lines
    assert lands["merged"]() == []


def test_landable_on_a_draft_with_every_proof_writes_nothing_to_github(
    lands: dict[str, Any], hub: dict[str, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only probe must not ready a draft whose handover was left a draft, nor add `risk:high` (#969)."""
    high_risk_draft(lands)
    go(lands, hub, lands["pulls"][7]["headRefOid"])
    writes: list[Any] = []
    monkeypatch.setattr(approvals, "label", lambda *args, **kwargs: writes.append(args))
    tracker, loop = approvals.project()
    verdict = approvals.landable(lands["pulls"][7], tracker, loop, set())
    assert (verdict.exit, verdict.draft) == (None, True)
    assert (lands["calls"], writes, lands["comments"], lands["pulls"][7]["isDraft"]) == (
        [],
        [],
        [],
        True,
    )
    # the same draft through `land` is readied, and its risk flagged: the writes belong to land's own path
    assert lands["land"]("7")[0] == 0
    assert (readied(lands), writes) == (["7"], [(7, approvals.RISK_LABEL)])  # flagged once


def test_landable_says_what_a_draft_missing_a_proof_lacks_and_writes_nothing(
    lands: dict[str, Any],
) -> None:
    draft(lands, 7, reviews=())
    tracker, loop = approvals.project()
    verdict = approvals.landable(lands["pulls"][7], tracker, loop, set())
    assert (verdict.exit, verdict.draft, verdict.issues) == ((1, False), False, None)
    assert lands["calls"] == []


def test_land_flags_a_high_risk_pr_that_waits_for_the_owner_and_one_that_lands(
    lands: dict[str, Any], hub: dict[str, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The label write stays in land's own path: the board shows why a PR waits (the check itself writes nothing)."""
    writes: list[Any] = []
    monkeypatch.setattr(approvals, "label", lambda *args, **kwargs: writes.append(args))
    lands["open"](7, {".github/ci.yml": "on: push\n"})
    lands["pulls"][7]["files"] = {"nodes": [{"path": ".github/ci.yml"}]}
    assert lands["land"]("7")[0] == approvals.WAITING
    assert writes == [(7, approvals.RISK_LABEL)]
    go(lands, hub, lands["pulls"][7]["headRefOid"])
    lands["pulls"][7]["labels"]["nodes"] = []
    writes.clear()
    assert lands["land"]("7")[0] == 0
    assert writes == [(7, approvals.RISK_LABEL)]


def test_a_risk_label_the_gate_would_take_off_no_longer_holds_the_merge(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """land judges the PR as `unflag` leaves it, then takes the label off in its own path."""
    writes: list[Any] = []
    monkeypatch.setattr(approvals, "label", lambda *args, **kwargs: writes.append((*args, kwargs)))
    lands["open"](7, {"7.txt": "7\n"})
    pull = lands["pulls"][7]
    pull["labels"]["nodes"] = [{"name": approvals.RISK_LABEL}]
    pull["comments"]["nodes"].append(note(f"{approvals.FLAGGED}, since the verifier said high."))
    pull["timelineItems"]["nodes"] = [
        {"createdAt": "2026-01-01T00:00:00Z", "label": {"name": approvals.RISK_LABEL}}
    ]
    tracker, loop = approvals.project()
    verdict = approvals.landable(pull, tracker, loop, set())
    assert verdict.exit is None
    assert (writes, approvals.RISK_LABEL in {n["name"] for n in pull["labels"]["nodes"]}) == (
        [],
        True,
    )
    assert lands["land"]("7")[0] == 0
    assert writes == [(7, approvals.RISK_LABEL, {"add": False})]


def test_a_board_field_at_high_holds_a_draft_in_landable_and_land_writes_the_label(
    lands: dict[str, Any], board_risks: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """flag() treats a board `Risk` field at `High` as a known high risk (#929): landable judges the PR so, reading
    only, and the label write stays in land's own path, so the draft is neither readied nor left unflagged."""
    writes: list[Any] = []
    monkeypatch.setattr(approvals, "label", lambda *args, **kwargs: writes.append(args))
    draft(lands, 7)
    board_risks["values"][7] = "High"
    tracker, loop = approvals.project()
    verdict = approvals.landable(lands["pulls"][7], tracker, loop, set())
    assert (verdict.exit, verdict.draft, writes) == ((approvals.WAITING, False), False, [])
    assert [n["name"] for n in lands["pulls"][7]["labels"]["nodes"]] == []
    assert lands["land"]("7")[0] == approvals.WAITING
    assert (readied(lands), writes) == ([], [(7, approvals.RISK_LABEL)])


def test_a_held_lock_is_remembered_for_the_wait_to_poll_without_gh(
    lands: dict[str, Any], one_wait: None
) -> None:
    approvals.held_tip = None
    draft(lands, 7)
    lock(lands, approvals.HELD)
    lands["land"]("7")
    tip = git_in(lands["origin"], "for-each-ref", "--format=%(objectname)", REF).strip()
    assert approvals.held_tip == (REF, tip)
