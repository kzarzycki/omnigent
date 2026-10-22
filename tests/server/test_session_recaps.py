"""Every top-level session gets a one-line recap status at turn end."""

from __future__ import annotations

import asyncio
import time

import pytest

from omnigent.entities import (
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
    NewConversationItem,
)
from omnigent.server import session_recaps
from omnigent.server.background_session_titles import BackgroundTitleRequest
from omnigent.server.server_config import session_recaps_enabled
from omnigent.server.session_recaps import RECAP_INSTRUCTIONS, SessionRecapCoordinator
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

pytestmark = pytest.mark.asyncio


def _turn(store: SqlAlchemyConversationStore, conv_id: str, ask: str, reply: str) -> None:
    def message(role: str, text: str) -> NewConversationItem:
        kind = "input_text" if role == "user" else "output_text"
        return NewConversationItem(
            type="message",
            response_id="r1",
            data=MessageData(role=role, agent="Claude", content=[{"type": kind, "text": text}]),
        )

    store.append(
        conv_id,
        [
            message("user", ask),
            NewConversationItem(
                type="function_call",
                response_id="r1",
                data=FunctionCallData(agent="Claude", name="Bash", arguments="{}", call_id="c1"),
            ),
            message("assistant", reply),
        ],
    )


class _Generator:
    def __init__(self, output: str = "Fixed the login bug; tests pass", on_call=None) -> None:
        self.output = output
        self.on_call = on_call
        self.requests: list[BackgroundTitleRequest] = []
        self.called_at: list[float] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def __call__(self, request: BackgroundTitleRequest) -> str:
        self.requests.append(request)
        self.called_at.append(time.monotonic())
        if self.on_call is not None:
            self.on_call()
        self.entered.set()
        await self.release.wait()
        return self.output


def _coordinator(store, generator, **kwargs) -> SessionRecapCoordinator:
    kwargs.setdefault("settle_seconds", 0)
    return SessionRecapCoordinator(store, generator, **kwargs)


async def test_turn_end_writes_a_recap_from_the_latest_turn(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(kind="default", title="Login bug")
    _turn(
        store,
        conv.id,
        "fix the login bug",
        "Fixed it and the tests pass." + "." * 3000 + "Next: deploy.",
    )
    generator = _Generator("x" * 200)
    coordinator = _coordinator(store, generator)

    coordinator.turn_ended(conv.id, from_runner=False)
    await coordinator.wait_for_idle()

    [request] = generator.requests
    assert request.additional_instructions == RECAP_INSTRUCTIONS
    assert "fix the login bug" in request.prompt
    assert "Bash" in request.prompt
    assert "Fixed it and the tests pass." in request.prompt
    assert request.prompt.endswith("Next: deploy.")
    assert len(request.prompt) < 2400
    labels = store.get_conversation(conv.id).labels
    assert len(labels["status.line"]) == 120
    assert labels["status.by"] == "recap"
    assert abs(int(labels["status.at"]) - time.time()) < 5


async def test_a_line_another_writer_set_during_the_turn_wins(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(kind="default")
    _turn(store, conv.id, "go", "done")
    store.set_labels(conv.id, {"status.line": "waits on CI", "status.at": str(int(time.time()))})
    generator = _Generator()
    coordinator = _coordinator(store, generator)

    coordinator.turn_ended(conv.id, from_runner=False)
    await coordinator.wait_for_idle()

    assert generator.requests == []
    assert store.get_conversation(conv.id).labels["status.line"] == "waits on CI"


async def test_a_line_another_writer_set_before_the_turn_is_replaced(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(kind="default")
    store.set_labels(conv.id, {"status.line": "old", "status.at": "1000"})
    _turn(store, conv.id, "go", "done")
    coordinator = _coordinator(store, _Generator("Done"))

    coordinator.turn_ended(conv.id, from_runner=False)
    await coordinator.wait_for_idle()

    assert store.get_conversation(conv.id).labels["status.line"] == "Done"


async def test_turn_ends_within_the_interval_share_one_call(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(kind="default")
    _turn(store, conv.id, "go", "done")
    generator = _Generator()
    coordinator = _coordinator(store, generator, settle_seconds=0.05, interval_seconds=0.3)

    for _ in range(3):
        coordinator.turn_ended(conv.id, from_runner=False)
    await coordinator.wait_for_idle()
    assert len(generator.requests) == 1

    # The next turn end inside the interval waits for it, then recaps once more.
    _turn(store, conv.id, "again", "done again")
    coordinator.turn_ended(conv.id, from_runner=False)
    await coordinator.wait_for_idle()
    assert len(generator.requests) == 2
    assert "done again" in generator.requests[-1].prompt
    assert generator.called_at[1] - generator.called_at[0] >= 0.25

    # A repeated idle edge with nothing new in the transcript makes no call.
    coordinator.turn_ended(conv.id, from_runner=False)
    await coordinator.wait_for_idle()
    assert len(generator.requests) == 2


async def test_turn_ends_before_the_recap_starts_share_it(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(kind="default")
    _turn(store, conv.id, "first", "first done")
    generator = _Generator()
    coordinator = _coordinator(store, generator, settle_seconds=0.2)

    coordinator.turn_ended(conv.id, from_runner=False)
    _turn(store, conv.id, "second", "second done")
    coordinator.turn_ended(conv.id, from_runner=False)
    await coordinator.wait_for_idle()

    [request] = generator.requests
    assert "second done" in request.prompt


async def test_a_turn_ending_during_a_recap_call_gets_its_own(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(kind="default")
    _turn(store, conv.id, "first", "first done")
    generator = _Generator()
    generator.release.clear()
    coordinator = _coordinator(store, generator, interval_seconds=0.1)

    coordinator.turn_ended(conv.id, from_runner=False)
    await generator.entered.wait()
    _turn(store, conv.id, "second", "second done")
    coordinator.turn_ended(conv.id, from_runner=False)
    generator.release.set()
    await coordinator.wait_for_idle()

    assert len(generator.requests) == 2
    assert "second done" in generator.requests[-1].prompt


async def test_a_line_written_during_the_recap_call_wins(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(kind="default")
    _turn(store, conv.id, "go", "done")

    def supervisor_writes() -> None:
        store.set_labels(
            conv.id,
            {
                "status.line": "turn ended: done",
                "status.at": str(int(time.time())),
                "status.by": "",
            },
        )

    coordinator = _coordinator(store, _Generator(on_call=supervisor_writes))
    coordinator.turn_ended(conv.id, from_runner=False)
    await coordinator.wait_for_idle()

    assert store.get_conversation(conv.id).labels["status.line"] == "turn ended: done"


async def test_a_turn_longer_than_the_excerpt_leaves_another_writers_line(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_recaps, "_EXCERPT_ITEMS", 10)
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(kind="default")
    _turn(store, conv.id, "migrate the schema", "starting")
    store.set_labels(conv.id, {"status.line": "migrating", "status.at": str(int(time.time()))})
    # The window then sees only items newer than that line.
    time.sleep(1.1)
    calls = []
    for n in range(35):
        calls += [
            NewConversationItem(
                type="function_call",
                response_id="r1",
                data=FunctionCallData(
                    agent="Claude", name="Bash", arguments="{}", call_id=f"c{n}"
                ),
            ),
            NewConversationItem(
                type="function_call_output",
                response_id="r1",
                data=FunctionCallOutputData(call_id=f"c{n}", output="ok"),
            ),
        ]
    store.append(conv.id, calls)
    reply = MessageData(
        role="assistant", agent="Claude", content=[{"type": "output_text", "text": "Migrated."}]
    )
    store.append(conv.id, [NewConversationItem(type="message", response_id="r1", data=reply)])
    generator = _Generator()
    coordinator = _coordinator(store, generator)

    coordinator.turn_ended(conv.id, from_runner=False)
    await coordinator.wait_for_idle()

    assert store.get_conversation(conv.id).labels["status.line"] == "migrating"


async def test_a_failed_turn_says_so_even_from_a_native_pane(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(kind="default", harness_override="claude-native")
    _turn(store, conv.id, "deploy it", "Running the deploy now.")
    generator = _Generator()
    coordinator = _coordinator(store, generator)

    # A crashed pane's failure is relayed by the runner; no forwarder edge follows.
    coordinator.turn_ended(conv.id, from_runner=True, failed=True)
    await coordinator.wait_for_idle()

    [request] = generator.requests
    assert request.prompt.endswith("Outcome: the turn failed before it finished.")


async def test_shutdown_drops_a_turn_end_queued_behind_a_call(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(kind="default")
    _turn(store, conv.id, "first", "first done")
    generator = _Generator()
    generator.release.clear()
    coordinator = _coordinator(store, generator, interval_seconds=0)

    coordinator.turn_ended(conv.id, from_runner=False)
    await generator.entered.wait()
    _turn(store, conv.id, "second", "second done")
    coordinator.turn_ended(conv.id, from_runner=False)
    await coordinator.shutdown()
    await asyncio.sleep(0.2)

    assert coordinator._tasks == {}
    assert len(generator.requests) == 1
    assert "status.line" not in store.get_conversation(conv.id).labels


async def test_skipped_sessions_make_no_call(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation(kind="default")
    child = store.create_conversation(kind="default", parent_conversation_id=parent.id)
    archived = store.create_conversation(kind="default")
    store.update_conversation(archived.id, archived=True)
    native = store.create_conversation(kind="default", harness_override="claude-native")
    # No title generator, so the runner could not answer.
    untitled = store.create_conversation(kind="default", harness_override="pi-native")
    for conv_id in (child.id, archived.id, native.id, untitled.id):
        _turn(store, conv_id, "go", "done")
    generator = _Generator()
    coordinator = _coordinator(store, generator)

    coordinator.turn_ended(child.id, from_runner=False)
    coordinator.turn_ended(archived.id, from_runner=False)
    coordinator.turn_ended(untitled.id, from_runner=False)
    # A native pane going quiet is not its turn end; the forwarder's edge is.
    coordinator.turn_ended(native.id, from_runner=True)
    await coordinator.wait_for_idle()
    assert generator.requests == []

    coordinator.turn_ended(native.id, from_runner=True)
    coordinator.turn_ended(native.id, from_runner=False)
    await coordinator.wait_for_idle()
    assert len(generator.requests) == 1


async def test_publish_status_hands_terminal_edges_to_the_recap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server.routes._sessions.helpers import _publish_status

    seen: list[tuple[str, bool]] = []

    class _Recorder:
        def turn_ended(self, session_id: str, *, from_runner: bool, failed: bool) -> None:
            seen.append((session_id, from_runner, failed))

    monkeypatch.setattr(session_recaps, "_coordinator", _Recorder())
    _publish_status("conv_recap_a", "running", persist_live_status=False)
    _publish_status("conv_recap_a", "idle", persist_live_status=False, from_runner=True)
    _publish_status("conv_recap_b", "failed", persist_live_status=False)
    await asyncio.sleep(0)

    assert seen == [("conv_recap_a", True, False), ("conv_recap_b", False, True)]


@pytest.mark.parametrize(
    ("config", "expected"),
    [({}, True), ({"session_recaps": True}, True), ({"session_recaps": False}, False)],
)
async def test_session_recaps_config_switch(config: dict, expected: bool) -> None:
    assert session_recaps_enabled(config) is expected
