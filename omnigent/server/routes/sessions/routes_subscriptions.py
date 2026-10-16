"""Session subscription routes: let one session hear another's turn ends."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import Response

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server import session_subscriptions
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import require_access as _require_access
from omnigent.server.routes._auth_helpers import require_user as _require_user
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.schemas import (
    SessionSubscriptionList,
    SessionSubscriptionObject,
    SessionSubscriptionRequest,
)
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore


def register_subscriptions_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Register the session subscription routes on router."""

    async def _live_session(session_id: str) -> None:
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise _session_not_found()
        if conv.archived:
            raise OmnigentError(f"Session {session_id!r} is archived.", code=ErrorCode.CONFLICT)

    @router.post("/sessions/{session_id}/subscriptions")
    async def subscribe_session(
        request: Request,
        session_id: str,
        body: SessionSubscriptionRequest,
    ) -> SessionSubscriptionObject:
        """
        Subscribe this session to another session's turn ends and prompts.

        From now on this session gets a ``[System: …]`` message whenever the
        watched session ends a turn (completed, cancelled or failed) or blocks
        on a prompt; a prompt it is already blocked on is delivered at once.
        Subscribing twice is a no-op. The watched session is unchanged: no
        parent link, runner or lifecycle coupling.

        Requires edit access to this session and read access to the watched one.

        :raises OmnigentError: 400 for a self-subscription, 404 for an unknown
            or unreadable session, 409 when either session is archived or the
            watched session already subscribes to this one (the two would wake
            each other forever).
        """
        user_id = _require_user(request, auth_provider)
        target_id = body.session_id
        await _require_access(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        await _require_access(user_id, target_id, LEVEL_READ, permission_store, conversation_store)
        if target_id == session_id:
            raise OmnigentError(
                "A session cannot subscribe to itself.", code=ErrorCode.INVALID_INPUT
            )
        await _live_session(session_id)
        await _live_session(target_id)
        if session_id in await asyncio.to_thread(
            conversation_store.list_session_subscriptions, target_id
        ):
            raise OmnigentError(
                f"Session {target_id!r} already subscribes to {session_id!r}; "
                "a mutual subscription would wake both sessions forever.",
                code=ErrorCode.CONFLICT,
            )
        await asyncio.to_thread(conversation_store.add_session_subscription, session_id, target_id)
        await session_subscriptions.deliver_pending_prompts(session_id, target_id)
        return SessionSubscriptionObject(subscriber_id=session_id, session_id=target_id)

    @router.delete("/sessions/{session_id}/subscriptions/{target_id}", status_code=204)
    async def unsubscribe_session(request: Request, session_id: str, target_id: str) -> Response:
        """
        Stop this session hearing about another session. Idempotent.

        Requires edit access to this session.
        """
        user_id = _require_user(request, auth_provider)
        await _require_access(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        await asyncio.to_thread(
            conversation_store.delete_session_subscription, session_id, target_id
        )
        return Response(status_code=204)

    @router.get("/sessions/{session_id}/subscriptions")
    async def list_session_subscriptions(
        request: Request, session_id: str
    ) -> SessionSubscriptionList:
        """
        List the sessions this session is subscribed to.

        Requires read access to this session.
        """
        user_id = _require_user(request, auth_provider)
        await _require_access(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        if await asyncio.to_thread(conversation_store.get_conversation, session_id) is None:
            raise _session_not_found()
        target_ids = await asyncio.to_thread(
            conversation_store.list_session_subscriptions, session_id
        )
        return SessionSubscriptionList(
            data=[
                SessionSubscriptionObject(subscriber_id=session_id, session_id=target_id)
                for target_id in target_ids
            ]
        )
