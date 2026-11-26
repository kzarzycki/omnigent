"""approvals.py: `land` through the GitHub merge queue, where the base's rules require one (#1114).

Driven end to end against the `lands` fake GitHub, which gains the queue: `enqueuePullRequest` and `dequeuePullRequest`,
a PR's `mergeQueueEntry`, its `RemovedFromMergeQueueEvent`s, and the check runs and job logs of a commit.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from approvals_harness import (
    GITHUB_RULE,
    HEAD,
    HELD,
    approvals,
    git_in,
    lands,  # noqa: F401
    loop_md,
)

RED_AT, AGAIN_AT, PUSHED_LATER, NEW_RED_AT = (
    "2026-10-02T09:00:00Z",
    "2026-10-02T10:00:00Z",
    "2026-10-02T11:00:00Z",
    "2026-10-02T12:00:00Z",
)
LOG = [f"log line {n}" for n in range(1, 41)]
# the function the autouse `required` fixture replaces, captured at import, before any test runs
REAL_REQUIRED = approvals.required_checks


def run(name: str, conclusion: str, job: int) -> dict[str, Any]:
    """A check run as REST's `commits/<ref>/check-runs` lists it."""
    return {
        "name": name,
        "conclusion": conclusion,
        "id": job,
        "html_url": f"https://github.com/o/r/actions/runs/{job // 10}/job/{job}",
    }


@pytest.fixture
def queue(lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """`lands` on a base whose rules require `qualify` and a SQUASH merge queue. `enqueued` and `dequeued` hold each
    mutation's (PR, head); `checks` the check runs by commit or branch, `logs` the job logs by id, `built` the commit of
    main each group was built on (`m0`, which has no checks, by default). `remove(n, reason, at, group)` has the queue
    drop PR #n; `merge(n)` has it merge #n, its runs on the merge commit."""
    rules = approvals.github.Required({"qualify"})
    rules.queue = "SQUASH"
    state = lands
    state.update(rules=rules, enqueued=[], dequeued=[], checks={}, logs={}, built={}, listed=[])
    pulls = state["pulls"]
    monkeypatch.setattr(approvals, "required_checks", lambda _base: state["rules"])
    fake = approvals.github.gh

    def numbered(node: str) -> int:
        return int(node.removeprefix("PR_"))

    def gh(*args: str, data: str | None = None) -> str:
        if args[:2] == ("api", "graphql") and ": pullRequest(number:" in " ".join(args):
            # a queue wait's poll (queue_watch): each PR's state, head, queue entry and labels
            return json.dumps(
                {
                    "data": {
                        "repository": {
                            f"p{n}": {
                                "state": pr["state"],
                                "headRefOid": pr["headRefOid"],
                                "isInMergeQueue": bool(pr.get("mergeQueueEntry")),
                                "labels": pr["labels"],
                            }
                            for n, pr in pulls.items()
                            if f"p{n}: pullRequest(number: {n})" in " ".join(args)
                        }
                    }
                }
            )
        if args[:2] == ("api", "graphql"):
            fields = dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "-f")
            number = numbered(fields["id"])
            if fields["query"] == approvals.github.DEQUEUE:
                state["dequeued"].append(number)
                pulls[number]["mergeQueueEntry"] = None
                return "{}"
            assert fields["query"] == approvals.github.ENQUEUE, fields["query"]
            if state.get("refuse") or fields["head"] != pulls[number]["headRefOid"]:
                raise subprocess.CalledProcessError(1, "gh", stderr="gh: head moved")
            state["enqueued"].append((number, fields["head"]))
            position = len(state["enqueued"])
            pulls[number]["mergeQueueEntry"] = {"state": "QUEUED", "position": position}
            return json.dumps(
                {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"position": position}}}}
            )
        if args[:2] == ("api", "--paginate") and "/check-runs" in args[2]:
            ref = args[2].split("/commits/")[1].split("/")[0]
            return "".join(json.dumps(each) + "\n" for each in state["checks"].get(ref, []))
        if args[0] == "api" and "/compare/" in args[1]:
            if (built := state["built"].get(args[1].split("...")[1], "m0")) is None:
                raise subprocess.CalledProcessError(1, "gh", stderr="gh: Not Found (HTTP 404)")
            return built + "\n"
        if args[:2] == ("pr", "list"):
            assert args[args.index("--state") + 1] == "merged"
            state["listed"].append(args)
            if state.get("unlisted"):
                raise subprocess.CalledProcessError(1, "gh", stderr="gh: timed out")
            wanted = args[args.index("--label") + 1]
            assert args[args.index("--jq") + 1] == ".[].number"
            return "".join(
                f"{n}\n"
                for n, pr in pulls.items()
                if pr["state"] == "MERGED" and wanted in labels_of(state, n)
            )
        if args[0] == "api" and args[1].endswith("/logs"):
            return "\n".join(state["logs"][int(args[1].split("/")[-2])]) + "\n"
        if args[0] == "api" and re.fullmatch(r"repos/\{owner\}/\{repo\}/commits/\w+", args[1]):
            sha = args[1].rsplit("/", 1)[1]
            return git_in(state["origin"], "rev-parse", f"{sha}^{{tree}}")
        return fake(*args, data=data)

    def opened(number: int, written: dict[str, str] | None = None, **fields: Any) -> str:
        head = state["open"](
            number,
            written or {f"{number}.txt": f"{number}\n"},
            checks=(("qualify", "SUCCESS"),),
            **fields,
        )
        pulls[number].update(id=f"PR_{number}", mergeable="MERGEABLE", mergeQueueEntry=None)
        pulls[number]["removals"] = {"nodes": []}
        return head

    def remove(number: int, reason: str, at: str = RED_AT, group: str = "g1") -> None:
        pulls[number]["mergeQueueEntry"] = None
        pulls[number]["removals"]["nodes"].append(
            {"reason": reason, "createdAt": at, "beforeCommit": {"oid": group}}
        )

    def merge(number: int) -> str:
        fake(
            "pr",
            "merge",
            str(number),
            "--squash",
            "--match-head-commit",
            pulls[number]["headRefOid"],
        )
        commit = state["squashes"][number]
        approvals.label(number, approvals.QUEUED, add=True)  # as the land that enqueued it did
        remove(number, approvals.QUEUE_MERGED, at=AGAIN_AT, group=commit)
        # a check another app runs, which links no Actions run
        other = {
            "name": "cla",
            "conclusion": "success",
            "id": 1,
            "html_url": "https://cla.example.com/7",
        }
        state["checks"][commit] = [
            run("qualify", "success", 991),
            run("lint", "success", 992),
            other,
        ]
        return commit

    def label(number: int, name: str, add: bool) -> None:
        nodes = pulls[number]["labels"]["nodes"]
        nodes[:] = [each for each in nodes if each["name"] != name] + (
            [{"name": name}] if add else []
        )

    monkeypatch.setattr(approvals.github, "gh", gh)
    monkeypatch.setattr(approvals, "label", label)
    state.update(opened=opened, remove=remove, queue_merge=merge)
    return state


def labels_of(queue: dict[str, Any], number: int) -> set[str]:
    return {each["name"] for each in queue["pulls"][number]["labels"]["nodes"]}


def red_group(queue: dict[str, Any], group: str, job: int) -> None:
    """`qualify`, which the base requires, failed on the queue's commit `group`, with LOG as its job's log, and so
    did `lint`, which it doesn't."""
    queue["checks"][group] = [run("lint", "failure", job + 1), run("qualify", "failure", job)]
    queue["logs"][job] = LOG


def notes_on(queue: dict[str, Any], number: int, title: str) -> list[str]:
    return [
        body for n, body in queue.get("raw", []) if n == number and body.startswith(title + "\n")
    ]


def test_a_bare_land_enqueues_every_landable_pr_oldest_first_pinned_to_its_head(
    queue: dict[str, Any],
) -> None:
    eight = queue["opened"](8)
    seven = queue["opened"](7)
    code, lines = queue["land"]()
    assert code == 0, lines
    assert queue["enqueued"] == [(7, seven), (8, eight)]
    assert lines[-2:] == [
        f"PR #7 enqueued at {seven}, position 1",
        f"PR #8 enqueued at {eight}, position 2",
    ]
    # no landing lock, no local gate, nothing merged by land itself
    assert (queue["tips"](), queue["runs"](), queue["merged"]()) == ([], 0, [])


def test_land_on_a_queue_base_enqueues_and_exits_without_a_sleep_or_another_read(
    queue: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without `--wait` the queue is GitHub's to watch: land enqueues and exits, so a session holds no slot and spends no
    quota; `land --wait` waits on until the queue merges it or takes it out (test_land_wait_queue_budget.py)."""
    head = queue["opened"](7)
    # a poll sleeps POLL (30 s) or more; subprocess.wait sleeps a few ms on the same time module, which a slow
    # runner reaches while a git push runs (#1892), so only a poll-length sleep fails
    real_sleep = approvals.time.sleep
    monkeypatch.setattr(
        approvals.time,
        "sleep",
        lambda seconds: pytest.fail("polled the queue") if seconds >= 1 else real_sleep(seconds),
    )
    code, lines = queue["land"]("7")
    assert (code, lines) == (0, [f"PR #7 enqueued at {head}, position 1"])
    assert queue["enqueued"] == [(7, head)]
    # a second land, or a relayed event, finds it in the queue: said, exit 0, not enqueued twice
    code, lines = queue["land"]("7")
    assert (code, lines) == (0, [f"PR #7 is in the merge queue at {head}, position 1"])
    assert queue["enqueued"] == [(7, head)]


def test_a_named_pr_enqueued_or_already_queued_exits_0_naming_its_position_and_head(
    queue: dict[str, Any],
) -> None:
    head = queue["opened"](7)
    assert queue["land"]("7") == (0, [f"PR #7 enqueued at {head}, position 1"])
    queue["pulls"][7]["mergeQueueEntry"]["position"] = 3
    code, lines = queue["land"]("7")
    assert (code, lines) == (
        0,
        [f"PR #7 is in the merge queue at {head}, position 3"],
    )
    assert queue["enqueued"] == [(7, head)]  # left alone at the head it was queued at


def test_a_high_risk_pr_without_the_owners_go_on_its_head_is_not_enqueued(
    queue: dict[str, Any],
) -> None:
    head = queue["opened"](7, {".github/ci.yml": "ci\n"})
    queue["pulls"][7]["files"] = {"nodes": [{"path": ".github/ci.yml"}]}
    code, lines = queue["land"]("7")
    assert code == approvals.WAITING, lines
    assert HELD.replace(HEAD, head) + "it matches a risk rule (path `.github/**`)" in lines
    assert queue["enqueued"] == []


def test_a_red_group_comments_the_failing_checks_tail_and_enqueues_the_pr_once_more(
    queue: dict[str, Any],
) -> None:
    head = queue["opened"](7)
    queue["remove"](7, approvals.FAILED_CHECKS, at=RED_AT, group="g1")
    red_group(queue, "g1", 410)
    code, lines = queue["land"]("7")
    assert code == 0, lines
    assert queue["enqueued"] == [(7, head)]
    [note] = notes_on(queue, 7, approvals.QUEUE_RED)
    assert f"Head: {head}\nGroup: g1\n" in note
    assert (
        "`qualify` failed on the merge queue's commit g1 (run https://github.com/o/r/actions/runs/41)"
        in note
    )
    assert "\n".join(LOG[-30:]) in note and LOG[9] + "\n" not in note
    assert not queue["pulls"][7]["isDraft"]


def test_a_second_red_on_the_same_head_turns_the_pr_back_to_draft(queue: dict[str, Any]) -> None:
    head = queue["opened"](7)
    queue["remove"](7, approvals.FAILED_CHECKS, at=RED_AT, group="g1")
    red_group(queue, "g1", 410)
    queue["land"]("7")
    queue["remove"](7, approvals.FAILED_CHECKS, at=AGAIN_AT, group="g2")
    red_group(queue, "g2", 420)
    code, lines = queue["land"]("7")
    assert code == 1, lines
    assert f"PR #7 is red in the merge queue twice at {head}: evicted" in lines
    assert queue["pulls"][7]["isDraft"] and queue["enqueued"] == [(7, head)]
    [evicted] = notes_on(queue, 7, approvals.QUEUE_EVICTED)
    assert (
        "`qualify` failed on the merge queue's commit g2 (run https://github.com/o/r/actions/runs/42)"
        in evicted
    )
    assert "\n".join(LOG[-30:]) in evicted


def test_a_red_on_a_new_head_counts_from_one_again(
    queue: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    queue["opened"](7)
    queue["remove"](7, approvals.FAILED_CHECKS, at=RED_AT, group="g1")
    red_group(queue, "g1", 410)
    queue["land"]("7")
    git_in(queue["work"], "commit", "-q", "--allow-empty", "-m", "fix")
    new = git_in(queue["work"], "rev-parse", "HEAD").strip()
    git_in(queue["work"], "push", "-q", "origin", f"{new}:refs/heads/pr7")
    queue["pulls"][7].update(headRefOid=new, mergeQueueEntry=None)
    queue["pulls"][7]["reviews"]["nodes"][0]["commit"]["oid"] = new
    monkeypatch.setattr(approvals, "head_pushed", lambda _pull: PUSHED_LATER)
    queue["remove"](7, approvals.FAILED_CHECKS, at=NEW_RED_AT, group="g3")
    red_group(queue, "g3", 430)
    code, lines = queue["land"]("7")
    assert code == 0, lines
    assert queue["enqueued"][-1] == (7, new)
    assert not queue["pulls"][7]["isDraft"] and notes_on(queue, 7, approvals.QUEUE_EVICTED) == []


def test_a_removal_while_mains_tip_is_red_counts_nothing_and_evicts_nobody(
    queue: dict[str, Any],
) -> None:
    queue["opened"](7)
    queue["remove"](7, approvals.FAILED_CHECKS, at=RED_AT, group="g1")
    queue["remove"](7, approvals.FAILED_CHECKS, at=AGAIN_AT, group="g2")
    red_group(queue, "g2", 420)
    queue["checks"]["main"] = [run("qualify", "failure", 500)]
    code, lines = queue["land"]("7")
    assert code == 1, lines
    assert lines[-1].startswith("main itself is red (`qualify` on its tip)")
    assert queue["comments"] == [] and not queue["pulls"][7]["isDraft"] and queue["enqueued"] == []


def test_a_removal_for_a_conflict_gets_the_conflict_comment_and_is_not_enqueued(
    queue: dict[str, Any],
) -> None:
    head = queue["opened"](7)
    queue["pulls"][7]["mergeable"] = "CONFLICTING"
    queue["remove"](7, approvals.MERGE_CONFLICT)
    code, lines = queue["land"]("7")
    assert code == 1, lines
    assert lines == ["PR #7 conflicts with main: merge `origin/main` in"]
    [note] = notes_on(queue, 7, approvals.CONFLICTS)
    assert f"Head: {head}" in note and "merge origin/main in" in note
    assert queue["enqueued"] == []
    queue["land"]("7")
    assert len(notes_on(queue, 7, approvals.CONFLICTS)) == 1  # once per head


@pytest.mark.parametrize("reason", ["checks_timed_out", "manual"])
def test_any_other_removal_counts_for_nothing_and_the_pr_is_enqueued_again(
    queue: dict[str, Any], reason: str
) -> None:
    head = queue["opened"](7)
    queue["remove"](7, approvals.FAILED_CHECKS, at=RED_AT, group="g1")
    queue["remove"](7, reason, at=AGAIN_AT)
    code, lines = queue["land"]("7")
    assert code == 0, lines
    assert queue["enqueued"] == [(7, head)] and queue["comments"] == []


def test_a_pr_the_queue_has_just_merged_exits_0_and_is_not_enqueued_again(
    queue: dict[str, Any],
) -> None:
    head = queue["opened"](7)
    queue["remove"](7, approvals.QUEUE_MERGED, at=AGAIN_AT)
    assert queue["land"]("7") == (
        0,
        [f"PR #7 left the merge queue merged at {head}"],
    )
    assert queue["enqueued"] == []


def entry(minutes: int, suites: int = 0, state: str = "AWAITING_CHECKS") -> dict[str, Any]:
    """A queue entry whose group commit was made `minutes` ago."""
    at = datetime.now(UTC) - timedelta(minutes=minutes)
    return {
        "state": state,
        "position": 1,
        "headCommit": {
            "committedDate": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "checkSuites": {"totalCount": suites},
        },
    }


def test_an_entry_awaiting_checks_github_never_started_is_dequeued_and_enqueued_again(
    queue: dict[str, Any],
) -> None:
    head = queue["opened"](7)
    queue["pulls"][7]["mergeQueueEntry"] = entry(11)
    code, lines = queue["land"]("7")
    assert code == 0, lines
    assert (queue["dequeued"], queue["enqueued"]) == ([7], [(7, head)])


@pytest.mark.parametrize(
    ("minutes", "suites", "state"),
    [(9, 0, "AWAITING_CHECKS"), (11, 1, "AWAITING_CHECKS"), (11, 0, "QUEUED")],
)
def test_an_entry_waiting_less_long_with_a_check_suite_or_not_for_checks_is_left_alone(
    queue: dict[str, Any], minutes: int, suites: int, state: str
) -> None:
    queue["opened"](7)
    queue["pulls"][7]["mergeQueueEntry"] = entry(minutes, suites, state)
    assert queue["land"]("7")[0] == 0
    assert (queue["dequeued"], queue["enqueued"]) == ([], [])


SYNC = GITHUB_RULE.replace("```yaml\n", "```yaml\nland:\n  merge_commit_heads:\n    - pr7\n")


@pytest.mark.parametrize(
    ("method", "enqueued"), [("SQUASH", False), ("REBASE", False), ("MERGE", True)]
)
def test_a_merge_commit_head_enters_only_a_queue_that_merges_with_merge_commits(
    queue: dict[str, Any], method: str, enqueued: bool
) -> None:
    (queue["work"] / "docs" / "agents" / "loop.md").write_text(SYNC)
    queue["rules"].queue = method
    queue["opened"](7)
    code, lines = queue["land"]("7")
    assert bool(queue["enqueued"]) is enqueued, lines
    if not enqueued:
        assert code == 1
        assert lines == [
            (
                f"PR #7's head `pr7` matches `land.merge_commit_heads`, but main's merge queue merges every entry with"
                f" {method}, not a merge commit: set the queue's merge method to MERGE"
            )
        ]


def test_a_queue_base_refuses_a_signed_squash(queue: dict[str, Any]) -> None:
    signed = loop_md("land:\n  signed_squash_to: main\n  allowed_signers: /dev/null\n")
    (queue["work"] / "docs" / "agents" / "loop.md").write_text(signed)
    queue["opened"](7)
    code, lines = queue["land"]("7")
    assert code == 1 and "`land.signed_squash_to`" in lines[-1], lines
    assert queue["enqueued"] == []


def test_a_policy_naming_the_github_queue_enqueues_as_the_rules_do(queue: dict[str, Any]) -> None:
    """`land.queue: github` on a base whose rules require the queue changes nothing: the PR is enqueued at its head."""
    (queue["work"] / "docs" / "agents" / "loop.md").write_text(loop_md("land:\n  queue: github\n"))
    head = queue["opened"](7)
    assert queue["land"]("7") == (0, [f"PR #7 enqueued at {head}, position 1"])
    assert (queue["enqueued"], queue["merged"]()) == ([(7, head)], [])


def test_a_policy_naming_the_github_queue_is_refused_on_a_base_without_one(
    queue: dict[str, Any],
) -> None:
    """Rules with no `merge_queue` rule would have land merge the PR itself while the policy claims GitHub's queue."""
    queue["rules"] = approvals.github.Required({"qualify"})
    (queue["work"] / "docs" / "agents" / "loop.md").write_text(loop_md("land:\n  queue: github\n"))
    queue["opened"](7)
    code, lines = queue["land"]("7")
    assert code == 1, lines
    assert "`land.queue: github`" in lines[-1] and "`merge_queue` rule" in lines[-1], lines
    assert (queue["enqueued"], queue["merged"](), queue["tips"](), queue["runs"]()) == (
        [],
        [],
        [],
        0,
    )


def test_required_checks_reports_the_merge_queue_and_its_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ruleset's `merge_queue` rule picks the GitHub merge queue; without one `queue` is None."""
    answers = {"rules": "qualify\n:merge_queue:SQUASH\n", "protection": "lint\n"}

    def gh(*args: str, **_kw: Any) -> str:
        return answers["rules" if args[1] == "--paginate" else "protection"]

    monkeypatch.setattr(approvals.github, "gh", gh)
    monkeypatch.setattr(approvals, "required_checks", REAL_REQUIRED)
    found = approvals.required_checks("main")
    assert (found, found.queue) == ({"qualify", "lint"}, "SQUASH")
    answers["rules"] = "qualify\n"
    approvals.github.forget()  # a new run: one run keeps the rules it read (#1771)
    assert approvals.required_checks("main").queue is None


def test_a_pr_github_reports_conflicting_gets_the_conflict_comment_and_is_not_enqueued(
    queue: dict[str, Any],
) -> None:
    queue["opened"](7)
    queue["pulls"][7]["mergeable"] = "CONFLICTING"
    code, lines = queue["land"]("7")
    assert (code, lines) == (1, ["PR #7 conflicts with main: merge `origin/main` in"])
    assert len(notes_on(queue, 7, approvals.CONFLICTS)) == 1 and queue["enqueued"] == []


def test_a_refused_enqueue_exits_1_and_a_red_is_commented_once_however_often_land_runs(
    queue: dict[str, Any],
) -> None:
    head = queue["opened"](7)
    queue["remove"](7, approvals.FAILED_CHECKS, group="g1")
    red_group(queue, "g1", 410)
    queue["refuse"] = True
    for _ in range(2):
        code, lines = queue["land"]("7")
        assert code == 1, lines
        assert lines[-1] == f"PR #7: GitHub refused to enqueue it at {head}: gh: head moved"
    assert len(notes_on(queue, 7, approvals.QUEUE_RED)) == 1 and queue["enqueued"] == []


def test_a_pr_marked_ready_after_its_eviction_counts_its_reds_anew(queue: dict[str, Any]) -> None:
    head = queue["opened"](7)
    queue["remove"](7, approvals.FAILED_CHECKS, at=RED_AT, group="g1")
    queue["remove"](7, approvals.FAILED_CHECKS, at=AGAIN_AT, group="g2")
    red_group(queue, "g2", 420)
    queue["pulls"][7]["timelineItems"]["nodes"].append(
        {"__typename": "ReadyForReviewEvent", "createdAt": PUSHED_LATER}
    )
    queue["remove"](7, approvals.FAILED_CHECKS, at=NEW_RED_AT, group="g3")
    red_group(queue, "g3", 430)
    code, lines = queue["land"]("7")
    assert code == 0, lines
    assert queue["enqueued"] == [(7, head)] and not queue["pulls"][7]["isDraft"]


def test_a_red_on_main_the_base_does_not_require_is_no_red_main(queue: dict[str, Any]) -> None:
    head = queue["opened"](7)
    queue["remove"](7, approvals.FAILED_CHECKS, group="g1")
    red_group(queue, "g1", 410)
    queue["checks"]["main"] = [run("lint", "failure", 500), run("qualify", "success", 501)]
    assert queue["land"]("7")[0] == 0
    assert queue["enqueued"] == [(7, head)]


def test_a_pr_merged_outside_the_queue_gets_no_landed_comment_from_it(
    queue: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    labelled: list[tuple[int, str, bool]] = []
    monkeypatch.setattr(approvals, "label", lambda *args: labelled.append(args))
    queue["opened"](7)
    queue["pulls"][7]["state"] = "MERGED"
    assert queue["land"]("7") == (0, ["PR #7 is merged"])
    # nor a label call, since it carries no `loop:queued` to take off
    assert queue["comments"] == [] and labelled == []


def test_a_bare_land_exits_1_when_a_candidate_conflicts_and_still_enqueues_the_rest(
    queue: dict[str, Any],
) -> None:
    queue["opened"](7)
    eight = queue["opened"](8)
    queue["pulls"][7]["mergeable"] = "CONFLICTING"
    code, lines = queue["land"]()
    assert code == 1, lines
    assert queue["enqueued"] == [(8, eight)]


LINK = "https://claude.ai/code/session_01AbCdEf"
BODY = "## Summary\n\nFix it.\n\n## Evidence\n\nlog line"


def test_a_pr_body_keeps_its_session_link_when_it_is_enqueued(queue: dict[str, Any]) -> None:
    head = queue["opened"](7, body=f"{BODY}\n\n{LINK}\n")
    code, lines = queue["land"]("7")
    assert code == 0, lines
    assert not queue.get("edits")
    assert queue["enqueued"] == [(7, head)]


def test_a_pr_the_queue_merged_has_its_worker_sessions_finished(
    queue: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    archived: list[int] = []
    monkeypatch.setattr(approvals, "archive_workers", archived.append)
    queue["opened"](7)
    queue["queue_merge"](7)
    assert queue["land"]("7") == (0, ["PR #7 is merged"])
    assert archived == [7] and len(notes_on(queue, 7, approvals.LANDED)) == 1


def test_a_bare_land_comments_landed_on_each_pr_the_queue_merged_without_one(
    queue: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    archived: list[int] = []
    monkeypatch.setattr(approvals, "archive_workers", archived.append)
    for number in (6, 7, 9):
        queue["opened"](number)
    queue["queue_merge"](6)
    queue["queue_merge"](7)
    approvals.comment(7, "Landed\nBase: x\n")  # a `land 7` already wrote its trail
    queue["comments"].clear()
    queue["pulls"][9]["state"] = "MERGED"  # merged outside the queue: not its trail
    code, lines = queue["land"]()
    assert code == 0, lines
    assert [n for n, _ in queue["comments"]] == [6] and archived == [6]
    assert queue["land"]()[0] == 0 and [n for n, _ in queue["comments"]] == [6]


def test_two_reds_on_groups_built_on_a_red_main_evict_nobody_once_main_is_green(
    queue: dict[str, Any],
) -> None:
    head = queue["opened"](7)
    queue["remove"](7, approvals.FAILED_CHECKS, at=RED_AT, group="g1")
    queue["remove"](7, approvals.FAILED_CHECKS, at=AGAIN_AT, group="g2")
    red_group(queue, "g2", 420)
    queue["built"].update(g1="m1", g2="m1")
    queue["checks"]["m1"] = [run("qualify", "failure", 500)]
    queue["checks"]["main"] = queue["checks"]["m1"]
    code, lines = queue["land"]("7")
    assert code == 1 and lines[-1].startswith("main itself is red"), lines
    queue["checks"]["main"] = [run("qualify", "success", 501)]
    code, lines = queue["land"]("7")
    assert code == 0, lines
    assert (
        "PR #7 was red in the merge queue on a red main: it counts for nothing, enqueued again"
        in lines
    )
    assert not queue["pulls"][7]["isDraft"] and queue["comments"] == []
    assert queue["enqueued"] == [(7, head)]


def test_one_red_on_a_group_built_on_a_red_main_gets_no_red_comment_once_main_is_green(
    queue: dict[str, Any],
) -> None:
    queue["opened"](7)
    queue["remove"](7, approvals.FAILED_CHECKS, at=RED_AT, group="g1")
    red_group(queue, "g1", 410)
    queue["built"]["g1"] = "m1"
    queue["checks"]["m1"] = [run("qualify", "failure", 500)]
    code, lines = queue["land"]("7")
    assert code == 0, lines
    assert notes_on(queue, 7, approvals.QUEUE_RED) == []


def test_a_red_under_a_red_main_then_one_under_a_green_main_is_the_prs_first(
    queue: dict[str, Any],
) -> None:
    head = queue["opened"](7)
    queue["remove"](7, approvals.FAILED_CHECKS, at=RED_AT, group="g1")
    queue["remove"](7, approvals.FAILED_CHECKS, at=AGAIN_AT, group="g2")
    red_group(queue, "g2", 420)
    queue["built"].update(g1="m1", g2="m2")
    queue["checks"]["m1"] = [run("qualify", "failure", 500)]
    queue["checks"]["m2"] = [run("qualify", "success", 501)]
    code, lines = queue["land"]("7")
    assert code == 0, lines
    assert not queue["pulls"][7]["isDraft"] and notes_on(queue, 7, approvals.QUEUE_EVICTED) == []
    [note] = notes_on(queue, 7, approvals.QUEUE_RED)
    assert "Group: g2\n" in note and queue["enqueued"] == [(7, head)]


def test_a_red_on_a_group_whose_main_commit_is_still_running_counts_nothing_yet(
    queue: dict[str, Any],
) -> None:
    queue["opened"](7)
    queue["remove"](7, approvals.FAILED_CHECKS, at=RED_AT, group="g1")
    red_group(queue, "g1", 410)
    queue["built"]["g1"] = "m1"
    queue["checks"]["m1"] = [run("qualify", None, 500)]  # type: ignore[arg-type]
    code, lines = queue["land"]("7")
    assert (code, lines) == (approvals.WAITING, ["PR #7: main's checks on m1 are still running"])
    assert queue["comments"] == [] and queue["enqueued"] == []


def test_an_entry_queued_long_whose_group_was_just_built_is_left_alone(
    queue: dict[str, Any],
) -> None:
    """Enqueued 40 minutes ago behind other entries, its group commit made a second ago, before GitHub's check suite."""
    queue["opened"](7)
    queue["pulls"][7]["mergeQueueEntry"] = {**entry(0), "enqueuedAt": "2026-10-02T09:00:00Z"}
    assert queue["land"]("7")[0] == 0
    assert queue["dequeued"] == []


def test_a_conflict_removal_github_now_reports_mergeable_is_enqueued_again(
    queue: dict[str, Any],
) -> None:
    """The queue tests an entry on those ahead of it: the conflict was with one of them, not with main."""
    head = queue["opened"](7)
    queue["remove"](7, approvals.MERGE_CONFLICT)
    code, lines = queue["land"]("7")
    assert code == 0, lines
    assert queue["enqueued"] == [(7, head)] and queue["comments"] == []


def test_a_red_on_a_group_github_cannot_place_on_main_counts(queue: dict[str, Any]) -> None:
    queue["opened"](7)
    queue["remove"](7, approvals.FAILED_CHECKS, at=RED_AT, group="g1")
    red_group(queue, "g1", 410)
    queue["built"]["g1"] = None  # GitHub compares no commit it no longer has
    assert queue["land"]("7")[0] == 0
    assert len(notes_on(queue, 7, approvals.QUEUE_RED)) == 1


def test_a_bare_land_that_cannot_list_the_merged_prs_says_so_and_still_exits_0(
    queue: dict[str, Any],
) -> None:
    queue["opened"](7)
    queue["unlisted"] = True
    code, lines = queue["land"]()
    assert code == 0, lines
    assert lines[-1] == "land could not list the PRs merged into main: gh: timed out"


def test_an_entry_awaiting_checks_on_a_commit_github_names_no_date_for_is_left_alone(
    queue: dict[str, Any],
) -> None:
    queue["opened"](7)
    queue["pulls"][7]["mergeQueueEntry"] = {
        "state": "AWAITING_CHECKS",
        "position": 1,
        "headCommit": None,
    }
    assert queue["land"]("7")[0] == 0
    assert (queue["dequeued"], queue["enqueued"]) == ([], [])


def test_the_prs_own_red_then_a_red_on_a_group_built_on_a_red_main_gets_no_comment_for_mains(
    queue: dict[str, Any],
) -> None:
    """The PR's own red (ga, on a green main commit), then a red on a group built on a red main commit (gb)."""
    head = queue["opened"](7)
    queue["remove"](7, approvals.FAILED_CHECKS, at=RED_AT, group="ga")
    queue["remove"](7, approvals.FAILED_CHECKS, at=AGAIN_AT, group="gb")
    red_group(queue, "ga", 610)
    red_group(queue, "gb", 620)
    queue["built"].update(ga="fine", gb="broken")
    queue["checks"]["fine"] = [run("qualify", "success", 701)]
    queue["checks"]["broken"] = [run("qualify", "failure", 700)]
    code, lines = queue["land"]("7")
    assert code == 0 and not queue["pulls"][7]["isDraft"], lines
    assert [n for n in notes_on(queue, 7, approvals.QUEUE_RED) if "Group: gb\n" in n] == []
    assert queue["enqueued"] == [(7, head)]


@pytest.mark.parametrize("conclusion", ["cancelled", "stale"])
def test_a_run_cancelled_or_stale_on_the_groups_main_commit_is_no_fault_of_main(
    queue: dict[str, Any], conclusion: str
) -> None:
    """Main's push run on the commit was cancelled by a newer push, or went stale, after the queue's run went green."""
    queue["opened"](7)
    queue["remove"](7, approvals.FAILED_CHECKS, at=RED_AT, group="ga")
    queue["remove"](7, approvals.FAILED_CHECKS, at=AGAIN_AT, group="gb")
    red_group(queue, "gb", 620)
    queue["built"].update(ga="older", gb="older")
    queue["checks"]["older"] = [run("qualify", conclusion, 700)]
    code, lines = queue["land"]("7")
    assert code == 1 and queue["pulls"][7]["isDraft"], lines
    assert len(notes_on(queue, 7, approvals.QUEUE_EVICTED)) == 1


def test_a_cancelled_run_on_mains_tip_still_stops_the_land(queue: dict[str, Any]) -> None:
    queue["opened"](7)
    queue["remove"](7, approvals.FAILED_CHECKS, group="g1")
    red_group(queue, "g1", 410)
    queue["checks"]["main"] = [run("qualify", "cancelled", 500)]
    code, lines = queue["land"]("7")
    assert code == 1 and lines[-1].startswith("main itself is red"), lines


def test_a_conflict_removal_while_github_works_out_mergeability_waits_without_a_comment(
    queue: dict[str, Any],
) -> None:
    queue["opened"](8)
    queue["pulls"][8]["mergeable"] = "UNKNOWN"
    queue["remove"](8, approvals.MERGE_CONFLICT)
    code, lines = queue["land"]("8")
    assert (code, lines) == (
        approvals.WAITING,
        ["GitHub is still working out whether PR #8 conflicts with main"],
    )
    assert queue["comments"] == [] and queue["enqueued"] == []


def test_a_failed_trail_of_one_merged_pr_keeps_its_label_and_never_fails_the_bare_land(
    queue: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading the tree of PR 6's queue merge commit fails once (a GitHub 502); the next land writes its trail."""
    queue["opened"](6)
    merge = queue["queue_merge"](6)
    seven = queue["opened"](7)
    fake = approvals.github.gh

    def gh(*args: str, **kw: Any) -> str:
        if args[0] == "api" and args[1].endswith(f"/commits/{merge}"):
            raise subprocess.CalledProcessError(1, "gh", stderr="gh: Bad Gateway (HTTP 502)")
        return fake(*args, **kw)

    monkeypatch.setattr(approvals.github, "gh", gh)
    code, lines = queue["land"]()
    assert code == 0, lines
    assert "PR #6: land could not write its `Landed` comment: gh: Bad Gateway (HTTP 502)" in lines
    assert queue["enqueued"] == [(7, seven)] and approvals.QUEUED in labels_of(queue, 6)
    monkeypatch.setattr(approvals.github, "gh", fake)
    assert queue["land"]()[0] == 0
    assert len(notes_on(queue, 6, approvals.LANDED)) == 1
    assert approvals.QUEUED not in labels_of(queue, 6)


def test_a_merged_pr_with_over_100_comments_never_fails_the_bare_land(
    queue: dict[str, Any],
) -> None:
    queue["opened"](6)
    queue["queue_merge"](6)
    queue["pulls"][6]["comments"]["totalCount"] = 101
    seven = queue["opened"](7)
    code, lines = queue["land"]()
    assert code == 0, lines
    assert queue["enqueued"] == [(7, seven)]
    assert any(
        line.startswith("PR #6: land could not write its `Landed` comment: #6 has more")
        for line in lines
    )


def test_enqueue_labels_the_pr_and_a_bare_land_finds_one_the_queue_merged_long_ago_by_it(
    queue: dict[str, Any],
) -> None:
    """The trail reads the label, not a window of merge dates: a PR merged a week before the next land still gets it."""
    queue["opened"](6)
    assert queue["land"]("6")[0] == 0
    assert approvals.QUEUED in labels_of(queue, 6)
    queue["queue_merge"](6)
    assert queue["land"]()[0] == 0
    [listed] = queue["listed"]
    assert "--search" not in listed and listed[listed.index("--label") + 1] == approvals.QUEUED
    assert len(notes_on(queue, 6, approvals.LANDED)) == 1
    assert approvals.QUEUED not in labels_of(queue, 6)


def test_a_named_land_that_comments_landed_takes_the_label_off(queue: dict[str, Any]) -> None:
    queue["opened"](7)
    queue["queue_merge"](7)
    assert queue["land"]("7") == (0, ["PR #7 is merged"])
    assert len(notes_on(queue, 7, approvals.LANDED)) == 1
    assert approvals.QUEUED not in labels_of(queue, 7)


def test_a_labelled_pr_merged_outside_the_queue_loses_the_label_on_a_bare_land(
    queue: dict[str, Any],
) -> None:
    """PR 6 was enqueued (labelled), evicted, then merged by hand: no `merged` removal, so no `Landed`, label off."""
    queue["opened"](6)
    approvals.label(6, approvals.QUEUED, add=True)
    queue["remove"](6, approvals.FAILED_CHECKS, group="g1")
    queue["pulls"][6]["state"] = "MERGED"
    code, _ = queue["land"]()
    assert code == 0 and notes_on(queue, 6, approvals.LANDED) == []
    assert approvals.QUEUED not in labels_of(queue, 6), (
        "the label stays, so every bare land reads PR 6 again"
    )


def test_a_labelled_pr_that_already_has_landed_loses_the_label(queue: dict[str, Any]) -> None:
    """PR 6's `Landed` was written by another land that read it merged at once, before its label call."""
    queue["opened"](6)
    queue["queue_merge"](6)
    queue["land"]("6")
    approvals.label(6, approvals.QUEUED, add=True)  # the racing land's label never came off
    assert queue["land"]()[0] == 0
    assert len(notes_on(queue, 6, approvals.LANDED)) == 1
    assert approvals.QUEUED not in labels_of(queue, 6)


def test_land_wait_on_an_unknown_mergeability_exits_3_at_once_and_a_later_land_enqueues(
    queue: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`land 8 --wait` after a conflict removal while GitHub reads UNKNOWN: exit 3 without a poll; once it reads
    MERGEABLE, the next `land` enqueues."""
    head = queue["opened"](8)
    queue["pulls"][8]["mergeable"] = "UNKNOWN"
    queue["remove"](8, approvals.MERGE_CONFLICT)
    # a poll sleeps POLL (30 s) or more; subprocess.wait sleeps a few ms on the same time module, which a slow
    # runner reaches while a git push runs (#1892), so only a poll-length sleep fails
    real_sleep = approvals.time.sleep
    monkeypatch.setattr(
        approvals.time,
        "sleep",
        lambda seconds: pytest.fail("polled the queue") if seconds >= 1 else real_sleep(seconds),
    )
    code, lines = queue["land"]("8", "--wait")
    assert code == approvals.WAITING and queue["enqueued"] == [], lines
    queue["pulls"][8]["mergeable"] = "MERGEABLE"
    # with --wait it would wait on, for the merge (test_land_wait_queue_budget.py)
    assert queue["land"]("8")[0] == 0
    assert queue["enqueued"] == [(8, head)]


def test_the_label_sync_keeps_loop_queued_and_gives_one_land_created_its_colour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`loop:queued` is one of the loop's labels: a repo where an enqueue's REST call created it bare gets it updated,
    never listed as foreign and deleted once nothing carries it."""
    from approvals_harness import SCRIPTS

    # the github-tracker skill's labels.py, which reads the gatekeeper's names beside it as installed
    monkeypatch.syspath_prepend(
        str(SCRIPTS.parents[3] / "github" / "skills" / "github-tracker" / "scripts")
    )
    import labels

    monkeypatch.setattr(
        labels, "tracker_settings", lambda _tracker: {}
    )  # no project labels or renames
    monkeypatch.setattr(labels, "renames", lambda _tracker: [])
    existing = {**labels.LOOP_LABELS, approvals.QUEUED: ("ededed", "")}
    actions, unlisted = labels.plan(existing, "")
    assert actions == [("update", approvals.QUEUED, *labels.LOOP_LABELS[approvals.QUEUED])]
    assert unlisted == []
