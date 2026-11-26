"""claim() names the lead's Omnigent session: a `Session:` line in the `Assigned:` record and the issue's optional
`Session` issue field (#2324). An account without the field claims as before, for one lookup and one line."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest
from approvals_harness import approvals, approved, held, labelled, note, pull, specced
from test_approvals import assigned_event, in_project

SERVER, LEAD, WORKER = "https://example.test", "lead-session", "worker-session"
LINK = f"{SERVER}/c/{LEAD}"


@pytest.fixture
def omnigent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lead's run under Omnigent: the server's URL and the lead's session id."""
    monkeypatch.setenv("RUNNER_SERVER_URL", f"{SERVER}/")
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", LEAD)


def free(number: int) -> dict[str, Any]:
    """An approved issue no one holds, its spec approved by ann."""
    return held(approved(number), events=(labelled("ann"),))


@pytest.mark.usefixtures("omnigent")
def test_the_claim_names_the_lead_s_session_in_its_record_and_the_issue_field(
    assigned: list[tuple[Any, ...]], session_fields: list[tuple[Any, ...]]
) -> None:
    assert approvals.claim(pull(), [free(1)]) == []
    assert assigned == [
        (1, f"Assigned: @ann\nBy: loop\nPR: #7\nSession: {LINK}\n"),
        (1, "ann", True),
        (7, "ann", True),
    ]
    assert session_fields == [("lookup", 1, "Session"), ("set", "ISSUE_1", "FIELD", LINK)]


def test_outside_omnigent_the_record_and_the_field_are_as_before(
    assigned: list[tuple[Any, ...]], session_fields: list[tuple[Any, ...]]
) -> None:
    assert approvals.claim(pull(), [free(1)]) == []
    assert assigned[0] == (1, "Assigned: @ann\nBy: loop\nPR: #7\n")
    assert session_fields == []


@pytest.mark.usefixtures("omnigent")
def test_a_worker_s_claim_names_its_lead_not_itself(
    monkeypatch: pytest.MonkeyPatch,
    assigned: list[tuple[Any, ...]],
    session_fields: list[tuple[Any, ...]],
) -> None:
    monkeypatch.setenv("OMNIGENT_SESSION_ID", WORKER)  # the child's own id, beside its lead's
    assert approvals.claim(pull(), [free(1)]) == []
    assert assigned[0][1].endswith(f"Session: {LINK}\n")
    assert session_fields[-1] == ("set", "ISSUE_1", "FIELD", LINK)
    assert WORKER not in repr((assigned, session_fields))


@pytest.mark.usefixtures("omnigent")
def test_a_refused_field_write_is_one_line_and_never_blocks_the_claim(
    monkeypatch: pytest.MonkeyPatch, assigned: list[tuple[Any, ...]]
) -> None:
    def refuse(issue: str, field: str, text: str) -> None:
        raise subprocess.CalledProcessError(1, "gh", stderr="FORBIDDEN")

    monkeypatch.setattr(approvals, "set_issue_field", refuse)
    assert approvals.claim(pull(), [free(1)]) == [
        "#1: could not set the `Session` field (FORBIDDEN); its `Assigned:` record names the session"
    ]
    assert (1, "ann", True) in assigned and (7, "ann", True) in assigned


@pytest.mark.usefixtures("omnigent")
def test_an_account_without_the_field_claims_for_one_lookup_and_one_line(
    monkeypatch: pytest.MonkeyPatch,
    assigned: list[tuple[Any, ...]],
    session_fields: list[tuple[Any, ...]],
) -> None:
    monkeypatch.setattr(
        approvals,
        "issue_field",
        lambda number, name: (
            session_fields.append(("lookup", number, name)) or (None, f"ISSUE_{number}")
        ),
    )
    lines = approvals.claim(pull(), [free(1), free(2)])
    assert lines == [
        (
            "no issue's `Session` field set (the account has no `Session` issue field); the `Assigned:` record"
            f" names the session. {approvals.SESSION_STEPS}"
        )
    ]
    assert "Field name `Session`, Field type Text" in approvals.SESSION_STEPS
    assert session_fields == [
        ("lookup", 1, "Session")
    ]  # one lookup a run, no write, whatever the issues
    assert [call for call in assigned if len(call) == 2] == [
        (1, f"Assigned: @ann\nBy: loop\nPR: #7\nSession: {LINK}\n"),
        (2, f"Assigned: @ann\nBy: loop\nPR: #7\nSession: {LINK}\n"),
    ]
    assert {(1, "ann", True), (2, "ann", True), (7, "ann", True)} <= set(assigned)
    assert approvals.claim(pull(), [free(3)]) == []  # said once a run, and asked no more
    assert session_fields == [("lookup", 1, "Session")]


@pytest.mark.usefixtures("omnigent")
def test_check_build_exits_0_where_the_account_lacks_the_field(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    assigned: list[tuple[Any, ...]],
    session_fields: list[tuple[Any, ...]],
) -> None:
    in_project(monkeypatch)
    monkeypatch.setattr(approvals, "issue_field", lambda number, name: (None, f"ISSUE_{number}"))
    pr = pull()
    pr["closingIssuesReferences"]["nodes"] = [held(specced(1, "kind:bug"))]
    monkeypatch.setattr(approvals, "pull_request", lambda _pr: pr)
    assert approvals.main(["check", "build", "7"]) == 0
    assert "the account has no `Session` issue field" in capsys.readouterr().out
    assert (1, "loop", True) in assigned
    assert [call for call in session_fields if call[0] == "set"] == []


@pytest.mark.usefixtures("omnigent")
def test_where_graphql_is_refused_rest_finds_the_field_once_and_writes_it(
    monkeypatch: pytest.MonkeyPatch, session_fields: list[tuple[Any, ...]]
) -> None:
    monkeypatch.setattr(approvals.github, "_rest_only", True)
    assert approvals.claim(pull(), [free(1), free(2)]) == []
    assert session_fields == [
        ("rest exists", "Session"),
        ("rest id", "Session"),
        ("rest set", 1, 42, LINK),
        ("rest set", 2, 42, LINK),
    ]


@pytest.mark.usefixtures("omnigent")
def test_a_graphql_refusal_mid_run_falls_back_to_rest(
    monkeypatch: pytest.MonkeyPatch, session_fields: list[tuple[Any, ...]]
) -> None:
    def refuse(number: int, name: str) -> tuple[str | None, str]:
        raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 403: Forbidden")

    monkeypatch.setattr(approvals, "issue_field", refuse)
    assert approvals.claim(pull(), [free(1)]) == []
    assert session_fields[-1] == ("rest set", 1, 42, LINK)


@pytest.mark.usefixtures("omnigent")
def test_over_rest_an_account_without_the_field_pays_one_lookup(
    monkeypatch: pytest.MonkeyPatch, session_fields: list[tuple[Any, ...]]
) -> None:
    monkeypatch.setattr(approvals.github, "_rest_only", True)
    monkeypatch.setattr(
        approvals,
        "rest_has_issue_field",
        lambda name: session_fields.append(("rest exists", name)) and False,
    )
    [line] = approvals.claim(pull(), [free(1), free(2)])
    assert line.startswith(
        "no issue's `Session` field set (the account has no `Session` issue field)"
    )
    assert session_fields == [("rest exists", "Session")]


@pytest.mark.usefixtures("omnigent")
def test_over_rest_a_field_no_issue_carries_yet_is_one_line(
    monkeypatch: pytest.MonkeyPatch, session_fields: list[tuple[Any, ...]]
) -> None:
    monkeypatch.setattr(approvals.github, "_rest_only", True)
    monkeypatch.setattr(approvals, "rest_issue_field_id", lambda name: None)
    [line] = approvals.claim(pull(), [free(1), free(2)])
    assert "REST names the `Session` field's id only once an issue carries a value" in line
    assert [call for call in session_fields if call[0] == "rest set"] == []


def test_check_setup_names_a_missing_field_with_github_s_steps_and_exits_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(approvals, "setup_problems", lambda _root: [])
    monkeypatch.setattr(approvals, "project", lambda: (None, ""))
    monkeypatch.setattr(approvals, "mergify_drift", lambda *_a, **_k: None)
    monkeypatch.setattr(approvals, "tree_policy", lambda _root: "")
    monkeypatch.setattr(approvals, "rest_has_issue_field", lambda name: False)
    assert approvals.setup() == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1:] == [
        (
            "setup: the account has no `Session` issue field (optional), so a claim's `Assigned:` record alone"
            " names the lead's Omnigent session."
        ),
        approvals.SESSION_STEPS,
    ]
    monkeypatch.setattr(approvals, "rest_has_issue_field", lambda name: True)
    assert approvals.setup() == 0
    assert "Session" not in capsys.readouterr().out


@pytest.mark.usefixtures("omnigent")
def test_a_failed_lookup_is_one_line_and_never_blocks_the_claim(
    monkeypatch: pytest.MonkeyPatch, assigned: list[tuple[Any, ...]]
) -> None:
    def fail(number: int, name: str) -> tuple[str | None, str]:
        raise subprocess.CalledProcessError(
            1, "gh", stderr="Field 'issueFields' doesn't exist on type 'Repository'"
        )

    monkeypatch.setattr(approvals, "issue_field", fail)
    assert approvals.claim(pull(), [free(1), free(2)]) == [
        (
            "no issue's `Session` field set (Field 'issueFields' doesn't exist on type 'Repository'); the"
            " `Assigned:` record names the session"
        )
    ]
    assert {(1, "ann", True), (2, "ann", True)} <= set(assigned)


def test_the_session_line_leaves_the_record_the_loop_s_own() -> None:
    body = approvals.fold(f"{approvals.ASSIGNED}ann", details=f"By: loop\nPR: #7\nSession: {LINK}")
    issue = held(
        {**approved(1), "comments": {"totalCount": 1, "nodes": [note(body)]}},
        "ann",
        events=(assigned_event("ann", "2099-01-01T00:00:00Z"),),
    )
    assert approvals.assigned_by_loop(issue, "ann")


def test_the_rest_adapter_checks_finds_and_writes_the_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[str, ...], str | None]] = []
    issues = [
        {"issue_field_values": []},
        {"issue_field_values": [{"issue_field_id": 7, "issue_field_name": "Session"}]},
    ]

    def gh(*args: str, data: str | None = None) -> str:
        calls.append((args, data))
        if "issue_field_values=session:-" in args:
            return "[]"
        if "issue_field_values=nothing-here:-" in args:
            raise subprocess.CalledProcessError(1, "gh", stderr="gh: Validation Failed (HTTP 422)")
        return json.dumps(issues)

    monkeypatch.setattr(approvals.github, "gh", gh)
    assert approvals.github.rest_has_issue_field("Session")
    assert not approvals.github.rest_has_issue_field("Nothing here")
    assert approvals.github.rest_issue_field_id("Session") == 7
    assert approvals.github.rest_issue_field_id("Owner") is None
    approvals.github.rest_set_issue_field(1, 7, LINK)
    args, data = calls[-1]
    assert ("-X", "POST") == args[
        1:3
    ] and "repos/{owner}/{repo}/issues/1/issue-field-values" in args
    assert json.loads(data or "") == {"issue_field_values": [{"field_id": 7, "value": LINK}]}


def test_the_adapter_reads_the_field_and_the_issue_in_one_query_and_writes_its_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    fields = [{}, {"id": "IF_other", "name": "Priority"}, {"id": "IF_session", "name": "Session"}]

    def gh(*args: str, **_kw: Any) -> str:
        calls.append(args)
        if "setIssueFieldValue" in " ".join(args):
            return ""
        repository = {"issueFields": {"nodes": fields}, "issue": {"id": "I_1"}}
        return json.dumps({"data": {"repository": repository}})

    monkeypatch.setattr(approvals.github, "gh", gh)
    assert approvals.github.issue_field(1, "Session") == ("IF_session", "I_1")
    assert approvals.github.issue_field(1, "Owner") == (None, "I_1")
    [query] = [arg for arg in calls[0] if arg.startswith("query=")]
    assert "issueFields(first: 100)" in query and "issue(number: $n) { id }" in query
    approvals.github.set_issue_field("I_1", "IF_session", LINK)
    write = calls[-1]
    assert "issueFields: [{fieldId: $field, textValue: $text}]" in " ".join(write)
    assert {"issue=I_1", "field=IF_session", f"text={LINK}"} <= set(write)
