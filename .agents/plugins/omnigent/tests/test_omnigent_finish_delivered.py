"""omnigent_agent.py: a session whose result is taken is archived at one shared point (#1579)."""

from pathlib import Path

import pytest
import test_omnigent_agent as base
from test_omnigent_agent import no_model, no_sleep  # noqa: F401  (the fixtures apply here by name)

omnigent_agent = base.omnigent_agent
CARD = {"elicitation_id": "question_1", "params": {"message": "Land it?"}}


@pytest.fixture
def og(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> base.FakeOmnigent:
    fake = base.FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: fake)
    monkeypatch.chdir(tmp_path)
    return fake


def child(og: base.FakeOmnigent, tmp_path: Path, title: str = "t", **kw: object) -> str:
    sid, _ = omnigent_agent.start(og, "claude", title, "do X", tmp_path / "r", None, 0, **kw)
    (tmp_path / "r.md").write_text("DONE: ok")
    return sid


# --- the one function ---------------------------------------------------------------------------


def test_an_idle_session_is_archived_and_one_already_archived_is_not_again(
    og: base.FakeOmnigent, tmp_path: Path
) -> None:
    sid = child(og, tmp_path)
    assert omnigent_agent.finish_delivered(og, sid) == (f"{sid} : archived (idle)", False)
    assert og.archived == [sid]
    assert omnigent_agent.finish_delivered(og, sid)[0].endswith("already archived")
    assert og.archived == [sid]


@pytest.mark.parametrize("how", ["keep", "label", "card", "prompt", "task"])
def test_a_kept_session_is_not_archived(og: base.FakeOmnigent, tmp_path: Path, how: str) -> None:
    sid = child(og, tmp_path)
    keep = how == "keep"
    if how == "label":
        og.labels[sid] = {omnigent_agent.KEEP_LABEL: "1"}
    if how == "card":
        og.prompts = [CARD]
    if how == "prompt":
        og.prompts = [{"elicitation_id": "elicit_approval_1", "params": {"message": "Bash?"}}]
    if how == "task":
        og.tasks = [{"type": "shell", "status": "running"}]
    line, failed = omnigent_agent.finish_delivered(og, sid, keep=keep)
    assert not failed and "kept (" in line and og.archived == []


def test_a_session_still_reading_running_is_finished_by_its_result_but_not_by_a_blind_reap(
    og: base.FakeOmnigent, tmp_path: Path
) -> None:
    og.final = "running"
    sid = child(og, tmp_path)
    assert "kept (running)" in omnigent_agent.finish_delivered(og, sid, settled=False)[0]
    assert og.archived == []
    assert omnigent_agent.finish_delivered(og, sid)[0].endswith("archived (running)")
    assert og.archived == [sid]


def test_a_refused_finish_is_reported_not_raised(
    og: base.FakeOmnigent, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = child(og, tmp_path)

    def refuse(s: str) -> None:
        raise omnigent_agent.Fail("PATCH: HTTP 503")

    og.archive = refuse  # type: ignore[method-assign]
    line, failed = omnigent_agent.finish_delivered(og, sid)
    assert failed and "finish failed: PATCH: HTTP 503" in line
    assert "delivered but was not finished" in capsys.readouterr().err


# --- run and wait ---------------------------------------------------------------------------


def test_wait_finishes_the_session_unless_kept_by_flag_or_card(
    og: base.FakeOmnigent, tmp_path: Path
) -> None:
    sid = child(og, tmp_path)
    og.prompts = [CARD]  # a card the owner has yet to answer
    assert omnigent_agent.main(["wait", sid, "r.md"]) == 0
    assert og.archived == []
    og.prompts = []
    assert omnigent_agent.main(["wait", sid, "r.md"]) == 0
    assert og.archived == [sid]
    other = child(og, tmp_path, "u")
    assert omnigent_agent.main(["wait", other, "r.md", "--keep"]) == 0
    assert og.archived == [sid]


def test_run_finishes_the_session_unless_keep(
    og: base.FakeOmnigent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(omnigent_agent, "wait", lambda *a, **k: "r.md")
    (tmp_path / "brief.md").write_text("do it")
    assert omnigent_agent.main(["run", "claude", "t", "brief.md", "r.md"]) == 0
    assert og.archived == ["s0"]
    assert omnigent_agent.main(["run", "claude", "u", "brief.md", "r2.md", "--keep"]) == 0
    assert og.archived == ["s0"]
    assert og.labels["s1"] == {omnigent_agent.KEEP_LABEL: "1"}  # the supervisor reads it too
    og.label = lambda *_: None  # type: ignore[method-assign]  # the flag alone keeps it, label or not
    assert omnigent_agent.main(["run", "claude", "v", "brief.md", "r3.md", "--keep"]) == 0
    assert og.archived == ["s0"]


# --- reap -------------------------------------------------------------------------------------


def test_reap_goes_through_the_same_function_and_keeps_what_it_keeps(
    og: base.FakeOmnigent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    real = omnigent_agent.finish_delivered
    monkeypatch.setattr(
        omnigent_agent, "finish_delivered", lambda o, s, **k: seen.append(s) or real(o, s, **k)
    )
    a, b = child(og, tmp_path, "a"), child(og, tmp_path, "b")
    og.labels[b] = {omnigent_agent.KEEP_LABEL: "1"}
    og.kids = [b]  # b is a's child: the cascade honours the label
    lines, failed = omnigent_agent.reap(og, [a])
    assert seen == [a, b] and not failed and og.archived == [a]  # b by the cascade
    assert "kept (--keep)" in lines[0]


def test_a_reap_naming_the_session_finishes_it_despite_its_keep_label_force_included(
    og: base.FakeOmnigent, tmp_path: Path
) -> None:
    a, b = child(og, tmp_path, "a"), child(og, tmp_path, "b")
    og.labels[a] = og.labels[b] = {omnigent_agent.KEEP_LABEL: "1"}
    og.kids = [b]
    assert "s0 : archived (idle)" in omnigent_agent.reap(og, [a], force=True)[0][0]
    assert og.archived == [a]  # b is a child, not named: it keeps
    assert omnigent_agent.reap(og, [b])[0][0].endswith("archived (idle)")
    assert og.archived == [a, b]


def test_a_top_level_lead_is_archived_when_its_starter_reaps_it(
    og: base.FakeOmnigent, tmp_path: Path
) -> None:
    sid, _ = omnigent_agent.start(
        og, "claude", "lead", "go", tmp_path / "r", None, 0, top_level=True
    )
    assert omnigent_agent.main(["reap", sid]) == 0
    assert og.archived == [sid]


# --- the supervisor ---------------------------------------------------------------------------


class Archiving(base.Leads):
    """Leads that record each session archived."""

    def __init__(self, **histories: list[dict]) -> None:
        super().__init__(**histories)
        self.archived: list[str] = []

    def archive(self, sid: str) -> None:
        self.archived.append(sid)


def relayed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, og: Archiving, **kw: object) -> Path:
    registry = tmp_path / "registry"
    report = base.aged(tmp_path / "b.md", 10)
    registry.write_text(f"b={report}\n")
    base.supervised(og, registry, monkeypatch, 2, **kw)
    return registry


def test_the_supervisor_archives_a_child_once_it_relays_its_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = Archiving(b=[base.told("brief", 60)])
    registry = relayed(tmp_path, monkeypatch, og)
    assert og.archived == ["b"] and registry.read_text() == ""


def test_the_supervisor_archives_a_failed_child_after_relaying_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = Archiving(b=[base.told("brief", 60)])
    og.snaps["b"] = {"status": "failed", "last_task_error": {"message": "boom"}}
    registry = tmp_path / "registry"
    registry.write_text(f"b={tmp_path / 'b.md'}\n")
    base.supervised(og, registry, monkeypatch, 2)
    assert [t for s, t in og.inbox] == [] and og.archived == ["b"]


@pytest.mark.parametrize("how", ["label", "card", "task", "asked"])
def test_the_supervisor_leaves_a_kept_child_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    og = Archiving(b=[base.told("brief", 60)])
    og.snaps["b"] = {
        "label": {"labels": {omnigent_agent.KEEP_LABEL: "1"}},
        "card": {"pending_elicitations": [CARD]},
        "task": {"background_tasks": [{"type": "shell", "status": "running"}]},
        "asked": {},
    }[how]
    registry = tmp_path / "registry"
    report = tmp_path / "b.md"
    if how == "asked":  # a question is not a result: nothing is taken
        base.aged(omnigent_agent.question_of(report), 10)
    else:
        base.aged(report, 10)
    registry.write_text(f"b={report}\n")
    base.supervised(og, registry, monkeypatch, 2)
    assert og.archived == []


def test_the_supervisor_archives_a_followed_lead_once_its_follower_is_told(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = base.follows(tmp_path, "a", "b", "c", after={"b": "a"})
    og = Archiving(
        a=[base.told("brief", 60)],
        b=[base.told("brief", 60), base.ending("STILL WORKING: waiting on lead a", 5)],
        c=[base.told("brief", 60), base.ending("STILL WORKING: waiting on CI", 5)],
    )

    def done(poll: int) -> None:
        if poll == 1:
            og.histories["a"].append(base.ending("DONE: PR #7 merged", 0))

    base.supervised(og, registry, monkeypatch, 3, done, notify="cos", console=True)
    assert og.archived == ["a"]  # b is told and still running; c waits on CI


def test_a_followed_lead_nobody_waits_on_stays_until_its_starter_reaps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = base.follows(tmp_path, "a")
    og = Archiving(a=[base.told("brief", 60)])

    def done(poll: int) -> None:
        if poll == 1:
            og.histories["a"].append(base.ending("DONE: all merged", 0))

    base.supervised(og, registry, monkeypatch, 3, done, notify="cos", console=True)
    assert og.archived == []


def test_a_lead_the_owner_drives_is_never_archived_by_the_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "registry"
    registry.write_text(
        f"a={tmp_path / 'a'}.md\tfollow\t{omnigent_agent.DIRECT_FIELD}\n"
        f"b={tmp_path / 'b'}.md\tfollow\tafter=a\n"
    )
    og = Archiving(
        a=[base.told("brief", 60)],
        b=[base.told("brief", 60), base.ending("STILL WORKING: waiting on lead a", 5)],
    )

    def done(poll: int) -> None:
        if poll == 1:
            og.histories["a"].append(base.ending("DONE: PR #7 merged", 0))

    base.supervised(og, registry, monkeypatch, 3, done, notify="cos", console=True)
    assert [s for s, _ in og.inbox if s == "b"] and og.archived == []  # told b, kept a


def test_a_stale_owner_round_label_holds_nothing_but_an_open_card_does(
    og: base.FakeOmnigent, tmp_path: Path
) -> None:
    sid = child(og, tmp_path)
    og.labels[sid] = {"omnigent.keep": "1"}  # set when a round needed the owner; never cleared
    og.prompts = [CARD]
    assert "kept (card open" in omnigent_agent.finish_delivered(og, sid)[0]
    og.prompts = []  # answered or withdrawn
    assert omnigent_agent.finish_delivered(og, sid)[0].endswith("archived (idle)")
    assert og.archived == [sid]


@pytest.mark.parametrize("status", ["failed", "cancelled", "errored"])
def test_run_writes_the_failed_report_and_archives_a_child_that_ended_without_a_result(
    og: base.FakeOmnigent, tmp_path: Path, status: str
) -> None:
    og.final = status
    (tmp_path / "brief.md").write_text("do it")
    assert omnigent_agent.main(["run", "claude", "t", "brief.md", "r.md", "--timeout", "5"]) != 0
    assert (tmp_path / "r.md").read_text().startswith("FAILED:")
    assert og.archived == ["s0"]


@pytest.mark.parametrize("status", ["failed", "cancelled", "errored"])
def test_a_run_that_ended_without_a_result_is_finished_too(
    og: base.FakeOmnigent, tmp_path: Path, status: str
) -> None:
    sid = child(og, tmp_path)
    og.final = status
    og.items = lambda s: [{"type": "message", "role": "user", "content": []}]  # type: ignore[method-assign]
    (tmp_path / "r.md").unlink()
    omnigent_agent.main(["wait", sid, "r.md", "--timeout", "1"])
    assert og.archived == [sid]


def test_archiving_a_parent_finishes_its_idle_children_and_spares_the_busy_ones(
    og: base.FakeOmnigent, tmp_path: Path
) -> None:
    parent = child(og, tmp_path, "p")
    idle, carded = child(og, tmp_path, "i"), child(og, tmp_path, "c")
    og.kids = [idle, carded]
    real = og.info
    og.info = lambda s: (  # type: ignore[method-assign]
        {**real(s), "pending_elicitations": [CARD]} if s == carded else real(s)
    )
    line, failed = omnigent_agent.finish_delivered(og, parent)
    assert not failed and og.archived == [parent, idle]
    assert "kept (card open" in line


def test_archiving_a_parent_spares_a_running_child(og: base.FakeOmnigent, tmp_path: Path) -> None:
    parent, busy = child(og, tmp_path, "p"), child(og, tmp_path, "b")
    og.kids = [busy]
    real = og.info
    og.info = lambda s: {**real(s), "status": "running"} if s == busy else real(s)  # type: ignore[method-assign]
    assert "kept (running)" in omnigent_agent.finish_delivered(og, parent)[0]
    assert og.archived == [parent]


@pytest.mark.parametrize("status", ["cancelled", "errored"])
def test_the_supervisor_archives_a_cancelled_or_errored_child_after_relaying_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    og = Archiving(b=[base.told("brief", 60)])
    og.snaps["b"] = {"status": status}
    registry = tmp_path / "registry"
    registry.write_text(f"b={tmp_path / 'b.md'}\n")
    base.supervised(og, registry, monkeypatch, 2)
    assert og.archived == ["b"]
