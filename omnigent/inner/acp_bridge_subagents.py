"""Omnigent's own ACP sub-agent dialect, for bridges Omnigent ships.

ACP has no standard sub-agent frames (see :mod:`omnigent.inner.acp_subagents`),
so a bridge that wraps an agent with sub-agents (the Claude cloud bridge) tags
the frames it already sends with ``_meta["omnigent/subagent"]``:

- on the ``tool_call`` that starts one: ``{"event": "start", "id", "title", "task"}``
- on a ``tool_call`` the sub-agent ran: ``{"event": "tool", "id", "callId", "name", "args"}``
- on the ``tool_call_update`` that ends it: ``{"event": "end", "id", "ok", "summary"}``

``id`` is the sub-agent's stable key. An agent that never sets the key is read
exactly as before.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from omnigent.inner.acp_subagents import (
    SubAgentActivity,
    SubAgentEnd,
    SubAgentEvent,
    SubAgentStart,
)

META_KEY = "omnigent/subagent"


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


class BridgeSubAgentSource:
    """Reads the ``omnigent/subagent`` dialect off one ``session/update``."""

    def read(self, update: Mapping[str, Any]) -> Sequence[SubAgentEvent]:
        """Return the sub-agent event the frame carries, if any.

        :param update: The ACP ``params.update`` object.
        :returns: One event for a tagged frame, else none.
        """
        meta = update.get("_meta")
        tag = meta.get(META_KEY) if isinstance(meta, Mapping) else None
        if not isinstance(tag, Mapping):
            return ()
        key = tag.get("id")
        if not isinstance(key, str) or not key:
            return ()
        event = tag.get("event")
        if event == "start":
            return (
                SubAgentStart(
                    child_key=key,
                    title=_text(tag.get("title")) or key,
                    task=_text(tag.get("task")),
                ),
            )
        if event == "tool":
            call_id, args = tag.get("callId"), tag.get("args")
            if not isinstance(call_id, str) or not call_id:
                return ()
            return (
                SubAgentActivity(
                    child_key=key,
                    call_id=call_id,
                    name=_text(tag.get("name")) or "tool",
                    args=args if isinstance(args, Mapping) else {},
                ),
            )
        if event == "end":
            return (
                SubAgentEnd(
                    child_key=key, ok=tag.get("ok") is not False, summary=_text(tag.get("summary"))
                ),
            )
        return ()
