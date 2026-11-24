"""A run queued on a host gate slot, and a mutate.py run, keep writing where wait-for's stall check reads (#1123)."""

from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import io
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import approvals
from test_omnigent_agent import omnigent_agent

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "gatekeeper" / "scripts"
LOOP_SCRIPTS = (
    Path(__file__).resolve().parents[2] / "loop" / "skills" / "engineering-loop" / "scripts"
)


@pytest.fixture
def mutate_module() -> Any:
    spec = importlib.util.spec_from_file_location("mutate_under_test", LOOP_SCRIPTS / "mutate.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def held_for(path: Path, seconds: float) -> None:
    """Hold the flock on `path` for `seconds`, then release it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    threading.Timer(seconds, os.close, (fd,)).start()


@pytest.mark.parametrize("land", [False, True], ids=["polling-gate", "land-blocking-in-the-kernel"])
def test_heavy_slot_repeats_its_waiting_line_while_it_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, land: bool
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("GATE_SLOTS", "1")
    monkeypatch.delenv(approvals.SLOT_HELD, raising=False)
    monkeypatch.setattr(approvals, "SLOT_SAY_SECONDS", 0.3)
    held_for(Path(approvals.slot_path()), 1.5)
    out = io.StringIO()
    with approvals.heavy_slot("gate", out, land=land):
        pass
    assert out.getvalue().count("local-ci: gate: waiting for the host gate slot") >= 3


def test_a_free_slot_says_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv(approvals.SLOT_HELD, raising=False)
    out = io.StringIO()
    with approvals.heavy_slot("gate", out):
        pass
    assert out.getvalue() == ""


def test_mutate_take_short_repeats_its_waiting_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate_module: Any
) -> None:
    monkeypatch.delenv(mutate_module.SLOT_HELD, raising=False)
    monkeypatch.setattr(mutate_module.approvals(), "SLOT_SAY_SECONDS", 0.3)
    slot = tmp_path / "short.lock"
    held_for(slot, 1.5)
    said: list[int] = []
    fd = mutate_module.take_short(str(slot), lambda: said.append(1))
    assert fd is not None
    os.close(fd)
    assert len(said) >= 3


def test_mutate_echoes_each_log_line_to_stdout(tmp_path: Path) -> None:
    """A detached run's recipe log sees only stdout, and wait-for kills a run whose log is silent."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("x = 1\n")
    (repo / ".gitignore").write_text("tmp/\n")
    run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True, text=True)
    run("git", "init", "-q")
    run("git", "add", ".")
    run("git", "-c", "user.name=t", "-c", "user.email=t@e.com", "commit", "-qm", "c")
    (repo / "calc.py").write_text("x = 2\n")
    patch = repo / "tmp" / "p.patch"
    patch.parent.mkdir()
    patch.write_text(run("git", "diff").stdout)
    run("git", "checkout", "--", "calc.py")
    done = subprocess.run(
        [sys.executable, str(LOOP_SCRIPTS / "mutate.py"), "--test", "grep -q 'x = 1' calc.py",
         "--log", str(repo / "tmp" / "log"), str(patch)],
        cwd=repo, capture_output=True, text=True, timeout=60, check=False,
        env={**os.environ, "XDG_CACHE_HOME": str(tmp_path / "cache")},
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert f"{patch}: exit 1 killed" in done.stdout.splitlines()


def test_mutate_runs_its_git_outside_its_own_process_group(
    tmp_path: Path, mutate_module: Any
) -> None:
    """wait-for's SIGTERM reaches the whole group: a git inside it, killed mid-restore, leaves the mutation."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    where = mutate_module.git("rev-parse", "--git-dir", cwd=repo)
    assert where.returncode == 0
    seen: list[Any] = []
    real = subprocess.run
    subprocess.run = lambda *a, **k: (seen.append(k), real(*a, **k))[1]  # type: ignore[assignment]
    try:
        mutate_module.git("status", cwd=repo)
    finally:
        subprocess.run = real  # type: ignore[assignment]
    assert seen[0].get("start_new_session") is True


def test_land_wait_repeats_an_unchanged_state_every_say_interval(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def landing(pr: Any, only: bool = False) -> tuple[int, bool]:
        print("PR #1: another land holds the landing lock (#7), taken 3 minutes ago")
        return approvals.WAITING, True

    monkeypatch.setattr(approvals, "landing", landing)
    monkeypatch.setattr(approvals, "POLL", 0.02)
    monkeypatch.setattr(approvals, "poll_seconds", lambda *_: approvals.POLL)  # no real quota read
    monkeypatch.setattr(approvals, "CEILING", 1)
    monkeypatch.setattr(approvals, "SLOT_SAY_SECONDS", 0.2)
    assert approvals.land(1, wait=True) == approvals.WAITING
    said = capsys.readouterr().out.count("another land holds the landing lock")
    assert 3 <= said <= 7  # about once per 0.2 s over 1 s, not once per 0.02 s poll


class Heard(io.StringIO):
    """A stand-in for the process's own stdout that sets `thrice` once `line` has been written three times: a test
    waits on it instead of sleeping, so a loaded host only makes it slower, never red."""

    def __init__(self, line: str) -> None:
        super().__init__()
        self.line, self.thrice = line, threading.Event()

    def write(self, text: str) -> int:
        written = super().write(text)
        if self.getvalue().count(self.line) >= 3:
            self.thrice.set()
        return written


def test_saying_repeats_its_line_while_the_block_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    heard = Heard("Tree t: gating, log l")
    monkeypatch.setattr(sys, "__stdout__", heard)
    monkeypatch.setattr(approvals, "SLOT_SAY_SECONDS", 0.05)
    # land --wait captures sys.stdout until landing returns: the line goes to the process's own stdout
    with contextlib.redirect_stdout(io.StringIO()), approvals.saying("Tree t: gating, log l"):
        assert heard.thrice.wait(30)


def test_the_say_interval_is_well_under_the_stall_default() -> None:
    assert approvals.SLOT_SAY_SECONDS == 60 and omnigent_agent.WAIT_FOR_STALL == 300


def test_repeating_stops_its_thread_when_the_block_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    """A thread left running keeps saying "waiting" or "gating" after its block, beside the next block's own."""
    monkeypatch.setattr(approvals, "SLOT_SAY_SECONDS", 0.02)
    calls: list[int] = []
    twice = threading.Event()
    before = set(threading.enumerate())
    left: list[threading.Thread] = []

    def block() -> None:
        with approvals.repeating(lambda: (calls.append(1), len(calls) >= 2 and twice.set())):
            assert twice.wait(30)
        left.extend(set(threading.enumerate()) - before - {threading.current_thread()})

    runner = threading.Thread(target=block, daemon=True)
    runner.start()
    runner.join(30)
    assert not runner.is_alive(), "repeating waits on a thread that never stops"
    assert left == []  # gone as the block ends, not only some time after
    after = len(calls)
    threading.Event().wait(0.2)
    assert len(calls) == after
