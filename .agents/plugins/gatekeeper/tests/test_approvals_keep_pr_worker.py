"""approvals.py: land finishes the kept --pr worker sessions of each PR it merged, detached (#943)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import (
    approvals,
    lands,  # noqa: F401
)


def test_a_land_that_merged_the_pr_finishes_its_workers(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    finished: list[int] = []
    monkeypatch.setattr(approvals, "archive_workers", finished.append)
    code, lines = lands["land"]("7")
    assert code == 0, lines
    assert lands["merged"]() == ["7"] and finished == [7]


def test_a_land_that_merged_nothing_finishes_no_worker(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    finished: list[int] = []
    monkeypatch.setattr(approvals, "archive_workers", finished.append)
    monkeypatch.setattr(
        approvals, "landable", lambda *_: approvals.Verdict((approvals.WAITING, True))
    )
    assert lands["land"]("7")[0] == approvals.WAITING
    assert finished == []


def test_archive_workers_starts_reap_merged_detached_and_does_not_wait(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started: list[tuple[list[str], dict[str, Any]]] = []

    class Slow:
        def __init__(self, cmd: list[str], **kw: Any) -> None:
            started.append((cmd, kw))

        def wait(self, *_: Any, **__: Any) -> int:
            raise AssertionError("land waited on the archive")

        communicate = poll = wait

    monkeypatch.setattr(approvals.subprocess, "Popen", Slow)
    approvals.archive_workers(7)
    ((cmd, kw),) = started
    assert cmd[2:] == ["reap", "--merged", "--pr", "7"]
    assert cmd[1].endswith("omnigent-agents/scripts/omnigent_agent.py")
    assert kw["start_new_session"] is True and kw["stdin"] == subprocess.DEVNULL
    out = capsys.readouterr().out
    assert "PR #7: finishing its worker sessions in the background, log " in out
    log = Path(out.rsplit("log ", 1)[1].strip())
    assert log.is_file()
    log.unlink()


def test_a_start_that_fails_is_a_printed_line_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*_: Any, **__: Any) -> None:
        raise OSError("no python")

    monkeypatch.setattr(approvals.subprocess, "Popen", refuse)
    approvals.archive_workers(7)  # raises nothing
    assert "PR #7: worker sessions not finished: no python" in capsys.readouterr().out


def test_a_checkout_without_the_script_prints_that_and_starts_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def never(*_: Any, **__: Any) -> None:
        raise AssertionError("started")

    monkeypatch.setattr(approvals.subprocess, "Popen", never)
    monkeypatch.setattr(approvals.Path, "is_file", lambda self: False)
    approvals.archive_workers(7)
    assert "PR #7: no worker sessions finished: omnigent_agent.py is not installed" in (
        capsys.readouterr().out
    )
