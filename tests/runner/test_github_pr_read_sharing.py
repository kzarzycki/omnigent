"""Sessions reading PRs of one repository share ``gh`` calls instead of repeating them."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import pytest

from omnigent.runner import github_resource as github
from omnigent.runner.session_prs import PullRequestRef

REPO = "https://github.com/acme/widgets"
AUTH = json.dumps(
    {"hosts": {"github.com": [{"login": "reader", "active": True, "state": "success"}]}}
)
OPEN_NUMBERS = (1, 2, 3, 4, 5)
MERGED_NUMBER = 9


def _pr(number: int, state: str) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Change {number}",
        "state": state,
        "url": f"{REPO}/pull/{number}",
        "isDraft": False,
        "author": {"login": "writer"},
        "baseRefName": "main",
        "headRefName": f"change-{number}",
        "statusCheckRollup": [],
        "body": "",
        "reviews": [],
        "comments": [],
    }


class FakeGh:
    """Answers ``gh`` from fixtures and counts each invocation by subcommand."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()

    def __call__(
        self, argv: list[str], *, cwd: str, token: str | None = None, timeout: float | None = None
    ) -> tuple[int | None, str, str]:
        self.calls[" ".join(argv[:2])] += 1
        if argv[:2] == ["auth", "status"]:
            return 0, AUTH, ""
        if argv[:2] == ["pr", "list"]:
            return 0, json.dumps([_pr(number, "OPEN") for number in OPEN_NUMBERS]), ""
        if argv[:2] == ["pr", "view"]:
            number = int(argv[2])
            state = "MERGED" if number == MERGED_NUMBER else "OPEN"
            return 0, json.dumps(_pr(number, state)), ""
        return 1, "", "not faked"


@pytest.fixture
def fake_gh(monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    gh = FakeGh()
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(github, "_gh", gh)
    monkeypatch.setattr(github.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(github, "_in_sandbox", lambda: False)
    return gh


def _read(number: int, *, background: bool) -> dict[str, Any]:
    """One session's refresh, in a runner process that starts with an empty memory cache."""
    github._preview_cache.clear()
    with github.background_reads(background):
        return github._reference_info("/work", PullRequestRef.from_url(f"{REPO}/pull/{number}"))


def test_sessions_without_a_viewer_share_one_listing_per_repository(fake_gh: FakeGh) -> None:
    for _ in range(3):  # three refresh rounds inside one window
        for number in OPEN_NUMBERS:
            assert _read(number, background=True)["pr"]["number"] == number
    assert fake_gh.calls == {"auth status": 1, "pr list": 1}


def test_open_pr_listing_is_read_again_after_the_window(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    _read(1, background=True)
    clock = github.time.time()
    monkeypatch.setattr(github.time, "time", lambda: clock + github._OPEN_PR_TTL_SECONDS + 1)
    _read(2, background=True)
    assert fake_gh.calls["pr list"] == 2


def test_merged_pr_is_not_read_again_within_the_long_period(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _read(MERGED_NUMBER, background=True)["pr"]["state"] == "MERGED"
    clock = github.time.time()
    monkeypatch.setattr(github.time, "time", lambda: clock + 3600)
    for _ in range(3):
        _read(MERGED_NUMBER, background=True)
    assert fake_gh.calls["pr view"] == 1


def test_sessions_viewing_one_pr_share_its_read(fake_gh: FakeGh) -> None:
    for _ in range(4):
        _read(1, background=False)
    assert fake_gh.calls["pr view"] == 1
    assert fake_gh.calls["pr list"] == 0
