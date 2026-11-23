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
        return 0, json.dumps({"number": 7, "state": "MERGED", "title": "t"}), ""

    monkeypatch.setattr(github, "_gh", gh)
    return calls


def test_auth_status_is_read_once_without_hosts_file(gh_calls: list[list[str]]) -> None:
    for _ in range(5):
        github._cached_accounts("/root")
    assert [c for c in gh_calls if c[:2] == ["auth", "status"]] == [gh_calls[0]]


def test_merged_pr_is_read_once_without_bound_account(gh_calls: list[list[str]]) -> None:
    reference = PullRequestRef.from_url(MERGED)
    for _ in range(5):
        data = github._reference_pr_data("/root", reference, background=False)
        assert data is not None and data["state"] == "MERGED"
    assert len([c for c in gh_calls if c[:2] == ["pr", "view"]]) == 1
    assert len([c for c in gh_calls if c[:2] == ["auth", "status"]]) == 1
