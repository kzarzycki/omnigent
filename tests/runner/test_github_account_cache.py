"""The runner looks up a GitHub account once per host and login until `gh` rejects it."""

from __future__ import annotations

import json
import multiprocessing
import subprocess
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omnigent.git_providers import github as git_github
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
        self.status_delay = 0.0  # seconds ``gh auth status`` takes
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
            time.sleep(self.status_delay)
            hosts: dict[str, list[dict[str, Any]]] = {}
            for account in self.accounts:
                hosts.setdefault(account["host"], []).append(account)
            return 0, json.dumps({"hosts": hosts}), ""
        if argv[:2] == ["auth", "token"]:
            # Without ``--user``, ``gh`` answers with its active account's token.
            login = argv[argv.index("--user") + 1] if "--user" in argv else "alice"
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


def test_enterprise_pr_reads_list_no_accounts(fake_gh: FakeGh) -> None:
    assert _read(ENTERPRISE_URL) is not None
    assert _read(ENTERPRISE_URL) is not None
    assert github._host_args("/work", PullRequestRef.from_url(ENTERPRISE_URL))
    assert fake_gh.calls["auth status"] == 0
    assert fake_gh.calls["auth token"] == 1


def test_an_enterprise_host_without_a_gh_login_is_not_read(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github._config, "github_account_preference", lambda _key: None)
    fake_gh.tokens = {}
    assert _read(ENTERPRISE_URL) is None
    with pytest.raises(ValueError, match="Sign in"):
        github._host_args("/work", PullRequestRef.from_url(ENTERPRISE_URL))
    assert fake_gh.calls["pr view"] == 0
    assert fake_gh.calls["auth status"] == 0


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
    ["active_login", "read_identity", "workspace_info", "created_prs"],
)
def test_two_calls_through_each_account_lookup_make_one_auth_status(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch, call_name: str
) -> None:
    calls: dict[str, Callable[[], object]] = {
        "active_login": lambda: github._active_login("/work", "github.com"),
        "read_identity": lambda: github._read_identity("/work", "acme", "widgets"),
        "workspace_info": _read_as_workspace(fake_gh, monkeypatch),
        "created_prs": lambda: github_created.created_prs(PR_URL),
    }
    calls[call_name]()
    calls[call_name]()
    assert fake_gh.calls["auth status"] == 1


def test_concurrent_account_list_readers_make_one_auth_status_per_window(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_gh.status_delay = 0.3
    start = threading.Barrier(6)

    def read() -> None:
        start.wait()
        assert github._cached_accounts("/work")[0] is True

    for window in range(2):
        threads = [threading.Thread(target=read) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert fake_gh.calls["auth status"] == window + 1
        # The next window: memory and disk entries have both run out.
        later = time.time() + github._ACCOUNTS_TTL_SECONDS + 1
        moved = time.monotonic() + github._ACCOUNTS_TTL_SECONDS + 1
        monkeypatch.setattr(github.time, "time", lambda later=later: later)
        monkeypatch.setattr(github.time, "monotonic", lambda moved=moved: moved)
        start.reset()


def _read_accounts_in_a_runner(start: Any, log: str) -> None:
    """One runner process: ``gh auth status`` appends a line to ``log`` and takes a while."""

    def run(argv: list[str], **_kwargs: object) -> tuple[int, str, str]:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(" ".join(argv) + "\n")
        time.sleep(0.3)
        hosts = {"github.com": [{"login": "alice", "active": True, "state": "success"}]}
        return 0, json.dumps({"hosts": hosts}), ""

    github._run = run  # type: ignore[assignment]  # this process only
    github._preview_cache.clear()
    start.wait()
    github._cached_accounts("/work")


def test_runner_processes_reading_the_account_list_at_once_make_one_auth_status(
    fake_gh: FakeGh, tmp_path: Path
) -> None:
    log = tmp_path / "auth-status.log"
    context = multiprocessing.get_context("spawn")
    start = context.Barrier(3)
    processes = [
        context.Process(target=_read_accounts_in_a_runner, args=(start, str(log)))
        for _ in range(3)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)
    assert [process.exitcode for process in processes] == [0, 0, 0]
    assert log.read_text().splitlines() == ["gh auth status --json hosts"]


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


def test_runners_with_different_gh_config_dirs_do_not_share_tokens(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Files on different filesystems can share a stat; the key must still tell them apart.
    monkeypatch.setattr(git_github, "hosts_file_stat", lambda _dir=None: [1, 1, 1])
    monkeypatch.setenv("GH_CONFIG_DIR", str(fake_gh.config_dir))
    assert (
        git_github.cached_gh_token("github.com", "alice", lambda: "first-token") == "first-token"
    )
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "other-config"))
    assert (
        git_github.cached_gh_token("github.com", "alice", lambda: "second-token") == "second-token"
    )
