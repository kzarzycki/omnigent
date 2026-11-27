"""Where a native Codex shell reads which Omnigent session it is in right now.

The app-server's env is fixed at launch, so after a native ``/clear`` rotation the
shell's ``OMNIGENT_SESSION_ID`` still names the old session. The launch env also names
this file (``OMNIGENT_SESSION_IDENTITY_FILE``, which never changes); the forwarder
rewrites its content on every rotation, e.g.
``jq -r .session_id "$OMNIGENT_SESSION_IDENTITY_FILE"``.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

IDENTITY_FILE_ENV_VAR = "OMNIGENT_SESSION_IDENTITY_FILE"
_IDENTITY_FILE = "session-identity.json"


def identity_file_for_bridge_dir(bridge_dir: Path) -> Path:
    """Return the identity file path for a bridge directory."""
    return bridge_dir / _IDENTITY_FILE


def write_shell_identity(bridge_dir: Path, *, session_id: str, primary_session_id: str) -> None:
    """
    Atomically write the identity a shell should see.

    :param bridge_dir: Native Codex bridge directory.
    :param session_id: Current Omnigent session id, e.g. ``"conv_new"``.
    :param primary_session_id: Session a new sub-agent files under.
    """
    bridge_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"session_id": session_id, "primary_session_id": primary_session_id})
    fd, tmp = tempfile.mkstemp(dir=bridge_dir, prefix=".identity-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, identity_file_for_bridge_dir(bridge_dir))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_shell_identity(bridge_dir: Path) -> dict[str, str] | None:
    """Return the identity file's content, or ``None`` when absent or malformed."""
    try:
        raw = json.loads(identity_file_for_bridge_dir(bridge_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    session_id, primary = raw.get("session_id"), raw.get("primary_session_id")
    if not isinstance(session_id, str) or not isinstance(primary, str):
        return None
    return {"session_id": session_id, "primary_session_id": primary}


def rotate_shell_identity(bridge_dir: Path, *, old_session_id: str, new_session_id: str) -> None:
    """
    Point the shell identity at a replacement session.

    A top-level session is its own primary, so its rotated session is too; a
    sub-agent's primary stays its parent. Does nothing for a launch that wrote
    no identity file.

    :param bridge_dir: Native Codex bridge directory.
    :param old_session_id: Session rotated away from.
    :param new_session_id: Replacement session.
    """
    current = read_shell_identity(bridge_dir)
    if current is None:
        return
    primary = current["primary_session_id"]
    if primary == old_session_id:
        primary = new_session_id
    write_shell_identity(bridge_dir, session_id=new_session_id, primary_session_id=primary)
