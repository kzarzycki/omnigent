"""Admin-only sampling profiler for the running server (``GET /v1/debug/profile``)."""

from __future__ import annotations

import asyncio
import threading

from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes.sharing import _require_admin
from omnigent.stores.permission_store import PermissionStore
from omnigent.util.stack_sampler import sample_stacks

MAX_PROFILE_SECONDS = 60
DEFAULT_PROFILE_SECONDS = 30


async def _wait_for_disconnect(request: Request) -> None:
    """Return once the client has disconnected."""
    while (await request.receive())["type"] != "http.disconnect":
        pass


def create_debug_profile_router(
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the profiler router (mounted under ``/v1``).

    Delegated device-grant tokens never reach it: they are confined to the
    allowlisted path prefixes in ``omnigent.server.auth``, which omit ``/v1/debug``.
    """
    router = APIRouter()
    running = asyncio.Lock()

    @router.get("/debug/profile", response_class=PlainTextResponse)
    async def get_profile(
        request: Request,
        seconds: int = Query(default=DEFAULT_PROFILE_SECONDS),
    ) -> PlainTextResponse:
        """
        Sample every server thread's stack for *seconds* and return collapsed stacks.

        :param seconds: Window length, 1 to 60.
        :returns: Plain text: collapsed stacks with counts, samples per thread, and the
            sampling interval achieved.
        :raises OmnigentError: 401/403 for a non-admin, 400 for ``seconds`` outside
            1..60, 409 while another profile is running. A cancelled or disconnected
            request ends its sampler before the next profile may start.
        """
        await _require_admin(request, auth_provider, permission_store)
        if not 1 <= seconds <= MAX_PROFILE_SECONDS:
            raise OmnigentError(
                f"seconds must be between 1 and {MAX_PROFILE_SECONDS}",
                code=ErrorCode.INVALID_INPUT,
            )
        if running.locked():
            raise OmnigentError("A profile is already running", code=ErrorCode.CONFLICT)
        async with running:
            stop = threading.Event()
            worker = asyncio.ensure_future(asyncio.to_thread(sample_stacks, seconds, stop=stop))
            disconnect = asyncio.ensure_future(_wait_for_disconnect(request))
            try:
                await asyncio.wait({worker, disconnect}, return_when=asyncio.FIRST_COMPLETED)
                return PlainTextResponse(worker.result() if worker.done() else "")
            finally:
                # Cancellation or disconnect: end the sampler before the lock is released.
                stop.set()
                disconnect.cancel()
                await asyncio.wait({worker})

    return router
