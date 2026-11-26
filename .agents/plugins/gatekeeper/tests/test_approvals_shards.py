"""approvals.py shards: `select.shards` splits a whole-suite selection into disjoint, balanced shards (#2229)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import approvals, gatekeeper_md, git_in

# the real record branch functions, which a conftest fixture fakes for every test
RECORD_REF, PUSH_RECORD = approvals.record_ref, approvals.push_record

SUITE = [f"t{n}" for n in range(1, 10)]


def policy_of(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    policy = approvals.Policy(gatekeeper_md(text))
    monkeypatch.setattr(approvals, "project", lambda: (None, policy))


def run(
    tmp_path: Path, *extra: str, selection: list[str] | None = None, durations: str | None = None
) -> None:
    (tmp_path / "suite").write_text("\n".join(SUITE))
    args = ["shards", "--suite", str(tmp_path / "suite"), *extra]
    if selection is not None:
        (tmp_path / "sel").write_text("\n".join(selection))
        args += ["--selection", str(tmp_path / "sel")]
    if durations is not None:
        (tmp_path / "dur").write_text(durations)
        args += ["--durations", str(tmp_path / "dur")]
    assert approvals.main(args) == 0


def printed(capsys: pytest.CaptureFixture[str]) -> list[list[str]]:
    shards: list[list[str]] = []
    for line in capsys.readouterr().out.splitlines():
        if line.startswith("# shard"):
            shards.append([])
        elif line:
            shards[-1].append(line)
    return shards


def test_shards_3_splits_the_whole_suite_into_three_disjoint_shards_balanced_by_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    policy_of(monkeypatch, "select:\n  shards: 3\n")
    # t1 is as long as t2..t9 together; t9 has no recorded duration
    run(
        tmp_path,
        durations='{"t1": 8, "t2": 1, "t3": 1, "t4": 1, "t5": 1, "t6": 1, "t7": 1, "t8": 1}',
    )
    shards = printed(capsys)
    assert len(shards) == 3
    flat = [item for shard in shards for item in shard]
    assert sorted(flat) == sorted(SUITE) and len(flat) == len(
        set(flat)
    )  # disjoint, the union is the suite
    assert any(
        shard == ["t1"] for shard in shards
    )  # the long one is alone: balanced by time, not count
    assert (
        sum("t9" in shard for shard in shards) == 1
    )  # no recorded duration, still in exactly one shard


def test_shards_without_durations_split_evenly_by_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    policy_of(monkeypatch, "select:\n  shards: 3\n")
    run(tmp_path)
    assert sorted(len(shard) for shard in printed(capsys)) == [3, 3, 3]


def test_nothing_set_or_one_shard_prints_the_one_selection_as_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for text in ("", "select:\n  shards: 1\n"):
        policy_of(monkeypatch, text)
        run(tmp_path)
        assert capsys.readouterr().out == "\n".join(SUITE) + "\n"


def test_a_narrow_selection_is_never_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    policy_of(monkeypatch, "select:\n  shards: 3\n")
    run(tmp_path, selection=["t1", "t2", "t3", "t4"])  # not over half of nine
    assert capsys.readouterr().out == "t1\nt2\nt3\nt4\n"


def test_a_selection_over_half_the_suite_or_flagged_whole_is_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    policy_of(monkeypatch, "select:\n  shards: 2\n")
    run(tmp_path, selection=SUITE[:5])
    assert len(printed(capsys)) == 2
    run(tmp_path, "--whole", selection=SUITE[:2])  # a build input changed: whole by rule
    assert len(printed(capsys)) == 2


@pytest.mark.parametrize("bad", ["0", "-1", "true", "two"])
def test_a_shards_value_below_one_or_not_a_number_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    policy_of(monkeypatch, f"select:\n  shards: {bad}\n")
    with pytest.raises(approvals.Refused):
        approvals.shard_count()


def test_one_shard_is_keyed_by_its_items_never_the_bare_command() -> None:
    """A run of some of the tests must not stand on record for the command's full run."""
    assert approvals.shard_command("run all", 1, 1, SUITE).startswith("GATE_SHARD=1/1:")
    assert approvals.shard_command("run all", 1, 1, SUITE) != approvals.shard_command(
        "run all", 1, 1, SUITE[:2]
    )


def test_each_shard_has_its_own_key_and_a_changed_split_is_another_record() -> None:
    """The same suite and tree with other durations splits differently: a shard's pass of the old split must not
    stand for the new one's items, so at least one shard's key differs."""
    old = approvals.shard_selections(
        SUITE, SUITE, 3, {"t1": 9, **dict.fromkeys(SUITE[1:], 1)}, True
    )
    new = approvals.shard_selections(
        SUITE, SUITE, 3, {"t9": 9, **dict.fromkeys(SUITE[:-1], 1)}, True
    )
    assert old != new
    keys = [
        [approvals.shard_command("run", at, 3, items) for at, items in enumerate(split, 1)]
        for split in (old, new)
    ]
    assert any(a != b for a, b in zip(*keys, strict=True))


def test_an_unrecorded_item_weighs_the_mean_of_the_recorded_ones() -> None:
    """a=10 and b=6 are recorded, c and d are not (mean 8): the two at 8 go before b and together, b joins a. At
    weight 0 they would all pile onto b's shard."""
    shards = approvals.shard_selections(list("abcd"), list("abcd"), 2, {"a": 10, "b": 6}, True)
    assert shards == [["a", "b"], ["c", "d"]]


def test_exactly_half_of_the_suite_is_a_narrow_selection() -> None:
    suite = list("abcd")
    assert approvals.shard_selections(suite[:2], suite, 2, None, False) == [suite[:2]]
    assert len(approvals.shard_selections(suite[:3], suite, 2, None, False)) == 2


@pytest.mark.parametrize("written", ["not json", '{"t1": null}', None])
def test_unreadable_durations_give_an_even_split_by_count_and_say_so(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    written: str | None,
) -> None:
    policy_of(monkeypatch, "select:\n  shards: 3\n")
    (tmp_path / "suite").write_text("\n".join(SUITE))
    durations = tmp_path / "dur"
    if written is not None:
        durations.write_text(written)
    code = approvals.main(
        ["shards", "--suite", str(tmp_path / "suite"), "--durations", str(durations)]
    )
    out = capsys.readouterr()
    assert code == 0 and "even split by count" in out.err
    assert sorted(len(shard) for shard in printed_from(out.out)) == [3, 3, 3]


def printed_from(text: str) -> list[list[str]]:
    shards: list[list[str]] = []
    for line in text.splitlines():
        if line.startswith("# shard"):
            shards.append([])
        elif line:
            shards[-1].append(line)
    return shards


def test_more_shards_than_items_never_prints_an_empty_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert [] not in approvals.shard_selections(["a", "b"], ["a", "b"], 3, None, True)
    policy_of(monkeypatch, "select:\n  shards: 5\n")
    (tmp_path / "suite").write_text("a\nb\nc")
    assert approvals.main(["shards", "--suite", str(tmp_path / "suite")]) == 0
    out = capsys.readouterr()
    assert [len(shard) for shard in printed_from(out.out)] == [1, 1, 1]
    assert "5 shards asked, but 3 items" in out.err


@pytest.mark.parametrize("flag", ["--passed", "--record"])
def test_passed_and_record_need_the_command_they_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flag: str,
) -> None:
    policy_of(monkeypatch, "select:\n  shards: 2\n")
    (tmp_path / "suite").write_text("\n".join(SUITE))
    code = approvals.main(
        ["shards", "--suite", str(tmp_path / "suite"), flag, "HEAD", "--shard", "1"]
    )
    assert code == 1 and "--command" in capsys.readouterr().out


@pytest.fixture
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A clone with a commit and a bare origin, the real record functions, and a shard command that logs the items
    it was given and fails while `fail` exists. `args(*more)` is the argv of a shards call over a six-item suite."""
    monkeypatch.setattr(approvals, "record_ref", RECORD_REF)
    monkeypatch.setattr(approvals, "push_record", PUSH_RECORD)
    monkeypatch.setattr(approvals, "one_repo", lambda: None)
    monkeypatch.setenv("GATE_SLOTS", "off")
    policy_of(monkeypatch, "select:\n  shards: 3\n")
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    git_in(tmp_path, "init", "-q", "--bare", str(origin))
    work.mkdir()
    git_in(work, "init", "-q")
    (work / "suite").write_text("\n".join(SUITE[:6]))
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).open('a').write(' '.join(sys.argv[2:]) + '\\n')\n"
        "sys.exit(1 if pathlib.Path(sys.argv[1]).with_suffix('.fail').exists() else 0)\n"
    )
    git_in(work, "add", "-A")
    git_in(work, "-c", "user.name=l", "-c", "user.email=l@example.com", "commit", "-qm", "base")
    git_in(work, "remote", "add", "origin", str(origin))
    git_in(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    monkeypatch.chdir(work)
    log = tmp_path / "ran.log"
    command = f"{sys.executable} {runner} {log}"
    head = git_in(work, "rev-parse", "HEAD").strip()
    return {
        "head": head,
        "command": command,
        "log": log,
        "fail": log.with_suffix(".fail"),
        "args": lambda *more: ["shards", "--suite", "suite", "--command", command, *more],
    }


def test_a_shard_pass_a_real_run_recorded_is_found_by_passed(
    clone: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """No stub on the record: `--record` runs shard i with its items and pushes R1's record; `--passed` reads those
    branches back, and exits 0 only once every shard has one, naming the shard that has none."""
    head, args = clone["head"], clone["args"]
    for shard in (1, 3):
        assert approvals.main(args("--record", head, "--shard", str(shard))) == 0
    assert approvals.main(args("--passed", head)) == 1
    assert "GATE_SHARD=2/3:" in capsys.readouterr().out
    assert approvals.main(args("--record", head, "--shard", "2")) == 0
    assert approvals.main(args("--passed", head)) == 0
    ran = [set(line.split()) for line in clone["log"].read_text().splitlines()]
    assert sorted(item for items in ran for item in items) == sorted(
        SUITE[:6]
    )  # each item ran once


def test_a_failed_shard_records_nothing_and_a_head_that_is_not_checked_out_is_refused(
    clone: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    head, args = clone["head"], clone["args"]
    clone["fail"].touch()
    assert approvals.main(args("--record", head, "--shard", "1")) == 1
    clone["fail"].unlink()
    assert approvals.main(args("--passed", head)) == 1  # shard 1 has no pass
    capsys.readouterr()
    assert approvals.main(args("--record", "0" * 40, "--shard", "1")) == 1
    assert "another commit" in capsys.readouterr().out
    assert approvals.main(args("--record", head, "--shard", "4")) == 1
    assert "--shard 1 to 3" in capsys.readouterr().out


def test_a_dirty_checkout_is_refused_before_the_run(
    clone: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """local-ci --record refuses a checkout with changes: a pass of the committed tree from a run on another tree
    says nothing about the head."""
    Path("suite").write_text("edited, uncommitted\n")
    assert approvals.main(clone["args"]("--record", clone["head"], "--shard", "1")) == 1
    assert "the tree has changes" in capsys.readouterr().out
    assert not clone["log"].exists()  # nothing ran
    assert approvals.main(clone["args"]("--passed", clone["head"])) == 1  # nothing recorded


def test_a_shard_that_rewrites_a_tracked_file_records_nothing(
    clone: dict[str, Any], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    writer = tmp_path / "writer.py"
    writer.write_text("import pathlib; pathlib.Path('suite').write_text('rewritten\\n')\n")
    head = clone["head"]
    argv = ["shards", "--suite", "suite", "--command", f"{sys.executable} {writer}"]
    assert approvals.main([*argv, "--record", head, "--shard", "1"]) == 1
    assert "changed the checkout" in capsys.readouterr().out
    git_in(Path.cwd(), "checkout", "--", "suite")
    assert approvals.main([*argv, "--passed", head]) == 1


def test_a_shard_runs_without_the_callers_tier_variables(
    clone: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """gate_env clears GATE_INCUBATING (a reduced tier): a pass recorded under it would stand for a full shard."""
    monkeypatch.setenv("GATE_INCUBATING", "1")
    seen = tmp_path / "seen"
    probe = tmp_path / "probe.py"
    probe.write_text(
        f"import os, pathlib; pathlib.Path({str(seen)!r}).write_text(os.environ.get('GATE_INCUBATING', 'unset'))\n"
    )
    argv = ["shards", "--suite", "suite", "--command", f"{sys.executable} {probe}"]
    assert approvals.main([*argv, "--record", clone["head"], "--shard", "1"]) == 0
    assert seen.read_text() == "unset"


def test_a_command_that_sets_a_variable_in_front_is_recorded_under_it(
    clone: dict[str, Any],
) -> None:
    """`FOO=1 <cmd>` runs (command_line), and its record's env holds FOO, which is how `--passed` keys it."""
    head = clone["head"]
    argv = ["shards", "--suite", "suite", "--command", f"FOO=1 {clone['command']}"]
    for shard in (1, 2, 3):
        assert approvals.main([*argv, "--record", head, "--shard", str(shard)]) == 0
    assert approvals.main([*argv, "--passed", head]) == 0
    assert (
        approvals.main(
            ["shards", "--suite", "suite", "--command", clone["command"], "--passed", head]
        )
        == 1
    )


def test_a_shard_that_moves_head_records_nothing(
    clone: dict[str, Any], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A clean checkout at another commit has no changes to list, yet its tree is not the one the pass names."""
    mover = tmp_path / "mover.py"
    mover.write_text(
        "import subprocess; subprocess.run(['git', '-c', 'user.name=l', '-c', 'user.email=l@example.com',"
        " 'commit', '-q', '--allow-empty', '-m', 'moved'], check=True)\n"
    )
    argv = ["shards", "--suite", "suite", "--command", f"{sys.executable} {mover}"]
    assert approvals.main([*argv, "--record", clone["head"], "--shard", "1"]) == 1
    assert "HEAD moved" in capsys.readouterr().out


@pytest.mark.parametrize(("limit", "parts"), [("10m", 2), ("15m", 1), ("20m", 1)])
def test_a_selection_whose_estimate_is_over_timeouts_run_is_split(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    limit: str,
    parts: int,
) -> None:
    """Three of nine items, 300 s each, is 900 s and narrow by count: over a 10 m limit it splits, at 15 m and under
    20 m it does not."""
    policy_of(monkeypatch, f"select:\n  shards: 2\ntimeouts:\n  run: {limit}\n")
    durations = json.dumps({item: 300 for item in SUITE})
    run(tmp_path, selection=SUITE[:3], durations=durations)
    out = capsys.readouterr().out
    if parts == 1:
        assert out == "t1\nt2\nt3\n"
    else:
        assert len(printed_from(out)) == parts


def test_an_estimate_needs_recorded_durations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With nothing recorded there is no estimate, so a narrow selection stays one, however short the limit."""
    policy_of(monkeypatch, "select:\n  shards: 2\ntimeouts:\n  run: 1s\n")
    run(tmp_path, selection=SUITE[:3])
    assert capsys.readouterr().out == "t1\nt2\nt3\n"


def test_an_unrecorded_item_counts_at_the_mean_in_the_estimate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two recorded items at 300 s and one unrecorded (the mean, 300 s) is 900 s, over a 10 m limit; counting only
    the recorded ones, 600 s, would be at it."""
    policy_of(monkeypatch, "select:\n  shards: 2\ntimeouts:\n  run: 10m\n")
    run(tmp_path, selection=SUITE[:3], durations=json.dumps({"t1": 300, "t2": 300}))
    assert len(printed_from(capsys.readouterr().out)) == 2


def test_a_double_quoted_command_recorded_is_found_by_passed(clone: dict[str, Any]) -> None:
    """A CI file writes `--command` with quotes: the record holds the form `gate()` records (command_line), which is
    the one `--passed` reads."""
    head = clone["head"]
    argv = ["shards", "--suite", "suite", "--command", f'{sys.executable} -c "import sys"']
    for shard in (1, 2, 3):
        assert approvals.main([*argv, "--record", head, "--shard", str(shard)]) == 0
    assert approvals.main([*argv, "--passed", head]) == 0


@pytest.mark.parametrize("policy", ["select:\n  shards: 3\n", ""])
def test_a_one_shard_record_is_no_pass_of_the_bare_command(
    clone: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    policy: str,
) -> None:
    """A narrow selection is one shard, with and without `select.shards`: its record is found by `--passed` for the
    same selection, and `gate-record` of the bare command finds nothing."""
    policy_of(monkeypatch, policy)
    head = clone["head"]
    (tmp_path / "sel").write_text("t1\n")
    argv = ["shards", "--suite", "suite", "--selection", str(tmp_path / "sel")]
    argv += ["--command", clone["command"]]
    assert approvals.main([*argv, "--record", head, "--shard", "1"]) == 0
    assert approvals.main([*argv, "--passed", head]) == 0
    capsys.readouterr()
    assert approvals.main(["gate-record", head, "--", *clone["command"].split()]) != 0
    assert "no record of" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("policy", "said"),
    [("", "(no `select.shards`)"), ("select:\n  shards: 3\n", "`approvals.py shards`")],
)
def test_the_over_budget_line_says_unsharded_and_names_the_shards_verb_when_it_is_set(
    monkeypatch: pytest.MonkeyPatch, policy: str, said: str
) -> None:
    policy_of(monkeypatch, policy)
    line = approvals.over_budget(60, Path("."), None)
    assert "unsharded" in line and said in line
    assert ("select.shards` is 3" in line) == bool(policy)
