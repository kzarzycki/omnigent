#!/usr/bin/env -S uv run -q --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""loop-metrics: one weekly number pair per repo, so a migration that makes the loop slower or dearer shows
within a week (#2233, part of #2143). Read-only: it prints and posts nothing.

    uv run scripts/loop_metrics.py <owner/repo> [<owner/repo> ...] --since <YYYY-MM-DD>

One line per repo for the 7 days from `--since`, with the 7 days before it beside it:

    owner/repo 2026-11-17..2026-11-23: merged 12, median ready->merged 1h12m, runner-min/PR 34.5 | week before 2026-11-10..2026-11-16: merged 9, ...

- merged: PRs merged in the week.
- median ready->merged: from the PR's last `ready_for_review` event (else its creation, when it was never a
  draft) to its merge.
- runner-min/PR: the sum of job durations (every attempt of a rerun counted) of every run on the PR's heads,
  on its queue entries and on the push of its merge commit, divided by the merged PRs. A queue batch (a
  Mergify draft PR from a `mergify/merge-queue/` branch) tests several PRs: its minutes split evenly over
  the batch's PRs that merged in the two weeks, and a line that holds such minutes says
  `batch minutes split evenly`.

- merged with no record: not measured yet (#2310). A merged PR's record ref is deleted at landing, so it can't be read
  after the merge; the line prints `merged with no record: not measured yet (#2310)` until `land` names the record
  in the head's `land` status.
- reverts: merged PRs titled `Revert`, counted from the merged-PR search already made.
- red-nights: red-nightly issues (`Nightly red since ...`, one search) created in the week whose body names the
  number of a merged PR (`#<n>`).

A repo with no Actions workflows is skipped with a line that says so.

Quota, per repo: one search for the two weeks' merged PRs (a page per 100), one search for the queue-batch
PRs, one for the red-nightly issues, one `timeline` call per merged PR (the only source of its ready events, heads and merge commit; never
per job), the runs listing in bulk, one day at a time (`created=<d>..<d>`, a page per 100 runs, indexed here by
`head_sha`; from the oldest merged PR's creation, at most 14 days before the two weeks, to the day after the
week's end), and one jobs listing per attributed run (`filter=all`, so every attempt). The runs listing serves
1000 results at most for a query and then answers empty pages without an error: a day with more runs than were
paged exits 1 naming the day, never a figure that is too low. A failed API call exits 1 naming the call.
Needs `gh`, nothing else.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

BATCH_PREFIX = "mergify/merge-queue/"
PR_NUMBER = re.compile(r"#(\d+)")
RUNS_CAP = 1000  # what the runs listing serves for one query, however many pages are asked
LOOKBACK_DAYS = 14  # how far before the two weeks a PR's earlier heads are searched for runs
Gh = Callable[[str], Any]


def real_gh(path: str) -> Any:
    """`gh api <path>` as JSON; a failure that is not a client error (HTTP 4xx: a server error, a cut connection) is retried twice."""
    for attempt in range(3):
        out = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False)
        if out.returncode == 0:
            return json.loads(out.stdout)
        message = out.stderr.strip() or out.stdout.strip()
        if attempt == 2 or "HTTP 4" in message:
            raise RuntimeError(f"gh api {path}: {message}")
        time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def parse_time(text: str) -> dt.datetime:
    return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def pages(gh: Gh, path: str, key: str | None = None) -> list[Any]:
    """Every item of a paginated listing (`key` names the list inside an object), 100 a page."""
    sep = "&" if "?" in path else "?"
    items: list[Any] = []
    page = 1
    while True:
        body = gh(f"{path}{sep}per_page=100&page={page}")
        chunk = body[key] if key else body
        items.extend(chunk)
        if len(chunk) < 100:
            return items
        page += 1


def has_actions(gh: Gh, repo: str) -> bool:
    """Whether the repo has Actions workflows. An API error raises: only a real empty answer is "no Actions"."""
    return gh(f"repos/{repo}/actions/workflows?per_page=1").get("total_count", 0) > 0


def runs_of_day(gh: Gh, repo: str, day: dt.date) -> list[dict[str, Any]]:
    """Every run created on `day`. The listing serves at most RUNS_CAP results for a query and then answers empty
    pages without an error, so a day whose `total_count` passes what was paged raises, naming the day."""
    runs: list[dict[str, Any]] = []
    page = 1
    while True:
        body = gh(f"repos/{repo}/actions/runs?created={day}..{day}&per_page=100&page={page}")
        runs.extend(body["workflow_runs"])
        if len(body["workflow_runs"]) < 100:
            break
        page += 1
    if body.get("total_count", len(runs)) > len(runs):
        raise RuntimeError(
            f"runs created on {day}: {body['total_count']} exist but the listing serves {len(runs)} "
            f"(its cap is {RUNS_CAP}), so the runner-minutes would be too low; narrow the window"
        )
    return runs


def merged_prs(gh: Gh, repo: str, first: dt.date, last: dt.date) -> dict[int, dict[str, Any]]:
    query = f"repo:{repo} is:pr is:merged merged:{first}..{last}"
    found = pages(gh, "search/issues?q=" + query.replace(" ", "+"), "items")
    return {
        item["number"]: {
            "title": item.get("title") or "",
            "created": parse_time(item["created_at"]),
            "merged": parse_time(item["pull_request"]["merged_at"]),
        }
        for item in found
        if (item.get("pull_request") or {}).get("merged_at")
    }


def batches(gh: Gh, repo: str, first: dt.date) -> dict[int, list[int]]:
    """Queue-batch draft PR number -> the PR numbers its body names."""
    query = f"repo:{repo} is:pr head:{BATCH_PREFIX} updated:>={first}"
    found = pages(gh, "search/issues?q=" + query.replace(" ", "+"), "items")
    return {
        item["number"]: sorted(
            {int(n) for n in PR_NUMBER.findall(item.get("body") or "")} - {item["number"]}
        )
        for item in found
    }


def read_timeline(gh: Gh, repo: str, number: int) -> dict[str, Any]:
    ready = None
    shas: set[str] = set()
    for event in pages(gh, f"repos/{repo}/issues/{number}/timeline"):
        kind = event.get("event")
        if kind == "ready_for_review":
            when = parse_time(event["created_at"])
            ready = when if ready is None or when > ready else ready
        elif kind == "committed" and event.get("sha"):
            shas.add(event["sha"])
        elif kind in ("head_ref_force_pushed", "merged") and event.get("commit_id"):
            shas.add(event["commit_id"])
    return {"ready": ready, "shas": shas}


def red_nights(gh: Gh, repo: str, first: dt.date) -> list[dict[str, Any]]:
    query = f'repo:{repo} is:issue "Nightly red since" in:title created:>={first}'
    found = pages(gh, "search/issues?q=" + query.replace(" ", "+"), "items")
    return [
        {
            "created": parse_time(i["created_at"]),
            "names": {int(n) for n in PR_NUMBER.findall(i.get("body") or "")},
        }
        for i in found
    ]


def job_minutes(gh: Gh, repo: str, run_id: int) -> float:
    total = 0.0
    for job in pages(gh, f"repos/{repo}/actions/runs/{run_id}/jobs?filter=all", "jobs"):
        if job.get("started_at") and job.get("completed_at"):
            total += (
                parse_time(job["completed_at"]) - parse_time(job["started_at"])
            ).total_seconds() / 60
    return total


def collect(gh: Gh, repo: str, since: dt.date) -> dict[str, Any]:
    """Per merged PR (the week and the week before): merged-at, ready->merged seconds, runner-minutes."""
    first, last = since - dt.timedelta(days=7), since + dt.timedelta(days=6)
    prs = merged_prs(gh, repo, first, last)
    batch_prs = batches(gh, repo, first)
    with ThreadPoolExecutor(8) as pool:  # the same calls, 8 at a time
        timelines = dict(zip(prs, pool.map(lambda n: read_timeline(gh, repo, n), prs)))
    owner_of_sha: dict[str, set[int]] = {}
    for number, timeline in timelines.items():
        for sha in timeline["shas"]:
            owner_of_sha.setdefault(sha, set()).add(number)
    runs: list[dict[str, Any]] = []
    if prs:
        # a day at a time, from the oldest merged PR's creation (at most LOOKBACK_DAYS before the two weeks) to the
        # day after the week's end, when its merge commit's push run still lands
        floor = max(
            min(p["created"] for p in prs.values()).date() - dt.timedelta(days=1),
            first - dt.timedelta(days=LOOKBACK_DAYS),
        )
        day = floor
        while day <= last + dt.timedelta(days=1):
            runs += runs_of_day(gh, repo, day)
            day += dt.timedelta(days=1)
    minutes = {n: 0.0 for n in prs}
    batched = False
    attributed: list[tuple[dict[str, Any], set[int], set[int]]] = []
    for run in runs:
        owners = set(owner_of_sha.get(run.get("head_sha", ""), ()))
        share: set[int] = set()
        for pull in run.get("pull_requests") or []:
            number = pull["number"]
            if number in prs:
                owners.add(number)
            elif number in batch_prs:
                share |= {n for n in batch_prs[number] if n in prs}
        if owners or share:
            attributed.append((run, owners, share))
    with ThreadPoolExecutor(8) as pool:  # one jobs listing per attributed run, 8 at a time
        spent = list(pool.map(lambda item: job_minutes(gh, repo, item[0]["id"]), attributed))
    for (_run, owners, share), used in zip(attributed, spent):
        for number in owners:
            minutes[number] += used
        for number in share:
            minutes[number] += used / len(share)
            batched = True
    nights = red_nights(gh, repo, since - dt.timedelta(days=7))
    rows = []
    for number, pr in prs.items():
        ready = timelines[number]["ready"] or pr["created"]
        rows.append(
            {
                "number": number,
                "merged": pr["merged"],
                "seconds": (pr["merged"] - ready).total_seconds(),
                "minutes": minutes[number],
                "revert": pr["title"].lstrip().lower().startswith("revert"),
            }
        )
    return {"rows": rows, "batched": batched, "nights": nights, "merged_numbers": set(prs)}


def duration(seconds: float) -> str:
    whole = round(seconds)
    hours, rest = divmod(whole, 3600)
    mins = rest // 60
    return f"{hours}h{mins:02d}m" if hours else f"{mins}m"


NO_RECORD = "merged with no record: not measured yet (#2310)"


def summary(rows: list[dict[str, Any]], red_nights: int) -> str:
    quality = f"{NO_RECORD}, red-nights {red_nights}"
    if not rows:
        return f"merged 0, reverts 0, {quality}"
    median = duration(statistics.median(r["seconds"] for r in rows))
    per_pr = sum(r["minutes"] for r in rows) / len(rows)
    return (
        f"merged {len(rows)}, median ready->merged {median}, runner-min/PR {per_pr:.1f}, "
        f"reverts {sum(r['revert'] for r in rows)}, {quality}"
    )


def line(gh: Gh, repo: str, since: dt.date) -> str:
    if not has_actions(gh, repo):
        return f"{repo}: skipped, no Actions workflows"
    data = collect(gh, repo, since)
    week = [r for r in data["rows"] if since <= r["merged"].date() < since + dt.timedelta(days=7)]
    before = [r for r in data["rows"] if since - dt.timedelta(days=7) <= r["merged"].date() < since]
    prior = since - dt.timedelta(days=7)

    def nights(first: dt.date, end: dt.date) -> int:
        return sum(
            1
            for n in data["nights"]
            if first <= n["created"].date() < end and n["names"] & data["merged_numbers"]
        )

    one = since + dt.timedelta(days=6)
    text = f"{repo} {since}..{one}: " + summary(week, nights(since, since + dt.timedelta(days=7)))
    text += f" | week before {prior}..{since - dt.timedelta(days=1)}: " + summary(
        before, nights(prior, since)
    )
    if data["batched"]:
        text += " (batch minutes split evenly across each queue batch's PRs)"
    return text


def main(argv: list[str] | None = None, gh: Gh = real_gh) -> int:
    parser = argparse.ArgumentParser(
        prog="loop-metrics",
        description="Weekly ready->merged time and runner-minutes per merged PR.",
    )
    parser.add_argument("repos", nargs="+", metavar="owner/repo")
    parser.add_argument(
        "--since",
        required=True,
        type=dt.date.fromisoformat,
        help="first day of the week, YYYY-MM-DD",
    )
    args = parser.parse_args(argv)
    code = 0
    for repo in args.repos:
        try:
            print(line(gh, repo, args.since))
        except RuntimeError as error:
            print(f"{repo}: error, {error}", file=sys.stderr)
            code = 1
    return code


if __name__ == "__main__":
    sys.exit(main())
