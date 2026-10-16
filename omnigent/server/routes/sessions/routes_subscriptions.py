"""Session subscription routes: let one session hear another's turn ends."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server import session_subscriptions
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, AuthProvider
from omnigent.server.permissions import check_session_access
from omnigent.server.routes._auth_helpers import require_access as _require_access
from omnigent.server.routes._auth_helpers import require_user as _require_user
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.schemas import (
    SessionSubscriptionList,
    SessionSubscriptionObject,
    SessionSubscriptionRequest,
)
from omnigent.stores import ConversationStore
from omnigent.stores.conversation_store import (
    MAX_SUBSCRIPTIONS_PER_SESSION,
    SubscriptionCycleError,
    SubscriptionLimitError,
)
from omnigent.stores.permission_store import PermissionStore

_LIST_SCAN = 100


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
            watched session already hears this one through subscriptions (they
            would wake each other in a loop), when there are too many
            subscriptions to follow to rule that out, or when this session
            already watches the most sessions one session can.
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
        try:
            created = await asyncio.to_thread(
                conversation_store.add_session_subscription, session_id, target_id
            )
        except SubscriptionLimitError:
            raise OmnigentError(
                f"Session {session_id!r} already watches {MAX_SUBSCRIPTIONS_PER_SESSION} "
                "sessions, the most one session can; unsubscribe from one first.",
                code=ErrorCode.CONFLICT,
            ) from None
        except SubscriptionCycleError as cycle:
            raise OmnigentError(
                f"Session {target_id!r} could not be checked for a subscription loop back to "
                f"{session_id!r}: too many subscriptions to follow."
                if cycle.bounded
                else f"Session {target_id!r} already hears {session_id!r} through "
                "subscriptions; subscribing back would wake them in a loop forever.",
                code=ErrorCode.CONFLICT,
            ) from None
        if created:
            # Only a new subscription replays prompts; a repeat already heard them.
            try:
                await session_subscriptions.record_pending_prompts(session_id, target_id)
            except Exception:
                # Undo, so a retried request is new again and replays them.
                await asyncio.to_thread(
                    conversation_store.delete_session_subscription, session_id, target_id
                )
                raise
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
        request: Request,
        session_id: str,
        after: str | None = Query(
            default=None, description="Continuation key: the previous page's ``last_id``."
        ),
        limit: int = Query(default=100, ge=1, le=100),
    ) -> SessionSubscriptionList:
        """
        List one page of the sessions this session is subscribed to, by session id.

        Requires read access to this session. Watched sessions the caller
        cannot read are left out, and never show up as a continuation key.
        """
        user_id = _require_user(request, auth_provider)
        await _require_access(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        if await asyncio.to_thread(conversation_store.get_conversation, session_id) is None:
            raise _session_not_found()
        page, has_more = await asyncio.to_thread(_readable_page, user_id, session_id, after, limit)
        return SessionSubscriptionList(
            data=[
                SessionSubscriptionObject(subscriber_id=session_id, session_id=target_id)
                for target_id in page
            ],
            last_id=page[-1] if page else None,
            has_more=has_more,
        )

    def _readable_page(
        user_id: str | None, session_id: str, after: str | None, limit: int
    ) -> tuple[list[str], bool]:
        """Up to *limit* watched sessions the caller can read, and whether more follow."""
        page: list[str] = []
        while True:
            # Skipping unreadable targets may scan all of them: at most
            # MAX_SUBSCRIPTIONS_PER_SESSION rows, the cap on creating them.
            batch = conversation_store.list_session_subscriptions(
                session_id, after=after, limit=_LIST_SCAN
            )
            for target_id in batch:
                if (
                    user_id is not None
                    and permission_store is not None
                    and not check_session_access(
                        user_id, target_id, LEVEL_READ, permission_store, conversation_store
                    )
                ):
                    continue
                if len(page) == limit:
                    return page, True
                page.append(target_id)
            if len(batch) < _LIST_SCAN:
                return page, False
            after = batch[-1]
