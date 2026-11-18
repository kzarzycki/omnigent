"""Sessions reading PRs of one repository share ``gh`` calls instead of repeating them."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from typing_extensions import get_protocol_members

from omnigent.git_providers.github import gh_config_dir
from omnigent.process_logging import data_dir
from omnigent.runner import create_runner_app, pr_resource
from omnigent.runner import github_resource as github
from omnigent.runner.git_providers import AccountMergeFacet, ReviewThreadsFacet
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry
from tests._helpers.polling import poll_until
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
        self.fail = False  # pr reads fail after their delay
        self.titles: dict[int, str] = {}  # overrides of a PR's title
        self.time_out = False  # pr reads are cut off by the caller's deadline
        self.rate_limited = False  # pr reads fail as GitHub does once the quota is spent
        self.rate_limit_message = "GraphQL: API rate limit exceeded for user ID 1."
        self.quota_reset: float | None = None  # ``gh api rate_limit``'s graphql reset
        self.quota_remaining = 0  # ``gh api rate_limit``'s graphql remaining
        self.conversation_rate_limited = False  # the conversation query fails as above
        self.argvs: list[list[str]] = []

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
            "title": self.titles.get(number, f"read as {self.reader(token)}"),
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
        self.argvs.append(argv)
        if argv[:2] == ["api", "rate_limit"] and self.quota_reset is not None:
            graphql = {"limit": 5000, "remaining": self.quota_remaining, "reset": self.quota_reset}
            return 0, json.dumps({"resources": {"graphql": graphql}}), ""
        if argv[0] == "pr" and self.rate_limited:
            return 1, "", self.rate_limit_message
        if argv[0] == "api" and "graphql" in argv and self.conversation_rate_limited:
            return 1, "", self.rate_limit_message
        if argv[:2] == ["auth", "status"]:
            hosts = {"github.com": [dict(account) for account in self.accounts]}
            return 0, json.dumps({"hosts": hosts}), ""
        if argv[:2] == ["auth", "token"]:
            login = argv[argv.index("--user") + 1]
            return (0, self.tokens[login], "") if login in self.tokens else (1, "", "no token")
        if argv[:2] == ["pr", "list"]:
            time.sleep(self.delay)
            if self.fail:
                return 1, "", "github is down"
            numbers = [n for n in OPEN_NUMBERS if self.states.get(n, "OPEN") == "OPEN"]
            return 0, json.dumps([self._pr(number, token) for number in numbers]), ""
        if argv[:2] == ["pr", "view"]:
            time.sleep(self.delay)
            if self.time_out:
                github._gh_timed_out.set(True)
                return None, "", "timed out"
            if self.fail:
                return 1, "", "github is down"
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


_REAL_TIME = time.time  # the unpatched clock, so repeated _later calls are absolute offsets


def _later(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    clock = _REAL_TIME()
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


def test_background_refreshes_within_120_seconds_share_one_listing(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    _read(1, background=True)
    _later(monkeypatch, 119)
    for number in OPEN_NUMBERS:
        _read(number, background=True)
    assert fake_gh.calls["pr list"] == 1


def test_open_pr_listing_is_read_again_after_the_window(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    _read(1, background=True)
    _later(monkeypatch, github._BACKGROUND_PR_TTL_SECONDS + 1)
    _read(2, background=True)
    assert fake_gh.calls["pr list"] == 2


def test_the_background_listing_reads_no_nested_lists(fake_gh: FakeGh) -> None:
    _read(1, background=True)
    (listing,) = [argv for argv in fake_gh.argvs if argv[:2] == ["pr", "list"]]
    fields = set(listing[listing.index("--json") + 1].split(","))
    nested = {"statusCheckRollup", "reviews", "reviewRequests", "latestReviews", "labels"}
    assert not fields & (nested | {"comments"})
    assert {"number", "title", "state", "isDraft"} <= fields


def test_a_viewer_reads_again_after_15_seconds(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    _read(1, background=True)
    _read(1, background=False)
    _later(monkeypatch, 15)
    _read(1, background=False)
    assert fake_gh.calls["pr view"] == 2


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
    monkeypatch.setattr(github, "hosts_file_stat", lambda _dir: [1, 1, 1])
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
    assert Path(gh_config_dir()) == Path("/xdg/gh")
    monkeypatch.setenv("GH_CONFIG_DIR", "/explicit")
    assert Path(gh_config_dir()) == Path("/explicit")
    monkeypatch.delenv("GH_CONFIG_DIR")
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert Path(gh_config_dir()) == Path.home() / ".config" / "gh"


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


ANCHOR = {
    "path": "a.py",
    "side": "RIGHT",
    "line": 1,
    "head_sha": "a" * 40,
    "base_sha": "b" * 40,
}
MERGE = {"merge_method": "squash", "expected_head": "a" * 40}
# route, write function, and the arguments that get it past its own validation
WRITE_ROUTES = [
    ("labels", "update_pr_labels", {"add": ["bug"]}),
    ("merge", "merge_pr", MERGE),
    ("merge-as", "merge_pr_as", {**MERGE, "account": "writer"}),
    ("disable-auto-merge", "disable_pr_auto_merge", {}),
    ("reviewers", "update_pr_reviewers", {"add": [{"login": "writer", "kind": "user"}]}),
    ("review-threads", "create_pr_review_thread", {"body": "x", "anchor": ANCHOR}),
    (
        "review-threads/thread/replies",
        "reply_to_pr_review_thread",
        {"body": "x", "thread_id": "t"},
    ),
    ("comments", "create_pr_comment", {"body": "x"}),
    ("details", "update_pr_details", {"title": "new"}),
]


def test_every_write_operation_is_covered_by_a_route_case() -> None:
    assert {operation for _, operation, _ in WRITE_ROUTES} == pr_resource.PR_WRITE_OPERATIONS


def _accepting_facet() -> Any:
    """A provider that accepts every capability and answers every write with ``{}``."""
    names = get_protocol_members(AccountMergeFacet) | get_protocol_members(ReviewThreadsFacet)
    answer = lambda *_a, **_kw: {}  # noqa: E731
    members = dict.fromkeys(names, answer)  # isinstance() on a protocol skips __getattr__
    members.update(capabilities=MagicMock(), __getattr__=lambda _self, _name: answer)
    return type("AcceptingFacet", (), members)()


@pytest.mark.parametrize(("route", "operation", "arguments"), WRITE_ROUTES)
def test_a_write_drops_the_pr_and_its_listing_but_not_other_prs(
    fake_gh: FakeGh,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    operation: str,
    arguments: dict[str, Any],
) -> None:
    for number in (1, 2):
        _read(number, background=False)
        _read(number, background=True)
    reference = PullRequestRef.from_url(f"{REPO}/pull/1")
    monkeypatch.setattr(
        pr_resource, "_capable_target", lambda *_a, **_kw: (_accepting_facet(), reference)
    )
    getattr(pr_resource, operation)(
        "/work", session_id="session", pr_url=reference.url, **arguments
    )
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


def _fetch(number: int, *, background: bool) -> dict[str, Any] | None:
    """A read that keeps the runner's in-memory cache."""
    return github._reference_pr_data(
        "/work", PullRequestRef.from_url(f"{REPO}/pull/{number}"), background=background
    )


def _in_threads(count: int, work: Any) -> None:
    start = threading.Barrier(count)

    def run(index: int) -> None:
        start.wait()
        work(index)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


class _MergeFacet:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def merge(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        if self.error:
            raise self.error
        return {}


def _merge_through_host(monkeypatch: pytest.MonkeyPatch, error: Exception | None = None) -> None:
    """Merge PR 1 the way the host does when the runner is offline."""
    from omnigent.host.connect import HostProcess

    reference = PullRequestRef.from_url(f"{REPO}/pull/1")
    monkeypatch.setattr(pr_resource, "_merge_target", lambda *_a: (_MergeFacet(error), reference))
    params = {"pr_url": reference.url, "merge_method": "squash", "expected_head": "a" * 40}
    HostProcess._dispatch_fs_write_op("/work", "github_merge", params)


def test_a_write_through_the_host_dispatcher_shows_the_new_state(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _read(1, background=False)["pr"]["state"] == "OPEN"
    fake_gh.states[1] = "MERGED"
    _merge_through_host(monkeypatch)
    assert _read(1, background=False)["pr"]["state"] == "MERGED"


def test_a_write_that_raises_still_drops_the_pr(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    _read(1, background=False)
    with pytest.raises(ValueError, match="refused"):
        _merge_through_host(monkeypatch, ValueError("refused"))
    assert ("pr", "1") not in {(key[0], key[3]) for key in _cached_keys() if key[0] == "pr"}


def test_a_write_drops_the_runners_own_in_memory_entry(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (_fetch(1, background=False) or {})["state"] == "OPEN"
    fake_gh.states[1] = "MERGED"
    _merge_through_host(monkeypatch)
    assert (_fetch(1, background=False) or {})["state"] == "MERGED"


def test_a_read_in_flight_across_a_write_is_not_cached(fake_gh: FakeGh) -> None:
    fake_gh.delay = 0.4
    github._preview_cache.clear()
    reader = threading.Thread(target=_fetch, args=(1,), kwargs={"background": False})
    reader.start()
    poll_until(lambda: fake_gh.calls["pr view"], timeout_s=5, interval_s=0.01)  # read is in flight
    github.invalidate_pr_reads(f"{REPO}/pull/1")
    reader.join()
    assert not [key for key in _cached_keys() if key[0] == "pr"]
    assert not [key for key in github._preview_cache if key[0] == "pr"]


def test_a_read_in_flight_across_a_write_to_another_pr_is_still_cached(fake_gh: FakeGh) -> None:
    fake_gh.delay = 0.4
    github._preview_cache.clear()
    reader = threading.Thread(target=_fetch, args=(1,), kwargs={"background": False})
    reader.start()
    poll_until(lambda: fake_gh.calls["pr view"], timeout_s=5, interval_s=0.01)  # read is in flight
    github.invalidate_pr_reads(f"{REPO}/pull/2")
    reader.join()
    assert [key[3] for key in _cached_keys() if key[0] == "pr"] == ["1"]


def test_waiters_do_not_retry_a_read_that_just_failed(fake_gh: FakeGh) -> None:
    fake_gh.delay = 0.3
    fake_gh.fail = True
    github._preview_cache.clear()
    _in_threads(4, lambda _: _fetch(1, background=True))
    assert (fake_gh.calls["pr list"], fake_gh.calls["pr view"]) == (1, 1)


def test_a_failed_read_is_tried_again_after_a_few_seconds(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_gh.fail = True
    assert _fetch(1, background=False) is None
    fake_gh.fail = False
    _later(monkeypatch, github._FAILED_READ_TTL_SECONDS + 1)
    github._preview_cache.clear()
    assert (_fetch(1, background=False) or {})["number"] == 1


def test_the_sweep_keeps_a_held_lock_and_removes_an_idle_old_one(fake_gh: FakeGh) -> None:
    held_key = ("pr", "token:x", "github.com/acme/widgets", "1", "full")
    idle_key = ("pr", "token:x", "github.com/acme/widgets", "2", "full")
    long_ago = time.time() - 2 * github._STALE_FILE_SECONDS
    with github._single_flight(held_key):
        github._single_flight(idle_key).__enter__()  # take and release the idle lock
        for key in (held_key, idle_key):
            os.utime(github._cache_path(key).with_suffix(".lock"), (long_ago, long_ago))
        github._sweep_cache_directory(github._cache_directory())
        assert github._cache_path(held_key).with_suffix(".lock").exists()
        assert not github._cache_path(idle_key).with_suffix(".lock").exists()


def test_different_prs_missing_at_once_share_one_listing_read(fake_gh: FakeGh) -> None:
    fake_gh.delay = 0.3
    github._preview_cache.clear()
    _in_threads(4, lambda index: _fetch(index + 1, background=True))
    assert fake_gh.calls["pr list"] == 1


PR_KEY = ("pr", "token:x", "github.com/acme/widgets", "1", "full")
LISTING_KEY = ("pr-list", "token:x", "github.com/acme/widgets")


def _is_cached(key: tuple[str, ...]) -> bool:
    return key in github._preview_cache or github._cache_path(key).exists()


def test_a_stamp_landing_during_the_put_removes_the_entry(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = time.time()
    real_replace = os.replace
    stamped = False

    def stamp_then_replace(source: Any, destination: Any) -> None:
        nonlocal stamped
        if not stamped and Path(destination).parent.name != "invalidated":
            stamped = True  # the write lands between the pre-check and the rename
            github.invalidate_pr_reads(f"{REPO}/pull/1")
        real_replace(source, destination)

    monkeypatch.setattr(github.os, "replace", stamp_then_replace)
    github._cache_put(PR_KEY, {"title": "old"}, 60, shared=True, started=started)
    assert stamped
    assert not _is_cached(PR_KEY)


@pytest.mark.parametrize("written", [1, 2])
def test_a_listing_in_flight_across_a_write_is_not_cached(fake_gh: FakeGh, written: int) -> None:
    fake_gh.delay = 0.4
    github._preview_cache.clear()
    reader = threading.Thread(target=_fetch, args=(1,), kwargs={"background": True})
    reader.start()
    poll_until(lambda: fake_gh.calls["pr list"], timeout_s=5, interval_s=0.01)  # list in flight
    github.invalidate_pr_reads(f"{REPO}/pull/{written}")
    reader.join()
    assert not [key for key in _cached_keys() if key[0] == "pr-list"]
    assert not [key for key in github._preview_cache if key[0] == "pr-list"]


@pytest.mark.parametrize("key", [PR_KEY, LISTING_KEY])
def test_a_wildcard_stamp_blocks_reads_in_flight(fake_gh: FakeGh, key: tuple[str, ...]) -> None:
    started = time.time()
    github.invalidate_pr_reads(None)
    github._cache_put(key, {"title": "old"}, 60, shared=True, started=started)
    assert not _is_cached(key)


def test_a_per_pr_stamp_blocks_a_listing_put_in_flight(fake_gh: FakeGh) -> None:
    started = time.time()
    github.invalidate_pr_reads(f"{REPO}/pull/3")
    github._cache_put(LISTING_KEY, [], 60, shared=True, started=started)
    assert not _is_cached(LISTING_KEY)


def test_a_failing_listing_is_read_once_for_different_prs(fake_gh: FakeGh) -> None:
    fake_gh.delay = 0.3
    fake_gh.fail = True
    github._preview_cache.clear()
    _in_threads(4, lambda index: _fetch(index + 1, background=True))
    assert fake_gh.calls["pr list"] == 1


def test_old_invalidation_stamps_are_swept(fake_gh: FakeGh) -> None:
    github.invalidate_pr_reads(f"{REPO}/pull/1")
    github.invalidate_pr_reads(f"{REPO}/pull/2")
    stamps = sorted(github._invalidation_directory().glob("*.json"))
    assert len(stamps) == 2
    long_ago = time.time() - 2 * github._STALE_FILE_SECONDS
    os.utime(stamps[0], (long_ago, long_ago))
    github._cache_put(PR_KEY, {"title": "new"}, 60, shared=True)
    assert sorted(github._invalidation_directory().glob("*.json")) == stamps[1:]


def test_a_stamp_keeps_the_later_time(fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch) -> None:
    github.invalidate_pr_reads(f"{REPO}/pull/1")
    (stamp,) = github._invalidation_directory().glob("*.json")
    first = json.loads(stamp.read_text())["at"]
    _later(monkeypatch, -100)  # a runner whose clock is behind
    github.invalidate_pr_reads(f"{REPO}/pull/1")
    assert json.loads(stamp.read_text())["at"] == first


TRACKED_NUMBERS = (1, 2, 3)


def _lookup_title(number: int) -> tuple[str | None, bool]:
    """One session's title lookup, in a runner process that starts with an empty memory cache."""
    github._preview_cache.clear()
    reference = PullRequestRef.from_url(f"{REPO}/pull/{number}")
    return github._pr_title_before("/work", reference, time.monotonic() + 5)


def test_sessions_tracking_the_same_prs_make_one_title_read_per_pr(fake_gh: FakeGh) -> None:
    for _session in range(5):
        for _refresh in range(3):
            for number in TRACKED_NUMBERS:
                assert _lookup_title(number) == ("read as reader", False)
    assert fake_gh.calls["pr view"] == len(TRACKED_NUMBERS)


@pytest.mark.parametrize("kind", ["full", "background", "listing"])
def test_a_fresh_read_of_the_pr_answers_the_title_without_gh(fake_gh: FakeGh, kind: str) -> None:
    if kind == "listing":
        _read(2, background=True)  # one pr list, which holds every open PR
    else:
        _fetch(2, background=kind == "background")
    calls_before = dict(fake_gh.calls)
    assert _lookup_title(2) == ("read as reader", False)
    assert fake_gh.calls == calls_before


def test_a_title_lookup_that_timed_out_is_not_retried_by_other_sessions(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_gh.time_out = True
    assert _lookup_title(1) == (None, True)
    assert _lookup_title(1) == (None, True)  # another session, inside the failed-read window
    assert fake_gh.calls["pr view"] == 1
    fake_gh.time_out = False
    _later(monkeypatch, github._FAILED_READ_TTL_SECONDS + 1)
    assert _lookup_title(1) == ("read as reader", False)
    assert fake_gh.calls["pr view"] == 2


def test_a_failed_title_lookup_that_did_not_time_out_is_not_flagged_as_one(
    fake_gh: FakeGh,
) -> None:
    fake_gh.fail = True
    assert _lookup_title(1) == (None, False)
    assert _lookup_title(1) == (None, False)
    assert fake_gh.calls["pr view"] == 1


def test_a_title_edit_shows_on_the_next_lookup_and_spares_other_prs(fake_gh: FakeGh) -> None:
    assert _lookup_title(1)[0] == "read as reader"
    assert _lookup_title(2)[0] == "read as reader"
    fake_gh.titles = {1: "edited", 2: "edited"}
    assert _lookup_title(1)[0] == "read as reader"  # still inside the title window
    github.invalidate_pr_reads(f"{REPO}/pull/1")  # what every details write does
    assert _lookup_title(1)[0] == "edited"
    assert _lookup_title(2)[0] == "read as reader"
    assert fake_gh.calls["pr view"] == 3


def test_a_merged_pr_title_is_kept_longer_than_an_open_ones(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_gh.states[MERGED_NUMBER] = "MERGED"
    _lookup_title(MERGED_NUMBER)
    _lookup_title(1)
    _later(monkeypatch, github._OPEN_PR_TITLE_TTL_SECONDS + 1)
    _lookup_title(MERGED_NUMBER)
    _lookup_title(1)
    assert fake_gh.calls["pr view"] == 3


def test_an_unknown_identity_title_is_read_every_time_and_never_cached(fake_gh: FakeGh) -> None:
    fake_gh.accounts = _accounts("carol", state="error")
    assert _lookup_title(1) == ("read as carol", False)
    assert _lookup_title(1) == ("read as carol", False)
    assert fake_gh.calls["pr view"] == 2
    assert not [key for key in _cached_keys() if key[0] == "pr"]


def test_each_identity_reads_its_own_title(fake_gh: FakeGh) -> None:
    fake_gh.accounts = _accounts("carol", "alice")
    fake_gh.tokens = {"alice": "alice-token"}
    fake_gh.preference = "alice"
    assert _lookup_title(1)[0] == "read as alice"
    fake_gh.preference = None
    assert _lookup_title(1)[0] == "read as carol"


def test_a_title_read_in_flight_across_a_write_is_not_cached(fake_gh: FakeGh) -> None:
    fake_gh.delay = 0.4
    reader = threading.Thread(target=_lookup_title, args=(1,))
    reader.start()
    poll_until(lambda: fake_gh.calls["pr view"], timeout_s=5, interval_s=0.01)  # read is in flight
    github.invalidate_pr_reads(f"{REPO}/pull/1")
    reader.join()
    assert not [key for key in _cached_keys() if key[0] == "pr"]


def test_concurrent_title_misses_share_one_read(fake_gh: FakeGh) -> None:
    fake_gh.delay = 0.3
    github._preview_cache.clear()
    reference = PullRequestRef.from_url(f"{REPO}/pull/1")
    _in_threads(4, lambda _: github._pr_title_before("/work", reference, time.monotonic() + 5))
    assert fake_gh.calls["pr view"] == 1


def _host_refresh(root: Path, session_id: str, *, background: bool = True) -> dict[str, Any]:
    """One session's refresh served by the host, with the server's query parameters."""
    from omnigent.host.connect import HostProcess
    from omnigent.workspace_fs import WorkspaceReader

    github._preview_cache.clear()
    params = {"background": "1"} if background else {}
    return HostProcess._dispatch_fs_op(WorkspaceReader(root), "github_info", session_id, params)


def _link(session_id: str, numbers: tuple[int, ...]) -> None:
    references = [PullRequestRef.from_url(f"{REPO}/pull/{number}") for number in numbers]
    SessionPrRegistry(session_id).record(references, relationship="created", source="test")


def test_a_background_refresh_through_the_host_reads_the_listing(
    fake_gh: FakeGh, tmp_path: Path
) -> None:
    _link("session", (1,))
    info = _host_refresh(tmp_path, "session")
    assert info["pr"]["state"] == "OPEN"
    assert fake_gh.calls["pr list"] == 1
    assert fake_gh.calls["pr view"] == 0
    assert not [argv for argv in fake_gh.argvs if argv[0] == "api"]  # no GraphQL conversation


def test_one_refresh_wave_of_35_sessions_linking_3_prs_through_the_host(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The server refreshes each session once, about a second apart."""
    linked = (1, 2, 3)
    for index in range(35):
        shift = index % len(linked)  # each PR is the first linked PR of some sessions
        _link(f"session-{index}", linked[shift:] + linked[:shift])
    started = time.time()
    for index in range(35):
        monkeypatch.setattr(github.time, "time", lambda index=index: started + index)
        _host_refresh(tmp_path, f"session-{index}")
    assert fake_gh.calls == {"auth status": 2, "pr list": 1}


def test_background_reads_pause_until_the_rate_limit_resets(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _link("session", (1, 2, 3))
    fake_gh.rate_limited = True
    fake_gh.quota_reset = time.time() + 600
    assert _read(1, background=True)["pr"] is None
    fake_gh.rate_limited = False
    calls = dict(fake_gh.calls)
    for number in OPEN_NUMBERS:
        assert _read(number, background=True)["pr"] is None
    assert _host_refresh(tmp_path, "session")["pr"] is None  # its title lookups wait too
    assert fake_gh.calls == calls
    assert _read(1, background=False)["pr"]["state"] == "OPEN"  # a viewer still reads
    _later(monkeypatch, 601)
    assert _read(2, background=True)["pr"]["state"] == "OPEN"


def test_a_rate_limit_with_no_reset_time_pauses_for_15_minutes(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_gh.rate_limited = True
    _read(1, background=True)
    fake_gh.rate_limited = False
    _later(monkeypatch, 15 * 60 - 5)
    assert _read(1, background=True)["pr"] is None
    _later(monkeypatch, 15 * 60 + 1)
    assert _read(1, background=True)["pr"]["state"] == "OPEN"


def _paused_for(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch, seconds: float, *, setup: Any = None
) -> None:
    """After a rate-limit error the stamp holds just under ``seconds`` and ends just after."""
    (setup or (lambda: _read(1, background=True)))()
    fake_gh.rate_limited = fake_gh.conversation_rate_limited = False
    _later(monkeypatch, seconds - 5)
    assert _read(1, background=True)["pr"] is None
    _later(monkeypatch, seconds + 1)
    assert _read(1, background=True)["pr"]["state"] == "OPEN"


def test_an_open_pr_outside_the_listing_is_read_once_in_the_background_window(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    beyond_listing = 50
    _read(beyond_listing, background=True)
    _later(monkeypatch, 119)
    _read(beyond_listing, background=True)
    assert (fake_gh.calls["pr list"], fake_gh.calls["pr view"]) == (1, 1)


def test_a_secondary_rate_limit_pauses_for_15_minutes_whatever_the_quota_says(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_gh.rate_limited = True
    fake_gh.rate_limit_message = "GraphQL: You have exceeded a secondary rate limit."
    fake_gh.quota_remaining = 100  # the hourly quota is not spent, so its reset time is moot
    fake_gh.quota_reset = time.time() + 600
    _paused_for(fake_gh, monkeypatch, 15 * 60)


def test_the_pause_ends_within_an_hour_of_a_later_reset(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_gh.rate_limited = True
    fake_gh.quota_reset = time.time() + 2 * 3600
    _paused_for(fake_gh, monkeypatch, 3600)


def test_a_rate_limited_conversation_query_pauses_background_reads(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_gh.conversation_rate_limited = True
    assert _read(1, background=False)["pr"]["state"] == "OPEN"  # the viewer's read still works
    assert fake_gh.calls["api rate_limit"] == 1
    assert _read(2, background=True)["pr"] is None
    assert fake_gh.calls["pr list"] == 0


def test_a_background_read_whose_own_listing_hits_the_limit_stops_there(fake_gh: FakeGh) -> None:
    fake_gh.rate_limited = True
    assert _read(1, background=True)["pr"] is None
    assert (fake_gh.calls["pr list"], fake_gh.calls["pr view"]) == (1, 0)
    assert fake_gh.calls["api rate_limit"] == 1


@pytest.mark.parametrize("copied_from_disk", [False, True], ids=["written", "copied-from-disk"])
@pytest.mark.parametrize("number", [1, 50], ids=["listed", "beyond-listing"])
def test_another_process_stops_serving_a_pr_after_a_write_within_the_viewer_window(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch, number: int, copied_from_disk: bool
) -> None:
    _read(number, background=True)
    if copied_from_disk:  # the second process starts empty and copies the first one's read
        github._preview_cache.clear()
        _read(number, background=True)
    other_process_memory = dict(github._preview_cache)  # what a second process still holds
    fake_gh.titles[number] = "after the write"
    github.invalidate_pr_reads(f"{REPO}/pull/{number}")
    github._preview_cache.update(other_process_memory)
    clock, wall = time.monotonic(), time.time()
    monkeypatch.setattr(github.time, "monotonic", lambda: clock + 60)
    monkeypatch.setattr(github.time, "time", lambda: wall + 60)
    with github.background_reads(True):
        info = github._reference_info("/work", PullRequestRef.from_url(f"{REPO}/pull/{number}"))
    assert info["pr"]["title"] == "after the write"
    assert fake_gh.calls["pr list"] == 2
