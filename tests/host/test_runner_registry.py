"""Runner registry: release, adoption, deliberate stop and the runner's watchdog."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest

from omnigent.host import runner_registry
from omnigent.host.runner_registry import (
    AdoptedRunnerProc,
    RegisteredRunner,
    RegistryOwner,
    RegistrySnapshot,
    RunnerRegistry,
    adoptable_runners,
    end_runner_sessions,
    live_runner_pids,
    next_owner,
    process_matches,
    process_start_stamp,
    read_registry,
    session_leaders,
    stop_registered_runners,
)
from omnigent.native.owner_claim import current_boot_id
from omnigent.runner import _entry
from omnigent.runner._zygote import ZYGOTE_RUNNER_FORKED_ENV_VAR


@pytest.fixture
def spawn_process() -> Iterator[callable]:
    """Start idle processes that stand in for daemons and runners."""
    started: list[subprocess.Popen[bytes]] = []

    def spawn() -> subprocess.Popen[bytes]:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        started.append(proc)
        return proc

    yield spawn
    for proc in started:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _end(proc: subprocess.Popen[bytes]) -> None:
    proc.kill()
    proc.wait()


def _owner(proc: subprocess.Popen[bytes], *, released: bool) -> RegistryOwner:
    stamp = process_start_stamp(proc.pid)
    assert stamp is not None
    return RegistryOwner(
        pid=proc.pid, started_at=stamp, released=released, boot_id=current_boot_id()
    )


def _runner(runner_id: str, proc: subprocess.Popen[bytes], log_path: Path) -> RegisteredRunner:
    stamp = process_start_stamp(proc.pid)
    assert stamp is not None
    return RegisteredRunner(
        runner_id=runner_id,
        pid=proc.pid,
        started_at=stamp,
        session_id="session-1",
        log_path=str(log_path),
        connect_marker=None,
    )


def _write(path: Path, owner: RegistryOwner, *runners: RegisteredRunner) -> None:
    runner_registry._write_registry(
        path, RegistrySnapshot(owner, {entry.runner_id: entry for entry in runners})
    )


def test_released_registry_is_adopted_by_the_next_daemon(tmp_path: Path, spawn_process) -> None:
    path = tmp_path / "target.runners"
    old_daemon, live_runner, other = spawn_process(), spawn_process(), spawn_process()
    recycled = _runner("runner-recycled", other, tmp_path / "b.log")
    recycled = RegisteredRunner(**{**recycled.__dict__, "started_at": recycled.started_at - 60})
    _write(
        path,
        _owner(old_daemon, released=True),
        _runner("runner-live", live_runner, tmp_path / "a.log"),
        recycled,
    )
    # The previous daemon still runs: nothing is up for adoption.
    assert adoptable_runners(read_registry(path)) == []
    _end(old_daemon)

    adopted = RunnerRegistry(path).claim()

    assert [entry.runner_id for entry in adopted] == ["runner-live"]
    snapshot = read_registry(path)
    assert snapshot is not None
    assert snapshot.owner.released is False
    assert list(snapshot.runners) == ["runner-live"]
    # A pid that no longer names the recorded process is dropped, never signalled.
    assert other.poll() is None


def test_unreleased_registry_is_not_adopted(tmp_path: Path, spawn_process) -> None:
    path = tmp_path / "target.runners"
    old_daemon, runner = spawn_process(), spawn_process()
    _write(path, _owner(old_daemon, released=False), _runner("runner-1", runner, tmp_path / "log"))
    _end(old_daemon)

    assert RunnerRegistry(path).claim() == []
    assert runner.poll() is None


def test_registry_from_another_boot_is_not_adopted(tmp_path: Path, spawn_process) -> None:
    path = tmp_path / "target.runners"
    old_daemon, runner = spawn_process(), spawn_process()
    owner = RegistryOwner(**{**_owner(old_daemon, released=True).__dict__, "boot_id": "other"})
    _write(path, owner, _runner("runner-1", runner, tmp_path / "log"))
    _end(old_daemon)

    assert RunnerRegistry(path).claim() == []


def test_release_keeps_runners_added_before_and_after(tmp_path: Path, spawn_process) -> None:
    path = tmp_path / "target.runners"
    registry = RunnerRegistry(path)
    first, late, gone = spawn_process(), spawn_process(), spawn_process()
    registry.add(
        "runner-first",
        first.pid,
        session_id=None,
        log_path=tmp_path / "a.log",
        connect_marker=None,
    )
    registry.add(
        "runner-gone", gone.pid, session_id=None, log_path=tmp_path / "c.log", connect_marker=None
    )
    registry.remove("runner-gone")
    registry.release()
    # A spawn that lands while the daemon is on its way out is still recorded.
    registry.add(
        "runner-late",
        late.pid,
        session_id="s",
        log_path=tmp_path / "b.log",
        connect_marker=tmp_path / "b.connected",
    )

    snapshot = read_registry(path)
    assert snapshot is not None
    assert snapshot.owner.released is True
    assert sorted(snapshot.runners) == ["runner-first", "runner-late"]
    assert snapshot.runners["runner-late"].connect_marker == str(tmp_path / "b.connected")

    registry.clear()
    snapshot = read_registry(path)
    assert snapshot is not None
    assert snapshot.runners == {}


def test_registry_written_by_another_build_is_still_read(tmp_path: Path, spawn_process) -> None:
    path = tmp_path / "target.runners"
    daemon, runner = spawn_process(), spawn_process()
    _write(path, _owner(daemon, released=True), _runner("runner-1", runner, tmp_path / "log"))
    raw = json.loads(path.read_text())
    raw["daemon"]["added_by_a_newer_build"] = 1
    raw["runners"]["runner-1"]["added_by_a_newer_build"] = 1
    path.write_text(json.dumps(raw))

    snapshot = read_registry(path)

    assert snapshot is not None
    assert snapshot.runners["runner-1"].pid == runner.pid


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("daemon", "pid", "12"),
        ("daemon", "started_at", None),
        ("daemon", "released", "yes"),
        ("runner", "pid", True),
        ("runner", "started_at", "now"),
    ],
)
def test_registry_with_a_wrongly_typed_field_is_not_read(
    tmp_path: Path, spawn_process, section: str, field: str, value: object
) -> None:
    path = tmp_path / "target.runners"
    daemon, runner = spawn_process(), spawn_process()
    _write(path, _owner(daemon, released=True), _runner("runner-1", runner, tmp_path / "log"))
    raw = json.loads(path.read_text())
    record = raw["daemon"] if section == "daemon" else raw["runners"]["runner-1"]
    record[field] = value
    path.write_text(json.dumps(raw))

    assert read_registry(path) is None
    assert next_owner(path, "runner-1") is False
    assert stop_registered_runners(path) == []
    assert runner.poll() is None


def test_next_owner_decisions(tmp_path: Path, spawn_process) -> None:
    path = tmp_path / "target.runners"
    daemon, runner = spawn_process(), spawn_process()
    entry = _runner("runner-1", runner, tmp_path / "log")

    assert next_owner(path, "runner-1") is False  # no registry

    _write(path, _owner(daemon, released=True), entry)
    assert next_owner(path, "runner-1") == (daemon.pid, process_start_stamp(daemon.pid))
    assert next_owner(path, "runner-other") is False  # a daemon that does not list it

    owner = _owner(daemon, released=True)
    crashed = _owner(daemon, released=False)
    _end(daemon)
    _write(path, owner, entry)
    assert next_owner(path, "runner-1") is True  # released, nobody adopted yet
    _write(path, crashed, entry)
    assert next_owner(path, "runner-1") is False  # died without releasing


def test_stop_registered_runners_ends_only_recorded_processes(
    tmp_path: Path, spawn_process, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "target.runners"
    daemon, runner, other = spawn_process(), spawn_process(), spawn_process()
    recycled = _runner("runner-recycled", other, tmp_path / "b.log")
    recycled = RegisteredRunner(**{**recycled.__dict__, "started_at": recycled.started_at - 60})
    _write(
        path, _owner(daemon, released=True), _runner("runner-1", runner, tmp_path / "a"), recycled
    )

    sent: list[tuple[int, int]] = []
    real_kill = os.kill

    def recording_kill(pid: int, signum: int) -> None:
        sent.append((pid, signum))
        real_kill(pid, signum)

    monkeypatch.setattr(runner_registry.os, "kill", recording_kill)

    assert stop_registered_runners(path, grace_s=5.0) == [runner.pid]

    # SIGTERM first: the runner gets the chance to shut down cleanly.
    assert runner.wait(timeout=5) == -signal.SIGTERM
    # It exited inside the grace period, so its pid gets nothing more.
    assert sent == [(runner.pid, signal.SIGTERM)]
    assert other.poll() is None
    assert not path.exists()
    assert stop_registered_runners(path) == []


def test_stop_registered_runners_leaves_another_boots_registry_alone(
    tmp_path: Path, spawn_process
) -> None:
    path = tmp_path / "target.runners"
    daemon, runner = spawn_process(), spawn_process()
    owner = RegistryOwner(**{**_owner(daemon, released=True).__dict__, "boot_id": "another-boot"})
    _write(path, owner, _runner("runner-1", runner, tmp_path / "log"))

    assert stop_registered_runners(path) == []

    assert runner.poll() is None
    assert not path.exists()


def test_stop_registered_runners_kills_a_runner_that_ignores_sigterm(tmp_path: Path) -> None:
    path = tmp_path / "target.runners"
    stubborn = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('ready', flush=True)\n"
            "time.sleep(120)\n",
        ],
        stdout=subprocess.PIPE,
    )
    try:
        assert stubborn.stdout is not None
        assert stubborn.stdout.readline().strip() == b"ready"
        daemon = subprocess.Popen([sys.executable, "-c", "pass"])
        owner = _owner(daemon, released=True)
        daemon.wait()
        _write(path, owner, _runner("runner-stubborn", stubborn, tmp_path / "log"))

        assert stop_registered_runners(path, grace_s=0.3) == [stubborn.pid]

        assert stubborn.wait(timeout=5) == -signal.SIGKILL
    finally:
        if stubborn.poll() is None:
            stubborn.kill()
        stubborn.wait()


def test_adopted_runner_handle(tmp_path: Path, spawn_process) -> None:
    runner = spawn_process()
    log_path = tmp_path / "runner.log"
    log_path.write_text("runner exiting: idle timeout\n")
    handle = AdoptedRunnerProc(_runner("runner-1", runner, log_path))

    assert handle.poll() is None
    with pytest.raises(subprocess.TimeoutExpired):
        handle.wait(timeout=0.2)
    handle.terminate()
    runner.wait(timeout=5)
    assert handle.wait(timeout=5) == 0  # the log shows an orderly exit
    handle.kill()  # a no-op on a runner that is gone

    crashed = spawn_process()
    crashed_handle = AdoptedRunnerProc(_runner("runner-2", crashed, tmp_path / "missing.log"))
    _end(crashed)
    assert crashed_handle.poll() == 1


def test_watchdog_survives_a_released_daemon_and_follows_its_successor(
    tmp_path: Path, spawn_process, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Probe the daemon pid instead of getppid(), as a zygote-forked runner does.
    monkeypatch.setenv(ZYGOTE_RUNNER_FORKED_ENV_VAR, "1")
    path = tmp_path / "target.runners"
    first_daemon, second_daemon, runner = spawn_process(), spawn_process(), spawn_process()
    entry = _runner("runner-1", runner, tmp_path / "log")
    _write(path, _owner(first_daemon, released=True), entry)
    reads: list[Path] = []
    real_next_owner = runner_registry.next_owner

    def _counting_next_owner(registry_path: Path, runner_id: str) -> tuple[int, float] | bool:
        reads.append(registry_path)
        return real_next_owner(registry_path, runner_id)

    monkeypatch.setattr(runner_registry, "next_owner", _counting_next_owner)
    shutdown_requested = threading.Event()
    exited = threading.Event()
    watchdog = threading.Thread(
        target=_entry._run_parent_death_killer,
        args=(first_daemon.pid, shutdown_requested.set),
        kwargs={
            "registry_path": path,
            "runner_id": "runner-1",
            "poll_interval_s": 0.02,
            "grace_s": 0.0,
            "exit_fn": lambda _code: exited.set(),
        },
        daemon=True,
    )
    watchdog.start()

    _end(first_daemon)
    time.sleep(0.3)
    assert not shutdown_requested.is_set()  # released: keeps serving

    _write(path, _owner(second_daemon, released=False), entry)
    time.sleep(0.3)
    assert not shutdown_requested.is_set()  # adopted: watches the new daemon
    # It watches the adopter's process, not the registry.
    registry_reads = len(reads)
    time.sleep(0.3)
    assert len(reads) == registry_reads

    _end(second_daemon)  # the adopter dies without releasing
    assert shutdown_requested.wait(timeout=5)
    assert exited.wait(timeout=5)


def test_watchdog_ends_the_runner_when_the_daemon_dies_unreleased(
    tmp_path: Path, spawn_process, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ZYGOTE_RUNNER_FORKED_ENV_VAR, "1")
    path = tmp_path / "target.runners"
    daemon, runner = spawn_process(), spawn_process()
    _write(path, _owner(daemon, released=False), _runner("runner-1", runner, tmp_path / "log"))
    shutdown_requested, session_ended = threading.Event(), threading.Event()
    order: list[str] = []
    watchdog = threading.Thread(
        target=_entry._run_parent_death_killer,
        args=(daemon.pid, lambda: order.append("shutdown") or shutdown_requested.set()),
        kwargs={
            "registry_path": path,
            "runner_id": "runner-1",
            "end_session": lambda: order.append("end_session") or session_ended.set(),
            "poll_interval_s": 0.02,
            "grace_s": 0.0,
            "exit_fn": lambda _code: None,
        },
        daemon=True,
    )
    watchdog.start()

    _end(daemon)

    assert shutdown_requested.wait(timeout=5)
    assert session_ended.wait(timeout=5)
    # The runner stops taking work before its tool processes are ended.
    assert order == ["shutdown", "end_session"]
    assert process_matches(runner.pid, process_start_stamp(runner.pid) or 0.0)


def test_watchdog_shuts_the_runner_down_when_the_registry_cannot_be_read(
    spawn_process, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ZYGOTE_RUNNER_FORKED_ENV_VAR, "1")

    def broken(*_args: object) -> None:
        raise RuntimeError("registry lookup failed")

    monkeypatch.setattr(_entry, "_await_next_host_daemon", broken)
    daemon = spawn_process()
    shutdown_requested = threading.Event()
    watchdog = threading.Thread(
        target=_entry._run_parent_death_killer,
        args=(daemon.pid, shutdown_requested.set),
        kwargs={"poll_interval_s": 0.02, "grace_s": 0.0, "exit_fn": lambda _code: None},
        daemon=True,
    )
    watchdog.start()

    _end(daemon)

    assert shutdown_requested.wait(timeout=5)


def test_claim_that_cannot_be_written_ends_the_runners_instead_of_adopting(
    tmp_path: Path, spawn_process, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "target.runners"
    old_daemon, runner = spawn_process(), spawn_process()
    owner = _owner(old_daemon, released=True)
    _end(old_daemon)
    _write(path, owner, _runner("runner-1", runner, tmp_path / "log"))

    def full_disk(*_args: object) -> None:
        raise OSError("no space left")

    real_write = runner_registry._write_registry
    monkeypatch.setattr(runner_registry, "_write_registry", full_disk)
    registry = RunnerRegistry(path)

    assert registry.claim() == []
    assert runner.wait(timeout=5) == -signal.SIGTERM
    # Once the disk has room again, the ended runner is not listed as this daemon's.
    monkeypatch.setattr(runner_registry, "_write_registry", real_write)
    registry.release()
    snapshot = read_registry(path)
    assert snapshot is not None
    assert snapshot.runners == {}


def _runner_with_a_nested_harness() -> tuple[subprocess.Popen[bytes], int]:
    """Start a runner stand-in whose child starts a harness in a session of its own."""
    idle = [sys.executable, "-c", "import time; time.sleep(120)"]
    middle = (
        "import subprocess, time\n"
        f"harness = subprocess.Popen({idle!r}, start_new_session=True)\n"
        "print(harness.pid, flush=True)\n"
        "time.sleep(120)\n"
    )
    runner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, time\n"
            f"subprocess.Popen({[sys.executable, '-c', middle]!r})\n"
            "time.sleep(120)\n",
        ],
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    assert runner.stdout is not None
    return runner, int(runner.stdout.readline())


def _end_runner_tree(runner: subprocess.Popen[bytes], harness_pid: int) -> None:
    with contextlib.suppress(psutil.Error):
        for child in psutil.Process(runner.pid).children(recursive=True):
            with contextlib.suppress(psutil.Error):
                child.kill()
    with contextlib.suppress(OSError):
        os.kill(harness_pid, signal.SIGKILL)
    runner.kill()
    runner.wait()


def test_live_runner_pids_lists_the_running_runners_of_this_boot(
    tmp_path: Path, spawn_process
) -> None:
    path = tmp_path / "target.runners"
    daemon, gone = spawn_process(), spawn_process()
    live, harness_pid = _runner_with_a_nested_harness()
    try:
        owner = _owner(daemon, released=True)
        entries = (
            _runner("runner-live", live, tmp_path / "a"),
            _runner("runner-gone", gone, tmp_path / "b"),
        )
        _end(gone)
        _write(path, owner, *entries)

        # A harness that leads its own session, however deep, is swept with its runner.
        assert live_runner_pids(path) == {live.pid, harness_pid}

        _write(path, RegistryOwner(**{**owner.__dict__, "boot_id": "another-boot"}), *entries)
        assert live_runner_pids(path) == set()
    finally:
        _end_runner_tree(live, harness_pid)


def test_runner_ends_its_own_session_but_not_itself() -> None:
    # A session leader stands in for a runner whose daemon died unreleased.
    runner = subprocess.run(
        [
            sys.executable,
            "-c",
            "import subprocess, sys\n"
            "idle = [sys.executable, '-c', 'import time; time.sleep(120)']\n"
            "job = subprocess.Popen(idle)\n"
            "harness = subprocess.Popen(idle, start_new_session=True)\n"
            "from omnigent.runner import _entry\n"
            "_entry._end_own_session()\n"
            "import psutil\n"
            "for proc in (job, harness):\n"
            "    print('alive' if psutil.pid_exists(proc.pid) else 'gone', end=' ')\n",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        start_new_session=True,
    )

    # The sweep reaps its own children, so their pids are gone, not zombies.
    # The harness in a session of its own is swept with the runner's.
    assert runner.returncode == 0, runner.stderr
    assert runner.stdout.strip() == "gone gone"


def test_end_runner_sessions_ends_what_a_runner_left_in_its_session(spawn_process) -> None:
    # The leader stands in for a runner: it starts a job in its session and exits.
    leader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys\n"
            "job = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "stubborn = subprocess.Popen([sys.executable, '-c', "
            "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print(1, flush=True); time.sleep(120)'], stdout=subprocess.PIPE)\n"
            "stubborn.stdout.readline()\n"
            "print(job.pid, stubborn.pid, flush=True)\n",
        ],
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    assert leader.stdout is not None
    job_pid, stubborn_pid = map(int, leader.stdout.readline().split())
    leader.wait()
    bystander = spawn_process()
    seen_at = time.time()

    assert end_runner_sessions({leader.pid}, started_before=seen_at - 3600) == []
    assert process_start_stamp(job_pid) is not None

    assert sorted(
        end_runner_sessions({leader.pid}, started_before=seen_at, grace_s=0.5)
    ) == sorted([job_pid, stubborn_pid])
    assert process_start_stamp(job_pid) is None
    # A member that ignores SIGTERM is killed after the grace period.
    deadline = time.monotonic() + 5
    while process_start_stamp(stubborn_pid) is not None:
        assert time.monotonic() < deadline, "the member that ignores SIGTERM survived"
        time.sleep(0.05)
    assert bystander.poll() is None


def test_session_leaders_include_a_runners_children_that_lead_a_session() -> None:
    # The runner stand-in starts a harness in a session of its own and a plain child.
    idle = "[sys.executable, '-c', 'import time; time.sleep(120)']"
    runner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys, time\n"
            f"harness = subprocess.Popen({idle}, start_new_session=True)\n"
            f"plain = subprocess.Popen({idle})\n"
            "print(harness.pid, plain.pid, flush=True)\n"
            "time.sleep(120)\n",
        ],
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert runner.stdout is not None
        harness_pid, plain_pid = map(int, runner.stdout.readline().split())

        leaders = session_leaders({runner.pid})

        assert leaders == {runner.pid, harness_pid}
        ended = end_runner_sessions(leaders, started_before=time.time())
        assert set(ended) == {runner.pid, harness_pid, plain_pid}
        assert process_start_stamp(harness_pid) is None
    finally:
        runner.kill()
        runner.wait()


def test_end_runner_sessions_skips_pid_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    # macOS lists pid 0, and getsid(0) answers with the caller's own session.
    class _KernelTask:
        pid = 0

        def create_time(self) -> float:
            return 0.0

        def terminate(self) -> None:
            raise ValueError("refusing to signal pid 0")

    monkeypatch.setattr(runner_registry.psutil, "process_iter", lambda: [_KernelTask()])

    assert end_runner_sessions({os.getsid(0)}, started_before=time.time(), grace_s=0.0) == []
