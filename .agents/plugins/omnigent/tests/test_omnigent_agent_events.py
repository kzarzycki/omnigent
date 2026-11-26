"""The supervisor reads loop events (#1377): a beating wait is live, a silent one raises its deadline, an end is told once.

The seam is `supervise_registry` with the fake fleet of test_omnigent_agent and a real bare remote holding
`refs/loop/*`, written by engineering-loop's events.py."""

import json
import subprocess
import time
from pathlib import Path

import pytest
from test_omnigent_agent import (
    Lead,
    ending,
    events_of,
    following,
    omnigent_agent,
    posted,
    skewed,
    supervised,
    told,
)

EVENTS = omnigent_agent._load_events()
READS_LOOP_EVENTS = True  # conftest.no_origin_reads leaves this module its bare remote
real_sleep = time.sleep  # `supervised` replaces the module-wide `time.sleep`
WAIT = "STILL WORKING: waiting on #12"
GREEN = (
    0,
    "PR #12 is draft",
    "s1",
)  # an open PR whose checks are settled and that is not waiting on checks: only events or a required check keep it live


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clone of a temporary bare remote, the supervisor's working directory; PRs 12 and 13 sit on `feat` and `feat2`."""
    bare, work = tmp_path / "origin.git", tmp_path / "clone"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    git(tmp_path, "clone", "-q", str(bare), str(work))
    monkeypatch.chdir(work)
    # the clone's origin reads as `o/r` (the lead's repository), though pushes still reach the bare remote
    git(work, "config", f"url.{bare}.insteadOf", "https://github.com/o/r.git")
    git(work, "remote", "set-url", "origin", "https://github.com/o/r.git")
    monkeypatch.setattr(omnigent_agent, "EVENT_SECONDS", 0.0)
    branches = {12: "feat", 13: "feat2"}
    monkeypatch.setattr(
        omnigent_agent,
        "pr_info",
        lambda pr, repo="": {"state": "open", "branch": branches[pr], "sha": "s1", "base": "main"},
    )
    monkeypatch.setattr(omnigent_agent, "pr_settled", lambda pr, repo="": GREEN)
    monkeypatch.setattr(omnigent_agent, "required_pending", lambda info, repo="": False)
    return work


def deadlines(registry: Path) -> list[str]:
    log = registry.with_suffix(".events.jsonl")
    return [
        e["line"] for e in (events_of(registry) if log.exists() else []) if e["kind"] == "deadline"
    ]


def wait_run(where: Path, monkeypatch: pytest.MonkeyPatch, skew: float) -> list[str]:
    """One lead waiting on PR 12, quiet for 25 minutes, polled once `skew` seconds after the events were written."""
    where.mkdir(exist_ok=True)
    registry = following(where)
    offset = skewed(monkeypatch)
    offset[0] = skew
    og = Lead(told("brief", 4000), ending(WAIT, 1500))
    og.snap = {"status": "running"}
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True, fyi="post")
    return deadlines(registry)


def test_beating_wait_raises_no_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path
) -> None:
    assert (
        len(wait_run(tmp_path / "ref", monkeypatch, 60.0)) == 1
    )  # the same history without events
    EVENTS.emit("land", "feat", "waiting", cwd=clone)
    # rewritten a minute ago: twice a 300 s beat is 600 s
    assert wait_run(tmp_path / "beat", monkeypatch, 60.0) == []


def test_silent_wait_raises_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path
) -> None:
    EVENTS.emit("land", "feat", "waiting", cwd=clone)
    lines = wait_run(tmp_path, monkeypatch, 900.0)  # unchanged for 15 minutes
    assert len(lines) == 1 and "land event silent 15m" in lines[0]


@pytest.mark.parametrize(("pending", "deadline"), [(True, 0), (False, 1)])
def test_pending_required_check_is_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path, pending: bool, deadline: int
) -> None:
    monkeypatch.setattr(omnigent_agent, "required_pending", lambda info, repo="": pending)
    assert len(wait_run(tmp_path, monkeypatch, 0.0)) == deadline


def test_required_pending_reads_the_rules_and_the_heads_check_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rule = {
        "type": "required_status_checks",
        "parameters": {"required_status_checks": [{"context": "gate"}]},
    }
    runs = {
        "check_runs": [
            {"name": "gate", "status": "queued"},
            {"name": "lint", "status": "completed"},
        ]
    }
    answers = {"rules/branches/main": [rule], "commits/s1/check-runs": runs}
    monkeypatch.setattr(
        omnigent_agent,
        "gh",
        lambda *args, **kw: json.dumps(next(v for k, v in answers.items() if args[-1].endswith(k))),
    )
    info = {"state": "open", "sha": "s1", "base": "main"}
    assert omnigent_agent.required_pending(info, "o/r") is True
    runs["check_runs"][0]["status"] = "completed"
    assert omnigent_agent.required_pending(info, "o/r") is False
    runs["check_runs"][0]["status"] = "queued"
    assert omnigent_agent.required_pending({**info, "state": "closed"}, "o/r") is False


def test_owner_question_sorts_before_news(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 60), ending(WAIT, 30))
    og.snap = {
        "status": "idle",
        "labels": {omnigent_agent.PR_LABEL: "o/r#13"},
    }  # its own PR: branch feat2
    question = "May the gate be skipped?\nThe whole text.\n"

    def write(poll: int) -> None:
        for kind, branch, state in [
            ("land", "feat", "ok"),
            ("gate", "feat", "failed"),
            ("verifier", "feat", "ok"),
            ("land", "feat2", "ok"),
            ("gate", "feat2", "failed"),
        ]:
            EVENTS.emit(kind, branch, "waiting", cwd=clone)
            EVENTS.emit(kind, branch, state, cwd=clone)  # a beat before the end: one news per ref
        EVENTS.emit("probe", "feat", "needs-owner", question=question, cwd=clone)

    supervised(og, registry, monkeypatch, 3, write, notify="cos", console=True, fyi="post")
    told_ = [
        e
        for e in events_of(registry)
        if e["kind"] in ("loop-owner", "news") and " on feat" in e["line"]
    ]
    assert [e["kind"] for e in told_] == ["loop-owner"] + ["news"] * 5
    assert any(
        "needs the owner: May the gate be skipped?" in t and "The whole text." in t
        for t in posted(og)
    )
    assert "needs the owner: May the gate be skipped?" in told_[0]["line"]
    assert [e["line"].split(": ", 1)[1].split(" on ")[0] for e in told_[1:]] == [
        "gate failed",
        "gate failed",
        "land ok",
        "land ok",
        "verifier ok",
    ]


def test_unchanged_poll_is_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path
) -> None:
    EVENTS.emit(
        "land", "feat", "ok", cwd=clone
    )  # ended before this supervisor started: recorded, not news
    EVENTS.emit("gate", "feat", "waiting", cwd=clone)
    registry = following(tmp_path)
    skewed(monkeypatch)[0] = 100.0
    og = Lead(told("brief", 60), ending(WAIT, 30))
    og.snap = {"status": "idle"}
    supervised(og, registry, monkeypatch, 4, notify="cos", console=True, fyi="post")
    log = registry.with_suffix(".events.jsonl")
    assert not [e for e in (events_of(registry) if log.exists() else []) if " on feat" in e["line"]]
    assert not [t for t in posted(og) if " on feat" in t]


def test_an_unreadable_poll_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path
) -> None:
    monkeypatch.setattr(omnigent_agent, "read_loop_events", lambda cwd=None: None)
    assert len(wait_run(tmp_path, monkeypatch, 0.0)) == 1  # no events known: today's deadline


def test_another_branchs_beating_event_is_no_life_for_this_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path
) -> None:
    EVENTS.emit(
        "land", "elsewhere", "waiting", cwd=clone
    )  # a beat on a branch this lead's PR is not on
    assert len(wait_run(tmp_path, monkeypatch, 60.0)) == 1


def test_an_end_written_again_with_the_same_state_is_told_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 60), ending(WAIT, 30))
    og.snap = {"status": "idle"}
    again = lambda poll: EVENTS.emit(
        "land", "feat", "ok", cwd=clone
    )  # a new commit, the same state
    supervised(og, registry, monkeypatch, 4, again, notify="cos", console=True, fyi="post")
    told_ = [e for e in events_of(registry) if "land ok on feat" in e["line"]]
    assert len(told_) == 1


def test_a_lead_needs_owner_event_is_not_told_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path
) -> None:
    """The lead's own `needs-owner` is raised by its turn end; the event poll adds no second item for it."""
    registry = following(tmp_path)
    og = Lead(told("brief", 60), ending(WAIT, 30))
    og.snap = {"status": "idle"}
    write = lambda poll: EVENTS.emit("lead", "feat", "needs-owner", question="Q?\n", cwd=clone)
    supervised(og, registry, monkeypatch, 3, write, notify="cos", console=True, fyi="post")
    assert not [
        e for e in events_of(registry) if e["kind"] == "loop-owner" and " on feat" in e["line"]
    ]


def lead_repo_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lead's repository is o/r (the supervisor's directory's too); PR 12 is read only through that repository."""
    monkeypatch.setattr(omnigent_agent, "workspace_repo", lambda og, sid: "o/r")

    def pr_info(pr: int, repo: str = "") -> dict:
        if repo != "o/r":
            raise omnigent_agent.Fail(f"HTTP 404: no PR {pr} in the supervisor's own repository")
        return {"state": "open", "branch": "feat", "sha": "s1", "base": "main"}

    monkeypatch.setattr(omnigent_agent, "pr_info", pr_info)


def test_a_bare_pr_wait_is_read_in_the_leads_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path
) -> None:
    lead_repo_only(monkeypatch)
    EVENTS.emit("land", "feat", "waiting", cwd=clone)
    lines = wait_run(tmp_path, monkeypatch, 900.0)
    assert len(lines) == 1 and "land event silent 15m" in lines[0]


def test_a_bare_pr_waits_end_and_question_reach_the_lead_and_the_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path
) -> None:
    lead_repo_only(monkeypatch)
    registry = following(tmp_path)
    og = Lead(told("brief", 60), ending(WAIT, 30))
    og.snap = {"status": "idle"}

    def write(poll: int) -> None:
        EVENTS.emit("land", "feat", "failed", url="https://x/log", cwd=clone)
        EVENTS.emit("gate", "feat", "needs-owner", question="Skip?\n", cwd=clone)

    supervised(og, registry, monkeypatch, 3, write, notify="cos", console=True, fyi="skip")
    assert [e for e in events_of(registry) if "land failed on feat" in e["line"]]
    assert [e for e in events_of(registry) if e["kind"] == "loop-owner"]


def test_a_step_question_reaches_the_owner_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 60), ending(WAIT, 30))
    og.snap = {"status": "idle"}
    question = "May the gate be skipped?\nSecond line: the reason.\nThird line: the risk.\n"
    write = lambda poll: EVENTS.emit("gate", "feat", "needs-owner", question=question, cwd=clone)
    supervised(og, registry, monkeypatch, 3, write, notify="cos", console=True, fyi="skip")
    posts = "\n".join(posted(og))
    assert "May the gate be skipped?" in posts and "Third line: the risk." in posts


def test_a_lead_of_another_repository_takes_no_life_from_this_directorys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path
) -> None:
    """Its workspace is no checkout here, and its repository is not this directory's: a beat of a same-named branch
    in this directory's repository holds nothing live."""
    monkeypatch.setattr(omnigent_agent, "workspace_repo", lambda og, sid: "o/x")
    assert len(wait_run(tmp_path / "ref", monkeypatch, 60.0)) == 1
    EVENTS.emit("land", "feat", "waiting", cwd=clone)
    assert len(wait_run(tmp_path / "beat", monkeypatch, 60.0)) == 1


def test_a_word_wait_on_the_leads_own_pr_is_live_while_its_event_beats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path
) -> None:
    """`waiting on land`, no PR ref: only the beating event on the lead's own PR's branch keeps it live."""
    monkeypatch.setattr(omnigent_agent, "workspace_repo", lambda og, sid: "o/r")

    def run(where: Path) -> list[str]:
        where.mkdir(exist_ok=True)
        registry = following(where)
        skewed(monkeypatch)[0] = 60.0
        og = Lead(told("brief", 4000), ending("STILL WORKING: waiting on land", 1500))
        og.snap = {"status": "running", "labels": {omnigent_agent.PR_LABEL: "o/r#12"}}
        supervised(og, registry, monkeypatch, 1, notify="cos", console=True, fyi="post")
        return deadlines(registry)

    assert len(run(tmp_path / "ref")) == 1
    EVENTS.emit("land", "feat", "waiting", cwd=clone)
    assert run(tmp_path / "beat") == []


def test_a_silent_event_decides_over_the_prs_running_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path
) -> None:
    monkeypatch.setattr(
        omnigent_agent, "pr_settled", lambda pr, repo="": None
    )  # checks still running
    assert (
        wait_run(tmp_path / "ref", monkeypatch, 900.0) == []
    )  # no events: the old rule reads it live
    EVENTS.emit("land", "feat", "waiting", cwd=clone)
    lines = wait_run(tmp_path / "silent", monkeypatch, 900.0)
    assert len(lines) == 1 and "land event silent" in lines[0]


@pytest.mark.parametrize(("age", "deadlines_"), [(400.0, 0), (700.0, 1)])
def test_live_means_changed_within_twice_the_beat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path, age: float, deadlines_: int
) -> None:
    EVENTS.emit("land", "feat", "waiting", cwd=clone)  # a 300 s beat
    assert len(wait_run(tmp_path, monkeypatch, age)) == deadlines_


def test_an_unreadable_poll_after_a_read_keeps_what_was_known(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path
) -> None:
    EVENTS.emit("land", "feat", "waiting", cwd=clone)
    registry = following(tmp_path)
    skewed(monkeypatch)[0] = 60.0
    og = Lead(told("brief", 4000), ending(WAIT, 1500))
    og.snap = {"status": "running"}
    real, reads = omnigent_agent.read_loop_events, []

    def read(cwd=None):
        reads.append(cwd)
        return real(cwd) if len(reads) == 1 else None  # the first read works, every later one fails

    monkeypatch.setattr(omnigent_agent, "read_loop_events", read)
    supervised(og, registry, monkeypatch, 4, notify="cos", console=True, fyi="post")
    assert len(reads) > 1  # the failed reads happened
    assert deadlines(registry) == []  # and forgot nothing: the first read said beating


def other_clone(tmp_path: Path) -> Path:
    """A second repository, with its own remote and clone."""
    bare, work = tmp_path / "other.git", tmp_path / "other"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    git(tmp_path, "clone", "-q", str(bare), str(work))
    return work


def test_a_beat_in_another_repository_holds_a_lead_of_this_one_live_for_no_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path
) -> None:
    """The lead's checkout is the second repository, whose PR 12 is on `feat`; a beating `feat` in the supervisor's
    own repository is another repository's event."""
    monkeypatch.setattr(
        omnigent_agent, "EVENT_SECONDS", 30.0
    )  # the real throttle: a read per repository per window
    other = other_clone(tmp_path)
    EVENTS.emit("land", "feat", "waiting", cwd=clone)  # the supervisor's repository, not the lead's
    where = tmp_path / "run"
    where.mkdir()
    registry = following(where)
    skewed(monkeypatch)[0] = 60.0
    og = Lead(told("brief", 4000), ending(WAIT, 1500))
    og.snap = {"status": "running", "workspace": str(other)}
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True, fyi="post")
    assert len(deadlines(registry)) == 1
    EVENTS.emit("land", "feat", "waiting", cwd=other)  # the lead's own repository: its wait is live
    where2 = tmp_path / "run2"
    where2.mkdir()
    registry = following(where2)
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True, fyi="post")
    assert deadlines(registry) == []


def test_a_wait_on_another_repositorys_pr_is_not_kept_live_by_this_ones_beat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path
) -> None:
    """`waiting on o/x#12`: that PR's branch `feat` is in o/x, whose events are not read from here."""
    EVENTS.emit("land", "feat", "waiting", cwd=clone)
    monkeypatch.setattr(omnigent_agent, "workspace_repo", lambda og, sid: "o/r")
    registry = following(tmp_path)
    skewed(monkeypatch)[0] = 60.0
    og = Lead(told("brief", 4000), ending("STILL WORKING: waiting on o/x#12", 1500))
    og.snap = {"status": "running"}
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True, fyi="post")
    assert len(deadlines(registry)) == 1


def test_no_file_is_read_for_a_session_with_no_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(
        omnigent_agent, "workspace_repo", lambda og, sid: asked.append(sid) or "o/r"
    )
    wait_run(tmp_path, monkeypatch, 60.0)
    assert "" not in asked


def test_an_end_written_before_a_restart_but_after_the_last_poll_is_told(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path
) -> None:
    registry = following(tmp_path)

    def run() -> Lead:
        og = Lead(told("brief", 60), ending(WAIT, 30))
        og.snap = {"status": "idle"}
        supervised(og, registry, monkeypatch, 2, notify="cos", console=True, fyi="post")
        return og

    run()  # the supervisor polls, saves, and stops
    EVENTS.emit("land", "feat", "ok", cwd=clone)  # written while it was down
    real_sleep(3)  # past the 2 s a whole-second `at` is allowed to trail a start
    run()  # restarted: it was not told this end
    assert [e for e in events_of(registry) if "land ok on feat" in e["line"]]
