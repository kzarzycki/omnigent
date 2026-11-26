"""approvals.py: `land` from a cloud session on a base that requires GitHub's merge queue (#1181).

A cloud session's proxy answers every GraphQL call with a 403, so the forge port answers each queue step over the REST
routes it serves: auto-merge on and off (`ccr/auto_merge`) to enqueue and dequeue, the draft routes
(`ccr/ready_for_review`, `ccr/convert_to_draft`), and the PR's queue state from its REST pull, its timeline's
`added_to_merge_queue` and `removed_from_merge_queue` events, GitHub's `gh-readonly-queue/<base>/pr-<n>-` ref and the
repo's `merge_group` runs. Driven end to end against the `queue` fake GitHub with GraphQL refused.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest
from approvals_harness import (
    Cloud,
    add,
    approvals,
    cloud,  # noqa: F401
    lands,  # noqa: F401
)
from test_approvals import REFUSED_GRAPHQL, cloud_land  # noqa: F401
from test_approvals_github_queue import LOG, notes_on, queue, red_group, run  # noqa: F401

MOVED = "f" * 40
BOARD = "could not set the board's `Risk` field"
# the commit of main every group here is built on, which GitHub names in the readonly ref
ON = "e" * 40


def lines(items: Any) -> str:
    """Items as `gh api --paginate --jq .[]` prints them, one JSON object a line."""
    return "".join(json.dumps(each) + "\n" for each in items)


def ccr(state: dict[str, Any], inner: Any) -> Any:
    """`inner` gh behind a cloud session's proxy: GraphQL (`gh api graphql`, and `gh pr` and `gh repo`, which read
    through it) refused with a 403 and kept in `graphql`; the draft routes kept in `routes` as (method, route, PR) and
    applied to the PR; every other call to `inner`."""
    state.update(graphql=[], routes=[])

    def gh(*args: str, data: str | None = None) -> str:
        if args[0] in ("pr", "repo") or args[:2] == ("api", "graphql"):
            state["graphql"].append(args)
            raise subprocess.CalledProcessError(1, "gh", stderr=REFUSED_GRAPHQL)
        if args[:2] == ("api", "-X") and "/ccr/" in args[3]:
            number, route = re.fullmatch(r".*/pulls/(\d+)/ccr/(\w+)", args[3]).groups()
            state["routes"].append((args[2], route, int(number)))
            if route in ("ready_for_review", "convert_to_draft"):
                state["pulls"][int(number)]["isDraft"] = route == "convert_to_draft"
                return ""
        return inner(*args, data=data)

    return gh


@pytest.fixture
def cloud_queue(queue: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """`queue` run in a cloud session: the cloud marker set, loop.md's `land` section with `where: cloud` and no
    landing key, and GitHub behind the proxy (ccr), GraphQL refused from the run's start. A PR's queue state is
    REST's alone: its `timeline` events, its group's readonly ref (`readonly`, the group's commit, made at `made`
    with `suites` check suites) and the repo's `merge_group` runs (`group_runs`), read through the adapter's rest_queue,
    which `pull_request` adds to the PR `queue` keeps. `enqueue(n)` is auto-merge on: the head the proofs judged in
    `enqueued`, the PR's head moved to `moved[n]` first, if set, and an add on its timeline; off, `dequeued` and a
    removal. `build(n, group)` has the queue build PR #n's group, `out(n)` take it out."""
    state = queue
    pulls, clock = state["pulls"], count()
    state.update(timeline={}, readonly={}, made={}, suites={}, group_runs=[], moved={}, methods=[])
    monkeypatch.setenv(approvals.CLOUD, "true")
    loop = state["work"] / "docs" / "agents" / "loop.md"
    loop.write_text(add(loop.read_text(), "land", "where: cloud\n"))
    monkeypatch.setattr(approvals.github, "_rest_only", True)
    monkeypatch.setattr(approvals.github, "required_checks", lambda _base: state["rules"])

    def stamp() -> str:
        return f"2026-10-03T00:{next(clock):02d}:00Z"

    def event(number: int, kind: str) -> None:
        state["timeline"].setdefault(number, []).append({"event": kind, "created_at": stamp()})

    def build(number: int, group: str) -> None:
        state["group_runs"].append(
            {
                "head_branch": f"gh-readonly-queue/main/pr-{number}-{ON}",
                "head_sha": group,
                "created_at": stamp(),
            }
        )
        state["readonly"][number] = group
        state["made"].setdefault(group, datetime.now(UTC).isoformat())

    def out(number: int) -> None:
        state["readonly"].pop(number, None)
        event(number, "removed_from_merge_queue")

    queued = state["queue_merge"]

    def queue_merge(number: int) -> str:
        """The queue merges #n: its group is the merge commit, then it leaves the queue merged."""
        commit = queued(number)
        build(number, commit)
        out(number)
        return commit

    faked = approvals.github.gh

    def gh(*args: str, data: str | None = None) -> str:
        path = args[2] if args[1] == "--paginate" else args[3] if args[1] == "-X" else args[1]
        if args[:3] == ("api", "-X", "PUT") and path.endswith("/ccr/auto_merge"):
            number = int(path.split("/")[-3])
            fields = dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "-f")
            if state.get("refuse"):
                raise subprocess.CalledProcessError(1, "gh", stderr="gh: not mergeable (HTTP 422)")
            if number in state["moved"]:
                pulls[number]["headRefOid"] = state["moved"].pop(number)
            state["enqueued"].append((number, fields["expected_head_oid"]))
            state["methods"].append(fields["merge_method"])
            event(number, "added_to_merge_queue")
            return ""
        if args[:3] == ("api", "-X", "DELETE") and path.endswith("/ccr/auto_merge"):
            number = int(path.split("/")[-3])
            if state.get("undeletable"):
                raise subprocess.CalledProcessError(1, "gh", stderr="gh: Server Error (HTTP 502)")
            state["dequeued"].append(number)
            out(number)
            return ""
        if re.fullmatch(r"repos/\{owner\}/\{repo\}/pulls/\d+", path) and "--jq" not in args:
            number = int(path.rsplit("/", 1)[1])
            pr = pulls[number]
            return json.dumps(
                {
                    "number": number,
                    "merged": pr["state"] == "MERGED",
                    "merge_commit_sha": state.get("squashes", {}).get(number),
                    "mergeable": {"MERGEABLE": True, "CONFLICTING": False}.get(pr["mergeable"]),
                    "auto_merge": None,
                    "head": {"sha": pr["headRefOid"]},
                    "base": {"ref": pr["baseRefName"]},
                }
            )
        if match := re.fullmatch(
            r"repos/\{owner\}/\{repo\}/issues/(\d+)/timeline\?per_page=100", path
        ):
            return lines(state["timeline"].get(int(match[1]), []))
        if match := re.fullmatch(
            r"repos/\{owner\}/\{repo\}/git/matching-refs/heads/gh-readonly-queue/main/pr-(\d+)-",
            path,
        ):
            group = state["readonly"].get(int(match[1]))
            ref = f"refs/heads/gh-readonly-queue/main/pr-{match[1]}-{ON}"
            return lines([{"ref": ref, "object": {"sha": group}}] if group else [])
        if path == "repos/{owner}/{repo}/actions/runs?event=merge_group&per_page=100":
            return json.dumps({"workflow_runs": state["group_runs"]})
        if (
            match := re.fullmatch(r"repos/\{owner\}/\{repo\}/commits/(\w+)", path)
        ) and "--jq" not in args:
            return json.dumps({"commit": {"committer": {"date": state["made"][match[1]]}}})
        if match := re.fullmatch(
            r"repos/\{owner\}/\{repo\}/commits/(\w+)/check-suites\?per_page=1", path
        ):
            return json.dumps({"total_count": state["suites"].get(match[1], 0)})
        if path == "repos/{owner}/{repo}":
            return json.dumps({"default_branch": "main"})
        if path == "repos/{owner}/{repo}/pulls?state=closed&base=main&per_page=100":
            return lines(
                {
                    "number": n,
                    "merged_at": "2026-10-03T01:00:00Z",
                    "labels": pr["labels"]["nodes"],
                }
                for n, pr in pulls.items()
                if pr["state"] == "MERGED"
            )
        return faked(*args, data=data)

    monkeypatch.setattr(approvals.github, "gh", ccr(state, gh))

    def read(number: int) -> dict[str, Any]:
        """PR #n as `queue` keeps it, its queue state as the adapter reads it from REST."""
        pull = {k: v for k, v in pulls[number].items() if k not in ("mergeQueueEntry", "removals")}
        found = approvals.github.rest(f"repos/{{owner}}/{{repo}}/pulls/{number}")
        timeline = approvals.github.rest_pages(f"issues/{number}/timeline?per_page=100")
        return pull | approvals.github.rest_queue(found, timeline)

    monkeypatch.setattr(approvals, "pull_request", read)
    landed = state["land"]

    def land(*argv: str) -> tuple[int, list[str]]:
        """land's exit and lines, less the board's `Risk` line: the board is GraphQL's alone, a separate limit."""
        code, said = landed(*argv)
        return code, [line for line in said if BOARD not in line]

    state.update(land=land, build=build, out=out, queue_merge=queue_merge, stamp=stamp, event=event)
    return state


def red_out(cloud_queue: dict[str, Any], number: int, group: str, job: int) -> None:
    """The queue built PR #n's `group`, a required check failed on it, and it took the PR out."""
    cloud_queue["build"](number, group)
    red_group(cloud_queue, group, job)
    cloud_queue["out"](number)


def test_a_cloud_land_enqueues_through_auto_merge_with_the_queues_method_and_the_judged_head(
    cloud_queue: dict[str, Any],
) -> None:
    """Example 1, with no landing key: GitHub merges and signs each PR itself."""
    eight = cloud_queue["opened"](8)
    seven = cloud_queue["opened"](7)
    code, lines = cloud_queue["land"]()
    assert code == 0, lines
    assert cloud_queue["enqueued"] == [(7, seven), (8, eight)]
    assert cloud_queue["methods"] == ["squash", "squash"]
    assert cloud_queue["graphql"] == []
    assert lines[-2:] == [
        f"PR #7 enqueued at {seven}, position None",
        f"PR #8 enqueued at {eight}, position None",
    ]
    assert (cloud_queue["tips"](), cloud_queue["runs"](), cloud_queue["merged"]()) == ([], 0, [])
    # in the queue now, by their timelines: left alone
    assert cloud_queue["land"]("7") == (
        0,
        [
            f"PR #7 is in the merge queue at {seven}, position None",
            f"PR #8 is in the merge queue at {eight}, position None",
        ],
    )
    assert len(cloud_queue["enqueued"]) == 2


def test_the_first_graphql_refusal_sends_the_enqueue_to_rest(cloud_queue: dict[str, Any]) -> None:
    """A run that has not yet seen GraphQL refused asks it once, then enables auto-merge."""
    head = cloud_queue["opened"](7)
    approvals.github._rest_only = False
    assert approvals.github.enqueue_pull(7, "PR_7", head, "SQUASH") is None
    assert [args[:2] for args in cloud_queue["graphql"]] == [("api", "graphql")]
    assert cloud_queue["enqueued"] == [(7, head)] and cloud_queue["methods"] == ["squash"]


def test_a_head_that_moved_as_auto_merge_went_on_is_taken_out_and_refused_and_the_others_land(
    cloud_queue: dict[str, Any],
) -> None:
    """Example 2: the re-read after the enable is the pin, whatever the route did with the head it was sent."""
    seven = cloud_queue["opened"](7)
    eight = cloud_queue["opened"](8)
    cloud_queue["moved"][7] = MOVED
    code, lines = cloud_queue["land"]()
    assert code == 1, lines
    assert (
        f"PR #7: its head is {MOVED} now, not {seven}, which the proofs judged: taken out of the merge queue again"
        in lines
    )
    assert cloud_queue["dequeued"] == [7]
    assert cloud_queue["enqueued"] == [(7, seven), (8, eight)]
    assert f"PR #8 enqueued at {eight}, position None" in lines


def test_a_moved_head_land_cannot_take_out_again_says_to_take_it_out_on_github(
    cloud_queue: dict[str, Any],
) -> None:
    seven = cloud_queue["opened"](7)
    cloud_queue.update(undeletable=True)
    cloud_queue["moved"][7] = MOVED
    code, lines = cloud_queue["land"]("7")
    assert code == 1, lines
    assert lines[-1] == (
        f"PR #7: its head is {MOVED} now, not {seven}, which the proofs judged, and land could not take it out of"
        " the merge queue: take it out on GitHub (gh: Server Error (HTTP 502))"
    )


def test_a_red_group_read_from_rest_comments_once_and_enqueues_again_and_a_second_turns_it_to_draft(
    cloud_queue: dict[str, Any],
) -> None:
    """Example 3's red group and example 4's second red: a removal names no reason over REST, and a required check
    that failed on its group's commit makes it `failed_checks`; the eviction goes through `ccr/convert_to_draft`."""
    head = cloud_queue["opened"](7)
    assert cloud_queue["land"]("7")[0] == 0
    red_out(cloud_queue, 7, "g1", 410)
    code, lines = cloud_queue["land"]("7")
    assert code == 0, lines
    [note] = notes_on(cloud_queue, 7, approvals.QUEUE_RED)
    assert f"Head: {head}\nGroup: g1\n" in note and "\n".join(LOG[-30:]) in note
    assert cloud_queue["enqueued"] == [(7, head), (7, head)]
    red_out(cloud_queue, 7, "g2", 420)
    code, lines = cloud_queue["land"]("7")
    assert code == 1, lines
    assert f"PR #7 is red in the merge queue twice at {head}: evicted" in lines
    assert cloud_queue["pulls"][7]["isDraft"]
    assert cloud_queue["routes"][-1] == ("POST", "convert_to_draft", 7)
    assert len(cloud_queue["enqueued"]) == 2 and cloud_queue["graphql"] == []


def test_a_removal_whose_group_failed_only_checks_the_base_does_not_require_counts_nothing(
    cloud_queue: dict[str, Any],
) -> None:
    head = cloud_queue["opened"](7)
    cloud_queue["land"]("7")
    cloud_queue["build"](7, "g1")
    cloud_queue["checks"]["g1"] = [run("lint", "failure", 411), run("qualify", "cancelled", 410)]
    cloud_queue["out"](7)
    code, lines = cloud_queue["land"]("7")
    assert code == 0, lines
    assert notes_on(cloud_queue, 7, approvals.QUEUE_RED) == []
    assert cloud_queue["enqueued"] == [(7, head), (7, head)]


def test_a_later_removal_with_no_group_of_its_own_never_reuses_an_earlier_red_group(
    cloud_queue: dict[str, Any],
) -> None:
    """A removal reads only a group built since the PR's add before it, so one red never counts twice."""
    head = cloud_queue["opened"](7)
    cloud_queue["land"]("7")
    red_out(cloud_queue, 7, "g1", 410)
    cloud_queue["land"]("7")
    cloud_queue["out"](7)  # taken out before the queue built it a group
    code, lines = cloud_queue["land"]("7")
    assert code == 0, lines
    assert not cloud_queue["pulls"][7]["isDraft"]
    assert notes_on(cloud_queue, 7, approvals.QUEUE_EVICTED) == []
    assert cloud_queue["enqueued"] == [(7, head)] * 3


def test_a_red_removal_while_mains_tip_is_red_counts_nothing(cloud_queue: dict[str, Any]) -> None:
    cloud_queue["opened"](7)
    cloud_queue["land"]("7")
    red_out(cloud_queue, 7, "g1", 410)
    cloud_queue["checks"]["main"] = [run("qualify", "failure", 500)]
    code, lines = cloud_queue["land"]("7")
    assert code == 1, lines
    assert lines[-1].startswith("main itself is red (`qualify` on its tip)")
    assert cloud_queue["comments"] == [] and len(cloud_queue["enqueued"]) == 1


def test_a_removal_github_reports_conflicting_gets_the_conflict_comment_and_no_enqueue(
    cloud_queue: dict[str, Any],
) -> None:
    cloud_queue["opened"](7)
    cloud_queue["land"]("7")
    cloud_queue["build"](7, "g1")
    cloud_queue["out"](7)
    cloud_queue["pulls"][7]["mergeable"] = "CONFLICTING"
    code, lines = cloud_queue["land"]("7")
    assert code == 1, lines
    assert "PR #7 conflicts with main: merge `origin/main` in" in lines
    assert len(cloud_queue["enqueued"]) == 1 and cloud_queue["comments"]


def test_an_entry_whose_group_waited_for_checks_github_never_started_is_taken_out_and_enqueued_again(
    cloud_queue: dict[str, Any],
) -> None:
    head = cloud_queue["opened"](7)
    cloud_queue["land"]("7")
    cloud_queue["made"]["g1"] = (datetime.now(UTC) - timedelta(minutes=11)).isoformat()
    cloud_queue["build"](7, "g1")
    code, lines = cloud_queue["land"]("7")
    assert code == 0, lines
    assert any(line.endswith("dequeued, to enqueue it again") for line in lines), lines
    assert cloud_queue["dequeued"] == [7] and cloud_queue["enqueued"] == [(7, head)] * 2


def test_land_on_the_rest_queue_enqueues_and_exits_with_no_poll_and_no_graphql(
    cloud_queue: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    head = cloud_queue["opened"](7)
    # a poll sleeps POLL (30 s) or more; subprocess.wait sleeps a few ms on the same time module, which a slow
    # runner reaches while a git push runs (#1892), so only a poll-length sleep fails
    real_sleep = approvals.time.sleep
    monkeypatch.setattr(
        approvals.time,
        "sleep",
        lambda seconds: pytest.fail("polled the queue") if seconds >= 1 else real_sleep(seconds),
    )
    # without --wait; `land --wait` waits on for the merge (test_land_wait_queue_budget.py)
    code, lines = cloud_queue["land"]("7")
    assert code == 0, lines
    assert cloud_queue["enqueued"] == [(7, head)] and cloud_queue["graphql"] == []


def test_a_green_group_already_in_main_is_read_as_merged_before_github_reports_it(
    cloud_queue: dict[str, Any],
) -> None:
    """GitHub takes the PR out a moment before it reports the merge: a group commit in main waits, never enqueued."""
    head = cloud_queue["opened"](7)
    cloud_queue["land"]("7")
    cloud_queue["build"](7, "g1")
    cloud_queue["checks"]["g1"] = [run("qualify", "success", 410)]
    cloud_queue["built"]["g1"] = "g1"  # its merge base with main is itself: main holds it
    cloud_queue["out"](7)
    assert cloud_queue["land"]("7") == (
        0,
        [f"PR #7 left the merge queue merged at {head}"],
    )
    assert len(cloud_queue["enqueued"]) == 1


def test_a_named_draft_is_marked_ready_through_the_draft_route_on_the_github_merge_queue(
    cloud_queue: dict[str, Any],
) -> None:
    """Example 4 on the GitHub merge queue: no longer refused in a cloud session."""
    cloud_queue["opened"](7)
    cloud_queue["pulls"][7]["isDraft"] = True
    code, lines = cloud_queue["land"]("7")
    assert code == approvals.WAITING, lines
    assert "PR #7 is ready now" in lines
    assert cloud_queue["routes"] == [("POST", "ready_for_review", 7)]
    assert not cloud_queue["pulls"][7]["isDraft"] and cloud_queue["graphql"] == []


def test_a_named_draft_is_marked_ready_through_the_draft_route_on_the_local_merge_queue(
    cloud_land: dict[str, Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Example 4 on the local merge queue: the cloud land readies the draft, then lands it signed."""
    monkeypatch.setattr(approvals.github, "_rest_only", True)
    monkeypatch.setattr(approvals.github, "gh", ccr(cloud_land, approvals.github.gh))
    cloud_land["pulls"][7]["isDraft"] = True
    lines = cloud_land["out"](capsys, cloud_land["land"]("7"))
    assert "PR #7 is ready now" in lines
    assert cloud_land["routes"] == [("POST", "ready_for_review", 7)]
    assert any(line.startswith("PR #7 landed in main as ") for line in lines), lines


def test_rest_pull_reads_the_queue_state_from_the_timeline(cloud: Cloud) -> None:
    """The PR read over REST carries rest_queue's entry and removals."""
    timeline = "repos/{owner}/{repo}/issues/7/timeline?per_page=100"
    refs = "repos/{owner}/{repo}/git/matching-refs/heads/gh-readonly-queue/main/pr-7-"
    cloud.routes.update(
        {
            timeline: [{"event": "added_to_merge_queue", "created_at": "2026-10-03T00:00:00Z"}],
            refs: [],
        }
    )
    pull = approvals.github.rest_pull(7)
    assert pull["mergeQueueEntry"] == {"state": "QUEUED", "position": None, "headCommit": None}
    assert pull["removals"] == {"nodes": []}
    cloud.routes[timeline].append(
        {"event": "removed_from_merge_queue", "created_at": "2026-10-03T00:01:00Z"}
    )
    cloud.routes["repos/{owner}/{repo}/actions/runs?event=merge_group&per_page=100"] = {
        "workflow_runs": []
    }
    cloud.routes["repos/{owner}/{repo}/pulls/7"]["mergeable"] = True  # GitHub knows: no conflict
    pull = approvals.github.rest_pull(7)
    assert pull["mergeQueueEntry"] is None
    assert pull["removals"] == {
        "nodes": [{"reason": "unknown", "createdAt": "2026-10-03T00:01:00Z", "beforeCommit": None}]
    }


def test_a_red_removal_is_read_while_the_groups_readonly_ref_outlives_it(
    cloud_queue: dict[str, Any],
) -> None:
    """The timeline says the PR is out: a ref GitHub has not deleted yet is no entry, so the red is handled."""
    head = cloud_queue["opened"](7)
    cloud_queue["land"]("7")
    red_out(cloud_queue, 7, "g1", 410)
    cloud_queue["suites"]["g1"] = 1
    cloud_queue["readonly"][7] = "g1"
    code, lines = cloud_queue["land"]("7")
    assert code == 0, lines
    assert not any("is in the merge queue" in line for line in lines), lines
    [note] = notes_on(cloud_queue, 7, approvals.QUEUE_RED)
    assert f"Head: {head}\nGroup: g1\n" in note
    assert cloud_queue["enqueued"] == [(7, head), (7, head)]


def test_a_conflict_removal_is_read_while_the_groups_readonly_ref_outlives_it(
    cloud_queue: dict[str, Any],
) -> None:
    cloud_queue["opened"](7)
    cloud_queue["land"]("7")
    cloud_queue["build"](7, "g1")
    cloud_queue["out"](7)
    cloud_queue["readonly"][7] = "g1"
    cloud_queue["pulls"][7]["mergeable"] = "CONFLICTING"
    code, lines = cloud_queue["land"]("7")
    assert code == 1, lines
    assert "PR #7 conflicts with main: merge `origin/main` in" in lines
    assert len(cloud_queue["enqueued"]) == 1


def test_a_removal_read_while_github_works_out_mergeability_waits_as_on_a_host(
    cloud_queue: dict[str, Any],
) -> None:
    """REST's `mergeable: null` after a removal: the gatekeeper waits a poll, as for GraphQL's `UNKNOWN` conflict."""
    head = cloud_queue["opened"](7)
    cloud_queue["land"]("7")
    cloud_queue["build"](7, "g1")
    cloud_queue["out"](7)
    cloud_queue["pulls"][7]["mergeable"] = "UNKNOWN"
    assert cloud_queue["land"]("7") == (
        approvals.WAITING,
        ["GitHub is still working out whether PR #7 conflicts with main"],
    )
    assert cloud_queue["enqueued"] == [(7, head)]
    cloud_queue["pulls"][7]["mergeable"] = "MERGEABLE"  # GitHub decided: no conflict
    assert cloud_queue["land"]("7")[0] == 0
    assert cloud_queue["enqueued"] == [(7, head), (7, head)]


def test_rest_pull_carries_each_review_s_submitted_time(cloud: Cloud) -> None:
    """`check stalled` measures a verified PR's idle hours from its newest verdict's time, which a cloud session reads
    over REST (#1746)."""
    reviews = cloud.routes["repos/{owner}/{repo}/pulls/7/reviews?per_page=100"]
    reviews[0]["submitted_at"] = "2026-10-03T00:00:00Z"
    pull = approvals.github.rest_pull(7)
    assert [review["submittedAt"] for review in pull["reviews"]["nodes"]] == [
        "2026-10-03T00:00:00Z"
    ]
