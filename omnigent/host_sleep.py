"""Keep the time a host slept out of a turn's ``asyncio.timeout`` watchdogs.

The harness runner's event loop is uvloop. On macOS its clock keeps counting
while the host sleeps, and ``time.monotonic`` stops. So on wake a deadline on
the loop's clock is already past, and it fires before the agent's next message
is read. :class:`HostSleepWatch` measures how far the loop's clock has run ahead
of ``time.monotonic`` and moves the deadlines it protects later by that much.
Where the two clocks agree (the stdlib loop, Linux, Windows) it moves nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Iterable

_STEP_S = 1.0
# Below this the difference is clock noise, not a sleep: uvloop reads its clock
# once per loop iteration, so mid-iteration it reads a little low.
_NOISE_S = 0.5


def _ahead(loop: asyncio.AbstractEventLoop) -> float:
    """Seconds the loop's clock has run ahead of the clock that stops in a sleep."""
    return loop.time() - time.monotonic()


class HostSleepWatch:
    """Move the deadlines of *timeouts()* later by each host sleep, until :meth:`stop`.

    The tick is a plain loop callback, not a task: the loop runs due timers in
    deadline order, so after a wake the tick runs before a deadline it protects
    and reschedules it. A deadline less than one step away when the host went
    to sleep can still fire first.

    ponytail: the first clock reading is trusted. Taken more than ``_NOISE_S``
    into one loop iteration it reads low, and that much is later added once.
    """

    def __init__(self, timeouts: Callable[[], Iterable[asyncio.Timeout | None]]) -> None:
        self._timeouts = timeouts
        self._loop = asyncio.get_running_loop()
        self._credited = _ahead(self._loop)
        self._handle: asyncio.TimerHandle | None = self._loop.call_later(_STEP_S, self._tick)

    def check(self) -> None:
        """Credit any host sleep not yet credited.

        Call it before reading or setting a protected deadline, so that a sleep
        is credited exactly once, whichever runs first after the wake.
        """
        ahead = _ahead(self._loop)
        lost = ahead - self._credited
        if lost < _NOISE_S:
            return  # a low reading is a stale loop clock: keep the old mark
        self._credited = ahead
        for timeout in self._timeouts():
            push_back(timeout, lost)

    def _tick(self) -> None:
        self._handle = self._loop.call_later(_STEP_S, self._tick)
        self.check()

    def stop(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None


def push_back(timeout: asyncio.Timeout | None, seconds: float) -> None:
    """Move a running ``asyncio.timeout``'s deadline *seconds* later."""
    if timeout is None or (when := timeout.when()) is None:
        return
    with contextlib.suppress(RuntimeError):  # not entered yet, or already expiring
        timeout.reschedule(when + seconds)
