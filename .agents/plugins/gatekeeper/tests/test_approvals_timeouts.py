"""gatekeeper.md's `timeouts` block: every time limit of a run derived from one place (#2228).

`timeouts.test` is one test's limit, which a project maps onto its runner's own per-test option; `timeouts.run` is one
selection run's, the queue's check included. The queue's checks timeout (the `.mergify.yml` the pack renders), a CI
job's `timeout-minutes` and `land --wait`'s ceiling all follow `run`, so raising it moves them together; with nothing
set they keep today's values (1 hour, 60 minutes, 2 hours). A run over the budget, or a gate that times out, is a
timeout and never a test's failure: no eviction, no red count.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import (
    LOOP,
    add,
    approvals,
    git_in,
    lands,  # noqa: F401
)
from test_approvals_affected_tests import worker  # noqa: F401
from test_approvals_cloud_queue import cloud_queue  # noqa: F401
from test_approvals_github_queue import (  # noqa: F401
    AGAIN_AT,
    RED_AT,
    labels_of,
    notes_on,
    queue,
    run,
)
from test_approvals_mergify_queue import land_statuses, mergify  # noqa: F401
from test_approvals_status_refused import handed, refused  # noqa: F401

TEMPLATE = Path(approvals.__file__).resolve().parent.parent / "mergify.yml"


def timed(settings: str) -> str:
    """The harness's policy with `settings` under `timeouts`."""
    return add(LOOP, "timeouts", settings)


# --- the policy reader


def test_nothing_set_keeps_todays_limits() -> None:
    assert approvals.timeouts(LOOP) == {"test": 60, "run": 3600}
    assert approvals.timeouts(None) == {"test": 60, "run": 3600}


def test_the_reader_takes_both_keys_and_each_alone() -> None:
    assert approvals.timeouts(timed("test: 90s\nrun: 4h15m\n")) == {"test": 90, "run": 15300}
    assert approvals.timeouts(timed("run: 4h\n")) == {"test": 60, "run": 14400}
    assert approvals.timeouts(timed("test: 2m\n")) == {"test": 120, "run": 3600}


@pytest.mark.parametrize("key", ["test", "run"])
@pytest.mark.parametrize("text", ["'soon'", "'4 hours'", "'30m4h'", "'0s'", "'-1h'"])
def test_a_malformed_duration_is_refused_naming_its_key(key: str, text: str) -> None:
    with pytest.raises(approvals.Refused, match=rf"`timeouts\.{key}` is"):
        approvals.settings(timed(f"{key}: {text}\n"))


def test_a_bare_number_and_an_unknown_key_are_refused_naming_the_key() -> None:
    with pytest.raises(approvals.Refused, match=r"timeouts\.run.* must be a string"):
        approvals.settings(timed("run: 60\n"))
    with pytest.raises(approvals.Refused, match="job"):
        approvals.settings(timed("job: 60m\n"))


# --- every other limit follows `timeouts.run`


@pytest.mark.parametrize(
    ("settings", "checks", "minutes", "ceiling"),
    [
        (None, "1 hour", 60, 2 * 3600),
        ("run: 4h\n", "4 hours", 240, 8 * 3600),
        ("run: 4h15m\n", "4 hours 15 minutes", 255, 8 * 3600 + 30 * 60),
        ("run: 90s\n", "1 minute 30 seconds", 2, 180),
    ],
)
def test_the_queue_timeout_the_job_limit_and_the_land_wait_ceiling_follow_the_run(
    settings: str | None, checks: str, minutes: int, ceiling: int
) -> None:
    """The ceiling is the queue's checks timeout plus a margin of one more run: 2 hours with nothing set."""
    loop = LOOP if settings is None else timed(settings)
    rendered = approvals.mergify_rendered(loop)
    assert f"    checks_timeout: {checks}\n" in rendered
    assert approvals.job_minutes(loop) == minutes
    assert approvals.ceiling_of(loop) == ceiling
    # nothing but the one line differs from the template
    template = TEMPLATE.read_text()
    assert rendered == template.replace("checks_timeout: 1 hour", f"checks_timeout: {checks}")


def test_with_nothing_set_the_rendered_file_is_the_template_byte_for_byte() -> None:
    assert approvals.mergify_rendered(LOOP) == TEMPLATE.read_text()


def test_land_wait_waits_until_the_derived_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    for policy, ceiling in ((LOOP, 7200), (timed("run: 4h\n"), 28800)):
        monkeypatch.setattr(approvals, "project", lambda policy=policy: (None, policy))
        assert approvals.land_wait() == ceiling
    # the landing lock's staleness is no project's setting
    assert approvals.CEILING == 2 * 60 * 60


def test_a_land_wait_set_before_the_derivation_still_wins_with_a_deprecation_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(approvals, "DEPRECATED", set())
    loop = add(timed("run: 4h\n"), "land", "wait: 4h30m\n")
    assert approvals.ceiling_of(loop) == 16200
    assert "deprecated: land.wait" in capsys.readouterr().err


# --- the drift check: the repo's `.mergify.yml` against the policy


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git checkout with a gatekeeper.md policy and `.mergify.yml` copied from the template."""
    git_in(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "docs" / "agents").mkdir(parents=True)
    (tmp_path / "docs" / "agents" / "gatekeeper.md").write_text(policy_md(""))
    (tmp_path / ".mergify.yml").write_text(TEMPLATE.read_text())
    monkeypatch.chdir(tmp_path)
    return tmp_path


def policy_md(timeouts: str) -> str:
    return f"# Gatekeeper\n\n## Policy\n\n```yaml\nspec:\n  auto: true\n{timeouts}```\n"


def verb(*args: str) -> tuple[int, str]:
    found = subprocess.run(
        [sys.executable, approvals.__file__, "timeouts", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return found.returncode, found.stdout + found.stderr


def test_the_check_passes_in_step_and_fails_once_the_run_moves(checkout: Path) -> None:
    assert verb("--check")[0] == 0
    (checkout / "docs" / "agents" / "gatekeeper.md").write_text(policy_md("timeouts:\n  run: 4h\n"))
    code, out = verb("--check")
    assert code == 1
    assert "`checks_timeout` is '1 hour', but the policy's `timeouts.run` makes it '4 hours'" in out
    # rendered from the policy, the file is in step again
    code, rendered = verb("--mergify")
    assert code == 0 and "checks_timeout: 4 hours\n" in rendered
    (checkout / ".mergify.yml").write_text(
        approvals.mergify_rendered(None).replace("1 hour", "4 hours")
    )
    assert verb("--check")[0] == 0


def test_the_check_names_drift_beyond_the_timeout(checkout: Path) -> None:
    (checkout / ".mergify.yml").write_text(
        TEMPLATE.read_text().replace("batch_size: 3", "batch_size: 5")
    )
    code, out = verb("--check")
    assert code == 1 and "differs from this skill's mergify.yml beyond `checks_timeout`" in out


def test_check_setup_names_a_checks_timeout_out_of_step(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (checkout / "docs" / "agents" / "gatekeeper.md").write_text(policy_md("timeouts:\n  run: 4h\n"))
    monkeypatch.setattr(approvals, "toplevel", lambda: checkout)
    monkeypatch.setattr(approvals, "project", lambda **_: (None, None))
    monkeypatch.setattr(approvals, "setup_problems", lambda root: [])
    assert approvals.setup() == 1
    assert "`checks_timeout` is '1 hour'" in capsys.readouterr().out


def test_check_setup_reads_only_the_timeout_line(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A consumer's other edits of .mergify.yml are no `check setup` problem; `timeouts --check` reads the whole file."""
    (checkout / ".mergify.yml").write_text(
        TEMPLATE.read_text().replace("batch_size: 3", "batch_size: 5")
    )
    monkeypatch.setattr(approvals, "toplevel", lambda: checkout)
    monkeypatch.setattr(approvals, "project", lambda **_: (None, None))
    monkeypatch.setattr(approvals, "setup_problems", lambda root: [])
    assert approvals.setup() == 0
    assert "mergify" not in capsys.readouterr().out.lower()


def test_the_verb_labels_a_land_wait_override_as_such(checkout: Path) -> None:
    gatekeeper = checkout / "docs" / "agents" / "gatekeeper.md"
    gatekeeper.write_text(policy_md("timeouts:\n  run: 4h\n"))
    assert "land --wait: 8 hours (the queue's checks timeout plus one more run)" in verb()[1]
    gatekeeper.write_text(policy_md("timeouts:\n  run: 4h\nland:\n  wait: 3h\n"))
    assert "land --wait: 3 hours (land.wait, deprecated)" in verb()[1]


def test_the_verb_prints_every_derived_limit(checkout: Path) -> None:
    (checkout / "docs" / "agents" / "gatekeeper.md").write_text(policy_md("timeouts:\n  run: 4h\n"))
    code, out = verb("--json")
    assert code == 0
    assert out.strip().splitlines()[-1] == (
        '{"test": 60, "run": 14400, "checks_timeout": "4 hours", "job_timeout_minutes": 240, "land_wait": 28800}'
    )


# --- a run over `timeouts.run`


def test_a_step_over_its_budget_is_killed_with_its_group_and_reads_as_a_timeout(
    tmp_path: Path,
) -> None:
    child = tmp_path / "child.pid"
    began = time.monotonic()
    code, output = approvals.run_heavy(
        ["sh", "-c", f"sleep 30 & echo $! > {child}; wait"], budget=0.5
    )
    assert (code, output) == (approvals.TIMED_OUT, approvals.SPENT)
    assert time.monotonic() - began < 10
    pid = int(child.read_text())
    assert not alive(pid)


def alive(pid: int) -> bool:
    """Whether `pid` still runs: gone, or a zombie no init reaped yet, is dead."""
    for _ in range(50):
        stat = Path(f"/proc/{pid}/stat")
        try:
            state = stat.read_text().rsplit(")", 1)[1].split()[0]
        except FileNotFoundError, ProcessLookupError:
            return False
        if state == "Z":
            return False
        time.sleep(0.1)
    return True


def test_a_command_exiting_124_itself_is_no_timeout() -> None:
    assert approvals.run_heavy(["sh", "-c", "exit 124"], budget=30) == (124, None)


def test_affected_tests_over_the_budget_says_so_in_one_line(
    worker: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    slow = worker["repo"].parent / "tools" / "slow.py"
    slow.write_text("import time\ntime.sleep(30)\n")
    worker["set_loop"](f"# Loop\n\nAffected tests: {sys.executable} {slow}\n")
    worker["commit"]("app.py", "value = 2\n")
    worker["commit"]("lib.py", "value = 3\n")  # with loop.md and app.py: 3 files
    monkeypatch.setitem(approvals.TIMEOUTS, "run", "1s")
    assert approvals.main(["affected-tests"]) == approvals.TIMED_OUT
    [line] = [each for each in capsys.readouterr().err.splitlines() if "timed out" in each]
    # the verb can't count what the project's selector picks: it says so, and gives the changed files
    assert line.startswith(
        "affected-tests: timed out after `timeouts.run` (1 second) running a selection of unknown size"
        " (3 changed files against "
    )
    assert "unsharded (no `select.shards`)" in line
    assert "a timeout, not a test failure" in line


def test_a_full_run_over_the_budget_names_the_whole_suite(
    worker: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`Gate: full` runs the whole suite with no base: the line says so."""
    mise = worker["repo"].parent / "tools" / "mise"
    mise.write_text("#!/bin/sh\nsleep 30\n")
    worker["set_loop"](f"# Loop\n\nAffected tests: {sys.executable} x.py\nGate: full\n")
    worker["commit"]("app.py", "value = 2\n")
    monkeypatch.setitem(approvals.TIMEOUTS, "run", "1s")
    assert approvals.main(["affected-tests"]) == approvals.TIMED_OUT
    [line] = [each for each in capsys.readouterr().err.splitlines() if "timed out" in each]
    assert "running the whole suite (`Gate: full`), unsharded" in line


# --- a queue's timeout is no red


def test_a_github_queue_group_red_only_by_a_timeout_counts_for_nothing(
    queue: dict[str, Any],
) -> None:
    head = queue["opened"](7)
    queue["remove"](7, approvals.FAILED_CHECKS, at=RED_AT, group="g1")
    queue["checks"]["g1"] = [run("qualify", "timed_out", 410)]
    code, lines = queue["land"]("7")
    assert code == 0, lines
    assert queue["enqueued"] == [(7, head)]
    assert notes_on(queue, 7, approvals.QUEUE_RED) == []
    assert not queue["pulls"][7]["isDraft"]


def test_a_github_queue_group_with_a_real_failure_beside_a_timeout_still_counts(
    queue: dict[str, Any],
) -> None:
    queue["opened"](7)
    queue["remove"](7, approvals.FAILED_CHECKS, at=RED_AT, group="g1")
    queue["checks"]["g1"] = [run("qualify", "failure", 410)]
    queue["logs"][410] = ["boom"]
    queue["land"]("7")
    assert len(notes_on(queue, 7, approvals.QUEUE_RED)) == 1


def test_a_github_queue_head_timing_out_twice_stops_requeueing_and_names_the_budget(
    queue: dict[str, Any],
) -> None:
    head = queue["opened"](7)
    queue["checks"]["g1"] = [run("qualify", "timed_out", 410)]
    queue["checks"]["g2"] = [run("qualify", "timed_out", 420)]
    queue["remove"](7, approvals.FAILED_CHECKS, at=RED_AT, group="g1")
    code, lines = queue["land"]("7")
    assert code == 0, lines
    assert queue["enqueued"] == [(7, head)]
    queue["remove"](7, approvals.FAILED_CHECKS, at=AGAIN_AT, group="g2")
    code, lines = queue["land"]("7")
    assert code == approvals.WAITING, lines
    assert queue["enqueued"] == [(7, head)]
    [line] = [each for each in lines if "timed out twice" in each]
    assert "`timeouts.run` (1 hour)" in line
    assert not queue["pulls"][7]["isDraft"]


MERGIFY_WORDINGS = [
    {"summary": "The checks have timed out after 1 hour."},
    # Mergify's own wording (its changelog, 2026-06-11)
    {"summary": "the checks did not pass within the checks timeout of 45 minutes"},
    {"title": "Checks timeout"},
]


def dequeued_with(state: dict[str, Any], head: str, *runs: dict[str, Any]) -> None:
    """Mergify dequeued PR #7 at `head`, labelling it, with `runs` as the head's check runs."""
    state["pulls"][7]["labels"]["nodes"].append({"name": "loop:dequeued"})
    state["checks"][head] = [run("qualify", "success", 550), *runs]


def queue_check(conclusion: str, **output: str) -> dict[str, Any]:
    found = run("Mergify Merge Queue", conclusion, 551)
    found["output"] = {
        "title": "The pull request has been removed from the queue `default`",
        **output,
    }
    return found


def requeues(state: dict[str, Any]) -> list[str]:
    return [
        body for n, body in state.get("raw", []) if n == 7 and body.startswith(approvals.REQUEUE)
    ]


@pytest.mark.parametrize("output", MERGIFY_WORDINGS)
def test_a_mergify_dequeue_on_a_timeout_queues_the_pr_again_and_evicts_no_one(
    mergify: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    output: dict[str, str],
) -> None:
    head = mergify["opened"](7)
    assert mergify["land"]("7")[0] == 0
    dequeued_with(mergify, head, queue_check("failure", **output))
    code, lines = mergify["land"]("7")
    assert code == 0, lines
    assert any("on a timeout" in line and "not evicted" in line for line in lines), lines
    assert land_statuses(hub, head)[0] == ("success", f"Queued for Mergify at {head}")
    assert labels_of(mergify, 7) & {"loop:queued", "loop:dequeued"} == {"loop:queued"}
    assert mergify["pulls"][7]["isDraft"] is False
    assert notes_on(mergify, 7, "Evicted from the merge queue") == []
    assert len(requeues(mergify)) == 1


@pytest.mark.parametrize(
    "runs",
    [
        # the queue's own check is green or still running: its text decides nothing
        [queue_check("success", summary="The checks have timed out")],
        [queue_check("", summary="The checks have timed out")],
        # a red test job whose title says timeout is the PR's red, not the queue's timeout
        [
            queue_check("failure"),
            {**run("test (timeout tests)", "failure", 552), "output": {"title": "timeout"}},
        ],
    ],
    ids=["green-queue-check", "running-queue-check", "red-test-job-titled-timeout"],
)
def test_a_mergify_dequeue_that_is_no_queue_timeout_is_evicted(
    mergify: dict[str, Any], runs: list[dict[str, Any]]
) -> None:
    head = mergify["opened"](7)
    assert mergify["land"]("7")[0] == 0
    dequeued_with(mergify, head, *runs)
    code, lines = mergify["land"]("7")
    assert code == 1, lines
    assert f"PR #7 was dequeued by Mergify at {head}: evicted" in lines
    assert requeues(mergify) == []


def test_a_stale_dequeued_label_with_timeout_text_only_comes_off(mergify: dict[str, Any]) -> None:
    """A label at a head land never queued is stale, timeout text or not: no queue command is posted."""
    head = mergify["opened"](7)
    dequeued_with(mergify, head, queue_check("failure", summary="The checks have timed out"))
    code, lines = mergify["land"]("7")
    assert code == 0, lines
    assert "loop:dequeued" not in labels_of(mergify, 7)
    assert lines[-1] == f"PR #7 queued for Mergify at {head}"
    assert requeues(mergify) == []


def test_a_refused_queue_command_leaves_the_dequeued_label_for_the_next_land(
    mergify: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    head = mergify["opened"](7)
    assert mergify["land"]("7")[0] == 0
    dequeued_with(mergify, head, queue_check("failure", summary="The checks have timed out"))

    def refused(number: int, body: str) -> None:
        raise subprocess.CalledProcessError(1, ["gh"], "", "HTTP 502")

    monkeypatch.setattr(approvals, "comment", refused)
    code, lines = mergify["land"]("7")
    assert code == 1, lines
    assert "loop:dequeued" in labels_of(mergify, 7)


@pytest.mark.parametrize(
    ("conclusions", "only"),
    [
        ({"qualify": "TIMED_OUT", "e2e": "SUCCESS"}, True),
        ({"qualify": "FAILURE", "e2e": "TIMED_OUT"}, False),
        ({"qualify": "FAILURE", "e2e": "SUCCESS"}, False),
        ({"qualify": "SUCCESS", "e2e": "SUCCESS"}, False),
    ],
)
def test_a_group_is_timeout_only_when_every_failed_required_check_timed_out(
    monkeypatch: pytest.MonkeyPatch, conclusions: dict[str, str], only: bool
) -> None:
    """A real failure beside a timeout still counts against the PR."""
    checks = {name: {"conclusion": done} for name, done in conclusions.items()}
    monkeypatch.setattr(approvals, "commit_checks", lambda ref: checks)
    assert approvals.timed_out_only("g1", {"qualify", "e2e"}) is only


def test_a_group_github_cannot_read_is_no_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unreadable, the group's red still counts against the PR."""

    def unreadable(ref: str) -> dict[str, Any]:
        raise subprocess.CalledProcessError(1, ["gh"], "", "HTTP 502")

    monkeypatch.setattr(approvals, "commit_checks", unreadable)
    assert approvals.timed_out_only("g1", {"qualify"}) is False


def test_a_mergify_head_timing_out_twice_stays_out_of_the_queue_and_names_the_budget(
    mergify: dict[str, Any],
) -> None:
    head = mergify["opened"](7)
    assert mergify["land"]("7")[0] == 0
    dequeued_with(mergify, head, queue_check("failure", summary="The checks have timed out"))
    assert mergify["land"]("7")[0] == 0
    assert len(requeues(mergify)) == 1
    # Mergify takes it in again and times out a second time at the same head
    assert mergify["land"]("7")[0] == 0
    mergify["pulls"][7]["labels"]["nodes"].append({"name": "loop:dequeued"})
    code, lines = mergify["land"]("7")
    assert code == approvals.WAITING, lines
    [line] = [each for each in lines if "timed out twice" in each]
    assert "`timeouts.run` (1 hour)" in line
    assert len(requeues(mergify)) == 1
    assert labels_of(mergify, 7) & {"loop:queued", "loop:dequeued"} == set()
    assert notes_on(mergify, 7, "Evicted from the merge queue") == []
    # the next land holds it out
    code, lines = mergify["land"]("7")
    assert code == approvals.WAITING, lines
    assert any("timed out twice" in each for each in lines)
    assert len(requeues(mergify)) == 1


def test_a_refused_requeue_after_a_first_timeout_is_no_second_timeout(
    mergify: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    head = mergify["opened"](7)
    assert mergify["land"]("7")[0] == 0
    dequeued_with(mergify, head, queue_check("failure", summary="The checks have timed out"))
    posted = approvals.comment

    def refuse_requeue(number: int, body: str) -> Any:
        if body.startswith(approvals.REQUEUE):
            raise subprocess.CalledProcessError(1, ["gh"], "", "HTTP 502")
        return posted(number, body)

    with monkeypatch.context() as patch:
        patch.setattr(approvals, "comment", refuse_requeue)
        assert mergify["land"]("7")[0] == 1
    code, lines = mergify["land"]("7")
    assert code == 0, lines
    assert not any("timed out twice" in each for each in lines)
    assert len(requeues(mergify)) == 1


def test_a_raised_budget_gives_the_head_a_fresh_first_timeout(
    mergify: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    head = mergify["opened"](7)
    assert mergify["land"]("7")[0] == 0
    dequeued_with(mergify, head, queue_check("failure", summary="The checks have timed out"))
    assert mergify["land"]("7")[0] == 0
    mergify["pulls"][7]["labels"]["nodes"].append({"name": "loop:dequeued"})
    assert mergify["land"]("7")[0] == approvals.WAITING
    stamp_stop_later(mergify)
    monkeypatch.setattr(approvals, "timeouts", lambda loop: {"test": 60, "run": 7200})
    code, lines = mergify["land"]("7")
    assert code == 0, lines
    assert not any("timed out twice" in each for each in lines)
    # the head goes back into Mergify's queue: its queue action does not re-embark a dequeued head by itself
    assert len(requeues(mergify)) == 2


def test_a_refused_label_removal_after_a_first_timeout_is_no_second_timeout(
    mergify: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    head = mergify["opened"](7)
    assert mergify["land"]("7")[0] == 0
    dequeued_with(mergify, head, queue_check("failure", summary="The checks have timed out"))
    with monkeypatch.context() as patch:

        def refused(number: int, name: str, add: bool) -> None:
            raise subprocess.CalledProcessError(1, ["gh"], "", "HTTP 502")

        patch.setattr(approvals, "label", refused)
        assert mergify["land"]("7")[0] == 1
    code, lines = mergify["land"]("7")
    assert code == 0, lines
    assert not any("timed out twice" in each for each in lines)


def stamp_stop_later(state: dict[str, Any]) -> None:
    """The fake stamps every comment alike: the stop record follows the comments it answers."""
    for node in state["pulls"][7]["comments"]["nodes"]:
        if node["body"].startswith(approvals.TIMED_OUT_TWICE):
            node["createdAt"] = "2026-10-02T13:00:00Z"


def mark_ready_again(state: dict[str, Any], at: str) -> None:
    state["pulls"][7]["timelineItems"]["nodes"].append(
        {"__typename": "ReadyForReviewEvent", "createdAt": at}
    )


def test_a_mergify_pr_marked_ready_again_counts_its_timeouts_anew(
    mergify: dict[str, Any],
) -> None:
    head = mergify["opened"](7)
    assert mergify["land"]("7")[0] == 0
    dequeued_with(mergify, head, queue_check("failure", summary="The checks have timed out"))
    assert mergify["land"]("7")[0] == 0
    mergify["pulls"][7]["labels"]["nodes"].append({"name": "loop:dequeued"})
    assert mergify["land"]("7")[0] == approvals.WAITING
    stamp_stop_later(mergify)
    mark_ready_again(mergify, "2999-01-01T00:00:00Z")
    code, lines = mergify["land"]("7")
    assert code == 0, lines
    assert not any("timed out twice" in each for each in lines)
    assert len(requeues(mergify)) == 2


def stopped_github_queue(queue: dict[str, Any]) -> str:
    head = queue["opened"](7)
    queue["checks"]["g1"] = [run("qualify", "timed_out", 410)]
    queue["checks"]["g2"] = [run("qualify", "timed_out", 420)]
    queue["remove"](7, approvals.FAILED_CHECKS, at=RED_AT, group="g1")
    queue["remove"](7, approvals.FAILED_CHECKS, at=AGAIN_AT, group="g2")
    assert queue["land"]("7")[0] == approvals.WAITING
    assert len(notes_on(queue, 7, approvals.TIMED_OUT_TWICE)) == 1
    # the fake stamps every comment alike: the stop record follows the removals it answers
    for node in queue["pulls"][7]["comments"]["nodes"]:
        if node["body"].startswith(approvals.TIMED_OUT_TWICE):
            node["createdAt"] = "2026-10-02T13:00:00Z"
    return head


def test_a_github_queue_stop_is_recorded_once_and_holds_on_later_lands(
    queue: dict[str, Any],
) -> None:
    stopped_github_queue(queue)
    assert queue["land"]("7")[0] == approvals.WAITING
    assert len(notes_on(queue, 7, approvals.TIMED_OUT_TWICE)) == 1
    assert queue["enqueued"] == []


def test_a_github_queue_pr_marked_ready_again_counts_its_timeouts_anew(
    queue: dict[str, Any],
) -> None:
    head = stopped_github_queue(queue)
    mark_ready_again(queue, "2999-01-01T00:00:00Z")
    code, lines = queue["land"]("7")
    assert code == 0, lines
    assert queue["enqueued"] == [(7, head)]


def test_a_raised_budget_lifts_a_github_queue_stop(
    queue: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    head = stopped_github_queue(queue)
    monkeypatch.setattr(approvals, "timeouts", lambda loop: {"test": 60, "run": 7200})
    code, lines = queue["land"]("7")
    assert code == 0, lines
    assert queue["enqueued"] == [(7, head)]


def test_a_dequeue_whose_timeout_text_is_older_than_the_first_record_is_evicted(
    mergify: dict[str, Any],
) -> None:
    head = mergify["opened"](7)
    assert mergify["land"]("7")[0] == 0
    dequeued_with(mergify, head, queue_check("failure", summary="The checks have timed out"))
    assert mergify["land"]("7")[0] == 0
    # Mergify dequeues again for another reason and leaves its old check run, a timeout's, as it was
    for each in mergify["checks"][head]:
        each["completed_at"] = "2026-09-01T00:00:00Z"
    mergify["pulls"][7]["labels"]["nodes"].append({"name": "loop:dequeued"})
    code, lines = mergify["land"]("7")
    assert code == 1, lines
    assert not any("timed out twice" in each for each in lines)


def test_an_old_timed_out_check_after_a_ready_again_is_no_new_first_timeout(
    mergify: dict[str, Any],
) -> None:
    head = mergify["opened"](7)
    assert mergify["land"]("7")[0] == 0
    dequeued_with(mergify, head, queue_check("failure", summary="The checks have timed out"))
    assert mergify["land"]("7")[0] == 0
    for each in mergify["checks"][head]:
        each["completed_at"] = "2026-09-01T00:00:00Z"
    mark_ready_again(mergify, "2999-01-01T00:00:00Z")
    mergify["pulls"][7]["labels"]["nodes"].append({"name": "loop:dequeued"})
    code, lines = mergify["land"]("7")
    assert code == 1, lines
    assert len(requeues(mergify)) == 1


def test_a_lifted_stop_in_a_cloud_session_hands_over_again_and_a_red_dequeue_evicts(
    refused: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    """The `land` status write is refused: the hand-over record is what the land-status workflow answers. The stop ends
    that hand-over as an eviction does, so the re-land after a ready-again posts a new record, and the head is then
    queued here, so Mergify's next dequeue is read, not skipped."""
    head = refused["opened"](7)
    assert refused["land"]("7")[0] == 0

    def workflow_posts(at: str) -> None:
        hub[head] = [
            {
                "context": "land",
                "state": "success",
                "description": f"Queued for Mergify at {head}",
                "created_at": at,
            }
        ]

    workflow_posts("2026-10-01T09:30:00Z")
    dequeued_with(refused, head, queue_check("failure", summary="The checks have timed out"))
    assert refused["land"]("7")[0] == 0
    refused["pulls"][7]["labels"]["nodes"].append({"name": "loop:dequeued"})
    assert refused["land"]("7")[0] == approvals.WAITING
    stamp_stop_later(refused)
    mark_ready_again(refused, "2026-10-02T14:00:00Z")
    code, lines = refused["land"]("7")
    assert code == 0, lines
    assert len(handed(refused, 7)) == 2
    workflow_posts("2026-10-03T00:00:00Z")
    dequeued_with(refused, head, queue_check("failure", title="Merge queue", summary="red"))
    refused["pulls"][7]["labels"]["nodes"].append({"name": "loop:dequeued"})
    code, lines = refused["land"]("7")
    assert code == 1, lines
    assert any("evicted" in each for each in lines), lines
