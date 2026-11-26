"""A PR's REST file list is read once per run, not once per caller: a `gh pr list` or GraphQL call between callers
doesn't drop it, and neither does time. Only a write, or a wait's read_afresh(), makes the next ask GitHub's; conftest's
forget() before each test drops the last test's."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest
from approvals_harness import approvals

github = approvals.github


class Hub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        args = tuple(cmd[1:])
        self.calls.append(args)
        path = " ".join(args)
        if "pulls/7/files" in path:
            out = json.dumps({"filename": "a.py", "status": "modified"})
        elif "pulls/7/reviews" in path or "timeline" in path:
            out = ""
        elif path.endswith("pulls/7"):
            out = json.dumps(
                {
                    "node_id": "n",
                    "number": 7,
                    "title": "t",
                    "state": "open",
                    "head": {"ref": "h", "sha": "a" * 40, "repo": {"full_name": "o/r"}},
                    "base": {"ref": "main", "sha": "b" * 40, "repo": {"full_name": "o/r"}},
                }
            )
        else:
            out = "[]"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def files(self) -> int:
        return sum("pulls/7/files" in " ".join(call) for call in self.calls)


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch) -> Hub:
    fake = Hub()
    monkeypatch.setattr(github.subprocess, "run", fake)
    monkeypatch.setattr(github, "wait_until", None)
    return fake


def test_many_callers_in_one_run_read_a_prs_files_once(hub: Hub) -> None:
    for _ in range(5):
        github.pr_changes(7)
        github.gh("pr", "list", "--state", "open")  # empties `reads`, not `files_read`
        github.gh("api", "graphql", "-f", "query=query { viewer { login } }")
        try:
            github.rest_pull(7)
        except KeyError, TypeError:
            pass  # the fake PR answers no more than the files and the PR need
    assert hub.files() == 1, hub.calls


def test_a_write_or_a_wait_makes_the_next_read_github_s(hub: Hub) -> None:
    github.pr_changes(7)
    github.gh("api", "-X", "POST", "repos/{owner}/{repo}/issues/7/comments", "-f", "body=x")
    github.pr_changes(7)
    assert hub.files() == 2
    github.read_afresh()
    github.pr_changes(7)
    assert hub.files() == 3


def test_a_long_run_still_reads_a_prs_files_once(hub: Hub, monkeypatch: pytest.MonkeyPatch) -> None:
    github.pr_changes(7)
    later = github.time.monotonic() + 10 * github.READ_TTL
    monkeypatch.setattr(github.time, "monotonic", lambda: later)
    github.pr_changes(7)
    assert hub.files() == 1, hub.calls


def test_open_pull_files_reads_a_big_prs_files_through_the_run_s_one_read(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = [{"path": f"f{i}"} for i in range(100)]
    listing = json.dumps(
        [{"number": 7, "baseRefName": "main", "headRefOid": "a" * 40, "files": big}]
    )
    inner = hub.__call__

    def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        if cmd[1:3] == ["pr", "list"]:
            hub.calls.append(tuple(cmd[1:]))
            return subprocess.CompletedProcess(cmd, 0, listing, "")
        return inner(cmd, **kw)

    monkeypatch.setattr(github.subprocess, "run", run)
    for _ in range(3):
        github.open_pull_files(10)
    assert hub.files() == 1, hub.calls
