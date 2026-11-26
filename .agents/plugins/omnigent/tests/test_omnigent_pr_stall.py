"""An open PR that hasn't moved toward landing for an hour wakes the manager with its blocker (#1819).

The seam: ``gh`` faked to serve each PR's GitHub timeline, statuses, check runs, its base's required checks and the
repo's loop events, so ``pr_stalls`` and the supervisor read them as they would GitHub's."""

import json
import re
import time
from datetime import UTC, datetime

import pytest
from test_omnigent_agent import (
    Lead,
    ending,
    following,
    in_checkout,
    omnigent_agent,
    supervised,
    told,
)

HOUR = 3600.0


def iso(ago: float) -> str:
    return datetime.fromtimestamp(time.time() - ago, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha(n: int) -> str:
    return f"{n:040x}"


def report(head: str, word: str) -> str:
    return f"## Verifier report\n\nHead: {head}\nVERDICT: 0 blocker\nSATISFIED: {word}\nRisk: low"


def pr(n: int, *, pushed: float = 2 * HOUR, **extra: object) -> dict:
    """PR ``n``, pushed ``pushed`` seconds ago, with its ``timeline``, ``statuses``, ``checks``, ``mergeable`` and
    ``labels`` overridable."""
    return {
        "number": n,
        "title": f"change {n}",
        "html_url": f"https://github.com/o/r/pull/{n}",
        "head": {"sha": sha(n), "ref": f"claude/ready-{n}"},
        "base": {"ref": "main"},
        "created_at": iso(pushed + 60),
        "updated_at": iso(pushed),
        "draft": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "labels": [],
        "timeline": [{"event": "committed", "sha": sha(n), "committer": {"date": iso(pushed)}}],
        "statuses": [],
        "checks": [],
        **extra,
    }


def github(
    monkeypatch: pytest.MonkeyPatch, prs: list[dict], cards: dict[str, str] | None = None
) -> list:
    """``gh`` faked for repo o/r: its open ``prs``, ``main``'s required check ``qualify``, and a loop event per
    branch in ``cards`` at the state given. Each call is recorded."""
    calls: list[tuple[str, ...]] = []
    by = {p["number"]: p for p in prs}
    refs = {
        f"refs/loop/verify/{b}": (sha(900 + i), st)
        for i, (b, st) in enumerate((cards or {}).items())
    }

    def lines(items: list) -> str:
        return json.dumps(items)

    def gh(*args: str, timeout: float = 0) -> str:
        """Answers as ``gh api -i`` does: one page of a list as a JSON array (an empty page ends a list), a single read
        as its body, each with a status line and an ETag."""
        calls.append(args)
        path = next(a for a in args if a.startswith("repos/"))
        paged = re.search(r"[?&]page=(\d+)", path)
        path = re.sub(r"[?&]page=\d+", "", path).replace("?&", "?").rstrip("?")
        out = serve(path)
        if paged and int(paged[1]) > 1:
            out = "[]" if out.startswith("[") else '{"check_runs": []}'
        return f'HTTP/2.0 200 OK\nEtag: "{hash(out)}"\n\n{out}'

    def serve(path: str) -> str:
        path = path.replace("&per_page=100", "").replace("?per_page=100", "")
        if path.startswith("repos/o/r/pulls?"):
            return lines(
                [
                    {k: v for k, v in p.items() if k not in ("timeline", "statuses", "checks")}
                    for p in prs
                ]
            )
        if path == "repos/o/r/rules/branches/main":
            return lines(
                [
                    {
                        "type": "required_status_checks",
                        "parameters": {"required_status_checks": [{"context": "qualify"}]},
                    }
                ]
            )
        if path == "repos/o/r/git/matching-refs/loop/":
            return lines([{"ref": r, "object": {"sha": s}} for r, (s, _) in refs.items()])
        for r, (s, st) in refs.items():
            if path == f"repos/o/r/git/commits/{s}":
                branch = r.removeprefix("refs/loop/verify/")
                return json.dumps({"message": f"loop event verify {branch}: {st} [skip ci]"})
        for n, p in by.items():
            if path == f"repos/o/r/pulls/{n}":
                return json.dumps(
                    {k: v for k, v in p.items() if k not in ("timeline", "statuses", "checks")}
                )
            if path.startswith(f"repos/o/r/issues/{n}/timeline"):
                return lines(p["timeline"])
            if path.startswith(f"repos/o/r/commits/{sha(n)}/statuses"):
                return lines(p["statuses"])
            if path.startswith(f"repos/o/r/commits/{sha(n)}/check-runs"):
                if "filter=all" in path:
                    return json.dumps({"check_runs": p["checks"]})
                newest: dict = {}  # GitHub's default, filter=latest: each check's newest run alone
                for c in p["checks"]:
                    if (c.get("started_at") or "") >= (
                        newest.get(c["name"], {}).get("started_at") or ""
                    ):
                        newest[c["name"]] = c
                return json.dumps({"check_runs": list(newest.values())})
        raise AssertionError(f"unexpected read: {path}")

    monkeypatch.setattr(omnigent_agent, "gh", gh)
    return calls


def verdict(n: int, word: str, ago: float) -> dict:
    return {
        "event": "commented",
        "body": report(sha(n), word),
        "created_at": iso(ago),
        "html_url": f"https://github.com/o/r/pull/{n}#v",
    }


def stalls(monkeypatch, prs, cards=None, memo=None) -> dict[int, str]:
    github(monkeypatch, prs, cards)
    found = omnigent_agent.pr_stalls("o/r", HOUR, {} if memo is None else memo)
    return {int(eid.split("#")[1].split(":")[0]): line for eid, line in found}


@pytest.mark.parametrize(
    "extra, blocker",
    [
        (  # #565's case: a red required check its diff didn't cause
            {
                "checks": [
                    {"name": "qualify", "conclusion": "failure", "completed_at": iso(3 * HOUR)}
                ]
            },
            "a red required check: qualify",
        ),
        ({"mergeable": False, "mergeable_state": "dirty"}, "a conflict with main"),
        (  # a satisfied verdict over an hour old with no go: finding 7 of the waste audit
            {"timeline_extra": [verdict(12, "yes", 90 * 60)]},
            "a verdict without a go (SATISFIED: yes, ",  # its age is read when the test runs, after collection
        ),
        (
            {"timeline_extra": [verdict(12, "no", 90 * 60)]},
            "a verdict asking a fix round (SATISFIED: no) with no push since",
        ),
        ({"card": "needs-owner"}, "a card waiting: a `verify` loop event at needs-owner"),
        (
            {
                "timeline_extra": [
                    {
                        "event": "commented",
                        "body": "## Verifier question\n\nWhich base?",
                        "created_at": iso(90 * 60),
                        "html_url": "https://github.com/o/r/pull/12#q",
                    }
                ]
            },
            "a card waiting: a verifier question: https://github.com/o/r/pull/12#q",
        ),
    ],
)
def test_a_pr_stalled_an_hour_names_its_blocker(monkeypatch, extra, blocker) -> None:
    extra = dict(extra)
    card = extra.pop("card", None)
    more = extra.pop("timeline_extra", [])
    p = pr(12, **extra)
    p["timeline"] += more
    found = stalls(monkeypatch, [p], {"claude/ready-12": card} if card else None)
    assert list(found) == [12]
    assert (
        blocker in found[12]
        and "o/r: PR #12 change 12: no push, verdict, go or queue step for" in found[12]
    )


def test_a_red_check_not_required_blocks_nothing_and_a_stall_with_no_blocker_still_wakes(
    monkeypatch,
) -> None:
    p = pr(12, checks=[{"name": "nightly", "conclusion": "failure"}])
    found = stalls(monkeypatch, [p])
    assert "blocker: none found on GitHub" in found[12]


@pytest.mark.parametrize(
    "moved",
    [
        {"pushed": 20 * 60},  # a push
        {"timeline_extra": [{"event": "head_ref_force_pushed", "created_at": iso(600)}]},
        {"timeline_extra": [verdict(12, "yes", 600)]},  # a verdict
        {  # a go, on a head whose CI started two hours ago: the go, not the status's first sight, moves it
            "statuses": [{"context": "owner", "state": "success", "created_at": iso(600)}],
            "checks": [{"name": "qualify", "started_at": iso(2 * HOUR), "conclusion": "success"}],
        },
        {
            "timeline_extra": [
                {"event": "labeled", "label": {"name": "loop:queued"}, "created_at": iso(600)}
            ]
        },
        {
            "checks": [{"name": "Queue: Embarked in merge queue", "started_at": iso(600)}]
        },  # queue progress
        {"draft": True},  # its lead is still building it
    ],
)
def test_a_pr_that_moved_within_the_hour_or_is_a_draft_is_no_stall(monkeypatch, moved) -> None:
    moved = dict(moved)
    more = moved.pop("timeline_extra", [])
    p = pr(12, **moved)
    p["timeline"] += more
    assert stalls(monkeypatch, [p]) == {}


def test_a_satisfied_verdict_with_its_go_or_queued_is_no_verdict_blocker(monkeypatch) -> None:
    gone = pr(12, statuses=[{"context": "owner", "state": "success", "created_at": iso(2 * HOUR)}])
    queued = pr(13, labels=[{"name": "loop:queued"}])
    for p in (gone, queued):
        p["timeline"].append(verdict(p["number"], "yes", 2 * HOUR))
    found = stalls(monkeypatch, [gone, queued])
    assert (
        all("blocker: none found on GitHub" in line for line in found.values()) and len(found) == 2
    )


def test_a_stall_already_reported_is_not_woken_twice_and_a_new_move_starts_a_new_one(
    monkeypatch,
) -> None:
    memo: dict = {}
    p = pr(12, mergeable=False)
    assert list(stalls(monkeypatch, [p], memo=memo)) == [12]
    p["updated_at"] = iso(
        0
    )  # a comment: the listing changed, so it is read again, but nothing moved
    p["timeline"].append({"event": "commented", "body": "any news?", "created_at": iso(0)})
    assert stalls(monkeypatch, [p], memo=memo) == {}
    # a push two hours ago that the first read missed is a new newest move: a new stall
    p["timeline"].append({"event": "committed", "committer": {"date": iso(90 * 60)}})
    p["updated_at"] = iso(-1)
    assert list(stalls(monkeypatch, [p], memo=memo)) == [12]


def test_a_pr_that_moved_costs_no_read_until_its_threshold(monkeypatch) -> None:
    memo: dict = {}
    stalls(monkeypatch, [pr(12, pushed=600)], memo=memo)
    calls = github(monkeypatch, [pr(12, pushed=600)])
    assert omnigent_agent.pr_stalls("o/r", HOUR, memo) == []
    assert [c for c in calls if any("pulls?" in a for a in c)] == calls  # the listing alone


def test_the_supervisor_wakes_the_manager_once_per_stall(tmp_path, monkeypatch) -> None:
    """Through the supervisor: a PR stalled in a followed lead's repo reaches the notified session (the manager) as
    an act item, once, however many sweeps find it."""
    registry = following(tmp_path)
    og = Lead(told("brief", 9000), ending("STILL WORKING: waiting on CI\nWaits: CI", 8000))
    in_checkout(og)
    github(monkeypatch, [pr(12, mergeable=False)])
    monkeypatch.setattr(omnigent_agent, "PR_STALL_POLL", 0.0)
    monkeypatch.setattr(omnigent_agent, "HEAD_SECONDS", 1e9)
    supervised(og, registry, monkeypatch, 4, notify="cos", console=True)
    woken = [t for s, t in og.inbox if s == "cos" and "PR #12" in t]
    assert len(woken) == 1 and "[act]" in woken[0] and "a conflict with main" in woken[0]


def test_stall_minutes_zero_turns_the_watch_off(tmp_path, monkeypatch) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 9000), ending("STILL WORKING: waiting on CI\nWaits: CI", 8000))
    in_checkout(og)
    calls = github(monkeypatch, [pr(12, mergeable=False)])
    monkeypatch.setattr(omnigent_agent, "PR_STALL_POLL", 0.0)
    monkeypatch.setattr(omnigent_agent, "HEAD_SECONDS", 1e9)
    supervised(og, registry, monkeypatch, 3, notify="cos", console=True, stall_after=0)
    assert not [c for c in calls if any("pulls" in a for a in c)]


def test_an_old_commit_pushed_recently_is_dated_by_its_first_check_not_its_commit(
    monkeypatch,
) -> None:
    """A worker commits, waits on its gate, then pushes: the commit's date is an hour old, the push is not."""
    p = pr(12, pushed=90 * 60, checks=[{"name": "qualify", "started_at": iso(110)}])
    assert stalls(monkeypatch, [p]) == {}


def test_a_head_with_no_ci_is_dated_by_the_sweep_that_first_saw_it(monkeypatch) -> None:
    """A head new since the last sweep, an old commit with no check run, was pushed after that sweep: no stall."""
    memo: dict = {}
    assert list(stalls(monkeypatch, [pr(12, pushed=3 * HOUR)], memo=memo)) == [12]
    memo["swept"] = time.time() - 600  # the last sweep was 10 minutes ago
    p = pr(12, pushed=3 * HOUR)
    p["head"] = {"sha": sha(12), "ref": "claude/ready-12"}
    memo["prs"]["12"]["head"] = sha(11)  # it saw another head then
    assert stalls(monkeypatch, [p], memo=memo) == {}
    entry = memo["prs"]["12"]
    assert entry["seen_at"] == pytest.approx(time.time() - 600, abs=5)
    assert entry["due"] == pytest.approx(
        time.time() - 600 + HOUR, abs=5
    )  # moving: due an hour after that sweep


def test_a_verdict_on_an_earlier_head_is_no_verdict_blocker(monkeypatch) -> None:
    old = verdict(12, "yes", 2 * HOUR)
    old["body"] = report(sha(999), "yes")  # it names a head the PR has since moved past
    p = pr(12)
    p["timeline"].append(old)
    found = stalls(monkeypatch, [p])
    assert "blocker: none found on GitHub" in found[12] and "without a go" not in found[12]


def test_the_heads_first_check_or_status_dates_its_push_not_a_later_one(monkeypatch) -> None:
    early = pr(
        12,
        checks=[
            {"name": "qualify", "started_at": iso(2 * HOUR)},
            {"name": "x", "started_at": iso(600)},
        ],
    )
    late = pr(
        13, statuses=[{"context": "gate", "state": "success", "created_at": iso(600)}]
    )  # no check run at all
    assert list(stalls(monkeypatch, [early, late])) == [
        12
    ]  # its first check is two hours old; 13's first status is not


def test_a_sweep_stores_its_time_so_the_next_dates_a_new_head_after_it(monkeypatch) -> None:
    """Two sweeps through pr_stalls alone: the second sees a new head (an old commit, no CI) and dates it by the
    first sweep, so it is no stall."""
    memo: dict = {}
    p = pr(12, pushed=3 * HOUR)
    assert list(stalls(monkeypatch, [p], memo=memo)) == [12]
    assert memo["swept"] == pytest.approx(time.time(), abs=5)
    memo["prs"]["12"]["head"] = sha(11)  # it saw another head in that sweep
    assert stalls(monkeypatch, [p], memo=memo) == {}
    assert memo["prs"]["12"]["seen_at"] == pytest.approx(time.time(), abs=5)


def test_a_rerun_of_a_check_is_no_push_and_its_latest_run_decides_red(monkeypatch) -> None:
    first, rerun = iso(3 * HOUR), iso(70 * 60)
    red = pr(
        12,
        pushed=3 * HOUR,
        checks=[
            {"name": "qualify", "started_at": first, "conclusion": "failure"},
            {"name": "qualify", "started_at": rerun, "conclusion": "failure"},
        ],
    )
    red["timeline"].append(verdict(12, "no", 90 * 60))
    green = pr(
        13,
        pushed=3 * HOUR,
        checks=[
            {"name": "qualify", "started_at": first, "conclusion": "failure"},
            {"name": "qualify", "started_at": rerun, "conclusion": "success"},
        ],
    )
    found = stalls(monkeypatch, [red, green])
    # moved by the verdict 90m ago, not by the re-run 70m ago
    assert "for 90m" in found[12] and "a red required check: qualify" in found[12]
    assert (
        "a verdict asking a fix round (SATISFIED: no)" in found[12]
    )  # the verdict is newer than the push
    assert "red" not in found[13]  # its re-run passed


def test_a_pr_held_by_the_queue_is_no_stall(monkeypatch) -> None:
    """omnigent #542's case (#2100): `loop:queued` on and Mergify's queue check running for over an hour is a PR
    waiting in the queue; once the check ended and Mergify let go of it, the label alone holds nothing."""
    queue = "Mergify Merge Queue"
    embarked = {
        "name": queue,
        "status": "in_progress",
        "conclusion": None,
        "started_at": iso(2 * HOUR),
    }
    earlier = {
        "name": queue,
        "status": "completed",
        "conclusion": "neutral",
        "started_at": iso(2 * HOUR + 60),
    }
    held = pr(12, labels=[{"name": "loop:queued"}], checks=[earlier, embarked])
    ended = {
        **embarked,
        "status": "completed",
        "conclusion": "failure",
        "completed_at": iso(2 * HOUR),
    }
    left = pr(13, labels=[{"name": "loop:queued"}], checks=[earlier, ended])
    unlabelled = pr(14, checks=[earlier, embarked])
    pending = {"context": queue, "state": "pending", "created_at": iso(2 * HOUR)}
    by_status = pr(
        15, labels=[{"name": "loop:queued"}], statuses=[pending]
    )  # a queue that reports by status
    assert list(stalls(monkeypatch, [held, left, unlabelled, by_status])) == [13, 14]


QUEUED = {"labels": [{"name": "loop:queued"}]}
RUN = {"name": "Mergify Merge Queue", "status": "in_progress", "conclusion": None}
DONE = {**RUN, "status": "completed", "conclusion": "failure"}


@pytest.mark.parametrize(
    "checks, statuses, holds",
    [
        ([], [{"context": "Mergify Merge Queue", "state": "pending"}], True),
        (
            [],
            [
                {"context": "Mergify Merge Queue", "state": "success"},
                {"context": "Mergify Merge Queue", "state": "pending"},
            ],
            False,
        ),
        ([], [{"context": "ci/test", "state": "pending"}], False),
        ([{**RUN, "started_at": "1"}, {**DONE, "started_at": "2"}], [], True),
        ([{**DONE, "started_at": "1"}, {**DONE, "started_at": "2"}], [], False),
        ([{**DONE, "started_at": "1"}, {**RUN, "started_at": "2"}], [], True),
    ],
    ids=[
        "a pending queue status",
        "its newest status ended",
        "a pending status of no queue",
        "a running run under a newer ended one (#2428)",
        "every run ended",
        "a running run over an older ended one",
    ],
)
def test_the_queue_holds_by_its_newest_check_run_or_status(
    checks: list[dict], statuses: list[dict], holds: bool
) -> None:
    """A queue that reports through a commit status holds a PR as one with a check run does; any queue check run still running holds it, whichever started last; a status context is read
    at its newest (statuses come newest first)."""
    assert omnigent_agent.queue_holds(QUEUED, checks, statuses) is holds
