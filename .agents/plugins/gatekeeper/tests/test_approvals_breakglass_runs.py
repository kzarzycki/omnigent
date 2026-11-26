"""approvals.py: the break-glass proof read from GitHub's run data, one test per item of #1722's section 2 and per refusal
of its section 5 that a run decides. The runs are recorded in fixtures/breakglass_runs.json, shaped as GitHub's REST
API returns them: the failing `qualify` run (11) on the head with two failed tests, a green re-run of both alone (12),
two earlier green runs (21, 22), main's failures of both tests (31) and the merge base's of one (32), a run too old
(41), one of another repository (51), one of another workflow (52) and a smoke run (61, of the same workflow; a run counts only if it is a `pull_request`, `push` or `schedule` run with a green `qualify` job)."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import BASE, HEAD, add, approvals, loop_md, pull

DATA = json.loads((Path(__file__).parent / "fixtures" / "breakglass_runs.json").read_text())
LOOP = add(loop_md("loop:\n  owner: octocat\n"), "breakglass", "max_commits: 20\n")
SLOW, FLAKY = "tests/test_a.py::test_slow", "tests/test_b.py::test_flaky"


@pytest.fixture
def forge(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """GitHub's runs from the recorded data, a copy a test may edit; `far` names the heads more than max_commits away."""
    data = copy.deepcopy(DATA)
    state: dict[str, Any] = {"data": data, "far": set(), "smoke": False}
    runs = {run["id"]: run for run in data["runs"]}

    def run_info(run: int) -> dict[str, Any]:
        if run not in runs:
            raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 404")
        return runs[run]

    def listed(
        flow: int, branch: str | None = None, head: str | None = None
    ) -> list[dict[str, Any]]:
        ids = {("main", None): [31, 41], (None, BASE): [32], (None, HEAD): [11, 12]}[branch, head]
        return [runs[i] for i in ids if runs[i]["workflow_id"] == flow]

    def compare(sha: str, _onto: str) -> dict[str, Any]:
        far = sha in state["far"]
        return {"status": "ahead", "ahead_by": 99 if far else 3, "behind_by": 0}

    monkeypatch.setattr(approvals, "run_info", run_info)
    monkeypatch.setattr(approvals, "run_jobs", lambda run: data["jobs"].get(str(run), []))
    monkeypatch.setattr(approvals, "workflow_runs", listed)
    monkeypatch.setattr(
        approvals, "run_artifact", lambda run, _name: data["artifacts"].get(str(run))
    )
    monkeypatch.setattr(approvals, "compare", compare)
    monkeypatch.setattr(approvals, "merge_base", lambda _base, _head: BASE)
    monkeypatch.setattr(approvals, "required_checks", lambda _base: {"qualify"})
    monkeypatch.setattr(approvals, "smoke_recorded", lambda _head, _cmd: state["smoke"])
    monkeypatch.setattr(approvals, "utcnow", lambda: approvals.parse_time("2026-10-02T00:00:00Z"))
    state["runs"] = runs
    return state


def proof(**ids: str) -> dict[str, Any]:
    """A proof comment naming `ids` (a key `smoke`, `rerun`, ... as the comment's lines)."""
    named = {"failed-run": "11", "rerun": "12", "earlier-runs": "21, 22", **ids}
    lines = [f"Head: {HEAD}", *(f"{k.capitalize()}: {v}" for k, v in named.items() if v)]
    return {"body": approvals.fold(approvals.PROOF, "", "\n".join(lines))}


def verify(note: dict[str, Any], loop: str = LOOP) -> Any:
    return approvals.breakglass_verify(pull(checks=(("qualify", "FAILURE"),)), loop, note)


def assert_holds(forge: dict[str, Any], note: dict[str, Any], *, smoke: bool = True) -> Any:
    forge["smoke"] = smoke
    got = verify(note)
    assert got.why == [], got.why
    return got


def test_every_item_holds_and_each_failed_test_is_classified_with_its_run_links(
    forge: dict[str, Any],
) -> None:
    got = assert_holds(forge, proof())
    text = "\n".join(got.lines)
    assert got.infra is False
    assert f"`{SLOW}`: not caused by this change; it also failed in" in text
    assert "actions/runs/31" in text and "actions/runs/32" in text  # the flaky test failed on both
    assert "the failed tests passed alone on the head: " in text and "runs/12" in text
    assert "earlier green run" in text and "smoke: local-ci" in text


# --- 2a: the failure on this head ---


def test_2a_the_failed_run_must_be_a_failure_on_the_exact_head(forge: dict[str, Any]) -> None:
    forge["runs"][11]["head_sha"] = "c" * 40
    assert "is not a failure on the head" in verify(proof()).why[0]
    forge["runs"][11].update(head_sha=HEAD, conclusion="success")
    assert "is not a failure on the head" in verify(proof()).why[0]


def test_2a_a_proof_naming_no_failed_run_or_an_unreadable_one_is_refused(
    forge: dict[str, Any],
) -> None:
    assert "names no failed run" in verify(proof(**{"failed-run": ""})).why[0]
    assert "can't be read from GitHub" in verify(proof(**{"failed-run": "404"})).why[0]


def test_2a_a_failure_with_no_test_ids_is_infra_and_skips_the_rerun(forge: dict[str, Any]) -> None:
    forge["data"]["artifacts"].pop("11")
    forge["data"]["jobs"]["11"] = forge["data"]["jobs"]["13"]  # the runner was lost
    forge["smoke"] = True
    got = verify(proof(rerun=""))
    assert (got.why, got.infra) == ([], True)
    assert "infra (no failed test ids)" in got.lines[0]


def test_2a_a_failure_with_empty_test_lists_and_no_job_down_is_the_changes_own_and_refused(
    forge: dict[str, Any],
) -> None:
    forge["data"]["artifacts"]["11"] = {"failed": [], "selected": []}  # a lint or build failure
    forge["smoke"] = True
    got = verify(proof(rerun=""))
    assert got.infra is False
    assert "failed with no failed test ids and no job shows CI down" in got.why[0]
    forge["data"]["jobs"]["11"] = forge["data"]["jobs"]["13"]
    assert verify(proof(rerun="")).infra is True


def test_2a_a_failed_run_with_no_artifact_and_no_lost_job_is_refused(forge: dict[str, Any]) -> None:
    forge["data"]["artifacts"].pop("11")
    assert "published no `failed-tests` artifact" in verify(proof()).why[0]


def test_the_failed_run_must_be_of_the_workflow_that_produces_the_required_check(
    forge: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals, "required_checks", lambda _base: {"other-check"})
    assert "produces none of the required checks" in verify(proof()).why[0]


# --- 2b: the classification ---


def test_2b_a_test_that_failed_on_no_recent_main_or_base_run_is_unexplained_and_refused(
    forge: dict[str, Any],
) -> None:
    forge["data"]["artifacts"]["11"]["failed"].append("tests/test_new.py::test_mine")
    forge["data"]["artifacts"]["12"]["selected"].append("tests/test_new.py::test_mine")
    forge["smoke"] = True
    got = verify(proof())
    assert got.why == [
        "PR #7: `tests/test_new.py::test_mine` is unexplained: only this change can explain it, so it is refused"
    ]
    assert "`tests/test_new.py::test_mine`: unexplained" in "\n".join(got.lines)


def test_2b_main_runs_older_than_max_age_explain_nothing(forge: dict[str, Any]) -> None:
    forge["data"]["artifacts"]["11"]["failed"].append(
        "tests/test_z.py::test_old"
    )  # run 41, a month old
    assert "`tests/test_z.py::test_old` is unexplained" in verify(proof()).why[0]


def test_2b_runs_more_than_max_commits_away_explain_nothing(forge: dict[str, Any]) -> None:
    forge["far"] = {"d" * 40, BASE}  # the heads of runs 31 and 32
    forge["smoke"] = True
    got = verify(proof())
    assert len(got.why) == 2 and all("is unexplained" in line for line in got.why)


def test_2b_a_main_run_of_another_repository_explains_nothing(forge: dict[str, Any]) -> None:
    forge["runs"][31]["repository"] = {"full_name": "someone/else"}
    forge["runs"][32]["head_repository"] = {"full_name": "someone/else"}
    forge["smoke"] = True
    assert len(verify(proof()).why) == 2


# --- 2c: the failed tests re-run alone on this head ---


def test_2c_a_rerun_that_does_not_cover_every_failed_test_is_refused(forge: dict[str, Any]) -> None:
    forge["data"]["artifacts"]["12"]["selected"] = [SLOW]
    forge["smoke"] = True
    assert "did not re-run every failed test alone" in verify(proof()).why[0]


def test_2c_a_rerun_that_is_red_or_on_another_head_is_refused(forge: dict[str, Any]) -> None:
    forge["smoke"] = True
    forge["runs"][12]["conclusion"] = "failure"
    assert "is not a green run of the workflow on the head" in verify(proof()).why[0]
    forge["runs"][12].update(conclusion="success", head_sha="c" * 40)
    assert "is not a green run of the workflow on the head" in verify(proof()).why[0]


def test_2c_a_rerun_that_contains_more_than_the_failed_tests_counts(forge: dict[str, Any]) -> None:
    forge["data"]["artifacts"]["12"]["selected"] = [SLOW, FLAKY, "tests/test_c.py::test_ok"]
    assert_holds(forge, proof())


def test_2c_no_rerun_named_is_refused(forge: dict[str, Any]) -> None:
    forge["smoke"] = True
    assert "names no re-run run" in verify(proof(rerun="")).why[0]


# --- 2d: the smoke run ---


def test_2d_no_smoke_record_and_no_smoke_run_is_refused(forge: dict[str, Any]) -> None:
    assert "no smoke proof on the head" in verify(proof()).why[0]


def test_2d_a_green_actions_run_of_the_smoke_job_on_the_head_counts(forge: dict[str, Any]) -> None:
    forge["runs"][61]["head_sha"] = HEAD
    assert_holds(forge, proof(smoke="61"), smoke=False)


def test_2d_a_smoke_run_on_another_head_or_red_is_refused(forge: dict[str, Any]) -> None:
    assert (
        "is not a green full run of the required check on the head"
        in verify(proof(smoke="61")).why[0]
    )
    forge["runs"][61].update(head_sha=HEAD, conclusion="failure")
    assert (
        "is not a green full run of the required check on the head"
        in verify(proof(smoke="61")).why[0]
    )


@pytest.mark.parametrize("tests", [[], ["tests/test_c.py::test_ok"]])
def test_2d_a_dispatch_run_on_the_head_is_no_smoke_run_empty_or_narrowed(
    forge: dict[str, Any], tests: list[str]
) -> None:
    forge["runs"][61].update(head_sha=HEAD, event="workflow_dispatch")
    forge["data"]["artifacts"]["61"] = {"failed": [], "selected": tests}
    assert "is not a green full run" in verify(proof(smoke="61")).why[0]


def test_2d_a_green_run_of_another_workflow_or_without_the_required_job_is_no_smoke_run(
    forge: dict[str, Any],
) -> None:
    forge["runs"][61].update(head_sha=HEAD, workflow_id=6)
    assert "is not a green full run" in verify(proof(smoke="61")).why[0]
    forge["runs"][61].update(workflow_id=5)
    forge["data"]["jobs"]["61"][0]["conclusion"] = "skipped"
    assert "is not a green full run" in verify(proof(smoke="61")).why[0]


def test_2d_the_smoke_record_is_read_for_the_policys_command(
    forge: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(
        approvals, "smoke_recorded", lambda _head, command: asked.append(command) or True
    )
    assert verify(proof(), add(LOOP, "breakglass", "smoke: mise run smoke\n")).why == []
    assert asked == ["mise run smoke"]


def test_2d_a_local_ci_record_of_another_tree_or_command_is_no_smoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = "t" * 40
    monkeypatch.setattr(approvals, "tree_of", lambda _head: tree)
    monkeypatch.setattr(approvals, "recorded", lambda held, _cmd: tmp_path / f"{held}.json")
    (tmp_path / f"{tree}.json").write_text(
        json.dumps({"tree": tree, "command": "check-land", "exit": 0})
    )
    assert approvals.smoke_recorded(HEAD, "check-land") is True
    assert approvals.smoke_recorded(HEAD, "another") is False  # a record of another command
    (tmp_path / f"{tree}.json").write_text(
        json.dumps({"tree": "x" * 40, "command": "check-land", "exit": 0})
    )
    assert approvals.smoke_recorded(HEAD, "check-land") is False  # another tree
    (tmp_path / f"{tree}.json").write_text(
        json.dumps({"tree": tree, "command": "check-land", "exit": 1})
    )
    assert approvals.smoke_recorded(HEAD, "check-land") is False  # a failed run


# --- 2e: earlier green runs ---


def test_2e_fewer_green_runs_than_earlier_runs_is_refused(forge: dict[str, Any]) -> None:
    forge["smoke"] = True
    assert "1 earlier green run(s)" in verify(proof(**{"earlier-runs": "21"})).why[0]


def test_2e_runs_of_another_repository_workflow_or_age_do_not_count(forge: dict[str, Any]) -> None:
    forge["smoke"] = True
    assert "0 earlier green run(s)" in verify(proof(**{"earlier-runs": "51, 52, 41"})).why[0]


def test_2e_a_run_further_than_max_commits_or_not_an_ancestor_does_not_count(
    forge: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    forge["smoke"] = True
    forge["far"] = {"e" * 40}
    assert "1 earlier green run(s)" in verify(proof()).why[0]
    forge["far"] = set()
    monkeypatch.setattr(
        approvals, "compare", lambda *_a: {"status": "diverged", "ahead_by": 1, "behind_by": 1}
    )
    assert "0 earlier green run(s)" in verify(proof(**{"earlier-runs": "21, 22"})).why[-1]


@pytest.mark.parametrize("tests", [[], ["tests/test_c.py::test_ok"]])
def test_2e_dispatch_runs_do_not_count_empty_or_narrowed(
    forge: dict[str, Any], tests: list[str]
) -> None:
    forge["smoke"] = True
    for run in (21, 22):
        forge["runs"][run]["event"] = "workflow_dispatch"
        forge["data"]["artifacts"][str(run)] = {"failed": [], "selected": tests}
    assert "0 earlier green run(s)" in verify(proof()).why[-1]
    forge["runs"][21]["event"] = "push"
    assert "1 earlier green run(s)" in verify(proof()).why[-1]


def test_2e_a_run_without_the_required_jobs_green_does_not_count(forge: dict[str, Any]) -> None:
    forge["smoke"] = True
    forge["data"]["jobs"]["21"][0]["conclusion"] = "skipped"
    assert "1 earlier green run(s)" in verify(proof()).why[-1]
    forge["data"]["jobs"]["21"] = []
    assert "1 earlier green run(s)" in verify(proof()).why[-1]


def test_2e_the_policy_sets_how_many_earlier_runs_it_needs(forge: dict[str, Any]) -> None:
    forge["smoke"] = True
    assert (
        "earlier green run(s)"
        in verify(proof(), add(LOOP, "breakglass", "earlier_runs: 3\n")).why[0]
    )
    assert (
        verify(proof(**{"earlier-runs": "21"}), add(LOOP, "breakglass", "earlier_runs: 1\n")).why
        == []
    )


# --- the cover, and what land does with a verified PR ---

RED = "PR #7: `qualify` is FAILURE on the head commit"


@pytest.fixture
def standing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner's record is current (test_approvals_breakglass.py reads it); only the runs decide here."""
    note = proof()
    note["url"] = "https://example.test/7#proof"
    monkeypatch.setattr(
        approvals, "standing", lambda *_a: approvals.Standing(True, [], {"head": HEAD}, note)
    )


def test_the_cover_clears_a_red_check_only_when_every_run_item_holds(
    forge: dict[str, Any], standing: None
) -> None:
    pr = pull(checks=(("qualify", "FAILURE"),))
    forge["smoke"] = False
    refused = approvals.breakglass_cover(pr, LOOP, [RED])
    assert refused.said == [] and refused.lines[0] == RED and "no smoke proof" in refused.lines[1]
    forge["smoke"] = True
    covered = approvals.breakglass_cover(pr, LOOP, [RED])
    assert covered.lines == []
    assert covered.said[0] == (
        f"PR #7: required check `qualify` red; break-glass approved by the owner on {HEAD}"
    )


def test_land_leaves_a_verified_break_glass_pr_unmerged_unqueued_and_not_landed(
    forge: dict[str, Any],
    standing: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    forge["smoke"] = True
    pr = pull(checks=(("qualify", "FAILURE"),), labels=("loop:queued",))
    done: list[Any] = []
    monkeypatch.setattr(
        approvals, "label", lambda n, name, add: done.append(("label", n, name, add))
    )
    monkeypatch.setattr(
        approvals,
        "statuses",
        lambda _head: [{"context": "land", "state": "success", "creator": {"login": "loop"}}],
    )
    monkeypatch.setattr(approvals, "post_status", lambda *a: done.append(("status", *a)))
    monkeypatch.setattr(approvals, "comment", lambda n, body: done.append(("comment", n, body)))
    monkeypatch.setattr(
        approvals, "asks_owner", lambda _p, lines, named: done.append(("ask", named, lines))
    )
    monkeypatch.setattr(approvals.port("forge"), "merge", lambda *a: done.append(("MERGE", *a)))
    monkeypatch.setattr(approvals, "required_checks", lambda _base: {"qualify"})
    monkeypatch.setattr(approvals, "flagged_as", lambda p, _i, _l: p)
    monkeypatch.setattr(approvals, "proof_gaps", lambda *_a: ([], []))
    monkeypatch.setattr(
        approvals, "head_checks", lambda *_a: ([("qualify", "FAILURE")], None, set())
    )
    verdict = approvals.landable(pr, None, LOOP, {"qualify"}, named=True)
    assert verdict.exit == (approvals.WAITING, False)
    assert not [d for d in done if d[0] == "MERGE"]
    assert ("label", 7, "loop:queued", False) in done
    [status] = [d for d in done if d[0] == "status"]
    assert status[1:3] == (HEAD, "land") and status[-1] == "error"
    [comment] = [d for d in done if d[0] == "comment"]
    assert comment[2].startswith("Break-glass verified\n")
    assert "Proof: https://example.test/7#proof" in comment[2]
    assert "Trailer: Break-glass: owner-approved, qualify red, proof " in comment[2]
    [ask] = [d for d in done if d[0] == "ask"]
    assert "the owner merges it in the GitHub UI" in ask[2][0]
    assert "break-glass approved by the owner" in capsys.readouterr().out


def test_after_the_owners_merge_the_trail_step_writes_one_break_glass_land_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = approvals.fold(
        approvals.VOICED["owner"], "", f"Approved: breakglass\nBy: owner\nHead: {HEAD}\nProof: x"
    )
    pr = pull(comments=(record,), checks=(("qualify", "FAILURE"),))
    pr["comments"]["nodes"][0]["url"] = "https://example.test/7#record"
    posted: list[str] = []
    monkeypatch.setattr(approvals, "merged_pull", lambda _n: {"merge_commit_sha": "m" * 40})
    monkeypatch.setattr(
        approvals,
        "commit_checks",
        lambda _ref: {
            "qualify": {"conclusion": "FAILURE", "url": "https://x/actions/runs/11/job/3"}
        },
    )
    monkeypatch.setattr(approvals, "required_checks", lambda _base: {"qualify"})
    monkeypatch.setattr(
        approvals, "comment", lambda _n, body: posted.append(approvals.machine(body))
    )
    approvals.breakglass_landed(pr)
    [body] = posted
    assert body.startswith("Break-glass land\nHead: " + HEAD)
    assert f"Merge: {'m' * 40}" in body and "`qualify` red: https://x/actions/runs/11" in body
    assert "Record: https://example.test/7#record" in body
    assert "Trailer: Break-glass: owner-approved, qualify red, proof " in body
    pr["comments"]["nodes"].append(
        {"id": "x", "author": {"login": "loop"}, "body": "Break-glass land\n"}
    )
    approvals.breakglass_landed(pr)
    assert len(posted) == 1  # once


def test_a_merged_pr_with_no_break_glass_record_gets_no_trail_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(approvals, "comment", lambda *_a: pytest.fail("no comment"))
    approvals.breakglass_landed(pull())
