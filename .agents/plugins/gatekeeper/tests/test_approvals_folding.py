"""Every comment the loop writes opens with a short summary and folds the rest (folding.py), and every reader of a
comment's key lines still reads it folded: the approval records, the verifier's review, the
assignment record, the conflict and queue notes, the cloud verifier's report and the nightly's marker."""

from __future__ import annotations

import importlib.util
import re
import sys
from typing import Any

from approvals_harness import HEAD, SCRIPTS, approvals, node, note, pull, verdict_on

BASE = SCRIPTS
LOOP_SCRIPTS = SCRIPTS.parents[3] / "loop" / "skills" / "engineering-loop" / "scripts"


def load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        f"folded_{name}",
        (LOOP_SCRIPTS if name in ("cloud_verifier", "nightly") else BASE) / f"{name}.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.dont_write_bytecode = True
    spec.loader.exec_module(module)
    return module


folding = load("folding")
fold, machine = folding.fold, folding.machine


def visible(body: str) -> list[str]:
    """What GitHub shows before the fold: the lines up to `<details>`."""
    return [line for line in body.split("<details>")[0].splitlines() if line.strip()]


def test_fold_keeps_the_heading_first_and_the_summary_short() -> None:
    body = fold("Landed", "Merged as abc1234.", "Base: x\nTree: y")
    assert body.splitlines()[0] == "Landed"
    assert visible(body) == ["Landed", "Merged as abc1234."]
    assert "<details><summary>Details</summary>" in body and body.rstrip().endswith("</details>")
    # nothing to fold: no empty fold
    assert fold("Assigned: @ann") == "Assigned: @ann\n"


def test_machine_reads_the_heading_then_the_folded_lines_where_they_stood() -> None:
    body = fold("Merge queue batch", "It passed.", "Head: abc\nExit: 0")
    assert machine(body) == "Merge queue batch\nHead: abc\nExit: 0\n"
    # a second block after the fold stays after the fields, a blank line apart
    two = body + "\n<details><summary>The gate's last lines</summary>\n\nlog\n</details>\n"
    assert machine(two).startswith("Merge queue batch\nHead: abc\nExit: 0\n\n<details>")


def test_a_comment_posted_before_folding_reads_as_it_is() -> None:
    old = "Landed\nBase: x\nTree: y\n"
    assert machine(old) == old
    assert machine("") == ""
    assert machine("## Spec\n\n<details><summary>More</summary>\n\nx\n</details>\n").count("More")


def test_approval_records_parse_folded() -> None:
    owner = fold(
        approvals.VOICED["owner"],
        f"Go to merge {HEAD[:7]}.",
        f"Approved: merge\nBy: owner\nHead: {HEAD}",
    )
    delegate = fold("Approved: merge", "The delegate approved the merge.", "By: delegate")
    spec = fold(
        "Approved: spec", "The owner approved the spec.", "By: owner\nSpec: as written 2026"
    )
    assert approvals.parsed(owner, "merge") == {"by": "owner", "head": HEAD}
    assert approvals.parsed(delegate, "merge") == {"by": "delegate"}
    assert approvals.parsed(spec, "spec")["by"] == "owner"
    assert approvals.parsed(spec, "merge") is None
    withdrawn = fold("Withdrawn: merge", "withdrew", f"By: owner\nHead: {HEAD}")
    assert approvals.parsed(withdrawn, "merge") is None


def test_the_verifiers_review_reads_folded() -> None:
    review = verdict_on(HEAD)
    review["body"] = fold(
        f"Verifier (claude), pass 1 on {HEAD[:7]}.",
        "VERDICT: 0 blocker, 0 major, 1 minor\nSATISFIED: yes\nRisk: low — docs only",
        "## Criteria\n\n- the fold parses",
    )
    [(commit, number, lines)] = approvals.passes(pull(reviews=(review,)))
    assert (commit, number) == (HEAD, 1)
    assert lines["Family"] == "claude" and lines["SATISFIED"] == "yes"
    assert lines["VERDICT"] == "0 blocker, 0 major, 1 minor" and lines["Risk"].startswith("low")


def test_the_assignment_record_reads_folded() -> None:
    issue = node(
        comments=(fold(f"{approvals.ASSIGNED}ann", details="By: loop\nPR: #7"),),
        timelineItems={
            "nodes": [
                {
                    "__typename": "AssignedEvent",
                    "createdAt": "2026-01-02T00:00:00Z",
                    "assignee": {"login": "ann"},
                }
            ]
        },
    )
    assert approvals.assigned_by_loop(issue, "ann")


def test_a_conflict_note_is_found_again_by_head_and_by_its_head_line(
    monkeypatch: Any,
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(approvals, "comment", lambda _n, body: sent.append(body))
    p = pull()
    p["headRefOid"] = HEAD
    approvals.conflict_note(p, "main", " plus #7 merged before it")
    [body] = sent
    assert visible(body)[0] == approvals.CONFLICTS and len(visible(body)) <= 3
    assert f"Head: {HEAD}" in body.splitlines()
    # the same head is not commented twice: the reader finds the folded note
    p["comments"]["nodes"].append(note(body))
    approvals.conflict_note(p, "main")
    assert len(sent) == 1


def test_the_cloud_verifiers_report_keeps_its_verdict_in_view_and_its_lines_readable() -> None:
    cv = load("cloud_verifier")
    report = f"# Report\n\nlong prose\n\nHead: {HEAD}\nVERDICT: 0 blocker, 0 major, 0 minor\nSATISFIED: yes\nRisk: low — x\n"
    body = cv.folded_report(report)
    assert visible(body)[0] == "## Verifier report" and len(visible(body)) <= 5
    assert "SATISFIED: yes" in visible(body)
    heads = [line for line in body.splitlines() if line.startswith("Head:")]
    assert set(heads) == {f"Head: {HEAD}"}
    assert re.search(r"^SATISFIED:\s*yes", body, re.MULTILINE)
    # a report with no closing lines is posted as it is
    assert cv.folded_report("no verdict") == "no verdict"
    # the lead's wait reads a question by its first line
    asked = fold("## Verifier question", "which tree?", "which tree?\nmore")
    assert re.match(r"\s*## Verifier question\b", asked)


def test_the_nightlys_marker_survives_the_fold() -> None:
    night = load("nightly")
    body = (
        fold("Still red on `abc`: run", details="Merged since `x`:\n\n- a")
        + "\n"
        + night.MARKER.format("a" * 40, "b" * 40)
    )
    assert night.MARKED.search(body)
    assert visible(body) == ["Still red on `abc`: run"]
