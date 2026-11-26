"""`land <pr> --wait` on a GitHub-queue base waits for the PR's own running check (#1287), then for the queue's
merge (#2101)."""

from __future__ import annotations

from typing import Any

import pytest
from approvals_harness import approvals, lands  # noqa: F401  (fixture)
from test_approvals_github_queue import queue  # noqa: F401  (fixture)


def qualify(state: dict[str, Any], number: int, conclusion: str | None) -> None:
    rollup = state["pulls"][number]["commits"]["nodes"][0]["commit"]["statusCheckRollup"]
    status = "COMPLETED" if conclusion else "IN_PROGRESS"
    rollup["contexts"]["nodes"] = [
        {"__typename": "CheckRun", "name": "qualify", "status": status, "conclusion": conclusion}
    ]


def test_land_wait_on_a_queue_base_waits_for_the_prs_own_running_check_then_enqueues(
    queue: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = queue["opened"](7)
    qualify(queue, 7, None)
    slept: list[float] = []

    def nap(seconds: float) -> None:
        slept.append(seconds)
        qualify(queue, 7, "SUCCESS")
        if queue["enqueued"] and queue["pulls"][7]["state"] == "OPEN":
            queue["queue_merge"](7)  # the queue merges it while land waits on it

    monkeypatch.setattr(approvals.time, "sleep", nap)
    monkeypatch.setattr(approvals.github.random, "randint", lambda low, high: 0)
    code, lines = queue["land"]("7", "--wait")
    assert (code, queue["enqueued"]) == (0, [(7, head)]), lines
