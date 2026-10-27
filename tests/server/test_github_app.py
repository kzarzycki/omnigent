"""Tests for the GitHub App HTTP client.

Network half only. HTTP is mocked at the transport boundary
(``httpx.MockTransport``); the App config (which owns the secret-shaped
fields) is built by :func:`tests.server.github_app_fixtures.make_config`
so this file never names a client secret alongside the httpx sink.
"""

from __future__ import annotations

import httpx
import pytest

from omnigent.server.github_app import GitHubAppError
from omnigent.server.github_app_client import GitHubAppClient
from tests.server.github_app_fixtures import make_config


def _client(handler) -> GitHubAppClient:
    return GitHubAppClient(make_config(), transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_exchange_code_parses_tokens() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/login/oauth/access_token"
        return httpx.Response(
            200,
            json={
                "access_token": "ghu_new",
                "refresh_token": "ghr_new",
                "expires_in": 28800,
                "refresh_token_expires_in": 15897600,
                "scope": "",
            },
        )

    tokens = await _client(handler).exchange_code("code123")
    assert tokens.access_token == "ghu_new"
    assert tokens.refresh_token == "ghr_new"


@pytest.mark.asyncio
async def test_exchange_code_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "bad_verification_code"})

    with pytest.raises(GitHubAppError):
        await _client(handler).exchange_code("nope")


@pytest.mark.asyncio
async def test_refresh_token_roundtrip() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "ghu_refreshed", "scope": "repo"})

    tokens = await _client(handler).refresh_token("ghr_old")
    assert tokens.access_token == "ghu_refreshed"


@pytest.mark.asyncio
async def test_token_endpoint_non_200_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(GitHubAppError):
        await _client(handler).exchange_code("c")


@pytest.mark.asyncio
async def test_fetch_login() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer ghu_x"
        return httpx.Response(200, json={"login": "octocat", "id": 583231})

    login, uid = await _client(handler).fetch_login("ghu_x")
    assert (login, uid) == ("octocat", 583231)


@pytest.mark.asyncio
async def test_list_repos_projects_fields_and_stops_on_short_page() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.url.path == "/user/repos"
        return httpx.Response(
            200,
            json=[
                {
                    "full_name": "caffeinelabs/app",
                    "clone_url": "https://github.com/caffeinelabs/app.git",
                    "default_branch": "main",
                    "private": True,
                    "pushed_at": "2026-07-28T00:00:00Z",
                    "stargazers_count": 3,
                },
                {"description": "no full_name — skipped"},
            ],
        )

    repos, truncated = await _client(handler).list_repos("ghu_x")
    # Short page (< per_page) → only one request, no over-fetch, not truncated.
    assert len(calls) == 1
    assert truncated is False
    # Only the projected keys survive; the entry missing full_name is dropped.
    assert repos == [
        {
            "full_name": "caffeinelabs/app",
            "clone_url": "https://github.com/caffeinelabs/app.git",
            "default_branch": "main",
            "private": True,
            "pushed_at": "2026-07-28T00:00:00Z",
        }
    ]


@pytest.mark.asyncio
async def test_list_repos_flags_truncation_at_page_cap() -> None:
    # Every page comes back full → the page cap is hit and truncated=True so the
    # UI can say the list is partial instead of silently dropping repos.
    def handler(request: httpx.Request) -> httpx.Response:
        page = [
            {"full_name": f"o/r{i}", "clone_url": None, "default_branch": "main"}
            for i in range(100)  # a full per_page page
        ]
        return httpx.Response(200, json=page)

    repos, truncated = await _client(handler).list_repos("ghu_x")
    assert truncated is True
    assert len(repos) == 300  # 3 full pages (the cap)


@pytest.mark.asyncio
async def test_list_repos_non_200_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    with pytest.raises(GitHubAppError):
        await _client(handler).list_repos("ghu_bad")


@pytest.mark.asyncio
async def test_list_branches_returns_names_and_stops_on_short_page() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.url.path == "/repos/caffeinelabs/app/branches"
        return httpx.Response(
            200,
            json=[
                {"name": "main", "protected": True},
                {"name": "dev"},
                {"no_name": "skipped"},
            ],
        )

    branches = await _client(handler).list_branches("ghu_x", "caffeinelabs/app")
    # Short page (< per_page) → single request, entries without a name dropped.
    assert calls == ["/repos/caffeinelabs/app/branches"]
    assert branches == ["main", "dev"]


@pytest.mark.asyncio
async def test_list_branches_non_200_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    with pytest.raises(GitHubAppError):
        await _client(handler).list_branches("ghu_bad", "caffeinelabs/nope")


def _preview_handler(routes: dict[str, httpx.Response], seen: list[str]):
    """Answer the API paths in ``routes`` and record every requested URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return routes.get(request.url.path, httpx.Response(404))

    return handler


_CHECK_RUNS = httpx.Response(
    200,
    json={
        "check_runs": [
            {"status": "completed", "conclusion": "success"},
            {"status": "completed", "conclusion": "skipped"},
            {"status": "completed", "conclusion": "failure"},
            {"status": "in_progress", "conclusion": None},
        ]
    },
)


@pytest.mark.asyncio
async def test_pull_preview_reads_only_the_github_api_with_the_users_token() -> None:
    seen: list[str] = []
    tokens: list[str] = []
    routes = {
        "/repos/acme/app/pulls/42": httpx.Response(
            200,
            json={
                "title": "Fix rendering",
                "state": "closed",
                "merged": True,
                "draft": False,
                "labels": [{"name": "bug", "color": "d73a4a"}],
                "head": {"sha": "abc123"},
            },
        ),
        "/repos/acme/app/commits/abc123/check-runs": _CHECK_RUNS,
    }
    handler = _preview_handler(routes, seen)

    def recording(request: httpx.Request) -> httpx.Response:
        tokens.append(request.headers["Authorization"])
        return handler(request)

    preview = await _client(recording).fetch_link_preview("ghu_a", "acme/app", "pull", "42")

    assert preview == {
        "kind": "pull",
        "title": "Fix rendering",
        "state": "merged",
        "labels": [{"name": "bug", "color": "d73a4a", "description": None}],
        "checks": {"passed": 2, "failed": 1, "pending": 1},
    }
    assert all(url.startswith("https://api.github.com/repos/acme/app/") for url in seen)
    assert tokens == ["Bearer ghu_a"] * 3


@pytest.mark.asyncio
async def test_issue_link_that_is_a_pull_request_previews_as_a_draft_pull() -> None:
    seen: list[str] = []
    routes = {
        "/repos/acme/app/issues/7": httpx.Response(
            200, json={"title": "Draft", "state": "open", "pull_request": {"url": "x"}}
        ),
        "/repos/acme/app/pulls/7": httpx.Response(
            200, json={"title": "Draft", "state": "open", "draft": True, "head": {"sha": "d1"}}
        ),
    }
    preview = await _client(_preview_handler(routes, seen)).fetch_link_preview(
        "ghu_a", "acme/app", "issue", "7"
    )

    assert preview is not None
    assert (preview["kind"], preview["state"], preview["checks"]) == ("pull", "draft", None)


@pytest.mark.asyncio
async def test_issue_closed_as_not_planned_keeps_that_state() -> None:
    routes = {
        "/repos/acme/app/issues/3": httpx.Response(
            200, json={"title": "Old", "state": "closed", "state_reason": "not_planned"}
        )
    }
    preview = await _client(_preview_handler(routes, [])).fetch_link_preview(
        "ghu_a", "acme/app", "issue", "3"
    )

    assert preview is not None
    assert preview["state"] == "not_planned"


@pytest.mark.asyncio
async def test_commit_and_run_previews_carry_their_title_and_state() -> None:
    routes = {
        "/repos/acme/app/commits/abc1234": httpx.Response(
            200, json={"sha": "abc1234full", "commit": {"message": "feat: x\n\nbody"}}
        ),
        "/repos/acme/app/commits/abc1234full/check-runs": _CHECK_RUNS,
        "/repos/acme/app/actions/runs/99": httpx.Response(
            200, json={"display_title": "CI", "status": "completed", "conclusion": "failure"}
        ),
    }
    client = _client(_preview_handler(routes, []))

    commit = await client.fetch_link_preview("ghu_a", "acme/app", "commit", "abc1234")
    run = await client.fetch_link_preview("ghu_a", "acme/app", "run", "99")

    assert commit is not None and run is not None
    assert (commit["title"], commit["checks"]) == (
        "feat: x",
        {"passed": 2, "failed": 1, "pending": 1},
    )
    assert (run["title"], run["state"]) == ("CI", "failure")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404])
async def test_preview_of_an_object_the_token_cannot_read_is_none(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    assert await _client(handler).fetch_link_preview("ghu_a", "acme/app", "pull", "1") is None


@pytest.mark.asyncio
async def test_preview_server_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(GitHubAppError):
        await _client(handler).fetch_link_preview("ghu_a", "acme/app", "run", "1")


@pytest.mark.asyncio
async def test_commit_status_contexts_count_toward_the_check_rollup() -> None:
    routes = {
        "/repos/acme/app/pulls/5": httpx.Response(
            200, json={"title": "T", "state": "open", "head": {"sha": "s5"}}
        ),
        "/repos/acme/app/commits/s5/check-runs": httpx.Response(
            200, json={"check_runs": [{"status": "completed", "conclusion": "success"}]}
        ),
        "/repos/acme/app/commits/s5/status": httpx.Response(
            200,
            json={"statuses": [{"state": "failure"}, {"state": "pending"}, {"state": "error"}]},
        ),
    }
    preview = await _client(_preview_handler(routes, [])).fetch_link_preview(
        "ghu_a", "acme/app", "pull", "5"
    )

    assert preview is not None
    assert preview["checks"] == {"passed": 1, "failed": 2, "pending": 1}
