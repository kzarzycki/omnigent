"""approvals.py: a pin bump that pins the engineering pack by a commit SHA equal to a release tag's commit is judged by
that tag's rule, as a pin by the tag is; a SHA no release tag points at keeps main's rule (#1296)."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from approvals_harness import approvals, writers  # noqa: F401
from test_approvals_pin_bump import (
    LENIENT,
    MID,
    STRICT,
    TAG,
    TAGGED,
    bump,
    change,
    gate,
    lock,
    refused,
)

UNTAGGED = "b" * 40  # letters: YAML reads an all-digit SHA as a number


def test_a_sha_pin_at_a_tags_commit_is_judged_by_the_tags_rule_both_ways(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # main refuses a mid-tier pass on a high-risk change; the tag's rule names no tier, so it lands
    assert gate(MID, bump(monkeypatch, ref=TAGGED, rule=LENIENT), monkeypatch) == []
    # main lands a same-family pass; the tag's rule counts it as a lower tier, so it is refused
    files = bump(monkeypatch, ref=TAGGED, rule=STRICT)
    found = refused(gate("claude, same-family", files, monkeypatch))
    assert found and "lower tier (same-family)" in found


def test_an_uppercase_sha_pin_at_a_tags_commit_gets_the_tags_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert gate(MID, bump(monkeypatch, ref=TAGGED.upper()), monkeypatch) == []


def test_a_sha_no_release_tag_points_at_keeps_mains_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    files = bump(monkeypatch, ref=UNTAGGED, head_lock=lock(UNTAGGED, UNTAGGED))
    assert refused(gate(MID, files, monkeypatch))


def test_a_sha_ref_other_than_the_tags_commit_keeps_mains_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the lock's resolved_commit is the tag's, but the ref it pins is a SHA no release tag points at
    files = bump(monkeypatch, ref=UNTAGGED, head_lock=lock(UNTAGGED, TAGGED))
    assert refused(gate(MID, files, monkeypatch))


def test_a_sha_only_another_packages_tag_points_at_keeps_mains_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = bump(monkeypatch, ref=TAGGED, tags={"research-v1.0.0": TAGGED})
    assert refused(gate(MID, files, monkeypatch))


def test_two_tags_on_one_commit_pick_the_highest_version(monkeypatch: pytest.MonkeyPatch) -> None:
    # 0.100.0 is the highest by version, though not by text or by listing order; only its rule is strict
    tags = {
        "engineering-v0.100.0": TAGGED,
        "engineering-v0.99.0": TAGGED,
        "engineering-v0.9.0": TAGGED,
        "research-v9.0.0": TAGGED,
    }
    read: list[str] = []
    files = bump(monkeypatch, ref=TAGGED, tags=tags)
    base = approvals.file_at

    def file_at(at: str, path: str, repo: str | None = None) -> str | None:
        if repo == approvals.PACK_REPO:
            read.append(at)
            found = base(at, path, repo)
            return STRICT if found and at == "engineering-v0.100.0" else found
        return base(at, path, repo)

    monkeypatch.setattr(approvals, "file_at", file_at)
    found = refused(gate("claude, same-family", files, monkeypatch))
    assert found and "lower tier (same-family)" in found
    assert set(read) == {"engineering-v0.100.0"}


def test_a_sha_pin_with_an_extra_file_keeps_mains_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    files = bump(monkeypatch, ref=TAGGED, extra=(change("src/app.py"),))
    assert refused(gate(MID, files, monkeypatch))


def test_a_sha_pin_with_an_edited_vendored_file_keeps_mains_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert refused(gate(MID, bump(monkeypatch, ref=TAGGED, blob="0" * 40), monkeypatch))


@pytest.mark.parametrize("pinned", [TAG, UNTAGGED], ids=["the-tag", "another-sha"])
def test_a_manifest_pinning_another_ref_than_the_locks_sha_keeps_mains_rule(
    monkeypatch: pytest.MonkeyPatch, pinned: str
) -> None:
    assert refused(gate(MID, bump(monkeypatch, ref=TAGGED, pinned=pinned), monkeypatch))


def test_a_tag_list_that_cannot_be_read_keeps_mains_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    files = bump(monkeypatch, ref=TAGGED)

    def failing(_repo: str) -> Any:
        raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502")

    monkeypatch.setattr(approvals, "repo_tags", failing)
    assert refused(gate(MID, files, monkeypatch))


def test_a_tag_pin_reads_no_tag_list(monkeypatch: pytest.MonkeyPatch) -> None:
    files = bump(monkeypatch)

    def unread(_repo: str) -> Any:
        raise AssertionError("a pin by tag needs no tag list")

    monkeypatch.setattr(approvals, "repo_tags", unread)
    assert gate(MID, files, monkeypatch) == []
