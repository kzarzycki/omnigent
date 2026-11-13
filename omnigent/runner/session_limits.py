"""Apply ``os_env.sandbox.resources`` to this runner's session.

The runner moves itself, and any children it already has, into a per-session
user-systemd slice before the session spawns its harness or tmux server, so
everything the session starts inherits the limits. The slice nests under the
host's session slice when the host runs one. Sub-agents run inside their
parent's runner and share its limits.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass

import psutil

from omnigent._platform import IS_LINUX
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.session_slice import DEFAULT_SESSION_SLICE_NAME, SESSION_SLICE_ENV_VAR
from omnigent.inner.datamodel import SandboxResourcesSpec
from omnigent.util.user_systemd import (
    UserSystemdError,
    child_slice_name,
    missing_user_manager_controllers,
    move_into_new_slice,
)

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _AppliedLimits:
    session_id: str
    properties: dict[str, str]


_lock = threading.Lock()
# The limits this runner process already carries, and the session they came from.
_applied: _AppliedLimits | None = None
# Sub-agent sessions served by this runner; they share their parent's limits.
_child_sessions: set[str] = set()


def note_child_session(session_id: str) -> None:
    """Record that *session_id* is a sub-agent sharing its parent's runner."""
    with _lock:
        _child_sessions.add(session_id)


def runner_has_session_slice() -> bool:
    """Whether this runner already moved into a session's limited slice."""
    return _applied is not None


def apply_spec_session_limits(session_id: str, spec: object) -> None:
    """Apply the limits in *spec*'s ``os_env.sandbox.resources``, if any.

    Call before anything that starts a process for *session_id*.
    """
    os_env = getattr(spec, "os_env", None)
    sandbox = getattr(os_env, "sandbox", None)
    apply_session_limits(session_id, getattr(sandbox, "resources", None))


def apply_session_limits(session_id: str, resources: SandboxResourcesSpec | None) -> None:
    """Put this runner in *session_id*'s limited slice, once per process.

    A later top-level session on the same runner (``/switch``, ``/clear``) runs
    under the limits already applied; one that declares different limits is
    refused, since a runner carries one budget.

    :param session_id: The session about to spawn processes.
    :param resources: Its ``os_env.sandbox.resources``; ``None`` sets no limit.
    :raises OmnigentError: If the limits cannot be applied on this host, or
        conflict with the ones this runner carries. They are never skipped silently.
    """
    global _applied
    properties = resources.systemd_properties() if resources is not None else {}
    with _lock:
        if session_id in _child_sessions:
            return
        if _applied is not None:
            if not properties or properties == _applied.properties:
                return
            raise OmnigentError(
                f"os_env.sandbox.resources of session {session_id} ({_describe(properties)}) "
                f"differ from the limits this runner already enforces for session "
                f"{_applied.session_id} ({_describe(_applied.properties)}). "
                "Fix: start the session on a new runner.",
                code=ErrorCode.CONFLICT,
            )
        if not properties:
            return
        if not IS_LINUX:
            raise _unavailable("they need Linux with a user systemd manager")
        needed = ["cpu"] if "CPUWeight" in properties else []
        if any(name.startswith("Memory") for name in properties):
            needed.append("memory")
        missing = missing_user_manager_controllers(needed)
        if missing is None:
            raise _unavailable("no user systemd manager is reachable")
        if missing:
            raise _unavailable(
                f"the user systemd manager does not delegate the {', '.join(missing)} "
                "cgroup controller"
            )
        parent = os.environ.get(SESSION_SLICE_ENV_VAR) or DEFAULT_SESSION_SLICE_NAME
        # The pid keeps a restarted runner's units apart from the ones its
        # predecessor's panes may still hold.
        slice_name = child_slice_name(parent, f"{session_id}p{os.getpid()}")
        # Children started before the limits (MCP servers, prewarm probes) move too.
        pids = [os.getpid(), *(child.pid for child in psutil.Process().children(recursive=True))]
        try:
            move_into_new_slice(
                pids,
                scope_name=f"{slice_name.removesuffix('.slice')}-runner.scope",
                slice_name=slice_name,
                slice_properties=properties,
                description=f"Omnigent session {session_id}",
            )
        except UserSystemdError as exc:
            raise _unavailable(f"the user systemd manager refused them ({exc})") from exc
        _applied = _AppliedLimits(session_id=session_id, properties=properties)
    _logger.info(
        "Session processes run in %s (%s)",
        slice_name,
        _describe(properties),
        extra={"session_id": session_id},
    )


def _describe(properties: dict[str, str]) -> str:
    return ", ".join(f"{name}={value}" for name, value in properties.items())


def _unavailable(reason: str) -> OmnigentError:
    return OmnigentError(
        f"os_env.sandbox.resources cannot be applied on this host: {reason}. "
        "Fix: run the agent on a Linux host with a user systemd manager, "
        "or remove os_env.sandbox.resources.",
        code=ErrorCode.INVALID_INPUT,
    )
