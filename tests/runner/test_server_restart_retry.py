"""The runner's server client waits out a server restart, within a bounded window."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import httpx
import pytest

from omnigent.runner.server_restart_retry import FAIL_FAST_EXTENSION, ServerRestartRetryClient

_Outcome = int | type[httpx.TransportError]


class _FakeTime:
    """A clock that advances only when the client sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _client(
    outcomes: Iterator[_Outcome], fake_time: _FakeTime, *, retry_window_s: float = 120.0
) -> tuple[ServerRestartRetryClient, list[str]]:
    """Build a client whose server answers each request with the next outcome."""
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.method)
        outcome = next(outcomes)
        if isinstance(outcome, int):
            return httpx.Response(outcome, json={})
        raise outcome("server down", request=request)

    client = ServerRestartRetryClient(
        base_url="http://server",
        transport=httpx.MockTransport(handler),
        retry_window_s=retry_window_s,
        sleep=fake_time.sleep,
        clock=fake_time.clock,
    )
    return client, sent


@pytest.mark.asyncio
async def test_get_waits_through_refused_connects_and_gateway_errors() -> None:
    fake_time = _FakeTime()
    client, sent = _client(iter([httpx.ConnectError, 502, 503, 504, 200]), fake_time)

    response = await client.get("/v1/sessions/s/agent/contents")

    assert response.status_code == 200
    assert sent == ["GET"] * 5
    assert fake_time.sleeps == [0.5, 1.0, 2.0, 4.0]


@pytest.mark.parametrize(
    ("outcomes", "expected_sends", "expected_status"),
    [
        # Refused: the request never reached the server, so a POST is resent.
        ([httpx.ConnectError, 201], 2, 201),
        # A gateway error may follow a POST the server handled: never resent.
        ([502, 201], 1, 502),
    ],
)
@pytest.mark.asyncio
async def test_post_is_resent_only_when_it_never_reached_the_server(
    outcomes: list[_Outcome], expected_sends: int, expected_status: int
) -> None:
    client, sent = _client(iter(outcomes), _FakeTime())

    response = await client.post("/v1/sessions/s/events", json={"type": "message"})

    assert (len(sent), response.status_code) == (expected_sends, expected_status)


@pytest.mark.asyncio
async def test_other_errors_fail_at_once() -> None:
    client, sent = _client(iter([500, httpx.ReadError]), _FakeTime())

    assert (await client.get("/a")).status_code == 500
    with pytest.raises(httpx.ReadError):
        await client.get("/b")
    assert sent == ["GET", "GET"]


@pytest.mark.asyncio
async def test_fail_fast_request_is_not_retried() -> None:
    fake_time = _FakeTime()
    client, sent = _client(iter([httpx.ConnectError, 200]), fake_time)

    with pytest.raises(httpx.ConnectError):
        await client.post("/policies/evaluate", json={}, extensions={FAIL_FAST_EXTENSION: True})

    assert sent == ["POST"]
    assert fake_time.sleeps == []


@pytest.mark.parametrize(
    ("last_outcome", "check"),
    [
        (502, lambda call: call.status_code == 502),
        (httpx.ConnectError, None),
    ],
)
@pytest.mark.asyncio
async def test_gives_up_after_the_window_with_the_last_outcome(
    last_outcome: _Outcome, check: Callable[[httpx.Response], bool] | None
) -> None:
    fake_time = _FakeTime()
    client, sent = _client(iter(lambda: last_outcome, None), fake_time, retry_window_s=10.0)

    if check is None:
        with pytest.raises(httpx.ConnectError):
            await client.get("/a")
    else:
        assert check(await client.get("/a"))
    assert fake_time.now <= 10.0
    assert len(sent) == len(fake_time.sleeps) + 1 > 1


@pytest.mark.asyncio
async def test_stops_retrying_once_shutdown_begins() -> None:
    client, sent = _client(iter([httpx.ConnectError, 200]), _FakeTime())
    client.stop_retrying_when(lambda: True)

    with pytest.raises(httpx.ConnectError):
        await client.get("/a")
    assert sent == ["GET"]
