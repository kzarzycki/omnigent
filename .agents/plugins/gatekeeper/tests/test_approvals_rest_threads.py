"""GitHub's own REST has no review-thread route: where the cloud proxy's route answers 404, the threads come from
GraphQL asked for them alone, and a failure of both raises (#1259)."""

from __future__ import annotations

import json
import subprocess

import pytest
from approvals_harness import (
    approvals,
)

ROUTE = "repos/{owner}/{repo}/pulls/7/ccr/review_threads"
THREADS = {
    "totalCount": 2,
    "nodes": [
        {
            "isResolved": True,
            "comments": {
                "nodes": [{"body": "done", "databaseId": 1, "url": "u1", "viewerCanUpdate": False}]
            },
        },
        {
            "isResolved": False,
            "comments": {
                "nodes": [{"body": "open", "databaseId": 2, "url": "u2", "viewerCanUpdate": False}]
            },
        },
    ],
}


def github(route_error: str, graphql_error: str | None = None):
    """`gh` for a run in REST-only mode: the thread route fails with `route_error`; GraphQL answers the thread-only
    query with THREADS, or fails with `graphql_error`."""
    calls: list[tuple[str, ...]] = []

    def gh(*args: str, data: str | None = None) -> str:
        calls.append(args)
        if "graphql" in args:
            assert "reviewThreads" in " ".join(args) and "closingIssuesReferences" not in " ".join(
                args
            )
            assert "n=7" in args
            if graphql_error:
                raise subprocess.CalledProcessError(1, "gh", stderr=graphql_error)
            return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": THREADS}}}})
        assert args == ("api", ROUTE), args
        raise subprocess.CalledProcessError(1, "gh", stderr=route_error)

    return gh, calls


def test_where_github_answers_404_on_the_thread_route_the_threads_come_from_graphql_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job token in a private organization repository: the run reads REST, GitHub has no thread route, and the
    threads, which that token may read, come from GraphQL, a resolved and an unresolved one as PULL reads them."""
    gh, calls = github(f"{ROUTE}: Not Found (HTTP 404)")
    monkeypatch.setattr(approvals.github, "_rest_only", True)
    monkeypatch.setattr(approvals.github, "gh", gh)
    assert approvals.github.rest_threads(7) == THREADS
    assert [call[1] for call in calls] == [ROUTE, "graphql"]


@pytest.mark.parametrize(
    ("route_error", "graphql_error", "raised"),
    [
        (
            "Not Found (HTTP 404)",
            "gh: Resource not accessible by integration",
            "Resource not accessible",
        ),
        ("gh: Server Error (HTTP 502)", None, "HTTP 502"),
    ],
)
def test_threads_neither_route_reads_raise(
    monkeypatch: pytest.MonkeyPatch, route_error: str, graphql_error: str | None, raised: str
) -> None:
    """Unresolved threads are never read as none: a 404 plus a GraphQL failure raises, and so does any route error
    other than 404, without asking GraphQL."""
    gh, calls = github(route_error, graphql_error)
    monkeypatch.setattr(approvals.github, "_rest_only", True)
    monkeypatch.setattr(approvals.github, "gh", gh)
    with pytest.raises(subprocess.CalledProcessError) as error:
        approvals.github.rest_threads(7)
    assert raised in error.value.stderr
    assert len(calls) == (2 if graphql_error else 1)


def test_rest_only_mode_is_forced_by_an_environment_variable(monkeypatch):
    """GATEKEEPER_REST_ONLY=1 starts a run in REST-only mode, so a check can walk the path a refused token takes."""
    import importlib

    import github_adapter

    monkeypatch.setenv("GATEKEEPER_REST_ONLY", "1")
    assert importlib.reload(github_adapter)._rest_only is True
    monkeypatch.delenv("GATEKEEPER_REST_ONLY")
    assert importlib.reload(github_adapter)._rest_only is False
