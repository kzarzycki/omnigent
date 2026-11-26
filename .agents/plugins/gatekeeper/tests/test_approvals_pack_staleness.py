"""approvals.py: a start-up line when the script's checkout is behind its upstream and the script changed there, and
`pack_staleness: refuse` making it a refusal (#942)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from approvals_harness import (
    approvals,
    git_in,
    loop_md,
)
from test_approvals import REPORT

SCRIPT = "scripts/tool.py"


def commit(where: Path, name: str, text: str) -> None:
    (where / name).parent.mkdir(parents=True, exist_ok=True)
    (where / name).write_text(text)
    git_in(where, "add", "-A")
    git_in(where, "-c", "user.email=a@example.com", "-c", "user.name=a", "commit", "-q", "-m", name)


@pytest.fixture
def clone(tmp_path: Path) -> tuple[Path, Path]:
    """(origin, work): work is a clone of origin holding SCRIPT, with its upstream not yet advanced."""
    origin, work = tmp_path / "origin", tmp_path / "work"
    git_in(tmp_path, "init", "-q", "-b", "main", str(origin))
    commit(origin, SCRIPT, "v1\n")
    git_in(tmp_path, "clone", "-q", str(origin), str(work))
    return origin, work


def fetched(origin: Path, work: Path) -> None:
    git_in(work, "fetch", "-q")


def test_behind_with_the_script_changed_warns(clone: tuple[Path, Path]) -> None:
    origin, work = clone
    commit(origin, SCRIPT, "v2\n")
    commit(origin, "other.txt", "x\n")
    fetched(origin, work)
    line = approvals.pack_behind(work / SCRIPT)
    assert line is not None and "1 commit(s) changed this script" in line and "git pull" in line


def test_upstream_changing_only_gatekeeper_py_makes_an_approvals_py_caller_stale(
    clone: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """approvals.py runs gatekeeper.py's source, so a change upstream to only the gatekeeper leaves the caller stale."""
    origin, work = clone
    scripts = Path(approvals.__file__).parent
    for source in scripts.glob("*.py"):  # the gatekeeper loads its adapter and board from beside it
        commit(origin, f"scripts/{source.name}", source.read_text())
    git_in(work, "pull", "-q")
    commit(origin, "scripts/gatekeeper.py", (scripts / "gatekeeper.py").read_text() + "\n# v2\n")
    fetched(origin, work)
    spec = importlib.util.spec_from_file_location(
        "approvals_by_path", work / "scripts/approvals.py"
    )
    assert spec and spec.loader
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)
    shim.refuses_stale = lambda: True
    assert shim.pack_behind(work / "scripts/approvals.py") is None
    assert shim.main(["report", "nonexistent.md"]) == 1
    assert "gatekeeper.py: this checkout is behind" in capsys.readouterr().err


def test_behind_with_the_script_unchanged_is_silent(clone: tuple[Path, Path]) -> None:
    origin, work = clone
    commit(origin, "other.txt", "x\n")
    fetched(origin, work)
    assert approvals.pack_behind(work / SCRIPT) is None


def test_not_behind_is_silent(clone: tuple[Path, Path]) -> None:
    _, work = clone
    assert approvals.pack_behind(work / SCRIPT) is None


def test_no_upstream_is_silent(tmp_path: Path) -> None:
    alone = tmp_path / "alone"
    git_in(tmp_path, "init", "-q", "-b", "main", str(alone))
    commit(alone, SCRIPT, "v1\n")
    assert approvals.pack_behind(alone / SCRIPT) is None


def test_it_never_fetches(clone: tuple[Path, Path]) -> None:
    origin, work = clone
    commit(origin, SCRIPT, "v2\n")  # origin moved, but work has not fetched
    assert approvals.pack_behind(work / SCRIPT) is None


def test_refuse_setting_is_read_from_the_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    text = loop_md("loop:\n  pack_staleness: refuse\n")
    monkeypatch.setattr(approvals, "project", lambda: (None, text))
    assert approvals.refuses_stale() is True
    monkeypatch.setattr(approvals, "project", lambda: (None, loop_md("loop:\n  cap: 3\n")))
    assert approvals.refuses_stale() is False


def test_main_warns_then_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        approvals, "pack_behind", lambda _: "tool.py: 3 commits behind: run `git pull`"
    )
    monkeypatch.setattr(approvals, "refuses_stale", lambda: True)
    assert approvals.main(["report", "nonexistent.md"]) == 1
    assert "git pull" in capsys.readouterr().err


def test_a_local_commit_to_the_script_with_upstream_moving_elsewhere_is_silent(
    clone: tuple[Path, Path],
) -> None:
    origin, work = clone
    commit(work, SCRIPT, "local edit\n")
    commit(origin, "other.txt", "x\n")
    fetched(origin, work)
    assert approvals.pack_behind(work / SCRIPT) is None


def test_a_local_commit_to_the_script_with_nothing_new_upstream_is_silent(
    clone: tuple[Path, Path],
) -> None:
    _, work = clone
    commit(work, SCRIPT, "local edit\n")
    assert approvals.pack_behind(work / SCRIPT) is None


def test_a_probe_that_fails_says_nothing(
    clone: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, work = clone

    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("no git")

    monkeypatch.setattr(approvals.subprocess, "run", broken)
    assert approvals.pack_behind(work / SCRIPT) is None


def test_the_refusal_exits_non_zero_only_when_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """On a command that otherwise exits 0 (`report` of a finished report), so only the refusal can fail it."""
    report = tmp_path / "report.md"
    report.write_text(REPORT)
    monkeypatch.setattr(approvals, "pack_behind", lambda _: "tool.py: behind: run `git pull`")
    monkeypatch.setattr(approvals, "refuses_stale", lambda: False)
    assert approvals.main(["report", str(report)]) == 0
    assert "git pull" in capsys.readouterr().err
    monkeypatch.setattr(approvals, "refuses_stale", lambda: True)
    assert approvals.main(["report", str(report)]) == 1


def test_running_the_gatekeeper_directly_probes_its_one_file() -> None:
    """RUNNING names the gatekeeper once when it is the running script, so `gatekeeper.py` probes one file, not two."""
    path = Path(approvals.__file__).with_name("gatekeeper.py")
    spec = importlib.util.spec_from_file_location("gatekeeper_by_path", path)
    assert spec and spec.loader
    gatekeeper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gatekeeper)
    assert gatekeeper.RUNNING == (gatekeeper.__file__,)
