"""omnigent_agent.py sweep: the daily sweep of sessions idle over a day (#1580)."""

import json
from pathlib import Path

import pytest
import test_omnigent_agent as base
from test_omnigent_agent import no_model, no_sleep  # noqa: F401  (the fixtures apply here by name)

omnigent_agent = base.omnigent_agent
NOW = 1_800_000_000.0
DAY = 86400
CARD = {"elicitation_id": "question_1", "params": {"message": "Land it?"}}


class Swept(base.FakeOmnigent):
    """Sessions as the listing shows them, each with its own status, cards, tasks and history."""

    def __init__(self) -> None:
        super().__init__()
        self.base = "https://omnigent.example"
        self.rows: list[dict] = []
        self.status: dict[str, str] = {}
        self.cards: dict[str, list[dict]] = {}
        self.background: dict[str, list[dict]] = {}

    def add(
        self, sid: str, idle_hours: float = 30, history: list[dict] | None = None, **row: object
    ) -> str:
        self.rows.append(
            {"id": sid, "title": f"t-{sid}", "updated_at": NOW - idle_hours * 3600, **row}
        )
        self.sessions[sid] = history or [{"id": f"{sid}-1", "type": "message", "role": "assistant"}]
        if row.get("parent_session_id"):
            self.parents[sid] = str(row["parent_session_id"])
        return sid

    def listing(self) -> list[dict]:
        return [dict(r, archived=r["id"] in self.archived) for r in self.rows]

    def info(self, sid: str) -> dict:
        info = super().info(sid)
        info["status"] = self.status.get(sid, "idle")
        info["pending_elicitations"] = self.cards.get(sid, [])
        info["background_tasks"] = self.background.get(sid, [])
        info["title"] = f"t-{sid}"
        return info

    def children(self, sid: str) -> list[str]:
        return [r["id"] for r in self.rows if r.get("parent_session_id") == sid]


@pytest.fixture
def og(monkeypatch: pytest.MonkeyPatch) -> Swept:
    monkeypatch.setattr(omnigent_agent, "own_id", lambda: "me")
    return Swept()


def typed(text: str = "can you look at this") -> dict:
    return {
        "id": "u1",
        "type": "message",
        "role": "user",
        "created_by": "owner",
        "content": [{"text": text}],
    }


def run(og: Swept, **kw: object) -> tuple[list[str], list[dict], bool]:
    return omnigent_agent.sweep(og, now=NOW, **kw)


def test_an_idle_session_is_archived_through_finish_delivered(
    og: Swept, monkeypatch: pytest.MonkeyPatch
) -> None:
    finished = []
    real = omnigent_agent.finish_delivered
    monkeypatch.setattr(
        omnigent_agent, "finish_delivered", lambda o, s, **k: finished.append(s) or real(o, s, **k)
    )
    og.add("s1")
    og.add("fresh", idle_hours=2)
    lines, talked, failed = run(og)
    assert og.archived == ["s1"] and finished == ["s1"] and talked == [] and not failed
    assert lines == ["s1 t-s1: archived (idle)"]


@pytest.mark.parametrize("status", ["failed", "cancelled", "errored"])
def test_a_failed_session_counts_as_finished(og: Swept, status: str) -> None:
    og.status[og.add("s1")] = status
    run(og)
    assert og.archived == ["s1"]


@pytest.mark.parametrize(
    ("how", "why"),
    [
        ("running", "kept (running)"),
        ("card", "kept (card open: question_1"),
        ("task", "kept (background work)"),
        ("keep-id", "kept (keep list)"),
        ("keep-title", "kept (keep list)"),
        ("manager", "kept (a standing manager)"),
        ("me", None),
    ],
)
def test_each_keep_rule_holds_an_idle_session(og: Swept, how: str, why: str | None) -> None:
    sid = og.add("me" if how == "me" else "s1")
    keep: set[str] = set()
    if how == "running":
        og.status[sid] = "running"
    if how == "card":
        og.cards[sid] = [CARD]
    if how == "task":
        og.background[sid] = [{"type": "shell", "status": "running"}]
    if how == "keep-id":
        keep = {sid}
    if how == "keep-title":
        keep = {f"t-{sid}"}
    if how == "manager":
        og.labels[sid] = {"omni.role": "cos"}
    lines, talked, failed = run(og, keep=keep)
    assert og.archived == [] and talked == [] and not failed
    assert (lines == []) if why is None else (lines[0].startswith(f"{sid} t-{sid}: {why}"))


def test_a_stale_keep_label_holds_only_while_a_reason_stands(og: Swept) -> None:
    sid = og.add("s1")
    og.labels[sid] = {"omnigent.keep": "1"}  # set when a round needed the owner; never cleared
    og.cards[sid] = [CARD]
    assert "kept (card open" in run(og)[0][0] and og.archived == []
    og.cards[sid] = []  # the card is answered: the label alone is stale
    assert run(og)[0] == ["s1 t-s1: archived (idle)"] and og.archived == ["s1"]


def test_the_keep_list_is_a_file_of_ids_and_titles(tmp_path: Path) -> None:
    keep = tmp_path / "keep"
    keep.write_text("# kept by hand\ns7\n  my long lead  # until the release\n\n")
    assert omnigent_agent.keep_list(keep) == {"s7", "my long lead"}
    assert omnigent_agent.keep_list(tmp_path / "missing") == set()


def test_a_session_the_owner_talked_to_is_never_archived_and_goes_on_the_card(og: Swept) -> None:
    og.add("talked", history=[typed(), {"id": "a", "type": "message", "role": "assistant"}])
    og.add("briefed", history=[{**typed("Work on #1 x. Your brief:"), "id": "b"}])
    og.add("relayed", history=[{**typed("[relayed by cos] go"), "id": "c"}])
    og.add("peer", history=[{**typed(), "sender_session_id": "other"}])
    og.add("meta", history=[{**typed(), "is_meta": True}])
    lines, talked, _ = run(og)
    assert talked == [{"id": "talked", "title": "t-talked"}]
    assert sorted(og.archived) == ["briefed", "meta", "peer", "relayed"]
    assert "talked t-talked: kept (the owner talked to it: on the owner card)" in lines


def test_a_talked_to_child_is_not_finished_with_its_parent(og: Swept) -> None:
    og.add("parent")
    og.add("kid", parent_session_id="parent", history=[typed()])
    og.add("other-kid", parent_session_id="parent", idle_hours=1)  # not idle itself: stays open
    _, talked, _ = run(og)
    assert [t["id"] for t in talked] == ["kid"]
    assert og.archived == ["parent"]


def test_the_owner_talked_set_makes_one_multiselect_card_with_archive_all_first(og: Swept) -> None:
    for sid in ("a1", "a2", "a3"):
        og.add(sid, history=[typed()])
    _, talked, _ = run(og)
    card = omnigent_agent.sweep_card(og.base, talked)
    assert card is not None and len(card["questions"]) == 1
    q = card["questions"][0]
    assert q["multiSelect"] is True
    assert q["options"][0]["label"] == "Archive all (Recommended)"
    assert [o["label"] for o in q["options"][1:]] == [
        "Keep t-a1 (a1)",
        "Keep t-a2 (a2)",
        "Keep t-a3 (a3)",
    ]
    assert all(f"({og.base}/c/{s})" in q["question"] for s in ("a1", "a2", "a3"))
    assert omnigent_agent.sweep_card(og.base, []) is None


def test_a_dry_run_archives_nothing(og: Swept) -> None:
    og.add("s1")
    assert run(og, dry_run=True)[0] == ["s1 t-s1: would archive (idle 30 h)"] and og.archived == []


def test_the_command_prints_lines_then_the_card(
    og: Swept, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda *a: og)
    monkeypatch.setattr(omnigent_agent.time, "time", lambda: NOW)
    og.add("s1")
    og.add("talked", history=[typed()])
    keep = tmp_path / "keep"
    keep.write_text("nothing\n")
    monkeypatch.setattr("sys.argv", ["omnigent_agent.py", "sweep", "--keep-file", str(keep)])
    assert omnigent_agent.main() == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-1].startswith("CARD: ")
    assert (
        json.loads(out[-1].removeprefix("CARD: "))["questions"][0]["options"][1]["label"]
        == "Keep t-talked (talked)"
    )
    assert og.archived == ["s1"]


def test_a_parents_finish_spares_its_kept_talked_and_busy_children(og: Swept) -> None:
    """The cascade never takes a child a keep rule holds, idle or not (verifier F1)."""
    og.add("parent")
    og.add("talked_kid", parent_session_id="parent", idle_hours=2, history=[typed()])
    og.add("listed_kid", parent_session_id="parent", idle_hours=2)
    og.add("plain_kid", parent_session_id="parent", idle_hours=2)
    og.add("idle_kid", parent_session_id="parent")
    og.add("idle_listed", parent_session_id="parent")
    run(og, keep={"t-listed_kid", "t-idle_listed"})
    assert og.archived == ["idle_kid", "parent"]


def test_a_session_idle_exactly_a_day_is_not_yet_swept(og: Swept) -> None:
    og.add("exact", idle_hours=24)
    og.add("over", idle_hours=24.01)
    run(og)
    assert og.archived == ["over"]


def test_an_idle_owner_talked_child_goes_on_the_card_when_its_parent_is_archived(og: Swept) -> None:
    og.add("parent")
    og.add("talked_kid", parent_session_id="parent", history=[typed()])
    _, talked, _ = run(og)
    assert talked == [{"id": "talked_kid", "title": "t-talked_kid"}] and og.archived == ["parent"]


def test_a_keep_entry_matches_a_whole_id_or_title_never_a_prefix(og: Swept) -> None:
    og.add("s1")
    run(og, keep={"s", "t", "t-s"})
    assert og.archived == ["s1"]


@pytest.mark.parametrize("how", ["directory", "unreadable"])
def test_a_keep_list_that_cannot_be_read_fails_the_sweep_and_archives_nothing(
    og: Swept,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    how: str,
) -> None:
    keep = tmp_path / "keep"
    if how == "directory":
        keep.mkdir()
    else:
        keep.write_text("s1\n")
        real = Path.read_text
        monkeypatch.setattr(
            Path,
            "read_text",
            lambda p, *a, **k: (
                (_ for _ in ()).throw(PermissionError(13, "denied"))
                if p == keep
                else real(p, *a, **k)
            ),
        )
    with pytest.raises(omnigent_agent.Fail, match="keep list .* can't be read"):
        omnigent_agent.keep_list(keep)
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda *a: og)
    og.add("s1")
    monkeypatch.setattr("sys.argv", ["omnigent_agent.py", "sweep", "--keep-file", str(keep)])
    assert omnigent_agent.main() == 1
    assert og.archived == [] and "can't be read" in capsys.readouterr().err


def test_an_idle_parent_is_archived_while_its_fresh_kept_and_talked_children_stay(
    og: Swept,
) -> None:
    """The lead's F1 case: parent idle 30 h; (a) idle 2 h, (b) idle 30 h on the keep list, (c) idle 30 h owner-talked."""
    og.add("parent")
    og.add("a", parent_session_id="parent", idle_hours=2)
    og.add("b", parent_session_id="parent")
    og.add("c", parent_session_id="parent", history=[typed()])
    _, talked, failed = run(og, keep={"b"})
    assert og.archived == ["parent"] and not failed
    assert talked == [{"id": "c", "title": "t-c"}]
    card = omnigent_agent.sweep_card(og.base, talked)
    assert card is not None and [o["label"] for o in card["questions"][0]["options"]] == [
        "Archive all (Recommended)",
        "Keep t-c (c)",
    ]
