"""approvals.py: a migration row's spec is credited to its approved parent story (#2266). A row's sub-issue whose body
opens with `Part of #<parent> (row <name>)` needs no approval of its own while its parent's spec is approved and
current, its author can write, and the PR is no high-risk change; `check` names the parent and the row, or the missing
piece."""

from __future__ import annotations

from typing import Any

import pytest
from approvals_harness import (  # writers: the fixture the cases run under
    AS_WRITTEN,
    approvals,
    issue,
    loop_md,
    pull,
    record,
    writers,  # noqa: F401
)

# a project whose `spec.auto` names only bugs, so a row's sub-issue is the owner's to approve but for the row rule
POLICY = loop_md("spec:\n  auto:\n    - bug\nrisk:\n  paths:\n    - billing/**\n")
EDITED = "2026-10-02T14:35:27Z"
ROW = 'Part of #9 (row "pack pin")\n\nReplace the pack pin, as the parent\'s table says.\n'


def story(**fields: Any) -> dict[str, Any]:
    """The parent story, #9, with the owner's record of its first version and `approved:spec`."""
    found = issue(
        9, "kind:enhancement", "approved:spec", comments=(record("spec", "owner", spec=AS_WRITTEN),)
    )
    return {**found, **fields}


def child(body: str = ROW, parent: dict[str, Any] | None = None, *labels: str) -> dict[str, Any]:
    """Row #1 of a parent story, opened by `worker`, who can write, with no approval of its own."""
    found = issue(1, "kind:chore", "area:web", "size:S", *labels, body=body)
    return {**found, "parent": parent, "author": {"login": "worker"}}


OWNER = ["#1 has no `Approved: spec` record by the owner", "#1 lacks the `approved:spec` label"]


@pytest.mark.parametrize(
    ("spec", "files", "why"),
    [
        (child(parent=story()), ("app.py",), None),
        (child(), ("app.py",), "it is no sub-issue of #9"),
        (
            child(ROW.replace("#9", "#8"), story()),
            ("app.py",),
            "its row line names #8, but its parent is #9",
        ),
        (
            child(parent=story(lastEditedAt=EDITED)),
            ("app.py",),
            "its parent #9 has no `Approved: spec` record by the owner for its current spec: the owner approves it again",
        ),
        (child(parent=story()), ("billing/a.py",), "it matches a risk rule (path `billing/**`)"),
        (child(ROW, story(), "risk:high"), ("app.py",), "`risk:high` is on #1"),
        (
            {**child(parent=story()), "author": {"login": "drive-by"}},
            ("app.py",),
            "its author @drive-by can't write to the repo",
        ),
    ],
    ids=[
        "covered",
        "no-sub-issue",
        "names-another-issue",
        "stale-parent",
        "risk-path",
        "risk-label",
        "non-writer",
    ],
)
def test_a_row_is_covered_by_its_approved_parent_and_names_what_is_missing(
    writers: list[str], spec: dict[str, Any], files: tuple[str, ...], why: str | None
) -> None:
    pr = pull(files=files)
    found = approvals.proofs("build", pr, [spec], POLICY)
    said = approvals.holds(pr, [spec], POLICY)
    if why is None:
        assert (found, said) == ([], ['#1: covered by #9\'s approved spec (row "pack pin")'])
    else:
        named = 8 if "names #8" in why else 9
        line = f'#1: its spec is the owner\'s to approve, though its body names row "pack pin" of #{named}, since {why}'
        assert (found, said) == ([line, *OWNER], [])


def test_a_sub_issue_without_the_row_line_is_not_credited(writers: list[str]) -> None:
    spec = child('Replace the pack pin.\n\nPart of #9 (row "pack pin")\n', story())
    assert approvals.proofs("build", pull(files=("app.py",)), [spec], POLICY) == OWNER
    assert approvals.holds(pull(files=("app.py",)), [spec], POLICY) == []


def test_a_row_spec_auto_already_covers_gets_no_credit_line(writers: list[str]) -> None:
    every = loop_md("spec:\n  auto: true\n")
    spec = child(parent=story())
    assert approvals.proofs("build", pull(files=("app.py",)), [spec], every) == []
    assert approvals.holds(pull(files=("app.py",)), [spec], every) == []


def test_a_row_touching_a_critical_path_is_the_owners(writers: list[str]) -> None:
    critical = loop_md("spec:\n  auto:\n    - bug\nrisk:\n  critical_paths:\n    - infra/**\n")
    spec, pr = child(parent=story()), pull(files=("infra/main.tf",))
    (line, *rest) = approvals.proofs("build", pr, [spec], critical)
    assert line.startswith(
        '#1: its spec is the owner\'s to approve, though its body names row "pack pin" of #9, since it touches'
        " `infra/main.tf`"
    )
    assert rest == OWNER


def test_check_build_credits_a_row_before_its_pr_exists(
    writers: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`check build --issue` on a row's sub-issue, before its PR, passes on the parent's approval and says so; a row
    under a stale parent fails and names the stale approval."""
    rows = {1: child(parent=story()), 2: {**child(parent=story(lastEditedAt=EDITED)), "number": 2}}
    monkeypatch.setattr(approvals, "project", lambda: (None, POLICY))
    monkeypatch.setattr(approvals, "one_repo", lambda: None)
    monkeypatch.setattr(approvals, "issue_node", lambda n: rows[n])
    monkeypatch.setattr(approvals, "current_pr", lambda: None)
    monkeypatch.setattr(
        approvals,
        "git",
        lambda *args: "app.py\0" if args[0] == "diff" else "origin/main\n",
    )

    def build(number: int) -> tuple[int, list[str]]:
        code = approvals.main(["check", "build", "--issue", str(number)])
        return code, capsys.readouterr().out.splitlines()

    assert build(1) == (
        0,
        [
            '#1: covered by #9\'s approved spec (row "pack pin")',
            "#1: every build proof holds; no PR yet",
        ],
    )
    code, said = build(2)
    assert code == 1 and said[0] == (
        '#2: its spec is the owner\'s to approve, though its body names row "pack pin" of #9, since its parent #9 has'
        " no `Approved: spec` record by the owner for its current spec: the owner approves it again"
    )


@pytest.mark.parametrize(("settings", "covered"), [("loop:\n  delegate: all\n", True), ("", False)])
def test_a_delegates_parent_approval_covers_a_row_only_where_it_counts(
    writers: list[str], settings: str, covered: bool
) -> None:
    """The parent's approval is judged as a direct link's: the delegate's record counts under `loop.delegate: all`."""
    policy = loop_md(f"spec:\n  auto:\n    - bug\n{settings}")
    parent = issue(
        9,
        "kind:enhancement",
        "approved:spec",
        comments=(record("spec", "delegate", spec=AS_WRITTEN),),
    )
    found = approvals.proofs("build", pull(files=("app.py",)), [child(parent=parent)], policy)
    assert (found == []) is covered


def test_a_wayfinder_row_gets_no_credit_line(writers: list[str]) -> None:
    spec = child(ROW, story(), "wayfinder:decision", "approved:spec")
    assert approvals.holds(pull(files=("app.py",)), [spec], POLICY) == []


def test_a_row_whose_pr_carries_risk_high_is_the_owners(writers: list[str]) -> None:
    pr = pull(files=("app.py",), labels=("risk:high",))
    (line, *rest) = approvals.proofs("build", pr, [child(parent=story())], POLICY)
    assert line.endswith(f"since `risk:high` is on PR #{pr['number']}") and rest == OWNER


def test_without_a_policy_file_a_row_gets_no_credit_line(writers: list[str]) -> None:
    assert approvals.holds(pull(files=("app.py",)), [child(parent=story())], None) == []


def test_under_spec_auto_true_a_row_the_rating_leaves_to_the_owner_is_credited_not_held(
    writers: list[str],
) -> None:
    """`levels.low.spec: owner` leaves every spec to the owner under `spec.auto: true`; a row's parent approval is the
    owner's, so `check` credits the row instead of saying its spec is the owner's to approve."""
    policy = loop_md("spec:\n  auto: true\nlevels:\n  low:\n    spec: owner\n    merge: gates\n")
    pr, spec = pull(files=("app.py",)), child(parent=story())
    assert approvals.proofs("build", pr, [spec], policy) == []
    assert approvals.holds(pr, [spec], policy) == [
        '#1: covered by #9\'s approved spec (row "pack pin")'
    ]
    held = (
        "#1: its spec is the owner's to approve under `spec.auto: true`, since nothing rates it above low, and"
        " `levels.low.spec` is `owner`"
    )
    assert approvals.holds(pr, [child("Replace the pin.\n", story())], policy) == [held]


def test_a_row_the_parent_does_not_cover_passes_on_its_own_owner_approval(
    writers: list[str],
) -> None:
    """A row line that doesn't credit the parent explains why the spec is the owner's only while the owner's approval
    is missing: with the owner's record and `approved:spec` on the row, nothing is left to say."""
    approved = issue(
        1,
        "kind:chore",
        "area:web",
        "size:S",
        "approved:spec",
        body=ROW.replace("#9", "#8"),
        comments=(record("spec", "owner", spec=AS_WRITTEN),),
    )
    spec = {**approved, "parent": story(), "author": {"login": "worker"}}
    assert approvals.proofs("build", pull(files=("app.py",)), [spec], POLICY) == []
    assert approvals.proofs("build", pull(files=("billing/pay.py",)), [spec], POLICY) == []
    unapproved = child(ROW.replace("#9", "#8"), story())
    line = (
        '#1: its spec is the owner\'s to approve, though its body names row "pack pin" of #8,'
        " since its row line names #8, but its parent is #9"
    )
    assert approvals.proofs("build", pull(files=("app.py",)), [unapproved], POLICY) == [
        line,
        *OWNER,
    ]
