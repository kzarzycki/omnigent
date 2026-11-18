"""The server relaunches a host's stale runner only while its session is idle."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.host.frames import (
    REPLACE_STALE_RUNNER_BUSY,
    REPLACE_STALE_RUNNER_FAILED,
    REPLACE_STALE_RUNNER_INELIGIBLE,
    REPLACE_STALE_RUNNER_REPLACED,
)
from omnigent.server import stale_runner_replacement
from omnigent.server.routes._sessions.helpers import _HostLaunchAttempt

HOST_ID = "a" * 32
SESSION_ID = "conv_stale"
OLD_RUNNER = "runner_old"
NEW_RUNNER = "runner_new"
NOW = 1_800_000_000.0


def _message(age_s: float, text: str, role: str = "user") -> SimpleNamespace:
    return SimpleNamespace(
        created_at=int(NOW - age_s),
        data=SimpleNamespace(role=role, content=[{"type": "input_text", "text": text}]),
    )


@dataclass
class _FakeStore:
    """The session row and its newest-first message items."""

    conv: SimpleNamespace
    messages: list[SimpleNamespace] = field(default_factory=list)
    rebinds: list[tuple[str, str, str | None]] = field(default_factory=list)

    def get_conversation(self, session_id: str) -> SimpleNamespace | None:
        return self.conv if session_id == self.conv.id else None

    def list_items(self, session_id: str, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(data=list(self.messages))

    def replace_runner_id(
        self, session_id: str, runner_id: str, *, expected_runner_id: str | None = None
    ) -> None:
        self.rebinds.append((session_id, runner_id, expected_runner_id))


@dataclass
class _Launches:
    """Records relaunches and answers each with *attempt*."""

    attempt: _HostLaunchAttempt
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(
        self, conv: Any, store: Any, registry: Any, conn: Any, **kwargs: Any
    ) -> Any:
        self.calls.append(kwargs)
        return self.attempt


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: str = "idle",
    queued: bool = False,
    elicitations: int = 0,
    open_questions: list[object] | None = None,
    messages: list[SimpleNamespace] | None = None,
    attempt: _HostLaunchAttempt | None = None,
) -> tuple[_FakeStore, _Launches]:
    conv = SimpleNamespace(
        id=SESSION_ID,
        host_id=HOST_ID,
        runner_id=OLD_RUNNER,
        live_status=status,
        pending_elicitation_count=0,
        open_questions=open_questions or [],
    )
    store = _FakeStore(conv, messages or [])
    launches = _Launches(attempt or _HostLaunchAttempt(runner_id=NEW_RUNNER, acknowledged=True))

    async def _owns(conv: Any, store: Any) -> bool:
        return True

    monkeypatch.setattr("omnigent.server.routes._sessions.helpers._owns_host_runner", _owns)
    monkeypatch.setattr("omnigent.server.routes.sessions._launch_runner_on_host", launches)
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.common._session_status_cache", {SESSION_ID: status}
    )
    monkeypatch.setattr("omnigent.runtime.pending_inputs.has_pending", lambda sid: queued)
    monkeypatch.setattr(
        "omnigent.runtime.pending_elicitations.count_for", lambda sid: elicitations
    )
    return store, launches


async def _replace(store: _FakeStore) -> tuple[str, str | None]:
    return await stale_runner_replacement.replace_stale_runner(
        HOST_ID,
        OLD_RUNNER,
        SESSION_ID,
        conversation_store=store,  # type: ignore[arg-type]
        host_registry=SimpleNamespace(get=lambda host_id: object()),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [_message(31 * 60, "an old request")],
        [_message(60, "[Message from session conv_peer]\nping"), _message(40 * 60, "old")],
        [_message(60, "an agent reply", role="assistant")],
    ],
    ids=["no-messages", "owner-message-31-min-ago", "recent-peer-message", "recent-reply"],
)
async def test_an_idle_session_is_relaunched_keeping_the_old_runner_until_the_new_one_is_up(
    monkeypatch: pytest.MonkeyPatch, messages: list[SimpleNamespace]
) -> None:
    store, launches = _setup(monkeypatch, messages=messages)

    assert await _replace(store) == (REPLACE_STALE_RUNNER_REPLACED, None)
    assert launches.calls == [{"stop_superseded_first": False}]


@pytest.mark.parametrize(
    ("condition", "reason"),
    [
        ({"status": "running"}, "turn running"),
        ({"status": "waiting"}, "turn running"),
        ({"queued": True}, "turn queued"),
        ({"open_questions": [{"id": "q1"}]}, "question or elicitation pending"),
        ({"elicitations": 1}, "question or elicitation pending"),
        ({"messages": [_message(5 * 60, "keep going")]}, "owner messaged recently"),
    ],
    ids=[
        "turn-running",
        "turn-waiting",
        "turn-queued",
        "question-card",
        "elicitation",
        "owner-message",
    ],
)
async def test_a_busy_or_owner_driven_session_is_not_touched(
    monkeypatch: pytest.MonkeyPatch, condition: dict[str, Any], reason: str
) -> None:
    store, launches = _setup(monkeypatch, **condition)

    assert await _replace(store) == (REPLACE_STALE_RUNNER_BUSY, reason)
    assert launches.calls == []


async def test_a_refused_launch_rebinds_the_session_to_its_old_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused = _HostLaunchAttempt(runner_id=NEW_RUNNER, error="failed to spawn runner")
    store, _ = _setup(monkeypatch, attempt=refused)

    assert await _replace(store) == (REPLACE_STALE_RUNNER_FAILED, "failed to spawn runner")
    assert store.rebinds == [(SESSION_ID, OLD_RUNNER, NEW_RUNNER)]


async def test_a_runner_the_session_no_longer_uses_is_ineligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, launches = _setup(monkeypatch)
    store.conv.runner_id = "runner_after_clear"

    status, _ = await _replace(store)

    assert status == REPLACE_STALE_RUNNER_INELIGIBLE
    assert launches.calls == []
