"""approvals.py: `land` posts the `land` status on a head right before it merges it, and on no head it doesn't merge (#1289)."""

from __future__ import annotations

import re
import subprocess
from typing import Any

import pytest
from approvals_harness import (
    approvals,
    git_in,
    lands,  # noqa: F401
)


@pytest.fixture
def posted(lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Every status land posts, in order with its merge calls (`lands["calls"]`)."""
    seen: list[tuple[Any, ...]] = []

    def post(head: str, context: str, description: str, state: str = "success") -> None:
        seen.append((head, context, description, state, len(lands["calls"])))

    monkeypatch.setattr(approvals, "post_status", post)
    return seen


def test_a_land_that_merges_posts_land_on_the_head_before_the_merge_call(
    lands: dict[str, Any], posted: list[tuple[Any, ...]]
) -> None:
    head = lands["open"](7, {"7.txt": "7\n"})
    code, lines = lands["land"]()
    assert code == 0, lines
    [(sha, context, description, state, merges_before)] = [p for p in posted if p[1] == "land"]
    assert (sha, context, state) == (head, "land", "success")
    assert re.search(r"nonce [0-9a-f]{16}$", description), description
    # the nonce is the one in the lock commit this land pushed
    nonce = description.rsplit(" ", 1)[-1]
    assert f"nonce {nonce}" in git_in(lands["origin"], "log", "--format=%b", "loop-landing/main")
    assert not any(call[:2] == ("pr", "merge") for call in lands["calls"][:merges_before])
    assert lands["calls"][merges_before][:2] == ("pr", "merge")


def test_a_land_that_does_not_merge_posts_no_land_status(
    lands: dict[str, Any], posted: list[tuple[Any, ...]]
) -> None:
    lands["open"](7, {"7.txt": "7\n"}, reviews=())
    code, lines = lands["land"]("7")
    assert code == 1, lines
    assert not any(call[:2] == ("pr", "merge") for call in lands["calls"])
    assert [p for p in posted if p[1] == "land"] == []


def test_a_merge_github_refuses_turns_the_land_status_to_an_error(
    lands: dict[str, Any], posted: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    head = lands["open"](7, {"7.txt": "7\n"})
    refuse_merges(monkeypatch, "Review required")
    code, lines = lands["land"]()
    assert code == 1, lines
    assert [(p[0], p[3]) for p in posted if p[1] == "land"] == [(head, "success"), (head, "error")]


def test_a_land_status_that_fails_to_post_stops_the_merge(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure that is no refusal (GitHub unreachable, a 5xx) stops the land before its merge call."""
    lands["open"](7, {"7.txt": "7\n"})

    def post(head: str, context: str, description: str, state: str = "success") -> None:
        if context == "land":
            raise subprocess.CalledProcessError(1, ("gh",), stderr="HTTP 502")

    monkeypatch.setattr(approvals, "post_status", post)
    code, lines = lands["land"]()
    assert code == 1, lines
    assert not any(call[:2] == ("pr", "merge") for call in lands["calls"])


def test_a_land_status_github_refuses_still_merges(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused write (a 403, as a cloud session's proxy answers) is printed as not written and the PR merges all
    the same (#1917)."""
    head = lands["open"](7, {"7.txt": "7\n"})

    def post(sha: str, context: str, description: str, state: str = "success") -> None:
        if context == "land":
            raise subprocess.CalledProcessError(1, ("gh",), stderr="HTTP 403")

    monkeypatch.setattr(approvals, "post_status", post)
    code, lines = lands["land"]()
    assert code == 0, lines
    assert any(call[:2] == ("pr", "merge") for call in lands["calls"])
    assert any(f"the `land` status on {head} was not written" in line for line in lines), lines


def refuse_merges(monkeypatch: pytest.MonkeyPatch, stderr: str) -> None:
    fake = approvals.github.gh

    def gh(*args: str, data: str | None = None) -> str:
        if args[:2] == ("pr", "merge"):
            raise subprocess.CalledProcessError(1, args, stderr=stderr)
        return fake(*args, data=data)

    monkeypatch.setattr(approvals.github, "gh", gh)


def test_a_long_refusal_still_withdraws_the_land_status_within_githubs_140_characters(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    head = lands["open"](7, {"7.txt": "7\n"})
    refuse_merges(monkeypatch, "Repository rule violations found " + "x" * 200)
    seen: list[tuple[str, str]] = []

    def post(sha: str, context: str, description: str, state: str = "success") -> None:
        if len(description) > 140:  # GitHub answers 422
            raise subprocess.CalledProcessError(
                1, ("gh",), stderr="HTTP 422: description is too long"
            )
        seen.append((sha, state))

    monkeypatch.setattr(approvals, "post_status", post)
    code, lines = lands["land"]()
    assert code == 1, lines
    assert seen[-2:] == [(head, "success"), (head, "error")], lines


def test_an_error_post_that_fails_is_reported_and_the_land_still_exits_1(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    refuse_merges(monkeypatch, "Review required")

    def post(sha: str, context: str, description: str, state: str = "success") -> None:
        if state == "error":
            raise subprocess.CalledProcessError(1, ("gh",), stderr="HTTP 502")

    monkeypatch.setattr(approvals, "post_status", post)
    code, lines = lands["land"]()
    assert code == 1, lines
    assert any("its `land` success could not be withdrawn: HTTP 502" in line for line in lines), (
        lines
    )
