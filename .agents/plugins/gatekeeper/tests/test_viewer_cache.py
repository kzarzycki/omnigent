"""github_adapter.viewer asks `gh api user` once per process (#1336)."""

from __future__ import annotations

import subprocess

import pytest
from approvals_harness import approvals


def test_two_calls_make_one_gh_api_user_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(approvals.github, "gh", lambda *args, **kw: calls.append(args) or "me\n")
    assert approvals.github.viewer() == "me" and approvals.github.viewer() == "me"
    assert calls == [("api", "user", "--jq", ".login")]


def test_a_failure_is_not_kept_so_the_next_call_asks_again(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([subprocess.CalledProcessError(1, "gh"), "me\n"])

    def gh(*args: str, **kw: object) -> str:
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(approvals.github, "gh", gh)
    assert approvals.github.viewer() is None
    assert approvals.github.viewer() == "me"
