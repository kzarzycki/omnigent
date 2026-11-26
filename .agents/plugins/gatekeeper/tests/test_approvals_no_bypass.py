"""The pack never merges past the base's rules (#1722 section 3): gatekeeper `land`, the adapter's `gh pr merge` wrapper and its
REST merge path refuse the bypass flag and any REST field that asks to bypass, and refuse any merge while a required check
is red or missing on the head, whether or not the owner approved a break-glass. A break-glass PR is merged by the owner
in the GitHub UI, never by a helper."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import HEAD, approvals, pull

GITHUB = approvals.github
RED_CHECKS = GITHUB.red_checks  # the harness replaces it with one that finds every check green
ROOT = Path(__file__).resolve().parents[2]
# built from parts, as the adapter builds it, so this file is no more a script that holds the flag than the pack's
FLAG = "-" * 2 + "ad" + "min"
SCRIPT_SUFFIXES = {".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".ts", ".rb"}
SKIPPED_DIRS = {".git", "apm_modules", "node_modules", "__pycache__", "tests", ".venv"}


def test_no_script_of_the_pack_holds_the_bypass_flag() -> None:
    """A grep over every script of the repo (tests apart, which build the flag from parts): a skill's prose may tell an
    agent never to use it, but no code passes it."""
    scripts = [
        path
        for path in ROOT.rglob("*")
        if path.suffix in SCRIPT_SUFFIXES
        and path.is_file()
        and not SKIPPED_DIRS & set(path.relative_to(ROOT).parts)
    ]
    assert len(scripts) > 50  # the scan reads the pack's scripts, not an empty tree
    assert [
        str(path.relative_to(ROOT)) for path in scripts if FLAG in path.read_text(errors="ignore")
    ] == []


@pytest.fixture
def ran(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """gh's own process, replaced: each command that reaches it."""
    calls: list[tuple[str, ...]] = []

    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(GITHUB.subprocess, "run", run)
    return calls


def test_gh_pr_merge_with_the_bypass_flag_is_refused_before_gh_runs(
    ran: list[tuple[str, ...]],
) -> None:
    for words in (("pr", "merge", "7", "--squash", FLAG), ("pr", "merge", "7", f"{FLAG}=true")):
        with pytest.raises(approvals.Refused, match="the owner's, in the GitHub UI"):
            GITHUB.gh(*words)
    assert ran == []


@pytest.mark.parametrize("field", ["bypass=true", "bypass_branch_protection=true", "admin=true"])
def test_a_rest_merge_asking_to_bypass_is_refused_before_gh_runs(
    ran: list[tuple[str, ...]], field: str
) -> None:
    route = "repos/{owner}/{repo}/pulls/7/merge"
    with pytest.raises(approvals.Refused, match="sends merge_method, sha, commit_title"):
        GITHUB.gh("api", "-X", "PUT", route, "-f", "merge_method=squash", "-F", field)
    with pytest.raises(approvals.Refused, match="--input"):
        GITHUB.gh("api", "-X", "PUT", route, "--input", "body.json")
    assert ran == []


ROUTE = "repos/{owner}/{repo}/pulls/7/merge"
# every spelling gh api accepts for sending one field, each carrying the field `bypass`
SPELLINGS = {
    "-f spaced": ("-f", "bypass=true"),
    "-F spaced": ("-F", "bypass=true"),
    "-f attached": ("-fbypass=true",),
    "-F attached": ("-Fbypass=true",),
    "-f attached with =": ("-f=bypass=true",),
    "--field spaced": ("--field", "bypass=true"),
    "--field=": ("--field=bypass=true",),
    "--raw-field spaced": ("--raw-field", "bypass=true"),
    "--raw-field=": ("--raw-field=bypass=true",),
    "a short cluster": ("-if", "bypass=true"),
    "a nested key": ("-f", "bypass[x]=true"),
}


@pytest.mark.parametrize("spelling", SPELLINGS)
@pytest.mark.parametrize("before", [True, False])
def test_a_rest_merge_asking_to_bypass_is_refused_in_every_spelling_of_a_field_flag(
    ran: list[tuple[str, ...]], spelling: str, before: bool
) -> None:
    flags = SPELLINGS[spelling]
    words = (*flags, ROUTE) if before else (ROUTE, *flags)
    with pytest.raises(approvals.Refused, match="sends merge_method, sha, commit_title"):
        GITHUB.gh("api", "-X", "PUT", *words)
    assert ran == []


@pytest.mark.parametrize(
    "flags", [("--input", "body.json"), ("--input=body.json",), ("--input", "-")]
)
def test_a_rest_merge_reading_its_body_from_input_is_refused_in_either_spelling(
    ran: list[tuple[str, ...]], flags: tuple[str, ...]
) -> None:
    with pytest.raises(approvals.Refused, match="--input"):
        GITHUB.gh("api", "-X", "PUT", ROUTE, *flags)
    assert ran == []


@pytest.mark.parametrize("route", [ROUTE + "/", ROUTE + "//", ROUTE + "/?x=1", ROUTE + "?x=1"])
def test_the_merge_route_is_refused_with_a_trailing_slash_or_a_query(
    ran: list[tuple[str, ...]], route: str
) -> None:
    with pytest.raises(approvals.Refused, match="sends merge_method"):
        GITHUB.gh("api", "-X", "PUT", route, "-f", "x=1")
    assert ran == []


def test_a_graphql_merge_pull_request_is_refused_before_gh_runs(ran: list[tuple[str, ...]]) -> None:
    query = 'mutation { mergePullRequest(input: {pullRequestId: "X"}) { clientMutationId } }'
    for words in (("-f", f"query={query}"), (f"-fquery={query}",), ("--field", f"query={query}")):
        with pytest.raises(approvals.Refused, match="GraphQL mergePullRequest"):
            GITHUB.gh("api", "graphql", *words)
    assert ran == []


def test_a_rest_merge_in_each_spelling_of_its_own_fields_goes_through(
    ran: list[tuple[str, ...]],
) -> None:
    GITHUB.gh("api", "-X", "PUT", ROUTE + "/", "-fmerge_method=squash", "--field=sha=abc")
    GITHUB.gh("api", "-XPUT", ROUTE, "--raw-field", "commit_title=t", "-H", "Accept: x")
    GITHUB.gh("api", "graphql", "-f", "query={ viewer { login } }")
    assert len(ran) == 3


def test_the_adapters_own_checks_are_the_gatekeepers() -> None:
    assert GITHUB.OWN_CHECKS == frozenset(approvals.OWN)


def test_a_rest_merge_with_its_own_fields_goes_through(ran: list[tuple[str, ...]]) -> None:
    GITHUB.gh(
        "api",
        "-X",
        "PUT",
        "repos/{owner}/{repo}/pulls/7/merge",
        "-f",
        "merge_method=squash",
        "-f",
        f"sha={HEAD}",
        "-f",
        "commit_message=body",
    )
    assert len(ran) == 1


@pytest.fixture
def forge(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The adapter reading a base that requires `qualify`: its conclusion on the head (`runs`), the statuses there, and
    each gh command the merge then makes (`ran`)."""
    state: dict[str, Any] = {"runs": {}, "statuses": [], "ran": []}
    monkeypatch.setattr(GITHUB, "red_checks", RED_CHECKS)
    monkeypatch.setattr(GITHUB, "pull_status", lambda _n: {"baseRefName": "main"})
    monkeypatch.setattr(GITHUB, "required_checks", lambda _base: {"qualify", "land"})
    monkeypatch.setattr(GITHUB, "commit_checks", lambda _ref: state["runs"])
    monkeypatch.setattr(GITHUB, "statuses", lambda _ref: state["statuses"])
    monkeypatch.setattr(GITHUB, "_rest_only", False)

    def gh(*args: str, data: str | None = None) -> str:
        state["ran"].append(args)
        if args[0] == "pr" and state.get("graphql_refused"):
            raise subprocess.CalledProcessError(1, "gh", stderr="gh: Forbidden (HTTP 403)")
        return ""

    monkeypatch.setattr(GITHUB, "gh", gh)
    return state


def conclusion(value: str) -> dict[str, dict[str, str]]:
    return {"qualify": {"conclusion": value, "url": "", "id": 1, "title": ""}}


@pytest.mark.parametrize("graphql_refused", [False, True], ids=["gh pr merge", "REST merge"])
def test_the_merge_wrapper_refuses_a_red_or_missing_required_check_on_either_path(
    forge: dict[str, Any], graphql_refused: bool
) -> None:
    forge["graphql_refused"] = graphql_refused
    for runs in (conclusion("FAILURE"), conclusion(""), {}):
        forge["runs"] = runs
        with pytest.raises(
            approvals.Refused, match=r"refusing to merge while a required check is not green"
        ):
            GITHUB.merge(7, "squash", HEAD)
    assert forge["ran"] == []  # neither gh pr merge nor the REST call was made


def test_the_merge_wrapper_refuses_a_required_status_that_is_not_success(
    forge: dict[str, Any],
) -> None:
    forge["runs"] = {}
    forge["statuses"] = [{"context": "qualify", "state": "failure"}]
    with pytest.raises(approvals.Refused, match="`qualify` is FAILURE"):
        GITHUB.merge(7, "squash", HEAD)
    forge["statuses"] = [{"context": "qualify", "state": "success"}]  # a green status is the check
    GITHUB.merge(7, "squash", HEAD)
    assert forge["ran"][-1][:3] == ("pr", "merge", "7")


@pytest.mark.parametrize("graphql_refused", [False, True], ids=["gh pr merge", "REST merge"])
def test_a_green_head_merges_by_the_path_the_proxy_allows(
    forge: dict[str, Any], graphql_refused: bool
) -> None:
    forge["graphql_refused"] = graphql_refused
    forge["runs"] = conclusion("SUCCESS")
    GITHUB.merge(7, "squash", HEAD)
    assert forge["ran"][-1][:2] == (("api", "-X") if graphql_refused else ("pr", "merge"))
    assert not any(FLAG in arg for call in forge["ran"] for arg in call)


def test_land_merges_nothing_while_a_required_check_is_red_even_with_the_owners_record(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """land's own merge step, the signed squash and GitHub's alike, stops at a head whose required check is red."""
    sent: list[Any] = []
    monkeypatch.setattr(approvals, "required_checks", lambda _base: {"qualify"})
    monkeypatch.setattr(approvals.port("forge"), "merge", lambda *a: sent.append(a))
    monkeypatch.setattr(approvals, "signed", lambda _loop: None)
    red = pull(checks=(("qualify", "FAILURE"),))
    failed: set[int] = set()
    approvals.merges([(red, "t" * 40)], "main", "b" * 40, None, failed)
    assert sent == []
    out = capsys.readouterr().out
    assert "refusing to merge while a required check is not green" in out
    assert "`qualify` is FAILURE on the head commit" in out


def test_land_does_not_merge_a_head_whose_required_check_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sent: list[Any] = []
    monkeypatch.setattr(approvals, "required_checks", lambda _base: {"qualify"})
    monkeypatch.setattr(approvals.port("forge"), "merge", lambda *a: sent.append(a))
    monkeypatch.setattr(approvals, "signed", lambda _loop: None)
    approvals.merges([(pull(checks=()), "t" * 40)], "main", "b" * 40, None, set())
    assert sent == []
    assert "no `qualify` on the head commit yet" in capsys.readouterr().out
