"""omnigent_agent.py: the supervisor's ``landed`` event counts only merges a followed, non-direct
lead made: a PR the login it runs as opened (#1129)."""

import json
from pathlib import Path

import pytest
import test_omnigent_agent as base  # the faked server, the supervisor loop
from test_omnigent_agent import no_model, no_sleep  # noqa: F401  (the fixtures apply here by name)

omnigent_agent = base.omnigent_agent
OLD, NEW = base.OLD, base.NEW


def github(
    monkeypatch: pytest.MonkeyPatch,
    head: list[str],
    authors: dict[str, str],
    listing: str | None = None,
    me: str = "me",
) -> list[tuple[str, ...]]:
    """``gh`` faked: the comparison lists one merge commit per PR of ``authors`` (number to login),
    or one direct push when there are none; ``gh pr list --author @me`` answers as GitHub does,
    with only the PRs ``me`` opened, unless ``listing`` is a Fail message, which makes that read
    fail instead. Each call is recorded."""
    monkeypatch.setattr(omnigent_agent, "HEAD_SECONDS", 0)
    calls: list[tuple[str, ...]] = []
    shas = {n: f"{int(n):040x}" for n in authors} or {"direct": "d" * 40}

    def gh(*args: str, timeout: float = 0) -> str:
        calls.append(args)
        if "repos/o/r/commits/HEAD" in args:
            return head[0] + "\n"
        if args[1] == f"repos/o/r/compare/{OLD}...{NEW}":
            return "".join(f"{sha}\t2026-11-01T10:00:00Z\n" for sha in shas.values())
        if args[:2] == ("pr", "list"):
            assert args[args.index("--author") + 1] == "@me"
            if listing is not None:
                raise omnigent_agent.Fail(listing)
            return json.dumps(
                [
                    {"number": int(n), "mergeCommit": {"oid": shas[n]}}
                    for n, login in authors.items()
                    if login == me
                ]
            )
        raise AssertionError(args)

    monkeypatch.setattr(omnigent_agent, "gh", gh)
    return calls


def waiting(*prs: str) -> base.Lead:
    """A lead that ended its turn waiting on ``prs``: a landing reaches only a lead whose wait
    names a merged PR."""
    wait = ", ".join(prs)
    og = base.Lead(base.told("brief", 60), base.ending(f"STILL WORKING: x\nWaits: {wait}", 5))
    og.snap = {"status": "idle"}
    return og


def moved(og: base.Fleet, registry: Path, monkeypatch: pytest.MonkeyPatch, head: list[str]) -> None:
    base.in_checkout(og)
    monkeypatch.setattr(omnigent_agent, "pr_settled", lambda pr, repo="": None)
    base.supervised(
        og, registry, monkeypatch, 2, lambda poll: head.__setitem__(0, NEW), console=True
    )


def test_a_bot_s_merge_alone_raises_nothing_and_stores_the_new_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, head = base.following(tmp_path), [OLD]
    github(monkeypatch, head, {"843": "release-bot[bot]"})
    moved(base.Lead(base.told("brief", 60)), registry, monkeypatch, head)
    assert base.landed(registry) == []
    assert base.state_of(registry)["heads"] == {"o/r": NEW}


def test_a_lead_s_pr_beside_a_bot_s_and_a_teammate_s_is_the_only_one_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, head = base.following(tmp_path), [OLD]
    github(monkeypatch, head, {"843": "release-bot[bot]", "844": "me", "845": "teammate"})
    moved(waiting("#844"), registry, monkeypatch, head)
    (event,) = base.landed(registry)
    assert event["line"].endswith(f"merged since {OLD[:12]}: #844")


def test_a_repo_only_direct_leads_work_in_is_not_watched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, head = base.direct_lead(tmp_path), [OLD]
    calls = github(monkeypatch, head, {"844": "me"})
    moved(base.Lead(base.told("brief", 60)), registry, monkeypatch, head)
    assert calls == [] and base.landed(registry) == []


def test_an_author_not_read_still_raises_the_landing_naming_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, head = base.following(tmp_path), [OLD]
    github(
        monkeypatch, head, {"843": "release-bot[bot]", "846": "me"}, listing="gh pr list: HTTP 502"
    )
    moved(waiting("#846"), registry, monkeypatch, head)
    (event,) = base.landed(registry)
    assert event["line"].endswith("merged since " + OLD[:12] + ": not read: gh pr list: HTTP 502")
    assert "#843" not in event["line"]


def test_a_direct_push_naming_no_pr_stores_the_new_head_and_raises_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, head = base.following(tmp_path), [OLD]
    github(monkeypatch, head, {})
    moved(base.Lead(base.told("brief", 60)), registry, monkeypatch, head)
    assert base.landed(registry) == []
    assert base.state_of(registry)["heads"] == {"o/r": NEW}


def test_the_login_not_read_still_raises_the_landing_naming_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, head = base.following(tmp_path), [OLD]
    github(monkeypatch, head, {"843": "release-bot[bot]", "844": "me"}, listing="gh api: HTTP 401")
    moved(waiting("#843", "#844"), registry, monkeypatch, head)
    (event,) = base.landed(registry)
    assert event["line"].endswith("not read: gh api: HTTP 401")


def test_a_set_that_shares_the_supervisor_with_a_pr_still_hears_that_pr_land(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, head = base.following(tmp_path), [OLD]
    github(monkeypatch, head, {"844": "me"})
    moved(waiting("supervisor", "#844"), registry, monkeypatch, head)
    (event,) = base.landed(registry)
    assert event["action"] == "act"  # it waits on the supervisor, who has the step to take


def test_a_pr_of_another_repo_that_a_wait_names_is_not_this_repos_landing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, head = base.following(tmp_path), [OLD]
    github(monkeypatch, head, {"844": "me"})
    moved(waiting("other/repo#844"), registry, monkeypatch, head)
    assert base.landed(registry) == []
