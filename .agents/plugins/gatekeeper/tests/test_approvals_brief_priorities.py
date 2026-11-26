"""The priorities file is written once and carried to both roles (#1039): `brief --priorities F` quotes it, and
lead.md and worker.md state the step. No worker-brief helper exists (the lead pastes the file into the worker's
message), so the brief tool is the only one that quotes it."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from approvals_harness import LOOP, approvals, pull

ROLES = Path(__file__).resolve().parents[2] / "loop" / "skills" / "engineering-loop"


def flat(name: str) -> str:
    return " ".join((ROLES / name).read_text().split())


def test_the_brief_quotes_the_priorities_file_as_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(approvals, "pull_request", lambda _n: pull(body="Closes #12.\n"))
    monkeypatch.setattr(approvals, "merge_base", lambda base, head: "b" * 40)
    monkeypatch.setattr(approvals, "project", lambda: (None, LOOP))
    priorities = tmp_path / "priorities.md"
    text = "- every new condition has a test\n- the lookup is bounded\n    - indented\n- a later comment narrows X"
    priorities.write_text(text + "\n\n")
    assert approvals.main(["brief", "7", "--priorities", str(priorities)]) == 0
    out = capsys.readouterr().out
    assert out.split("\nPriorities:\n", 1)[1] == text + "\n"


def test_the_roles_state_the_step_with_the_file_named_once() -> None:
    lead, worker = flat("lead.md"), flat("worker.md")
    assert len(re.findall(r"priorities-<pr>\.md", lead)) == 1
    assert "priorities-<pr>" not in worker
    assert "write the verifier's priority checks once, from the spec and its comments" in lead
    assert "Before the first worker starts, write the verifier's priority checks once" in lead
    assert 'under "the verifier will check" (text, not a path' in lead
    assert "the priorities file from step 4 (it exists only in your checkout" in lead
    assert "and the priorities file already in that checkout" in lead
    assert "never a verdict, review text or the verifier's report" in lead
    assert 'A brief with "the verifier will check"' in worker
    assert "run each on your own head before you report" in worker
    assert "Read the spec's comments as well as its body" in worker
