"""omnigent_agent.py wait-for kills a live run whose log stops growing (#1123)."""

import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest
from test_omnigent_agent import omnigent_agent

started: list[int] = []


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(omnigent_agent, "WAIT_FOR_EVERY", 0.05)
    monkeypatch.setattr(omnigent_agent, "KILL_GRACE", 0.5)
    yield
    for pgid in started:  # only the groups this module launched
        try:
            os.killpg(pgid, 9)
        except ProcessLookupError:
            pass
    started.clear()


def detach(tmp_path: Path, command: str) -> tuple[Path, Path, int]:
    """The detached recipe (verifier.md) in its own session: pid file, then ``run <pid>``, the
    command's output, an exit line. Perl's setsid, as the recipe uses, since macOS has no
    `setsid` command; the launcher exits after its fork, so init reaps the run."""
    log, pidfile = tmp_path / "run.log", tmp_path / "pid"
    inner = f'echo $$ > {pidfile}; echo "run $$" > {log}; {{ {command}; }} >> {log} 2>&1; echo "exit $?" >> {log}'
    launcher = [
        "perl",
        "-MPOSIX=setsid",
        "-e",
        "fork and exit; setsid; exec @ARGV",
        "sh",
        "-c",
        inner,
    ]
    subprocess.run(launcher, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, check=True)
    for _ in range(100):
        if pidfile.exists() and log.exists() and "run " in log.read_text():
            break
        time.sleep(0.05)
    pid = int(pidfile.read_text())
    started.append(pid)
    return log, pidfile, pid


def gone(pgid: int) -> bool:
    for _ in range(60):  # init reaps the killed group
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError, PermissionError:  # macOS: EPERM for a group of zombies only
            return True
        time.sleep(0.1)
    return False


def age(log: Path, seconds: float) -> None:
    os.utime(log, (time.time() - seconds, time.time() - seconds))


def wait_for_text(log: Path, text: str) -> None:
    for _ in range(100):
        if text in log.read_text():
            return
        time.sleep(0.05)
    raise AssertionError(f"{text!r} never reached {log}")


def test_a_silent_run_is_killed_with_its_group_and_notifies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    log, pidfile, pid = detach(tmp_path, "echo hello; sleep 60")
    wait_for_text(log, "hello")
    notice = tmp_path / "notice"
    script = tmp_path / "notify.sh"
    script.write_text(f'#!/bin/sh\nprintf %s "$1" > {notice}\n')
    script.chmod(0o755)
    monkeypatch.setenv("LOOP_NOTIFY", str(script))
    start = time.monotonic()
    argv = ["wait-for", "--log", str(log), "--pid", str(pidfile), "--stall", "1", "--budget", "60"]
    assert omnigent_agent.main([*argv, "--notify"]) == 1
    assert time.monotonic() - start < 10
    out = capsys.readouterr().out
    first, *tail = out.splitlines()
    assert first.startswith(f"stalled: no output in {log} for ")
    assert first.endswith(f"s; killed pid {pid}'s group")
    assert tail == ["hello"]
    assert notice.read_text() == out.rstrip("\n")
    assert gone(pid)


def test_a_run_that_ignores_sigterm_is_killed_after_the_grace(tmp_path: Path) -> None:
    log, pidfile, pid = detach(tmp_path, 'trap "" TERM; echo hello; sleep 60')
    wait_for_text(log, "hello")
    argv = ["wait-for", "--log", str(log), "--pid", str(pidfile), "--stall", "1", "--budget", "60"]
    assert omnigent_agent.main(argv) == 1
    assert gone(pid)


def test_a_run_that_keeps_printing_ends_at_its_own_exit_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log, pidfile, _ = detach(tmp_path, "for i in 1 2 3 4 5 6 7 8; do echo tick $i; sleep 0.4; done")
    argv = [
        "wait-for",
        "--log",
        str(log),
        "--pid",
        str(pidfile),
        "--stall",
        "1.5",
        "--budget",
        "60",
    ]
    assert omnigent_agent.main(argv) == 0
    assert capsys.readouterr().out == "exit 0\ntick 8\n"


def test_a_run_waiting_on_the_gate_lock_is_not_killed_for_silence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log, pidfile, pid = detach(tmp_path, "sleep 60")
    lock = tmp_path / "gate.lock"
    log.write_text(f"run {pid}\nlock {lock}\n")
    age(log, 100)
    argv = ["wait-for", "--log", str(log), "--pid", str(pidfile), "--stall", "1", "--budget", "0.3"]
    assert omnigent_agent.main(argv) == omnigent_agent.LIMIT
    assert capsys.readouterr().out.startswith("waiting on the gate lock")
    os.killpg(pid, 0)  # still there
    age(log, omnigent_agent.LOCK_CEILING + 10)  # the 2-hour ceiling stands
    assert omnigent_agent.main(argv) == omnigent_agent.LIMIT
    assert "gave up waiting on the gate lock" in capsys.readouterr().out
    os.killpg(pid, 0)


def test_stall_zero_and_a_log_alone_kill_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log, pidfile, pid = detach(tmp_path, "echo hello; sleep 60")
    wait_for_text(log, "hello")
    age(log, 100)
    off = ["wait-for", "--log", str(log), "--pid", str(pidfile), "--stall", "0", "--budget", "0.3"]
    assert omnigent_agent.main(off) == omnigent_agent.LIMIT
    assert capsys.readouterr().out.startswith("still running")
    os.killpg(pid, 0)
    # no --pid: nothing to kill, whatever the log's age
    assert (
        omnigent_agent.main(["wait-for", "--log", str(log), "--stall", "1", "--budget", "0.3"]) == 6
    )
    assert capsys.readouterr().out.startswith("still running")
    os.killpg(pid, 0)


def test_a_reused_pid_is_never_killed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    other = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        log, pidfile = tmp_path / "run.log", tmp_path / "pid"
        log.write_text("run 1\nhello\n")
        pidfile.write_text(f"{other.pid}\n")  # written after the run's process ended...
        old = time.time() - 3600  # ...so a younger live process has taken its pid
        os.utime(pidfile, (old, old))
        age(log, 100)
        argv = [
            "wait-for",
            "--log",
            str(log),
            "--pid",
            str(pidfile),
            "--stall",
            "1",
            "--budget",
            "30",
        ]
        assert omnigent_agent.main(argv) == 1
        assert capsys.readouterr().out.startswith("gone: ")
        assert other.poll() is None  # untouched
    finally:
        other.kill()
        other.wait()


def test_a_pid_that_leads_no_group_is_killed_alone(tmp_path: Path) -> None:
    run = subprocess.Popen(["sleep", "60"])  # in this process's own group
    threading.Thread(target=run.wait).start()  # reaped, so the pid goes
    log, pidfile = tmp_path / "run.log", tmp_path / "pid"
    log.write_text(f"run {run.pid}\nhello\n")
    pidfile.write_text(f"{run.pid}\n")
    age(log, 100)
    argv = ["wait-for", "--log", str(log), "--pid", str(pidfile), "--stall", "1", "--budget", "30"]
    assert omnigent_agent.main(argv) == 1
    assert run.wait() == -15  # SIGTERM to the pid; this test process lives on


def test_a_pid_leading_wait_fors_own_group_is_killed_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log, pidfile, pid = detach(tmp_path, "echo hello; sleep 60")
    wait_for_text(log, "hello")
    monkeypatch.setattr(os, "getpgrp", lambda: pid)  # as if wait-for ran in the run's group
    argv = ["wait-for", "--log", str(log), "--pid", str(pidfile), "--stall", "1", "--budget", "60"]
    assert omnigent_agent.main(argv) == 1
    os.killpg(pid, 0)  # the group's other member, `sleep`, is still there


def test_a_pid_inside_another_group_is_killed_alone(tmp_path: Path) -> None:
    child, beat = tmp_path / "child", tmp_path / "beat"
    log, pidfile, _ = detach(
        tmp_path, f"sleep 60 & echo $! > {child}; while :; do echo beat >> {beat}; sleep 0.1; done"
    )
    for _ in range(100):
        if child.exists() and child.read_text().strip():
            break
        time.sleep(0.05)
    pidfile.write_text(child.read_text())  # a member of the run's group, not its leader
    age(log, 100)
    argv = ["wait-for", "--log", str(log), "--pid", str(pidfile), "--stall", "1", "--budget", "60"]
    assert omnigent_agent.main(argv) == 1
    seen = beat.stat().st_size
    time.sleep(0.5)
    assert beat.stat().st_size > seen  # the leader still runs: only the pid was signalled


def test_a_group_that_answers_eperm_once_signalled_counts_as_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS answers EPERM, not ESRCH, for a group left with only zombies: kill_group stops there
    instead of raising out of wait-for."""
    sent: list[int] = []

    def killpg(group: int, sig: int) -> None:
        sent.append(sig)
        if sig != signal.SIGTERM:
            raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(omnigent_agent.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(omnigent_agent.os, "killpg", killpg)
    omnigent_agent.kill_group(4242)
    assert sent == [signal.SIGTERM, 0]


def test_wait_for_without_stall_uses_the_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recipes never pass --stall, so the default is the behaviour: a silent run is killed under it."""
    monkeypatch.setattr(omnigent_agent, "WAIT_FOR_STALL", 1.0)
    log, pidfile, pid = detach(tmp_path, "echo hello; sleep 60")
    wait_for_text(log, "hello")
    argv = ["wait-for", "--log", str(log), "--pid", str(pidfile), "--budget", "60"]
    assert omnigent_agent.main(argv) == 1
    assert capsys.readouterr().out.startswith(f"stalled: no output in {log} for ")
    assert gone(pid)
