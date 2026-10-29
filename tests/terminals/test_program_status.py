"""Wire parsing for program status; real launch coverage lives with the runner."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from omnigent.terminals.program_status import ProgramStatus, ProgramStatusParser, parse_report


@pytest.mark.parametrize("ending", [b"\x07", b"\x1b\\"])
def test_fragmented_reports_queries_and_resets(ending: bytes) -> None:
    events = []
    parser = ProgramStatusParser(
        on_report=events.append,
        on_query=lambda: events.append("query"),
        on_reset=lambda full: events.append(("reset", full)),
    )
    stream = (
        b"screen\x1b]7501;?"
        + ending
        + b"\x1b]7501;state=blocked:app=claude-code:kind=question:msg=QW5zd2VyPw"
        + ending
        + b"\x1b]7501;state=working:app=claude-code"
        + ending
        + b"\x1b]133;A;redraw=0"
        + ending
        + b"\x1bc"
    )
    output = b"".join(parser.feed(bytes([byte])) for byte in stream)
    assert output == stream.replace(b"\x1b]7501;?" + ending, b"")
    assert events == [
        "query",
        ProgramStatus("blocked", app="claude-code", kind="question", message="Answer?"),
        ProgramStatus("working", app="claude-code"),
        ("reset", False),
        ("reset", True),
    ]


@pytest.mark.parametrize(
    "body",
    [
        b"app=claude-code",
        b"state=future",
        b"state=working:id=",
        b"state=working:id=a//b",
        b"state=working:id=bad!",
        b"state=working:id=" + b"a/" * 8 + b"a",
        b"state=working:app=" + b"a" * 33,
        b"state=working:" + b"k" * 17 + b"=value",
        b"state=working:msg=a",
        b"state=working:msg=a:msg=QQ",
        b"state=working:app=" + b"a" * 33 + b":app=claude-code",
        b"state=working:title=/w==",
        b"state=working:msg=" + base64.b64encode(b"line\nbreak"),
        b"state=working:msg=" + base64.b64encode("\u0085".encode()),
        b"state=working:title=" + base64.b64encode(b"a" * 193),
        b"state=working:msg=" + base64.b64encode(b"a" * 2049),
    ],
)
def test_invalid_reports_are_discarded_whole(body: bytes) -> None:
    assert parse_report(body) is None


def test_unknown_keys_and_malformed_pairs_do_not_discard_valid_state() -> None:
    assert parse_report(
        b" broken : state=idle : future=x : state=working : msg=invalid! : kind=auth"
    ) == ProgramStatus("working")


def test_other_control_strings_and_oversized_reports_cannot_change_status() -> None:
    events = []
    parser = ProgramStatusParser(
        on_report=events.append, on_query=lambda: None, on_reset=lambda _: None
    )
    parser.feed(b"\x1bPignored\x1b]7501;state=error\x07\x1b\\")
    parser.feed(b"\x1b]0;title\x1b]7501;state=error\x07")
    parser.feed(b"\x1b]7501;state=error:unknown=" + b"x" * 4096 + b"\x1b\\")
    parser.feed(b"\x1b]7501;state=error\x18")
    parser.feed(b"\x1b]7501;state=idle\x07")
    assert events == [ProgramStatus("idle")]


@pytest.mark.parametrize("chunk_size", [1, 7, 65536])
def test_output_is_preserved_except_capability_query(chunk_size: int) -> None:
    stream = (
        b"text\x1b[?1049h\x1b[31mred\x1b[0m\n"
        b"\x1b]7501;?\x07\x1b[c"
        b"\x1bPnested\x1b]7501;?\x07\x1b\\"
        b"\x1b]7501;state=working:msg=" + b"a" * 5000 + b"\x1b\\"
        b"\x1b]7501;cancelled\x18\x1b]0;title\x07\x1b\x1bXignored\x1b\\"
        b"\x1b]unfinished"
    )
    queries = []
    parser = ProgramStatusParser(
        on_report=lambda _: None, on_query=lambda: queries.append(True), on_reset=lambda _: None
    )
    output = (
        b"".join(
            parser.feed(stream[i : i + chunk_size]) for i in range(0, len(stream), chunk_size)
        )
        + parser.finish()
    )
    assert output == stream.replace(b"\x1b]7501;?\x07\x1b[c", b"\x1b[c")
    assert queries == [True]


@pytest.mark.parametrize("failure", ["missing", "not-executable", "stdin-eof", "stdin-eio"])
def test_relay_process_failure_cleanup(tmp_path: Path, failure: str) -> None:
    """Exercise exec errors and input loss with a real child and PTY."""
    state_file = tmp_path / "status.json"
    ready = tmp_path / "ready"
    command = [str(tmp_path / "command")]
    wrapper = "from omnigent.terminals.program_status import main; main()"
    if failure == "not-executable":
        Path(command[0]).write_text("#!/bin/sh\nexit 0\n")
    if failure.startswith("stdin-"):
        command = [
            sys.executable,
            "-c",
            "import os,signal,time; from pathlib import Path; "
            "signal.signal(signal.SIGHUP, lambda *_: "
            "(time.sleep(0.05), print('graceful-exit-tail', flush=True), exit(23))); "
            # Publish readiness only after the PID file is fully written and closed.
            f"ready = Path({str(ready)!r}); pending = ready.with_suffix('.tmp'); "
            "pending.write_text(str(os.getpid())); pending.replace(ready); "
            "time.sleep(30)",
        ]
        # Inject only the outer input failure; fork, inner PTY and child are real.
        wrapper = (
            "import errno,os\n"
            "from omnigent.terminals.program_status import main\n"
            "read = os.read\n"
            "def disconnected_read(fd, size):\n"
            " if fd == 0:\n"
            + (
                "  raise OSError(errno.EIO, 'terminal disconnected')\n"
                if failure == "stdin-eio"
                else "  return b''\n"
            )
            + " return read(fd, size)\n"
            "os.read = disconnected_read\n"
            "main()\n"
        )
    master, slave = os.openpty()
    try:
        with subprocess.Popen(
            [
                sys.executable,
                "-c",
                wrapper,
                "--state-file",
                str(state_file),
                "--app",
                "claude-code",
                "--",
                *command,
            ],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ) as process:
            if failure.startswith("stdin-"):
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    assert process.poll() is None
                    time.sleep(0.01)
                assert ready.exists(), "child never became ready"
                os.write(master, b"x")
            output, _ = process.communicate(timeout=10)
            assert b"Traceback" not in output, output
            if failure.startswith("stdin-"):
                assert process.returncode == 23, output
                assert b"graceful-exit-tail" in output
                with pytest.raises(ProcessLookupError):
                    os.kill(int(ready.read_text()), 0)
            else:
                assert process.returncode == (127 if failure == "missing" else 126), output
                assert b"cannot exec" in output
            assert not state_file.exists()
    finally:
        os.close(master)
        os.close(slave)


# macOS before Python 3.13 has no os.waitid; a wrapper models that elsewhere.
_WITHOUT_WAITID = "if hasattr(os, 'waitid'): del os.waitid\n"
_WAITID_CASES = [
    pytest.param(True, marks=pytest.mark.skipif(not hasattr(os, "waitid"), reason="no os.waitid")),
    False,
]


def test_relay_exits_on_sigterm_when_child_exit_waits_for_master_close(tmp_path: Path) -> None:
    """A stop must not deadlock on a child whose exit waits for the PTY master to close."""
    ready = tmp_path / "ready"
    # Model macOS: a child exiting with unread PTY output is not reapable until the master closes.
    wrapper = (
        "import os,pty,time\n"
        "from omnigent.terminals.program_status import main\n"
        "real_fork, real_close = pty.fork, os.close\n"
        "real_waitpid, real_waitid = os.waitpid, getattr(os, 'waitid', None)\n"
        "masters = []\n"
        "def fork():\n"
        " pid, fd = real_fork()\n"
        " if pid: masters.append(fd)\n"
        " return pid, fd\n"
        "def close(fd):\n"
        " if fd in masters: masters.remove(fd)\n"
        " real_close(fd)\n"
        "def waitpid(pid, flags):\n"
        " while masters:\n"
        "  if flags & os.WNOHANG: return 0, 0\n"
        "  time.sleep(0.01)\n"
        " return real_waitpid(pid, flags)\n"
        "def waitid(idtype, pid, flags):\n"
        " if masters: return None\n"
        " return real_waitid(idtype, pid, flags)\n"
        "pty.fork, os.close, os.waitpid = fork, close, waitpid\n"
        "if real_waitid: os.waitid = waitid\n"
        "main()\n"
    )
    child = (
        "import os,signal,time; from pathlib import Path; "
        "[signal.signal(s, signal.SIG_IGN) for s in (signal.SIGTERM, signal.SIGHUP)]; "
        f"ready = Path({str(ready)!r}); pending = ready.with_suffix('.tmp'); "
        "pending.write_text(str(os.getpid())); pending.replace(ready); time.sleep(60)"
    )
    master, slave = os.openpty()
    try:
        with subprocess.Popen(
            [
                sys.executable,
                "-c",
                wrapper,
                "--state-file",
                str(tmp_path / "status.json"),
                "--app",
                "claude-code",
                "--",
                sys.executable,
                "-c",
                child,
            ],
            stdin=slave,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) as process:
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    assert process.poll() is None
                    time.sleep(0.01)
                assert ready.exists(), "child never became ready"
                process.terminate()
                process.wait(timeout=10)
                with pytest.raises(ProcessLookupError):
                    os.kill(int(ready.read_text()), 0)
            finally:
                if process.poll() is None:
                    process.kill()
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize("has_waitid", _WAITID_CASES)
def test_relay_stop_kills_descendants_after_child_exits(tmp_path: Path, has_waitid: bool) -> None:
    """A descendant ignoring SIGHUP and SIGTERM must not outlive the stopped relay.

    With ``os.waitid`` the group kill comes before the child is reaped, while its PID
    cannot be reused; without it (macOS before Python 3.13) the relay still works.
    """
    ready = tmp_path / "ready"
    errors = tmp_path / "relay-errors"
    wrapper = (
        "import os\n"
        "from omnigent.terminals.program_status import main\n"
        "real_killpg = os.killpg\n"
        + ("" if has_waitid else _WITHOUT_WAITID)
        + "def killpg(pgid, sig):\n"
        " flags = os.WEXITED | os.WNOHANG | os.WNOWAIT\n"
        " try: hasattr(os, 'waitid') and os.waitid(os.P_PID, pgid, flags)\n"
        " except ChildProcessError: os.write(2, b'killpg-after-reap\\n')\n"
        " real_killpg(pgid, sig)\n"
        "os.killpg = killpg\n"
        "main()\n"
    )
    descendant = (
        "import os,signal,time; from pathlib import Path; "
        "[signal.signal(s, signal.SIG_IGN) for s in (signal.SIGTERM, signal.SIGHUP)]; "
        f"ready = Path({str(ready)!r}); pending = ready.with_suffix('.tmp'); "
        "pending.write_text(str(os.getpid())); pending.replace(ready); time.sleep(60)"
    )
    child = (
        f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', {descendant!r}]); "
        "time.sleep(60)"
    )
    master, slave = os.openpty()
    descendant_pid = None
    try:
        with (
            errors.open("wb") as stderr,
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    wrapper,
                    "--state-file",
                    str(tmp_path / "status.json"),
                    "--app",
                    "claude-code",
                    "--",
                    sys.executable,
                    "-c",
                    child,
                ],
                stdin=slave,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
            ) as process,
        ):
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    assert process.poll() is None
                    time.sleep(0.01)
                assert ready.exists(), "descendant never became ready"
                descendant_pid = int(ready.read_text())
                process.terminate()
                process.wait(timeout=10)
                deadline = time.monotonic() + 5
                while _alive(descendant_pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
                assert not _alive(descendant_pid)
                assert b"killpg-after-reap" not in errors.read_bytes()
                assert b"Traceback" not in errors.read_bytes()
            finally:
                if process.poll() is None:
                    process.kill()
    finally:
        if descendant_pid is not None and _alive(descendant_pid):
            os.kill(descendant_pid, 9)
        os.close(master)
        os.close(slave)


def _alive(pid: int) -> bool:
    """Whether *pid* runs and is not a zombie awaiting its reaper."""
    import psutil

    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@pytest.mark.parametrize("has_waitid", _WAITID_CASES)
def test_relay_keeps_child_exit_status(tmp_path: Path, has_waitid: bool) -> None:
    """The child's own exit status passes through, with or without ``os.waitid``."""
    wrapper = (
        "import os\n"
        + ("" if has_waitid else _WITHOUT_WAITID)
        + "from omnigent.terminals.program_status import main\n"
        "main()\n"
    )
    master, slave = os.openpty()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                wrapper,
                "--state-file",
                str(tmp_path / "status.json"),
                "--app",
                "claude-code",
                "--",
                "sh",
                "-c",
                "exit 7",
            ],
            stdin=slave,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 7, result.stdout + result.stderr
        assert b"Traceback" not in result.stderr
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize("has_waitid", _WAITID_CASES)
def test_relay_forwards_input_to_a_running_child(tmp_path: Path, has_waitid: bool) -> None:
    """Checking for the child's exit never blocks input forwarding while it runs."""
    import select

    wrapper = (
        "import os\n"
        + ("" if has_waitid else _WITHOUT_WAITID)
        + "from omnigent.terminals.program_status import main\n"
        "main()\n"
    )
    master, slave = os.openpty()
    try:
        with subprocess.Popen(
            [
                sys.executable,
                "-c",
                wrapper,
                "--state-file",
                str(tmp_path / "status.json"),
                "--app",
                "claude-code",
                "--",
                "sh",
                "-c",
                'echo READY; read line; [ "$line" = ping ] && exit 5; exit 1',
            ],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as process:
            try:
                assert process.stdout is not None
                output = b""
                deadline = time.monotonic() + 5
                while b"READY" not in output and time.monotonic() < deadline:
                    if select.select([process.stdout], [], [], 0.1)[0]:
                        output += os.read(process.stdout.fileno(), 4096)
                assert b"READY" in output, output
                os.write(master, b"ping\r")
                assert process.wait(timeout=10) == 5
            finally:
                if process.poll() is None:
                    process.kill()
    finally:
        os.close(master)
        os.close(slave)
