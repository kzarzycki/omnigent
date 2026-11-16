"""Sessions reading PRs of one repository share ``gh`` calls instead of repeating them."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.process_logging import data_dir
from omnigent.runner import create_runner_app, pr_resource
from omnigent.runner import github_resource as github
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry
from tests.runner.helpers import NullServerClient

REPO = "https://github.com/acme/widgets"
OPEN_NUMBERS = (1, 2, 3, 4, 5)
MERGED_NUMBER = 9
CLOSED_NUMBER = 10


def _accounts(active: str, *logins: str, state: str = "success") -> list[dict[str, Any]]:
    return [
        {"login": login, "active": login == active, "state": state, "host": "github.com"}
        for login in (active, *logins)
    ]


class FakeGh:
    """Answers ``gh`` from fixtures, as whoever the call authenticates as, and counts calls."""

    def __init__(self, config_dir: Path) -> None:
        self.calls: Counter[str] = Counter()
        self.config_dir = config_dir
        self._writes = 0
        self.accounts = _accounts("reader")
        self.tokens: dict[str, str] = {}  # login -> token ``gh auth token`` hands out
        self.preference: str | None = None
        self.states: dict[int, str] = {}
        self.delay = 0.0

    @property
    def accounts(self) -> list[dict[str, Any]]:
        return self._accounts

    @accounts.setter
    def accounts(self, accounts: list[dict[str, Any]]) -> None:
        """Set the accounts as ``gh auth switch`` / ``login`` does: rewrite ``hosts.yml``."""
        self._accounts = accounts
        self.config_dir.mkdir(parents=True, exist_ok=True)
        hosts_file = self.config_dir / "hosts.yml"
        hosts_file.write_text(json.dumps(accounts))
        self._writes += 1
        os.utime(hosts_file, ns=(self._writes * 10**9, self._writes * 10**9))

    def reader(self, token: str | None) -> str:
        """Who a call authenticates as: its token, an ambient token, or the active account."""
        for login, known in self.tokens.items():
            if token is not None and known == token:
                return login
        if token is None and os.environ.get("GH_TOKEN"):
            return "ambient"
        return next(account["login"] for account in self.accounts if account["active"])

    def _pr(self, number: int, token: str | None) -> dict[str, Any]:
        return {
            "number": number,
            "title": f"read as {self.reader(token)}",
            "state": self.states.get(number, "OPEN"),
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

    def __call__(
        self, argv: list[str], *, cwd: str, token: str | None = None, timeout: float | None = None
    ) -> tuple[int | None, str, str]:
        self.calls[" ".join(argv[:2])] += 1
        if argv[:2] == ["auth", "status"]:
            hosts = {"github.com": [dict(account) for account in self.accounts]}
            return 0, json.dumps({"hosts": hosts}), ""
        if argv[:2] == ["auth", "token"]:
            login = argv[argv.index("--user") + 1]
            return (0, self.tokens[login], "") if login in self.tokens else (1, "", "no token")
        if argv[:2] == ["pr", "list"]:
            time.sleep(self.delay)
            numbers = [n for n in OPEN_NUMBERS if self.states.get(n, "OPEN") == "OPEN"]
            return 0, json.dumps([self._pr(number, token) for number in numbers]), ""
        if argv[:2] == ["pr", "view"]:
            time.sleep(self.delay)
            return 0, json.dumps(self._pr(int(argv[2]), token)), ""
        return 1, "", "not faked"


@pytest.fixture
def fake_gh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeGh:
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh-config"))
    gh = FakeGh(tmp_path / "gh-config")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(github, "_gh", gh)
    monkeypatch.setattr(github.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(github, "_in_sandbox", lambda: False)
    monkeypatch.setattr(github._config, "github_account_preference", lambda _key: gh.preference)
    return gh


def _read(number: int, *, background: bool) -> dict[str, Any]:
    """One session's refresh, in a runner process that starts with an empty memory cache."""
    github._preview_cache.clear()
    reference = PullRequestRef.from_url(f"{REPO}/pull/{number}")
    if not background:
        return github._reference_info("/work", reference)
    with github.background_reads(True):
        return github._reference_info("/work", reference)


def _title(number: int = 1, *, background: bool = False) -> str:
    return _read(number, background=background)["pr"]["title"]


def _cached_keys() -> list[list[str]]:
    directory = data_dir() / "github" / "read-cache"
    return [json.loads(path.read_text())["key"] for path in directory.glob("*.json")]


def _later(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    clock = time.time()
    monkeypatch.setattr(github.time, "time", lambda: clock + seconds)


def test_sessions_without_a_viewer_share_one_listing_per_repository(fake_gh: FakeGh) -> None:
    for _ in range(3):  # three refresh rounds inside one window
        for number in OPEN_NUMBERS:
            assert _read(number, background=True)["pr"]["number"] == number
    assert fake_gh.calls == {"auth status": 1, "pr list": 1}


def test_five_sessions_viewing_their_prs_make_one_read_each_per_window(fake_gh: FakeGh) -> None:
    for _ in range(3):
        for number in OPEN_NUMBERS:
            assert _read(number, background=False)["pr"]["number"] == number
    assert fake_gh.calls == {"auth status": 1, "pr view": 5, "api --hostname": 5}


def test_open_pr_listing_is_read_again_after_the_window(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    _read(1, background=True)
    _later(monkeypatch, github._OPEN_PR_TTL_SECONDS + 1)
    _read(2, background=True)
    assert fake_gh.calls["pr list"] == 2


def test_merged_pr_is_not_read_again_within_the_long_period(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_gh.states[MERGED_NUMBER] = "MERGED"
    for background in (True, False):
        assert _read(MERGED_NUMBER, background=background)["pr"]["state"] == "MERGED"
    _later(monkeypatch, 3600)
    for background in (True, False):
        _read(MERGED_NUMBER, background=background)
    assert fake_gh.calls["pr view"] == 2  # one per kind of read


def test_closed_pr_is_only_skipped_by_the_refresher_since_it_can_reopen(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_gh.states[CLOSED_NUMBER] = "CLOSED"
    _read(CLOSED_NUMBER, background=True)
    _read(CLOSED_NUMBER, background=False)
    fake_gh.states[CLOSED_NUMBER] = "OPEN"
    _later(monkeypatch, github._OPEN_PR_TTL_SECONDS + 1)
    assert _read(CLOSED_NUMBER, background=False)["pr"]["state"] == "OPEN"
    assert _read(CLOSED_NUMBER, background=True)["pr"]["state"] == "CLOSED"
    assert fake_gh.calls["pr view"] == 3


@pytest.mark.parametrize("background", [False, True])
def test_every_identity_reads_its_own_data(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch, background: bool
) -> None:
    fake_gh.accounts = _accounts("carol", "alice", "bob")
    fake_gh.tokens = {"alice": "alice-token", "bob": "bob-token"}
    seen = {}
    fake_gh.preference = "alice"
    seen["alice"] = _title(background=background)
    fake_gh.preference = "bob"
    seen["bob"] = _title(background=background)
    fake_gh.preference = None
    monkeypatch.setenv("GH_TOKEN", "ambient-token")
    seen["ambient"] = _title(background=background)
    monkeypatch.delenv("GH_TOKEN")
    seen["carol"] = _title(background=background)
    fake_gh.accounts = _accounts("dave")
    seen["dave"] = _title(background=background)
    assert seen == {
        "alice": "read as alice",
        "bob": "read as bob",
        "ambient": "read as ambient",
        "carol": "read as carol",
        "dave": "read as dave",
    }


def test_a_preferred_account_without_a_token_does_not_share_its_key_with_a_working_one(
    fake_gh: FakeGh,
) -> None:
    fake_gh.accounts = _accounts("carol", "alice")
    fake_gh.preference = "alice"
    assert _title() == "read as carol"  # gh falls back to the active account
    fake_gh.tokens = {"alice": "alice-token"}
    assert _title() == "read as alice"


def test_switching_the_active_account_is_seen_at_once(fake_gh: FakeGh) -> None:
    fake_gh.accounts = _accounts("carol", "dave")
    assert _title() == "read as carol"
    fake_gh.accounts = _accounts("dave", "carol")  # `gh auth switch`, inside the accounts window
    assert _title() == "read as dave"


def test_a_refresh_with_an_unchanged_accounts_file_lists_no_accounts(fake_gh: FakeGh) -> None:
    for _ in range(3):
        _read(1, background=False)
    assert fake_gh.calls["auth status"] == 1


def test_runners_with_different_gh_config_dirs_do_not_share_accounts(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Files on different filesystems can share a stat; the key must still tell them apart.
    monkeypatch.setattr(github, "_accounts_file_stat", lambda _dir: [1, 1, 1])
    fake_gh.accounts = _accounts("carol")
    assert _title() == "read as carol"
    other = FakeGh(tmp_path / "other-config")
    other.accounts = _accounts("dave")
    monkeypatch.setattr(github, "_gh", other)
    monkeypatch.setenv("GH_CONFIG_DIR", str(other.config_dir))
    assert _title() == "read as dave"


def test_the_gh_config_dir_follows_ghs_own_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg")
    assert github._gh_config_dir() == Path("/xdg/gh")
    monkeypatch.setenv("GH_CONFIG_DIR", "/explicit")
    assert github._gh_config_dir() == Path("/explicit")
    monkeypatch.delenv("GH_CONFIG_DIR")
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert github._gh_config_dir() == Path.home() / ".config" / "gh"


@pytest.mark.parametrize("background", [False, True])
def test_an_unknown_identity_is_read_every_time_and_never_cached(
    fake_gh: FakeGh, background: bool
) -> None:
    fake_gh.accounts = _accounts("carol", state="error")
    assert _title(background=background) == "read as carol"
    _read(1, background=background)
    assert fake_gh.calls["pr view"] == 2
    assert not [key for key in _cached_keys() if key[0] in ("pr", "pr-list")]


@pytest.mark.parametrize(
    ("value", "expected"), [("1", True), (True, True), ("0", False), (None, False)]
)
def test_background_reads_follow_the_query_flag(value: object, expected: bool) -> None:
    with github.background_reads(value):
        assert github._background_read.get() is expected
    assert github._background_read.get() is False


@pytest.fixture
def runner_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:
    monkeypatch.setattr(github, "_gh_installed", lambda: True)
    pr_url = f"{REPO}/pull/1"
    SessionPrRegistry("session").record(
        [PullRequestRef.from_url(pr_url)], relationship="created", source="test"
    )
    app = create_runner_app(
        runner_workspace=tmp_path,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://runner")


async def test_the_runner_route_reads_the_listing_for_a_background_refresh(
    fake_gh: FakeGh, runner_client: httpx.AsyncClient
) -> None:
    path = "/v1/sessions/session/resources/github"
    async with runner_client as client:
        viewer = await client.get(path, params={"pr_url": f"{REPO}/pull/1"})
        github._preview_cache.clear()
        for child in (data_dir() / "github" / "read-cache").glob("*.json"):
            child.unlink()
        refresh = await client.get(path, params={"pr_url": f"{REPO}/pull/1", "background": "1"})
    assert viewer.status_code == refresh.status_code == 200
    assert (fake_gh.calls["pr view"], fake_gh.calls["pr list"]) == (1, 1)


WRITE_ROUTES = [
    ("labels", "update_pr_labels", {}),
    ("merge", "merge_pr", {}),
    ("merge-as", "merge_pr_as", {}),
    ("disable-auto-merge", "disable_pr_auto_merge", {}),
    ("reviewers", "update_pr_reviewers", {}),
    ("review-threads", "create_pr_review_thread", {}),
    ("review-threads/thread/replies", "reply_to_pr_review_thread", {}),
    ("comments", "create_pr_comment", {}),
    ("details", "update_pr_details", {}),
]


def test_every_write_operation_is_covered_by_a_route_case() -> None:
    assert {operation for _, operation, _ in WRITE_ROUTES} == pr_resource.PR_WRITE_OPERATIONS


@pytest.mark.parametrize(("route", "operation", "extra"), WRITE_ROUTES)
async def test_a_write_drops_the_pr_and_its_listing_but_not_other_prs(
    fake_gh: FakeGh,
    runner_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    operation: str,
    extra: dict[str, Any],
) -> None:
    for number in (1, 2):
        _read(number, background=False)
        _read(number, background=True)
    monkeypatch.setattr(pr_resource, operation, lambda *_a, **_kw: {})
    async with runner_client as client:
        response = await client.post(
            f"/v1/sessions/session/resources/github/{route}",
            json={"pr_url": f"{REPO}/pull/1", "body": "x", "thread_id": "thread", **extra},
        )
    assert response.status_code == 200, response.text
    remaining = {(key[0], key[3] if key[0] == "pr" else "") for key in _cached_keys()}
    assert ("pr", "1") not in remaining
    assert "pr-list" not in {kind for kind, _ in remaining}
    assert ("pr", "2") in remaining


def test_concurrent_misses_share_one_read(fake_gh: FakeGh) -> None:
    fake_gh.delay = 0.3
    github._preview_cache.clear()
    start = threading.Barrier(4)

    def refresh() -> None:
        start.wait()
        github._reference_pr_data(
            "/work", PullRequestRef.from_url(f"{REPO}/pull/1"), background=True
        )

    threads = [threading.Thread(target=refresh) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert fake_gh.calls["pr list"] == 1


def test_an_expired_entry_is_removed_when_read(fake_gh: FakeGh) -> None:
    key = ("pr", "token:x", "github.com/acme/widgets", "1", "full")
    github._cache_put(key, {"title": "old"}, -1, shared=True)
    github._preview_cache.clear()
    path = github._cache_path(key)
    assert path.exists()
    assert github._cache_get(key, shared=True) == (False, None)
    assert not path.exists()


def test_writing_sweeps_expired_entries_and_stale_temporary_files(fake_gh: FakeGh) -> None:
    expired = ("pr", "token:x", "github.com/acme/widgets", "1", "full")
    github._cache_put(expired, {"title": "old"}, -1, shared=True)
    directory = github._cache_path(expired).parent
    crashed = directory / "crashed.tmp"
    crashed.write_text("{")
    long_ago = time.time() - 2 * github._STALE_FILE_SECONDS
    os.utime(crashed, (long_ago, long_ago))
    fresh_key = ("pr", "token:x", "github.com/acme/widgets", "2", "full")
    github._cache_put(fresh_key, {"title": "new"}, 60, shared=True)
    assert sorted(path.name for path in directory.iterdir() if path.suffix != ".lock") == [
        github._cache_path(fresh_key).name
    ]
