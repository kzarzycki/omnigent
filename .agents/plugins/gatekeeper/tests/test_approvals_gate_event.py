"""approvals.py: the gate emits the `gate` event (events.md) on the branch it gates: `waiting` while it waits for a slot
or runs, rewritten at each beat, then `ok` with the record's path, or `failed` with the detached run's log when there is one (#1379)."""

from __future__ import annotations

import fcntl
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import approvals, gatekeeper_md
from test_approvals_gate_steps import git_in, steps  # noqa: F401


@pytest.fixture
def emitted(
    steps: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, int, str, str, float]]:
    """The checkout of `steps` with a bare origin, and each gate event written, as (state, seq, url, branch, time); the
    events are the real ones, written to the checkout's refs and pushed to origin."""
    bare = tmp_path / "origin.git"
    git_in(tmp_path, "init", "-q", "--bare", str(bare))
    git_in(steps["repo"], "remote", "add", "origin", str(bare))
    monkeypatch.setenv("GATE_SLOTS", "1")
    monkeypatch.setattr(approvals, "SLOT_SAY_SECONDS", 0.2)
    found: list[tuple[str, int, str, str, float]] = []
    real = approvals.loop_events.emit

    def emit(kind: str, branch: str, state: str, **kwargs: Any) -> str:
        commit = real(kind, branch, state, **kwargs)
        data = json.loads(git_in(steps["repo"], "cat-file", "-p", f"{commit}:event.json"))
        found.append((state, data["seq"], kwargs.get("url", ""), branch, time.monotonic()))
        return commit

    monkeypatch.setattr(approvals.loop_events, "emit", emit)
    return found


def gate(steps: dict[str, Any], script: str) -> str:
    command = f"{sys.executable} -c {script!r}"
    steps["set_policy"](gatekeeper_md(f"loop:\n  gate: {command}\n"))
    return approvals.gate()[0]  # the command as the gate states it, which names the record


def branch_of(steps: dict[str, Any]) -> str:
    return git_in(steps["repo"], "symbolic-ref", "--short", "HEAD").strip()


def on_origin(steps: dict[str, Any]) -> str:
    return git_in(steps["repo"], "ls-remote", "origin", "refs/loop/gate/*")


def test_gate_success_event(
    steps: dict[str, Any], emitted: list[tuple[str, int, str, str, float]]
) -> None:
    command = gate(steps, "pass")
    assert approvals.main(["local-ci", "--record"]) == 0
    record = approvals.recorded(steps["tree"], command)
    assert record.exists()
    assert (emitted[-1][0], emitted[-1][2]) == ("ok", str(record))
    assert {state for state, *_ in emitted[:-1]} == {"waiting"}
    assert f"refs/loop/gate/{branch_of(steps)}" in on_origin(steps)


def test_gate_failure_event(
    steps: dict[str, Any], emitted: list[tuple[str, int, str, str, float]]
) -> None:
    command = gate(steps, "raise SystemExit(3)")
    assert approvals.main(["local-ci", "--record"]) == 1
    # a failed step records nothing, and with no detached run there is no log: the url is empty
    assert (emitted[-1][0], emitted[-1][2]) == ("failed", "")
    assert not approvals.recorded(steps["tree"], command).exists()
    assert f"refs/loop/gate/{branch_of(steps)}" in on_origin(steps)


def test_gate_waiting_beats(
    steps: dict[str, Any], emitted: list[tuple[str, int, str, str, float]]
) -> None:
    """A second gate on a one-slot host: the slot's holder keeps it for a while, and the gate's `waiting` event is
    rewritten at each beat (its seq rises) until the holder lets go, when the gate runs and ends `ok`."""
    gate(steps, "pass")
    path = Path(approvals.slot_path(1))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    released: list[float] = []

    def release() -> None:
        # let go once three beats are seen, with a deadline generous enough for a loaded host
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and sum(s == "waiting" for s, *_ in list(emitted)) < 3:
            time.sleep(0.05)
        released.append(time.monotonic())
        os.close(fd)

    holder = threading.Thread(target=release)
    holder.start()
    code: list[int] = []
    run = threading.Thread(target=lambda: code.append(approvals.main(["local-ci", "--record"])))
    run.start()
    run.join(180)
    holder.join(10)
    assert code == [0]
    while_held = [
        seq for state, seq, _, _, at in emitted if state == "waiting" and at < released[0]
    ]
    assert len(while_held) >= 3
    assert while_held == sorted(set(while_held)) and while_held[0] == 1
    assert emitted[-1][0] == "ok"
    assert [seq for _, seq, *_ in emitted] == list(range(1, len(emitted) + 1))


def test_a_running_step_beats_every_interval(
    steps: dict[str, Any], emitted: list[tuple[str, int, str, str, float]]
) -> None:
    """With the slot free the event still beats while the step runs, so a long step is not read as silent."""
    gate(steps, "import time; time.sleep(1)")
    assert approvals.main(["local-ci", "--record"]) == 0
    assert [state for state, *_ in emitted].count("waiting") >= 3
    assert emitted[-1][0] == "ok"


def test_a_failed_step_names_the_detached_runs_log_when_there_is_one(
    steps: dict[str, Any], emitted: list[tuple[str, int, str, str, float]]
) -> None:
    gate(steps, "raise SystemExit(3)")
    log = approvals.run_files(steps["head"])[0]
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("output\n")
    assert approvals.main(["local-ci", "--record"]) == 1
    assert (emitted[-1][0], emitted[-1][2]) == ("failed", str(log))


def test_a_run_refused_before_its_step_ends_ends_failed(
    steps: dict[str, Any], emitted: list[tuple[str, int, str, str, float]]
) -> None:
    """A gate command that cannot run is a refusal, not an exit: the event must not be left `waiting`."""
    steps["set_policy"](gatekeeper_md("loop:\n  gate: /nonexistent/gate-command\n"))
    assert approvals.main(["local-ci", "--record"]) == 1
    assert (emitted[-1][0], emitted[-1][2]) == ("failed", "")


def test_a_reused_pass_ends_ok_for_the_checkout_and_for_a_pr(
    steps: dict[str, Any],
    emitted: list[tuple[str, int, str, str, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(approvals, "comment", lambda *_a: None)
    command = gate(steps, "pass")
    assert approvals.main(["local-ci", "--record"]) == 0
    emitted.clear()
    record = str(approvals.recorded(steps["tree"], command))
    for argv in (["local-ci", "--record"], ["local-ci", "7"]):
        assert approvals.main(argv) == 0
        assert [(state, url) for state, _, url, *_ in emitted] == [("ok", record)]
        emitted.clear()


def test_a_step_left_for_a_host_ends_no_event_with_ok(
    steps: dict[str, Any],
    emitted: list[tuple[str, int, str, str, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    assert approvals.main(["local-ci", "--record"]) == 0
    assert steps["ran"]() == ["lint", "unit"]
    assert emitted and {state for state, *_ in emitted} == {"waiting"}


def test_an_event_that_cannot_be_written_leaves_the_gate_exit(
    steps: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_a: Any, **_k: Any) -> str:
        raise subprocess.CalledProcessError(128, "git", stderr="fatal: not a git repository")

    monkeypatch.setattr(approvals.loop_events, "emit", broken)
    gate(steps, "pass")
    assert approvals.main(["local-ci", "--record"]) == 0
    gate(steps, "raise SystemExit(3)")
    assert approvals.main(["local-ci", "--record"]) == 1


def test_a_gate_on_a_new_head_is_not_read_as_the_previous_heads_end(
    steps: dict[str, Any],
    emitted: list[tuple[str, int, str, str, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a pass on one head, the detaching `local-ci` on the next says `waiting` before it starts the run, so a
    `wait` that follows it does not return the old `ok`."""
    gate(steps, "pass")
    assert approvals.main(["local-ci", "--record"]) == 0
    assert emitted[-1][0] == "ok"
    (steps["repo"] / "app.py").write_text("value = 2\n")
    git_in(steps["repo"], "commit", "-qam", "next")
    # a run that leaves a host step (it wrote no end, and says so): the event is the parent's alone
    head = git_in(steps["repo"], "rev-parse", "HEAD").strip()

    def left(*_a: Any) -> int:
        approvals.ended_file(head).write_text("left\n")
        return 0

    monkeypatch.setattr(approvals, "follow", left)
    assert approvals.detach(["--help"]) == 0
    assert emitted[-1][0] == "waiting"
    out = io.StringIO()
    code = approvals.loop_events.wait(
        "gate", branch_of(steps), timeout=1, poll=0.1, cwd=steps["repo"], out=out
    )
    assert code != 0 and "ok" not in out.getvalue()


def test_a_gate_steps_policy_ends_ok_with_the_records_directory(
    steps: dict[str, Any], emitted: list[tuple[str, int, str, str, float]]
) -> None:
    assert approvals.main(["local-ci", "--record"]) == 0
    assert (emitted[-1][0], emitted[-1][2]) == ("ok", str(approvals.local_ci_dir()))


def test_a_detached_head_writes_no_event(
    steps: dict[str, Any], emitted: list[tuple[str, int, str, str, float]]
) -> None:
    git_in(steps["repo"], "checkout", "-q", "--detach")
    assert approvals.main(["local-ci", "--record"]) == 0
    assert emitted == []
    assert git_in(steps["repo"], "for-each-ref", "refs/loop/gate/") == ""


def test_the_events_unpushed_warning_stays_off_the_gates_stderr(
    steps: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git_in(steps["repo"], "remote", "add", "origin", str(tmp_path / "nowhere.git"))
    gate(steps, "pass")
    assert approvals.main(["local-ci", "--record"]) == 0
    assert "not pushed" not in capsys.readouterr().err


def ended_by_detach(
    steps: dict[str, Any], argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> tuple[int, str]:
    """The exit of a real detached `local-ci <argv>` on the checkout, and what a `wait` then reports for its branch."""
    monkeypatch.chdir(steps["repo"])
    code = approvals.detach(argv)
    out = io.StringIO()
    waited = approvals.loop_events.wait(
        "gate", branch_of(steps), timeout=1, poll=0.1, cwd=steps["repo"], out=out
    )
    return code, f"{waited}: {out.getvalue()}"


def test_a_detached_run_refused_before_the_gate_ends_failed_with_the_log(
    steps: dict[str, Any],
    emitted: list[tuple[str, int, str, str, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`local-ci <pr>` on a head with no pass refuses in the child before any gate step: the event ends `failed`."""
    gate(steps, "pass")
    monkeypatch.setattr(
        approvals, "follow", lambda *_a: 1
    )  # a child that wrote nothing and exited 1
    code, waited = ended_by_detach(steps, ["--help"], monkeypatch)
    assert code == 1
    log = approvals.run_files(git_in(steps["repo"], "rev-parse", "HEAD").strip())[0]
    assert [(state, url) for state, _, url, *_ in emitted][-1] == ("failed", str(log))
    assert not waited.startswith("3")


def test_a_detached_record_on_a_dirty_tree_ends_ok_with_no_url(
    steps: dict[str, Any],
    emitted: list[tuple[str, int, str, str, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit 0 with no record written (`--record` on a changed tree): the end is `ok`, its url empty."""
    monkeypatch.setattr(approvals, "follow", lambda *_a: 0)
    ended_by_detach(steps, ["--help"], monkeypatch)
    assert [(state, url) for state, _, url, *_ in emitted][-2:] == [("waiting", ""), ("ok", "")]


def in_process_child(
    monkeypatch: pytest.MonkeyPatch, steps: dict[str, Any], argv: list[str], die: bool = False
) -> None:
    """Make detach() run its child in this process, where the test's policy is set: `follow` runs `cli` as the child
    does (the head in DETACHED, the exit and event files written at its end), or, with `die`, writes nothing."""
    head = git_in(steps["repo"], "rev-parse", "HEAD").strip()
    monkeypatch.chdir(steps["repo"])
    approvals.ENDED.clear()
    real = subprocess.Popen

    def popen(*a: Any, **k: Any) -> Any:
        if approvals.DETACHED in (k.get("env") or {}):  # only the child detach() starts
            return type("P", (), {"pid": os.getpid()})()
        return real(*a, **k)

    monkeypatch.setattr(approvals.subprocess, "Popen", popen)

    def follow(*_a: Any) -> int:
        if die:
            return 1
        monkeypatch.setenv(approvals.DETACHED, head)
        code = approvals.cli(argv)
        return code

    monkeypatch.setattr(approvals, "follow", follow)


def test_a_detached_child_killed_before_writing_ends_failed(
    steps: dict[str, Any],
    emitted: list[tuple[str, int, str, str, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child that dies before it writes anything: `follow` returns 1 with no exit file, and the event fails."""
    in_process_child(monkeypatch, steps, ["local-ci", "--record"], die=True)
    assert approvals.detach(["local-ci", "--record"]) == 1
    assert emitted[-1][0] == "failed"


def test_a_detached_run_that_wrote_its_end_is_not_ended_twice(
    steps: dict[str, Any],
    emitted: list[tuple[str, int, str, str, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate(steps, "raise SystemExit(3)")
    in_process_child(monkeypatch, steps, ["local-ci", "--record"])
    assert approvals.detach(["local-ci", "--record"]) == 1
    states = [state for state, *_ in emitted]
    assert states.count("failed") == 1 and states[-1] == "failed"


def test_a_detached_run_with_a_step_left_for_a_host_stays_waiting(
    steps: dict[str, Any],
    emitted: list[tuple[str, int, str, str, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    in_process_child(monkeypatch, steps, ["local-ci", "--record"])
    assert approvals.detach(["local-ci", "--record"]) == 0
    assert {state for state, *_ in emitted} == {"waiting"}


def test_a_stale_end_marker_of_an_earlier_run_on_the_head_does_not_hide_a_dead_child(
    steps: dict[str, Any],
    emitted: list[tuple[str, int, str, str, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    in_process_child(monkeypatch, steps, ["local-ci", "--record"], die=True)
    head = git_in(steps["repo"], "rev-parse", "HEAD").strip()
    approvals.local_ci_dir().mkdir(parents=True, exist_ok=True)
    approvals.ended_file(head).write_text("ok\n")
    assert approvals.detach(["local-ci", "--record"]) == 1
    assert emitted[-1][0] == "failed"
