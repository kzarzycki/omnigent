"""The runner looks up a GitHub account once per host and login until `gh` rejects it."""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omnigent.onboarding import copilot_auth
from omnigent.runner import github_resource as github
from omnigent.runner.git_providers import github_created
from omnigent.runner.session_prs import PullRequestRef

PR_URL = "https://github.com/acme/widgets/pull/1"
ENTERPRISE_URL = "https://ghe.example.com/acme/widgets/pull/1"


class FakeGh:
    """Answers the ``gh`` subprocess from fixtures and counts each call by its first words."""

    def __init__(self, config_dir: Path) -> None:
        self.calls: Counter[str] = Counter()
        self.tokens = {"alice": "alice-token-1"}
        self.valid_tokens: set[str | None] = {"alice-token-1"}
        self.unreachable = False  # PR and repo reads fail without an auth error
        self.config_dir = config_dir
        self.accounts = [
            {"login": "alice", "active": True, "state": "success", "host": host}
            for host in ("github.com", "ghe.example.com")
        ]
        self.rewrite_hosts_file("fixture")

    def rewrite_hosts_file(self, text: str) -> None:
        """Change ``hosts.yml`` as ``gh auth login`` / ``logout`` / ``switch`` does."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        (self.config_dir / "hosts.yml").write_text(text)

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
        if self.unreachable and argv[:2] in (["pr", "view"], ["repo", "view"]):
            return 1, "", "could not resolve to a Repository"
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


def test_a_logout_makes_the_next_read_run_as_the_active_account(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str | None] = []
    run = github._run

    def spy(argv, *, cwd, timeout, env):  # type: ignore[no-untyped-def]
        if argv[1:3] == ["pr", "view"]:
            seen.append((env or {}).get("GH_TOKEN"))
        return run(argv, cwd=cwd, timeout=timeout, env=env)

    monkeypatch.setattr(github, "_run", spy)
    assert _read() is not None
    fake_gh.tokens = {}  # `gh auth logout --user alice`: the token is gone, the file changes
    fake_gh.valid_tokens = {"alice-token-1", None}  # the old token is still accepted
    fake_gh.rewrite_hosts_file("after logout")
    assert _read() is not None
    assert seen == ["alice-token-1", None]


def _read_as_workspace(fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch) -> Callable[[], object]:
    fake_gh.unreachable = True
    monkeypatch.setattr(github, "_git", lambda argv, cwd: (0, "main\n", ""))
    monkeypatch.setattr(github.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(github, "_workspace_key", lambda _root: None)
    return lambda: github._workspace_github_info("/work")


@pytest.mark.parametrize(
    "call_name",
    ["host_args", "active_login", "read_identity", "workspace_info", "created_prs"],
)
def test_two_calls_through_each_account_lookup_make_one_auth_status(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch, call_name: str
) -> None:
    calls: dict[str, Callable[[], object]] = {
        "host_args": lambda: github._host_args("/work", PullRequestRef.from_url(ENTERPRISE_URL)),
        "active_login": lambda: github._active_login("/work", "github.com"),
        "read_identity": lambda: github._read_identity("/work", "acme", "widgets"),
        "workspace_info": _read_as_workspace(fake_gh, monkeypatch),
        "created_prs": lambda: github_created.created_prs(PR_URL),
    }
    calls[call_name]()
    calls[call_name]()
    assert fake_gh.calls["auth status"] == 1


def test_an_auth_failure_drops_the_shared_accounts_list(fake_gh: FakeGh) -> None:
    assert github._active_login("/work", "github.com") == "alice"
    fake_gh.valid_tokens = set()  # the token stops working without hosts.yml changing
    assert _read() is None
    fake_gh.accounts = [{**fake_gh.accounts[0], "login": "bob"}]
    assert github._active_login("/work", "github.com") == "bob"


def test_copilot_token_lookups_spawn_gh_once_until_hosts_file_changes(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        spawned.append(argv)
        return subprocess.CompletedProcess(argv, 0, f"token-{len(spawned)}\n", "")

    monkeypatch.setattr(copilot_auth.subprocess, "run", run)
    assert copilot_auth.gh_cli_github_token("ghe.example.com") == "token-1"
    assert copilot_auth.gh_cli_github_token("ghe.example.com") == "token-1"
    assert copilot_auth.gh_cli_github_token() == "token-2"  # another host is its own entry
    assert len(spawned) == 2
    fake_gh.rewrite_hosts_file("after login")
    assert copilot_auth.gh_cli_github_token("ghe.example.com") == "token-3"
