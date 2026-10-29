"""An orphan sweep may act only on owner identities it can resolve."""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import omnigent.inner.terminal as terminal_mod
from omnigent.native import owner_claim


@pytest.mark.parametrize("failure", ["spawn", "timeout", "nonzero"])
def test_failed_reap_preserves_control_socket_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    directory = tmp_path / "omnigent-terminal-retry"
    directory.mkdir()
    socket = directory / "tmux.sock"
    socket.touch()
    owner_claim.write_owner_claim(directory)
    monkeypatch.setattr(terminal_mod, "_terminals_tmp_root", lambda: tmp_path)
    monkeypatch.setattr(terminal_mod, "_tmux_available", lambda: True)
    monkeypatch.setattr(terminal_mod, "_process_alive", lambda pid: False)

    def fail(*args: object, **kwargs: object) -> SimpleNamespace:
        if failure == "spawn":
            raise OSError(errno.EAGAIN, "cannot fork")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("tmux", 10)
        return SimpleNamespace(returncode=1, stderr=b"permission denied")

    monkeypatch.setattr(terminal_mod.subprocess, "run", fail)
    assert terminal_mod.reap_orphaned_terminals() == 0
    assert socket.exists()
    monkeypatch.setattr(
        terminal_mod.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0)
    )
    assert terminal_mod.reap_orphaned_terminals() == 1
    assert not directory.exists()


def test_reap_removes_stale_socket_when_tmux_reports_server_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "omnigent-terminal-stale"
    directory.mkdir()
    socket = directory / "tmux.sock"
    socket.touch()
    owner_claim.write_owner_claim(directory)
    monkeypatch.setattr(terminal_mod, "_terminals_tmp_root", lambda: tmp_path)
    monkeypatch.setattr(terminal_mod, "_tmux_available", lambda: True)
    monkeypatch.setattr(terminal_mod, "_process_alive", lambda pid: False)

    monkeypatch.setattr(
        terminal_mod.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stderr=f"no server running on {socket}".encode()
        ),
    )

    assert terminal_mod.reap_orphaned_terminals() == 1
    assert not directory.exists()


@pytest.mark.parametrize(
    "record",
    [
        b"123456789",
        b"123456789\npid_ns=foreign\nboot=local-boot\n",
        b"123456789\npid_ns=local-ns\nboot=other-boot\n",
        b"\xff",
    ],
)
def test_sweep_preserves_unresolvable_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record: bytes
) -> None:
    directory = tmp_path / "omnigent-terminal-owned"
    directory.mkdir()
    (directory / "owner.pid").write_bytes(record)
    monkeypatch.setattr(terminal_mod, "_terminals_tmp_root", lambda: tmp_path)
    monkeypatch.setattr(terminal_mod, "_tmux_available", lambda: True)
    monkeypatch.setattr(terminal_mod, "_process_alive", lambda pid: False)
    monkeypatch.setattr(owner_claim, "current_pid_namespace", lambda: "local-ns")
    monkeypatch.setattr(owner_claim, "current_boot_id", lambda: "local-boot")

    assert terminal_mod.reap_orphaned_terminals() == 0
    assert directory.exists()


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
@pytest.mark.parametrize("ownership", ["legacy", "foreign", "dead_local"])
def test_sweep_only_kills_real_tmux_with_proven_dead_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ownership: str
) -> None:
    """A live server survives even when its owner's PID is not locally visible."""
    directory = tmp_path / "omnigent-terminal-live"
    directory.mkdir()
    record = "123456789\n"
    if ownership != "legacy":
        namespace = "local-ns" if ownership == "dead_local" else "foreign"
        record += f"pid_ns={namespace}\nboot=local-boot\n"
    (directory / "owner.pid").write_text(record)
    base = ["tmux", "-S", str(directory / "tmux.sock"), "-f", os.devnull]
    subprocess.run(
        [*base, "new-session", "-d", "-s", "main", "sleep 60"],
        check=True,
        capture_output=True,
        timeout=10,
    )
    monkeypatch.setattr(terminal_mod, "_terminals_tmp_root", lambda: tmp_path)
    monkeypatch.setattr(terminal_mod, "_process_alive", lambda pid: False)
    monkeypatch.setattr(owner_claim, "current_pid_namespace", lambda: "local-ns")
    monkeypatch.setattr(owner_claim, "current_boot_id", lambda: "local-boot")
    try:
        assert terminal_mod.reap_orphaned_terminals() == (1 if ownership == "dead_local" else 0)
        result = subprocess.run(
            [*base, "has-session", "-t", "main"],
            check=False,
            capture_output=True,
            timeout=10,
        )
        assert (result.returncode == 0) is (ownership != "dead_local")
    finally:
        subprocess.run([*base, "kill-server"], check=False, capture_output=True, timeout=10)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_sweep_kills_relays_whose_instance_dir_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relay outliving its terminal dies with its child; a live terminal's relay survives."""
    import sys
    import time

    from omnigent.terminals import program_status

    monkeypatch.setattr(terminal_mod, "_terminals_tmp_root", lambda: tmp_path)
    relays: dict[str, tuple[subprocess.Popen[bytes], Path]] = {}
    ptys: list[int] = []
    child_pids: dict[str, int | None] = {}
    try:
        for name in ("gone", "live"):
            instance_dir = tmp_path / f"omnigent-terminal-{name}"
            instance_dir.mkdir()
            state_file = instance_dir / program_status.STATE_FILE_NAME
            master, slave = os.openpty()
            ptys += [master, slave]
            relays[name] = (
                subprocess.Popen(
                    [
                        sys.executable,
                        program_status.__file__,
                        "--state-file",
                        str(state_file),
                        "--app",
                        "claude-code",
                        "--",
                        "sh",
                        "-c",
                        "trap '' HUP TERM; sleep 60",
                    ],
                    stdin=slave,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ),
                state_file,
            )
        for name, (_, state_file) in relays.items():
            deadline = time.monotonic() + 5
            while not state_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            child_pids[name] = program_status.ProgramStatusReader(state_file).child_pid
            assert child_pids[name] is not None
        shutil.rmtree(tmp_path / "omnigent-terminal-gone")

        assert terminal_mod.reap_orphaned_terminals() == 1

        relays["gone"][0].wait(timeout=5)
        deadline = time.monotonic() + 5
        while terminal_mod._process_alive(child_pids["gone"]) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not terminal_mod._process_alive(child_pids["gone"])
        assert relays["live"][0].poll() is None
        assert terminal_mod._process_alive(child_pids["live"])
    finally:
        for process, _ in relays.values():
            process.kill()
            process.wait(timeout=5)
        for pid in child_pids.values():
            with contextlib.suppress(OSError, TypeError):
                os.killpg(pid, signal.SIGKILL)
        for fd in ptys:
            os.close(fd)


@pytest.mark.parametrize(
    "case",
    [
        "orphan",
        "other_user",
        "other_script",
        "other_flag",
        "other_state_file",
        "other_prefix",
        "other_temp_root",
        "dir_present",
        "dir_present_in_relay_namespace",
    ],
)
def test_relay_selection_spares_everything_but_an_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Only this user's relay from this module, for a gone dir under the temp root, is selected."""
    from omnigent.terminals import program_status

    root = tmp_path / "tmp"
    root.mkdir()
    instance = root / ("other-terminal-x" if case == "other_prefix" else "omnigent-terminal-x")
    if case == "other_temp_root":
        instance = tmp_path / "elsewhere" / "omnigent-terminal-x"
    if case == "dir_present":
        instance.mkdir()
    if case == "dir_present_in_relay_namespace":
        namespace_root = tmp_path / "namespace"
        (namespace_root / instance.relative_to("/")).mkdir(parents=True)
        monkeypatch.setattr(terminal_mod, "_process_root", lambda pid: namespace_root)
    cmdline = [
        "python3",
        "/opt/x/omnigent/terminals/other.py"
        if case == "other_script"
        else program_status.__file__,
        "--app" if case == "other_flag" else "--state-file",
        str(
            instance / ("x.json" if case == "other_state_file" else program_status.STATE_FILE_NAME)
        ),
        "--app",
        "claude-code",
    ]
    uid = os.getuid() + 1 if case == "other_user" else os.getuid()

    selected = terminal_mod._orphaned_relay_dir(os.getpid(), uid, cmdline, root)

    assert selected == (instance if case == "orphan" else None)


@pytest.mark.parametrize("has_proc", [True, False])
def test_process_root_follows_the_relay_namespace_only_where_proc_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_proc: bool
) -> None:
    """Linux probes the relay's own root; a system without /proc (macOS) probes the host's."""
    proc_self_root = tmp_path / "proc-self-root"
    if has_proc:
        proc_self_root.mkdir()
    monkeypatch.setattr(terminal_mod, "_PROC_SELF_ROOT", proc_self_root)

    root = terminal_mod._process_root(4321)

    assert root == (Path("/proc/4321/root") if has_proc else Path("/"))
