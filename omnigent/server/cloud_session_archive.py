"""Archive a ``claude-cloud`` session's claude.ai cloud session when its Omnigent session is archived.

The bridge (``integrations/claude-cloud-acp``) holds the claude.ai login and already
implements the call, so this runs its ``--archive -`` command on the server host with the
session's assistant text on stdin; the bridge reads the cloud session id from the last
``cloud session:`` line there. A failure is logged and never blocks the Omnigent archive.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from omnigent.entities.conversation import Conversation
from omnigent.harness_aliases import canonicalize_harness

_logger = logging.getLogger(__name__)

# The first line of the bridge's first answer; same text as its ``MARKER``.
CLOUD_SESSION_MARKER = "cloud session: https://claude.ai/code/"
_BRIDGE_PATH = (
    Path(__file__).resolve().parents[2] / "integrations" / "claude-cloud-acp" / "claude_cloud_acp.py"
)
_ARCHIVE_TIMEOUT_S = 60.0
_TRANSCRIPT_ITEM_LIMIT = 200


def _assistant_text(conversation_store: Any, session_id: str) -> str:
    """Join the text of the session's first assistant messages, where the marker line sits."""
    page = conversation_store.list_items(session_id, limit=_TRANSCRIPT_ITEM_LIMIT, type="message")
    texts: list[str] = []
    for item in page.data:
        data = item.data
        if getattr(data, "role", None) != "assistant":
            continue
        for block in data.content:
            text = block.get("text") if isinstance(block, dict) else None
            if isinstance(text, str):
                texts.append(text)
    return "\n".join(texts)


def archive_claude_cloud_session(
    conv: Conversation, conversation_store: Any, agent_store: Any = None
) -> bool:
    """
    Archive the claude.ai cloud session behind *conv*; ``False`` when there is none to archive.

    Any other harness makes no call. The bridge treats an already archived cloud session as
    done, and a failed call is logged, so the caller's own archive proceeds either way.

    :param conv: The archived Omnigent session.
    :param conversation_store: Store the session's items are read from.
    :param agent_store: Store for resolving the session's harness, or ``None`` for the global one.
    :returns: Whether the bridge archived a cloud session.
    """
    from omnigent.server.routes._sessions.helpers import _resolve_harness

    try:
        harness = _resolve_harness(conv, agent_store=agent_store)
        if harness is None or canonicalize_harness(harness) != "acp":
            return False
        transcript = _assistant_text(conversation_store, conv.id)
        if CLOUD_SESSION_MARKER not in transcript or not _BRIDGE_PATH.is_file():
            return False
        done = subprocess.run(
            [sys.executable, str(_BRIDGE_PATH), "--archive", "-"],
            input=transcript,
            capture_output=True,
            text=True,
            timeout=_ARCHIVE_TIMEOUT_S,
            check=False,
        )
    except Exception:  # noqa: BLE001 — a failed cloud archive must not undo the Omnigent archive
        _logger.warning(
            "Cloud session archive failed for %s", conv.id, exc_info=True, extra={"session_id": conv.id}
        )
        return False
    if done.returncode != 0:
        _logger.warning(
            "Cloud session archive failed for %s: %s",
            conv.id,
            done.stderr.strip(),
            extra={"session_id": conv.id},
        )
        return False
    return True


async def archive_claude_cloud_session_async(
    conv: Conversation, conversation_store: Any, agent_store: Any = None
) -> bool:
    """:func:`archive_claude_cloud_session` off the event loop."""
    return await asyncio.to_thread(
        archive_claude_cloud_session, conv, conversation_store, agent_store
    )
