"""Ride out a server restart in the runner's direct HTTP calls to the server.

A deploy restarts the server while runners keep their turns. Until the new
process listens, the runner's calls are refused, or a reverse proxy in front
answers 502/503/504. The tunnel already reconnects through that; this client
gives every other runner call to the server the same patience, for a bounded
window, instead of failing the turn that made it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

_logger = logging.getLogger(__name__)

# Long enough for a container restart behind a proxy; past it, callers see the error.
SERVER_RESTART_RETRY_WINDOW_S = 120.0
_INITIAL_DELAY_S = 0.5
_MAX_DELAY_S = 5.0
# Request extension that opts one call out of the window (a caller with its own retry).
FAIL_FAST_EXTENSION = "omnigent_fail_fast"
# What a proxy answers while the server behind it is down.
_GATEWAY_STATUSES = frozenset({502, 503, 504})
# Retrying these after a gateway answer cannot repeat a side effect.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


def _replayable(request: httpx.Request) -> bool:
    """Whether *request*'s body can be sent again (no body, or bytes already in memory)."""
    return isinstance(request.stream, httpx.ByteStream)


def _server_unavailable(request: httpx.Request, outcome: httpx.Response | Exception) -> bool:
    """Whether *outcome* means the server is restarting, and *request* is safe to resend.

    A refused or timed-out connect never reached the server, so any method may
    be resent. A gateway status may follow a request the server partly handled,
    so only idempotent methods are.
    """
    if not _replayable(request):
        return False
    if isinstance(outcome, (httpx.ConnectError, httpx.ConnectTimeout)):
        return True
    return (
        isinstance(outcome, httpx.Response)
        and outcome.status_code in _GATEWAY_STATUSES
        and request.method in _IDEMPOTENT_METHODS
    )


class ServerRestartRetryClient(httpx.AsyncClient):
    """An :class:`httpx.AsyncClient` that waits out a server restart before failing.

    :param retry_window_s: Longest time to keep retrying one request, in seconds.
    :param sleep: Awaitable sleep, replaceable in tests.
    :param clock: Monotonic clock, replaceable in tests.
    """

    def __init__(
        self,
        *,
        retry_window_s: float = SERVER_RESTART_RETRY_WINDOW_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        **kwargs: Any,  # type: ignore[explicit-any]
    ) -> None:
        super().__init__(**kwargs)
        self._retry_window_s = retry_window_s
        self._sleep = sleep
        self._clock = clock
        self._stop_retrying: Callable[[], bool] = lambda: False

    def stop_retrying_when(self, predicate: Callable[[], bool]) -> None:
        """Fail at once, as a plain client does, whenever *predicate* holds (e.g. shutdown)."""
        self._stop_retrying = predicate

    async def send(  # type: ignore[override]
        self, request: httpx.Request, **kwargs: Any
    ) -> httpx.Response:
        """Send *request*, resending it while the server is restarting, within the window."""
        deadline: float | None = None
        delay = _INITIAL_DELAY_S
        while True:
            outcome: httpx.Response | Exception
            try:
                outcome = await super().send(request, **kwargs)
            except httpx.TransportError as exc:
                outcome = exc
            if (
                request.extensions.get(FAIL_FAST_EXTENSION)
                or not _server_unavailable(request, outcome)
                or self._stop_retrying()
            ):
                return _result(outcome)
            now = self._clock()
            if deadline is None:
                deadline = now + self._retry_window_s
                _logger.warning(
                    "Server unavailable for %s %s (%s); retrying for up to %.0fs",
                    request.method,
                    request.url.path,
                    _describe(outcome),
                    self._retry_window_s,
                )
            if now + delay > deadline:
                _logger.warning(
                    "Server still unavailable for %s %s after %.0fs; giving up",
                    request.method,
                    request.url.path,
                    self._retry_window_s,
                )
                return _result(outcome)
            if isinstance(outcome, httpx.Response):
                await outcome.aclose()
            await self._sleep(delay)
            delay = min(delay * 2, _MAX_DELAY_S)


def _result(outcome: httpx.Response | Exception) -> httpx.Response:
    """Return the response, or raise the transport error, as a plain client would."""
    if isinstance(outcome, Exception):
        raise outcome
    return outcome


def _describe(outcome: httpx.Response | Exception) -> str:
    if isinstance(outcome, httpx.Response):
        return f"HTTP {outcome.status_code}"
    return type(outcome).__name__
