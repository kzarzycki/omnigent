"""approvals.py: `approve spec|plan <n> --by delegate` is the delegated approver's record under `loop.delegate: all`,
which the gate counts in place of the owner's while the PR touches no `risk.critical_paths` glob (#1805)."""

from __future__ import annotations

from typing import Any

import pytest
from approvals_harness import (
    AS_WRITTEN,
    BARE,
    HEAD,
    TEXTED,
    add,
    approvals,
    github,  # noqa: F401  the fixture, by name
    issue,
    note,
    pull,
    record,
    specced,
)

ALL = add(BARE, "loop", "delegate: all\n")
CRITICAL = add(ALL, "risk", "critical_paths:\n  - src/orders/**\n")
DELEGATED = "Approved under the owner's delegation.\n"


def on_main(monkeypatch: pytest.MonkeyPatch, loop: str) -> None:
    """The policy on origin's default branch, the one `approve` reads, is `loop`."""
    monkeypatch.setattr(approvals, "project", lambda **_kw: (None, loop))


def delegates(*labels: str, point: str = "spec") -> dict[str, Any]:
    """An issue whose spec carries the delegate's current record and the `approved:spec` label."""
    return issue(
        1,
        "size:S",
        f"approved:{point}",
        *labels,
        comments=(DELEGATED + record(point, "delegate", **{point: AS_WRITTEN}),),
    )


def test_under_delegate_all_the_delegate_records_a_spec_approval_under_the_owners_delegation(
    github: list[tuple[Any, ...]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    on_main(monkeypatch, ALL)
    monkeypatch.setattr(approvals, "issue_node", lambda _n: specced(1, "kind:bug", "needs-owner"))
    assert approvals.main(["approve", "spec", "1", "--by", "delegate"]) == 0
    assert github == [
        ("comment", 1, f"{DELEGATED}Approved: spec\nBy: delegate\nSpec: {TEXTED}\n"),
        ("label", 1, "approved:spec", True),
        ("label", 1, "needs-owner", False),
    ]
    assert "under the owner's delegation" in capsys.readouterr().out
    # the record reads back as the delegate's, never the owner's
    body = github[0][2]
    assert approvals.parsed(body, "spec") == {"by": "delegate", "spec": TEXTED}


def test_the_delegate_records_a_plan_the_same_way(
    github: list[tuple[Any, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    on_main(monkeypatch, ALL)
    planned = specced(1)
    planned["comments"]["nodes"] = [note("## Plan\n\n1. slice")]
    monkeypatch.setattr(approvals, "issue_node", lambda _n: planned)
    assert approvals.main(["approve", "plan", "1", "--by", "delegate"]) == 0
    assert github[0][2].startswith(f"{DELEGATED}Approved: plan\nBy: delegate\nPlan: sha256 ")


def test_without_delegate_all_the_delegates_spec_approval_is_refused_and_posts_nothing(
    github: list[tuple[Any, ...]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    on_main(monkeypatch, BARE)
    for point in ("spec", "plan"):
        assert approvals.main(["approve", point, "1", "--by", "delegate"]) == 1
        out = capsys.readouterr().out
        assert f"approve {point} --by delegate needs `loop.delegate: all`" in out
    assert github == []


def test_the_gate_counts_the_delegates_record_only_under_delegate_all() -> None:
    assert approvals.proofs("build", pull(), [delegates()], ALL) == []
    assert not any("spec" in line for line in approvals.proofs("merge", pull(), [delegates()], ALL))
    assert approvals.proofs("build", pull(), [delegates()], BARE) == [
        (
            "#1: the delegate's `Approved: spec` record counts for nothing here, since loop.md sets no"
            " `loop.delegate: all`: the owner approves it"
        ),
        "#1 has no `Approved: spec` record by the owner",
    ]


def test_a_critical_path_keeps_the_spec_the_owners_whatever_the_delegate_recorded() -> None:
    orders = pull(files=("src/orders/place.py",))
    found = approvals.proofs("build", orders, [delegates()], CRITICAL)
    assert len(found) == 2 and "`src/orders/place.py`" in found[0], found
    assert found[0].startswith("#1: the delegate's `Approved: spec` record counts for nothing here")
    assert approvals.proofs("build", pull(files=("README.md",)), [delegates()], CRITICAL) == []
    # the owner's own record still covers it
    owners = issue(
        1, "size:S", "approved:spec", comments=(record("spec", "owner", spec=AS_WRITTEN),)
    )
    assert approvals.proofs("build", orders, [owners], CRITICAL) == []


def test_breakglass_stays_the_owners_alone(
    github: list[tuple[Any, ...]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    on_main(monkeypatch, ALL)
    argv = ["approve", "breakglass", "7", "--head", "a" * 40, "--reason", "r", "--by", "delegate"]
    assert approvals.main(argv) == 1
    assert "approve breakglass is the owner's alone" in capsys.readouterr().out
    assert github == []


def test_a_delegated_spec_leaves_no_owner_hold_on_the_gate() -> None:
    """The issue as the delegate's approve leaves it: its record, `approved:spec`, and no `needs-owner`, so neither
    the proofs nor the label problems hold `check build` or `check merge` for the owner (#1808, D1)."""
    approved = delegates()
    assert (
        approvals.problems([approved], None) + approvals.proofs("build", pull(), [approved], ALL)
        == []
    )
    held = delegates("needs-owner")  # what the issue would be had the approve left the hold
    assert approvals.problems([held], None) == ["#1 waits for the owner (needs-owner)"]


OFF = add(
    ALL, "loop", "delegate_spec: false\n"
)  # delegate: all for the merge go, specs and plans the owner's


def test_delegate_spec_false_refuses_the_delegates_spec_and_plan_approval_and_posts_nothing(
    github: list[tuple[Any, ...]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    on_main(monkeypatch, OFF)
    monkeypatch.setattr(approvals, "issue_node", lambda _n: specced(1, "kind:bug", "needs-owner"))
    for point in ("spec", "plan"):
        assert approvals.main(["approve", point, "1", "--by", "delegate"]) == 1
        out = capsys.readouterr().out
        assert f"approve {point} --by delegate is refused" in out
        assert "`loop.delegate_spec: false`" in out
    assert github == []
    # the owner's own approval still records
    assert approvals.main(["approve", "spec", "1", "--by", "owner"]) == 0
    assert github[0][2].startswith("Approved: spec")


def test_delegate_spec_false_keeps_the_delegates_record_out_of_the_gate_with_one_line_why() -> None:
    for gate in ("build", "merge"):
        found = approvals.proofs(gate, pull(), [delegates()], OFF)
        why = [line for line in found if "counts for nothing" in line]
        assert why == [
            (
                "#1: the delegate's `Approved: spec` record counts for nothing here, since loop.md sets"
                " `loop.delegate_spec: false`, which keeps specs and plans the owner's: the owner approves it"
            )
        ], found
        assert "#1 has no `Approved: spec` record by the owner" in found
    # the owner's record still covers it
    owners = issue(
        1, "size:S", "approved:spec", comments=(record("spec", "owner", spec=AS_WRITTEN),)
    )
    assert approvals.proofs("build", pull(), [owners], OFF) == []


def test_delegate_spec_true_or_unset_keeps_the_delegates_record_counting() -> None:
    on = add(ALL, "loop", "delegate_spec: true\n")
    assert approvals.proofs("build", pull(), [delegates()], on) == []
    assert approvals.delegate_spec(ALL) is True and approvals.delegate_spec(OFF) is False


def test_delegate_spec_false_leaves_the_delegates_merge_go_counting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = "a" * 40
    status = {
        "context": "owner",
        "state": "success",
        "description": f"go on {head[:12]}{approvals.DELEGATED}",
        "creator": {"login": "bot"},
    }
    monkeypatch.setattr(approvals, "statuses", lambda _head: [status])
    monkeypatch.setattr(approvals, "viewer", lambda: "bot")
    monkeypatch.setattr(approvals, "current", lambda *_a: True)
    assert approvals.delegation(OFF) == "all"
    assert approvals.go_on(pull(), head, OFF) == "delegate"


def test_delegate_spec_false_leaves_approve_merge_by_delegate_giving_the_go(
    github: list[tuple[Any, ...]],
    hub: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    on_main(monkeypatch, OFF)
    monkeypatch.setattr(approvals, "pull_request", lambda _n: pull())
    assert approvals.default(
        pull()
    )  # into the default branch, where only `delegate: all` lets the go count
    assert approvals.main(["approve", "merge", "7", "--head", HEAD, "--by", "delegate"]) == 0
    assert [(s["context"], s["description"]) for s in hub[HEAD]] == [
        ("owner", f"The delegate's go on {HEAD[:12]}{approvals.DELEGATED}")
    ]
    assert github[0][2].endswith(f"Approved: merge\nBy: delegate\nHead: {HEAD}\n")
