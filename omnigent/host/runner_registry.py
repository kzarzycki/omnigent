"""On-disk list of the runners a host daemon owns.

The daemon keeps one file per daemon target. It names the owning daemon and
every runner it started or adopted. A daemon that is asked to stop marks the
file ``released`` before it dies, which tells its runners to keep serving and
tells the next daemon for the same target that it may adopt them. Without the
mark (a crash, ``SIGKILL``) runners exit as they always did.

Processes are identified by pid plus start stamp, so a recycled pid is never
adopted and never signalled.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Collection
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

import psutil

from omnigent.host.daemon_lifecycle import _target_digest, daemon_registry_dir
from omnigent.inner._proc import _process_start_stamp
from omnigent.native.owner_claim import current_boot_id

_logger = logging.getLogger(__name__)

# Tells a runner where its daemon's registry lives.
RUNNER_HOST_REGISTRY_ENV_VAR = "OMNIGENT_RUNNER_HOST_REGISTRY"

# Start stamps are read by different processes; allow one clock tick of noise.
_START_STAMP_TOLERANCE_S = 0.02


@dataclass(frozen=True)
class RegisteredRunner:
    """One runner process a daemon owns."""

    runner_id: str
    pid: int
    started_at: float
    session_id: str | None
    log_path: str
    connect_marker: str | None


@dataclass(frozen=True)
class RegistryOwner:
    """The daemon a registry file belongs to."""

    pid: int
    started_at: float
    released: bool
    boot_id: str | None


@dataclass(frozen=True)
class RegistrySnapshot:
    """A registry file's content."""

    owner: RegistryOwner
    runners: dict[str, RegisteredRunner]


def runner_registry_path(target: str, *, base_dir: Path | None = None) -> Path:
    """Return the registry path for a daemon *target*, e.g. ``"local"``."""
    # Not ``*.json``: the daemon record reader globs those.
    return daemon_registry_dir(base_dir) / f"{_target_digest(target)}.runners"


def process_start_stamp(pid: int) -> float | None:
    """Return *pid*'s start stamp, or ``None`` when it is gone or unreadable."""
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return None
        return float(_process_start_stamp(proc))
    except (psutil.Error, OSError):
        return None


def process_matches(pid: int, started_at: float) -> bool:
    """Whether *pid* is alive and is the process that started at *started_at*."""
    stamp = process_start_stamp(pid)
    return stamp is not None and abs(stamp - started_at) <= _START_STAMP_TOLERANCE_S


def _known_fields(record_type: type, raw: dict[str, Any]) -> dict[str, Any]:
    """Drop keys another build wrote that this one does not know."""
    names = {field.name for field in fields(record_type)}
    return {key: value for key, value in raw.items() if key in names}


def _is_process_identity(pid: object, started_at: object) -> bool:
    """Whether the values can name a process: an int pid and a numeric start stamp."""
    return type(pid) is int and type(started_at) in (int, float)


def read_registry(path: Path) -> RegistrySnapshot | None:
    """Read a registry file; ``None`` when it is missing or not valid."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        owner = RegistryOwner(**_known_fields(RegistryOwner, raw["daemon"]))
        runners = {
            runner_id: RegisteredRunner(
                **{**_known_fields(RegisteredRunner, entry), "runner_id": runner_id}
            )
            for runner_id, entry in raw["runners"].items()
        }
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
    # A wrongly typed field would crash the liveness checks that act on it.
    records = [owner, *runners.values()]
    if type(owner.released) is not bool or not all(
        _is_process_identity(record.pid, record.started_at) for record in records
    ):
        return None
    return RegistrySnapshot(owner=owner, runners=runners)


def _write_registry(path: Path, snapshot: RegistrySnapshot) -> None:
    """Replace the registry file in one step, so a reader never sees half of it."""
    payload = {
        "daemon": asdict(snapshot.owner),
        "runners": {
            runner_id: {key: value for key, value in asdict(entry).items() if key != "runner_id"}
            for runner_id, entry in snapshot.runners.items()
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp_path, path)


def adoptable_runners(snapshot: RegistrySnapshot | None) -> list[RegisteredRunner]:
    """Return the runners of a previous daemon that a new daemon may adopt.

    Only a released registry from this boot whose owner is gone qualifies, and
    of its runners only those whose pid still names the recorded process.
    """
    if snapshot is None or not snapshot.owner.released:
        return []
    if snapshot.owner.boot_id != current_boot_id():
        return []
    if process_matches(snapshot.owner.pid, snapshot.owner.started_at):
        return []
    return [
        entry
        for entry in snapshot.runners.values()
        if process_matches(entry.pid, entry.started_at)
    ]


class RunnerRegistry:
    """The registry of the running daemon. Safe to call from any thread."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._owner = RegistryOwner(
            pid=os.getpid(),
            started_at=process_start_stamp(os.getpid()) or 0.0,
            released=False,
            boot_id=current_boot_id(),
        )
        self._runners: dict[str, RegisteredRunner] = {}

    def claim(self) -> list[RegisteredRunner]:
        """Take over the file and return the previous daemon's adoptable runners.

        The new file names this daemon and the adopted runners in one write, so
        a waiting runner sees either the old owner or the new one with itself
        listed, never a new owner that has not decided yet.
        """
        with self._lock:
            adopted = adoptable_runners(read_registry(self.path))
            self._runners = {entry.runner_id: entry for entry in adopted}
            if self._write_locked():
                return adopted
            # The runners cannot see an owner that is not on disk, so they would
            # serve on with no daemon to stop them. End them as a crash would.
            self._runners = {}
        for entry in adopted:
            _signal_recorded(entry, signal.SIGTERM)
        return []

    def add(
        self,
        runner_id: str,
        pid: int,
        *,
        session_id: str | None,
        log_path: Path,
        connect_marker: Path | None,
    ) -> None:
        """Record a runner this daemon just started."""
        entry = RegisteredRunner(
            runner_id=runner_id,
            pid=pid,
            started_at=process_start_stamp(pid) or 0.0,
            session_id=session_id,
            log_path=str(log_path),
            connect_marker=str(connect_marker) if connect_marker is not None else None,
        )
        with self._lock:
            self._runners[runner_id] = entry
            self._write_locked()

    def remove(self, runner_id: str) -> None:
        """Forget a runner that exited or was stopped."""
        with self._lock:
            if self._runners.pop(runner_id, None) is not None:
                self._write_locked()

    def remove_pid(self, pid: int) -> None:
        """Forget the runner with *pid*, for one that never got a tracked handle."""
        with self._lock:
            stale = [rid for rid, entry in self._runners.items() if entry.pid == pid]
            for runner_id in stale:
                del self._runners[runner_id]
            if stale:
                self._write_locked()

    def clear(self) -> None:
        """Forget every runner, after the daemon stopped them all itself."""
        with self._lock:
            self._runners = {}
            self._write_locked()

    def release(self) -> None:
        """Mark the runners as free to outlive this daemon."""
        with self._lock:
            self._owner = RegistryOwner(**{**asdict(self._owner), "released": True})
            self._write_locked()

    def _write_locked(self) -> bool:
        try:
            _write_registry(self.path, RegistrySnapshot(self._owner, dict(self._runners)))
        except OSError:
            # Without the file runners fall back to exiting with the daemon.
            _logger.warning("Could not write runner registry %s", self.path, exc_info=True)
            return False
        return True


class AdoptedRunnerProc:
    """Popen-like handle on a runner this daemon adopted and is not the parent of."""

    def __init__(self, entry: RegisteredRunner) -> None:
        self.pid = entry.pid
        self.returncode: int | None = None
        self._started_at = entry.started_at
        self._log_path = Path(entry.log_path)

    def poll(self) -> int | None:
        """Return the exit code once the runner is gone, else ``None``."""
        if self.returncode is None and not process_matches(self.pid, self._started_at):
            self.returncode = self._exit_code_from_log()
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        """Block until the runner is gone; raise ``TimeoutExpired`` after *timeout*."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while (code := self.poll()) is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("adopted runner", timeout or 0.0)
            time.sleep(0.1)
        return code

    def terminate(self) -> None:
        """Ask the runner to shut down."""
        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        """End the runner at once."""
        self._signal(getattr(signal, "SIGKILL", signal.SIGTERM))

    def _signal(self, signum: int) -> None:
        if self.poll() is None:
            with contextlib.suppress(OSError):
                os.kill(self.pid, signum)

    def _exit_code_from_log(self) -> int:
        # ponytail: only a parent can read an exit status. An orderly runner
        # exit logs "runner exiting:"; anything else counts as a crash.
        try:
            tail = self._log_path.read_bytes()[-8192:]
        except OSError:
            return 1
        return 0 if b"runner exiting:" in tail else 1


def next_owner(path: Path, runner_id: str) -> tuple[int, float] | bool:
    """Decide what a runner does once the daemon it watched is gone.

    :returns: ``(pid, started_at)`` of the live daemon that lists this runner
        (a new daemon adopted it); ``True`` to keep serving and ask again (the
        dead daemon released its runners and no daemon has claimed them yet);
        ``False`` to shut down.
    """
    snapshot = read_registry(path)
    if snapshot is None or runner_id not in snapshot.runners:
        return False
    owner = snapshot.owner
    if process_matches(owner.pid, owner.started_at):
        return owner.pid, owner.started_at
    return owner.released


def _signal_recorded(entry: RegisteredRunner, signum: int) -> None:
    """Signal *entry*'s process, unless its pid now names another process."""
    if process_matches(entry.pid, entry.started_at):
        with contextlib.suppress(OSError):
            os.kill(entry.pid, signum)


def _live_runners(snapshot: RegistrySnapshot | None) -> list[RegisteredRunner]:
    """The listed runners that still run; none for a registry from another boot."""
    if snapshot is None or snapshot.owner.boot_id != current_boot_id():
        return []
    return [
        entry
        for entry in snapshot.runners.values()
        if process_matches(entry.pid, entry.started_at)
    ]


def live_runner_pids(path: Path) -> set[int]:
    """Pids of the runners the registry lists that still run."""
    return {entry.pid for entry in _live_runners(read_registry(path))}


def end_runner_sessions(
    leader_pids: Collection[int], *, started_before: float, grace_s: float = 2.0
) -> list[int]:
    """End what is left in the sessions that runners with *leader_pids* lead.

    A runner leads its own session, so its tool calls and background jobs
    carry its pid as their session id; a service manager no longer sweeps
    them. *started_before* is when the leaders were seen alive: a session id
    stays taken while a member lives, so an older process with that id is a
    member of the runner's session and not of one that reused the pid.

    :returns: The pids that were signalled.
    """
    members = []
    for proc in psutil.process_iter():
        with contextlib.suppress(psutil.Error, OSError):
            if (
                proc.pid != os.getpid()
                and os.getsid(proc.pid) in leader_pids
                and proc.create_time() <= started_before
            ):
                members.append(proc)
    for proc in members:
        with contextlib.suppress(psutil.Error):
            proc.terminate()
    _, stubborn = psutil.wait_procs(members, timeout=grace_s)
    for proc in stubborn:
        with contextlib.suppress(psutil.Error):
            proc.kill()
    return [proc.pid for proc in members]


def stop_registered_runners(path: Path, *, grace_s: float = 5.0) -> list[int]:
    """End every runner the registry lists and remove the file.

    For a deliberate stop, after the daemon is gone. A pid that no longer names
    the recorded process is left alone, and so is everything a registry from
    before the last boot lists.

    :returns: The pids that were signalled.
    """
    live = _live_runners(read_registry(path))
    for entry in live:
        _signal_recorded(entry, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline and any(
        process_matches(entry.pid, entry.started_at) for entry in live
    ):
        time.sleep(0.1)
    for entry in live:
        _signal_recorded(entry, getattr(signal, "SIGKILL", signal.SIGTERM))
    with contextlib.suppress(OSError):
        path.unlink()
    return [entry.pid for entry in live]
