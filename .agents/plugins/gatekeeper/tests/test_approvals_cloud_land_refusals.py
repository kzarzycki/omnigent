"""approvals.py: `land` from a cloud session on a base with no merge queue, behind the proxy's own refusals (#1917).

The proxy refuses `gh pr merge` (GraphQL) in words that name no `HTTP 403`, and refuses `POST statuses/<sha>` with
one, while it passes the REST merge (`PUT pulls/N/merge`). The land reads the first as GraphQL's refusal and merges
over REST, and goes on past the second, as each was seen from a cloud session.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from approvals_harness import approvals, lands  # noqa: F401
from test_approvals import cloud_land, rest_land  # noqa: F401

# what `gh pr merge` prints where a cloud session's proxy refuses it, as `gh` 2.89 printed it there (cut short)
PR_MERGE_REFUSED = (
    'non-200 OK status code: 403 Forbidden body: "{\\"message\\":\\"GitHub GraphQL is not available from Claude'
    ' Code sessions; use the REST API (gh api repos/{owner}/{repo}/...).\\"}"'
)
# what the same proxy prints for a commit-status write
STATUS_REFUSED = (
    "gh: Write access to this GitHub API path is not permitted through this proxy. (HTTP 403)"
)


def test_the_proxys_pr_merge_refusal_lands_over_rest(rest_land: dict[str, Any]) -> None:
    """`gh pr merge` refused in the proxy's words: the land merges over REST at the gated head, rather than raising
    the refusal as the land's failure."""
    rest_land["refusal"] = PR_MERGE_REFUSED
    code, lines = rest_land["land"]("7")
    assert code == 0, lines
    [put] = rest_land["puts"]
    assert put[3] == "repos/{owner}/{repo}/pulls/7/merge" and f"sha={rest_land['head']}" in put
    assert rest_land["main"]() != rest_land["base"]


def test_a_refused_land_status_still_lands_and_says_so(
    rest_land: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proxy refuses the `land` status written just before the merge: the land prints that it was not written
    and merges over REST all the same."""
    rest_land["refusal"] = PR_MERGE_REFUSED

    def refused(*_args: Any, **_kw: Any) -> None:
        raise subprocess.CalledProcessError(1, "gh", stderr=STATUS_REFUSED)

    monkeypatch.setattr(approvals, "post_status", refused)
    code, lines = rest_land["land"]("7")
    assert code == 0, lines
    assert any("the `land` status on" in line and "was not written" in line for line in lines), (
        lines
    )
    assert rest_land["puts"] and rest_land["main"]() != rest_land["base"]


def test_a_refused_land_status_and_a_failed_merge_withdraw_nothing(
    rest_land: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no `land` success written there is none to withdraw: a merge GitHub refuses posts no error status."""
    rest_land["refusal"] = PR_MERGE_REFUSED
    tried: list[tuple[Any, ...]] = []

    def refused(*args: Any, **_kw: Any) -> None:
        tried.append(args)
        raise subprocess.CalledProcessError(1, "gh", stderr=STATUS_REFUSED)

    monkeypatch.setattr(approvals, "post_status", refused)
    inner = approvals.github.gh

    def gh(*args: str, data: str | None = None) -> str:
        if args[:3] == ("api", "-X", "PUT") and args[3].endswith("/merge"):
            raise subprocess.CalledProcessError(
                1, "gh", stderr="Pull Request is not mergeable (HTTP 405)"
            )
        return inner(*args, data=data)

    monkeypatch.setattr(approvals.github, "gh", gh)
    _code, lines = rest_land["land"]("7")
    assert any("the merge was refused" in line for line in lines), lines
    assert len(tried) == 1 and rest_land["main"]() == rest_land["base"]


@pytest.mark.parametrize(
    "stderr",
    [PR_MERGE_REFUSED, "gh: GitHub GraphQL is not available from Claude Code sessions (HTTP 403)"],
)
def test_the_proxys_graphql_refusals_read_as_refused(stderr: str) -> None:
    assert approvals.github.refused(stderr)


def test_a_500_is_no_refusal() -> None:
    assert not approvals.github.refused(
        'non-200 OK status code: 500 Internal Server Error body: ""'
    )
