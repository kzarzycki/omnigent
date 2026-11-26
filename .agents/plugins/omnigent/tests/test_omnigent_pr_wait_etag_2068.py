"""A registered PR wait is read through ETags (#2068): N polls of an unchanged PR make one full read of each resource
and N-1 conditional ones. A fake GitHub answers as the real one does, through the real gh()."""

import json
import subprocess
from typing import Any

import pytest
from test_omnigent_agent import omnigent_agent as oa

PULL = {"state": "open", "head": {"sha": "abc", "ref": "feat"}, "base": {"ref": "main"}}
RUNS = {"check_runs": [{"name": "gate", "status": "completed", "conclusion": "success"}]}
STATUS: dict[str, Any] = {"statuses": []}


class Hub:
    """GitHub's PR resources by path; `spent` counts the 200s, `free` the 304s."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.spent = self.free = 0
        self.bodies = {
            "repos/o/r/pulls/7": PULL,
            "repos/o/r/commits/abc/check-runs?per_page=100": RUNS,
            "repos/o/r/commits/abc/status": STATUS,
        }
        monkeypatch.setattr(oa, "ETAGS", {})
        monkeypatch.setattr(oa, "PR_VIEWS", {})
        monkeypatch.setattr(oa.subprocess, "run", self.run)

    def run(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        assert cmd[:3] == ["gh", "api", "-i"], cmd  # never a GraphQL `pr view`
        text = json.dumps(self.bodies[cmd[-1]])
        tag = f'"v{cmd[-1]}"'
        if f"If-None-Match: {tag}" in cmd:
            self.free += 1
            raise subprocess.CalledProcessError(
                1, cmd, output="HTTP/2.0 304 Not Modified\n\n", stderr="gh: HTTP 304"
            )
        self.spent += 1
        return subprocess.CompletedProcess(cmd, 0, stdout=f"HTTP/2.0 200 OK\nEtag: {tag}\n\n{text}")


def test_polls_of_an_unchanged_pr_are_one_full_read_and_the_rest_conditional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = Hub(monkeypatch)
    n = 5
    ends = [oa.pr_settled(7, "o/r") for _ in range(n)]
    assert ends == [(0, "PR #7 checks green (1)", "abc")] * n
    assert (hub.spent, hub.free) == (3, 3 * (n - 1))  # pull, check runs, status: once each


def test_the_read_carries_the_branches_the_supervisor_maps_a_wait_by(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Hub(monkeypatch)
    oa.pr_settled(7, "o/r")
    assert oa.PR_VIEWS[("o/r", 7)][1]["headRefName"] == "feat"
    assert oa.PR_VIEWS[("o/r", 7)][1]["baseRefName"] == "main"


def test_a_merged_pull_reads_as_merged_not_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    hub = Hub(monkeypatch)
    hub.bodies["repos/o/r/pulls/7"] = {**PULL, "state": "closed", "merged": True}
    assert oa.pr_settled(7, "o/r")[:2] == (0, "PR #7 merged")
