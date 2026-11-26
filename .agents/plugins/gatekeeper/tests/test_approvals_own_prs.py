"""approvals.py: land starts omnigent_agent.py `own-pr` for each PR it names, so a dequeue reaches the lead (#2161)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from approvals_harness import approvals


def test_own_prs_starts_one_run_naming_every_pull_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "omnigent_agent.py"
    script.write_text("")
    started: list[list[str]] = []
    monkeypatch.setattr(approvals, "agent_script", lambda: script)
    monkeypatch.setattr(approvals, "own_started", set())
    monkeypatch.setattr(approvals.subprocess, "Popen", lambda argv, **_k: started.append(argv))
    pulls: list[dict[str, Any]] = [
        {"number": 7, "baseRepository": {"nameWithOwner": "o/r"}},
        {"number": 9, "baseRepository": {"nameWithOwner": "o/r"}},
        {"number": 11},
    ]
    approvals.own_prs(pulls)
    approvals.own_prs(pulls)  # a land --wait poll again: nothing new to start
    assert [argv[2:] for argv in started] == [["own-pr", "o/r#7", "o/r#9"]]


def test_own_prs_is_silent_without_the_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals, "agent_script", lambda: tmp_path / "missing.py")
    monkeypatch.setattr(approvals.subprocess, "Popen", lambda *a, **k: pytest.fail("started"))
    approvals.own_prs([{"number": 7, "baseRepository": {"nameWithOwner": "o/r"}}])
