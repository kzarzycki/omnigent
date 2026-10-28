"""Bounded retry of status posts refused with ``subagent_delivery_not_confirmed``.

The shared :class:`SubagentStatusRetry` is checked directly, then each native
forwarder's real status path runs against a server that refuses its status
posts, so every forwarder is shown to stop at the shared budget, never to post
``failed`` in place of the refused edge, and to deliver once the refusal clears.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.native import _native_post_delivery as delivery
from omnigent.native._native_post_delivery import (
    SUBAGENT_DELIVERY_NOT_CONFIRMED_MAX_ATTEMPTS,
    SubagentStatusRetry,
    is_subagent_delivery_not_confirmed,
    post_session_event_with_retry,
)

_BUDGET = SUBAGENT_DELIVERY_NOT_CONFIRMED_MAX_ATTEMPTS
_NOT_CONFIRMED_BODIES = [
    {"error": "subagent_delivery_not_confirmed", "reason": "missing_parent_inbox"},
    {"error": {"code": "subagent_delivery_not_confirmed", "message": "refused"}},
]


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the production budget but drop its seconds-long delays."""
    monkeypatch.setattr(delivery, "_SUBAGENT_DELIVERY_RETRY_BASE_DELAY_S", 0.0)
    monkeypatch.setattr(delivery, "_SUBAGENT_DELIVERY_RETRY_MAX_DELAY_S", 0.0)


def _not_confirmed_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, json=_NOT_CONFIRMED_BODIES[0], request=request)


class _RefusingStatusServer:
    """Omnigent stand-in that refuses status posts as not confirmed.

    :param refusals: How many posts of one status edge are refused before it is
        accepted; ``None`` refuses every one.
    """

    def __init__(self, refusals: int | None) -> None:
        self._refusals = refusals
        self.attempts: Counter[tuple[str, str]] = Counter()
        self.delivered: Counter[tuple[str, str]] = Counter()
        self._last_change = time.monotonic()

    def _record(self, session_id: str, status: str) -> bool:
        """Count one attempt; return whether it is accepted."""
        key = (session_id, status)
        self.attempts[key] += 1
        self._last_change = time.monotonic()
        accepted = self._refusals is not None and self.attempts[key] > self._refusals
        if accepted:
            self.delivered[key] += 1
        return accepted

    def statuses(self) -> set[str]:
        return {status for _session, status in self.attempts}

    def settled(self, session_id: str, status: str) -> bool:
        """Whether the edge was posted and nothing has been posted for a while."""
        return (
            self.attempts[(session_id, status)] > 0 and time.monotonic() - self._last_change > 0.3
        )

    async def post_status(self, _client: object, **kwargs: Any) -> None:
        """Stand in for a forwarder's own status-post helper."""
        terminal = kwargs.get("terminal")
        status = kwargs["status"] if terminal is None else terminal.status
        if not self._record(str(kwargs["session_id"]), str(status)):
            request = httpx.Request("POST", "http://test/v1/sessions/x/events")
            response = _not_confirmed_response(request)
            raise httpx.HTTPStatusError("refused", request=request, response=response)

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one HTTP request the way the server would."""
        body = json.loads(request.content) if request.content else {}
        if body.get("type") == "external_session_status":
            session_id = request.url.path.split("/")[3]
            if not self._record(session_id, body["data"]["status"]):
                return _not_confirmed_response(request)
            return httpx.Response(204, request=request)
        if body.get("type") == "external_devin_subagent_start":
            child = f"conv_child_{body['data']['agent_id']}"
            return httpx.Response(200, json={"child_session_id": child}, request=request)
        if body.get("type") == "external_acp_subagent_start":
            return httpx.Response(200, json={"child_session_id": "conv_acp"}, request=request)
        return httpx.Response(200, json={}, request=request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(self.handle)
        )


Done = Callable[[], bool]
Driver = Callable[[_RefusingStatusServer, Path, pytest.MonkeyPatch, Done], Awaitable[str]]


async def _wait(done: Done, *, timeout_s: float = 10.0) -> None:
    """Yield to the event loop until *done* holds."""
    deadline = time.monotonic() + timeout_s
    while not done():
        if time.monotonic() > deadline:
            return
        await asyncio.sleep(0.01)


async def _run_until(coro: Awaitable[object], done: Done) -> None:
    """Run a forwarder loop as a task until *done*, then cancel it."""
    task = asyncio.ensure_future(coro)
    try:
        await _wait(lambda: done() or task.done())
        if task.done():
            task.result()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _serve(server: _RefusingStatusServer, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hand the fake server to forwarders that open their own client."""

    @contextlib.asynccontextmanager
    async def _open(*_args: object, **_kwargs: object) -> AsyncIterator[httpx.AsyncClient]:
        async with server.client() as client:
            yield client

    monkeypatch.setattr("omnigent.cli_auth.open_server_client", _open)


# --- one driver per native forwarder; each returns the session it reports on ---


async def _drive_devin_turn(server, tmp_path, monkeypatch, done) -> str:
    from omnigent.harnesses.devin_native import forwarder as fwd
    from tests.test_devin_native_forwarder import _STOP, _USER_PROMPT

    state = fwd._ForwardState()
    turn = fwd._TurnState()
    async with server.client() as client:
        for payload in (_USER_PROMPT, _STOP):
            await fwd._handle_event(
                client,
                session_id="conv_devin",
                bridge_dir=tmp_path,
                agent_name="devin",
                payload=payload,
                state=state,
                turn=turn,
            )
        await _wait(done)
        await state.status_retry.aclose()
    return "conv_devin"


async def _drive_devin_subagent_mirror(server, tmp_path, monkeypatch, done) -> str:
    from omnigent.harnesses.devin_native import forwarder as fwd
    from tests.test_devin_native_forwarder import (
        _ONE_TASK,
        _SUBAGENT_FIXTURE,
        _run_subagent_post,
    )

    monkeypatch.setattr(fwd, "devin_sessions_db_path", lambda _env: tmp_path / "sessions.db")
    monkeypatch.setattr(fwd, "load_message_nodes", lambda _db, _sid: _SUBAGENT_FIXTURE)
    state = fwd._ForwardState(devin_session_id="possible-umbra", session_id_persisted=True)
    turn = fwd._TurnState()
    async with server.client() as client:
        for payload in (
            _run_subagent_post("690d786b", _ONE_TASK, "Write one.txt"),
            {"hook_event_name": "Stop", "session_id": "possible-umbra", "prompt_id": "p1"},
        ):
            await fwd._handle_event(
                client,
                session_id="conv_devin",
                bridge_dir=tmp_path,
                agent_name="devin",
                payload=payload,
                state=state,
                turn=turn,
            )
        await _wait(done)
        await state.status_retry.aclose()
    assert state.subagents["690d786b"]["mirrored"] is True
    return "conv_child_690d786b"


async def _drive_goose(server, tmp_path, monkeypatch, done) -> str:
    from omnigent.harnesses.goose_native import forwarder as fwd
    from tests.test_goose_native_forwarder import _insert, _prose, _seed_empty_db

    db = tmp_path / "sessions.db"
    _seed_empty_db(db)
    _insert(db, "user", _prose("hi"))
    _insert(db, "assistant", _prose("hello"))
    _serve(server, monkeypatch)
    await _run_until(
        fwd.forward_goose_store_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_goose",
            bridge_dir=tmp_path / "bridge",
            agent_name="goose-native",
            goose_session_name="omni-1",
            db_path=db,
            poll_interval_s=0.001,
        ),
        done,
    )
    return "conv_goose"


async def _drive_cursor(server, tmp_path, monkeypatch, done) -> str:
    from omnigent.harnesses.cursor_native import forwarder as fwd
    from omnigent.harnesses.cursor_native import status

    status.record_turn_end(tmp_path)
    monkeypatch.setattr(fwd, "_discover_store", lambda *a, **k: None)
    monkeypatch.setattr(fwd, "_post_external_session_status", server.post_status)
    await _run_until(
        fwd.forward_cursor_store_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_cursor",
            bridge_dir=tmp_path,
            agent_name="cursor-native-ui",
            workspace="/ws",
            launch_epoch_ms=1_000,
            poll_interval_s=0.001,
        ),
        done,
    )
    return "conv_cursor"


async def _drive_hermes(server, tmp_path, monkeypatch, done) -> str:
    from omnigent.harnesses.hermes_native import forwarder as fwd
    from tests.test_hermes_native_forwarder import _seed_db

    db = tmp_path / "state.db"
    _seed_db(db, cwd=str(tmp_path), started_at=1000.0)

    async def _accept_item(_client: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(fwd, "_post_conversation_item", _accept_item)
    monkeypatch.setattr(fwd, "_post_external_session_status", server.post_status)
    _serve(server, monkeypatch)
    await _run_until(
        fwd.forward_hermes_store_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_hermes",
            bridge_dir=tmp_path / "bridge",
            agent_name="hermes-native-ui",
            workspace=str(tmp_path),
            launch_epoch_s=1000.0,
            db_path=db,
            poll_interval_s=0.001,
        ),
        done,
    )
    return "conv_hermes"


async def _drive_kimi(server, tmp_path, monkeypatch, done) -> str:
    from omnigent.harnesses.kimi_native import forwarder as fwd
    from tests.test_kimi_native_forwarder import (
        _assistant_row,
        _drive_loop_until,
        _prompt_row,
        _turn_ended_row,
    )

    async def _accept_item(_client: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(fwd, "_post_conversation_item", _accept_item)
    monkeypatch.setattr(fwd, "_post_reasoning_item", _accept_item)
    monkeypatch.setattr(fwd, "_post_external_session_status", server.post_status)
    rows = [_prompt_row(), _assistant_row("u1", "answer"), _turn_ended_row("completed")]
    with contextlib.suppress(AssertionError):  # the loop helper's own 3 s cap
        await _drive_loop_until(tmp_path, rows, done)
    return "conv_k"


async def _drive_qwen(server, tmp_path, monkeypatch, done) -> str:
    from omnigent.harnesses.qwen_native import forwarder as fwd
    from omnigent.harnesses.qwen_native.bridge import events_file_path
    from tests.test_qwen_native_forwarder import _asst_ev, _ev_bytes

    bridge = tmp_path / "bridge"
    bridge.mkdir()
    events_file_path(bridge).write_bytes(
        _ev_bytes(_asst_ev("a1", [{"type": "text", "text": "answer"}]))
        + _ev_bytes({"type": "result", "subtype": "success", "is_error": False, "uuid": "r1"})
    )

    async def _accept_item(_client: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(fwd, "_post_conversation_item", _accept_item)
    monkeypatch.setattr(fwd, "_post_external_session_status", server.post_status)
    _serve(server, monkeypatch)
    await _run_until(
        fwd.forward_qwen_events_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_qwen",
            bridge_dir=bridge,
            agent_name="qwen-native-ui",
            poll_interval_s=0.001,
        ),
        done,
    )
    return "conv_qwen"


async def _drive_opencode(server, tmp_path, monkeypatch, done) -> str:
    from omnigent.harnesses.opencode_native import forwarder as fwd
    from omnigent.harnesses.opencode_native.client import OpenCodeEvent
    from tests.test_opencode_native_forwarder import _FakeOpenCodeClient

    class _IdleOnce(_FakeOpenCodeClient):
        async def events(self) -> AsyncIterator[OpenCodeEvent]:
            yield OpenCodeEvent(
                id=None, type="session.idle", properties={"sessionID": "ses_1"}, raw={}
            )
            await _wait(done)

    async with server.client() as client:
        forwarder = fwd.OpenCodeNativeForwarder(
            session_id="conv_opencode",
            opencode_session_id="ses_1",
            opencode_client=_IdleOnce(),  # type: ignore[arg-type]
            server_client=client,
        )
        await forwarder.run(max_reconnects=0)
    return "conv_opencode"


async def _drive_codex(server, tmp_path, monkeypatch, done) -> str:
    from omnigent.harnesses.codex_native import forwarder as fwd

    class _AppServer:
        async def connect(self) -> None:
            return None

        async def request(self, _method: str, _params: dict[str, Any]) -> dict[str, Any]:
            return {"result": {"thread": {"id": "thread_1"}}}

        async def respond(self, *_args: object) -> None:
            return None

        async def close(self) -> None:
            return None

        async def iter_events(self) -> AsyncIterator[dict[str, Any]]:
            yield {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread_1",
                    "turn": {"id": "turn_1", "status": "completed"},
                },
            }
            await _wait(done)

    await fwd.supervise_forwarder(
        base_url="http://test",
        headers={},
        session_id="conv_codex",
        bridge_dir=tmp_path,
        app_server_url=str(tmp_path / "app-server.sock"),
        thread_id="thread_1",
        client=_AppServer(),  # type: ignore[arg-type]
        ap_transport=httpx.MockTransport(server.handle),
    )
    return "conv_codex"


async def _drive_antigravity(server, tmp_path, monkeypatch, done) -> str:
    from omnigent.harnesses.antigravity_native import reader

    async with server.client() as client, SubagentStatusRetry() as status_retry:
        token = reader._status_retry.set(status_retry)
        try:
            await reader._post_event(client, "conv_agy", reader._status_event("idle"))
            await _wait(done)
        finally:
            reader._status_retry.reset(token)
    return "conv_agy"


async def _drive_acp(server, tmp_path, monkeypatch, done) -> str:
    from omnigent.runner import acp_subagent_sessions

    child_id: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    child_id.set_result("conv_acp")
    async with server.client() as client, SubagentStatusRetry() as status_retry:
        await acp_subagent_sessions._complete_acp_subagent_child(
            client,
            child_key="sub_1",
            ok=True,
            summary="done",
            child_id_future=child_id,
            status_retry=status_retry,
        )
        await _wait(done)
    return "conv_acp"


_DRIVERS: dict[str, tuple[Driver, str]] = {
    "devin-native": (_drive_devin_turn, "conv_devin"),
    "devin-native-subagent-mirror": (_drive_devin_subagent_mirror, "conv_child_690d786b"),
    "goose-native": (_drive_goose, "conv_goose"),
    "cursor-native": (_drive_cursor, "conv_cursor"),
    "hermes-native": (_drive_hermes, "conv_hermes"),
    "kimi-native": (_drive_kimi, "conv_k"),
    "qwen-native": (_drive_qwen, "conv_qwen"),
    "opencode-native": (_drive_opencode, "conv_opencode"),
    "codex-native": (_drive_codex, "conv_codex"),
    "antigravity-native": (_drive_antigravity, "conv_agy"),
    "acp-subagent": (_drive_acp, "conv_acp"),
}


@pytest.mark.parametrize("forwarder", list(_DRIVERS))
async def test_a_refused_idle_edge_stops_at_the_budget_without_posting_failed(
    forwarder: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, session_id = _DRIVERS[forwarder]
    server = _RefusingStatusServer(refusals=None)

    await driver(server, tmp_path, monkeypatch, lambda: server.settled(session_id, "idle"))

    assert server.attempts[(session_id, "idle")] == _BUDGET
    assert "failed" not in server.statuses()


@pytest.mark.parametrize("forwarder", list(_DRIVERS))
async def test_an_idle_edge_refused_twice_is_delivered_once(
    forwarder: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, session_id = _DRIVERS[forwarder]
    server = _RefusingStatusServer(refusals=2)

    await driver(server, tmp_path, monkeypatch, lambda: server.settled(session_id, "idle"))

    assert server.attempts[(session_id, "idle")] == 3
    assert server.delivered[(session_id, "idle")] == 1
    assert "failed" not in server.statuses()


# --- the shared tracker and classifier ---


def _refusal(body: object = _NOT_CONFIRMED_BODIES[0], status: int = 503) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://test/v1/sessions/c/events")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError("refused", request=request, response=response)


@pytest.mark.parametrize("body", _NOT_CONFIRMED_BODIES, ids=["runner", "server-envelope"])
def test_the_classifier_reads_both_refusal_envelopes(body: object) -> None:
    assert is_subagent_delivery_not_confirmed(_refusal(body)) is True


@pytest.mark.parametrize(
    "exc",
    [
        _refusal({"error": "something_else"}),
        _refusal(_NOT_CONFIRMED_BODIES[0], status=500),
        httpx.ConnectError("down"),
    ],
    ids=["other-503", "500", "transport"],
)
def test_the_classifier_rejects_every_other_failure(exc: httpx.HTTPError) -> None:
    assert is_subagent_delivery_not_confirmed(exc) is False


class _ScriptedPost:
    """A status post that fails with the queued errors, then succeeds."""

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)


async def _drain(retry: SubagentStatusRetry, key: str) -> None:
    await _wait(lambda: not retry.is_pending(key), timeout_s=5.0)


async def test_the_tracker_returns_at_once_and_retries_in_the_background() -> None:
    post = _ScriptedPost(_refusal(), _refusal())
    async with SubagentStatusRetry() as retry:
        assert await retry.send("conv", post) is False
        assert post.calls == 1  # the caller's loop was not held for the retries
        await _drain(retry, "conv")
    assert post.calls == 3


async def test_the_tracker_gives_up_after_its_budget() -> None:
    post = _ScriptedPost(*[_refusal() for _ in range(_BUDGET + 5)])
    async with SubagentStatusRetry() as retry:
        await retry.send("conv", post)
        await _drain(retry, "conv")
        await asyncio.sleep(0.05)
    assert post.calls == _BUDGET


async def test_other_failures_reach_the_caller_unretried() -> None:
    post = _ScriptedPost(_refusal({"error": "other"}))
    async with SubagentStatusRetry() as retry:
        with pytest.raises(httpx.HTTPStatusError):
            await retry.send("conv", post)
        assert retry.is_pending("conv") is False
    assert post.calls == 1


async def test_a_different_refusal_during_the_retry_stops_it() -> None:
    post = _ScriptedPost(_refusal(), _refusal({"error": "other"}, status=404), _refusal())
    async with SubagentStatusRetry() as retry:
        await retry.send("conv", post)
        await _drain(retry, "conv")
        await asyncio.sleep(0.05)
    assert post.calls == 2


async def test_a_newer_status_for_the_session_replaces_the_pending_one() -> None:
    stale = _ScriptedPost(*[_refusal() for _ in range(_BUDGET)])
    newer = _ScriptedPost()
    async with SubagentStatusRetry(base_delay_s=60.0) as retry:
        await retry.send("conv", stale)
        assert await retry.send("conv", newer) is True
        assert retry.is_pending("conv") is False
    assert (stale.calls, newer.calls) == (1, 1)


async def test_closing_the_tracker_cancels_pending_retries() -> None:
    post = _ScriptedPost(*[_refusal() for _ in range(_BUDGET)])
    retry = SubagentStatusRetry(base_delay_s=60.0)
    await retry.send("conv", post)
    await retry.aclose()
    assert retry.is_pending("conv") is False
    assert post.calls == 1


async def test_the_shared_retry_loop_leaves_a_refusal_to_the_tracker() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _not_confirmed_response(request)

    async def no_sleep(_seconds: float) -> None:
        return None

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handle)
    ) as client:
        response = await post_session_event_with_retry(
            client=client,
            url="/v1/sessions/c/events",
            payload={"type": "external_session_status", "data": {"status": "idle"}},
            event_type="external_session_status",
            max_attempts=3,
            retry_status_codes=frozenset({503}),
            sleep=no_sleep,
            retry_delay=lambda _attempt: 0.0,
            logger_name=__name__,
        )
    assert response is not None and response.status_code == 503
    assert calls == 1
