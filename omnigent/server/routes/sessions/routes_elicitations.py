"""Elicitation routes: resolve and get elicitations."""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

from fastapi import (
    APIRouter,
    Request,
)

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.routing import RunnerRouter
from omnigent.runtime import (
    pending_elicitations,
)
from omnigent.runtime.policies.approval import _ELICITATION_MODE
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_elicitation_registry,
    _harness_parked_elicitations,
    _harness_pre_resolved_elicitations,
    _ParkedHarnessElicitation,
    _PreResolvedHarnessElicitation,
)
from omnigent.server.auth import (
    LEVEL_EDIT,
    AuthProvider,
)
from omnigent.server.routes._auth_helpers import (
    get_user_id as _get_user_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.routes._sessions.common import (
    _logger,
    get_server_runner_router,
    session_stream,
    set_server_runner_router,
)
from omnigent.server.routes._sessions.helpers import (
    _apply_pending_policy_ask_writes,
    _publish_elicitation_request_to_ancestors,
    _structured_ask_user_question,
)
from omnigent.server.routes._sessions.orchestration import (
    _resolve_elicitation,
)
from omnigent.server.schemas import (
    ElicitationRequestEvent,
    ElicitationRequestParams,
    ElicitationResult,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.tools.builtins.ask_user_question import QUESTION_ID_PREFIX, SysAskUserQuestionTool

# Level needed to view or answer an elicitation. Elicitations carry
# collaborative prompts (``AskUserQuestion``, plan review, MCP forms) as
# well as policy ASK gates, and the in-band ``approval`` session event
# resolves the same elicitations at the events route's edit bar — so the
# two paths must stay at the same level. Raising it to owner is a product
# decision that needs a "waiting for the owner" UI state first.
ELICITATION_RESOLVE_LEVEL: int = LEVEL_EDIT


def register_elicitations_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    runner_router: RunnerRouter | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Register the elicitations routes on router."""

    @router.post(
        "/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
        # Internal elicitation flow — hidden from the public API reference.
        include_in_schema=False,
        status_code=202,
        # response_model=None: the body is a small acknowledgement
        # dict, not a domain model.
        response_model=None,
    )
    async def resolve_elicitation(
        request: Request,
        session_id: str,
        elicitation_id: str,
        body: ElicitationResult,
    ) -> dict[str, bool]:
        """
        Resolve an outstanding elicitation by its URL (URL-based
        elicitation).

        The dedicated, RESTful counterpart to delivering a verdict
        via the ``type == "approval"`` event on
        ``POST /v1/sessions/{id}/events``. An elicitation request
        published in ``mode == "url"`` carries this endpoint's path
        as its ``params.url``; the client hits it directly with the
        MCP :class:`ElicitationResult` body instead of POSTing a
        generic approval event. The verdict routes through the
        shared :func:`_resolve_elicitation`, so resolution semantics
        are identical to the event path.

        The ``elicitation_id`` is taken from the URL rather than the
        body, so the unguessable id (``secrets.token_hex(16)``) is
        the capability scoping the resolution — combined with the
        :data:`ELICITATION_RESOLVE_LEVEL` gate below (edit, not owner:
        any collaborator who can drive the agent may answer it) and
        the server-side ownership check inside
        :func:`_resolve_elicitation`.

        :param request: The inbound request, used for identity
            extraction.
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param elicitation_id: Correlation id of the elicitation to
            resolve, e.g. ``"elicit_abc123"``. Taken from the URL
            path, not the body.
        :param body: The MCP-shaped verdict — ``action``
            (``"accept"`` / ``"decline"`` / ``"cancel"``) plus
            optional form ``content``.
        :returns: ``{"queued": False}`` — resolution is synchronous
            and persists no conversation item.
        :raises OmnigentError: 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, ELICITATION_RESOLVE_LEVEL, permission_store, conversation_store
        )
        conv = access.conversation
        if conv is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        _resolve_data = {"elicitation_id": elicitation_id, **body.model_dump(exclude_none=True)}
        # Wired by the app; absent, a question answer refuses and keeps its card.
        post_message = getattr(request.app.state, "post_server_message", None)
        await _resolve_elicitation(
            session_id,
            _resolve_data,
            runner_router,
            conversation_store,
            post_answer=(
                (lambda text: post_message(session_id, user_id, text))
                if post_message is not None
                else None
            ),
        )
        # Apply any policy writes deferred by the relay tool-call ASK gate
        # (e.g. a cost-budget checkpoint) now that the verdict is in.
        await _apply_pending_policy_ask_writes(
            session_id, conv, conversation_store, agent_store, _resolve_data
        )
        return {"queued": False}

    @router.post(
        "/sessions/{session_id}/questions",
        # Runner-side tool endpoint — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
    )
    async def ask_question(
        request: Request,
        session_id: str,
        body: dict[str, Any],
    ) -> dict[str, str]:
        """
        Publish a ``sys_ask_user_question`` card and return its id without waiting.

        The card is an ordinary form elicitation carrying the
        ``ask_user_question`` payload, so the pending index, session-list
        badge, web card and notifications show it like any other prompt.
        Nothing is parked: the verdict reaches the session later as a user
        message (see :func:`_resolve_elicitation`).

        :param request: The inbound request, used for identity extraction.
        :param session_id: Session that asks, e.g. ``"conv_abc123"``.
        :param body: ``{"questions": [{"question", "options", ...}]}``, the
            ``AskUserQuestion`` input shape.
        :returns: ``{"question_id": "question_<hex>", "status": "waiting"}``.
        :raises OmnigentError: 404 if no session exists; 400 if no question
            has a text and at least one labelled option.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        if access.conversation is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        ask_payload = _structured_ask_user_question(body)
        if ask_payload is None:
            raise OmnigentError(
                "questions must hold at least one question with a text and labelled options",
                code=ErrorCode.INVALID_INPUT,
            )
        # ponytail: the card lives in the in-memory pending index, so a server
        # restart drops it; persist questions with the session if that matters.
        question_id = f"{QUESTION_ID_PREFIX}{secrets.token_hex(16)}"
        params = ElicitationRequestParams(
            mode="form",
            message="\n".join(question["question"] for question in ask_payload["questions"]),
            policy_name=SysAskUserQuestionTool.name(),
            ask_user_question=ask_payload,
        )
        event_payload = ElicitationRequestEvent(
            type="response.elicitation_request",
            elicitation_id=question_id,
            params=params,
        ).model_dump()
        session_stream.publish(session_id, event_payload)
        await asyncio.to_thread(
            _publish_elicitation_request_to_ancestors,
            conversation_store,
            session_id,
            event_payload,
        )
        return {"question_id": question_id, "status": "waiting"}

    @router.get(
        "/sessions/{session_id}/elicitations/{elicitation_id}",
        # Internal elicitation flow — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
    )
    async def get_elicitation(
        request: Request,
        session_id: str,
        elicitation_id: str,
    ) -> dict[str, Any]:
        """
        Return the state of a pending elicitation as JSON.

        Used by the frontend's standalone approval page
        (``/approve/:sessionId/:elicitationId``) to fetch the
        elicitation prompt and render approve/reject controls.
        The payload is read from the in-memory
        :mod:`omnigent.runtime.pending_elicitations` index — no
        database persistence required.

        :param request: The inbound request, used for identity
            extraction.
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param elicitation_id: Correlation id of the elicitation,
            e.g. ``"elicit_abc123"``.
        :returns: JSON with ``status`` (``"pending"`` or
            ``"resolved"``), and when pending: ``message``,
            ``phase``, ``policy_name``, ``content_preview``. Native requests
            may include ``resolution`` (``"unanswered"`` or ``"confirmed"``)
            after clearing; absence alone does not prove native confirmation.
        :raises OmnigentError: 404 if the session does not exist.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, ELICITATION_RESOLVE_LEVEL, permission_store, conversation_store
        )
        if access.conversation is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()

        found = pending_elicitations.lookup(elicitation_id, conversation_id=session_id)
        if found is None:
            resolution = pending_elicitations.native_resolution_for(session_id, elicitation_id)
            return {"status": "resolved", **({"resolution": resolution} if resolution else {})}

        _conv_id, event = found
        params_value = event.get("params")
        params = params_value if isinstance(params_value, dict) else {}
        if params.get("target_session_id") not in (None, session_id):
            return {"status": "resolved"}
        return {
            "status": "pending",
            "message": params.get("message", "Approval required"),
            "phase": params.get("phase", ""),
            "policy_name": params.get("policy_name", ""),
            "content_preview": params.get("content_preview", ""),
            **{
                key: params[key]
                for key in ("native_delivery", "permission_directories", "ask_user_question")
                if key in params
            },
        }
