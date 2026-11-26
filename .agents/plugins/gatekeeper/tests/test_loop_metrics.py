"""scripts/loop_metrics.py: the weekly ready->merged time and runner-minutes per merged PR (#2233), on recorded API
responses served by a fake `gh api`."""

from __future__ import annotations

import datetime as dt
import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote

SCRIPT = (
    Path(__file__).resolve().parents[1] / "skills" / "gatekeeper" / "scripts" / "loop_metrics.py"
)
spec = importlib.util.spec_from_file_location("loop_metrics", SCRIPT)
assert spec and spec.loader
loop_metrics = importlib.util.module_from_spec(spec)
sys.modules["loop_metrics"] = loop_metrics
spec.loader.exec_module(loop_metrics)

REPO = "acme/widgets"
SINCE = dt.date(2026, 11, 17)


def job(minutes: int, start: str = "2026-11-18T10:00:00Z") -> dict[str, str]:
    begin = loop_metrics.parse_time(start)
    return {
        "started_at": start,
        "completed_at": (begin + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def search_item(
    number: int, created: str, merged: str | None, title: str = "", body: str = ""
) -> dict[str, Any]:
    return {
        "number": number,
        "created_at": created,
        "title": title,
        "body": body,
        "pull_request": {"merged_at": merged},
    }


def run(run_id: int, sha: str, created: str, pulls: tuple[int, ...] = ()) -> dict[str, Any]:
    return {
        "id": run_id,
        "head_sha": sha,
        "created_at": created,
        "pull_requests": [{"number": n} for n in pulls],
    }


def committed(sha: str, tree: str) -> dict[str, Any]:
    return {"event": "committed", "sha": sha, "tree": {"sha": tree}}


# The week is 2026-11-17..2026-11-23, the week before 2026-11-10..2026-11-16.
# #1: a draft first (last ready event 1h before merge, created a day earlier), one head (a run with no PR list),
#     one 10-minute job.
# #2: never a draft (created 1h before merge); its run was rerun (attempts of 10 and 6 minutes), a force-pushed head
#     (5), the push of its merge commit (3) and a run reachable only through its `pull_requests` (2): 26.
# #3 and #4: merged through one queue batch whose run took 20 minutes; the batch's body also names #8, which never
#     merged, so the 20 minutes split over two: 10 each.
# #5: merged the week before, one 4-minute job on its head (and an incomplete job that counts nothing).
# #6: merged on the week's first day. #7: merged on the next week's first day. A search item with no merge date.
# #10: created months before the lookback reaches: its run of that day is never listed, its recent one (4) is.
RECORDED: dict[str, Any] = {
    "repos/acme/widgets/actions/workflows": {"total_count": 1},
    "search/issues|is:pr is:merged": {
        "items": [
            search_item(1, "2026-11-17T08:00:00Z", "2026-11-18T12:00:00Z"),
            search_item(2, "2026-11-19T09:00:00Z", "2026-11-19T10:00:00Z"),
            search_item(3, "2026-11-20T08:00:00Z", "2026-11-20T10:00:00Z"),
            search_item(4, "2026-11-20T08:00:00Z", "2026-11-20T10:00:00Z", 'Revert "add widgets"'),
            search_item(6, "2026-11-17T08:30:00Z", "2026-11-17T09:00:00Z"),
            search_item(5, "2026-11-12T08:00:00Z", "2026-11-12T09:00:00Z"),
            search_item(7, "2026-11-23T08:00:00Z", "2026-11-24T00:30:00Z"),
            search_item(9, "2026-11-19T08:00:00Z", None),
            search_item(10, "2026-09-01T08:00:00Z", "2026-11-20T09:00:00Z"),
        ]
    },
    "search/issues|head:mergify/merge-queue/": {
        "items": [
            search_item(90, "2026-11-20T09:00:00Z", None, body="Speculative check of #3, #4, #8")
        ]
    },
    "search/issues|is:issue": {
        "items": [
            {"number": 70, "created_at": "2026-11-21T03:00:00Z", "body": "Merges: #3 and #4"},
            {
                "number": 71,
                "created_at": "2026-11-22T03:00:00Z",
                "body": "Merges: #999, not in the set",
            },
            {"number": 72, "created_at": "2026-11-13T03:00:00Z", "body": "Merges: #5"},
        ]
    },
    "repos/acme/widgets/issues/1/timeline": [
        {"event": "ready_for_review", "created_at": "2026-11-18T09:00:00Z"},
        {"event": "ready_for_review", "created_at": "2026-11-18T11:00:00Z"},
        committed("aaa", "T1"),
    ],
    "repos/acme/widgets/issues/2/timeline": [
        committed("bbb0", "T0"),
        committed("bbb", "T2"),
        {"event": "head_ref_force_pushed", "commit_id": "fff"},
        {"event": "merged", "commit_id": "ggg"},
    ],
    "repos/acme/widgets/issues/3/timeline": [committed("ccc", "T3")],
    "repos/acme/widgets/issues/4/timeline": [committed("ddd", "T4")],
    "repos/acme/widgets/issues/6/timeline": [],
    "repos/acme/widgets/issues/5/timeline": [committed("eee", "T5")],
    "repos/acme/widgets/issues/7/timeline": [committed("hhh", "T7")],
    "repos/acme/widgets/issues/10/timeline": [committed("jjj", "T10")],
    "runs": [
        run(11, "aaa", "2026-11-18T10:00:00Z"),
        run(12, "bbb", "2026-11-19T10:00:00Z", (2,)),
        run(13, "zzz", "2026-11-20T10:00:00Z", (90,)),
        run(14, "eee", "2026-11-12T08:00:00Z", (5,)),
        run(15, "unrelated", "2026-11-18T10:00:00Z"),
        run(16, "fff", "2026-11-19T10:00:00Z"),
        run(17, "ggg", "2026-11-19T11:00:00Z"),
        run(18, "yyy", "2026-11-19T10:00:00Z", (2,)),
        run(20, "jjj", "2026-09-02T08:00:00Z"),  # before the lookback: never listed
        run(21, "jjj", "2026-11-18T09:00:00Z"),
    ],
    "repos/acme/widgets/actions/runs/11/jobs": {"jobs": [job(10)]},
    "repos/acme/widgets/actions/runs/12/jobs": {"jobs": [job(10), job(6, "2026-11-19T10:30:00Z")]},
    "repos/acme/widgets/actions/runs/13/jobs": {
        "jobs": [job(12), job(8), {"started_at": None, "completed_at": None}]
    },
    "repos/acme/widgets/actions/runs/14/jobs": {
        "jobs": [job(4), {"started_at": "2026-11-12T08:00:00Z", "completed_at": None}]
    },
    "repos/acme/widgets/actions/runs/16/jobs": {"jobs": [job(5)]},
    "repos/acme/widgets/actions/runs/17/jobs": {"jobs": [job(3)]},
    "repos/acme/widgets/actions/runs/18/jobs": {"jobs": [job(2)]},
    "repos/acme/widgets/actions/runs/21/jobs": {"jobs": [job(4)]},
}


class FakeGh:
    """A recorded `gh api`: routes by path, and serves the runs listing as GitHub does: by the `created` day asked,
    a page of 100, at most `cap` results however many pages are asked, with the true `total_count`."""

    def __init__(self, recorded: dict[str, Any], cap: int = 1000) -> None:
        self.recorded = recorded
        self.cap = cap
        self.calls: list[str] = []

    def __call__(self, path: str) -> Any:
        self.calls.append(path)
        route = unquote(path.replace("+", " "))
        base = route.split("?")[0]
        page = int(re.search(r"[?&]page=(\d+)", route).group(1)) if "&page=" in route else 1
        if base == "repos/acme/widgets/actions/runs":
            day = route.split("created=")[1].split("..")[0]
            same = [r for r in self.recorded["runs"] if r["created_at"][:10] == day]
            return {
                "total_count": len(same),
                "workflow_runs": same[: self.cap][(page - 1) * 100 : page * 100],
            }
        if base == "search/issues":
            for key, body in self.recorded.items():
                if key.startswith("search/issues|") and key.split("|", 1)[1] in route:
                    return body if page == 1 else {"items": []}
        if base in self.recorded:
            body = self.recorded[base]
            if page == 1:
                return body
            return [] if isinstance(body, list) else {k: [] for k in body}
        raise RuntimeError(f"no recorded response for {path}")


def test_week_line_counts_median_minutes_and_the_week_before() -> None:
    gh = FakeGh(RECORDED)
    text = loop_metrics.line(gh, REPO, SINCE)
    # week: #1 1h from its last ready event, #2 1h from creation, #3 and #4 2h, #6 30m (merged on the first day),
    # #10 merged 09:00 and created months ago, no draft: far over 2h. Median of six: 1h30m (#7 is next week's).
    # minutes: #1 10, #2 26, #3 and #4 (12+8)/2 = 10 each, #6 0, #10 4: 60 / 6 = 10.0
    assert text.startswith(
        f"{REPO} 2026-11-17..2026-11-23: merged 6, median ready->merged 1h30m, runner-min/PR 10.0, reverts 1, merged with no record: not measured yet (#2310), red-nights 1"
    )
    assert (
        "| week before 2026-11-10..2026-11-16: merged 1, median ready->merged 1h00m, runner-min/PR 4.0, reverts 0, merged with no record: not measured yet (#2310), red-nights 1"
        in text
    )
    assert "batch minutes split evenly across each queue batch's PRs" in text
    assert text.count("\n") == 0


def test_runs_are_listed_by_day_in_bulk_and_jobs_once_per_attributed_run() -> None:
    gh = FakeGh(RECORDED)
    loop_metrics.line(gh, REPO, SINCE)
    searches = [c for c in gh.calls if c.startswith("search/issues") and "merged" in unquote(c)]
    assert len(searches) == 1
    listings = [unquote(c) for c in gh.calls if "/actions/runs?" in c]
    days = [c.split("created=")[1].split("..")[0] for c in listings]
    # from 14 days before the two weeks (#10 was created long before) to the day after the week's end
    assert days[0] == "2026-10-27" and days[-1] == "2026-11-24" and len(days) == 29
    assert all(c.split("created=")[1].startswith(f"{d}..{d}&") for c, d in zip(listings, days))
    assert all("filter=all" in c for c in gh.calls if "/jobs" in c)  # every attempt of a rerun
    jobs = [c.split("?")[0] for c in gh.calls if "/jobs" in c]
    assert sorted(jobs) == [
        f"repos/acme/widgets/actions/runs/{n}/jobs" for n in (11, 12, 13, 14, 16, 17, 18, 21)
    ]  # run 15 is nobody's, 20 is never listed
    assert len([c for c in gh.calls if "/timeline" in c]) == 8
    assert not [
        c for c in gh.calls if "matching-refs" in c or "loop-gate" in c
    ]  # no record ref is read
    assert (
        len([c for c in gh.calls if "is:issue" in unquote(c)]) == 1
    )  # one search for the red nights


def test_a_day_with_more_runs_than_the_listing_serves_exits_naming_the_day(capsys) -> None:
    flood = [run(1000 + n, f"s{n}", "2026-11-18T10:00:00Z") for n in range(1200)]
    recorded = RECORDED | {"runs": RECORDED["runs"] + flood}
    assert loop_metrics.main([REPO, "--since", "2026-11-17"], FakeGh(recorded)) == 1
    err = capsys.readouterr().err
    assert "2026-11-18" in err and "1203" in err and "1000" in err


def test_a_repo_with_no_actions_is_skipped_with_a_line_saying_so() -> None:
    gh = FakeGh({"repos/acme/widgets/actions/workflows": {"total_count": 0}})
    assert loop_metrics.line(gh, REPO, SINCE) == f"{REPO}: skipped, no Actions workflows"
    assert gh.calls == ["repos/acme/widgets/actions/workflows?per_page=1"]


def test_an_api_error_on_the_workflows_lookup_is_an_error_not_no_actions(capsys) -> None:
    assert loop_metrics.main([REPO, "--since", "2026-11-17"], FakeGh({})) == 1
    captured = capsys.readouterr()
    assert "actions/workflows" in captured.err and "skipped" not in captured.out


def test_main_prints_one_line_per_repo(capsys) -> None:
    assert loop_metrics.main([REPO, "--since", "2026-11-17"], FakeGh(RECORDED)) == 0
    assert len(capsys.readouterr().out.splitlines()) == 1


def test_a_week_with_no_merges_prints_zero() -> None:
    gh = FakeGh(
        {
            "repos/acme/widgets/actions/workflows": {"total_count": 1},
            "search/issues|is:pr is:merged": {"items": []},
            "search/issues|head:mergify/merge-queue/": {"items": []},
            "search/issues|is:issue": {"items": []},
        }
    )
    assert (
        "merged 0, reverts 0, merged with no record: not measured yet (#2310), red-nights 0 | week before"
        in loop_metrics.line(gh, REPO, SINCE)
    )


def test_the_no_record_field_prints_its_replacement_for_both_weeks() -> None:
    text = loop_metrics.line(FakeGh(RECORDED), REPO, SINCE)
    assert (
        text.count("merged with no record: not measured yet (#2310)") == 2
    )  # the week and the week before
    assert "no-record" not in text


def test_a_server_error_or_cut_output_is_retried_and_a_client_error_is_not(monkeypatch) -> None:
    answers = [
        (1, "", "gh: Server Error (HTTP 502)"),
        (1, "", "gh: Server Error (HTTP 502)"),
        (0, '{"ok": 1}', ""),
    ]
    calls: list[str] = []

    def fake_run(args, **_kw):
        calls.append(args[-1])
        code, out, err = answers[len(calls) - 1]
        return subprocess.CompletedProcess(args, code, out, err)

    monkeypatch.setattr(loop_metrics.subprocess, "run", fake_run)
    monkeypatch.setattr(loop_metrics.time, "sleep", lambda _s: None)
    assert loop_metrics.real_gh("x") == {"ok": 1}
    assert len(calls) == 3
    calls.clear()
    answers[:] = [(1, "", "gh: Not Found (HTTP 404)")]
    try:
        loop_metrics.real_gh("y")
    except RuntimeError as error:
        assert "404" in str(error)
    assert calls == ["y"]
