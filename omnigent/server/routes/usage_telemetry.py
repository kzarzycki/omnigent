"""Read-only telemetry route: what the ``omnigent-telemetry`` report reads, aggregated."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from omnigent.entities import Conversation
from omnigent.errors import restart_on_stale_cursor
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes.usage import (
    _load_trees_by_root,
    _preload_agents,
    _preloaded_agents,
    _resolve_session_harness,
    _session_cost,
)
from omnigent.server.schemas import (
    DailyCost,
    TelemetryModelUsage,
    TelemetryScheduledTask,
    TelemetrySession,
    TelemetrySessionUsage,
    TelemetrySidebar,
    UsageTelemetry,
)
from omnigent.stores import ConversationStore
from omnigent.stores.conversation_store import SCHEDULED_TASK_RUN_LABEL_KEY
from omnigent.stores.host_store import HostStore
from omnigent.stores.scheduled_task_store import ScheduledTaskStore

_DAY = 86_400
_DEFAULT_WINDOW_DAYS = 7
_MAX_WINDOW_DAYS = 90
_DEFAULT_MAX_SESSIONS = 200
_MAX_SESSIONS = 1000
_MAX_TASKS = 200
_RUN_PAGE = 100
_MAX_RUN_PAGES = 10  # 1,000 runs per task at most
_MAX_SIDEBAR_ROWS = 5000
_TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens")


def _tokens(bucket: dict[str, Any], key: str) -> int:
    try:
        return int(bucket.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _model_bucket(name: str, bucket: dict[str, Any]) -> TelemetryModelUsage:
    """One ``by_model`` bucket; cost is ``None`` when the model has no price."""
    return TelemetryModelUsage(
        model=name,
        input_tokens=_tokens(bucket, "input_tokens"),
        output_tokens=_tokens(bucket, "output_tokens"),
        cache_read_input_tokens=_tokens(bucket, "cache_read_input_tokens"),
        cost_usd=_session_cost(bucket),
        sessions=1,
    )


def _session_usage(usage: dict[str, Any]) -> TelemetrySessionUsage:
    """Project one session's own ``session_usage`` blob (no subtree sum)."""
    by_model = usage.get("by_model")
    return TelemetrySessionUsage(
        cost_usd=_session_cost(usage),
        input_tokens=_tokens(usage, "input_tokens"),
        output_tokens=_tokens(usage, "output_tokens"),
        cache_read_input_tokens=_tokens(usage, "cache_read_input_tokens"),
        by_model={
            str(name): _model_bucket(str(name), bucket)
            for name, bucket in (by_model if isinstance(by_model, dict) else {}).items()
            if isinstance(bucket, dict)
        },
    )


def _sum_models(sessions: list[TelemetrySession]) -> list[TelemetryModelUsage]:
    """Add every session's per-model buckets up by model, costliest first."""
    totals: dict[str, TelemetryModelUsage] = {}
    for session in sessions:
        for name, bucket in session.usage.by_model.items():
            total = totals.setdefault(name, TelemetryModelUsage(model=name))
            total.sessions += 1
            for key in _TOKEN_KEYS:
                setattr(total, key, getattr(total, key) + getattr(bucket, key))
            if bucket.cost_usd is not None:
                total.cost_usd = (total.cost_usd or 0.0) + bucket.cost_usd
    return sorted(totals.values(), key=lambda m: (-(m.cost_usd or 0.0), m.model))


def _window_roots(
    store: ConversationStore, user_id: str | None, since: int, until: int, cap: int
) -> tuple[list[Conversation], bool]:
    """The caller's top-level sessions last active in the window, newest first, up to *cap*."""
    roots: list[Conversation] = []
    after: str | None = None
    while True:
        page = store.list_conversations(
            limit=200,
            after=after,
            accessible_by=user_id,
            has_agent_id=True,
            kind="default",
            order="desc",
            sort_by="updated_at",
        )
        for conv in page.data:
            if conv.updated_at < since:
                return roots, False
            if conv.updated_at > until:
                continue
            if len(roots) >= cap:
                return roots, True
            roots.append(conv)
        if not page.has_more or page.last_id is None:
            return roots, False
        after = page.last_id


def _sidebar(
    store: ConversationStore, user_id: str | None, truncated: list[str]
) -> TelemetrySidebar:
    """Count the caller's unarchived top-level sessions, and those a scheduled run created."""
    out = TelemetrySidebar()
    after: str | None = None
    seen = 0
    while True:
        page = store.list_conversations(
            limit=200, after=after, accessible_by=user_id, kind="default", order="desc"
        )
        for conv in page.data:
            seen += 1
            out.unarchived += 1
            if SCHEDULED_TASK_RUN_LABEL_KEY in conv.labels:
                out.from_scheduled_tasks += 1
        if not page.has_more or page.last_id is None:
            return out
        if seen >= _MAX_SIDEBAR_ROWS:
            truncated.append("sidebar")
            return out
        after = page.last_id


def _scheduled_tasks(
    task_store: ScheduledTaskStore | None,
    owner: str | None,
    since: int,
    until: int,
    failed_sessions: set[str],
    truncated: list[str],
) -> list[TelemetryScheduledTask]:
    """The caller's tasks (all in single-user mode), each with its window's run outcomes."""
    if task_store is None:
        return []
    tasks = task_store.list(owner_user_id=owner)
    if len(tasks) > _MAX_TASKS:
        tasks = tasks[:_MAX_TASKS]
        truncated.append("scheduled_tasks")
    out: list[TelemetryScheduledTask] = []
    for task in tasks:
        row = TelemetryScheduledTask(
            id=task.id, name=task.name, state=task.state, workspace=task.workspace
        )
        after: str | None = None
        for _ in range(_MAX_RUN_PAGES):
            runs, after = task_store.list_runs(task.id, limit=_RUN_PAGE, after_id=after)
            done = False
            for run in runs:  # newest first
                if run.scheduled_at < since:
                    done = True
                    break
                if run.scheduled_at > until:
                    continue
                row.runs += 1
                session_failed = run.conversation_id in failed_sessions
                if run.status == "failed" or session_failed:
                    row.failed += 1
                elif run.status == "succeeded":
                    row.succeeded += 1
                elif run.status == "skipped":
                    row.skipped += 1
                if row.last_error is None and run.error_code:
                    row.last_error = run.error_code
            if done or after is None:
                break
        else:
            truncated.append(f"scheduled_task_runs:{task.id}")
        out.append(row)
    return out


@restart_on_stale_cursor
def _build_telemetry(
    store: ConversationStore,
    task_store: ScheduledTaskStore | None,
    host_store: HostStore | None,
    user_id: str | None,
    *,
    since: int,
    until: int,
    max_sessions: int,
) -> UsageTelemetry:
    owner = user_id if user_id is not None else RESERVED_USER_LOCAL
    truncated: list[str] = []

    def day(ts: int) -> str:
        return datetime.fromtimestamp(ts, tz=UTC).date().isoformat()

    daily = [
        DailyCost(day=d, cost_usd=c)
        for d, c in store.list_daily_costs(owner, day(since))
        if d <= day(until)
    ]

    roots, cut = _window_roots(store, user_id, since, until, max_sessions)
    if cut:
        truncated.append("sessions")
    trees = _load_trees_by_root(store, [r.root_conversation_id for r in roots])
    members = {m.id: m for tree in trees.values() for m in tree}
    stats = store.get_session_item_stats(list(members))

    hosts: dict[str, str] = {}
    if host_store is not None:
        hosts = {h.host_id: h.name for h in host_store.list_hosts(owner)}

    sessions: list[TelemetrySession] = []
    with _preload_agents(members.values()):
        agents = _preloaded_agents.get() or {}
        for conv in members.values():
            st = stats[conv.id]
            agent = agents.get(conv.agent_id) if conv.agent_id else None
            subs = store.list_session_subscriptions(conv.id, limit=100)
            sessions.append(
                TelemetrySession(
                    id=conv.id,
                    parent_id=conv.parent_conversation_id,
                    root_id=conv.root_conversation_id,
                    title=conv.title,
                    created_at=conv.created_at,
                    updated_at=conv.updated_at,
                    archived=conv.archived,
                    agent=agent.name if agent is not None else None,
                    harness=_resolve_session_harness(conv),
                    host=hosts.get(conv.host_id) if conv.host_id else None,
                    workspace=conv.workspace,
                    live_status=conv.live_status,
                    sub_agent_name=conv.sub_agent_name,
                    labels=dict(conv.labels),
                    items=st.items,
                    turns=st.turns,
                    compactions=st.compactions,
                    inherited_items=st.inherited_items,
                    tool_calls=st.tool_calls,
                    usage=_session_usage(conv.session_usage),
                    subscribes_to=subs,
                )
            )
    sessions.sort(key=lambda s: (s.created_at, s.id))
    failed = {m.id for m in members.values() if m.live_status == "failed"}
    return UsageTelemetry(
        since=since,
        until=until,
        daily_cost=daily,
        models=_sum_models(sessions),
        sessions=sessions,
        scheduled_tasks=_scheduled_tasks(task_store, user_id, since, until, failed, truncated),
        sidebar=_sidebar(store, user_id, truncated),
        truncated=truncated,
    )


def create_usage_telemetry_router(
    conversation_store: ConversationStore,
    *,
    scheduled_task_store: ScheduledTaskStore | None = None,
    host_store: HostStore | None = None,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """
    Create the router for ``GET /v1/usage/telemetry``.

    :param conversation_store: Store for sessions, items, subscriptions and the daily rollup.
    :param scheduled_task_store: Store for scheduled tasks and runs; ``None`` leaves that
        section empty.
    :param host_store: Store for host names; ``None`` leaves each session's ``host`` unset.
    :param auth_provider: Auth provider; ``None`` disables auth (single-user mode).
    :returns: The configured router (mounted under ``/v1``).
    """
    router = APIRouter()

    @router.get("/usage/telemetry", response_model=UsageTelemetry)
    async def get_usage_telemetry(
        request: Request,
        since: int | None = Query(
            default=None, ge=0, description="Window start, epoch seconds. Default: 7 days ago."
        ),
        until: int | None = Query(
            default=None, ge=0, description="Window end, epoch seconds. Default: now."
        ),
        max_sessions: int = Query(
            default=_DEFAULT_MAX_SESSIONS,
            ge=1,
            le=_MAX_SESSIONS,
            description="Most top-level sessions (their sub-agents come with them).",
        ),
    ) -> UsageTelemetry:
        """
        Read-only telemetry for the calling user, aggregated in the server.

        Scoped like ``GET /v1/usage``: sessions the caller holds a grant on,
        the caller's daily-cost rollup and the caller's scheduled tasks. The
        window is ``[since, until]`` (epoch seconds, at most 90 days); a
        session is in it when its top-level session was last active inside
        it, and its sub-agents come with it. Bounds that cut a section short
        are named in ``truncated``.

        Response (``object: "usage_telemetry"``)::

            since, until: int                    # the window used
            daily_cost: [{day: "YYYY-MM-DD", cost_usd: float}]
            models: [{model, input_tokens, output_tokens,
                      cache_read_input_tokens, cost_usd: float|null, sessions}]
            sessions: [{                         # every session in the trees
              id, parent_id|null, root_id, title, created_at, updated_at,
              archived, agent|null, harness|null, host|null, workspace|null,
              live_status|null ("idle"|"running"|"waiting"|"failed"),
              sub_agent_name|null, labels: {str: str},
              items, turns, compactions, inherited_items: int,
              tool_calls: {tool name: count},
              usage: {cost_usd|null, input_tokens, output_tokens,
                      cache_read_input_tokens, by_model: {model: {...as models}}},
              subscribes_to: [session id]
            }]
            scheduled_tasks: [{id, name, state, workspace|null, runs,
                               succeeded, failed, skipped, last_error|null}]
            sidebar: {unarchived, from_scheduled_tasks}
            truncated: [str]                     # sections a bound cut short

        ``usage`` is each session's own recorded usage, cumulative since it
        began; ``models`` adds those up over the returned sessions.
        ``turns`` counts user messages; ``inherited_items`` counts items older
        than the session (copied from a fork). A scheduled run counts as failed
        when it failed or its session's last status was ``failed``;
        ``from_scheduled_tasks`` counts unarchived top-level sessions labelled
        as serving a scheduled run.
        """
        user_id = require_user(request, auth_provider)
        end = until if until is not None else int(datetime.now(UTC).timestamp())
        start = since if since is not None else end - _DEFAULT_WINDOW_DAYS * _DAY
        if start > end:
            raise HTTPException(status_code=422, detail="since must not be after until")
        if end - start > _MAX_WINDOW_DAYS * _DAY:
            raise HTTPException(
                status_code=422, detail=f"window is at most {_MAX_WINDOW_DAYS} days"
            )
        return await asyncio.to_thread(
            _build_telemetry,
            conversation_store,
            scheduled_task_store,
            host_store,
            user_id,
            since=start,
            until=end,
            max_sessions=max_sessions,
        )

    return router
