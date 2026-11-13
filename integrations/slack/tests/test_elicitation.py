from __future__ import annotations

import asyncio
import logging
from typing import Any

from omnigent_slack.approvals import ElicitationCoordinator, Verdict
from omnigent_slack.elicitation import (
    ElicitationController,
    ElicitationTurnState,
    PendingElicitation,
)
from omnigent_slack.events import ElicitationRequest, extract_elicitation_request
from omnigent_slack.models import SlackTurn, ThreadKey

_KEY = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")


class _RecordingOmnigent:
    """Records resolve_elicitation calls; optionally fails the decline POST."""

    def __init__(self, *, fail_resolve: bool = False) -> None:
        self.snapshot_ids: set[str] | None = set()
        self.resolve_calls: list[tuple[str, str, bool]] = []
        self._fail_resolve = fail_resolve

    async def resolve_elicitation(
        self,
        session_id: str,
        elicitation_id: str,
        *,
        accepted: bool,
        content: dict[str, Any] | None = None,
    ) -> None:
        self.resolve_calls.append((session_id, elicitation_id, accepted))
        if self._fail_resolve:
            raise RuntimeError("resolve POST failed")

    async def pending_elicitation_ids(self, session_id: str) -> set[str] | None:
        return None if self.snapshot_ids is None else set(self.snapshot_ids)


class _FakeSlack:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "ts": "200.1"}

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self.updates.append(kwargs)
        return {"ok": True}


def _controller(coordinator: ElicitationCoordinator | None = None) -> ElicitationController:
    async def _post_reply(client: Any, key: ThreadKey, text: str) -> None:
        return None

    return ElicitationController(
        coordinator or ElicitationCoordinator(),
        server_url="http://omnigent.test",
        post_reply=_post_reply,
        logger=logging.getLogger("test.elicitation"),
    )


def _turn() -> SlackTurn:
    return SlackTurn(
        key=_KEY,
        text="",
        user_id="U1",
        create_if_missing=False,
        title="",
        slack_client=_FakeSlack(),
        agent_id="",
        owner_user_id="U1",
    )


def _pending(**flags: Any) -> PendingElicitation:
    request = ElicitationRequest(elicitation_id="el_1", message="ok?", session_id="conv_1")
    return PendingElicitation(request=request, card_ts="200.1", **flags)


async def test_finish_pending_declines_when_timeout_decline_post_failed() -> None:
    # Regression: a timed-out elicitation whose decline POST ALSO failed leaves
    # the server parked (timed_out=True AND delivery_failed=True). finish_pending
    # must still re-decline to release the park — timed_out must not mask
    # delivery_failed and skip the release, or the session wedges.
    omnigent = _RecordingOmnigent()
    controller = _controller()
    turn = _turn()
    state = ElicitationTurnState()
    state.pending["el_1"] = _pending(timed_out=True, delivery_failed=True)

    await controller.finish_pending(omnigent, turn, state)  # type: ignore[arg-type]

    # The park was released with a best-effort decline.
    assert omnigent.resolve_calls == [("conv_1", "el_1", False)]


async def test_finish_pending_no_redecline_when_timeout_decline_succeeded() -> None:
    # A clean timeout (decline POST landed) must NOT be re-declined — the park is
    # already released; a second POST would be wasteful.
    omnigent = _RecordingOmnigent()
    controller = _controller()
    state = ElicitationTurnState()
    state.pending["el_1"] = _pending(timed_out=True)

    await controller.finish_pending(omnigent, _turn(), state)  # type: ignore[arg-type]

    assert omnigent.resolve_calls == []


async def test_finish_pending_declines_unanswered() -> None:
    # A card left genuinely unanswered (server still parked) is declined.
    omnigent = _RecordingOmnigent()
    controller = _controller()
    state = ElicitationTurnState()
    state.pending["el_1"] = _pending()

    await controller.finish_pending(omnigent, _turn(), state)  # type: ignore[arg-type]

    assert omnigent.resolve_calls == [("conv_1", "el_1", False)]


async def test_finish_pending_no_redecline_when_verdict_delivered() -> None:
    # A delivered verdict already released the park — no re-decline.
    omnigent = _RecordingOmnigent()
    controller = _controller()
    state = ElicitationTurnState()
    state.pending["el_1"] = _pending(verdict=Verdict(accepted=True))

    await controller.finish_pending(omnigent, _turn(), state)  # type: ignore[arg-type]

    assert omnigent.resolve_calls == []


async def test_nonblocking_question_survives_turn_end_until_answered() -> None:
    # sys_ask_user_question parks no turn: the turn ending (or the click timeout
    # passing) must not decline it, since the server takes a decline as the answer.
    omnigent = _RecordingOmnigent()
    controller = _controller(ElicitationCoordinator(timeout_seconds=0.01))
    turn = _turn()
    state = ElicitationTurnState()
    request = extract_elicitation_request(
        {
            "type": "response.elicitation_request",
            "elicitation_id": "question_1",
            "params": {
                "mode": "form",
                "policy_name": "sys_ask_user_question",
                "message": "Ship?",
                "ask_user_question": {
                    "questions": [
                        {"question": "Ship?", "options": [{"label": "Yes"}, {"label": "No"}]}
                    ]
                },
            },
        },
        "conv_1",
    )
    assert request is not None
    await controller.start(omnigent, turn, request, state)  # type: ignore[arg-type]

    await controller.finish_pending(omnigent, turn, state)  # type: ignore[arg-type]
    await asyncio.sleep(0.05)  # custom-lint: disable=sleep-then-assert -- nothing may resolve
    assert omnigent.resolve_calls == []
    assert turn.slack_client.updates == []  # type: ignore[attr-defined]

    assert await controller.handle_action(
        session_id="conv_1",
        elicitation_id="question_1",
        verdict=Verdict(accepted=True, content={"Ship?": "0"}),
    )
    resolver = state.pending["question_1"].resolver
    assert resolver is not None
    await resolver
    assert omnigent.resolve_calls == [("conv_1", "question_1", True)]
    assert turn.slack_client.updates[0]["text"] == "Request answered."  # type: ignore[attr-defined]


def _question_request() -> ElicitationRequest:
    request = extract_elicitation_request(
        {
            "type": "response.elicitation_request",
            "elicitation_id": "question_1",
            "params": {
                "mode": "form",
                "policy_name": "sys_ask_user_question",
                "message": "Ship?",
                "ask_user_question": {
                    "questions": [
                        {"question": "Ship?", "options": [{"label": "Yes"}, {"label": "No"}]}
                    ]
                },
            },
        },
        "conv_1",
    )
    assert request is not None
    return request


async def _detached_question(omnigent: _RecordingOmnigent) -> tuple[Any, SlackTurn, Any]:
    controller = _controller()
    controller._detached_poll_seconds = 0.01
    turn = _turn()
    state = ElicitationTurnState()
    await controller.start(omnigent, turn, _question_request(), state)  # type: ignore[arg-type]
    await controller.finish_pending(omnigent, turn, state)  # type: ignore[arg-type]
    return controller, turn, state.pending["question_1"]


async def test_detached_question_card_finalizes_when_answered_elsewhere() -> None:
    # Turn over, question answered in the web UI: the snapshot drops it, so the
    # card loses its buttons and its resolver ends without a Slack click.
    omnigent = _RecordingOmnigent()
    omnigent.snapshot_ids = {"question_1"}
    controller, turn, pending = await _detached_question(omnigent)
    assert turn.slack_client.updates == []  # type: ignore[attr-defined]

    omnigent.snapshot_ids = set()
    assert pending.resolver is not None
    await asyncio.wait_for(pending.resolver, timeout=2)
    await asyncio.wait_for(asyncio.gather(*controller._resolvers), timeout=2)

    updates = turn.slack_client.updates  # type: ignore[attr-defined]
    assert len(updates) == 1
    assert "answered" in updates[0]["text"].lower()
    assert omnigent.resolve_calls == []
    assert controller._resolvers == set()


async def test_detached_question_card_stays_live_while_snapshot_lists_or_fails() -> None:
    # Still listed, or an unreadable snapshot (None): the card must not finalize.
    omnigent = _RecordingOmnigent()
    omnigent.snapshot_ids = {"question_1"}
    controller, turn, pending = await _detached_question(omnigent)
    await asyncio.sleep(0.05)  # custom-lint: disable=sleep-then-assert -- nothing may resolve
    omnigent.snapshot_ids = None  # type: ignore[assignment]
    await asyncio.sleep(0.05)  # custom-lint: disable=sleep-then-assert -- nothing may resolve
    assert turn.slack_client.updates == []  # type: ignore[attr-defined]
    assert not pending.finalized
    await controller.shutdown()


async def test_detached_question_click_in_flight_is_finalized_by_resolver() -> None:
    # A Slack click settled the waiter and its POST is in flight when the snapshot
    # drops the question: the watcher defers, so the card shows the click's outcome.
    omnigent = _RecordingOmnigent()
    omnigent.snapshot_ids = {"question_1"}
    release = asyncio.Event()
    original = omnigent.resolve_elicitation

    async def slow_resolve(*args: Any, **kwargs: Any) -> None:
        await release.wait()
        await original(*args, **kwargs)

    omnigent.resolve_elicitation = slow_resolve  # type: ignore[method-assign]
    controller, turn, pending = await _detached_question(omnigent)
    assert await controller.handle_action(
        session_id="conv_1", elicitation_id="question_1", verdict=Verdict(accepted=True)
    )
    omnigent.snapshot_ids = set()
    await asyncio.sleep(0.05)  # custom-lint: disable=sleep-then-assert -- watcher must defer
    assert turn.slack_client.updates == []  # type: ignore[attr-defined]
    release.set()
    assert pending.resolver is not None
    await asyncio.wait_for(pending.resolver, timeout=2)
    updates = turn.slack_client.updates  # type: ignore[attr-defined]
    assert [u["text"] for u in updates] == ["Request answered."]
