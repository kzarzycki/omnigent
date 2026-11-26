"""approvals.py: a PR's risk as the board's single-select `Risk` field beside the `risk:high` label (#929).

flag() sets the field to `High` when it adds the label, adds the label when the field already says `High`, never
takes the label off for a lower field, and sets the field to the verifier's `Risk:` word. Board access is best
effort: no board or no `project` scope is a line, never a failure. The board answers through board.py's GraphQL
calls, stubbed by the `board_risks` fixture.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import (
    HELD,
    SCRIPTS,
    TRACKER,
    VERIFIED,
    approvals,
    approved,
    gating,  # noqa: F401
    issue,
    judged_pr,
    judging,
    pull,
)


@pytest.fixture
def comments(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    """Every comment approvals.py posts, instead of posting it."""
    sent: list[tuple[int, str]] = []
    monkeypatch.setattr(approvals, "comment", lambda number, body: sent.append((number, body)))
    return sent


def test_adding_risk_high_sets_the_board_field_to_high(
    board_risks: dict[str, Any],
    added: list[tuple[int, str, bool]],
    comments: list[tuple[int, str]],
) -> None:
    board_risks["values"][7] = "Low"
    assert approvals.flag(pull(), [approved(1), issue(3, "risk:high")], None) == []
    assert added == [(7, "risk:high", True)]
    assert board_risks["set"] == [(7, "High")]


def test_a_pr_off_the_board_is_added_with_the_field_at_high(
    board_risks: dict[str, Any],
    added: list[tuple[int, str, bool]],
    comments: list[tuple[int, str]],
) -> None:
    assert approvals.flag(pull(), [issue(3, "risk:high")], None) == []
    assert board_risks["set"] == [(7, "High")]


def test_a_field_at_high_adds_the_label_names_the_board_and_holds_the_merge(
    board_risks: dict[str, Any],
    added: list[tuple[int, str, bool]],
    comments: list[tuple[int, str]],
) -> None:
    board_risks["values"][7] = "High"
    pr = pull(labels=("risk:low",))
    assert approvals.risk(pr, [approved(1)], None) is None
    assert approvals.flag(pr, [approved(1)], None) == []
    assert added == [(7, "risk:high", True), (7, "risk:low", False)]
    assert comments == [(7, "`risk:high` added, since the board's `Risk` field says `High`.\n")]
    # the checks after flag() read the label it added, so the merge holds in this run too
    assert approvals.risk(pr, [approved(1)], None) == "`risk:high` is on PR #7"
    assert board_risks["set"] == []


def test_a_field_at_high_holds_check_merge_for_the_owner(
    gating: tuple[dict[str, Any], list[tuple[int, str]]],
    board_risks: dict[str, Any],
    added: list[tuple[int, str, bool]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    state, sent = gating
    state["pull"] = judged_pr(judging("low — a flag on two test commands"))
    board_risks["values"][7] = "High"
    assert approvals.main(["check", "merge", "7"]) == approvals.WAITING
    assert capsys.readouterr().out.splitlines() == [HELD + "`risk:high` is on PR #7"]
    assert added == [(7, "risk:high", True)]
    assert [body for _, body in sent] == [
        "`risk:high` added, since the board's `Risk` field says `High`.\n"
    ]


@pytest.mark.parametrize("field", ["Low", "Medium", None])
def test_a_lower_field_leaves_an_existing_risk_high_and_rises_to_it(
    board_risks: dict[str, Any],
    added: list[tuple[int, str, bool]],
    comments: list[tuple[int, str]],
    field: str | None,
) -> None:
    if field:
        board_risks["values"][7] = field
    assert approvals.flag(pull(labels=("risk:high",)), [approved(1)], None) == []
    assert (added, comments) == ([], [])
    assert board_risks["set"] == [(7, "High")]


def test_a_field_already_at_high_beside_the_label_is_not_set_again(
    board_risks: dict[str, Any], added: list[tuple[int, str, bool]]
) -> None:
    board_risks["values"][7] = "High"
    assert approvals.flag(pull(labels=("risk:high",)), [approved(1)], None) == []
    assert (added, board_risks["set"]) == ([], [])


@pytest.mark.parametrize(
    ("said", "option"),
    [
        ("low — a doc fix", "Low"),
        ("high — it narrows the gate", "High"),
        ("medium — unsure", "High"),
    ],
    ids=["low", "high", "another-word"],
)
def test_the_verifiers_risk_line_sets_the_field(
    gating: tuple[dict[str, Any], list[tuple[int, str]]],
    board_risks: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    said: str,
    option: str,
) -> None:
    state, _ = gating
    state["pull"] = judged_pr(judging(said))
    approvals.main(["check", "merge", "7"])
    assert board_risks["set"] == [(7, option)]
    assert "board" not in capsys.readouterr().out


def test_a_low_pr_the_board_refuses_names_the_verifiers_line_not_the_label(
    gating: tuple[dict[str, Any], list[tuple[int, str]]],
    board_risks: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refused(*_args: Any, **_kwargs: Any) -> None:
        raise approvals.subprocess.CalledProcessError(1, "gh", stderr="HTTP 502\n")

    monkeypatch.setattr(approvals.board(), "set_field", refused)
    state, _ = gating
    state["pull"] = judged_pr(judging("low — a doc fix"))
    approvals.main(["check", "merge", "7"])
    out = capsys.readouterr().out
    assert "field to `Low` (HTTP 502); the verifier's `Risk:` line alone shows the risk" in out
    assert "risk:high" not in out


def test_without_a_verifier_pass_on_the_head_a_low_pr_leaves_the_field(
    board_risks: dict[str, Any],
) -> None:
    assert approvals.flag(pull(), [approved(1)], None) == []
    assert board_risks["set"] == []


@pytest.mark.parametrize(
    ("setup", "why"),
    [
        ({"linked": False}, "no project board is linked to this repo"),
        (
            {"refused": "gh: Your token has not been granted the required scopes: project\n"},
            "gh: Your token has not been granted the required scopes: project",
        ),
    ],
    ids=["no-board", "no-scope"],
)
def test_without_a_board_or_its_scope_the_label_is_added_with_one_line_about_the_board(
    board_risks: dict[str, Any],
    added: list[tuple[int, str, bool]],
    comments: list[tuple[int, str]],
    setup: dict[str, Any],
    why: str,
) -> None:
    board_risks.update(setup)
    assert approvals.flag(pull(), [issue(3, "risk:high")], None) == [
        f"PR #7: could not set the board's `Risk` field to `High` ({why}); the `risk:high` label alone shows the risk"
    ]
    assert added == [(7, "risk:high", True)]
    assert [number for number, _ in comments] == [7]


def test_a_board_without_the_risk_field_is_a_line(
    board_risks: dict[str, Any],
    added: list[tuple[int, str, bool]],
    comments: list[tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = approvals.board()._graphql

    def fieldless(query: str, **values: Any) -> Any:
        found = real(query, **values)
        if query == approvals.board().LOOKUP:
            found["repository"]["projectsV2"]["nodes"][0]["field"] = None
        return found

    monkeypatch.setattr(approvals.board(), "_graphql", fieldless)
    assert approvals.flag(pull(), [issue(3, "risk:high")], None) == [
        (
            "PR #7: could not set the board's `Risk` field to `High` (the board has no Risk field); the `risk:high`"
            " label alone shows the risk"
        )
    ]
    assert added == [(7, "risk:high", True)]


def test_a_refused_set_is_a_line_not_a_failure(
    board_risks: dict[str, Any],
    added: list[tuple[int, str, bool]],
    comments: list[tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refused(*_args: Any, **_kwargs: Any) -> None:
        raise approvals.subprocess.CalledProcessError(1, "gh", stderr="HTTP 502\n")

    monkeypatch.setattr(approvals.board(), "set_field", refused)
    assert approvals.flag(pull(), [issue(3, "risk:high")], None) == [
        "PR #7: could not set the board's `Risk` field to `High` (HTTP 502); the `risk:high` label alone shows the risk"
    ]


def test_a_run_refused_graphql_reads_no_board(
    board_risks: dict[str, Any],
    added: list[tuple[int, str, bool]],
    comments: list[tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_risks["values"][7] = "High"
    monkeypatch.setattr(approvals.github, "_rest_only", True)
    assert approvals.flag(pull(), [approved(1)], None) == []
    assert (added, board_risks["set"]) == ([], [])


@pytest.mark.parametrize(
    "stderr", ["HTTP 403: Resource not accessible by integration\n", "HTTP 502: Bad Gateway\n"]
)
def test_a_refused_label_write_still_holds_the_merge_and_the_landing_for_a_board_high(
    gating: tuple[dict[str, Any], list[tuple[int, str]]],
    board_risks: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stderr: str,
) -> None:
    """The board's `High` is a known high risk for this run, whether or not GitHub keeps the label."""

    def refused(number: int, name: str, add: bool) -> None:
        raise subprocess.CalledProcessError(1, "gh", stderr=stderr)

    monkeypatch.setattr(approvals, "label", refused)
    board_risks["values"][7] = "High"
    state, _ = gating
    state["pull"] = judged_pr(judging("low — a flag on two test commands"))
    assert approvals.main(["check", "merge", "7"]) == approvals.WAITING
    out = capsys.readouterr().out.splitlines()
    assert f"PR #7: could not add `risk:high` ({stderr.strip()}): add it by hand" in out
    assert HELD + "`risk:high` is on PR #7" in out
    landing = judged_pr(judging("low — a flag on two test commands"))
    assert approvals.landable(landing, TRACKER, VERIFIED, set()).exit == (approvals.WAITING, False)


FAKE_GH = """#!{python}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["GH_CALLS"], "a") as calls:
    calls.write(json.dumps(args) + "\\n")
query = next(a[len("query="):] for a in args if a.startswith("query="))
values = dict(a.split("=", 1) for a in args if "=" in a and not a.startswith("query="))
if "issueOrPullRequest" in query:
    data = {{"repository": {{
        "issueOrPullRequest": {{"id": "PR_7", "projectItems": {{"nodes": [
            {{"project": {{"id": "P"}}, "fieldValueByName": {{"name": "Medium"}}}}]}}}},
        "projectsV2": {{"nodes": [{{"id": "P", "field": {{"id": "R", "options": [
            {{"id": "r1", "name": "Low"}}, {{"id": "r2", "name": "Medium"}}, {{"id": "r3", "name": "High"}}]}}}}]}}}}}}
elif "addProjectV2ItemById" in query:
    data = {{"addProjectV2ItemById": {{"item": {{"id": "I"}}}}}}
else:
    data = {{"updateProjectV2ItemFieldValue": {{"projectV2Item": {{"id": values["item"]}}}}}}
print(json.dumps({{"data": data}}))
"""


def test_a_fresh_run_loads_board_py_and_reads_and_sets_the_field_through_gh(tmp_path: Path) -> None:
    """approvals.py imported as a run imports it, in a process of its own, loads board.py beside it on first use;
    only gh, the GitHub boundary, is stubbed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH.format(python=sys.executable))
    gh.chmod(0o755)
    calls = tmp_path / "calls"
    code = (
        # as a run starts it: the script's folder on the path, the module under a name other than `approvals`
        "import importlib.util, sys\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        f"spec = importlib.util.spec_from_file_location('run', {str(SCRIPTS / 'approvals.py')!r})\n"
        "approvals = sys.modules['run'] = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(approvals)\n"
        "pull = {'number': 7, 'labels': {'nodes': [{'name': 'risk:high'}]}}\n"
        "shown = approvals.board_risk(7)\n"
        "print(shown[1])\n"
        "print(approvals.boarded(pull, None, shown))\n"
        # board.py reads the running module, not a second copy of it
        "print(sys.modules['approvals'] is approvals)\n"
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GH_CALLS": str(calls),
    }
    run = subprocess.run(
        [sys.executable, "-B", "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
    )
    assert (run.returncode, run.stdout, run.stderr) == (0, "Medium\n[]\nTrue\n", "")
    queries = [
        next(a for a in json.loads(line) if a.startswith("query="))
        for line in calls.read_text().splitlines()
    ]
    assert [
        "issueOrPullRequest" in queries[0],
        "addProjectV2ItemById" in queries[1],
        len(queries),
    ] == [True, True, 3]
    assert "option=r3" in calls.read_text().splitlines()[2]
