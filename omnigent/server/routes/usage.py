"""API route for the per-user LLM cost report (``omni usage``)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query, Request

from omnigent._wrapper_labels import WRAPPER_LABEL_KEY
from omnigent.entities import Agent, Conversation
from omnigent.errors import restart_on_stale_cursor
from omnigent.runtime.policies.builder import (
    _sum_subtree_usage,
    load_session_usage_and_tree,
)
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.feature_flags import Feature, FeatureFlags, resolve_feature_flags
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._sessions import helpers as _session_helpers
from omnigent.server.routes._sessions.helpers import _resolve_harness_impl
from omnigent.server.schemas import DailyCost, SessionUsage, UsageReport
from omnigent.stores import ConversationStore

# The daily rollup floor for an "all-time" sum: earlier than any real row, so
# ``sum_daily_cost`` with this lower bound totals every recorded day.
_EPOCH_DAY = "0000-00-00"


# Agents the current report preloaded in one read, so resolving each session's
# harness and model does not hit the agent store once per session.
_preloaded_agents: ContextVar[Mapping[str, Agent] | None] = ContextVar(
    "usage_preloaded_agents", default=None
)

_AGENT_READ_CHUNK = 500


@contextmanager
def _preload_agents(conversations: Iterable[Conversation]) -> Iterator[None]:
    """
    Read every agent the given conversations are bound to in a few queries.

    While active, :func:`_resolve_session_harness` and :func:`_resolve_llm_model`
    take agents from this map instead of one store read per session. Without a
    runtime agent store the resolvers keep their own lookup.
    """
    from omnigent.runtime._globals import _agent_store

    agent_ids = sorted({conv.agent_id for conv in conversations if conv.agent_id is not None})
    agents: dict[str, Agent] = {}
    if _agent_store is not None:
        for start in range(0, len(agent_ids), _AGENT_READ_CHUNK):
            agents.update(_agent_store.get_many(agent_ids[start : start + _AGENT_READ_CHUNK]))
    token = _preloaded_agents.set(agents if _agent_store is not None else None)
    try:
        yield
    finally:
        _preloaded_agents.reset(token)


def _resolve_llm_model(conv: Conversation) -> str | None:
    """Resolve a session's LLM model from its bound agent, using preloaded agents."""
    return _session_helpers._resolve_llm_model(conv, agents_by_id=_preloaded_agents.get())


def _utc_today() -> str:
    """Return the current UTC calendar day as ``"YYYY-MM-DD"``."""
    from omnigent.db.utils import now_epoch

    return datetime.fromtimestamp(now_epoch(), tz=UTC).date().isoformat()


def _day_offset(day_utc: str, *, days: int) -> str:
    """Return the UTC day *days* before *day_utc*, as ``"YYYY-MM-DD"``."""
    base = datetime.strptime(day_utc, "%Y-%m-%d").replace(tzinfo=UTC)
    return (base - timedelta(days=days)).date().isoformat()


def _session_models(usage: dict[str, Any]) -> dict[str, float]:
    """
    Project a session's ``by_model`` map into a ``{model_id: cost_usd}`` dict.

    Mirrors the web session sidebar's per-model list: each model's recorded
    cost, keyed by the raw harness model id, shown faithfully. Models with no
    recorded cost are omitted. NOT guaranteed to sum to the session's
    ``total_cost_usd`` — see :class:`SessionUsage`.

    :param usage: A subtree-summed ``session_usage`` dict.
    :returns: Per-model cost map (empty when no per-model cost was recorded).
    """
    by_model = usage.get("by_model")
    if not isinstance(by_model, dict):
        return {}
    models: dict[str, float] = {}
    for name, bucket in by_model.items():
        if not isinstance(bucket, dict) or "total_cost_usd" not in bucket:
            continue
        try:
            models[str(name)] = float(bucket["total_cost_usd"])
        except (TypeError, ValueError):
            continue
    return models


def _collect_other_harnesses(
    primary: str | None,
    tree: list[Conversation],
    root_id: str,
) -> list[str] | None:
    """Distinct harnesses used by sub-agents, excluding the primary."""
    seen: set[str] = set()
    for conv in tree:
        if conv.id == root_id:
            continue
        h = _resolve_session_harness(conv)
        if h and h != primary:
            seen.add(h)
    return sorted(seen) if seen else None


def _resolve_session_harness(conv: Conversation) -> str | None:
    """
    Best-effort harness resolution for the usage report.

    Checks, in order: (1) per-session harness override, (2) the
    ``omnigent.wrapper`` label stamped at creation by native wrappers,
    (3) agent-spec resolution via the runtime (works when the server has
    a live agent cache). Returns a display-friendly name or ``None``.
    """
    if conv.harness_override:
        return conv.harness_override
    wrapper = conv.labels.get(WRAPPER_LABEL_KEY)
    if wrapper:
        # Strip the "-ui" suffix for display: "claude-code-native-ui" → "claude-code-native"
        return wrapper.removesuffix("-ui") if wrapper.endswith("-ui") else wrapper
    return _resolve_harness_impl(conv, agents_by_id=_preloaded_agents.get())


def _session_cost(usage: dict[str, Any]) -> float:
    """
    Read a session's authoritative cumulative cost, or ``0.0`` when unpriced.

    ``total_cost_usd`` is present only on priced sessions (the "priced ⟺ key
    present" contract); an absent or malformed value reads as ``0.0``.
    """
    if "total_cost_usd" not in usage:
        return 0.0
    try:
        return float(usage["total_cost_usd"])
    except (TypeError, ValueError):
        return 0.0


_TREE_PAGE_SIZE = 500


def _load_trees_by_root(
    conversation_store: ConversationStore,
    root_ids: list[str],
) -> dict[str, list[Conversation]]:
    """
    Load the spawn trees of many roots in a few reads instead of one per root.

    :param conversation_store: Store to read from.
    :param root_ids: Tree roots, e.g. one listing page's sessions.
    :returns: Every conversation (archived included) grouped by root id.
    """
    trees: dict[str, list[Conversation]] = {root_id: [] for root_id in root_ids}
    after: str | None = None
    while root_ids:
        page = conversation_store.list_conversations(
            limit=_TREE_PAGE_SIZE,
            after=after,
            kind=None,
            root_conversation_ids=root_ids,
            include_archived=True,
        )
        for tree_conv in page.data:
            trees.setdefault(tree_conv.root_conversation_id, []).append(tree_conv)
        if not page.has_more or page.last_id is None:
            break
        after = page.last_id
    return trees


@restart_on_stale_cursor
def _build_usage_report(
    conversation_store: ConversationStore,
    user_id: str | None,
    *,
    include_page_details: bool = False,
    days: int | None = None,
) -> UsageReport:
    """
    Build the usage report: a daily-rollup cost summary plus session detail.

    A session deleted while the detail enumeration is paging invalidates the
    page cursor; the whole report is rebuilt (via
    :func:`restart_on_stale_cursor`) so the detail list is never silently
    truncated.

    The summary (today / last 7 days / last 30 days / all-time) is summed
    from the per-user daily-cost rollup (``user_daily_cost``), which
    attributes spend to the UTC day it occurred on — so the windows reflect
    when spend actually happened, not merely a session's last-activity time.

    The per-session detail is a separate view over each top-level session's
    cumulative ``session_usage`` (rolled up across its sub-agent subtree via
    :func:`load_session_usage_and_tree`), newest activity first, carrying the
    authoritative session cost and the per-model breakdown.

    :param conversation_store: Store to read the rollup and sessions from.
    :param user_id: The caller / ACL scope. ``None`` in single-user mode maps
        to the reserved local owner the daily rollup and grants are keyed by.
    :param include_page_details: Populate the timeline and display metadata
        used only by the release-gated web Usage page.
    :param days: Restrict the per-session detail to sessions active within the
        last *days* UTC calendar days (today included); ``None`` lists every
        session. The listing is newest-activity-first, so paging stops at the
        first session older than the window instead of reading every tree.
    :returns: The populated :class:`UsageReport`.
    """
    # The daily rollup and session-permission grants key spend by the resolved
    # owner, which is the reserved local sentinel in single-user mode (where
    # require_user yields None). Map None -> "local" so the summary reads the
    # same rows the write path recorded.
    rollup_user = user_id if user_id is not None else RESERVED_USER_LOCAL

    today = _utc_today()
    cost_today = conversation_store.sum_daily_cost(rollup_user, today)
    cost_7d = conversation_store.sum_daily_cost(rollup_user, _day_offset(today, days=6))
    cost_30d = conversation_store.sum_daily_cost(rollup_user, _day_offset(today, days=29))
    total = conversation_store.sum_daily_cost(rollup_user, _EPOCH_DAY)

    cutoff_epoch: int | None = None
    if days is not None:
        window_start = _day_offset(today, days=days - 1)
        cutoff_epoch = int(
            datetime.strptime(window_start, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
        )

    sessions: list[SessionUsage] = []
    after: str | None = None
    window_exhausted = False
    while not window_exhausted:
        page = conversation_store.list_conversations(
            limit=200,
            after=after,
            accessible_by=user_id,
            has_agent_id=True,
            kind="default",
            order="desc",
            sort_by="updated_at",
        )
        # Newest first: the window ends at the first older row, and nothing
        # past it needs its tree read.
        in_window = page.data
        if cutoff_epoch is not None:
            for position, conv in enumerate(page.data):
                if conv.updated_at < cutoff_epoch:
                    in_window = page.data[:position]
                    window_exhausted = True
                    break
        trees = _load_trees_by_root(
            conversation_store,
            [conv.root_conversation_id for conv in in_window],
        )
        page_conversations = [
            *in_window,
            *(member for tree in trees.values() for member in tree),
        ]
        with _preload_agents(page_conversations) if include_page_details else nullcontext():
            for conv in in_window:
                if conv.agent_id is None:
                    continue
                # One batched read covers the whole page's trees. A row missing
                # from its tree (deleted and recreated mid-request) falls back to
                # the verified per-session load.
                tree = trees.get(conv.root_conversation_id, [])
                if any(member.id == conv.id for member in tree):
                    usage = _sum_subtree_usage(tree, conv.id)
                else:
                    usage, tree = load_session_usage_and_tree(conv.id, conversation_store)
                primary_harness = _resolve_session_harness(conv) if include_page_details else None
                other_harnesses = (
                    _collect_other_harnesses(primary_harness, tree, conv.id)
                    if include_page_details
                    else None
                )
                sessions.append(
                    SessionUsage(
                        id=conv.id,
                        created_at=conv.created_at,
                        updated_at=conv.updated_at,
                        title=conv.title,
                        cost_usd=_session_cost(usage),
                        models=_session_models(usage),
                        harness=primary_harness,
                        other_harnesses=other_harnesses,
                        llm_model=(
                            conv.model_override or _resolve_llm_model(conv)
                            if include_page_details
                            else None
                        ),
                        agent_name=conv.sub_agent_name if include_page_details else None,
                    )
                )
        if not page.has_more:
            break
        after = page.last_id

    daily_costs_raw = (
        conversation_store.list_daily_costs(rollup_user, _EPOCH_DAY)
        if include_page_details
        else []
    )

    return UsageReport(
        cost_today=cost_today,
        cost_last_7d=cost_7d,
        cost_last_30d=cost_30d,
        total_cost_usd=total,
        daily_costs=[DailyCost(day=d, cost_usd=c) for d, c in daily_costs_raw],
        sessions=sessions,
    )


def create_usage_router(
    conversation_store: ConversationStore,
    *,
    auth_provider: AuthProvider | None = None,
    feature_flags: FeatureFlags | None = None,
) -> APIRouter:
    """
    Create the per-user usage-report router.

    The report is user-scoped, not session-scoped, so it lives in its own
    router rather than under the sessions router.

    :param conversation_store: Store for the daily rollup and session reads.
    :param auth_provider: Auth provider for user identity. ``None`` disables
        auth (single-user / local mode).
    :param feature_flags: Immutable deployment release-feature snapshot.
        When omitted, resolves ``OMNIGENT_FEATURES`` at router construction.
    :returns: The configured router (mounted under ``/v1``).
    """
    flags = feature_flags or resolve_feature_flags()
    router = APIRouter()

    @router.get("/usage", response_model=UsageReport)
    async def get_usage(
        request: Request,
        days: int | None = Query(
            default=None,
            ge=1,
            le=3650,
            description="Only list sessions active in the last N UTC days.",
        ),
    ) -> UsageReport:
        """
        Aggregate the calling user's LLM spend across their sessions.

        require_user, not get_user_id: the aggregation scopes to the caller,
        so a request slipping through as ``None`` in multi-user mode would
        read another scope. Fail closed with 401 instead (``user_id`` is
        ``None`` only when auth is disabled — the single-user / local case).
        """
        user_id = require_user(request, auth_provider)
        return await asyncio.to_thread(
            _build_usage_report,
            conversation_store,
            user_id,
            include_page_details=flags.enabled(Feature.USAGE_PAGE),
            days=days,
        )

    return router
