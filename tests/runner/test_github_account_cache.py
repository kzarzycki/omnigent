"""The runner looks up a GitHub account once per host and login, until ``gh`` stops accepting it."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from omnigent.runner import github_resource as github
from omnigent.runner.session_prs import PullRequestRef

PR_URL = "https://github.com/acme/widgets/pull/1"
ENTERPRISE_URL = "https://ghe.example.com/acme/widgets/pull/1"


class FakeGh:
    """Answers the ``gh`` subprocess from fixtures and counts each call by its first words."""

    def __init__(self, config_dir: Path) -> None:
        self.calls: Counter[str] = Counter()
        self.tokens = {"alice": "alice-token-1"}
        self.valid_tokens = {"alice-token-1"}
        self.accounts = [
            {"login": "alice", "active": True, "state": "success", "host": host}
            for host in ("github.com", "ghe.example.com")
        ]
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "hosts.yml").write_text("fixture")

    def __call__(
        self, argv: list[str], *, cwd: str, timeout: float | None, env: dict[str, str] | None
    ) -> tuple[int | None, str, str]:
        argv = argv[1:]  # drop "gh"
        self.calls[" ".join(argv[:2])] += 1
        if argv[:2] == ["auth", "status"]:
            hosts: dict[str, list[dict[str, Any]]] = {}
            for account in self.accounts:
                hosts.setdefault(account["host"], []).append(account)
            return 0, json.dumps({"hosts": hosts}), ""
        if argv[:2] == ["auth", "token"]:
            login = argv[argv.index("--user") + 1]
            return (0, self.tokens[login], "") if login in self.tokens else (1, "", "no token")
        if argv[:2] == ["pr", "view"]:
            if (env or {}).get("GH_TOKEN") not in self.valid_tokens:
                return 1, "", "HTTP 401: Bad credentials (https://api.github.com/graphql)"
            return 0, json.dumps({"number": 1, "title": "fixture", "state": "OPEN"}), ""
        return 1, "", "not faked"


@pytest.fixture
def fake_gh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeGh:
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh-config"))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    gh = FakeGh(tmp_path / "gh-config")
    monkeypatch.setattr(github, "_run", gh)
    monkeypatch.setattr(github, "_in_sandbox", lambda: False)
    monkeypatch.setattr(github._config, "github_account_preference", lambda _key: "alice")
    return gh


def _read(url: str = PR_URL) -> dict[str, Any] | None:
    return github._pr_json("/work", PullRequestRef.from_url(url), "number,title,state")


def test_two_pr_reads_in_a_row_make_one_gh_auth_call(fake_gh: FakeGh) -> None:
    assert _read() is not None
    assert _read() is not None
    assert fake_gh.calls["auth token"] == 1
    assert fake_gh.calls["pr view"] == 2


def test_enterprise_pr_reads_list_the_accounts_once(fake_gh: FakeGh) -> None:
    assert _read(ENTERPRISE_URL) is not None
    assert _read(ENTERPRISE_URL) is not None
    assert fake_gh.calls["auth status"] == 1


def test_an_auth_failure_drops_the_cache_so_a_re_login_is_seen(fake_gh: FakeGh) -> None:
    assert _read() is not None
    fake_gh.valid_tokens = {"alice-token-2"}  # `gh auth login` again: the old token is dead
    fake_gh.tokens = {"alice": "alice-token-2"}
    assert _read() is None  # the stale token fails and is dropped
    assert _read() is not None  # the next read looks the account up again
    assert fake_gh.calls["auth token"] == 2
