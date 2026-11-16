"""Archive the claude.ai cloud session of a ``claude-cloud`` session when it is archived.

The bridge (``integrations/claude-cloud-acp``) holds the claude.ai login and already
implements the call, so this runs its ``--archive -`` command on the server host with the
session's assistant text on stdin; the bridge reads the cloud session id from the newest
``cloud session:`` line there. A failure is logged and never blocks the Omnigent archive.
"""

from __future__ import annotations

import asyncio
import logging
import re
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
    Path(__file__).resolve().parents[2]
    / "integrations"
    / "claude-cloud-acp"
    / "claude_cloud_acp.py"
)
_ARCHIVE_TIMEOUT_S = 60.0
_PAGE_SIZE = 200
# The bridge's own line: a whole line that starts an assistant message. A marker a
# message quotes (a transcript read aloud) is not at its start or has text after the id.
_MARKER_LINE_RE = re.compile(
    r"^" + re.escape(CLOUD_SESSION_MARKER) + r"(?:session|cse)_[A-Za-z0-9]{10,}[ \t]*$"
)
_login_warning_logged = False


def _bridge_marker(conversation_store: Any, session_id: str) -> str | None:
    """The newest marker line the bridge wrote in the session's assistant messages, if any."""
    newest: str | None = None
    after: str | None = None
    while True:
        page = conversation_store.list_items(
            session_id, limit=_PAGE_SIZE, after=after, type="message"
        )
        for item in page.data:
            data = item.data
            if getattr(data, "role", None) != "assistant":
                continue
            for block in data.content:
                text = block.get("text") if isinstance(block, dict) else None
                first_line = text.split("\n", 1)[0] if isinstance(text, str) else ""
                if _MARKER_LINE_RE.match(first_line):
                    newest = first_line.strip()
        if not page.has_more or not page.last_id:
            return newest
        after = page.last_id


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
        marker = _bridge_marker(conversation_store, conv.id)
        if marker is None or not _BRIDGE_PATH.is_file():
            return False
        done = subprocess.run(
            [sys.executable, str(_BRIDGE_PATH), "--archive", "-"],
            input=marker,
            capture_output=True,
            text=True,
            timeout=_ARCHIVE_TIMEOUT_S,
            check=False,
        )
    except Exception:
        _logger.warning(
            "Cloud session archive failed for %s",
            conv.id,
            exc_info=True,
            extra={"session_id": conv.id},
        )
        return False
    if done.returncode != 0:
        _warn_once_without_login(done.stderr)
        _logger.warning(
            "Cloud session archive failed for %s: %s",
            conv.id,
            done.stderr.strip(),
            extra={"session_id": conv.id},
        )
        return False
    return True


def _warn_once_without_login(stderr: str) -> None:
    """Say once that the server host lacks the claude.ai login the archive needs."""
    global _login_warning_logged
    if _login_warning_logged or "login" not in stderr.lower():
        return
    _login_warning_logged = True
    _logger.warning(
        "Cloud sessions are not archived: the server host holds no claude.ai login "
        "(run `claude` there and sign in)."
    )


async def archive_claude_cloud_session_async(
    conv: Conversation, conversation_store: Any, agent_store: Any = None
) -> bool:
    """:func:`archive_claude_cloud_session` off the event loop."""
    return await asyncio.to_thread(
        archive_claude_cloud_session, conv, conversation_store, agent_store
    )
