"""``reap`` clears the loop events of a closed PR's branch (#1380), and leaves an open PR's."""

import json
import subprocess
from pathlib import Path

import pytest
import test_omnigent_agent as base
from test_omnigent_agent import no_model, no_sleep  # noqa: F401  (the fixtures apply here by name)
from test_omnigent_keep_pr_worker import og, worker  # noqa: F401  (a fixture, and the helper)

oa = base.omnigent_agent
BRANCH = "loop/x"


def refs(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", "refs/loop/"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A clone with a ``verifier`` event on ``loop/x``, written here and pushed to its ``origin``."""
    bare, work = tmp_path / "origin.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True, capture_output=True)
    oa.EVENTS.emit("verifier", BRANCH, "ok", cwd=work)
    assert "refs/loop/verifier/loop/x" in refs(work) and "refs/loop/verifier/loop/x" in refs(bare)
    return work


def reap_with(
    og,  # noqa: F811
    clone: Path,
    monkeypatch,
    tmp_path: Path,
    state: str = "MERGED",
    *,
    open_prs: list | None = None,
    fails: str = "",
    **info: object,
) -> tuple[list[str], bool]:
    """Reap a worker of a PR in ``state`` whose workspace is ``clone`` (``info`` overrides the session's fields).
    ``fails`` is the field of the ``gh pr view`` (``headRefName``) or the call (``pr list``) that raises; ``pr_hold``'s
    own read, of ``state``, always answers."""

    def gh(*args: str) -> str:
        if args[:2] == ("repo", "view"):
            return "o/r\n"
        if fails and (args[-1] == fails or " ".join(args[:2]) == fails):
            raise oa.Fail("gh: HTTP 502")
        if args[:2] == ("pr", "list"):
            return json.dumps(open_prs or [])
        return json.dumps({"state": state, "headRefName": BRANCH})

    monkeypatch.setattr(oa, "gh", gh)
    sid = worker(og, tmp_path)
    got = og.info
    og.info = lambda s: {**got(s), "workspace": str(clone), **info}  # type: ignore[method-assign]
    result = oa.reap(og, [sid])
    assert og.archived == [sid] or state == "OPEN"
    return result


def kept(clone: Path) -> bool:
    return "refs/loop/verifier/loop/x" in refs(clone)


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_reap_of_a_closed_pr_clears_its_branchs_events_here_and_on_origin(
    og,  # noqa: F811
    clone,
    tmp_path,
    monkeypatch,
    state,
) -> None:
    reap_with(og, clone, monkeypatch, tmp_path, state)
    assert refs(clone) == "" and refs(tmp_path / "origin.git") == ""


def test_reap_of_an_open_pr_keeps_its_events(og, clone, tmp_path, monkeypatch) -> None:  # noqa: F811
    reap_with(og, clone, monkeypatch, tmp_path, "OPEN")
    assert kept(clone)


def test_an_open_pr_on_the_same_head_branch_keeps_the_events(
    og,  # noqa: F811
    clone,
    tmp_path,
    monkeypatch,
) -> None:
    """A closed PR replaced by a new one from the same branch: the new PR's live events stay."""
    reap_with(og, clone, monkeypatch, tmp_path, open_prs=[{"number": 9}])
    assert kept(clone) and "refs/loop/verifier/loop/x" in refs(tmp_path / "origin.git")


@pytest.mark.parametrize("fails", ["headRefName", "pr list", "clear"])
def test_a_failed_clear_still_archives_the_session_and_says_so(
    og,  # noqa: F811
    clone,
    tmp_path,
    monkeypatch,
    capsys,
    fails,
) -> None:
    if fails == "clear":

        def boom(*a, **kw):
            raise OSError("update-ref failed")

        monkeypatch.setattr(oa.EVENTS, "clear", boom)
    reap_with(og, clone, monkeypatch, tmp_path, fails="" if fails == "clear" else fails)
    assert "o/r#7: loop events not cleared:" in capsys.readouterr().err and kept(clone)


def test_a_failed_clear_does_not_stop_the_next_session(og, clone, tmp_path, monkeypatch) -> None:  # noqa: F811
    """One session's failed clear never leaves a later session of the same reap unarchived."""
    monkeypatch.setattr(oa.EVENTS, "clear", lambda *a, **kw: 1 / 0)
    monkeypatch.setattr(
        oa,
        "gh",
        lambda *a: (
            "o/r\n"
            if a[:2] == ("repo", "view")
            else json.dumps(
                [] if a[:2] == ("pr", "list") else {"state": "MERGED", "headRefName": BRANCH}
            )
        ),
    )
    first, second = worker(og, tmp_path, 7), worker(og, tmp_path, 8)
    for sid in (first, second):
        got = og.info
        og.info = lambda s, got=got: {**got(s), "workspace": str(clone)}  # type: ignore[method-assign]
    oa.reap(og, [first, second])
    assert og.archived == [first, second]


def test_a_session_on_another_host_clears_nothing(og, clone, tmp_path, monkeypatch) -> None:  # noqa: F811
    """Its workspace is a path on that host: a directory of the same name here is not its checkout."""
    reap_with(og, clone, monkeypatch, tmp_path, host_id="elsewhere")
    assert kept(clone)


def test_a_workspace_that_is_not_a_directory_here_clears_nothing(
    og,  # noqa: F811
    clone,
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    reap_with(og, tmp_path / "no-such-dir", monkeypatch, tmp_path)
    assert (
        kept(clone) and "not cleared" not in capsys.readouterr().err
    )  # skipped, not tried and failed


def test_a_host_without_the_events_module_still_archives(
    og,  # noqa: F811
    clone,
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(oa, "EVENTS", None)
    reap_with(og, clone, monkeypatch, tmp_path)
    assert kept(clone) and "not cleared" not in capsys.readouterr().err


def test_a_start_here_never_reads_the_hosts_cpu() -> None:
    """conftest's `_host_has_room` reaches this module whatever name it binds the script to (a busy host stalled it)."""
    assert oa.capacity() == (True, "")
