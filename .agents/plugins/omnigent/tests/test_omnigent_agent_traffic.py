"""One notice per event: the supervisor stays silent on what the lead already has, and owner questions are cards (#1529)."""

import contextlib
import time
from pathlib import Path

import pytest
import test_omnigent_agent as base
from test_omnigent_agent import (  # noqa: F401  (the fixtures apply here by name)
    COS,
    WORKER,
    Lead,
    ending,
    manager,
    no_model,
    no_sleep,
    supervised,
    told,
)

omnigent_agent = base.omnigent_agent

WAIT = "STILL WORKING: waiting on the check. Report: m.md"
CARD = {
    "elicitation_id": "question_q1",
    "params": {"message": "pick one", "ask_user_question": True},
}
PR = {omnigent_agent.PR_LABEL: "o/r#7", "omni.role": "worker"}
OTHER_PR = {omnigent_agent.PR_LABEL: "o/r#8", "omni.role": "worker"}
PROOF = {omnigent_agent.PR_LABEL: "o/r#7"}  # a proof run: no worker role


def told_m(og: base.Leads) -> list[str]:
    return [t for s, t in og.inbox if s == "m" and "a session it started" in t]


def run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker: dict,
    history=(),
    live=None,
    said: str = "DONE: checked. Report: w.md",
):
    """A manager idle an hour with ``WORKER`` its child, shaped by ``worker`` and ending ``said``; ``history`` joins its
    lead's, ``live`` is the session list."""
    tmp_path.mkdir(exist_ok=True)
    gone = {"status": "idle", "updated_at": time.time() - 1300, **worker}
    registry, og = manager(tmp_path, WAIT, m={"status": "idle"}, **{WORKER: gone})
    og.histories[WORKER] = [ending(said, 1300)]
    og.histories["m"].extend(history)
    og.call = lambda method, path, *a: {"data": live or []}  # type: ignore[method-assign]
    supervised(og, registry, monkeypatch, 2, notify=COS, console=True, fyi="post")
    return og


def test_an_unread_done_still_reaches_the_lead_once(tmp_path, monkeypatch) -> None:
    assert len(told_m(run(tmp_path, monkeypatch, {}))) == 1


def test_a_failed_worker_still_reaches_the_lead_once(tmp_path, monkeypatch) -> None:
    told = told_m(run(tmp_path, monkeypatch, {"status": "failed"}))
    assert len(told) == 1 and "failed" in told[0]


def test_an_archived_worker_raises_no_alert(tmp_path, monkeypatch) -> None:
    assert told_m(run(tmp_path, monkeypatch, {"archived": True})) == []


def test_a_failed_worker_whose_pr_has_a_newer_worker_raises_no_alert(tmp_path, monkeypatch) -> None:
    old = {"status": "failed", "labels": PR, "created_at": 100.0}
    newer = [{"id": "n" * 32, "labels": PR, "created_at": 200.0}]
    assert told_m(run(tmp_path, monkeypatch, old, live=newer)) == []


OLD = {"status": "failed", "labels": PR, "created_at": 100.0}


@pytest.mark.parametrize(
    "other",
    [
        {"labels": PR, "created_at": 50.0},  # older
        {"labels": PR, "created_at": 200.0, "archived": True},  # an archived replacement is none
        {"labels": OTHER_PR, "created_at": 200.0},  # another PR's worker
        {"labels": PROOF, "created_at": 200.0},  # a proof run on the same PR
    ],
)
def test_a_failed_worker_without_a_live_newer_worker_of_its_pr_still_alerts(
    tmp_path, monkeypatch, other
) -> None:
    live = [{"id": "n" * 32, **other}]
    assert len(told_m(run(tmp_path, monkeypatch, OLD, live=live))) == 1


def test_a_turn_end_the_lead_has_read_raises_no_alert(tmp_path, monkeypatch) -> None:
    seen = [
        {**told(f"[notice] {WORKER} ended DONE", 1000), "id": "n1"}
    ]  # after the end, 1300 s ago
    assert told_m(run(tmp_path, monkeypatch, {}, history=seen)) == []


def test_an_unrelated_message_after_the_end_is_no_read_of_it(tmp_path, monkeypatch) -> None:
    seen = [{**told("[owner] carry on with the other task", 1000), "id": "n1"}]
    assert len(told_m(run(tmp_path, monkeypatch, {}, history=seen))) == 1


def test_a_failure_the_lead_has_read_still_alerts(tmp_path, monkeypatch) -> None:
    seen = [told(f"[notice] {WORKER} failed", 1000)]
    assert len(told_m(run(tmp_path, monkeypatch, {"status": "failed"}, history=seen))) == 1


ASK = "USER NEEDED: pick one. Report: w.md"


def test_a_child_turn_end_with_no_open_card_is_still_forwarded(tmp_path, monkeypatch) -> None:
    assert len(told_m(run(tmp_path, monkeypatch, {}, said=ASK))) == 1


CARDED = {"pending_elicitations": [CARD]}
MENTIONING = [
    "DONE: shipped. Report: w.md\n- USER NEEDED: x",
    "DONE: shipped\n> USER NEEDED: x",
    "DONE: shipped\nneeds-owner: label removed",
]


@pytest.mark.parametrize("said", MENTIONING)
def test_a_done_that_mentions_the_owner_is_forwarded_though_a_card_is_open(
    tmp_path, monkeypatch, said
) -> None:
    assert len(told_m(run(tmp_path, monkeypatch, CARDED, said=said))) == 1


@pytest.mark.parametrize(
    "said",
    [
        ASK,
        "Two options were compared at length.\nUSER NEEDED: Postgres or SQLite? Report: w.md",
        "waiting on the card. Report: w.md",
        "needs owner: pick one. Report: w.md",
    ],
)
def test_a_child_turn_end_waiting_on_an_open_owner_card_is_not_forwarded(
    tmp_path, monkeypatch, said
) -> None:
    assert told_m(run(tmp_path, monkeypatch, CARDED, said=said)) == []


def turn_ends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    card: bool,
    said: str = ASK,
    user_last: bool = False,
) -> list[str]:
    items = [told("brief", 60), ending(said, 5)] + ([told(said, 3)] if user_last else [])
    og = Lead(*items)
    og.snap = {"status": "idle", **({"pending_elicitations": [CARD]} if card else {})}
    offset = base.skewed(monkeypatch)
    supervised(
        og,
        base.following(tmp_path),
        monkeypatch,
        3,
        lambda poll: offset.__setitem__(0, poll * 400.0),
        notify="cos",
        console=True,
    )
    return [t for t in base.posted(og) if "turn ended" in t]


def test_a_followed_lead_ending_on_its_open_card_is_not_forwarded(tmp_path, monkeypatch) -> None:
    assert turn_ends(tmp_path, monkeypatch, True) == []


def test_a_followed_lead_ending_with_no_open_card_is_still_forwarded(tmp_path, monkeypatch) -> None:
    assert len(turn_ends(tmp_path, monkeypatch, False)) == 1


@pytest.mark.parametrize("said", MENTIONING)
def test_a_followed_done_that_mentions_the_owner_is_forwarded_with_a_card_open(
    tmp_path, monkeypatch, said
) -> None:
    assert len(turn_ends(tmp_path, monkeypatch, True, said)) == 1


def test_a_followed_lead_whose_last_message_is_the_users_is_not_held_back_by_its_card(
    tmp_path, monkeypatch
) -> None:
    assert len(turn_ends(tmp_path, monkeypatch, True, user_last=True)) == 1


def test_relay_refuses_a_message_whose_own_line_is_an_owner_question(
    monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = base.FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    for text in (
        "USER NEEDED: which one?",
        "Compared both at length.\nUSER NEEDED: Postgres or SQLite?",
    ):
        assert omnigent_agent.main(["relay", "s1", text]) == 1
        err = capsys.readouterr().err
        assert "relay refused" in err and "sys_ask_user_question" in err
    assert og.posts == 0


@pytest.mark.parametrize(
    "text",
    [
        "> USER NEEDED: which one?\nthe owner said B",
        "- needs-owner is on it",
        "Settled: carry on.\nUSER NEEDED is for the owner only",
        "Body\n- USER NEEDED: x\n> needs owner: y",
        "needs-owner label removed",
    ],
)
def test_a_quoted_bullet_or_mentioning_line_is_no_owner_question(text: str) -> None:
    assert not omnigent_agent.asks_owner(text)


@pytest.mark.parametrize(
    "text", ["USER NEEDED: x", "**needs owner: x", "Question for the owner: x"]
)
def test_an_opening_owner_line_is_an_owner_question(text: str) -> None:
    assert omnigent_agent.asks_owner(text)


@pytest.mark.parametrize("flag", [["--answer"], ["--from", "task"]])
def test_relay_answer_and_source_are_never_refused(
    monkeypatch, capsys: pytest.CaptureFixture[str], flag: list[str]
) -> None:
    og = base.FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    with contextlib.suppress(
        Exception, SystemExit
    ):  # the fake knows no such session: only the refusal matters
        omnigent_agent.main(["relay", "s1", "USER NEEDED: B", *flag])
    assert "relay refused" not in capsys.readouterr().err


def test_withdraw_cancels_the_open_cards_only() -> None:
    og = base.FakeOmnigent()
    og.info = lambda sid: {  # type: ignore[method-assign]
        "pending_elicitations": [
            CARD,
            {"elicitation_id": "a1", "params": {"content_preview": "ls"}},
        ]
    }
    assert omnigent_agent.withdraw(og, "s") == [
        "session s: withdrew card question_q1: question_q1: pick one"
    ]
    assert og.resolved == [("s", "question_q1", "cancel", None)]
    og.info = lambda sid: {}  # type: ignore[method-assign]
    with pytest.raises(omnigent_agent.Fail):
        omnigent_agent.withdraw(og, "s")


def test_withdraw_by_id_cancels_that_card_only() -> None:
    og = base.FakeOmnigent()
    other = {"elicitation_id": "question_q2", "params": {"message": "again"}}
    og.info = lambda sid: {"pending_elicitations": [CARD, other]}  # type: ignore[method-assign]
    omnigent_agent.withdraw(og, "s", "question_q2")
    assert og.resolved == [("s", "question_q2", "cancel", None)]


def reaped(monkeypatch, newer: dict) -> tuple[base.FakeOmnigent, list[str]]:
    og = base.FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "pr_hold", lambda repo, pr: "o/r#7 is open")
    infos = {
        "old": dict(OLD),
        "new": {"labels": PR, "created_at": 200.0, "status": "running", **newer},
    }
    og.info = lambda sid: dict(infos[sid], id=sid)  # type: ignore[method-assign]
    og.call = lambda method, path, *a: {  # type: ignore[method-assign]
        "data": [dict(v, id=k) for k, v in infos.items()]
    }
    return og, omnigent_agent.reap(og, ["old"], force=True)[0]


def test_reap_force_finishes_a_replaced_worker_while_its_pr_is_open(monkeypatch) -> None:
    og, _ = reaped(monkeypatch, {})
    assert og.archived == ["old"]


@pytest.mark.parametrize("newer", [{"labels": PROOF}, {"archived": True}, {"labels": OTHER_PR}])
def test_reap_force_holds_a_worker_with_no_live_newer_worker_of_its_pr(monkeypatch, newer) -> None:
    og, lines = reaped(monkeypatch, newer)
    assert og.archived == [] and "kept (o/r#7 is open)" in lines[0]


def test_a_failed_proof_run_with_a_later_worker_still_alerts_and_stays_held(
    tmp_path, monkeypatch
) -> None:
    proof = {"status": "failed", "labels": PROOF, "created_at": 100.0}
    live = [{"id": "n" * 32, "labels": PR, "created_at": 200.0}]
    assert len(told_m(run(tmp_path, monkeypatch, proof, live=live))) == 1
    og = base.FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "pr_hold", lambda repo, pr: "o/r#7 is open")
    infos = {"old": dict(proof), "new": {"labels": PR, "created_at": 200.0, "status": "running"}}
    og.info = lambda sid: dict(infos[sid], id=sid)  # type: ignore[method-assign]
    og.call = lambda method, path, *a: {"data": [dict(v, id=k) for k, v in infos.items()]}  # type: ignore[method-assign]
    assert "kept" in omnigent_agent.reap(og, ["old"], force=True)[0][0] and og.archived == []


def test_reap_without_force_holds_a_replaced_worker(monkeypatch) -> None:
    og, _ = reaped(monkeypatch, {})
    og.archived.clear()
    assert "kept" in omnigent_agent.reap(og, ["old"])[0][0] and og.archived == []
