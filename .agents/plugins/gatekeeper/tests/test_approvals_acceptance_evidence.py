"""approvals.py: a verifier report states its criteria first and may accept a low-risk `Manual:` line on the worker's
evidence; `brief` names the spec's acceptance command; the role files carry the criteria step, the shared worktree's
conditions and the `Verifier session:` setting (#1157)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import (
    BASE,
    HEAD,
    LOOP,
    SCRIPTS,
    approvals,
    pull,
)

SKILL = Path(__file__).resolve().parents[2] / "loop" / "skills" / "engineering-loop"
CRITERIA = "- A retry never double-charges: Before merge line 1, its check `tests/test_retry.py`."
ACCEPTED = "Accepted on evidence: Manual: the receipt shows one charge — tmp/loop/receipt.log"


def report_text(
    risk: str = "low — a test fixture", criteria: str | None = CRITERIA, extra: str = ""
) -> str:
    """A satisfied report with a `## Criteria` section and a manual line taken on the worker's evidence."""
    section = f"## Criteria\n\n{criteria}\n\n" if criteria is not None else ""
    return (
        "Verifier: codex/terra-6.1/med, pass 1\n\n"
        f"{section}"
        "## Correctness\n\nNothing found.\n\n"
        f"{ACCEPTED}\n{extra}\n"
        f"Head: {HEAD}\nVERDICT: 0 blocker, 0 major, 0 minor\nSATISFIED: yes\nRisk: {risk}\n"
    )


@pytest.fixture
def posting(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, dict[str, Any]]]:
    """`verdict` against a stubbed GitHub: each review it posts."""
    posted: list[tuple[int, dict[str, Any]]] = []
    monkeypatch.setattr(approvals, "pull_request", lambda _n: pull())
    monkeypatch.setattr(approvals, "diff_lines", lambda _base, _head: {"app.py": [1]})
    monkeypatch.setattr(approvals, "post_review", lambda pr, review: posted.append((pr, review)))
    return posted


def test_a_low_risk_report_accepting_a_manual_line_on_evidence_is_posted_with_its_criteria_first(
    posting: list[tuple[int, dict[str, Any]]], tmp_path: Path
) -> None:
    report = tmp_path / "report.md"
    report.write_text(report_text())
    assert approvals.main(["verdict", "7", str(report)]) == 0
    [(pr, review)] = posting
    assert (pr, review["commit_id"]) == (7, HEAD)
    assert review["body"] == approvals.fold(
        f"Verifier (codex/terra-6.1/med), pass 1 on {HEAD[:7]}.",
        "VERDICT: 0 blocker, 0 major, 0 minor\nSATISFIED: yes\nRisk: low — a test fixture",
        f"## Criteria\n\n{CRITERIA}\n\n{ACCEPTED}",
    )


@pytest.mark.parametrize(
    "risk", ["high — billing", "medium — unsure", "High — billing", "lowish — no"]
)
def test_the_same_report_under_any_risk_but_low_is_refused_naming_the_line(
    posting: list[tuple[int, dict[str, Any]]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    risk: str,
) -> None:
    """Any word but `low` reads as high, as the gate reads it, so a manual line there is probed by hand."""
    report = tmp_path / "report.md"
    report.write_text(report_text(risk=risk))
    assert approvals.main(["verdict", "7", str(report)]) == 1
    said = capsys.readouterr().out
    assert f"`Risk: {risk}` with `{ACCEPTED}`" in said
    assert "probed by hand" in said
    assert posting == []


def test_a_low_risk_written_against_the_dash_still_reads_as_low(
    posting: list[tuple[int, dict[str, Any]]], tmp_path: Path
) -> None:
    report = tmp_path / "report.md"
    report.write_text(report_text(risk="low—a test fixture"))
    assert approvals.main(["verdict", "7", str(report)]) == 0


@pytest.mark.parametrize(
    "criteria",
    [None, "", "```\n## Criteria\n\n- inside a fence\n```"],
    ids=["no section", "an empty section", "only in a code fence"],
)
def test_a_report_without_its_criteria_is_refused(
    posting: list[tuple[int, dict[str, Any]]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    criteria: str | None,
) -> None:
    report = tmp_path / "report.md"
    text = report_text(criteria=criteria)
    if criteria and criteria.startswith("```"):  # the fence alone, with no section of its own
        text = text.replace("## Criteria\n\n```", "```", 1)
    report.write_text(text)
    assert approvals.main(["verdict", "7", str(report)]) == 1
    assert capsys.readouterr().out.startswith(f"{report} has no `## Criteria` section")
    assert approvals.main(["report", str(report)]) == 1
    assert posting == []


def test_the_criteria_end_at_the_next_section_and_keep_a_fenced_heading(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    fenced = "- First.\n\n```\n## not a heading\n```\n\n### Mapping\n\n- C1 to line 1."
    report.write_text(report_text(criteria=fenced))
    assert approvals.report_facts(report)["criteria"] == fenced


def test_an_accepted_line_naming_no_proof_log_is_refused(
    posting: list[tuple[int, dict[str, Any]]], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "report.md"
    report.write_text(report_text().replace(" — tmp/loop/receipt.log", ""))
    assert approvals.main(["verdict", "7", str(report)]) == 1
    assert "can't read `Accepted on evidence: Manual: the receipt shows one charge`" in (
        capsys.readouterr().out
    )
    assert posting == []


@pytest.mark.parametrize("line", ["Not run: acceptance line 2", "Not run: nonesuch"])
def test_a_satisfied_report_with_a_not_run_line_is_still_refused(
    posting: list[tuple[int, dict[str, Any]]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    line: str,
) -> None:
    report = tmp_path / "report.md"
    report.write_text(report_text(extra=f"{line}\n"))
    assert approvals.main(["verdict", "7", str(report)]) == 1
    assert "lists a `Not run:` line" in capsys.readouterr().out
    assert posting == []


@pytest.mark.parametrize(
    "line", ["Not run: none", "Not run: none.", "not run: None.", "Not run:  NONE  "]
)
def test_not_run_none_is_no_not_run_line(
    posting: list[tuple[int, dict[str, Any]]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    line: str,
) -> None:
    report = tmp_path / "report.md"
    report.write_text(report_text(extra=f"{line}\n"))
    assert approvals.main(["verdict", "7", str(report)]) == 0
    assert "lists a `Not run:` line" not in capsys.readouterr().out
    assert len(posting) == 1


def test_an_inline_accepted_phrase_is_refused_and_an_out_of_scope_line_blocks_nothing(
    posting: list[tuple[int, dict[str, Any]]], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "report.md"
    report.write_text(
        report_text(extra="7. Manual: a demo. **Accepted on evidence:** the PR has one\n")
    )
    assert approvals.main(["verdict", "7", str(report)]) == 1
    assert "write it as a line of its own" in capsys.readouterr().out
    # a quote in prose, inline code or a fence takes nothing on evidence
    quote = "C2: a line written `7. Manual: x **Accepted on evidence:** y` is refused.\n"
    fence = "```\n7. Manual: x **Accepted on evidence:** y\nAccepted on evidence: z\n```\n"
    report.write_text(report_text(extra=quote + fence))
    assert approvals.main(["report", str(report)]) == 0
    report.write_text(report_text(extra="Out of scope: the full gate, left to the lead\n"))
    assert approvals.main(["verdict", "7", str(report)]) == 0
    assert len(posting) == 1


def test_report_json_gives_the_criteria_and_each_line_taken_on_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "report.md"
    report.write_text(report_text())
    assert approvals.main(["report", "--json", str(report)]) == 0
    facts = json.loads(capsys.readouterr().out)
    assert facts["criteria"] == CRITERIA
    assert facts["accepted_on_evidence"] == [
        {"line": "Manual: the receipt shows one charge", "proof": "tmp/loop/receipt.log"}
    ]


@pytest.fixture
def briefing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """`brief` for PR #7, its body in `state["body"]`, with loop.md's text in `state["loop"]`."""
    monkeypatch.chdir(tmp_path)  # no clone here, so the unforced-merge read's fetch fails at once
    state: dict[str, Any] = {"loop": LOOP, "body": "Closes #12.\n"}
    monkeypatch.setattr(approvals, "pull_request", lambda _n: pull(body=state["body"]))
    monkeypatch.setattr(approvals, "merge_base", lambda _base, _head: BASE)
    monkeypatch.setattr(approvals, "project", lambda: (None, state["loop"]))
    return state


def acceptance_lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("- Acceptance")
    ]


@pytest.mark.parametrize(
    "line", ["Acceptance tests: mise run acceptance", "Acceptance tests: `mise run acceptance`  "]
)
def test_brief_prints_loop_md_s_acceptance_command_for_the_spec(
    briefing: dict[str, Any], capsys: pytest.CaptureFixture[str], line: str
) -> None:
    briefing["loop"] = LOOP + f"\n{line}\n"
    briefing["body"] = "Closes #12.\n"
    assert approvals.main(["brief", "7"]) == 0
    assert acceptance_lines(capsys) == ["- Acceptance tests: mise run acceptance 12"]


def test_brief_prints_one_acceptance_line_per_spec(
    briefing: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    briefing["loop"] = LOOP + "\nAcceptance tests: mise run acceptance\n"
    briefing["body"] = "Closes #13.\n\nCloses #12.\n"
    assert approvals.main(["brief", "7"]) == 0
    assert acceptance_lines(capsys) == [
        "- Acceptance tests: mise run acceptance 12",
        "- Acceptance tests: mise run acceptance 13",
    ]


def test_brief_prints_the_fallback_runner_when_loop_md_has_no_line(
    briefing: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    briefing["body"] = "Closes #12.\n"
    assert "Acceptance tests:" not in LOOP
    assert approvals.main(["brief", "7"]) == 0
    runner = os.path.relpath(SCRIPTS / "acceptance.py")
    assert acceptance_lines(capsys) == [f"- Acceptance tests: uv run {runner} 12"]
    # an empty line names no command, so the fallback runs
    briefing["loop"] = LOOP + "\nAcceptance tests: ``\n"
    assert approvals.main(["brief", "7"]) == 0
    assert acceptance_lines(capsys) == [f"- Acceptance tests: uv run {runner} 12"]


def test_brief_names_no_acceptance_command_without_a_spec(
    briefing: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    briefing["body"] = "No issue here.\n"
    assert approvals.main(["brief", "7"]) == 0
    assert acceptance_lines(capsys) == []


def flat(name: str) -> str:
    return " ".join((SKILL / name).read_text().split())


def test_verifier_writes_its_criteria_before_reading_the_workers_report() -> None:
    verifier = flat("verifier.md")
    criteria = verifier.index("**Criteria first.**")
    # the step comes before the Method that reads the change and before any steering that reads the worker's evidence
    assert criteria < verifier.index("## Method") < verifier.index("**Proof provenance.**")
    step = verifier[criteria : verifier.index("## Method")]
    assert step.startswith(
        "**Criteria first.** Before reading the worker's report or proof logs, write from the spec"
    )
    assert "`## Criteria`" in step
    assert "A criterion no line covers is a finding" in step
    assert "`Accepted on evidence: <line> — <proof log>`" in verifier
    assert "You never rely on the worker's test log" in verifier  # #1872: the worker runs no gate


def test_lead_step_7_states_the_four_shared_worktree_conditions() -> None:
    lead = flat("lead.md")
    step = lead[lead.index("7. **Verify**") : lead.index("8. **Land**")]
    shared = step[step.index("**Shared worktree.**") :]
    for condition in (
        "its HEAD is the pushed head under review",
        "`git status --porcelain` is empty",
        "built from that head with its data reset by the project's seed step",
        "the worker's session is stopped (no turn running)",
    ):
        assert condition in shared, condition
    assert "its own detached worktree" in shared
    assert "Verifier session: reuse" in step


def test_skill_states_the_verifier_session_setting() -> None:
    skill = flat("SKILL.md")
    assert "`Verifier session: reuse | fresh`" in skill
    assert "independent of the author but not fresh" in skill


def test_a_far_brief_names_the_fallback_runner_in_the_verifier_s_worktree(
    briefing: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A verifier on another host runs the worktree's own copy of the runner, as it reads its own role file."""
    monkeypatch.setattr(approvals, "toplevel", lambda: SCRIPTS.parents[3])
    assert approvals.main(["brief", "7", "--far", "/there/repo"]) == 0
    tree = "/there/repo/tmp/loop/pr7-v1-tree"
    assert acceptance_lines(capsys) == [
        f"- Acceptance tests: uv run {tree}/gatekeeper/skills/gatekeeper/scripts/acceptance.py 12"
    ]
