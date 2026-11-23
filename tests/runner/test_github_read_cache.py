"""Merged PR and ``gh auth status`` reads are cached even when ``hosts.yml`` is missing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.runner import github_resource as github
from omnigent.runner.session_prs import PullRequestRef

MERGED = "https://github.com/example/one/pull/7"


@pytest.fixture
def gh_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Fake ``gh`` with no ``hosts.yml`` (token auth) and no ambient token."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(github._config, "github_account_preference", lambda _: None)
    monkeypatch.setattr(github, "_in_sandbox", lambda: False)
    github._preview_cache.clear()
    calls: list[list[str]] = []

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        calls.append(args)
        if args[:2] == ["auth", "status"]:
            hosts = {"github.com": [{"login": "me", "active": True, "state": "error"}]}
            return 0, json.dumps({"hosts": hosts}), ""
        if args[:2] == ["auth", "token"]:
            return 1, "", "no oauth token found for github.com"
        return 0, json.dumps({"number": 7, "state": "MERGED", "title": "t"}), ""

    monkeypatch.setattr(github, "_gh", gh)
    return calls


def test_auth_status_is_read_once_without_hosts_file(gh_calls: list[list[str]]) -> None:
    for _ in range(5):
        github._cached_accounts("/root")
    assert [c for c in gh_calls if c[:2] == ["auth", "status"]] == [gh_calls[0]]


def test_creating_hosts_file_is_seen_at_once(gh_calls: list[list[str]], tmp_path: Path) -> None:
    github._cached_accounts("/root")
    (tmp_path / "gh").mkdir()
    (tmp_path / "gh" / "hosts.yml").write_text("github.com:\n  user: me\n")
    github._cached_accounts("/root")
    assert len([c for c in gh_calls if c[:2] == ["auth", "status"]]) == 2


def test_failed_auth_status_is_not_kept_for_minutes(
    gh_calls: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github, "_FAILED_READ_TTL_SECONDS", -1.0)
    monkeypatch.setattr(github, "_gh", lambda args, **_: (1, "", "offline"))
    assert github._cached_accounts("/root") == (False, [])
    monkeypatch.setattr(github, "_gh", lambda args, **_: (0, json.dumps({"hosts": {}}), ""))
    assert github._cached_accounts("/root") == (False, [])


def _pr_views(calls: list[list[str]]) -> int:
    return len([c for c in calls if c[:2] == ["pr", "view"]])


def test_merged_pr_is_read_once_when_no_account_can_be_bound(gh_calls: list[list[str]]) -> None:
    reference = PullRequestRef.from_url(MERGED)
    for _ in range(5):
        data = github._reference_pr_data("/root", reference, background=False)
        assert data is not None and data["state"] == "MERGED"
    assert _pr_views(gh_calls) == 1


def test_open_pr_is_still_read_every_time_when_no_account_can_be_bound(
    gh_calls: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        gh_calls.append(args)
        if args[:2] == ["auth", "token"]:
            return 1, "", "no oauth token found for github.com"
        return 0, json.dumps({"number": 7, "state": "OPEN", "title": "t"}), ""

    monkeypatch.setattr(github, "_gh", gh)
    reference = PullRequestRef.from_url(MERGED)
    for _ in range(3):
        github._reference_pr_data("/root", reference, background=False)
    assert _pr_views(gh_calls) == 3


def test_unbound_reads_are_not_shared_between_environment_tokens(
    gh_calls: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    reference = PullRequestRef.from_url(MERGED)
    github._reference_pr_data("/root", reference, background=False)
    monkeypatch.setenv("GH_TOKEN", "another-token")
    monkeypatch.setattr(github, "_in_sandbox", lambda: False)
    github._preview_cache.clear()
    github._reference_pr_data("/root", reference, background=False)
    assert _pr_views(gh_calls) == 2


def test_accounts_are_not_shared_between_environment_tokens(
    gh_calls: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    github._cached_accounts("/root")
    monkeypatch.setenv("GH_TOKEN", "another-token")
    github._cached_accounts("/root")
    assert len([c for c in gh_calls if c[:2] == ["auth", "status"]]) == 2
