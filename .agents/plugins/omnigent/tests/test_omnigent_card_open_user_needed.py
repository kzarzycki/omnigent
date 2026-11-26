"""omnigent_agent.py supervise: a lead whose turn ends USER NEEDED with its question card open is recorded, not posted;
with no card it still posts (the unasked event) (#1575); a card unanswered past CARD_REMIND_SECONDS reaches the
manager once (#2170)."""

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

CARD = {
    "elicitation_id": "q1",
    "params": {
        "policy_name": "sys_ask_user_question",
        "ask_user_question": {"questions": [{"question": "Which base?"}]},
        "message": "Which base?",
    },
}


def run(tmp_path, monkeypatch, pending):
    registry = following(tmp_path)
    monkeypatch.setattr(omnigent_agent, "PROMPT_CONFIRM_SECONDS", 0.0)
    offset = skewed(monkeypatch)
    og = Lead(told("brief", 60), ending("USER NEEDED: which base?", 5))
    og.snap = {"host_id": "h", "pending_elicitations": pending}

    def later(poll):
        offset[0] = {1: 30.0, 2: 61.0}.get(poll, 400.0)

    supervised(og, registry, monkeypatch, 3, later, notify="cos", console=True)
    return [(e["kind"], e["action"]) for e in events_of(registry)], posted(og)


def test_card_open_records(tmp_path, monkeypatch):
    routed, posts = run(tmp_path, monkeypatch, [CARD])
    assert not any(kind == "ended" for kind, _ in routed)  # told above, as #1529 does
    assert not any("USER NEEDED" in p for p in posts)


def test_no_card_still_posts(tmp_path, monkeypatch):
    routed, posts = run(tmp_path, monkeypatch, [])
    assert routed and routed[0][0] == omnigent_agent.UNASKED
    assert posts


def remind(tmp_path, monkeypatch, pending):
    """The events and posts of a lead whose card stays ``pending`` past ``CARD_REMIND_SECONDS`` over four polls."""
    registry = following(tmp_path)
    monkeypatch.setattr(omnigent_agent, "PROMPT_CONFIRM_SECONDS", 0.0)
    offset = skewed(monkeypatch)
    og = Lead(told("brief", 60), ending("USER NEEDED: which base?", 5))
    og.snap = {"host_id": "h", "pending_elicitations": pending}
    past = omnigent_agent.CARD_REMIND_SECONDS

    def later(poll):
        offset[0] = {1: 30.0, 2: past + 60.0, 3: past + 400.0}.get(poll, 2 * past + 900.0)

    supervised(og, registry, monkeypatch, 4, later, notify="cos", console=True)
    return [(e["kind"], e["action"]) for e in events_of(registry)], posted(og)


def test_a_card_unanswered_past_the_reminder_reaches_the_manager_once(tmp_path, monkeypatch):
    """#2170: the manager hears once, as act, so one the owner told to act in their name can answer the card."""
    routed, posts = remind(tmp_path, monkeypatch, [CARD])
    assert routed.count(("card-waiting", "act")) == 1
    [post] = [p for p in posts if "waits unanswered" in p]
    assert "[act]" in post and "q1" in post


def test_an_answered_card_or_a_direct_lead_raises_no_reminder(tmp_path, monkeypatch):
    routed, posts = remind(tmp_path, monkeypatch, [])
    assert not any(kind == "card-waiting" for kind, _ in routed) and not any(
        "waits unanswered" in p for p in posts
    )
    event = {"kind": "card-waiting", "line": "a: its card waits unanswered", "direct": True}
    assert omnigent_agent.route(event, lambda: ("none", ""))[1] == "record"
