"""approvals.py: a level's `rerun` (#2240). A high-risk head merges only on an independent rerun of its selection: a
record of `local-ci --record --rerun` on its tree that an app pushed (a CI run) or that a verifier pass on the head
names on a `Rerun:` line (a fresh session the verifier started). The worker's own record, or a rerun record a person's
account pushed by hand, is no such proof; a low-risk head still merges on the worker's record (#2143's D1)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import (  # noqa: F401
    HEAD,
    LABELED,
    TREE,
    approvals,
    gating,
    git_in,
    issue,
    judged_pr,
    judging,
    loop_md,
    pull,
)
from test_approvals import github, reviewing  # noqa: F401
from test_approvals_narrowed_gate import repo  # noqa: F401

Gate = tuple[dict[str, Any], list[tuple[int, str]]]

# the real check and the real record push, which the harness's `reran` and `refs` fixtures replace for other modules
REAL_GAP, REAL_PUSH = approvals.rerun_gap, approvals.push_record
RERUN_REF = f"refs/heads/loop-gate/{TREE}-111111111111"
RERUN_COMMIT = "c" * 40
GATES = "risk:\n  verifier: true\nlevels:\n  high:\n    merge: gates\n"
ASKED = "PR #7 is rated high, and `levels.high.rerun` asks an independent rerun of its selection on its head:"


@pytest.fixture
def forge(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The real rerun check, on origin's rerun records (`records`, as reruns() lists them) and the forge's newest push
    to each branch (`pushes`, by ref), the head's tree TREE."""
    state: dict[str, Any] = {"records": [], "pushes": {}}
    monkeypatch.setattr(approvals, "rerun_gap", REAL_GAP)
    monkeypatch.setattr(approvals, "tree_of", lambda _commit: TREE)
    monkeypatch.setattr(approvals, "reruns", lambda held: state["records"] if held == TREE else [])
    monkeypatch.setattr(approvals, "pusher", lambda ref: state["pushes"].get(ref))
    return state


def policy(settings: str) -> None:
    Path("docs/agents/loop.md").write_text(loop_md(settings))


def check_merge(capsys: pytest.CaptureFixture[str]) -> tuple[int, list[str]]:
    code = approvals.main(["check", "merge", "7"])
    return code, capsys.readouterr().out.splitlines()


def naming(commit: str) -> dict[str, Any]:
    """A satisfied low-risk verifier review on the head whose report names a rerun record's commit."""
    review = judging("low — fine")
    return {**review, "body": review["body"] + f"Rerun: {commit}\n"}


def test_a_high_risk_head_with_only_the_workers_record_is_refused(
    gating: Gate, forge: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The worker's record carries no rerun mark, so reruns() lists none, and the merge is refused."""
    state, _ = gating
    policy(GATES)
    state["pull"] = judged_pr(judging("low — fine"), labels=("risk:high",))
    code, out = check_merge(capsys)
    assert code == 1
    assert out == [
        (
            f"{ASKED} origin holds no rerun record on tree {TREE}. Where the repo has CI, a CI run on the head runs"
            " `local-ci --record --rerun` and pushes its record; with no CI, the verifier starts a fresh session that"
            " runs it on the head and names the record's commit on a `Rerun: <commit>` line of its report"
        )
    ]


def test_a_rerun_record_a_person_pushed_by_hand_is_refused(
    gating: Gate, forge: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    state, _ = gating
    policy(GATES)
    forge["records"] = [(RERUN_REF, RERUN_COMMIT)]
    forge["pushes"][RERUN_REF] = {"after": RERUN_COMMIT, "login": "worker", "app": False}
    state["pull"] = judged_pr(judging("low — fine"), labels=("risk:high",))
    code, out = check_merge(capsys)
    assert code == 1
    assert out[0].startswith(
        f"{ASKED} origin holds 1 rerun record(s) on tree {TREE}, but none github-actions[bot] pushed or a verifier"
        " pass on the head names."
    )


def test_a_rerun_record_an_app_pushed_lets_a_high_risk_head_merge(
    gating: Gate, forge: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    state, _ = gating
    policy(GATES)
    forge["records"] = [(RERUN_REF, RERUN_COMMIT)]
    forge["pushes"][RERUN_REF] = {
        "after": RERUN_COMMIT,
        "login": "github-actions[bot]",
        "app": True,
    }
    state["pull"] = judged_pr(judging("low — fine"), labels=("risk:high",))
    assert check_merge(capsys) == (0, [])
    # another app's push is not the CI's, unless the policy names that app
    forge["pushes"][RERUN_REF] = {"after": RERUN_COMMIT, "login": "mergify[bot]", "app": True}
    code, out = check_merge(capsys)
    assert code == 1
    assert "none github-actions[bot] pushed" in out[0]
    policy(GATES + "loop:\n  rerun_pusher: mergify[bot]\n")
    assert check_merge(capsys) == (0, [])
    policy(GATES)
    forge["pushes"][RERUN_REF] = {
        "after": RERUN_COMMIT,
        "login": "github-actions[bot]",
        "app": True,
    }
    # an app's push that set the branch to another commit than its tip is someone else's tip
    forge["pushes"][RERUN_REF] = {"after": "d" * 40, "login": "github-actions[bot]", "app": True}
    assert check_merge(capsys)[0] == 1


def test_a_rerun_record_the_verifier_names_lets_a_high_risk_head_merge(
    gating: Gate, forge: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """With no CI, the verifier's fresh session pushes under the shared account; the verdict naming it is the fact."""
    state, _ = gating
    policy(GATES)
    forge["records"] = [(RERUN_REF, RERUN_COMMIT)]
    forge["pushes"][RERUN_REF] = {"after": RERUN_COMMIT, "login": "worker", "app": False}
    state["pull"] = judged_pr(naming(RERUN_COMMIT), labels=("risk:high",))
    assert check_merge(capsys) == (0, [])
    # a name that is no rerun record's commit names nothing
    state["pull"] = judged_pr(naming("d" * 40), labels=("risk:high",))
    assert check_merge(capsys)[0] == 1
    # trailing words after the commit are the verifier's note, not part of the name
    state["pull"] = judged_pr(
        naming(f"{RERUN_COMMIT} (the fresh session's)"), labels=("risk:high",)
    )
    assert check_merge(capsys) == (0, [])


def test_a_head_of_the_same_tree_keeps_the_rerun_its_carried_verdict_named(
    gating: Gate,
    forge: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reword of commit messages keeps the reviewed tree: the verdict carries, and so does the rerun it names,
    since the record is keyed by that tree (R1). A pass on a head of another tree carries neither."""
    state, _ = gating
    policy(GATES)
    forge["records"] = [(RERUN_REF, RERUN_COMMIT)]
    forge["pushes"][RERUN_REF] = {"after": RERUN_COMMIT, "login": "worker", "app": False}
    reworded = {**naming(RERUN_COMMIT), "commit": {"oid": "f" * 40}}
    state["pull"] = judged_pr(reworded, labels=("risk:high",))
    code, out = check_merge(capsys)
    assert code == 0
    assert not any(ASKED in line for line in out)
    monkeypatch.setattr(
        approvals, "tree_of", lambda commit: "9" * 40 if commit == "f" * 40 else TREE
    )
    state["pull"] = judged_pr(reworded, labels=("risk:high",))
    assert check_merge(capsys)[0] == 1


def test_a_low_risk_head_merges_on_the_workers_record(
    gating: Gate, forge: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """No rerun record at all: the default `levels.low.rerun: false` keeps the worker's record as the proof."""
    state, _ = gating
    policy("risk:\n  verifier: true\n")
    state["pull"] = judged_pr(judging("low — fine"))
    assert check_merge(capsys) == (0, [])


def test_each_level_takes_its_own_rerun_option(
    gating: Gate, forge: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    state, _ = gating
    policy(GATES + "    rerun: false\n")
    state["pull"] = judged_pr(judging("low — fine"), labels=("risk:high",))
    assert check_merge(capsys) == (0, [])
    policy("risk:\n  verifier: true\nlevels:\n  low:\n    rerun: true\n")
    state["pull"] = judged_pr(judging("low — fine"))
    code, out = check_merge(capsys)
    assert code == 1
    assert out[0].startswith(
        "PR #7 is rated low, and `levels.low.rerun` asks an independent rerun of its selection on its head:"
    )


def test_rerun_is_a_boolean_per_level() -> None:
    with pytest.raises(approvals.Refused) as refused:
        approvals.settings(loop_md("levels:\n  high:\n    rerun: sometimes\n"))
    assert "levels" in str(refused.value) and "rerun" in str(refused.value)


# --- the rerun's record: local-ci --record --rerun, and reruns() on a real origin ---


def test_a_rerun_runs_again_whatever_is_recorded_and_records_apart(
    repo: dict[str, Any],
    refs: dict[str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In a repo with CI, the worker's narrowed record exists; the rerun still runs the selection, narrowed against
    the merge base, and records under a command naming LOOP_RERUN, so its key is not the worker's."""
    monkeypatch.delenv(approvals.RERUN_VAR, raising=False)
    repo["loop"]("# Loop\n\nAffected tests: SELECTOR\n")
    base = git_in(repo["work"], "merge-base", "origin/main", "HEAD").strip()
    assert approvals.main(["local-ci", "--record", "--base", base]) == 0
    assert (
        approvals.main(["local-ci", "--record", "--base", base]) == 0
    )  # the worker's pass, reused
    assert len(repo["ran"]()) == 1
    assert approvals.main(["local-ci", "--record", "--rerun"]) == 0
    assert repo["ran"]() == [f"selected app.py docs/agents/loop.md against {base}"] * 2
    commands = sorted(record["command"] for record in refs.values())
    assert len(commands) == 2
    rerun = next(record for record in refs.values() if approvals.RERUN_VAR in record["env"])
    assert rerun["command"].startswith(f"GATE_BASE={base} {approvals.RERUN_VAR}=1 ")
    assert approvals.narrowed_base(rerun["command"]) == base
    # a second rerun runs again too: nothing it finds is reused
    assert approvals.main(["local-ci", "--record", "--rerun"]) == 0
    assert len(repo["ran"]()) == 3
    # and leaves nothing behind: the next command in this process reuses as before
    assert approvals.os.environ.get(approvals.RERUN_VAR) is None
    assert (approvals.FRESH.get(), approvals.KEEP.get()) == (False, False)
    assert approvals.main(["local-ci", "--record", "--base", base]) == 0
    assert len(repo["ran"]()) == 3


def test_a_rerun_whose_record_cannot_reach_origin_fails(
    repo: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A branch of the rerun's name already on origin (someone else's) refuses the push: the rerun exits 1, since a
    record only this clone holds is none rerun_gap reads, where a worker's pass would only say so."""

    def refused(_record: dict[str, Any]) -> str:
        raise subprocess.CalledProcessError(1, "git push", stderr="rejected: non-fast-forward")

    monkeypatch.delenv(approvals.RERUN_VAR, raising=False)
    monkeypatch.setattr(approvals, "push_record", refused)
    repo["loop"]("# Loop\n\nAffected tests: SELECTOR\n")
    assert approvals.main(["local-ci", "--record", "--rerun"]) == 1
    assert (
        "the rerun's record was not pushed to origin, so no gate reads it: rejected"
        in capsys.readouterr().out
    )


@pytest.mark.parametrize(
    "argv",
    [["local-ci", "7", "--rerun"], ["local-ci", "--record", "--head-proof", "--rerun"]],
    ids=["with-a-pr", "with-head-proof"],
)
def test_rerun_goes_with_record_alone(
    argv: list[str], gating: Gate, capsys: pytest.CaptureFixture[str]
) -> None:
    policy("loop:\n  head_proof:\n    status: gate-ui\n    env:\n      - GATE_UI\n")
    assert approvals.main(argv) == 1
    assert "--rerun goes with --record alone" in capsys.readouterr().out


def test_reruns_lists_only_the_rerun_records_on_origin(
    repo: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pushed for real: the worker's record of the same tree and a rerun record; only the rerun's is listed, and
    one whose commit holds another tree than its name says is not."""
    monkeypatch.setattr(approvals, "push_record", REAL_PUSH)
    repo["loop"]("# Loop\n")
    head = repo["head"]()
    held = git_in(repo["work"], "rev-parse", "HEAD^{tree}").strip()
    when = "2026-01-01T00:00:00Z"
    worker = {"head": head, "tree": held, "command": "mise run check", "env": {}, "time": when}
    approvals.push_record(worker)
    rerun = {
        "head": head,
        "tree": held,
        "command": f"{approvals.RERUN_VAR}=1 mise run check",
        "env": {approvals.RERUN_VAR: "1"},
        "time": when,
    }
    ref = approvals.push_record(rerun)
    tip = git_in(repo["work"], "ls-remote", "origin", ref).split()[0]
    assert approvals.reruns(held) == [(ref, tip)]
    # a branch named for this tree whose commit holds another, its message copied from the real record
    other = git_in(repo["work"], "rev-parse", "HEAD~1^{tree}").strip()
    message = git_in(repo["work"], "cat-file", "commit", tip).partition("\n\n")[2]
    forged = git_in(
        repo["work"], "commit-tree", other, "-p", head, "-m", message.rstrip("\n")
    ).strip()
    git_in(
        repo["work"], "push", "-q", "origin", f"{forged}:refs/heads/loop-gate/{held}-222222222222"
    )
    # one with the head's tree but two parents, and one whose first line names another commit than its parent
    base = git_in(repo["work"], "rev-parse", "HEAD~1").strip()
    two = git_in(
        repo["work"], "commit-tree", held, "-p", head, "-p", base, "-m", message.rstrip("\n")
    ).strip()
    git_in(repo["work"], "push", "-q", "origin", f"{two}:refs/heads/loop-gate/{held}-333333333333")
    elsewhere = message.replace(f" on {head[:12]}: ", f" on {base[:12]}: ", 1)
    assert elsewhere != message
    named = git_in(
        repo["work"], "commit-tree", held, "-p", head, "-m", elsewhere.rstrip("\n")
    ).strip()
    git_in(
        repo["work"], "push", "-q", "origin", f"{named}:refs/heads/loop-gate/{held}-444444444444"
    )
    assert approvals.reruns(held) == [(ref, tip)]
    assert approvals.reruns("0" * 40) == []


def test_pusher_reads_the_newest_push_and_whether_an_app_made_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []

    def rest(path: str) -> Any:
        asked.append(path)
        return [{"after": RERUN_COMMIT, "actor": {"login": "github-actions[bot]", "type": "Bot"}}]

    monkeypatch.setattr(approvals.github, "rest", rest)
    assert approvals.github.pusher(RERUN_REF) == {
        "after": RERUN_COMMIT,
        "login": "github-actions[bot]",
        "app": True,
    }
    assert asked == [
        f"repos/{{owner}}/{{repo}}/activity?ref=refs%2Fheads%2Floop-gate%2F{TREE}-111111111111&per_page=1"
    ]
    # a person's account, as a cloud worker's push shows, is no app
    monkeypatch.setattr(
        approvals.github,
        "rest",
        lambda _path: [{"after": RERUN_COMMIT, "actor": {"login": "worker", "type": "User"}}],
    )
    assert approvals.github.pusher(RERUN_REF)["app"] is False
    monkeypatch.setattr(approvals.github, "rest", lambda _path: [])
    assert approvals.github.pusher(RERUN_REF) is None


# --- the verifier's brief in a repo with no CI ---


def brief(
    settings: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> list[str]:
    """`brief 7`'s `- Rerun:` lines under a loop.md with `settings`."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(approvals, "pull_request", lambda _n: pull(body="Closes #12.\n"))
    monkeypatch.setattr(approvals, "merge_base", lambda base, head: "b" * 40)
    monkeypatch.setattr(approvals, "project", lambda: (None, loop_md(settings)))
    assert approvals.main(["brief", "7"]) == 0
    return [line for line in capsys.readouterr().out.splitlines() if line.startswith("- Rerun:")]


def test_with_no_ci_the_verifiers_brief_says_to_start_the_rerun_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    [line] = brief("loop:\n  ci: false\n", monkeypatch, tmp_path, capsys)
    assert line.startswith(
        "- Rerun: this repo has no CI, and gatekeeper.md's `levels` asks an independent rerun for a high or critical"
        " head; where the head rates so (your own `Risk: high` makes it high), start a fresh cloud session that runs `uv run "
    )
    assert line.endswith(
        f"approvals.py local-ci --record --rerun` on a clean checkout of {HEAD} and returns the commit of the record"
        " branch it pushed, then write `Rerun: <that commit>` in your report, since `check merge` refuses that head"
        " without it"
    )


def test_with_ci_or_no_level_asking_a_rerun_the_brief_says_nothing_of_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert brief("", monkeypatch, tmp_path, capsys) == []
    off = "loop:\n  ci: false\nlevels:\n  high:\n    rerun: false\n  critical:\n    rerun: false\n"
    assert brief(off, monkeypatch, tmp_path, capsys) == []


# --- the CI half's two gatekeeper steps: check rerun, and a rerun kept for another job to push ---


def test_check_rerun_says_needed_done_or_no(
    gating: Gate, forge: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    state, _ = gating
    policy(GATES)
    state["pull"] = judged_pr(judging("low — fine"), labels=("risk:high",))
    assert approvals.main(["check", "rerun", "7"]) == 0
    assert capsys.readouterr().out == "PR #7: rerun: needed (rated high)\n"
    forge["records"] = [(RERUN_REF, RERUN_COMMIT)]
    forge["pushes"][RERUN_REF] = {
        "after": RERUN_COMMIT,
        "login": "github-actions[bot]",
        "app": True,
    }
    assert approvals.main(["check", "rerun", "7"]) == 0
    assert capsys.readouterr().out == "PR #7: rerun: done (rated high)\n"
    state["pull"] = judged_pr(judging("low — fine"))
    assert approvals.main(["check", "rerun", "7"]) == 0
    assert (
        capsys.readouterr().out
        == "PR #7: rerun: not needed (`levels.low.rerun` is off) (rated low)\n"
    )


def test_check_rerun_needs_a_pr(gating: Gate, capsys: pytest.CaptureFixture[str]) -> None:
    policy(GATES)
    assert approvals.main(["check", "rerun"]) == 1
    assert "check rerun needs a PR" in capsys.readouterr().out


def test_a_rerun_with_no_push_keeps_its_record_as_a_local_branch_and_names_it(
    repo: dict[str, Any],
    refs: dict[str, dict[str, Any]],
    hub: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """For a CI job whose token can't write: nothing is pushed, no status is written, and the line names the branch and its commit, which
    holds the head's tree, its one parent the head."""
    monkeypatch.delenv(approvals.RERUN_VAR, raising=False)
    repo["loop"]("# Loop\n\nAffected tests: SELECTOR\n")
    capsys.readouterr()
    assert approvals.main(["local-ci", "--record", "--rerun", "--no-push"]) == 0
    assert refs == {}
    assert hub == {}
    [kept] = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith(approvals.KEPT)
    ]
    ref, commit = kept[len(approvals.KEPT) :].split(" ")
    held = git_in(repo["work"], "rev-parse", "HEAD^{tree}").strip()
    assert ref.startswith(f"refs/heads/loop-gate/{held}-")
    assert git_in(repo["work"], "rev-parse", ref).strip() == commit
    assert git_in(repo["work"], "rev-list", "--parents", "-n", "1", commit).split() == [
        commit,
        repo["head"](),
    ]
    assert f'"{approvals.RERUN_VAR}": "1"' in git_in(
        repo["work"], "log", "-1", "--format=%B", commit
    )
    # the record the push job pushes is one the gatekeeper reads as a rerun's
    assert approvals.reruns(held) == []  # not on origin: kept here only


def test_no_push_goes_with_rerun(gating: Gate, capsys: pytest.CaptureFixture[str]) -> None:
    policy(GATES)
    assert approvals.main(["local-ci", "--record", "--no-push"]) == 1
    assert "--no-push goes with --rerun" in capsys.readouterr().out


# --- fix round 1: the verifier's Rerun line reaches the gate through `verdict` (C1) ---

REPORT = f"""# Review

Verifier: claude, same-family, pass 1

## Criteria

- A high-risk head merges on the rerun the verifier started: Before merge line 2, its check.

## Correctness

No findings.

Head: {HEAD}
VERDICT: 0 blocker, 0 major, 0 minor
SATISFIED: yes
Risk: RISK
"""


def report_file(tmp_path: Path, risk: str = "low — fine", rerun: str | None = None) -> Path:
    report = tmp_path / "report.md"
    report.write_text(
        REPORT.replace("RISK", risk) + (f"{approvals.RERUN_LINE}: {rerun}\n" if rerun else "")
    )
    return report


def test_the_posted_review_keeps_the_reports_rerun_line(
    reviewing: list[tuple[Any, ...]],
    gating: Gate,
    forge: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No CI: the verifier writes `Rerun: <commit>` after its `Head:` line, the lead posts the report with
    `verdict`, and `check merge` reads the review as posted: that head merges."""
    state, _ = gating
    policy(GATES)
    state["pull"] = judged_pr()
    assert approvals.main(["verdict", "7", str(report_file(tmp_path, rerun=RERUN_COMMIT))]) == 0
    posted = next(call[2] for call in reviewing if call[0] == "review")
    assert f"Rerun: {RERUN_COMMIT}" in posted["body"]
    capsys.readouterr()
    forge["records"] = [(RERUN_REF, RERUN_COMMIT)]
    forge["pushes"][RERUN_REF] = {"after": RERUN_COMMIT, "login": "worker", "app": False}
    state["pull"] = judged_pr(
        {**judging("low — fine"), "body": posted["body"]}, labels=("risk:high",)
    )
    assert check_merge(capsys) == (0, [])


def test_report_json_names_the_rerun_and_a_malformed_line_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert approvals.main(["report", "--json", str(report_file(tmp_path, rerun=RERUN_COMMIT))]) == 0
    assert json.loads(capsys.readouterr().out)["rerun"] == RERUN_COMMIT
    assert approvals.main(["report", str(report_file(tmp_path, rerun="the CI one"))]) == 1
    assert "write `Rerun: <commit>`, the full commit" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("settings", "risk", "rerun", "warned"),
    [
        ("loop:\n  ci: false\n", "high — a gate change", None, True),
        ("loop:\n  ci: false\n", "high — a gate change", RERUN_COMMIT, False),
        ("loop:\n  ci: false\n", "low — fine", None, False),
        ("", "high — a gate change", None, False),
        ("loop:\n  ci: false\nlevels:\n  high:\n    rerun: false\n", "high — x", None, False),
    ],
    ids=["no-ci-high-unnamed", "named", "low", "with-ci", "rerun-off"],
)
def test_report_warns_on_a_high_risk_report_naming_no_rerun_where_there_is_no_ci(
    gating: Gate,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    settings: str,
    risk: str,
    rerun: str | None,
    warned: bool,
) -> None:
    policy(settings)
    assert approvals.main(["report", str(report_file(tmp_path, risk, rerun))]) == 0
    err = capsys.readouterr().err
    assert ("names no `Rerun: <commit>`" in err) is warned


# --- fix round 1: check rerun applies check merge's exemptions (C4) ---


@pytest.mark.parametrize("case", ["revert", "bot", "incubating"])
def test_a_pr_that_owes_no_verifier_pass_needs_no_rerun_in_check_rerun_and_check_merge(
    gating: Gate,
    forge: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    state, _ = gating
    policy(GATES)
    pr = judged_pr(judging("low — fine"), labels=("risk:high",))
    said = {
        "revert": "it is an exact revert of #3",
        "bot": "a `bot` section of the policy names it",
        "incubating": "it is incubating and waives the verifier",
    }[case]
    if case == "revert":
        monkeypatch.setattr(approvals, "reverts", lambda _pull: (3, []))
    elif case == "bot":
        monkeypatch.setattr(approvals, "exempting", lambda _pull, _loop: {"paths": ["app.py"]})
    else:
        monkeypatch.setattr(approvals, "incubation", lambda _p, _i, _l: ({"verifier"}, []))
    state["pull"] = pr
    assert approvals.main(["check", "rerun", "7"]) == 0
    assert capsys.readouterr().out == f"PR #7: rerun: not needed ({said}) (rated high)\n"
    approvals.main(["check", "merge", "7"])
    assert not any(ASKED in line for line in capsys.readouterr().out.splitlines())


def test_check_rerun_reads_the_bases_policy(
    gating: Gate, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A PR's own edit of gatekeeper.md never decides whether CI reruns it."""
    state, _ = gating
    state["pull"] = {**judged_pr(judging("low — fine")), "baseRefName": "feature"}
    read: list[str | None] = []
    real = approvals.project

    def project(**kw: Any) -> Any:
        read.append(approvals.POLICY_BASE.get())
        if read[-1] == "feature":
            raise approvals.Refused("read")
        return real(**kw)

    monkeypatch.setattr(approvals, "project", project)
    assert approvals.main(["check", "rerun", "7"]) == 1
    assert read[-1] == "feature"


# --- fix round 1: the survivors of the verifier's mutation run (T1) ---


def test_a_pusher_the_forge_cannot_read_leaves_the_refusal_standing(
    gating: Gate,
    forge: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _ = gating
    policy(GATES)
    forge["records"] = [(RERUN_REF, RERUN_COMMIT)]

    def unreadable(_ref: str) -> Any:
        raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 502")

    monkeypatch.setattr(approvals, "pusher", unreadable)
    state["pull"] = judged_pr(judging("low — fine"), labels=("risk:high",))
    code, out = check_merge(capsys)
    assert code == 1
    assert out[0].startswith(f"{ASKED} origin holds 1 rerun record(s)")


def test_a_head_no_verdict_covers_asks_its_review_not_its_rerun(
    gating: Gate, forge: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The rerun is read only on a verdict that would land: an unreviewed head is told to get its review first."""
    state, _ = gating
    policy(GATES)
    state["pull"] = judged_pr(labels=("risk:high",))
    code, out = check_merge(capsys)
    assert code == 1
    assert not any(ASKED in line for line in out)


def test_a_far_brief_names_the_rerun_script_on_the_verifiers_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(approvals, "pull_request", lambda _n: pull(body="Closes #12.\n"))
    monkeypatch.setattr(approvals, "merge_base", lambda base, head: "b" * 40)
    monkeypatch.setattr(approvals, "project", lambda: (None, loop_md("loop:\n  ci: false\n")))
    root = Path(approvals.__file__).resolve().parents[4]
    monkeypatch.setattr(approvals, "toplevel", lambda: root)
    assert approvals.main(["brief", "7", "--far", "/there/repo"]) == 0
    [line] = [l for l in capsys.readouterr().out.splitlines() if l.startswith("- Rerun:")]
    script = "/there/repo/tmp/loop/pr7-v1-tree/gatekeeper/skills/gatekeeper/scripts/approvals.py"
    assert f"`uv run {script} local-ci --record --rerun`" in line


SKILLS = Path(__file__).resolve().parents[2]


def flat(path: str) -> str:
    return " ".join((SKILLS / path).read_text().split())


def test_the_skills_state_the_option_its_defaults_and_the_report_line() -> None:
    loop_skill = flat("loop/skills/engineering-loop/SKILL.md")
    assert (
        "gatekeeper.md `levels.<level>.rerun` is on (by default `high` and `critical`)"
        in loop_skill
    )
    assert "(the gatekeeper skill's policy.md, Independent rerun)" in loop_skill
    policy_md = (SKILLS / "gatekeeper/skills/gatekeeper/policy.md").read_text()
    assert "    rerun: false   # the worker's record is the proof\n" in policy_md
    assert policy_md.count("    rerun: true") == 2
    assert "`low` {policy, gates, rerun false}" in policy_md
    assert "## Independent rerun" in policy_md
    gate_skill = flat("gatekeeper/skills/gatekeeper/SKILL.md")
    assert "after the `Head:` line, a line `Rerun: <commit>`" in gate_skill


def test_verdict_warns_as_report_does_and_still_posts(
    reviewing: list[tuple[Any, ...]],
    gating: Gate,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state, _ = gating
    policy("loop:\n  ci: false\n")
    state["pull"] = judged_pr()
    assert approvals.main(["verdict", "7", str(report_file(tmp_path, "high — a gate change"))]) == 0
    assert "names no `Rerun: <commit>`" in capsys.readouterr().err
    assert any(call[0] == "review" for call in reviewing)


# --- fix round 2: check rerun writes nothing (C6) ---


def owners_label_after_the_edit() -> dict[str, Any]:
    """A spec carrying `needs-owner` and the owner's `approved:spec`, added after its last edit: adopted() records it."""
    spec = issue(1, "kind:enhancement", "area:web", "size:S", "needs-owner", "approved:spec")
    spec["timelineItems"] = {
        "nodes": [
            {
                "__typename": "LabeledEvent",
                "createdAt": LABELED,
                "actor": {"__typename": "User", "login": "octocat"},
                "label": {"name": "approved:spec"},
            }
        ]
    }
    return spec


def test_check_rerun_records_no_approval_and_still_answers(
    gating: Gate,
    forge: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under the workflow's read-only token an approval record is refused: `check rerun` never tries it, and prints
    its `rerun:` line."""
    state, comments = gating
    policy(GATES + "loop:\n  owner: octocat\n")
    tried: list[tuple[Any, ...]] = []

    def refused(*args: Any) -> None:
        tried.append(args)
        raise subprocess.CalledProcessError(
            1, "gh", stderr="Resource not accessible by integration (HTTP 403)"
        )

    monkeypatch.setattr(approvals, "approve", refused)
    spec = owners_label_after_the_edit()
    monkeypatch.setattr(approvals, "issue_node", lambda _n: spec)
    pr = judged_pr(judging("low — fine"), labels=("risk:high",))
    pr["closingIssuesReferences"] = {"nodes": [spec]}
    state["pull"] = pr
    assert approvals.main(["check", "rerun", "7"]) == 0
    assert capsys.readouterr().out == "PR #7: rerun: needed (rated high)\n"
    assert (tried, comments) == ([], [])


def test_the_read_only_adoption_is_the_same_decision_without_the_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    loop = loop_md("loop:\n  owner: octocat\n")
    recorded: list[tuple[Any, ...]] = []
    monkeypatch.setattr(approvals, "approve", lambda *args: recorded.append(args))
    spec = owners_label_after_the_edit()
    monkeypatch.setattr(approvals, "issue_node", lambda _n: spec)
    [read] = approvals.adopting([spec], loop, record=False)
    assert (recorded, capsys.readouterr().out) == ([], "")
    assert approvals.names(read) == {"kind:enhancement", "area:web", "size:S", "approved:spec"}
    assert "needs-owner" in approvals.names(spec)  # the issue read is left as it was
    approvals.adopted([spec], loop)
    assert [args[:2] for args in recorded] == [("spec", 1)]
    # the owner's label from before the edit: nothing to record either way, and the read-only form says nothing
    spec["timelineItems"]["nodes"][0]["createdAt"] = "2000-01-01T00:00:00Z"
    recorded.clear()
    capsys.readouterr()
    assert approvals.adopting([spec], loop, record=False) == [spec]
    assert (recorded, capsys.readouterr().out) == ([], "")
