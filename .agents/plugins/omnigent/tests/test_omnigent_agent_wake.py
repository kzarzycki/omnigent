"""supervise wakes the notified session only for owner cards, true stalls and failures (#1426, #1403)."""

import time
from pathlib import Path

import pytest
from test_omnigent_agent import (
    COS,
    Lead,
    blocked_on,
    deadlines,
    ending,
    following,
    manager,
    posted,
    skewed,
    stalls,
    supervised,
    told,
)


def raised(
    og: Lead, registry: Path, polls: int, monkeypatch: pytest.MonkeyPatch, **kw
) -> list[str]:
    offset = skewed(monkeypatch)

    def later(poll: int) -> None:
        offset[0] = poll * 400.0

    supervised(og, registry, monkeypatch, polls, later, notify="cos", console=True, **kw)
    return posted(og)


def test_a_done_still_reaches_the_notified_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 60), ending("DONE: shipped", 5))
    [post] = raised(og, registry, 3, monkeypatch)
    assert "[act] a t-a: turn ended" in post


def test_a_new_owner_card_is_raised_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 60), ending("STILL WORKING: its worker runs", 5))
    og.snap = blocked_on("e1")
    wait = omnigent_agent_confirm()
    offset = skewed(monkeypatch)
    supervised(
        og,
        registry,
        monkeypatch,
        5,
        lambda p: offset.__setitem__(0, {1: 30.0, 2: wait, 3: wait + 600}.get(p, wait + 900)),
        notify="cos",
        console=True,
    )
    [post] = posted(og)
    assert "[owner] a t-a: blocked on a prompt" in post


def omnigent_agent_confirm() -> float:
    from test_omnigent_agent import omnigent_agent

    return omnigent_agent.PROMPT_CONFIRM_SECONDS


def test_a_session_that_fails_again_after_its_revive_reaches_the_notified_session_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 60), ending("STILL WORKING: building", 50))
    og.snap = {"status": "failed", "last_task_error": {"message": "boom"}}
    states = {1: "running", 2: "failed", 3: "failed"}
    supervised(
        og,
        registry,
        monkeypatch,
        4,
        lambda p: og.snap.update(status=states[p]),
        notify="cos",
        console=True,
    )
    [post] = posted(og)
    assert "[act] a t-a: failed: boom (failed again after its revive)" in post


WAIT = "STILL WORKING: waiting on the {}. Report: m.md"


def test_a_wait_on_a_followed_lead_by_its_title_word_raises_no_deadline_while_it_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, og = manager(tmp_path, WAIT.format("queue"), y={"title": "queue lead"})
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    assert stalls(og) == [] and deadlines(registry) == []


def test_a_wait_on_a_followed_lead_by_its_title_word_raises_its_deadline_once_the_lead_is_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet = {"title": "queue lead", "status": "idle", "updated_at": time.time() - 1300}
    registry, og = manager(tmp_path, WAIT.format("queue"), y=quiet)
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    assert stalls(og) == ["Supervisor: m t-m: no progress 60m (queue)"]


def test_a_wait_that_names_no_session_raises_one_deadline_across_progress_chains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = WAIT.format("rest")
    registry, og = manager(tmp_path, text)

    def later(poll: int) -> None:  # new progress, the same wait: a new chain
        og.histories["m"].append(ending(text, 3000 - poll))

    supervised(og, registry, monkeypatch, 3, later, notify=COS, console=True, fyi="post")
    assert len(stalls(og)) == 1


def test_a_wait_that_names_no_session_yet_raises_its_deadline_once_a_lead_with_that_word_goes_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, og = manager(tmp_path, WAIT.format("queue"))

    def later(poll: int) -> None:  # a lead titled "queue" joins and is quiet; new progress for m
        og.snaps["y"].update(title="queue lead", status="idle", updated_at=time.time() - 1300)
        og.histories["m"].append(ending(WAIT.format("queue"), 3000 - poll))

    supervised(og, registry, monkeypatch, 3, later, notify=COS, console=True, fyi="post")
    # One event per declared wait (#2150): the wait is keyed on its text and the sessions it names (`queue`, then
    # `queue|` once the lead joins the registry), so a reply that restates it (the third poll's progress) fires no
    # third deadline; it fires once more only after 4x DEADLINE_SECONDS.
    assert stalls(og) == [
        "Supervisor: m t-m: no progress 60m (queue)",
        "Supervisor: m t-m: no progress 50m (queue)",
    ]
