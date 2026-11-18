"""Server side of replacing an adopted runner on an older build once its session is idle."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from omnigent.entities.conversation import Conversation, MessageData, NewConversationItem
from omnigent.host.frames import HostReplaceRunnerFrame
from omnigent.server import stale_runner_replacement
from omnigent.server.routes import terminal_attach
from omnigent.server.stale_runner_replacement import (
    NO_ROUTE,
    ReplacementDeps,
    replace_stale_runner,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

pytestmark = pytest.mark.asyncio

_HOST = "0" * 31 + "1"
_OLD = "runner-old"
_NEW = "runner-new"


@dataclass
class _FakeSessionMachinery:
    """Records launches, stops and resumes; the new runner connects unless told not to."""

    store: SqlAlchemyConversationStore
    new_runner_connects: bool = True
    queued: set[str] = field(default_factory=set)
    launched: list[str] = field(default_factory=list)
    stopped: list[tuple[str, str]] = field(default_factory=list)
    resumed: list[tuple[str, str | None]] = field(default_factory=list)
    on_new_runner_up: list[str] = field(default_factory=list)
    # What the old runner answers about background work; ``None`` is no answer.
    old_runner_background: bool | str | None = False
    background_starts_during_boot: bool = False
    asked_background: list[tuple[str, str | None]] = field(default_factory=list)
    # The old runner reports background work after the check, before the rebind.
    background_reported_before_rebind: bool = False
    told_lost_background_work: list[str] = field(default_factory=list)

    async def launch(self, conv: Conversation) -> tuple[str, str | None]:
        self.launched.append(conv.id)
        if self.background_reported_before_rebind:
            self.store.set_background_work_running(conv.id, True, runner_id=_OLD)
        self.store.replace_runner_id(conv.id, _NEW)
        return _NEW, None

    async def wait_connected(self, runner_id: str, timeout_s: float) -> bool:
        for session_id in self.on_new_runner_up:
            self.store.set_session_live_status(session_id, "running")
        if self.background_starts_during_boot:
            self.old_runner_background = True
        return self.new_runner_connects

    async def stop(self, session_id: str, host_id: str, runner_id: str, reason: str) -> bool:
        self.stopped.append((runner_id, reason))
        return True

    async def resume(self, conv: Conversation) -> None:
        self.resumed.append((conv.id, conv.runner_id))

    async def runner_background_work(self, conv: Conversation) -> bool | str | None:
        self.asked_background.append((conv.id, conv.runner_id))
        return self.old_runner_background

    async def resume_lost_background_work(self, session_id: str) -> bool:
        conv = self.store.get_conversation(session_id)
        if conv is None or conv.background_work_lost_at is None:
            return False
        self.told_lost_background_work.append(session_id)
        return True

    def deps(self) -> ReplacementDeps:
        return ReplacementDeps(
            store=self.store,
            launch=self.launch,
            wait_connected=self.wait_connected,
            is_connected=lambda runner_id: (
                runner_id == _OLD and (_OLD, "superseded") not in self.stopped
            ),
            stop=self.stop,
            resume=self.resume,
            status_of=lambda _session_id: None,
            has_queued_input=lambda session_id: session_id in self.queued,
            has_terminal_attach=terminal_attach.has_write_terminal_attach,
            runner_background_work=self.runner_background_work,
            resume_lost_background_work=self.resume_lost_background_work,
        )


@pytest.fixture
def store(db_uri: str) -> SqlAlchemyConversationStore:
    return SqlAlchemyConversationStore(db_uri)


@pytest.fixture
def machinery(store: SqlAlchemyConversationStore) -> _FakeSessionMachinery:
    return _FakeSessionMachinery(store)


def _bound_session(store: SqlAlchemyConversationStore) -> str:
    return store.create_conversation(host_id=_HOST, workspace="/ws", runner_id=_OLD).id


def _request(session_id: str) -> HostReplaceRunnerFrame:
    return HostReplaceRunnerFrame(
        request_id="req_1",
        runner_id=_OLD,
        session_id=session_id,
        runner_commit="old000000",
        host_commit="new111111",
    )


def _user_message(store: SqlAlchemyConversationStore, session_id: str) -> None:
    store.append(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_seed",
                data=MessageData(role="user", content=[{"type": "input_text", "text": "hi"}]),
            )
        ],
    )


def _move_clock_past_owner_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read the replacement's clock just past the owner-activity window from now."""
    later = time.time() + stale_runner_replacement.OWNER_ACTIVE_WINDOW_S + 60
    monkeypatch.setattr(stale_runner_replacement, "time", SimpleNamespace(time=lambda: later))


async def test_idle_session_resumes_on_a_new_runner_and_the_old_one_stops(
    store: SqlAlchemyConversationStore,
    machinery: _FakeSessionMachinery,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _bound_session(store)
    _user_message(store, session_id)
    # The owner's last message is older than the window.
    _move_clock_past_owner_window(monkeypatch)
    child = store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=session_id,
        host_id=_HOST,
        workspace="/ws",
        runner_id=_OLD,
    )

    result = await replace_stale_runner(_HOST, _request(session_id), machinery.deps())

    assert (result.status, result.reason) == ("replaced", None)
    assert machinery.stopped == [(_OLD, "superseded")]
    assert machinery.resumed == [(session_id, _NEW)]
    bound = store.get_conversation(session_id)
    assert bound is not None and bound.runner_id == _NEW
    # The history stays on the session; only the runner changed.
    assert [item.type for item in store.list_items(session_id).data] == ["message"]
    moved = store.get_conversation(child.id)
    assert moved is not None and moved.runner_id == _NEW


async def test_running_turn_keeps_the_runner(
    store: SqlAlchemyConversationStore, machinery: _FakeSessionMachinery
) -> None:
    session_id = _bound_session(store)
    store.set_session_live_status(session_id, "running")

    result = await replace_stale_runner(_HOST, _request(session_id), machinery.deps())

    assert (result.status, result.reason) == ("skipped", "turn_running")
    assert machinery.launched == []


async def test_queued_turn_keeps_the_runner(
    store: SqlAlchemyConversationStore, machinery: _FakeSessionMachinery
) -> None:
    session_id = _bound_session(store)
    machinery.queued.add(session_id)

    result = await replace_stale_runner(_HOST, _request(session_id), machinery.deps())

    assert (result.status, result.reason) == ("skipped", "turn_queued")
    assert machinery.launched == []


async def test_pending_question_card_keeps_the_runner(
    store: SqlAlchemyConversationStore, machinery: _FakeSessionMachinery
) -> None:
    session_id = _bound_session(store)
    store.add_open_question(session_id, "question_1", {"type": "response.elicitation_request"})

    result = await replace_stale_runner(_HOST, _request(session_id), machinery.deps())

    assert (result.status, result.reason) == ("skipped", "pending_question")
    assert machinery.launched == []


async def test_pending_elicitation_keeps_the_runner(
    store: SqlAlchemyConversationStore, machinery: _FakeSessionMachinery
) -> None:
    session_id = _bound_session(store)
    store.set_pending_elicitation_count(session_id, 1)

    result = await replace_stale_runner(_HOST, _request(session_id), machinery.deps())

    assert (result.status, result.reason) == ("skipped", "pending_elicitation")
    assert machinery.launched == []


async def test_attached_interactive_terminal_keeps_the_runner(
    store: SqlAlchemyConversationStore, machinery: _FakeSessionMachinery
) -> None:
    session_id = _bound_session(store)

    with terminal_attach._counting_write_attach(session_id, read_only=False):
        result = await replace_stale_runner(_HOST, _request(session_id), machinery.deps())
    # A read-only viewer is not driving the session.
    with terminal_attach._counting_write_attach(session_id, read_only=True):
        assert not terminal_attach.has_write_terminal_attach(session_id)

    assert (result.status, result.reason) == ("skipped", "terminal_attached")
    assert machinery.launched == []


async def test_recent_owner_message_keeps_the_runner(
    store: SqlAlchemyConversationStore, machinery: _FakeSessionMachinery
) -> None:
    session_id = _bound_session(store)
    _user_message(store, session_id)

    result = await replace_stale_runner(_HOST, _request(session_id), machinery.deps())

    assert (result.status, result.reason) == ("skipped", "owner_active")
    assert machinery.launched == []


async def test_new_runner_that_never_connects_keeps_the_old_one(
    store: SqlAlchemyConversationStore, machinery: _FakeSessionMachinery
) -> None:
    session_id = _bound_session(store)
    machinery.new_runner_connects = False

    result = await replace_stale_runner(_HOST, _request(session_id), machinery.deps())

    assert result.status == "failed"
    assert result.reason == "the new runner did not connect; the old runner was kept"
    assert machinery.stopped == [(_NEW, "replacement_failed")]
    assert machinery.resumed == []
    bound = store.get_conversation(session_id)
    assert bound is not None and bound.runner_id == _OLD


async def test_turn_started_on_the_new_runner_during_boot_finishes_the_swap(
    store: SqlAlchemyConversationStore, machinery: _FakeSessionMachinery
) -> None:
    session_id = _bound_session(store)
    # The launch already bound the session to the new runner, so this turn runs there.
    machinery.on_new_runner_up.append(session_id)

    result = await replace_stale_runner(_HOST, _request(session_id), machinery.deps())

    assert (result.status, result.reason) == ("replaced", None)
    assert machinery.stopped == [(_OLD, "superseded")]
    # The running turn is left alone rather than resumed over.
    assert machinery.resumed == []
    bound = store.get_conversation(session_id)
    assert bound is not None and bound.runner_id == _NEW and bound.live_status == "running"


async def test_sub_agent_turn_started_during_boot_keeps_the_old_runner(
    store: SqlAlchemyConversationStore, machinery: _FakeSessionMachinery
) -> None:
    session_id = _bound_session(store)
    child = store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=session_id,
        host_id=_HOST,
        workspace="/ws",
        runner_id=_OLD,
    )
    machinery.on_new_runner_up.append(child.id)

    result = await replace_stale_runner(_HOST, _request(session_id), machinery.deps())

    assert (result.status, result.reason) == ("skipped", "turn_running")
    assert machinery.stopped == [(_NEW, "replacement_failed")]
    for kept_id in (session_id, child.id):
        kept = store.get_conversation(kept_id)
        assert kept is not None and kept.runner_id == _OLD


async def test_stored_background_work_keeps_the_runner(
    store: SqlAlchemyConversationStore, machinery: _FakeSessionMachinery
) -> None:
    session_id = _bound_session(store)
    store.set_background_work_running(session_id, True, runner_id=_OLD)

    result = await replace_stale_runner(_HOST, _request(session_id), machinery.deps())

    assert (result.status, result.reason) == ("skipped", "background_work_running")
    assert machinery.launched == []


async def test_background_work_the_old_runner_reports_keeps_it(
    store: SqlAlchemyConversationStore, machinery: _FakeSessionMachinery
) -> None:
    session_id = _bound_session(store)
    machinery.old_runner_background = True

    result = await replace_stale_runner(_HOST, _request(session_id), machinery.deps())

    assert (result.status, result.reason) == ("skipped", "background_work_running")
    assert machinery.launched == []
    assert machinery.asked_background == [(session_id, _OLD)]


async def test_background_work_reported_during_boot_rolls_back(
    store: SqlAlchemyConversationStore, machinery: _FakeSessionMachinery
) -> None:
    session_id = _bound_session(store)
    # The server's copy of the flag lags; only the old runner knows yet.
    machinery.background_starts_during_boot = True

    result = await replace_stale_runner(_HOST, _request(session_id), machinery.deps())

    assert (result.status, result.reason) == ("skipped", "background_work_running")
    assert machinery.stopped == [(_NEW, "replacement_failed")]
    # Both checks asked the old runner, not the newly bound one.
    assert machinery.asked_background == [(session_id, _OLD), (session_id, _OLD)]
    bound = store.get_conversation(session_id)
    assert bound is not None and bound.runner_id == _OLD


async def test_old_runner_that_cannot_answer_counts_as_busy(
    store: SqlAlchemyConversationStore, machinery: _FakeSessionMachinery
) -> None:
    session_id = _bound_session(store)
    machinery.old_runner_background = None

    result = await replace_stale_runner(_HOST, _request(session_id), machinery.deps())

    assert result.status == "failed"
    assert result.reason == "the old runner did not report its background work"
    assert machinery.launched == []
    assert machinery.stopped == []


async def test_runner_without_the_background_route_falls_back_to_the_stored_flag(
    store: SqlAlchemyConversationStore, machinery: _FakeSessionMachinery
) -> None:
    idle_id = _bound_session(store)
    machinery.old_runner_background = NO_ROUTE

    replaced = await replace_stale_runner(_HOST, _request(idle_id), machinery.deps())

    assert (replaced.status, replaced.reason) == ("replaced", None)
    assert machinery.stopped == [(_OLD, "superseded")]

    busy_id = _bound_session(store)
    store.set_background_work_running(busy_id, True, runner_id=_OLD)
    machinery.launched.clear()

    skipped = await replace_stale_runner(_HOST, _request(busy_id), machinery.deps())

    assert (skipped.status, skipped.reason) == ("skipped", "background_work_running")
    assert machinery.launched == []


async def test_swap_that_drops_unreported_background_work_resumes_the_session(
    store: SqlAlchemyConversationStore, machinery: _FakeSessionMachinery
) -> None:
    session_id = _bound_session(store)
    # A runner built before the background-work route can only report through
    # the stored flag, and its report lands after the swap checked it.
    machinery.old_runner_background = NO_ROUTE
    machinery.background_reported_before_rebind = True

    result = await replace_stale_runner(_HOST, _request(session_id), machinery.deps())

    assert (result.status, result.reason) == ("replaced", None)
    assert machinery.stopped == [(_OLD, "superseded")]
    swapped = store.get_conversation(session_id)
    assert swapped is not None and swapped.runner_id == _NEW
    # The rebind dropped the flag but kept the loss, and the session is told.
    assert swapped.background_work_running is None
    assert swapped.background_work_lost_at is not None
    assert machinery.told_lost_background_work == [session_id]


async def test_swap_with_no_background_work_tells_the_session_nothing(
    store: SqlAlchemyConversationStore, machinery: _FakeSessionMachinery
) -> None:
    session_id = _bound_session(store)

    result = await replace_stale_runner(_HOST, _request(session_id), machinery.deps())

    assert result.status == "replaced"
    swapped = store.get_conversation(session_id)
    assert swapped is not None and swapped.background_work_lost_at is None
    assert machinery.told_lost_background_work == []


@pytest.mark.parametrize("earlier_loss", [False, True], ids=["fresh", "earlier_loss_kept"])
async def test_rollback_to_a_connected_old_runner_clears_only_the_loss_its_launch_recorded(
    store: SqlAlchemyConversationStore, machinery: _FakeSessionMachinery, earlier_loss: bool
) -> None:
    session_id = _bound_session(store)
    if earlier_loss:
        # A loss from an earlier runner the session was not yet told about.
        store.set_background_work_running(session_id, True, runner_id=_OLD)
        store.set_background_work_running(
            session_id, False, runner_id=_OLD, unseen_since=2_000, lost_at=1_000
        )
    machinery.old_runner_background = NO_ROUTE
    machinery.background_reported_before_rebind = True
    machinery.new_runner_connects = False

    result = await replace_stale_runner(_HOST, _request(session_id), machinery.deps())

    assert result.status == "failed"
    assert result.reason == "the new runner did not connect; the old runner was kept"
    rolled_back = store.get_conversation(session_id)
    assert rolled_back is not None and rolled_back.runner_id == _OLD
    # The old runner still holds the work its launch's rebind recorded as lost.
    assert rolled_back.background_work_lost_at == (1_000 if earlier_loss else None)
    assert machinery.told_lost_background_work == []
