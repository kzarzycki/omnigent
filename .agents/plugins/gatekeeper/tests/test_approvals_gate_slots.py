"""The host gate has `GATE_SLOTS` heavy slots, a third of the cores by default (#1138): a step or a mutation run takes
the first free one, waits only when every one is held, and a record is never torn when two gates write it."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import approvals, gatekeeper_md
from test_approvals_gate_steps import steps  # noqa: F401
from test_mutate import clean, log, mutate, patch, policy, recording, repo, slots  # noqa: F401


def hold(number: int, seconds: float = 3.0) -> Callable[[], None]:
    """Hold heavy slot `number` the way a running gate does; the returned function frees it, and it frees itself after
    `seconds`, so a step that wrongly waits for it fails its assertion instead of hanging."""
    path = Path(approvals.slot_path(number))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    freed = threading.Lock()

    def release() -> None:
        if freed.acquire(blocking=False):
            os.close(fd)

    timer = threading.Timer(seconds, release)
    timer.daemon = True
    timer.start()
    return release


def test_a_second_step_takes_the_next_free_slot_without_waiting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GATE_SLOTS", "2")
    first = hold(1)
    try:
        with approvals.heavy_slot("unit") as (path, waited):
            assert path == approvals.slot_path(2) and waited == 0
    finally:
        first()
    assert "waiting" not in capsys.readouterr().err


def test_with_every_slot_held_a_step_waits_and_says_so_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GATE_SLOTS", "2")
    held = [hold(1, 3.0), hold(2, 1.0)]
    try:
        with approvals.heavy_slot("unit") as (path, waited):
            assert path == approvals.slot_path(2) and waited >= 1
    finally:
        held[0]()
    assert capsys.readouterr().err.count("local-ci: unit: waiting for the host gate slot") == 1


def test_gate_slots_1_keeps_one_gate_at_a_time(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GATE_SLOTS", "1")
    hold(1, 1.0)
    began = time.monotonic()
    with approvals.heavy_slot("unit") as (path, _):
        assert path == approvals.slot_path(1)
    assert time.monotonic() - began >= 1.0
    assert "waiting for the host gate slot" in capsys.readouterr().err


@pytest.mark.parametrize(("cores", "slots"), [(12, 4), (8, 2), (3, 1), (2, 1), (1, 1), (None, 1)])
def test_the_slot_count_defaults_to_a_third_of_the_cores_and_at_least_one(
    monkeypatch: pytest.MonkeyPatch, cores: int | None, slots: int
) -> None:
    monkeypatch.delenv("GATE_SLOTS")
    monkeypatch.setattr(os, "cpu_count", lambda: cores)
    assert approvals.gate_slots() == slots


@pytest.mark.parametrize("said", ["", "0", "-2", "many", "1.5"])
def test_a_gate_slots_that_is_not_a_whole_number_from_1_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, said: str
) -> None:
    monkeypatch.setenv("GATE_SLOTS", said)
    monkeypatch.setattr(os, "cpu_count", lambda: 12)
    assert approvals.gate_slots() == 4


def test_a_run_under_the_second_slot_takes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATE_SLOTS", "2")
    held = [hold(1), hold(2)]
    monkeypatch.setenv(approvals.SLOT_HELD, approvals.slot_path(2))
    try:
        began = time.monotonic()
        with approvals.heavy_slot("unit") as (path, waited):
            assert (path, waited) == (approvals.slot_path(2), 0)
        assert time.monotonic() - began < 1.0
    finally:
        for release in held:
            release()


def test_two_gates_on_one_tree_and_command_one_per_slot_leave_one_valid_record(
    steps: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each gate sleeps in its slot until the other has started, so the two overlap on slots 1 and 2."""
    monkeypatch.setenv("GATE_SLOTS", "2")
    seen, script = tmp_path / "seen", tmp_path / "gate.py"
    script.write_text(
        "import os, pathlib, sys, time\n"
        "seen = pathlib.Path(sys.argv[1])\n"
        "seen.open('a').write(os.environ['LOOP_GATE_SLOT_HELD'] + '\\n')\n"
        "for _ in range(200):\n"
        "    if len(seen.read_text().split()) >= 2:\n"
        "        break\n"
        "    time.sleep(0.05)\n"
    )
    command = f"{sys.executable} {script} {seen}"
    steps["set_policy"](gatekeeper_md(f"loop:\n  gate: {command}\n"))
    codes: list[int] = []
    runs = [
        threading.Thread(target=lambda: codes.append(approvals.main(["local-ci", "--record"])))
        for _ in range(2)
    ]
    for run in runs:
        run.start()
    for run in runs:
        run.join(60)
    assert codes == [0, 0]
    assert sorted(seen.read_text().split()) == [approvals.slot_path(1), approvals.slot_path(2)]
    files = sorted(approvals.local_ci_dir().iterdir())
    assert [f.name for f in files] == [approvals.recorded(steps["tree"], command).name]
    assert json.loads(files[0].read_text())["exit"] == 0


def test_a_record_is_replaced_whole_never_truncated_in_place(
    steps: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader meeting a record mid-write would find it empty: the write goes to a file of its own, which replaces the
    record in one step, so the old record is whole until then."""
    record = {"tree": steps["tree"], "command": "gate", "exit": 0}
    approvals.stored(record)
    where = approvals.recorded(record["tree"], record["command"])
    replaced: list[dict[str, Any]] = []
    real = os.replace

    def replace(source: Any, target: Any) -> None:
        replaced.append(json.loads(where.read_text()))
        assert json.loads(Path(source).read_text())["seconds"] == 2
        real(source, target)

    monkeypatch.setattr(approvals.os, "replace", replace)
    approvals.stored({**record, "seconds": 2})
    assert replaced == [record]
    assert json.loads(where.read_text())["seconds"] == 2
    assert [f.name for f in where.parent.iterdir()] == [where.name]


def test_mutate_takes_a_free_slot_when_the_first_is_held(repo: Path, tmp_path: Path) -> None:
    policy(repo, 0)
    first = patch(repo, "add.patch", "a + b", "a - b")
    record = tmp_path / "slots"
    held = hold(1, 8.0)
    try:
        # a run that took only slot 1 would wait for its holder to let go, and name that slot
        result = mutate(repo, first, test=recording(record), env={"GATE_SLOTS": "2"}, timeout=30)
    finally:
        held()
    assert result.returncode == 0, result.stderr
    assert slots(record) == [approvals.slot_path(2)]
    assert not any("waiting" in line for line in log(repo))
    assert clean(repo)


@pytest.mark.parametrize("slots", ["1", "2"])
def test_a_waiting_land_takes_the_next_free_slot_ahead_of_a_waiting_pr_gate(
    monkeypatch: pytest.MonkeyPatch, slots: str
) -> None:
    """A land holds the landing lock while its gate waits, so it takes a freed slot first though the PR gate waited
    longer. With one slot the land blocks in the kernel; with several it polls, and the slots free between two polls
    of the PR gate, so a plain race would give the PR gate the first."""
    monkeypatch.setenv("GATE_SLOTS", slots)
    for number in range(1, int(slots) + 1):
        hold(number, 1.12)  # 1.52 s before, 0.4 s less, so the poll phases (.12, .1) stay the same
    order: list[str] = []

    def gate(name: str, land: bool) -> None:
        fd, _ = approvals.take_slot(land=land)
        order.append(name)
        time.sleep(0.4)
        assert fd is not None
        os.close(fd)

    pr = threading.Thread(target=gate, args=("pr", False), daemon=True)
    land = threading.Thread(target=gate, args=("land", True), daemon=True)
    pr.start()
    time.sleep(0.3)
    land.start()
    for thread in (pr, land):
        thread.join(20)
    assert order == ["land", "pr"]


def test_a_land_takes_a_free_slot_at_once_on_several_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    """A land yields to no one, its own mark included: with every slot free it holds one without waiting."""
    monkeypatch.setenv("GATE_SLOTS", "2")
    got: list[str] = []
    waited: list[bool] = []
    thread = threading.Thread(
        target=lambda: got.append(approvals.take_slot(lambda: waited.append(True), land=True)[1]),
        daemon=True,
    )
    thread.start()
    thread.join(5)
    assert got == [approvals.slot_path(1)] and not waited


def test_a_waiting_land_on_one_slot_beats_a_blocking_flock_that_started_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gate wrapper's own flock is woken by the kernel the moment the slot frees, so a polling land would lose every
    race to it; a land on one slot blocks in the kernel too, and the one that waited longer gets the slot."""
    monkeypatch.setenv("GATE_SLOTS", "1")
    hold(1, 1.0)
    order: list[str] = []

    def land() -> None:
        fd, _ = approvals.take_slot(land=True)
        order.append("land")
        time.sleep(0.3)
        assert fd is not None
        os.close(fd)

    def wrapper() -> None:
        fd = os.open(approvals.slot_path(1), os.O_WRONLY | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        order.append("wrapper")
        os.close(fd)

    threads = [threading.Thread(target=land), threading.Thread(target=wrapper)]
    threads[0].start()
    time.sleep(0.2)
    threads[1].start()
    for thread in threads:
        thread.join(20)
    assert order == ["land", "wrapper"]


def test_a_run_under_any_slot_is_held_whatever_gate_slots_it_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parent took `gate.3.lock` under the default count; a child under a wrapper's `GATE_SLOTS=1` still runs under
    it. Only a `gate.<n>.lock` of the slot directory counts."""
    monkeypatch.setenv("GATE_SLOTS", "1")
    monkeypatch.setenv(approvals.SLOT_HELD, approvals.slot_path(3))
    assert approvals.held_slot() == approvals.slot_path(3)
    for other in (approvals.slot_file("gate.short.lock"), "/elsewhere/gate.1.lock", "gate.x.lock"):
        monkeypatch.setenv(approvals.SLOT_HELD, other)
        assert approvals.held_slot() is None


def test_this_repos_gate_wrapper_takes_no_slot_under_any_gate_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mise run test` runs pytest behind tools/gate_slot.py: under a gate that holds `gate.2.lock` it takes none,
    though `gate.1.lock` is held, and a bare run takes the free slot 2 and tells its command."""
    wrapper = Path(__file__).resolve().parents[2] / "tools" / "gate_slot.py"
    hold(1, 30.0)
    env = {**os.environ, "GATE_SLOTS": "2"}
    # prints the slot it is told it runs under, and whether that lock is still held (a try at it fails) by the wrapper
    probe = (
        "import fcntl, os\n"
        "path = os.environ['LOOP_GATE_SLOT_HELD']\n"
        "fd = os.open(path, os.O_WRONLY)\n"
        "try:\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "    print(path, 'free')\n"
        "except BlockingIOError:\n"
        "    print(path, 'held')\n"
    )
    show = [sys.executable, "-c", probe]
    bare = subprocess.run(
        [sys.executable, str(wrapper), *show],
        env={k: v for k, v in env.items() if k != approvals.SLOT_HELD},
        capture_output=True, text=True, timeout=10, check=False,
    )  # fmt: skip
    # the lock survives the exec: pytest under it holds the slot, not just the variable naming it
    assert bare.stdout.split() == [approvals.slot_path(2), "held"] and "waiting" not in bare.stderr
    # every slot held, one of them by this run's parent: a second slot would wait for good
    hold(2, 30.0)
    held = subprocess.run(
        [sys.executable, str(wrapper), *show],
        env={**env, approvals.SLOT_HELD: approvals.slot_path(2)},
        capture_output=True, text=True, timeout=10, check=False,
    )  # fmt: skip
    assert held.stdout.split()[0] == approvals.slot_path(2) and "waiting" not in held.stderr


def test_a_lands_gate_step_reaches_take_slot_as_a_land(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag travels ran_step -> heavy_slot -> take_slot; a link dropping it would switch land priority off."""
    seen: list[bool] = []

    def take_slot(waiting: Any = None, land: bool = False) -> tuple[None, str]:
        seen.append(land)
        return None, approvals.slot_path()

    monkeypatch.setattr(approvals, "take_slot", take_slot)
    real = subprocess.run
    monkeypatch.setattr(
        approvals.subprocess,
        "run",
        lambda words, **kw: (
            subprocess.CompletedProcess(words, 0) if words == ["true"] else real(words, **kw)
        ),
    )
    step = {"name": "unit", "words": ["true"], "sets": {}, "legacy": False, "command": "true"}
    approvals.ran_step(step, land=True)
    approvals.ran_step(step)
    assert seen == [True, False]


def test_a_failed_record_write_leaves_no_partial_file(
    steps: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    record = {"tree": steps["tree"], "command": "gate", "exit": 0}
    approvals.stored(record)
    where = approvals.recorded(record["tree"], record["command"])

    def replace(*_args: Any) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(approvals.os, "replace", replace)
    with pytest.raises(OSError):
        approvals.stored({**record, "seconds": 2})
    assert json.loads(where.read_text()) == record
    assert [f.name for f in where.parent.iterdir()] == [where.name]


def test_mutate_under_the_second_slot_stays_in_it_and_hands_it_down(
    repo: Path, tmp_path: Path
) -> None:
    """A run under `gate.2.lock` runs every patch in that slot, not the short lane, so a heavy wrapper under it takes
    none."""
    first = patch(repo, "add.patch", "a + b", "a - b")
    record = tmp_path / "slots"
    held = approvals.slot_path(2)
    result = mutate(
        repo, first, test=recording(record),
        env={"GATE_SLOTS": "2", approvals.SLOT_HELD: held}, timeout=30,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert log(repo)[2] == "heavy: run under a holder of the heavy slot"
    assert slots(record) == [held]


def test_the_repos_test_task_runs_pytest_behind_the_slot_wrapper() -> None:
    """The wrapper above only counts if `mise run test` runs pytest behind it, not behind a lock of its own."""
    root = Path(__file__).resolve().parents[2]
    run = tomllib.loads((root / "mise.toml").read_text())["tasks"]["test"]["run"]
    assert run.startswith("uv run python tools/gate_slot.py pytest ")


# the slot rule's shared table (#1232): the template's gate-slot reads the same file, so both give one count per row
TABLE = Path(__file__).resolve().parent / "fixtures" / "gate-slots.tsv"


def table_rows() -> list[list[str]]:
    lines = TABLE.read_text().splitlines()
    assert lines[0].split("\t") == [
        "cores", "memory_gb", "cost_cores", "cost_gb", "reserve_gb", "gate_slots", "count", "limit",
    ]  # fmt: skip
    return [line.split("\t") for line in lines[1:]]


def given(cell: str) -> Fraction | None:
    """A table cell as slot_rule takes it: `-` is none."""
    return None if cell == "-" else Fraction(cell)


@pytest.mark.parametrize("row", table_rows(), ids="-".join)
def test_the_slot_rule_gives_each_row_of_the_shared_table_its_count_and_limit(
    row: list[str],
) -> None:
    cores, memory, cost_cores, cost_gb, reserve, said, count, limit = row
    cost = None if cost_cores == "-" else (Fraction(cost_cores), Fraction(cost_gb))
    got = approvals.slot_rule(
        int(cores), given(memory), cost, given(reserve), "" if said == "-" else said
    )
    assert got[:2] == (int(count), limit)


def test_the_shared_table_holds_a_row_each_limit_sets() -> None:
    limits = {row[7] for row in table_rows()}
    assert limits == {"cores", "memory", "GATE_SLOTS", "off"}


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str, int, int], None]:
    """A checkout whose loop.md holds `line`, on a host of `cores` cores and `memory` GB, with GATE_SLOTS unset; the
    tests run in it."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GATE_SLOTS")

    def make(line: str, cores: int, memory: int) -> None:
        (tmp_path / "docs" / "agents").mkdir(parents=True, exist_ok=True)
        (tmp_path / "docs" / "agents" / "loop.md").write_text(f"# Loop\n\nOwner: someone\n{line}\n")
        monkeypatch.setattr(os, "cpu_count", lambda: cores)
        monkeypatch.setattr(approvals, "total_memory", lambda: Fraction(memory))

    return make


def test_cores_set_the_count_where_the_gate_costs_little_memory(
    host: Callable[[str, int, int], None],
) -> None:
    host("Gate cost: 4 cores, 2 GB", 16, 64)
    assert approvals.slot_plan() == (
        4, "slots 4: cores (16 cores, 64 GB, cost 4 cores 2 GB, reserve 16 GB)"
    )  # fmt: skip
    assert approvals.gate_slots() == 4


def test_memory_sets_the_count_where_the_gate_starts_a_database(
    host: Callable[[str, int, int], None],
) -> None:
    """16 GB less the 4 GB reserve holds two 6 GB runs, though the cores would take eight."""
    host("Gate cost: 2 cores, 6 GB", 16, 16)
    assert approvals.slot_plan() == (
        2, "slots 2: memory (16 cores, 16 GB, cost 2 cores 6 GB, reserve 4 GB)"
    )  # fmt: skip


def test_the_lines_reserve_replaces_the_default(host: Callable[[str, int, int], None]) -> None:
    host("Gate cost: `2 cores, 6 GB, reserve 2 GB`", 16, 32)
    assert approvals.slot_plan()[1] == (
        "slots 5: memory (16 cores, 32 GB, cost 2 cores 6 GB, reserve 2 GB)"
    )


def test_without_a_cost_line_the_count_stays_a_third_of_the_cores(
    host: Callable[[str, int, int], None],
) -> None:
    host("Gate base: GATE_BASE", 12, 16)
    assert approvals.slot_plan() == (
        4, "slots 4: cores (12 cores, 16 GB, cost none, reserve 4 GB)"
    )  # fmt: skip


def test_gate_slots_still_overrides_and_off_says_so(
    host: Callable[[str, int, int], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    host("Gate cost: 2 cores, 6 GB", 16, 16)
    monkeypatch.setenv("GATE_SLOTS", "3")
    assert approvals.slot_plan()[1].startswith("slots 3: GATE_SLOTS (16 cores, 16 GB,")
    monkeypatch.setenv("GATE_SLOTS", "off")
    assert approvals.slot_plan() == (
        0, "slots 0: off (16 cores, 16 GB, cost 2 cores 6 GB, reserve 4 GB)"
    )  # fmt: skip


@pytest.mark.parametrize(
    "line", ["Gate cost: 4 cores", "Gate cost: four cores, 2 GB", "Gate cost: 0 cores, 2 GB",
             "Gate cost: 2 cores, 0.0 GB", "Gate cost: 2 cores, 6 GB, reserve GB"],
)  # fmt: skip
def test_a_cost_line_off_its_form_is_refused_naming_it(
    host: Callable[[str, int, int], None], line: str
) -> None:
    host(line, 16, 16)
    with pytest.raises(approvals.Refused, match=re.escape(repr(line))):
        approvals.gate_slots()


def test_outside_a_checkout_no_cost_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert not any((d / ".git").exists() for d in (tmp_path, *tmp_path.parents))
    monkeypatch.chdir(tmp_path)
    assert approvals.gate_cost() == (None, None)


def test_the_cost_is_read_from_the_checkouts_root_below_it(
    host: Callable[[str, int, int], None], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host("Gate cost: 2 cores, 6 GB", 16, 16)
    (tmp_path / "src" / "deep").mkdir(parents=True)
    monkeypatch.chdir(tmp_path / "src" / "deep")
    assert approvals.gate_cost() == ((Fraction(2), Fraction(6)), None)


def test_heavy_plan_prints_the_line_and_runs_nothing(
    host: Callable[[str, int, int], None], capsys: pytest.CaptureFixture[str]
) -> None:
    host("Gate cost: 2 cores, 6 GB", 16, 16)
    assert approvals.main(["heavy", "--plan"]) == 0
    assert capsys.readouterr().out == (
        "slots 2: memory (16 cores, 16 GB, cost 2 cores 6 GB, reserve 4 GB)\n"
    )


def test_a_wait_for_a_slot_says_the_plan_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GATE_SLOTS", "1")
    hold(1, 1.0)
    with approvals.heavy_slot("unit"):
        pass
    said = capsys.readouterr().err
    assert "local-ci: unit: waiting for the host gate slot: slots 1: GATE_SLOTS (" in said


def held_elsewhere(path: str) -> subprocess.Popen[str]:
    """Another process holding `path`'s flock until its stdin closes; it prints `held` once it holds it."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    code = (
        "import fcntl, os, sys\n"
        "fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('held', flush=True)\n"
        "sys.stdin.read()\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", code, path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    assert holder.stdout is not None and holder.stdout.readline() == "held\n"
    return holder


def test_with_gate_slots_off_a_command_runs_while_another_process_holds_slot_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    holder = held_elsewhere(approvals.slot_path(1))
    try:
        monkeypatch.setenv("GATE_SLOTS", "off")
        began = time.monotonic()
        with approvals.heavy_slot("unit") as (path, waited):
            assert (path, waited) == ("", 0)
        assert approvals.take_slot(land=True) == (None, "")
        wrapper = Path(__file__).resolve().parents[2] / "tools" / "gate_slot.py"
        ran = subprocess.run(
            [sys.executable, str(wrapper), sys.executable, "-c", "print('ran')"],
            env={**os.environ, "GATE_SLOTS": "off"},
            capture_output=True, text=True, timeout=10, check=False,
        )  # fmt: skip
        assert (ran.returncode, ran.stdout) == (0, "ran\n")
        assert time.monotonic() - began < 5
        assert "waiting" not in capsys.readouterr().err + ran.stderr
    finally:
        holder.communicate("")


def test_with_gate_slots_off_mutate_takes_no_short_lane_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop_scripts = (
        Path(approvals.__file__).resolve().parents[4] / "loop/skills/engineering-loop/scripts"
    )
    sys.path.insert(0, str(loop_scripts))
    import mutate as module

    lane = str(tmp_path / "gate.short.lock")
    holder = held_elsewhere(lane)
    try:
        monkeypatch.setenv("GATE_SLOTS", "off")
        assert module.take_short(lane, lambda: pytest.fail("waited")) is None
        monkeypatch.setenv("GATE_SLOTS", "1")
        fd = module.take_short(lane + ".free", lambda: None)
        assert fd is not None
        os.close(fd)
    finally:
        holder.communicate("")


def test_total_memory_is_page_size_times_physical_pages_and_runs_no_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {
        "SC_PAGE_SIZE": 4096,
        "SC_PHYS_PAGES": 16303428 * 1024 // 4096,
    }  # 15.55 GiB rounds to 16

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("total_memory must run no command")

    monkeypatch.setattr(approvals.os, "sysconf", lambda name: pages[name])
    monkeypatch.setattr(approvals.subprocess, "run", refuse)
    assert approvals.total_memory() == 16


def test_unread_memory_limits_by_the_cores_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_name: str) -> int:
        raise OSError("no sysconf")

    monkeypatch.setattr(approvals.os, "sysconf", broken)
    assert approvals.total_memory() is None
    assert approvals.plan_number(None) == "?"


def test_heavy_plan_with_a_command_is_refused_and_runs_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ran = tmp_path / "ran"
    assert approvals.main(["heavy", "--plan", "touch", str(ran)]) == 1
    assert "heavy --plan takes no command" in capsys.readouterr().out
    assert not ran.exists()
