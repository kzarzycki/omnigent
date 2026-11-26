"""events.py: a step's state as a git ref, written in one clone and read in another through a bare remote (#1375)."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "loop_events",
    Path(__file__).resolve().parent.parent / "skills" / "gatekeeper" / "scripts" / "events.py",
)
events = importlib.util.module_from_spec(spec)
sys.modules["loop_events"] = events
spec.loader.exec_module(events)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def clones(tmp_path: Path) -> tuple[Path, Path, Path]:
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    made = []
    for name in ("a", "b"):
        clone = tmp_path / name
        git(tmp_path, "init", "-q", str(clone))
        git(clone, "remote", "add", "origin", str(bare))
        made.append(clone)
    return bare, made[0], made[1]


def test_event_round_trips_between_clones(clones: tuple[Path, Path, Path]) -> None:
    _, a, b = clones
    events.emit(
        "lead",
        "feat/x",
        "needs-owner",
        url="https://example.test/1",
        question="Which one?\n\nA or B.\n",
        cwd=a,
    )
    (got,) = events.read("lead", cwd=b)
    assert (got["state"], got["url"], got["question"], got["branch"], got["kind"]) == (
        "needs-owner",
        "https://example.test/1",
        "Which one?\n\nA or B.\n",
        "feat/x",
        "lead",
    )
    assert got["seq"] == 1 and got["beat_s"] == events.BEATS["lead"]


def test_read_fetches_only_a_moved_sha(
    clones: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, a, b = clones
    events.emit("land", "x", "waiting", cwd=a)
    first = events.read(cwd=b)[0]
    fetched = []
    real = events.fetch_event
    monkeypatch.setattr(
        events, "fetch_event", lambda cwd, ref: fetched.append(ref) or real(cwd, ref)
    )
    again = events.read(cwd=b)[0]
    assert fetched == [] and again["changed"] == first["changed"]
    time.sleep(1.1)
    events.emit("land", "x", "waiting", cwd=a)
    moved = events.read(cwd=b)[0]
    assert fetched == [moved["ref"]] and moved["seq"] == 2 and moved["changed"] > first["changed"]


def test_filtered_read_keeps_the_other_kinds_cached(
    clones: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, a, b = clones
    events.emit("lead", "x", "waiting", cwd=a)
    events.emit("gate", "x", "waiting", cwd=a)
    first = {e["kind"]: e["changed"] for e in events.read(cwd=b)}
    events.read("lead", cwd=b)
    time.sleep(1.1)
    fetched = []
    real = events.fetch_event
    monkeypatch.setattr(
        events, "fetch_event", lambda cwd, ref: fetched.append(ref) or real(cwd, ref)
    )
    again = {e["kind"]: e["changed"] for e in events.read(cwd=b)}
    assert fetched == [] and again == first


def test_read_with_origin_down_lists_the_local_events_only(
    clones: tuple[Path, Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, a, _ = clones
    events.emit("lead", "y", "waiting", cwd=a)
    events.emit("probe", "loop/x", "waiting", cwd=a)
    # the gatekeeper's refs share the namespace and are not events
    head = git(a, "rev-parse", "refs/loop/lead/y").strip()
    git(a, "update-ref", "refs/loop/records/abc-key", head)
    git(a, "update-ref", "refs/loop/landing", head)
    git(a, "remote", "set-url", "origin", str(tmp_path / "nowhere.git"))
    assert [e["ref"] for e in events.read(cwd=a)] == [
        "refs/loop/lead/y",
        "refs/loop/probe/loop/x",
    ]
    assert [e["ref"] for e in events.read("probe", cwd=a)] == ["refs/loop/probe/loop/x"]
    assert capsys.readouterr().err.count("origin not read") == 2


def test_beat_leaves_no_history(clones: tuple[Path, Path, Path]) -> None:
    bare, a, _ = clones
    events.emit("gate", "x", "waiting", cwd=a)
    events.emit("gate", "x", "waiting", cwd=a)
    ref = "refs/loop/gate/x"
    assert git(a, "rev-list", "--count", ref).strip() == "1"
    assert git(bare, "rev-list", "--count", ref).strip() == "1"
    assert git(a, "rev-parse", ref) == git(bare, "rev-parse", ref)


def waiting_on(clone: Path, kind: str, branch: str, timeout: float) -> tuple[int, str]:
    out = io.StringIO()
    code = events.wait(kind, branch, poll=0.1, timeout=timeout, cwd=clone, out=out)
    return code, out.getvalue()


def test_wait_ends_with_0_when_another_clone_emits_ok(clones: tuple[Path, Path, Path]) -> None:
    _, a, b = clones
    events.emit("land", "x", "waiting", cwd=a)
    threading.Timer(
        0.5, lambda: events.emit("land", "x", "ok", url="https://example.test/p", cwd=a)
    ).start()
    code, text = waiting_on(b, "land", "x", 10)
    assert code == 0 and "https://example.test/p" in text


def test_wait_ends_with_1_on_failed(clones: tuple[Path, Path, Path]) -> None:
    _, a, b = clones
    events.emit("land", "y", "failed", cwd=a)
    assert waiting_on(b, "land", "y", 5)[0] == 1


def test_wait_ends_with_2_on_needs_owner_and_prints_the_question(
    clones: tuple[Path, Path, Path],
) -> None:
    _, a, b = clones
    events.emit("lead", "y", "needs-owner", question="Go?\n", cwd=a)
    code, text = waiting_on(b, "lead", "y", 5)
    assert code == 2 and "Go?" in text


def test_wait_ends_with_4_on_a_waiting_event_nobody_rewrites(
    clones: tuple[Path, Path, Path],
) -> None:
    _, a, b = clones
    events.emit("gate", "z", "waiting", beat=0.3, cwd=a)
    assert waiting_on(b, "gate", "z", 10)[0] == 4


def test_wait_times_out_with_3_on_an_event_still_beating(clones: tuple[Path, Path, Path]) -> None:
    _, a, b = clones
    events.emit("gate", "q", "waiting", beat=60, cwd=a)
    assert waiting_on(b, "gate", "q", 0.5)[0] == 3


def test_wait_times_out_with_3_when_nothing_is_there(clones: tuple[Path, Path, Path]) -> None:
    _, _, b = clones
    assert waiting_on(b, "gate", "none", 0.3)[0] == 3


@pytest.mark.parametrize("zone", ["Europe/Warsaw", "America/New_York", "Australia/Sydney", "UTC"])
def test_a_fresh_waiting_event_is_not_silent_in_any_time_zone(
    clones: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, zone: str
) -> None:
    # the zones cover summer time in both hemispheres, in whatever season the test runs
    _, a, b = clones
    events.emit("land", "x", "waiting", cwd=a)
    monkeypatch.setenv("TZ", zone)
    time.tzset()
    try:
        assert waiting_on(b, "land", "x", 0.5)[0] == 3
    finally:
        monkeypatch.undo()
        time.tzset()


def test_parse_at_reads_the_utc_stamp_in_any_time_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    stamp = "2026-07-01T12:00:00Z"
    for zone in ("Europe/Warsaw", "Australia/Sydney", "UTC"):
        monkeypatch.setenv("TZ", zone)
        time.tzset()
        assert events.parse_at(stamp) == 1782907200
    monkeypatch.undo()
    time.tzset()


def test_wait_takes_the_later_at_of_the_local_and_the_remote_event(
    clones: tuple[Path, Path, Path],
) -> None:
    _, a, b = clones
    events.emit("land", "x", "waiting", cwd=b)
    time.sleep(1.1)
    events.emit("land", "x", "ok", cwd=a)
    assert waiting_on(b, "land", "x", 5)[0] == 0


def test_emit_pushes_under_a_lease_so_a_racing_writer_wins(
    clones: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bare, a, b = clones
    ref = "refs/loop/lead/x"
    stale = events.emit("lead", "x", "waiting", cwd=a)
    winner = events.emit("lead", "x", "ok", cwd=b)
    assert git(bare, "rev-parse", ref).strip() == winner
    # a's ls-remote answered before b pushed, so its lease names the sha origin no longer holds
    monkeypatch.setattr(events, "remote_refs", lambda cwd, pattern: {ref: stale})
    capsys.readouterr()
    events.emit("lead", "x", "waiting", cwd=a)
    assert git(bare, "rev-parse", ref).strip() == winner
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_unreadable_event_is_unknown_not_ended(clones: tuple[Path, Path, Path]) -> None:
    _, a, _ = clones
    tree = subprocess.run(
        ["git", "mktree"], cwd=a, input="", text=True, capture_output=True, check=True
    ).stdout.strip()
    commit = git(
        a, "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", "-m", "junk", tree
    ).strip()
    git(a, "update-ref", "refs/loop/lead/j", commit)
    assert events.event_at(a, commit)["state"] == "unknown"
    assert events.wait("lead", "j", poll=0.1, timeout=0.3, cwd=a, out=io.StringIO()) == 3


def test_emit_offline_is_not_a_failure(
    clones: tuple[Path, Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, a, _ = clones
    git(a, "remote", "set-url", "origin", str(tmp_path / "nowhere.git"))
    assert events.main(["--repo", str(a), "emit", "lead", "x", "waiting"]) == 0
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and err[0].startswith("events: refs/loop/lead/x written here, not pushed")
    local = json.loads(git(a, "show", "refs/loop/lead/x:event.json"))
    assert local["state"] == "waiting"
    # a native lead in the same clone still waits on it
    events.emit("lead", "x", "ok", cwd=a)
    assert events.wait("lead", "x", poll=0.1, timeout=2, cwd=a, out=io.StringIO()) == 0


def test_clear_removes_refs_here_and_on_origin(clones: tuple[Path, Path, Path]) -> None:
    bare, a, b = clones
    events.emit("lead", "x", "waiting", cwd=a)
    events.emit("land", "x", "waiting", cwd=a)
    events.emit("land", "keep", "waiting", cwd=a)
    events.read(cwd=b)
    events.clear("x", cwd=a)
    events.clear("x", cwd=b)
    assert git(bare, "for-each-ref", "--format=%(refname)", "refs/loop/").split() == [
        "refs/loop/land/keep"
    ]
    assert git(a, "for-each-ref", "--format=%(refname)", "refs/loop/").split() == [
        "refs/loop/land/keep"
    ]
    assert git(b, "for-each-ref", "--format=%(refname)", "refs/loop-remote/").split() == [
        "refs/loop-remote/land/keep"
    ]


def test_the_cli_emits_reads_and_waits_between_clones(
    clones: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _, a, b = clones
    question = tmp_path / "q.md"
    question.write_text("Go?\n")
    script = Path(events.__file__)

    def run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script), "--repo", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    assert (
        run(
            a,
            "emit",
            "lead",
            "b1",
            "needs-owner",
            "--url",
            "https://example.test/9",
            "--question",
            str(question),
        ).returncode
        == 0
    )
    read = run(b, "read", "--json")
    assert read.returncode == 0 and json.loads(read.stdout)["question"] == "Go?\n"
    assert run(b, "wait", "lead", "b1", "--timeout", "2").returncode == 2
