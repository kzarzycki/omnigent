"""approvals.py: `land.merge_commit_heads` lands a matching head with a merge commit, the rest squashed (#1029)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from approvals_harness import HEAD, TREE, approvals, loop_md, pull

SYNC = loop_md("land:\n  merge_commit_heads:\n    - sync/*\n")
BOTH = loop_md(
    "land:\n  signed_squash_to: main\n  allowed_signers: /dev/null\n  merge_commit_heads:\n    - sync/*\n"
)
UNSET = loop_md("land:\n  where: both\n")


def land(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, loop: str, branch: str
) -> list[tuple[str, ...]]:
    """merges() of one PR on `branch`: the `gh pr merge` calls it makes."""
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(approvals.github, "gh", lambda *args, **_k: calls.append(args) or "")
    monkeypatch.setattr(approvals, "pull_request", lambda _n: {"state": "MERGED"})
    monkeypatch.setattr(approvals, "git", lambda *args: TREE if args[0] == "rev-parse" else "")
    monkeypatch.setattr(approvals, "drop_records", lambda _heads: None)
    monkeypatch.setattr(approvals, "comment", lambda *_a: None)
    monkeypatch.setattr(approvals, "project", lambda: (None, loop))
    landed: list[Any] = [({**pull(), "headRefName": branch}, TREE)]
    approvals.merges(landed, "main", HEAD, loop, set())
    return [call for call in calls if call[:2] == ("pr", "merge")]


def test_a_matching_head_lands_with_a_merge_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (call,) = land(monkeypatch, tmp_path, SYNC, "sync/upstream")
    assert "--merge" in call and "--squash" not in call
    assert call[-2:] == ("--match-head-commit", pull()["headRefOid"])
    assert "lands as a merge" in capsys.readouterr().out


def test_a_head_that_does_not_match_lands_with_a_squash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (call,) = land(monkeypatch, tmp_path, SYNC, "feature/x")
    assert "--squash" in call and "--merge" not in call
    assert "lands as a squash" in capsys.readouterr().out


@pytest.mark.parametrize("loop", [UNSET, loop_md()])
def test_with_the_key_unset_the_command_is_unchanged(
    loop: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (call,) = land(monkeypatch, tmp_path, loop, "sync/upstream")
    assert call == ("pr", "merge", "7", "--squash", "--match-head-commit", pull()["headRefOid"])


def test_with_both_keys_set_a_matching_pr_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = land(monkeypatch, tmp_path, BOTH, "sync/upstream")
    assert calls == []
    out = capsys.readouterr().out
    assert "land.merge_commit_heads" in out and "land.signed_squash_to" in out
    assert "was refused" in out


def test_the_merge_gate_holds_a_matching_pr_under_both_keys() -> None:
    held = approvals.merge_method
    with pytest.raises(approvals.Refused, match="merge_commit_heads.*signed_squash_to"):
        held({**pull(), "headRefName": "sync/a"}, BOTH)
    assert held({**pull(), "headRefName": "other"}, BOTH) == "squash"


def test_the_key_alone_signs_nothing_and_one_signing_key_alone_is_refused() -> None:
    assert approvals.signed(SYNC) is None
    with pytest.raises(approvals.Refused, match="lacks `allowed_signers`"):
        approvals.settings(loop_md("land:\n  signed_squash_to: main\n"))


def test_a_signed_squash_prints_its_method_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    both = loop_md(
        "land:\n  signed_squash_to: main\n  allowed_signers: /dev/null\n  merge_commit_heads:\n    - sync/*\n"
    )
    monkeypatch.setattr(approvals, "signed_squash", lambda *_a: "c" * 40)
    land(monkeypatch, tmp_path, both, "feature/x")
    assert "PR #7 lands as a squash" in capsys.readouterr().out


def test_the_both_keys_refusal_is_returned_before_any_gate_runs() -> None:
    """proof_gaps(), the pre-land check, returns the refusal first, naming both keys, before the PR is gated."""
    found, issues = approvals.proof_gaps({**pull(), "headRefName": "sync/a"}, None, BOTH)
    assert issues is None
    (line,) = found
    assert "land.merge_commit_heads" in line and "land.signed_squash_to" in line
