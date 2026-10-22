"""A one-line recap of where each session stands, written at turn end.

The recap rides the background-title generator: the runner already runs custom
title requirements on the harness's economy model, so recap requirements and an
excerpt of the latest turn make it a status line with no runner change.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from omnigent.entities import FunctionCallData, MessageData
from omnigent.harness_aliases import is_native_harness
from omnigent.server.background_session_titles import (
    BackgroundTitleGenerator,
    BackgroundTitleRequest,
    _background_session_title_harness_supported,
    normalize_background_title,
)

if TYPE_CHECKING:
    from omnigent.entities.conversation import ConversationItem
    from omnigent.stores.conversation_store import ConversationStore

_logger = logging.getLogger(__name__)

STATUS_LINE_KEY = "status.line"
STATUS_AT_KEY = "status.at"
#: Who wrote the current ``status.line``; a writer that doesn't name itself clears it.
STATUS_BY_KEY = "status.by"
RECAP_WRITER = "recap"
RECAP_MAX_CHARS = 120
RECAP_INSTRUCTIONS = (
    "This title is a one-line status recap shown beside an agent session in a "
    "sidebar, so its owner sees what the session is doing without opening it. "
    "The text inside <user_message> is an excerpt of the session's latest turn: "
    "the user's last message, the tools the agent called, and the agent's last "
    "reply. In at most 100 characters, say what the session did or is doing and "
    "where it stands now: done, waiting on the user, blocked, failed, or still "
    "working. Lead with that state or a verb. No preamble and no session id."
)
_EXCERPT_ITEMS = 200
_ASK_CHARS = 1000
# A long reply keeps its opening (the result, usually) and its end (what comes next).
_REPLY_HEAD_CHARS = 1200
_REPLY_TAIL_CHARS = 800
_TOOL_NAMES = 12


@dataclass
class _TurnEnds:
    """The turn ends a recap not yet started stands for."""

    context: contextvars.Context
    # Every edge was a native pane's relayed idle, which only means it went quiet.
    pane_quiet_only: bool
    failed: bool


class SessionRecapCoordinator:
    """Write ``status.line`` for a session after its turns end, one call per interval."""

    def __init__(
        self,
        conversation_store: ConversationStore,
        generator: BackgroundTitleGenerator,
        *,
        interval_seconds: float = 60.0,
        settle_seconds: float = 5.0,
        timeout_seconds: float = 70.0,
        max_concurrency: int = 2,
    ) -> None:
        self._store = conversation_store
        self._generator = generator
        self._interval = interval_seconds
        # Lets the turn's last items persist before the excerpt is read.
        self._settle = settle_seconds
        self._timeout = timeout_seconds
        self._slots = asyncio.Semaphore(max_concurrency)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._turn_ends: dict[str, _TurnEnds] = {}
        # ponytail: per-session entries live as long as the server; prune on archive if it matters.
        self._last_call: dict[str, float] = {}
        # Newest transcript item each session was last recapped at.
        self._recapped_item: dict[str, str] = {}

    def turn_ended(self, session_id: str, *, from_runner: bool, failed: bool = False) -> None:
        """Schedule a recap; one not yet started covers this turn end too."""
        context = contextvars.copy_context()
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            if self._loop is None or self._loop.is_closed():
                return
            self._loop.call_soon_threadsafe(
                self._schedule, session_id, from_runner, failed, context
            )
            return
        self._schedule(session_id, from_runner, failed, context)

    def _schedule(
        self, session_id: str, from_runner: bool, failed: bool, context: contextvars.Context
    ) -> None:
        pane_quiet = from_runner and not failed
        earlier = self._turn_ends.get(session_id)
        self._turn_ends[session_id] = _TurnEnds(
            context=context,
            pane_quiet_only=pane_quiet and (earlier is None or earlier.pane_quiet_only),
            failed=failed,
        )
        # A running recap took its turn ends already; this one starts when it is done.
        if session_id not in self._tasks:
            self._start(session_id)

    def _start(self, session_id: str) -> None:
        last = self._last_call.get(session_id)
        delay = (
            self._settle
            if last is None
            else max(self._settle, last + self._interval - time.monotonic())
        )
        task = asyncio.get_running_loop().create_task(
            self._run(session_id, delay),
            name=f"session-recap-{session_id}",
            context=self._turn_ends[session_id].context,
        )
        self._tasks[session_id] = task

        def _done(_task: asyncio.Task[None]) -> None:
            self._tasks.pop(session_id, None)
            if session_id in self._turn_ends and not _task.cancelled():
                self._start(session_id)

        task.add_done_callback(_done)

    async def _run(self, session_id: str, delay: float) -> None:
        started = time.perf_counter()
        try:
            await asyncio.sleep(delay)
            turn_ends = self._turn_ends.pop(session_id)
            prepared = await asyncio.to_thread(self._request, session_id, turn_ends)
            if prepared is None:
                return
            request, turn_started = prepared
            self._last_call[session_id] = time.monotonic()
            async with asyncio.timeout(self._timeout), self._slots:
                generated = await self._generator(request)
            line = normalize_background_title(
                generated, max_chars=RECAP_MAX_CHARS, truncate_overflow=True
            )
            written = line is not None and await asyncio.to_thread(
                self._write, session_id, line, turn_started
            )
            _logger.info(
                "session recap completed session=%s written=%s elapsed_ms=%.1f",
                session_id,
                written,
                (time.perf_counter() - started) * 1000,
                extra={"session_id": session_id},
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a recap is display metadata, never the turn's failure
            _logger.warning(
                "session recap failed session=%s",
                session_id,
                exc_info=True,
                extra={"session_id": session_id},
            )

    def _request(
        self, session_id: str, turn_ends: _TurnEnds
    ) -> tuple[BackgroundTitleRequest, int] | None:
        """The recap request for the latest turn and when it started, or ``None``."""
        from omnigent.server.routes._sessions.helpers import _session_status_cache

        conv = self._store.get_conversation(session_id)
        if (
            conv is None
            or conv.archived
            or conv.parent_conversation_id is not None
            or not _background_session_title_harness_supported(conv.harness_override)
            # A newer turn is under way; its own end brings the next recap.
            or _session_status_cache.get(session_id) == "running"
        ):
            return None
        if turn_ends.pane_quiet_only:
            from omnigent.server.routes._sessions.orchestration import _native_pane_harness

            # A native pane going quiet is not its turn end; its forwarder's edge is.
            if is_native_harness(_native_pane_harness(conv)):
                return None
        rows = self._store.list_items(session_id, limit=_EXCERPT_ITEMS, order="desc").data
        # A repeated idle edge with nothing new since the last recap.
        if not rows or self._recapped_item.get(session_id) == rows[0].id:
            return None
        prompt, turn_started = turn_excerpt(rows, failed=turn_ends.failed)
        if not prompt or _written_by_another_since(conv.labels, turn_started):
            return None
        self._recapped_item[session_id] = rows[0].id
        request = BackgroundTitleRequest(
            session_id=session_id,
            prompt=prompt,
            agent_id=conv.agent_id,
            harness_override=conv.harness_override,
            model_override=conv.model_override,
            sub_agent_name=conv.sub_agent_name,
            additional_instructions=RECAP_INSTRUCTIONS,
        )
        return request, turn_started

    def _write(self, session_id: str, line: str, turn_started: int) -> bool:
        """Write the recap unless another writer set a line while it was generated."""
        conv = self._store.get_conversation(session_id)
        # ponytail: a write landing between this read and the upsert is replaced once;
        # the supervisor rewrites its line within a minute.
        if conv is None or _written_by_another_since(conv.labels, turn_started):
            return False
        self._store.set_labels(
            session_id,
            {
                STATUS_LINE_KEY: line,
                STATUS_AT_KEY: str(int(time.time())),
                STATUS_BY_KEY: RECAP_WRITER,
            },
        )
        return True

    async def wait_for_idle(self) -> None:
        """Wait for the pending recaps; used by focused tests."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks.values()), return_exceptions=True)

    async def shutdown(self) -> None:
        """Cancel and drain the pending recaps during server shutdown."""
        self._turn_ends.clear()
        pending = tuple(self._tasks.values())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def _written_by_another_since(labels: dict[str, str], turn_started: int) -> bool:
    """Whether a writer other than the recap set ``status.line`` since the turn started."""
    if not labels.get(STATUS_LINE_KEY, "").strip() or labels.get(STATUS_BY_KEY) == RECAP_WRITER:
        return False
    try:
        written_at = float(labels.get(STATUS_AT_KEY) or 0)
    except ValueError:
        return False
    return written_at >= turn_started


def turn_excerpt(
    rows_newest_first: list[ConversationItem], *, failed: bool = False
) -> tuple[str, int]:
    """The latest turn as recap input, and when it started.

    The start is its user message's time, or 0 when the window doesn't reach it,
    so another writer's line wins over a turn whose start is unknown.
    """
    ask = reply = ""
    tools: list[str] = []
    started = 0
    for row in rows_newest_first:
        data = row.data
        if isinstance(data, FunctionCallData):
            if data.name not in tools:
                tools.append(data.name)
        elif isinstance(data, MessageData) and not data.is_meta:
            text = _text(data.content)
            if data.role == "user" and text:
                ask, started = text, row.created_at
                break
            if data.role == "assistant" and text and not reply:
                reply = text
    if not ask and not reply:
        return "", started
    parts = [f"User: {ask[:_ASK_CHARS]}"] if ask else []
    if tools:
        parts.append(f"Tools: {', '.join(reversed(tools[:_TOOL_NAMES]))}")
    if len(reply) > _REPLY_HEAD_CHARS + _REPLY_TAIL_CHARS:
        reply = f"{reply[:_REPLY_HEAD_CHARS]} … {reply[-_REPLY_TAIL_CHARS:]}"
    if reply:
        parts.append(f"Agent: {reply}")
    if failed:
        parts.append("Outcome: the turn failed before it finished.")
    return "\n".join(parts), started


def _text(content: list[dict[str, Any]]) -> str:
    return "\n".join(
        block["text"] for block in content if isinstance(block.get("text"), str)
    ).strip()


_coordinator: SessionRecapCoordinator | None = None


def configure(coordinator: SessionRecapCoordinator | None) -> None:
    """Install the server's recap coordinator; ``None`` turns recaps off."""
    global _coordinator
    _coordinator = coordinator


def turn_ended(session_id: str, *, from_runner: bool, failed: bool = False) -> None:
    """Hand a session's terminal status edge to the recap. Never raises."""
    coordinator = _coordinator
    if coordinator is None:
        return
    try:
        coordinator.turn_ended(session_id, from_runner=from_runner, failed=failed)
    except Exception:  # noqa: BLE001 - the status edge must publish whatever happens here
        _logger.warning("session recap not scheduled session=%s", session_id, exc_info=True)
