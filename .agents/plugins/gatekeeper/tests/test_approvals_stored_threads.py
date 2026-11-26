"""approvals.py `stored`: two writers of one record in one process (threads, as two gates on one tree and command can
be) each write a file of their own, so neither's replace finds the other's already moved."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from approvals_harness import approvals
from test_approvals_gate_steps import steps  # noqa: F401  (a fixture)


def test_two_threads_storing_one_record_both_succeed(
    steps: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = {"tree": steps["tree"], "command": "gate", "exit": 0}
    both = threading.Barrier(2, timeout=10)
    real = approvals.os.replace

    def replace(source: Any, target: Any) -> None:
        both.wait()  # each has written its partial file before either moves one
        real(source, target)

    monkeypatch.setattr(approvals.os, "replace", replace)
    failed: list[BaseException] = []

    def write() -> None:
        try:
            approvals.stored(record)
        except BaseException as exc:  # noqa: BLE001  (the test reports it)
            failed.append(exc)

    writers = [threading.Thread(target=write) for _ in range(2)]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(20)
    assert failed == []
    where = approvals.recorded(record["tree"], record["command"])
    assert [f.name for f in where.parent.iterdir()] == [where.name]
