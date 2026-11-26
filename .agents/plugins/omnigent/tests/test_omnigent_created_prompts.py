"""omnigent_agent.py: a supervisor reports a prompt on any session its followed lead created (#1633)."""

import pytest
import test_omnigent_agent as base  # the faked server
from test_omnigent_agent import no_model, no_sleep  # noqa: F401  (the fixtures apply here by name)

omnigent_agent = base.omnigent_agent
PROMPT = {"elicitation_id": "e1", "params": {"message": "Claude Cloud wants to use Bash"}}


def lead_with_worker(lead_status: str, pending: list[dict]) -> base.FakeOmnigent:
    """Lead ``k``, its last message an assistant reply; ``w``, a --top-level worker it created."""
    og = base.running_session(10**5)
    og.sessions["k"].append(
        {"id": "i2", "type": "message", "role": "assistant", "created_at": 0, "content": []}
    )
    infos = {
        "k": {"status": lead_status, "title": "lead"},
        "w": {"status": "running", "title": "worker", "pending_elicitations": pending},
    }
    og.info = lambda s: infos[s]  # type: ignore[method-assign]
    og.creations = lambda s: ["w"] if s == "k" else []  # type: ignore[method-assign]
    return og


@pytest.mark.parametrize("status", ["running", "idle"])
def test_a_prompt_on_a_session_the_lead_created_is_its_blocked_prompt_told_once_it_holds(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    og = lead_with_worker(status, [PROMPT])
    told: dict = {}
    holding: dict = {}
    # held for PROMPT_CONFIRM_SECONDS first, as a mirrored prompt is
    assert omnigent_agent.follow(og, "k", {}, told, holding) == ("", ("blocked", "w:e1"))
    monkeypatch.setattr(omnigent_agent, "PROMPT_CONFIRM_SECONDS", 0.0)
    line, key = omnigent_agent.follow(og, "k", {}, told, holding)
    assert key == ("blocked", "w:e1")
    assert line == (
        "k (in 1 session(s) it created): blocked on a prompt:"
        " e1: Claude Cloud wants to use Bash (in w worker)"
    )
    assert omnigent_agent.follow(og, "k", {}, told, holding)[0] == ""  # told once
    # the owner item drops the prompt's id, as it does a mirrored prompt's
    assert omnigent_agent.owner_item(line) == (
        "blocked on a prompt: Claude Cloud wants to use Bash (in w worker)"
    )


def test_every_blocked_session_the_lead_created_is_named_not_only_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(omnigent_agent, "PROMPT_CONFIRM_SECONDS", 0.0)
    og = lead_with_worker("idle", [PROMPT])
    other = {"elicitation_id": "e2", "params": {"message": "Claude Cloud wants to use Edit"}}
    infos = {"w2": {"status": "running", "title": "second", "pending_elicitations": [other]}}
    info = og.info
    og.info = lambda s: infos.get(s) or info(s)  # type: ignore[method-assign]
    og.creations = lambda s: ["w", "w2"]  # type: ignore[method-assign]
    line, key = omnigent_agent.follow(og, "k", {}, {}, {})
    assert key == ("blocked", "w:e1,w2:e2")
    assert "(in 2 session(s) it created)" in line
    assert "use Bash (in w worker); e2: Claude Cloud wants to use Edit (in w2 second)" in line


def test_a_question_card_on_a_created_session_blocks_nothing() -> None:
    card = {"elicitation_id": "question_1", "params": {"message": "pick one"}}
    og = lead_with_worker("running", [card])
    assert omnigent_agent.follow(og, "k", {}, {}, {}) == ("", ("running",))


def test_creations_are_the_live_top_level_sessions_it_made_from_one_listing_per_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {"id": "w1", "labels": {"omnigent.created_by_session_id": "k"}},  # as a live row names it
        {"id": "w6", "labels": {"omni.created_by": "k"}},  # a top-level start of this pack's
        {"id": "w2", "created_by_session_id": "k", "parent_session_id": "k"},  # mirrored: its own
        {"id": "w3", "created_by_session_id": "k", "archived": True},
        {"id": "w4", "created_by_session_id": "other"},
        {"id": "w5", "created_by_session_id": "k", "parent_session_id": "p"},
    ]
    calls: list[str] = []

    def call(method: str, path: str, body: object = None) -> dict:
        calls.append(path)
        if "after=" in path:
            return {"data": rows[3:]}
        return {"data": rows[:3], "has_more": True, "last_id": "w2"}

    og = omnigent_agent.Omnigent.__new__(omnigent_agent.Omnigent)
    og.call = call  # type: ignore[method-assign]
    assert og.creations("k") == ["w1", "w6", "w5"]
    assert og.creations("other") == ["w4"]
    assert len(calls) == 2  # two pages, read once for both leads
    monkeypatch.setattr(omnigent_agent, "CREATED_SECONDS", 0.0)
    og.creations("k")
    assert len(calls) == 4


def test_a_top_level_create_labels_the_session_that_started_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(omnigent_agent, "own_id", lambda: "lead1")
    monkeypatch.setattr(omnigent_agent, "parent_id", lambda: None)
    calls = base._agent_server(
        monkeypatch,
        {
            "/v1/projects": {"data": []},
            "/v1/sessions": {"id": "s1"},
            "/v1/sessions/lead1": {"id": "lead1"},
        },
    )
    monkeypatch.setattr(
        omnigent_agent.Omnigent, "agent", lambda self, name: {"id": name, "harness": "x"}
    )
    omnigent_agent.Omnigent().create("claude-cloud", "t", top_level=True)
    assert calls[-1][2]["labels"]["omni.created_by"] == "lead1"
    omnigent_agent.Omnigent().create("claude-cloud", "t2")  # a child: its parent names it
    assert "omni.created_by" not in (calls[-1][2].get("labels") or {})
