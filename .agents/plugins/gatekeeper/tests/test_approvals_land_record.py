"""approvals.py: the local queue's `land` refuses a PR head with no record of a run of its tests (R6, #2178): it runs
no test, so a merge with neither a queue run nor such a record of each PR is refused, naming the missing record and
the command that makes it. The record is the one `affected-tests --record` writes, loop.md's `Affected tests:` command
narrowed against the PR's merge base; a project without that line reads its gate's record (`local-ci --record`)."""

from __future__ import annotations

import shlex
from typing import Any

import pytest
from approvals_harness import (
    approvals,
    git_in,
    lands,  # noqa: F401
)

GATE = "the gate"


@pytest.fixture
def records(monkeypatch: pytest.MonkeyPatch) -> dict[str, set[str]]:
    """Each head's recorded commands on origin (GATE for the gate's record); gate_record answers for them as
    `gate-record <head> [-- <words>]` does, its words joined back the way it joins a narrowed command."""
    held: dict[str, set[str]] = {}

    def gate_record(head: str, words: list[str]) -> int:
        if words[-1:] == ["(narrowed)"]:
            command = shlex.join(words[:-1]) + " (narrowed)"
        else:
            command = shlex.join(words) if words else GATE
        if command in held.get(head, set()):
            print(f"recorded: Tree t on {head[:12]}: {command}")
            return 0
        print(f"no record of `{command}` on tree t: run it")
        return 1

    monkeypatch.setattr(approvals, "gate_record", gate_record)
    return held


def affected_line(lands: dict[str, Any]) -> None:
    """loop.md gains `Affected tests: true`, a narrowed run whose record `affected-tests --record` writes."""
    loop = lands["work"] / "docs" / "agents" / "loop.md"
    loop.write_text(loop.read_text() + "\nAffected tests: true\n")


def narrowed(lands: dict[str, Any], head: str) -> str:
    """The narrowed command `affected-tests --record` keys the head's record by: against its merge base with main."""
    point = git_in(lands["work"], "merge-base", "origin/main", head).strip()
    return f"GATE_BASE={point} true (narrowed)"


def test_without_an_affected_tests_line_an_unrecorded_head_is_refused_naming_local_ci(
    lands: dict[str, Any], records: dict[str, set[str]]
) -> None:
    head = lands["open"](7, {"7.txt": "7\n"})
    code, lines = lands["land"]("7")
    assert code == 1, lines
    assert lands["merged"]() == []
    said = "\n".join(lines)
    assert f"PR #7: not landed: no pass record on its head {head[:12]}" in said
    assert f"no record of `{GATE}` on tree t" in said
    assert "run `approvals.py local-ci --record` on that head" in said


def test_without_an_affected_tests_line_a_gate_record_lands_beside_a_refused_head(
    lands: dict[str, Any], records: dict[str, set[str]]
) -> None:
    records[lands["open"](7, {"7.txt": "7\n"})] = {GATE}
    lands["open"](8, {"8.txt": "8\n"})
    code, lines = lands["land"]()
    assert lands["merged"]() == ["7"], lines
    assert any(line.startswith("PR #8: not landed: no pass record") for line in lines)
    assert code == 0  # a bare land: the refused PR is said, and only a named one's exit counts
    assert lands["land"]("8")[0] == 1


def test_a_gate_record_alone_is_refused_where_the_affected_tests_run_is_the_record(
    lands: dict[str, Any], records: dict[str, set[str]]
) -> None:
    """The gate may run no test (a lint-only check), so only the `Affected tests:` run's record lets the head land."""
    affected_line(lands)
    head = lands["open"](7, {"7.txt": "7\n"})
    records[head] = {GATE}
    code, lines = lands["land"]("7")
    assert code == 1, lines
    assert lands["merged"]() == []
    said = "\n".join(lines)
    assert f"no record of `{narrowed(lands, head)}` on tree t" in said
    assert "run `approvals.py affected-tests --record` on that head" in said


def test_a_record_of_the_affected_tests_run_lands(
    lands: dict[str, Any], records: dict[str, set[str]]
) -> None:
    affected_line(lands)
    head = lands["open"](7, {"7.txt": "7\n"})
    records[head] = {narrowed(lands, head)}
    code, lines = lands["land"]("7")
    assert code == 0, lines
    assert lands["merged"]() == ["7"]


def test_a_head_with_no_merge_base_is_refused_since_its_record_cant_be_found(
    lands: dict[str, Any], records: dict[str, set[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `Affected tests:` record is keyed by the merge base: without one, no record is found, so nothing lands."""
    affected_line(lands)
    head = lands["open"](7, {"7.txt": "7\n"})
    records[head] = {GATE, narrowed(lands, head)}
    monkeypatch.setattr(approvals, "merge_base_of", lambda _base, _head: None)
    code, lines = lands["land"]("7")
    assert code == 1, lines
    assert lands["merged"]() == []
    assert any(
        "no merge base with origin/main, so no `Affected tests:` record is found" in line
        for line in lines
    )
