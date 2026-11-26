"""A run answers a repeat of the same plain `gh api` read from memory (`reads`) until it writes (#2016), and a wait
that re-reads to see someone else's change never answers itself: settled_head's 10 s re-reads reach GitHub each time,
so a head GitHub shows 25 s after the push is still found on the fourth read."""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any

import pytest
from approvals_harness import approvals

github = approvals.github
OLD, NEW = "a" * 40, "b" * 40


@pytest.mark.parametrize("lag", [5, 15, 25])
def test_settled_head_reads_github_on_every_try(lag: int, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"t": 1000.0}
    calls: list[float] = []

    def run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        assert cmd[:2] == ["gh", "api"] and cmd[-1].endswith("/pulls/7"), cmd
        calls.append(clock["t"] - 1000.0)
        head = NEW if clock["t"] - 1000.0 >= lag else OLD
        body = {
            "number": 7,
            "state": "open",
            "merged": False,
            "base": {"ref": "main"},
            "head": {"sha": head},
            "mergeable": True,
        }
        return subprocess.CompletedProcess(cmd, 0, json.dumps(body), "")

    monkeypatch.setattr(github.subprocess, "run", run)
    monkeypatch.setattr(github.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(github.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    github.forget()
    assert approvals.settled_head(7, NEW) == NEW
    assert calls == [
        10.0 * n for n in range(len(calls))
    ]  # one GitHub read per try, none from memory


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    seen: list[tuple[str, ...]] = []

    def run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(tuple(cmd[1:]))
        if "rate_limit" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 0, f"4000 5000 {int(time.time()) + 600}", "")
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    monkeypatch.setattr(github.subprocess, "run", run)
    github.forget()
    return seen


def reads_of(seen: list[tuple[str, ...]], path: str) -> int:
    return sum(1 for call in seen if call[-1] == path)


def test_a_guarded_quota_read_neither_answers_nor_forgets(
    counted: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """While land --wait runs, each gh call's guard reads `rate_limit` first; that read must not empty `reads`."""
    monkeypatch.setattr(github, "wait_until", time.monotonic() + 3600)
    monkeypatch.setattr(github, "QUOTA_EVERY", 0)  # the guard reads the bucket before every call
    github.gh("api", "repos/o/r/pulls/1")
    github.gh("api", "repos/o/r/pulls/2")  # a new read: its guard reads the bucket first
    github.gh("api", "repos/o/r/pulls/1")
    assert reads_of(counted, "repos/o/r/pulls/1") == 1
    assert sum(1 for call in counted if "rate_limit" in " ".join(call)) >= 2


@pytest.mark.parametrize("write", [("--field=k=v",), ("--raw-field=k=v",), ("--input=body.json",)])
def test_a_long_field_spelling_is_a_write(
    counted: list[tuple[str, ...]], write: tuple[str, ...]
) -> None:
    github.gh("api", "repos/o/r/pulls/1")
    github.gh("api", "repos/o/r/issues/1/comments", *write)
    github.gh("api", "repos/o/r/pulls/1")
    assert reads_of(counted, "repos/o/r/pulls/1") == 2  # the write emptied the memory
    assert not github.plain_read(("api", "repos/o/r/issues/1/comments", *write))


def test_a_call_with_data_is_never_kept_and_forgets(counted: list[tuple[str, ...]]) -> None:
    github.gh("api", "repos/o/r/pulls/1")
    github.gh("api", "repos/o/r/pulls/2", data="{}")
    github.gh("api", "repos/o/r/pulls/2", data="{}")
    github.gh("api", "repos/o/r/pulls/1")
    assert (
        reads_of(counted, "repos/o/r/pulls/2") == 2 and reads_of(counted, "repos/o/r/pulls/1") == 2
    )
