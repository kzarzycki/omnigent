"""approvals.py: a merge call that fails after GitHub merged the PR counts as landed, not refused (#923)."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest
from approvals_harness import (
    approvals,
    lands,  # noqa: F401
)

REFUSED = "Required status check is expected"


@pytest.mark.parametrize(
    ("reread", "why"),
    [
        ("open", REFUSED),
        ("another head", REFUSED),
        ("fails", f"{REFUSED}; re-reading the PR failed too: HTTP 502"),
    ],
)
def test_a_merge_call_failing_after_github_merged_lands_and_any_other_failure_is_refused(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch, reread: str, why: str
) -> None:
    """#7's merge call is cut off after GitHub merged it at its head; #8's fails and its re-read finds it still open,
    merged at a head other than the pinned one, or fails itself."""
    heads = {number: lands["open"](number, {f"{number}.txt": f"{number}\n"}) for number in (7, 8)}
    fake = approvals.github.gh

    def gh(*args: str, data: str | None = None) -> str:
        if args[:3] == ("pr", "merge", "7"):
            fake(*args, data=data)
            raise subprocess.CalledProcessError(1, args, stderr="unexpected EOF")
        if args[:3] == ("pr", "merge", "8"):
            raise subprocess.CalledProcessError(1, args, stderr=REFUSED)
        if args[:2] == ("api", "repos/{owner}/{repo}/pulls/8") and reread == "fails":
            raise subprocess.CalledProcessError(1, args, stderr="HTTP 502")
        if args[:2] == ("api", "repos/{owner}/{repo}/pulls/8") and reread == "another head":
            return json.dumps(
                {"merged": True, "head": {"sha": "0" * 40}, "merge_commit_sha": "1" * 40}
            )
        return fake(*args, data=data)

    monkeypatch.setattr(approvals.github, "gh", gh)
    code, lines = lands["land"]()
    assert code == 1, lines
    assert (
        f"PR #7: the merge call failed, but the PR is merged at {heads[7]} as {lands['squashes'][7]}"
        in lines
    )
    assert not any(line.startswith("PR #7: the merge was refused") for line in lines)
    assert any(
        line.startswith("PR #8: the merge was refused") and line.endswith(f": {why}")
        for line in lines
    )
    assert [n for n, body in lands["comments"] if body.startswith("Landed\n")] == [7]
