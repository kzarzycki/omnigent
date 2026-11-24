"""gatekeeper.py: the policy reaches the tracker, the forge and the evidence only through their ports, an adapter per
port named by the policy's `adapters`; its contract verbs, today's as aliases, and `report --json` (#614)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from approvals_harness import (
    HEAD,
    HELD,
    SCRIPTS,
    TRACKER,
    approvals,
    approved,
    gatekeeper_md,
    git_in,
    note,
    pull,
    record,
    status,
    verdict_on,
)
from test_approvals import REPORT

# every name through which the policy calls a port, as gatekeeper.py defines it, before the autouse fixtures above
# stub the GitHub ones: the fake adapter's test puts each back, so its calls reach the ports
FACADES = {
    name: getattr(approvals, name)
    for name in (
        *(op for ops in approvals.PORTS.values() for op in ops if hasattr(approvals, op)),
        "issues_of",
        "merged_high",
        "open_pulls",
    )
}
ADAPTED = gatekeeper_md("adapters:\n  tracker: memory\n  forge: memory\n  evidence: memory\n")


class Memory:
    """An in-memory tracker, forge and evidence store, as another team's adapter plugs in: each port a namespace of its
    operations over this state, every call recorded in `calls`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.issues: dict[int, dict[str, Any]] = {}
        self.pulls: dict[int, dict[str, Any]] = {}
        self.statuses: dict[str, list[dict[str, Any]]] = {}
        self.comments: list[tuple[int, str]] = []
        self.labels: list[tuple[int, str, bool]] = []
        handlers = {
            "issue_node": lambda n: self.issues[n],
            "issues": lambda numbers: [self.issues[n] for n in numbers if n in self.issues],
            "comment": lambda n, body: self.comments.append((n, body)),
            "label": lambda n, name, add: self.labels.append((n, name, add)),
            "assign": lambda n, login, add: None,
            "close_issue": lambda n: None,
            "pull_request": lambda n: self.pulls[n],
            "pull_status": lambda n: self.pulls[n],
            "queue_watch": lambda numbers: {n: self.pulls[n] for n in numbers},
            "current_pr": lambda: None,
            "pr_changes": lambda n: [],
            "merged_pull": lambda n: None,
            "merge_base": lambda base, head: base,
            "diff_lines": lambda base, head: {},
            "tree": lambda sha: None,
            "head_pushed": lambda pull: "2026-10-01T10:00:00Z",
            "merged_labelled": lambda branch, name: [],
            "open_pulls": lambda limit: [],
            "open_pull_files": lambda limit: [],
            "open_heads": list,
            # the gate's own status under either name is never waited on
            "required_checks": lambda base: {"check", approvals.APPROVALS, approvals.GATEKEEPER},
            "default_branch": lambda: "main",
            "viewer": lambda: "loop",
            "writer": lambda login: login == "loop",
            "github_repo": lambda url: None,
            "one_repo": lambda: None,
            "ready": lambda n, undo=False: None,
            "merge": lambda n, method, head: None,
            "enqueue_pull": lambda number, node, head, method: None,
            "dequeue_pull": lambda number, node: None,
            "commit_checks": lambda ref: {},
            "run_info": lambda run: {},
            "run_jobs": lambda run: [],
            "workflow_runs": lambda workflow, branch=None, head=None: [],
            "run_artifact": lambda run, name: None,
            "compare": lambda base, head: {"status": "identical", "ahead_by": 0, "behind_by": 0},
            "job_log": lambda job: "",
            "commit_tree": lambda sha: sha,
            "repo_tags": lambda repo: {},
            "tag_tree": lambda repo, tag: None,
            "file_at": lambda ref, path, repo=None: None,
            "statuses": lambda sha: self.statuses.get(sha, []),
            "post_status": lambda sha, context, description, state="success": None,
            "post_review": lambda pr, review: None,
            "minimize": lambda comment_id: None,
        }
        for name, operations in approvals.PORTS.items():
            setattr(
                self,
                name,
                SimpleNamespace(**{op: self.recorded(op, handlers[op]) for op in operations}),
            )

    def recorded(self, op: str, handler: Any) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((op, args))
            return handler(*args, **kwargs)

        return call


@pytest.fixture
def memory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Memory:
    """A checkout whose gatekeeper.md names the memory adapter for every port, with the gatekeeper's port calls as it
    defines them (FACADES), and no gh on PATH: only git, which the policy runs itself."""
    agents = tmp_path / "docs" / "agents"
    agents.mkdir(parents=True)
    (agents / "issue-tracker.md").write_text(TRACKER)
    (agents / "gatekeeper.md").write_text(ADAPTED)
    for name, facade in FACADES.items():
        monkeypatch.setattr(approvals, name, facade)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = shutil.which("git")
    assert git
    (bin_dir / "git").symlink_to(git)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.chdir(tmp_path)
    adapter = Memory()
    monkeypatch.setitem(approvals.ADAPTERS, "memory", adapter)
    return adapter


def test_check_merge_runs_end_to_end_through_an_in_memory_tracker_forge_and_evidence(
    memory: Memory, capsys: pytest.CaptureFixture[str]
) -> None:
    """A high-risk PR closing #1, which only its body names, waits for the owner's go until the evidence holds it;
    every read and write goes through the memory adapter's ports, and none reaches gh."""
    memory.issues[1] = approved(1, "area:web", "kind:enhancement")
    memory.pulls[7] = pull(
        body="Closes #1\n\n## Evidence\n\nlog line\n",
        labels=("risk:high",),
        reviews=(verdict_on(HEAD),),
        comments=(note("Approved by the owner.\n" + record("merge", "owner", head=HEAD)),),
    )
    assert approvals.main(["check", "merge", "7"]) == 3
    out = capsys.readouterr().out
    assert HELD in out
    memory.statuses[HEAD] = [status("owner", f"The owner's go on {HEAD[:12]}")]
    assert approvals.main(["check", "merge", "7"]) == 0
    assert capsys.readouterr().out == f"PR #7: the go on {HEAD} is the owner's\n"
    called = {op for op, _args in memory.calls}
    assert {"one_repo", "pull_request", "issues", "required_checks", "statuses"} <= called
    assert ("issues", ([1],)) in memory.calls


def test_a_port_the_policy_names_no_adapter_for_is_refused_naming_it() -> None:
    with pytest.raises(approvals.Refused) as refused:
        approvals.settings(approvals.Policy(gatekeeper_md("adapters:\n  forge: gitlab\n")))
    assert str(refused.value) == (
        "gatekeeper.md's `adapters.forge` names 'gitlab', which no adapter implements: github"
    )
    with pytest.raises(approvals.Refused, match="unknown setting `adapters.board`"):
        approvals.settings(approvals.Policy(gatekeeper_md("adapters:\n  board: github\n")))


def test_every_port_defaults_to_github_and_an_adapter_lacking_an_operation_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chosen = approvals.adapters(approvals.Policy(gatekeeper_md("adapters:\n  forge: github\n")))
    assert chosen == {name: getattr(approvals.github, name) for name in approvals.PORTS}
    partial = Memory()
    del partial.evidence.minimize
    monkeypatch.setitem(approvals.ADAPTERS, "partial", partial)
    with pytest.raises(
        approvals.Refused, match="the partial adapter's evidence port lacks minimize"
    ):
        approvals.adapters(approvals.Policy(gatekeeper_md("adapters:\n  evidence: partial\n")))


def test_report_json_gives_the_reports_facts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Today's report fixture, read offline as `report` reads it."""
    monkeypatch.setattr(approvals, "one_repo", lambda: pytest.fail("report --json reached out"))
    report = tmp_path / "report.md"
    report.write_text(REPORT)
    assert approvals.main(["report", "--json", str(report)]) == 0
    facts = json.loads(capsys.readouterr().out)
    assert facts == {
        "criteria": "- A draft never merges on its skipped checks: Before merge line 1, its check.",
        "accepted_on_evidence": [],
        "verifier": {"family": "claude, same-family", "pass": 2, "qualifier": ""},
        "head": HEAD,
        "verdict": {"blocker": 1, "major": 0, "minor": 2},
        "satisfied": False,
        "risk": {"level": "low", "reason": "a test fixture"},
        "logic_change": None,
        "scope": None,
        "findings": [
            {
                "id": "C1",
                "severity": "blocker",
                "path": "app.py",
                "line": "12",
                "title": "a draft merges on its skipped checks",
                "body": "- Failing input: a draft.",
            },
            {
                "id": "C2",
                "severity": "minor",
                "path": "app.py",
                "line": "40",
                "title": "a line outside the diff",
                "body": "Evidence: the run.",
            },
            {
                "id": "S1",
                "severity": "minor",
                "path": "docs/old.md",
                "line": "3",
                "title": "a file outside the diff",
                "body": "The doc.",
            },
        ],
    }


def test_report_json_reads_a_satisfied_report_with_a_logic_change_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "report.md"
    report.write_text(
        f"Verifier: codex/terra-6.1/high, pass 3: mid\n\n## Criteria\n\n- Billing holds.\n\nHead: {HEAD}\n"
        "VERDICT: 0 blocker, 0 major, 0 minor\nSATISFIED: yes\nRisk: high — billing\n"
        "Logic change: yes — the merge order\n"
    )
    assert approvals.main(["report", "--json", str(report)]) == 0
    facts = json.loads(capsys.readouterr().out)
    assert (facts["verifier"], facts["satisfied"], facts["risk"], facts["logic_change"]) == (
        {"family": "codex/terra-6.1/high", "pass": 3, "qualifier": "mid"},
        True,
        {"level": "high", "reason": "billing"},
        "yes — the merge order",
    )
    assert facts["findings"] == []


def test_report_json_refuses_an_unfinished_report_as_report_does(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "report.md"
    report.write_text(REPORT.replace("SATISFIED: no\n", ""))
    assert approvals.main(["report", "--json", str(report)]) == 1
    assert capsys.readouterr().out == (
        f"{report} has no `VERDICT:` or `SATISFIED:` line: the report is unfinished\n"
    )


@pytest.mark.parametrize(
    ("today", "contract", "called"),
    [
        (
            ["approve", "spec", "3", "--by", "owner"],
            ["record", "approval", "spec", "3", "--by", "owner"],
            "approve",
        ),
        (["verdict", "7", "r.md"], ["record", "verdict", "7", "r.md"], "verdict"),
        (["local-ci", "--status", HEAD], ["record", "checks", "--status", HEAD], "local_ci_status"),
    ],
)
def test_todays_verbs_run_the_record_verbs(
    monkeypatch: pytest.MonkeyPatch, today: list[str], contract: list[str], called: str
) -> None:
    """`approve`, `verdict` and `local-ci` are aliases of `record approval`, `record verdict` and `record checks`."""
    monkeypatch.setattr(approvals, "one_repo", lambda: None)
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(approvals, called, lambda *args: calls.append(args) or 0)
    assert approvals.main(today) == 0
    assert approvals.main(contract) == 0
    assert len(calls) == 2 and calls[0] == calls[1]


def test_approvals_py_imported_is_the_gatekeeper(tmp_path: Path) -> None:
    """A script that imports approvals (board.py, labels.py, mutate.py) gets the gatekeeper's names in one namespace: a
    name set on it is the one the gatekeeper's own functions read."""
    code = (
        f"import sys\nsys.path.insert(0, {str(SCRIPTS)!r})\nimport approvals\n"
        "assert approvals.READY == 'approved:spec'\n"
        "approvals.toplevel = lambda: 'here'\n"
        "assert approvals.check.__globals__['toplevel']() == 'here'\n"
        "print('ok')\n"
    )
    run = subprocess.run(
        [sys.executable, "-B", "-c", code],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
    )
    assert (run.returncode, run.stdout, run.stderr) == (0, "ok\n", "")


def test_base_drift_names_the_running_github_adapter_when_it_differs_from_the_bases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adapter judges a PR as the gatekeeper does, so a PR's own copy of it is refused as one of the gatekeeper is."""
    adapter = tmp_path / "scripts" / "github_adapter.py"
    adapter.parent.mkdir()
    adapter.write_text(Path(approvals.github.__file__).read_text())
    git_in(tmp_path, "init", "-q")
    git_in(tmp_path, "add", "-A")
    git_in(tmp_path, "-c", "user.name=t", "-c", "user.email=t@invalid", "commit", "-qm", "base")
    git_in(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    monkeypatch.setattr(approvals, "toplevel", lambda: tmp_path)
    monkeypatch.setattr(approvals.github, "__file__", str(adapter))
    assert approvals.base_drift("main") == []
    adapter.write_text(adapter.read_text() + "\n# a PR's own edit\n")
    assert approvals.base_drift("main") == ["scripts/github_adapter.py"]


@pytest.mark.parametrize("refused", [False, True], ids=["graphql", "rest"])
def test_the_default_branch_is_read_over_graphql_or_rest(
    monkeypatch: pytest.MonkeyPatch, refused: bool
) -> None:
    """`land` with no PR named lands into the repository's default branch, read over REST where GraphQL is refused."""
    monkeypatch.setattr(approvals.github, "_rest_only", refused)
    answers = {
        ("repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"): "trunk\n",
        ("api", "repos/{owner}/{repo}"): json.dumps({"default_branch": "trunk"}),
    }
    monkeypatch.setattr(approvals.github, "gh", lambda *args, data=None: answers[args])
    assert approvals.default_branch() == "trunk"


def test_open_pulls_asks_the_forge_for_up_to_open_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """`check build` compares a PR with up to OPEN_LIMIT open PRs, and says when the list may have left one out."""
    monkeypatch.setattr(approvals, "open_pulls", FACADES["open_pulls"])
    asked: list[int] = []
    monkeypatch.setattr(approvals.github, "open_pulls", lambda limit: asked.append(limit) or [])
    assert approvals.open_pulls() == []
    assert asked == [approvals.OPEN_LIMIT]
