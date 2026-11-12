"""Chat link previews read through the host's ``gh``, or without a login for public repos."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app
from omnigent.runner import github_resource as github
from tests.runner.helpers import NullServerClient

SIGNED_IN = json.dumps(
    {"hosts": {"github.com": [{"login": "octo", "active": True, "state": "success"}]}}
)
LEGACY_GH = "legacy"
SIGNED_OUT = json.dumps({"hosts": {}})
REAL_REPO_TOKEN = github._repo_token  # the autouse fixture stubs the module's
GhCall = tuple[list[str], str | None]


def _api(path: str, body: object) -> tuple[str, tuple[int, str, str]]:
    return path, (0, json.dumps(body), "")


class FakeGh:
    """A ``gh`` stand-in: answers ``auth status`` and routes ``pr view`` / ``api`` by path."""

    def __init__(self, auth: str, api: dict[str, tuple[int, str, str]], pr: dict | None) -> None:
        self.auth = auth
        self.api = api
        self.pr = pr
        self.calls: list[GhCall] = []
        self.cwds: set[str] = set()

    def __call__(
        self, argv: list[str], *, cwd: str, token: str | None = None, timeout: float | None = None
    ) -> tuple[int | None, str, str]:
        self.cwds.add(cwd)
        self.calls.append((argv, token))
        if argv[:2] == ["auth", "status"]:
            if self.auth == LEGACY_GH:
                # An older gh has no --json for auth status, but its plain status succeeds.
                return (1, "", "unknown flag: --json") if "--json" in argv else (0, "", "")
            return 0, self.auth, ""
        if argv[:2] == ["pr", "view"]:
            if self.pr is None:
                return 1, "", "GraphQL: Could not resolve to a PullRequest (HTTP 404)"
            return 0, json.dumps(self.pr), ""
        assert argv[:3] == ["api", "--hostname", "github.com"], argv
        return self.api.get(argv[3], (1, "", "gh: Not Found (HTTP 404)"))


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    github._preview_cache.clear()
    monkeypatch.setattr(github, "_public_blocked_until", 0.0)
    monkeypatch.setattr(github.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(github, "_repo_token", lambda _root, _repo: "repo-token")


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    auth: str = SIGNED_IN,
    api: dict[str, tuple[int, str, str]] | None = None,
    pr: dict | None = None,
) -> FakeGh:
    fake = FakeGh(auth, api or {}, pr)
    monkeypatch.setattr(github, "_gh", fake)
    return fake


def _forbid_public_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a signed-in preview must not reach GitHub except through gh")

    monkeypatch.setattr(github.httpx, "get", refuse)


CHECK_RUNS = {
    "check_runs": [
        {"status": "completed", "conclusion": "success"},
        {"status": "completed", "conclusion": "skipped"},
        {"status": "completed", "conclusion": "failure"},
        {"status": "in_progress", "conclusion": None},
    ]
}


def test_pull_preview_summarises_gh_pr_view_with_the_panels_check_rollup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(
        monkeypatch,
        pr={
            "title": "Fix rendering",
            "state": "MERGED",
            "isDraft": False,
            "labels": [{"name": "bug", "color": "D73A4A", "description": ""}],
            "statusCheckRollup": [
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "a"},
                {"status": "COMPLETED", "conclusion": "FAILURE", "name": "b"},
                {"status": "IN_PROGRESS", "name": "c"},
                {"state": "SUCCESS", "context": "d"},
            ],
        },
    )
    _forbid_public_reads(monkeypatch)

    preview = github.github_link_preview("/workspace", "acme", "app", "pull", "42")

    assert preview == {
        "kind": "pull",
        "title": "Fix rendering",
        "state": "merged",
        "labels": [{"name": "bug", "color": "d73a4a", "description": None}],
        "checks": {"passed": 2, "failed": 1, "pending": 1},
    }
    view = next(argv for argv, _ in fake.calls if argv[:2] == ["pr", "view"])
    assert view[:5] == ["pr", "view", "42", "-R", "github.com/acme/app"]
    assert {token for argv, token in fake.calls if argv[0] != "auth"} == {"repo-token"}


def test_draft_pull_without_checks_has_null_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        pr={
            "title": "WIP",
            "state": "OPEN",
            "isDraft": True,
            "labels": [],
            "statusCheckRollup": [],
        },
    )

    preview = github.github_link_preview("/workspace", "acme", "app", "pull", "7")

    assert preview is not None
    assert (preview["state"], preview["checks"]) == ("draft", None)


def test_issue_commit_and_run_read_through_gh_api(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(
        monkeypatch,
        api=dict(
            [
                _api(
                    "/repos/acme/app/issues/3",
                    {
                        "title": "Old",
                        "state": "closed",
                        "state_reason": "not_planned",
                        "labels": [{"name": "wontfix", "color": "ffffff"}],
                    },
                ),
                _api(
                    "/repos/acme/app/commits/abc1234",
                    {"sha": "abc1234full", "commit": {"message": "feat: x\n\nbody"}},
                ),
                _api("/repos/acme/app/commits/abc1234full/check-runs?per_page=100", CHECK_RUNS),
                _api(
                    "/repos/acme/app/commits/abc1234full/status?per_page=100",
                    {"statuses": [{"state": "error"}]},
                ),
                _api(
                    "/repos/acme/app/actions/runs/99",
                    {"display_title": "CI", "status": "completed", "conclusion": "failure"},
                ),
            ]
        ),
    )
    _forbid_public_reads(monkeypatch)

    issue = github.github_link_preview("/workspace", "acme", "app", "issue", "3")
    commit = github.github_link_preview("/workspace", "acme", "app", "commit", "abc1234")
    run = github.github_link_preview("/workspace", "acme", "app", "run", "99")

    assert issue == {
        "kind": "issue",
        "title": "Old",
        "state": "not_planned",
        "labels": [{"name": "wontfix", "color": "ffffff", "description": None}],
        "checks": None,
    }
    assert commit is not None
    assert (commit["title"], commit["state"], commit["checks"]) == (
        "feat: x",
        None,
        {"passed": 2, "failed": 2, "pending": 1},
    )
    assert run is not None
    assert (run["kind"], run["title"], run["state"]) == ("run", "CI", "failure")
    assert all(argv[0] in ("auth", "api") for argv, _ in fake.calls)


def test_issue_link_to_a_pull_request_previews_as_a_pull(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        api=dict(
            [
                _api("/repos/acme/app/issues/5", {"title": "T", "pull_request": {"url": "x"}}),
                _api(
                    "/repos/acme/app/pulls/5",
                    {"title": "T", "state": "open", "draft": True, "head": {"sha": "s5"}},
                ),
            ]
        ),
    )

    preview = github.github_link_preview("/workspace", "acme", "app", "issue", "5")

    assert preview is not None
    assert (preview["kind"], preview["state"]) == ("pull", "draft")


@pytest.mark.parametrize("kind", ["pull", "issue", "commit", "run"])
def test_unreadable_repo_is_null_for_every_kind(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    _install(monkeypatch)
    _forbid_public_reads(monkeypatch)

    assert github.github_link_preview("/workspace", "acme", "private", kind, "1234567") is None


def test_results_are_cached_for_a_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(
        monkeypatch,
        api=dict([_api("/repos/acme/app/actions/runs/9", {"name": "CI", "status": "queued"})]),
    )

    first = github.github_link_preview("/workspace", "acme", "app", "run", "9")
    second = github.github_link_preview("/workspace", "ACME", "app", "run", "9")

    assert first == second
    assert sum(argv[0] == "api" for argv, _ in fake.calls) == 1


def test_missing_gh_leaves_the_link_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github.shutil, "which", lambda _name: None)
    _forbid_public_reads(monkeypatch)

    assert github.github_link_preview("/workspace", "acme", "app", "pull", "1") is None


@pytest.mark.parametrize(
    ("owner", "repo", "kind", "ref"),
    [
        ("acme", "app", "page", "1"),
        ("acme", "app", "pull", "abc"),
        ("acme", "app", "commit", "xyz1234"),
        ("o..o", "app", "pull", "1"),
        ("acme", "app/../x", "pull", "1"),
        ("acme", "app", "pull", "1?x=1"),
        ("acme\n", "app", "pull", "1"),
        ("acme", "app\n", "pull", "1"),
        ("acme", "app", "pull", "42\n"),
        ("acme", "app", "commit", "abc1234\n"),
    ],
)
def test_invalid_parts_run_nothing(
    monkeypatch: pytest.MonkeyPatch, owner: str, repo: str, kind: str, ref: str
) -> None:
    fake = _install(monkeypatch)

    with pytest.raises(ValueError):
        github.github_link_preview("/workspace", owner, repo, kind, ref)
    assert fake.calls == []


class PublicApi:
    """GitHub's unauthenticated API: serves ``routes`` and counts requests."""

    def __init__(self, routes: dict[str, httpx.Response]) -> None:
        self.routes = routes
        self.requests: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        self.requests.append((url, headers))
        path = url.removeprefix("https://api.github.com")
        return self.routes.get(path, httpx.Response(404))


def _signed_out(
    monkeypatch: pytest.MonkeyPatch, routes: dict[str, httpx.Response]
) -> tuple[PublicApi, FakeGh]:
    fake = _install(monkeypatch, auth=SIGNED_OUT)
    api = PublicApi(routes)
    monkeypatch.setattr(github.httpx, "get", api)
    return api, fake


def test_signed_out_gh_previews_a_public_repo_without_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, fake = _signed_out(
        monkeypatch,
        {
            "/repos/acme/app/pulls/42": httpx.Response(
                200,
                json={"title": "Public fix", "state": "open", "head": {"sha": "s1"}, "labels": []},
            ),
            "/repos/acme/app/commits/s1/check-runs?per_page=100": httpx.Response(
                200, json=CHECK_RUNS
            ),
            "/repos/acme/app/commits/s1/status?per_page=100": httpx.Response(
                200, json={"statuses": []}
            ),
        },
    )

    preview = github.github_link_preview("/workspace", "acme", "app", "pull", "42")

    assert preview is not None
    assert (preview["title"], preview["state"]) == ("Public fix", "open")
    assert preview["checks"] == {"passed": 2, "failed": 1, "pending": 1}
    assert all("Authorization" not in headers for _url, headers in api.requests)
    assert all(argv[0] == "auth" for argv, _ in fake.calls)


def test_signed_out_gh_leaves_a_private_repo_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    api, _ = _signed_out(monkeypatch, {})

    assert github.github_link_preview("/workspace", "acme", "private", "issue", "1") is None
    assert len(api.requests) == 1


def test_rate_limit_leaves_links_plain_and_pauses_public_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limited = httpx.Response(403, headers={"x-ratelimit-remaining": "0"})
    api, _ = _signed_out(
        monkeypatch,
        {
            "/repos/acme/app/issues/1": limited,
            "/repos/acme/app/issues/2": httpx.Response(200, json={"title": "T", "state": "open"}),
        },
    )

    assert github.github_link_preview("/workspace", "acme", "app", "issue", "1") is None
    # The allowance is spent: no further request goes out, even for a readable link.
    assert github.github_link_preview("/workspace", "acme", "app", "issue", "2") is None
    assert github.github_link_preview("/workspace", "acme", "app", "issue", "1") is None
    assert len(api.requests) == 1


def test_public_results_are_cached_longer_than_signed_in_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(github.time, "monotonic", lambda: clock[0])
    api, _ = _signed_out(
        monkeypatch,
        {"/repos/acme/app/issues/2": httpx.Response(200, json={"title": "T", "state": "open"})},
    )

    github.github_link_preview("/workspace", "acme", "app", "issue", "2")
    clock[0] += 5 * 60
    github.github_link_preview("/workspace", "acme", "app", "issue", "2")
    assert len(api.requests) == 1

    clock[0] += 11 * 60
    github.github_link_preview("/workspace", "acme", "app", "issue", "2")
    assert len(api.requests) == 2


async def test_runner_route_serves_the_preview_and_rejects_invalid_parts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(
        monkeypatch,
        api=dict([_api("/repos/acme/app/actions/runs/9", {"name": "CI", "status": "queued"})]),
    )
    app = create_runner_app(
        runner_workspace=tmp_path,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    path = "/v1/sessions/session/resources/github/preview"
    query = {"owner": "acme", "repo": "app", "kind": "run", "ref": "9"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        served = await client.get(path, params=query)
        invalid = await client.get(path, params={**query, "owner": "o..o"})

    assert served.status_code == 200, served.text
    assert served.json()["preview"]["title"] == "CI"
    assert invalid.status_code == 400


def _signed_in_as(login: str) -> str:
    return json.dumps(
        {"hosts": {"github.com": [{"login": login, "active": True, "state": "success"}]}}
    )


def test_cached_results_do_not_cross_an_account_change(monkeypatch: pytest.MonkeyPatch) -> None:
    private = dict([_api("/repos/acme/secret/issues/1", {"title": "Private", "state": "open"})])
    fake = _install(monkeypatch, auth=_signed_in_as("alice"), api=private)
    monkeypatch.setattr(github, "_repo_token", lambda _root, _repo: None)

    seen_by_alice = github.github_link_preview("/workspace", "acme", "secret", "issue", "1")
    fake.auth, fake.api = _signed_in_as("bob"), {}
    seen_by_bob = github.github_link_preview("/workspace", "acme", "secret", "issue", "1")
    # A cached null does not hide a repo the new account can read either.
    fake.auth, fake.api = _signed_in_as("carol"), private
    seen_by_carol = github.github_link_preview("/workspace", "acme", "secret", "issue", "1")

    assert seen_by_alice is not None and seen_by_alice["title"] == "Private"
    assert seen_by_bob is None
    assert seen_by_carol is not None and seen_by_carol["title"] == "Private"


def test_cached_results_follow_the_repos_preferred_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = dict([_api("/repos/acme/secret/issues/1", {"title": "Private", "state": "open"})])
    fake = _install(monkeypatch, auth=_signed_in_as("alice"), api=private)
    preference = {"login": "alice"}
    monkeypatch.setattr(github, "_repo_token", lambda _root, _repo: f"token-{preference['login']}")

    assert github.github_link_preview("/workspace", "acme", "secret", "issue", "1") is not None
    preference["login"] = "bob"
    fake.api = {}
    assert github.github_link_preview("/workspace", "acme", "secret", "issue", "1") is None


def test_a_preferred_account_whose_token_is_missing_is_keyed_by_the_active_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = dict([_api("/repos/acme/secret/issues/1", {"title": "Private", "state": "open"})])
    fake = _install(monkeypatch, auth=_signed_in_as("alice"), api=private)
    # A saved preference whose token can't be fetched: gh reads as the active account.
    monkeypatch.setattr(github, "_in_sandbox", lambda: False)
    monkeypatch.setattr(github._config, "github_account_preference", lambda _key: "alice")
    monkeypatch.setattr(github, "_gh_auth_token", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(github, "_repo_token", REAL_REPO_TOKEN)

    assert github.github_link_preview("/workspace", "acme", "secret", "issue", "1") is not None
    fake.auth, fake.api = _signed_in_as("bob"), {}
    assert github.github_link_preview("/workspace", "acme", "secret", "issue", "1") is None


def test_an_older_gh_never_shares_a_cached_preview_between_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = dict([_api("/repos/acme/secret/issues/1", {"title": "Private", "state": "open"})])
    fake = _install(monkeypatch, auth=LEGACY_GH, api=private)
    monkeypatch.setattr(github, "_repo_token", lambda _root, _repo: None)

    assert github.github_link_preview("/workspace", "acme", "secret", "issue", "1") is not None
    # The account switched; an older gh can't say so, so the read must go to GitHub again.
    fake.api = {}
    assert github.github_link_preview("/workspace", "acme", "secret", "issue", "1") is None


def test_an_older_gh_with_a_login_reads_through_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(
        monkeypatch,
        auth=LEGACY_GH,
        api=dict([_api("/repos/acme/secret/issues/1", {"title": "Private", "state": "open"})]),
    )
    _forbid_public_reads(monkeypatch)

    preview = github.github_link_preview("/workspace", "acme", "secret", "issue", "1")

    assert preview is not None and preview["title"] == "Private"
    assert any(argv[0] == "api" for argv, _ in fake.calls)


def test_a_failed_active_account_is_not_cached_under_another_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readable = dict([_api("/repos/acme/secret/issues/1", {"title": "Private", "state": "open"})])
    both = json.dumps(
        {
            "hosts": {
                "github.com": [
                    {"login": "alice", "active": True, "state": "error"},
                    {"login": "bob", "active": False, "state": "success"},
                ]
            }
        }
    )
    fake = _install(monkeypatch, auth=both, api={})
    monkeypatch.setattr(github, "_repo_token", lambda _root, _repo: None)

    # gh reads as alice (active, failing): a refusal must not be remembered for bob.
    assert github.github_link_preview("/workspace", "acme", "secret", "issue", "1") is None
    fake.auth, fake.api = _signed_in_as("bob"), readable
    preview = github.github_link_preview("/workspace", "acme", "secret", "issue", "1")

    assert preview is not None and preview["title"] == "Private"


def test_an_ambient_gh_token_names_the_identity_over_the_active_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = dict([_api("/repos/acme/secret/issues/1", {"title": "Private", "state": "open"})])
    fake = _install(monkeypatch, auth=_signed_in_as("alice"), api=private)
    monkeypatch.setattr(github, "_repo_token", lambda _root, _repo: None)
    monkeypatch.setattr(github, "_in_sandbox", lambda: False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "token-one")

    assert github.github_link_preview("/workspace", "acme", "secret", "issue", "1") is not None
    # Same active account, but gh now runs as another token's owner.
    monkeypatch.setenv("GH_TOKEN", "token-two")
    fake.api = {}
    assert github.github_link_preview("/workspace", "acme", "secret", "issue", "1") is None


def test_a_result_read_while_the_account_switched_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = dict([_api("/repos/acme/secret/issues/1", {"title": "Private", "state": "open"})])
    fake = _install(monkeypatch, auth=_signed_in_as("alice"), api=private)
    monkeypatch.setattr(github, "_repo_token", lambda _root, _repo: None)
    original = fake.__call__
    switched = []

    def switch_before_the_read(argv: list[str], **kwargs: Any) -> tuple[int | None, str, str]:
        if argv[0] == "api" and not switched:
            switched.append(True)
            # Bob became active after the identity was decided, and the read ran as Bob.
            fake.auth, fake.api = _signed_in_as("bob"), {}
        return original(argv, **kwargs)

    monkeypatch.setattr(github, "_gh", switch_before_the_read)
    assert github.github_link_preview("/workspace", "acme", "secret", "issue", "1") is None

    # Alice is active again: Bob's refusal must not be remembered under her name.
    fake.auth, fake.api = _signed_in_as("alice"), private
    preview = github.github_link_preview("/workspace", "acme", "secret", "issue", "1")
    assert preview is not None and preview["title"] == "Private"
