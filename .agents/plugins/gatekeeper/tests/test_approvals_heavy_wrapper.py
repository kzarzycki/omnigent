"""approvals.py: the heavy wrapper, one host-configured command prefix every heavy step runs under, and the stop that
reaches a step it moved into a process group of its own (#1092)."""

from __future__ import annotations

import contextlib
import importlib.util
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import (
    SCRIPTS,
    approvals,
)

# a wrapper that logs its argv, takes its own leading options as a scope's properties, then runs the step as it is
LOGS = '#!/bin/sh\nprintf "%s\\n" "$*" >> "$WRAP_LOG"\nwhile [ "${1#-}" != "$1" ]; do shift; done\n'
EXECS = LOGS + 'exec "$@"\n'
# the same, but it keeps running beside the step, a child of its own in its group, as one that forks would
FORKS = LOGS + '"$@" &\nwait\n'
# a wrapper whose probe never ends
HANGS = "#!/bin/sh\nexec sleep 30\n"
# a runner of one heavy step, stopped by the test: `default` leaves SIGTERM and SIGHUP as they are, `stopped` handles
# them as land does (SystemExit, further stops ignored) and `interrupted` as mutate.py does (an exception of its own)
RUNNER = """
import signal, sys
sys.path.insert(0, sys.argv[1])
import approvals

class Interrupted(Exception):
    pass

def stopped(signum, _frame):
    for sig in approvals.STOPS:
        signal.signal(sig, signal.SIG_IGN)
    sys.exit(128 + signum)

def interrupted(signum, _frame):
    raise Interrupted(signum)

mode = sys.argv[2]
for sig in approvals.STOPS:
    # `default` resets the stops itself: a runner inherits an ignored SIGHUP from a gate run under nohup
    signal.signal(sig, signal.SIG_DFL if mode == "default" else stopped if mode == "stopped" else interrupted)
try:
    print(approvals.run_heavy(sys.argv[3:])[0])
except Interrupted:
    sys.exit(99)
"""
# a step that writes its pid and its group's, then sleeps far past the test's patience (STOP_BOUND_S): a step a
# stop failed to kill must still be running when that patience runs out, so the test fails rather than waits it out
SLEEPER = 'echo "$$ $(ps -o pgid= -p $$)" > "$1"; exec sleep 600'

LOOP_SCRIPTS = (
    Path(__file__).resolve().parents[2] / "loop" / "skills" / "engineering-loop" / "scripts"
)


@pytest.fixture(autouse=True)
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake host: its config home, where the wrapper file goes, and no wrapped run above this one; each test probes
    afresh."""
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv(approvals.HEAVY_WRAPPED, raising=False)
    monkeypatch.setenv("WRAP_LOG", str(tmp_path / "wrap.log"))
    monkeypatch.setattr(approvals, "heavy_probed", {})
    return home


def configure(host: Path, text: str) -> Path:
    """The host's wrapper file holding `text`."""
    path = host / ".config" / "engineering-loop" / "host"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# host\nother = 1\nheavy_wrapper = {text}" if text.strip() else text)
    return path


def wrapper(tmp_path: Path, body: str = EXECS, name: str = "wrap") -> Path:
    script = tmp_path / name
    script.write_text(body)
    script.chmod(0o755)
    return script


def wrapped(tmp_path: Path, probes: bool = False) -> list[str]:
    """What each run of a fake wrapper was handed, one line each, without its probes (`<options> true`) unless
    `probes`."""
    log = tmp_path / "wrap.log"
    lines = log.read_text().splitlines() if log.exists() else []
    return [
        line
        for line in lines
        if probes
        or not (line.split()[-1:] == ["true"] and all(w.startswith("-") for w in line.split()[:-1]))
    ]


@pytest.mark.parametrize("text", [None, "", "\n  \n", "# off for now\n\n   # still off\n"])
def test_without_a_wrapper_line_a_heavy_step_keeps_its_argv(host: Path, text: str | None) -> None:
    if text is not None:
        configure(host, text)
    assert approvals.heavy_argv(["mise", "run", "check"]) == ["mise", "run", "check"]


def test_xdg_config_home_names_where_the_wrapper_file_is(
    tmp_path: Path, host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = wrapper(tmp_path)
    configure(host, f"{script} --ignored-home\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert approvals.heavy_argv(["true"]) == ["true"]
    (tmp_path / "xdg" / "engineering-loop").mkdir(parents=True)
    (tmp_path / "xdg" / "engineering-loop" / "host").write_text(
        f"heavy_wrapper = {script} --p='A B'\n"
    )
    assert approvals.heavy_argv(["true"]) == [str(script), "--p=A B", "true"]


def test_the_first_heavy_wrapper_line_is_the_prefix(tmp_path: Path, host: Path) -> None:
    script = wrapper(tmp_path)
    configure(host, f"{script} --slice='loop heavy' --\nheavy_wrapper = {script} second\n")
    assert approvals.heavy_argv(["make", "test"]) == [
        str(script),
        "--slice=loop heavy",
        "--",
        "make",
        "test",
    ]


def step(words: list[str]) -> dict[str, Any]:
    return {"name": "test", "words": words, "sets": {}, "legacy": False, "command": " ".join(words)}


def test_a_gate_step_runs_under_the_wrapper_marked_wrapped_with_its_own_exit(
    tmp_path: Path, host: Path
) -> None:
    script = wrapper(tmp_path)
    configure(host, f"{script} --limit\n")
    seen = tmp_path / "seen"
    words = ["sh", "-c", f'echo "$LOOP_HEAVY_WRAPPED" > {seen}; exit 7']
    with (tmp_path / "out.log").open("w") as out:
        assert approvals.ran_step(step(words), tmp_path, out)[0] == 7
    assert wrapped(tmp_path) == [f"--limit sh -c {words[2]}"]
    assert seen.read_text() == "1\n"


def test_without_the_wrapper_a_gate_step_runs_as_before(tmp_path: Path, host: Path) -> None:
    seen = tmp_path / "seen"
    words = ["sh", "-c", f'echo "${{LOOP_HEAVY_WRAPPED-unset}}" > {seen}; exit 3']
    assert approvals.ran_step(step(words))[0] == 3
    assert seen.read_text() == "unset\n"


def test_a_wrapped_run_adds_no_second_prefix(
    tmp_path: Path, host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(host, f"{wrapper(tmp_path)}\n")
    monkeypatch.setenv(approvals.HEAVY_WRAPPED, "1")
    assert approvals.heavy_argv(["true"]) == ["true"]
    assert approvals.run_heavy(["true"])[0] == 0
    assert wrapped(tmp_path) == []
    assert approvals.heavy_argv(["true"], {"PATH": os.environ["PATH"]}) != ["true"]


def test_the_pre_land_check_runs_under_the_wrapper(
    tmp_path: Path, host: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(host, f"{wrapper(tmp_path)}\n")
    monkeypatch.setattr(
        approvals, "settings", lambda _loop: {"pre_land": "sh -c 'echo refused; exit 4'"}
    )
    monkeypatch.setattr(approvals, "fetched", lambda _head: True)
    monkeypatch.setattr(approvals, "run_in", lambda _work, *_args: "tip")
    monkeypatch.setattr(approvals, "reset", lambda _work, _tip: None)
    monkeypatch.setattr(approvals, "notes", lambda _pull: [])
    posted: list[str] = []
    monkeypatch.setattr(approvals, "comment", lambda _number, body: posted.append(body))
    failed: set[int] = set()
    pull = {"number": 8, "headRefOid": "head"}
    assert not approvals.pre_landed(pull, "main", tmp_path, None, failed)
    assert failed == {8}
    assert wrapped(tmp_path) == ["sh -c echo refused; exit 4"]
    assert "exiting 4: left out of this batch\n  refused" in capsys.readouterr().out
    assert "refused" in posted[0]


def mutate_module() -> Any:
    spec = importlib.util.spec_from_file_location("mutate_heavy", LOOP_SCRIPTS / "mutate.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_mutate_test_run_goes_through_the_wrapper(tmp_path: Path, host: Path) -> None:
    mutate = mutate_module()
    mutate.approvals().heavy_probed.clear()
    command = "echo ran; exit 3"
    assert mutate.run_command(command, "slot") == (3, "ran\n")
    assert wrapped(tmp_path) == []
    configure(host, f"{wrapper(tmp_path)} --scope\n")
    mutate.approvals().heavy_probed.clear()
    assert mutate.run_command(command, "slot") == (3, "ran\n")
    assert wrapped(tmp_path) == [f"--scope /bin/sh -c {command}"]


def test_without_the_wrapper_mutate_runs_the_shells_own_argv(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mutate = mutate_module()
    loaded = mutate.approvals()
    seen: list[list[str]] = []
    real = loaded.subprocess.Popen

    def spy(args: list[str], **kw: Any) -> Any:
        seen.append(args)
        return real(args, **kw)

    monkeypatch.setattr(loaded.subprocess, "Popen", spy)
    loaded.heavy_probed.clear()
    mutate.run_command("exit 0", "slot")
    assert seen == [["/bin/sh", "-c", "exit 0"]]


@pytest.mark.parametrize(
    ("line", "why"),
    [
        ("no-such-heavy-wrapper --scope", "`no-such-heavy-wrapper` is not on PATH"),
        ("{failing} --bad-property", "true` exited 1"),
    ],
)
def test_a_wrapper_that_cant_run_says_so_once_and_the_step_runs_unwrapped(
    tmp_path: Path, host: Path, capsys: pytest.CaptureFixture[str], line: str, why: str
) -> None:
    failing = wrapper(tmp_path, '#!/bin/sh\nprintf "%s\\n" "$*" >> "$WRAP_LOG"\nexit 1\n')
    configure(host, line.format(failing=failing) + "\n")
    assert approvals.run_heavy(["sh", "-c", "exit 5"])[0] == 5
    assert approvals.run_heavy(["sh", "-c", "exit 6"])[0] == 6
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1 and err[0].startswith("heavy wrapper unavailable: "), err
    assert why in err[0] and err[0].endswith("; running unwrapped")
    # only the probe ran the wrapper
    assert wrapped(tmp_path, probes=True) == ([] if "PATH" in why else ["--bad-property true"])


def test_a_probe_that_hangs_counts_as_a_failing_one(
    tmp_path: Path, host: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(host, f"{wrapper(tmp_path, HANGS)}\n")
    monkeypatch.setattr(approvals, "HEAVY_PROBE_SECONDS", 0.5)
    began = time.monotonic()
    assert approvals.run_heavy(["sh", "-c", "exit 2"])[0] == 2
    assert time.monotonic() - began < 10
    (err,) = capsys.readouterr().err.splitlines()
    assert "took over 0.5 s" in err


def test_a_heavy_step_leaves_each_stop_handler_as_it_found_it() -> None:
    def mine(_signum: int, _frame: Any) -> None:
        pass

    before = {sig: signal.getsignal(sig) for sig in approvals.STOPS}
    try:
        signal.signal(signal.SIGTERM, mine)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        assert approvals.run_heavy(["true"])[0] == 0
        assert signal.getsignal(signal.SIGTERM) is mine
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        seen: list[Any] = []
        approvals.run_heavy(["true"], stdout=subprocess.DEVNULL)
        with approvals.stops_raise():
            seen.append(signal.getsignal(signal.SIGTERM))
        assert seen == [approvals.stop_exit]
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
    finally:
        for sig, handler in before.items():
            signal.signal(sig, handler)


# Each wait below ends on its event; the bound only decides when to give up. On a host whose CPUs are
# saturated (every core busy, load several times the core count) a stop takes seconds to reach a forked
# step, and 10-15 s bounds failed there (#1189), so the bound is a minute.
STOP_BOUND_S = 60


def alive(pgid: int) -> bool:
    """Whether the group `pgid` has a process that has not ended: a zombie has, and only waits for its parent (or init,
    which may take seconds on a host) to reap it."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    states = subprocess.run(
        ["ps", "-A", "-o", "pgid=,stat="], capture_output=True, text=True, check=False
    ).stdout
    return any(
        line.split()[0] == str(pgid) and not line.split()[1].startswith("Z")
        for line in states.splitlines()
        if len(line.split()) == 2
    )


def gone(pgid: int, within: float = STOP_BOUND_S) -> bool:
    """Whether the group `pgid` has no process left within `within` seconds, zombies reaped by their parents."""
    deadline = time.monotonic() + within
    while alive(pgid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def stop_run(argv: list[str], pids: Path, sig: signal.Signals) -> tuple[int, int]:
    """Start `argv` in a session of its own, wait for its step to write its pid file, send `sig` to the runner alone
    (never its group, which the step is no longer in), and return the runner's exit and the step's group. A runner
    still going after STOP_BOUND_S is killed with its step's group, and the test fails."""
    runner = subprocess.Popen(argv, start_new_session=True, stdout=subprocess.PIPE, text=True)
    deadline = time.monotonic() + STOP_BOUND_S
    while not pids.exists() or not pids.read_text().strip():
        assert time.monotonic() < deadline and runner.poll() is None, "the step never started"
        time.sleep(0.05)
    _pid, pgid = map(int, pids.read_text().split())
    assert pgid != runner.pid, "the step runs in a group of its own"
    runner.send_signal(sig)
    try:
        code = runner.wait(timeout=STOP_BOUND_S)
    except subprocess.TimeoutExpired:
        for group in (runner.pid, pgid):
            with contextlib.suppress(OSError):
                os.killpg(group, signal.SIGKILL)
        runner.wait()
        pytest.fail("the stopped runner kept waiting on its step")
    return code, pgid


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
@pytest.mark.parametrize("mode", ["default", "stopped", "interrupted"])
def test_a_stop_reaches_a_step_the_wrapper_forked(
    tmp_path: Path, host: Path, sig: signal.Signals, mode: str
) -> None:
    configure(host, f"{wrapper(tmp_path, FORKS)}\n")
    pids = tmp_path / "pids"
    code, pgid = stop_run(
        [sys.executable, "-c", RUNNER, str(SCRIPTS), mode, "sh", "-c", SLEEPER, "sh", str(pids)],
        pids,
        sig,
    )
    assert code == (99 if mode == "interrupted" else 128 + sig)
    assert gone(pgid), "a process of the step's group outlived the stopped run"
    assert len(wrapped(tmp_path, probes=True)) == 2  # the probe, then the step


def test_a_stop_reaches_an_unwrapped_steps_group(tmp_path: Path, host: Path) -> None:
    pids = tmp_path / "pids"
    code, pgid = stop_run(
        [
            sys.executable,
            "-c",
            RUNNER,
            str(SCRIPTS),
            "default",
            "sh",
            "-c",
            f"({SLEEPER}) & wait",
            "sh",
            str(pids),
        ],
        pids,
        signal.SIGTERM,
    )
    assert code == 128 + signal.SIGTERM
    assert gone(pgid)


def heavy_command(*words: str) -> list[str]:
    return [sys.executable, str(SCRIPTS / "approvals.py"), "heavy", *words]


def test_the_heavy_command_runs_outside_any_repo_with_the_commands_exit(
    tmp_path: Path, host: Path
) -> None:
    configure(host, f"{wrapper(tmp_path)} --scope\n")
    ran = subprocess.run(
        heavy_command("sh", "-c", "echo $LOOP_HEAVY_WRAPPED; exit 9"),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (ran.returncode, ran.stdout) == (9, "1\n"), ran.stderr
    assert wrapped(tmp_path)[-1] == "--scope sh -c echo $LOOP_HEAVY_WRAPPED; exit 9"


def test_the_heavy_command_forwards_a_stop(tmp_path: Path, host: Path) -> None:
    configure(host, f"{wrapper(tmp_path, FORKS)}\n")
    pids = tmp_path / "pids"
    code, pgid = stop_run(heavy_command("sh", "-c", SLEEPER, "sh", str(pids)), pids, signal.SIGTERM)
    assert code == 128 + signal.SIGTERM
    assert gone(pgid)


def test_an_exception_while_a_step_runs_kills_its_group(
    tmp_path: Path, host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(host, f"{wrapper(tmp_path, FORKS)}\n")
    pids = tmp_path / "pids"
    real = subprocess.Popen.communicate

    def raising(self: subprocess.Popen[Any], *args: Any, **kw: Any) -> Any:
        while not pids.exists() or not pids.read_text().strip():
            time.sleep(0.05)
        raise RuntimeError("stopped while waiting")

    monkeypatch.setattr(approvals.subprocess.Popen, "communicate", raising)
    began = time.monotonic()
    with pytest.raises(RuntimeError):
        approvals.run_heavy(["sh", "-c", SLEEPER, "sh", str(pids)])
    monkeypatch.setattr(approvals.subprocess.Popen, "communicate", real)
    # killed, not waited out: the step would sleep 600 s
    assert time.monotonic() - began < STOP_BOUND_S
    assert gone(int(pids.read_text().split()[1]))


def test_the_heavy_command_exits_as_a_shell_does_for_a_command_a_signal_ended(
    tmp_path: Path,
) -> None:
    ran = subprocess.run(
        heavy_command("sh", "-c", "kill -KILL $$"), cwd=tmp_path, capture_output=True, check=False
    )
    assert ran.returncode == 128 + signal.SIGKILL
