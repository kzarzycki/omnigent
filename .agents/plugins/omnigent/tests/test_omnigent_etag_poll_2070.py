"""The supervisor's list polls (rest_pages) send each page's last ETag, so a poll where nothing moved gets GitHub's 304,
which spends no quota, and still returns the kept body (#2070). A fake GitHub answers as the real one does, through the
real gh()."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_omnigent_agent import omnigent_agent as oa


class Hub:
    """GitHub's REST lists by path (without ``page``); `spent` counts the 200s, `free` the 304s."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, lists: dict[str, Any]) -> None:
        self.lists, self.spent, self.free = lists, 0, 0
        monkeypatch.setattr(oa, "ETAGS", {})
        monkeypatch.setattr(oa.subprocess, "run", self.run)

    def run(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        assert cmd[:3] == ["gh", "api", "-i"], cmd
        path, _, page = cmd[-1].rpartition("&page=")
        body = self.lists[path][int(page) - 1]
        text, tag = json.dumps(body), f'"v{len(json.dumps(body))}"'
        if f"If-None-Match: {tag}" in cmd:
            self.free += 1  # gh exits 1 on a 304, printing the status line with -i
            raise subprocess.CalledProcessError(
                1, cmd, output="HTTP/2.0 304 Not Modified\n\n", stderr="gh: HTTP 304"
            )
        self.spent += 1
        return subprocess.CompletedProcess(cmd, 0, stdout=f"HTTP/2.0 200 OK\nEtag: {tag}\n\n{text}")


def test_a_304_returns_the_cached_body(monkeypatch: pytest.MonkeyPatch) -> None:
    full = [{"n": i} for i in range(100)]
    hub = Hub(monkeypatch, {"repos/o/r/pulls?state=open&per_page=100": [full, [{"n": 100}]]})
    first = oa.rest_pages("repos/o/r/pulls?state=open&per_page=100")
    assert (hub.spent, hub.free) == (2, 0)
    again = oa.rest_pages("repos/o/r/pulls?state=open&per_page=100")
    assert again == first == full + [{"n": 100}]  # the kept bodies, both pages
    assert (hub.spent, hub.free) == (2, 2)  # a 304 per page, no quota


def test_a_check_runs_list_is_read_under_its_key(monkeypatch: pytest.MonkeyPatch) -> None:
    Hub(
        monkeypatch,
        {"repos/o/r/commits/x/check-runs?per_page=100": [{"check_runs": [{"name": "a"}]}]},
    )
    assert oa.rest_pages("repos/o/r/commits/x/check-runs", "check_runs") == [{"name": "a"}]


def test_kept_etags_survive_a_restart(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hub = Hub(monkeypatch, {"repos/o/r/pulls?per_page=100": [[{"n": 1}]]})
    oa.rest_pages("repos/o/r/pulls")
    kept = tmp_path / "x.etags.json"
    oa.etags_save(kept)
    oa.ETAGS.clear()  # a new process
    oa.etags_load(kept)
    assert oa.rest_pages("repos/o/r/pulls") == [{"n": 1}]
    assert (hub.spent, hub.free) == (1, 1)
    oa.etags_load(tmp_path / "missing.json")  # none kept: no error


def test_a_non_paging_endpoint_is_read_once_2343(monkeypatch: pytest.MonkeyPatch) -> None:
    """git/matching-refs ignores page=: the same 100 items come back for every page, which looped forever (#2343)."""
    full = [{"ref": f"refs/heads/loop/{i}"} for i in range(100)]
    calls: list[str] = []

    def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd[-1])
        assert len(calls) <= 10, "rest_pages never stopped"
        return subprocess.CompletedProcess(cmd, 0, stdout=f"HTTP/2.0 200 OK\n\n{json.dumps(full)}")

    monkeypatch.setattr(oa, "ETAGS", {})
    monkeypatch.setattr(oa.subprocess, "run", run)
    assert oa.rest_pages("repos/o/r/git/matching-refs/loop/") == full
    assert len(calls) == 2
