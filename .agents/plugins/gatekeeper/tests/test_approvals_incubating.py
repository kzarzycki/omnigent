"""approvals.py: an `incubating` PR stops owing the requirements `incubating.waive` names, while it stays eligible."""

from __future__ import annotations

from typing import Any

import pytest
from approvals_harness import (
    HEAD,
    approvals,
    approved,
    issue,
    loop_md,
    pull,
    verdict_on,
)

BODY = "Incubates: #9\n"
POLICY = loop_md("incubating:\n  append_paths: [mise.toml]\n")


@pytest.fixture(autouse=True)
def epic(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The tracking epic #9, open and labelled `incubating`; a test edits it."""
    node = {**issue(9, "incubating", "epic"), "state": "OPEN"}
    monkeypatch.setattr(approvals, "issue_node", lambda _n: node)
    return node


def merge(
    files: list[dict[str, Any]], loop: str = POLICY, body: str = BODY, **fields: Any
) -> list[str]:
    pr = pull(body=body, labels=("incubating",), **fields)
    pr["files"] = {"nodes": files}
    issues = [approved(1, "area:web", "kind:enhancement")]
    return approvals.proofs("merge", pr, issues, loop)


def new_file(path: str) -> dict[str, Any]:
    return {"path": path, "changeType": "ADDED", "additions": 5, "deletions": 0}


def edited(path: str, deletions: int = 0) -> dict[str, Any]:
    return {"path": path, "changeType": "MODIFIED", "additions": 2, "deletions": deletions}


def test_added_files_owe_no_evidence_or_verifier() -> None:
    assert merge([new_file("new/skill.md")]) == []


def test_an_append_to_an_allowed_file_is_eligible() -> None:
    assert merge([new_file("new.py"), edited("mise.toml")]) == []


@pytest.mark.parametrize(
    "files, named",
    [
        ([edited("mise.toml", deletions=1)], "mise.toml"),
        ([edited("src/app.py")], "src/app.py"),
        ([{"path": "a.py", "changeType": "DELETED", "deletions": 3}], "a.py"),
        ([{"path": "b.py", "changeType": "RENAMED", "deletions": 0}], "b.py"),
        ([{"path": "mise.toml", "changeType": "MODIFIED"}], "mise.toml"),  # no count read: not zero
    ],
)
def test_touching_existing_code_owes_the_full_loop(files: list[dict[str, Any]], named: str) -> None:
    found = merge(files)
    assert any(named in line and "owes the full loop" in line for line in found)
    assert any("## Evidence" in line for line in found)  # the body has none, and none is waived
    assert any("verifier" in line.lower() or "review" in line.lower() for line in found)


def test_no_epic_line_owes_the_full_loop() -> None:
    found = merge([new_file("x.md")], body="")
    assert any("no `Incubates: #<epic>` line" in line for line in found)
    assert any("## Evidence" in line for line in found)


def test_a_closed_epic_owes_the_full_loop(epic: dict[str, Any]) -> None:
    epic["state"] = "CLOSED"
    assert any(
        "not an open issue labelled `incubating`" in line for line in merge([new_file("x.md")])
    )


def test_a_high_risk_change_is_not_eligible() -> None:
    loop = loop_md("incubating:\n  append_paths: []\nrisk:\n  paths: [secure/**]\n")
    found = merge([new_file("secure/key.py")], loop=loop)
    assert any("rates high or critical" in line for line in found)


def test_waive_names_the_requirements_and_the_rest_stay_required() -> None:
    loop = loop_md("incubating:\n  waive: [evidence]\n")
    found = merge([new_file("x.md")], loop=loop)
    assert not any("## Evidence" in line for line in found)
    assert any("review" in line.lower() or "verifier" in line.lower() for line in found)


def test_an_unknown_waive_name_is_refused() -> None:
    with pytest.raises(approvals.Refused, match="coverage"):
        merge([new_file("x.md")], loop=loop_md("incubating:\n  waive: [speed]\n"))


def test_the_label_means_nothing_without_the_policy() -> None:
    found = merge([new_file("x.md")], loop=loop_md(), body="")
    assert any("## Evidence" in line for line in found)


def test_a_waived_verifier_still_reads_an_unresolved_thread() -> None:
    found = merge([new_file("x.md")], threads=(False,))
    assert any("unresolved" in line for line in found)


def test_a_verdict_that_exists_is_not_required_to_be_satisfied_but_threads_still_count() -> None:
    assert merge([new_file("x.md")], reviews=(verdict_on(HEAD),)) == []


def test_a_truncated_file_list_owes_the_full_loop() -> None:
    pr = pull(body=BODY, labels=("incubating",))
    pr["files"] = {"totalCount": 101, "nodes": [new_file("x.md")]}
    found = approvals.proofs("merge", pr, [approved(1, "area:web", "kind:enhancement")], POLICY)
    assert any("can't give every file" in line for line in found)


def test_an_open_epic_without_the_label_owes_the_full_loop(epic: dict[str, Any]) -> None:
    epic["labels"] = {"nodes": [{"name": "epic"}]}
    assert any(
        "not an open issue labelled `incubating`" in line for line in merge([new_file("x.md")])
    )


@pytest.mark.parametrize("body", ["`Incubates: #9`\n", "```\nIncubates: #9\n```\n"])
def test_a_quoted_incubates_line_counts_for_nothing(body: str) -> None:
    assert any(
        "no `Incubates: #<epic>` line" in line for line in merge([new_file("x.md")], body=body)
    )


def test_a_waived_verifier_still_reads_a_request_for_changes() -> None:
    found = merge([new_file("x.md")], latest=("CHANGES_REQUESTED",))
    assert any("requested changes" in line for line in found)


def closes_epic(body: str, labels: tuple[str, ...] = ()) -> list[str]:
    """`proofs` at merge for a PR whose body is `body`, with epic #9 (labelled `incubating`) among its issues."""
    pr = pull(body=body, labels=labels)
    pr["files"] = {"nodes": [new_file("new/skill.md")]}
    issues = [
        approved(1, "area:web", "kind:enhancement"),
        approved(9, "incubating", "epic", "area:web", "kind:enhancement"),
    ]
    return [
        line
        for line in approvals.proofs("merge", pr, issues, POLICY)
        if "graduat" in line or "Graduates" in line
    ]


@pytest.mark.parametrize("keyword", ["Closes", "Fixes", "Resolves"])
def test_a_pr_that_closes_an_incubating_issue_must_graduate_it(keyword: str) -> None:
    (line,) = closes_epic(f"{keyword} #9\n")
    assert "labelled `incubating`" in line and "Graduates #9" in line


def test_part_of_and_a_graduates_line_let_the_merge_through() -> None:
    assert closes_epic("Part of #9\n") == []
    assert closes_epic("Closes #9\nGraduates #9\n") == []


def test_a_quoted_graduates_line_graduates_nothing() -> None:
    assert closes_epic("Closes #9\n```\nGraduates #9\n```\n")


def test_a_graduating_pr_owes_the_full_loop_even_when_labelled_incubating() -> None:
    lines = closes_epic("Incubates: #9\nCloses #9\nGraduates #9\n", labels=("incubating",))
    assert any("owes the full loop" in line for line in lines)


@pytest.fixture
def graduating(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Every call `graduate` makes, in order, with PR #5 merged and saying `Graduates #9`."""
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        approvals,
        "merged_pull",
        lambda n: {"body": "Graduates #9\n", "base": {"ref": "main"}} if n == 5 else None,
    )
    monkeypatch.setattr(approvals, "default_branch", lambda: "main")
    monkeypatch.setattr(approvals, "comment", lambda n, body: calls.append(("comment", n, body)))
    monkeypatch.setattr(
        approvals, "label", lambda n, name, add: calls.append(("label", n, name, add))
    )
    monkeypatch.setattr(approvals, "close_issue", lambda n: calls.append(("close", n)))
    return calls


def test_graduate_names_the_pr_then_drops_the_label_then_closes(
    graduating: list[tuple[Any, ...]],
) -> None:
    assert approvals.graduate(9, 5) == 0
    assert [call[0] for call in graduating] == ["comment", "label", "close"]
    assert graduating[0][2].startswith("Graduated by #5") and graduating[1] == (
        "label",
        9,
        "incubating",
        False,
    )


@pytest.mark.parametrize("pr", [6, 5])
def test_graduate_refuses_without_a_merged_pr_that_says_so(
    graduating: list[tuple[Any, ...]], pr: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    if pr == 5:
        monkeypatch.setattr(
            approvals, "merged_pull", lambda n: {"body": "Part of #9\n", "base": {"ref": "main"}}
        )
    with pytest.raises(approvals.Refused):
        approvals.graduate(9, pr)
    assert graduating == []


def test_graduate_refuses_a_closed_or_unlabelled_epic(
    graduating: list[tuple[Any, ...]], epic: dict[str, Any]
) -> None:
    epic["state"] = "CLOSED"
    with pytest.raises(approvals.Refused):
        approvals.graduate(9, 5)
    assert graduating == []


def test_graduate_refuses_an_unlabelled_epic(
    graduating: list[tuple[Any, ...]], epic: dict[str, Any]
) -> None:
    epic["labels"] = {"nodes": []}
    with pytest.raises(approvals.Refused):
        approvals.graduate(9, 5)
    assert graduating == []


def test_graduate_refuses_a_pr_merged_into_a_feature_branch(
    graduating: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        approvals, "merged_pull", lambda n: {"body": "Graduates #9\n", "base": {"ref": "feature"}}
    )
    with pytest.raises(approvals.Refused):
        approvals.graduate(9, 5)
    assert graduating == []
