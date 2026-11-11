"""Runner registry: release, adoption, deliberate stop and the runner's watchdog."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.host import runner_registry
from omnigent.host.runner_registry import (
    AdoptedRunnerProc,
    RegisteredRunner,
    RegistryOwner,
    RegistrySnapshot,
    RunnerRegistry,
    adoptable_runners,
    next_owner,
    process_matches,
    process_start_stamp,
    read_registry,
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
    tmp_path: Path, spawn_process
) -> None:
    path = tmp_path / "target.runners"
    daemon, runner, other = spawn_process(), spawn_process(), spawn_process()
    recycled = _runner("runner-recycled", other, tmp_path / "b.log")
    recycled = RegisteredRunner(**{**recycled.__dict__, "started_at": recycled.started_at - 60})
    _write(
        path, _owner(daemon, released=True), _runner("runner-1", runner, tmp_path / "a"), recycled
    )

    assert stop_registered_runners(path, grace_s=5.0) == [runner.pid]

    assert runner.wait(timeout=5) is not None
    assert other.poll() is None
    assert not path.exists()
    assert stop_registered_runners(path) == []


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
    shutdown_requested = threading.Event()
    watchdog = threading.Thread(
        target=_entry._run_parent_death_killer,
        args=(daemon.pid, shutdown_requested.set),
        kwargs={
            "registry_path": path,
            "runner_id": "runner-1",
            "poll_interval_s": 0.02,
            "grace_s": 0.0,
            "exit_fn": lambda _code: None,
        },
        daemon=True,
    )
    watchdog.start()

    _end(daemon)

    assert shutdown_requested.wait(timeout=5)
    assert process_matches(runner.pid, process_start_stamp(runner.pid) or 0.0)
