"""omnigent_agent.py: an open question card holds no input; a parked question or approval does (#977)."""

import time
from pathlib import Path

import pytest
import test_omnigent_agent as base
from test_omnigent_agent import no_model, no_sleep  # noqa: F401  (the fixtures apply here by name)

omnigent_agent = base.omnigent_agent

CARD = {"elicitation_id": "question_476b", "params": {"message": "Land it?"}}
PARKED = {
    "elicitation_id": "elicit_question_1",
    "params": {
        "message": "Which one?",
        "ask_user_question": {"questions": [{"question": "Which?"}]},
    },
}
APPROVAL = {
    "elicitation_id": "elicit_approval_1",
    "params": {"message": "Claude wants to call Bash"},
}
CARD_ANSWERABLE = {
    "elicitation_id": "question_9",
    "params": {"message": "Pick", "ask_user_question": {"questions": [{"question": "Pick?"}]}},
}


@pytest.fixture(autouse=True)
def queued_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # a held message queues once it has been pending QUEUED_SECONDS of the clock; `hold` counts polls, which a fast host
    # drains within those seconds, so the held message would land instead of queueing
    monkeypatch.setattr(omnigent_agent, "QUEUED_SECONDS", 0)


def fake(*prompts: dict, hold: int = 0) -> base.FakeOmnigent:
    og = base.FakeOmnigent(hold=hold)
    og.sessions["s1"] = []
    og.prompts = list(prompts)
    return og


def test_holds_input_by_kind() -> None:
    assert not omnigent_agent.holds_input(CARD)
    assert not omnigent_agent.holds_input(
        {"elicitation_id": "x", "params": {"policy_name": "sys_ask_user_question"}}
    )
    assert omnigent_agent.holds_input(PARKED)
    assert omnigent_agent.holds_input(  # the harness's own id wins over a card policy name
        {"elicitation_id": "elicit_question_2", "params": {"policy_name": "sys_ask_user_question"}}
    )
    assert omnigent_agent.holds_input(APPROVAL)


def test_prompts_and_questions_ignore_a_card() -> None:
    info = {"pending_elicitations": [CARD, CARD_ANSWERABLE, PARKED, APPROVAL]}
    assert omnigent_agent.prompts_of(info) == [
        "elicit_question_1: Which one?",
        "elicit_approval_1: Claude wants to call Bash",
    ]
    assert omnigent_agent.questions_of(info) == ["elicit_question_1: Which one?"]
    assert omnigent_agent.cards_of(info) == ["question_476b: Land it?", "question_9: Pick"]


def test_relay_delivers_past_a_card_but_queues_behind_a_parked_question() -> None:
    og = fake(CARD)
    assert omnigent_agent.relay(og, "s1", "hi", 0).startswith("session s1: delivered")
    with pytest.raises(omnigent_agent.Queued, match="behind a question"):
        omnigent_agent.relay(fake(PARKED, hold=10**6), "s1", "hi", 0)
    with pytest.raises(omnigent_agent.Queued, match="behind an approval prompt"):
        omnigent_agent.relay(fake(APPROVAL, hold=10**6), "s1", "hi", 0)
    line = omnigent_agent.relay(fake(CARD, hold=10**6), "s1", "hi", 0)  # a step holds it: no raise
    assert line == "session s1: queued until the session's current step ends\nit lands then"


@pytest.mark.parametrize("held", [PARKED, APPROVAL])
def test_relay_skip_skips_a_held_prompt_not_a_card(held: dict) -> None:
    assert "skipped" in omnigent_agent.relay(fake(held), "s1", "x", 0, skip=True)
    assert "skipped" not in omnigent_agent.relay(fake(CARD), "s1", "x", 0, skip=True)


def test_relay_answer_still_answers_a_card() -> None:
    og = fake(CARD_ANSWERABLE)
    line = omnigent_agent.relay(og, "s1", "yes", 0, answer=True)
    assert "answered its open question" in line
    assert og.resolved == [("s1", "question_9", "accept", {"Pick?": "yes"})]


def test_approve_still_resolves_approvals_only() -> None:
    og = fake(CARD, APPROVAL)
    lines, _ = omnigent_agent.approve(og, "s1")
    assert [r[1] for r in og.resolved] == ["elicit_approval_1"] and len(lines) == 1


@pytest.mark.parametrize("force", [False, True])
def test_reap_keeps_a_session_with_an_open_card_and_a_parked_question_alike(force: bool) -> None:
    states = {"card": [CARD], "parked": [PARKED], "free": []}
    og = base.FakeOmnigent()
    og.info = lambda s: {"status": "idle", "title": s, "pending_elicitations": states[s]}  # type: ignore[method-assign]
    og.children = lambda p: list(states)  # type: ignore[attr-defined]
    lines, _ = omnigent_agent.reap(og, [], parent="p", force=force)
    assert og.archived == ["free"]
    assert lines[0] == "card card: kept (card open: question_476b: Land it?)"
    assert lines[1] == "parked parked: kept (blocked on a prompt)"


def test_watch_status_shows_a_card_open_not_blocked() -> None:
    og = fake(CARD)
    info = {"status": "idle", "pending_elicitations": [CARD]}
    assert omnigent_agent.lead_status(info, [])["state"] != "blocked"
    assert (
        omnigent_agent.lead_status({**info, "pending_elicitations": [PARKED]}, [])["state"]
        == "blocked"
    )
    assert og.info("s1")["pending_elicitations"] == [CARD]


@pytest.mark.parametrize(("kind", "held"), [("card", False), ("parked", True), ("approval", True)])
def test_the_supervisors_alive_counts_a_card_session_as_alive_and_a_held_one_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, held: bool
) -> None:
    """A manager waiting on a running child: with a card open the child still shows life, so no
    deadline is raised; with a parked question or an approval prompt it does not."""
    prompt = {"card": CARD, "parked": PARKED, "approval": APPROVAL}[kind]
    registry, og = base.manager(
        tmp_path,
        f"STILL WORKING: waiting on {base.WORKER}. Report: m.md",
        **{base.WORKER: {"pending_elicitations": [prompt]}},
    )
    base.supervised(og, registry, monkeypatch, 1, notify=base.COS, console=True, fyi="post")
    assert base.stalls(og) == (
        [f"Supervisor: m t-m: no progress 60m ({base.WORKER})"] if held else []
    )


@pytest.mark.parametrize(
    ("kind", "skipped"), [("card", False), ("parked", True), ("approval", True)]
)
def test_broadcast_delivers_to_a_card_session_and_skips_a_held_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    skipped: bool,
) -> None:
    og = base.BroadcastFake()
    sid = og.add("asking")
    og.prompts = [{"card": CARD, "parked": PARKED, "approval": APPROVAL}[kind]]
    assert base.broadcast(og, tmp_path, monkeypatch, "--include", sid) == 0
    out = capsys.readouterr().out
    if skipped:
        assert out.endswith(": skipped: waiting on a question\n") and og.posts == 0
    else:
        assert ": delivered" in out and og.posts == 1
        assert "queued" not in out and "skipped" not in out


def stuck_running(prompts: list[dict]) -> base.FakeOmnigent:
    """One running session ``s`` that wrote nothing for hours, with ``prompts`` pending."""
    og = base.FakeOmnigent()
    old = time.time() - 4 * 3600
    og.info = lambda s: {  # type: ignore[method-assign]
        "status": "running",
        "title": s,
        "pending_elicitations": prompts,
    }
    og.items = lambda s: [{"type": "message", "role": "user", "created_at": old}]  # type: ignore[method-assign]
    return og


def test_reap_force_neither_interrupts_nor_archives_a_stuck_running_card_session() -> None:
    og = stuck_running([CARD])
    lines, failed = omnigent_agent.reap(og, ["s"], force=True)
    assert lines == ["s s: kept (card open: question_476b: Land it?)"] and not failed
    assert og.interrupted == [] and og.archived == []


def test_reap_force_still_interrupts_and_archives_a_stuck_running_session_without_a_card() -> None:
    og = stuck_running([])
    lines, _ = omnigent_agent.reap(og, ["s"], force=True)
    assert og.interrupted == ["s"] and og.archived == ["s"]
    assert lines[0].startswith("s s: archived (stuck)")


def test_observe_names_a_card_open_and_blocks_on_the_held_prompts_only() -> None:
    og = fake(CARD)
    key, line = omnigent_agent.observe(og, "s1", {})
    assert (
        key[0] != "blocked"
        and "card open: question_476b: Land it?" in line
        and "blocked" not in line
    )
    og = fake(CARD, APPROVAL)
    key, line = omnigent_agent.observe(og, "s1", {})
    assert key == ("blocked", "elicit_approval_1")
    assert "blocked on a prompt: elicit_approval_1: Claude wants to call Bash" in line
    key, line = omnigent_agent.observe(fake(PARKED), "s1", {})
    assert (
        key == ("blocked", "elicit_question_1")
        and "blocked on a prompt: elicit_question_1: Which one?" in line
    )


LONG = "Which one? " * 400


@pytest.mark.parametrize(
    ("questions", "label"),
    [([], LONG[:80] + "…"), ([{"header": "Pick one", "question": LONG}], "“Pick one”")],
)
def test_relay_queued_behind_a_long_question_names_its_id_not_its_text(
    questions: list, label: str
) -> None:
    long = {
        "elicitation_id": "elicit_question_long",
        "params": {"message": LONG, "ask_user_question": {"questions": questions}},
    }
    with pytest.raises(omnigent_agent.Queued) as err:
        omnigent_agent.relay(fake(long, hold=10**6), "s1", "hi", 0)
    line = str(err.value).splitlines()[0]
    assert len(line) < 200 and "elicit_question_long" in line and label in line
