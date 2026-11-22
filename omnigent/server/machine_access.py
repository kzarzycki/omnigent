"""What the client-credentials machine principal may start, on whose hosts.

The machine principal (``OMNIGENT_MACHINE_SUB``) is a separate non-admin user,
so it owns no host and every host-bound start answers "not your host". An
operator opts it in with three settings, read once at mount:

- ``OMNIGENT_MACHINE_ACTS_FOR`` — the user whose hosts it may start sessions on.
- ``OMNIGENT_MACHINE_ALLOWED_AGENTS`` — comma-separated names of operator
  registered agents it may start.
- ``OMNIGENT_MACHINE_ALLOW_CLOUD_SESSIONS`` — also allow operator registered
  agents on the ``acp:claude-cloud`` harness.

Unset ``OMNIGENT_MACHINE_ACTS_FOR`` is the default and changes nothing. The
grant is narrow on purpose: only the session-create and runner-launch routes
accept it, only for the named user's hosts, and a start that would open a
terminal on a host in a bypass permission mode is refused whatever the agent.
See ``designs/CLIENT_CREDENTIALS.md``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn

from omnigent.server.auth import RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC, env_var_is_truthy

if TYPE_CHECKING:
    from omnigent.entities import Agent, Conversation
    from omnigent.spec import AgentSpec

_logger = logging.getLogger(__name__)

ACTS_FOR_ENV = "OMNIGENT_MACHINE_ACTS_FOR"
ALLOWED_AGENTS_ENV = "OMNIGENT_MACHINE_ALLOWED_AGENTS"
ALLOW_CLOUD_SESSIONS_ENV = "OMNIGENT_MACHINE_ALLOW_CLOUD_SESSIONS"

# Harness of the bridge that runs an agent in a Claude cloud session.
CLOUD_SESSION_HARNESS = "acp:claude-cloud"

# Fields a machine principal may set on a session create. Anything else (labels,
# launch args, permission mode, parent / sibling / project placement, harness
# override, managed sandboxes ...) is refused rather than reasoned about.
MACHINE_CREATE_FIELDS = frozenset(
    {
        "agent_id",
        "title",
        "host_id",
        "workspace",
        "model_override",
        "reasoning_effort",
    }
)

# Terminal launch flags (and ``--permission-mode`` values) that skip approval.
_BYPASS_FLAGS = frozenset(
    {
        "--dangerously-skip-permissions",
        "--dangerously-bypass-approvals-and-sandbox",
        "--yolo",
    }
)
_BYPASS_MODE_VALUES = frozenset({"bypassPermissions", "dangerous", "danger-full-access"})


def launch_args_bypass_permissions(args: Iterable[str] | None) -> bool:
    """Whether native-terminal launch args skip the approval prompt.

    :param args: Pass-through CLI args, e.g. ``["--permission-mode", "plan"]``.
    :returns: ``True`` for a known bypass flag, or a bypass value after
        ``--permission-mode`` / ``--sandbox`` (also in ``--flag=value`` form).
    """
    from omnigent.harnesses.codex_native.app_server import codex_launch_args_grant_full_access

    args = list(args or ())
    if codex_launch_args_grant_full_access(args):
        return True
    previous_flag = ""
    for arg in args:
        flag, _, inline_value = arg.partition("=")
        if flag in _BYPASS_FLAGS:
            return True
        if inline_value and flag in ("--permission-mode", "--sandbox"):
            if inline_value in _BYPASS_MODE_VALUES:
                return True
        elif previous_flag in ("--permission-mode", "--sandbox") and arg in _BYPASS_MODE_VALUES:
            return True
        previous_flag = arg
    return False


def conversation_bypasses_permissions(conv: Conversation) -> bool:
    """Whether a stored session starts its terminal without approval prompts.

    Reads both places a bypass is recorded: the launch args, and the codex-native
    full-bypass label (which maps to empty args).

    :param conv: The stored session row.
    :returns: ``True`` when either records a bypass.
    """
    from omnigent.stores.conversation_store import CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY

    if launch_args_bypass_permissions(conv.terminal_launch_args):
        return True
    return bool((conv.labels or {}).get(CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY))


_PLAIN_BLOCK_TYPES = frozenset({"input_text", "input_image", "input_file"})


def native_input_runs_on_host(content: object) -> bool:
    """Whether native-terminal input could run as a host command for the machine principal.

    Decides on what the harness receives: anything but plain text or a list of
    ``input_text`` / attachment blocks is refused (other shapes are read
    differently by each harness), and a ``!`` or ``/`` that leads the joined
    text, which a native TUI runs.

    :param content: A message's ``content``.
    :returns: ``True`` when the machine principal must not send it.
    """
    if isinstance(content, list):
        texts: list[str] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") not in _PLAIN_BLOCK_TYPES:
                return True
            if block["type"] == "input_text":
                text = block.get("text")
                if not isinstance(text, str):
                    return True
                texts.append(text)
        content = "\n".join(texts)
    if not isinstance(content, str):
        return True
    return content.lstrip()[:1] in ("!", "/")


def spec_harness_id(spec: AgentSpec) -> str:
    """Return the harness the spec names, keeping an ``acp:<slug>`` suffix.

    The canonical harness id drops that suffix, which is what tells a cloud
    session from any other ACP agent.

    :param spec: A parsed agent spec.
    :returns: e.g. ``"acp:claude-cloud"`` or ``"claude-native"``.
    """
    return str(spec.executor.config.get("harness") or spec.executor.type)


def spec_declares_bypass(spec: AgentSpec) -> bool:
    """Whether an agent spec's own settings start its terminal / harness in bypass.

    Checks the launch args the create route would derive from the spec, plus the
    spec's ``permission_mode`` / ``yolo`` settings read by non-terminal harnesses.

    :param spec: A parsed, server-loaded agent spec.
    :returns: ``True`` when the spec asks to skip approval.
    """
    from omnigent.server.routes._sessions.helpers import _derive_terminal_launch_args_from_spec

    try:
        derived_args = _derive_terminal_launch_args_from_spec(spec, headless_defaults=False)
    except ValueError:
        # An unusable spec can't be shown safe, so treat it as bypass.
        return True
    if launch_args_bypass_permissions(derived_args):
        return True
    config: dict[str, Any] = spec.executor.config
    if config.get("permission_mode") in _BYPASS_MODE_VALUES:
        return True
    yolo = config.get("yolo")
    return yolo is True or (isinstance(yolo, str) and yolo.lower() == "true")


@dataclass(frozen=True)
class MachineSessionAccess:
    """The operator's grant to the machine principal.

    :param machine_sub: The machine principal the grant applies to.
    :param acts_for: The user whose hosts the principal may start sessions on.
    :param allowed_agents: Names of operator registered agents it may start.
    :param allow_cloud_sessions: Whether operator registered agents on
        :data:`CLOUD_SESSION_HARNESS` may be started too.
    """

    machine_sub: str
    acts_for: str
    allowed_agents: frozenset[str]
    allow_cloud_sessions: bool

    @staticmethod
    def from_env(machine_sub: str) -> MachineSessionAccess | None:
        """Read the grant from the environment, or ``None`` when it is off.

        :param machine_sub: The configured machine principal.
        :returns: The grant, or ``None`` when ``OMNIGENT_MACHINE_ACTS_FOR`` is
            unset (today's behaviour).
        :raises RuntimeError: When the agent settings are set without a user to
            act for, the user is a reserved identity or the machine principal
            itself, or the grant names nothing to start.
        """
        acts_for = os.environ.get(ACTS_FOR_ENV, "").strip()
        raw_agents = os.environ.get(ALLOWED_AGENTS_ENV, "")
        allowed_agents = frozenset(name.strip() for name in raw_agents.split(",") if name.strip())
        allow_cloud = env_var_is_truthy(ALLOW_CLOUD_SESSIONS_ENV, default=False)
        if not acts_for:
            if allowed_agents or allow_cloud:
                raise RuntimeError(
                    f"client-credentials: {ALLOWED_AGENTS_ENV} and {ALLOW_CLOUD_SESSIONS_ENV} "
                    f"need {ACTS_FOR_ENV}; unset them or name the user"
                )
            return None
        if acts_for in (RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC) or acts_for == machine_sub:
            raise RuntimeError(
                f"client-credentials: {ACTS_FOR_ENV}={acts_for!r} must be a real user other "
                "than the machine principal"
            )
        if not allowed_agents and not allow_cloud:
            raise RuntimeError(
                f"client-credentials: {ACTS_FOR_ENV} is set but neither {ALLOWED_AGENTS_ENV} "
                f"nor {ALLOW_CLOUD_SESSIONS_ENV} names anything the machine principal may start"
            )
        return MachineSessionAccess(
            machine_sub=machine_sub,
            acts_for=acts_for,
            allowed_agents=allowed_agents,
            allow_cloud_sessions=allow_cloud,
        )

    def applies_to(self, user_id: str | None) -> bool:
        """Whether *user_id* is the machine principal this grant is for."""
        return user_id is not None and user_id == self.machine_sub

    def may_use_host(self, user_id: str | None, host_owner: str | None) -> bool:
        """Whether the machine principal may use a host owned by *host_owner*."""
        return self.applies_to(user_id) and host_owner == self.acts_for

    def agent_permitted(self, agent: Agent, harness: str | None) -> bool:
        """Whether the machine principal may start a session of *agent*.

        Only operator registered agents count, so an agent the principal
        uploads can never take a listed name.

        :param agent: The agent row the session would bind to.
        :param harness: The canonical harness the session would run on.
        :returns: ``True`` when the agent is listed, or is a cloud-session
            agent and cloud sessions are allowed.
        """
        if not agent.operator_authored:
            return False
        if agent.name in self.allowed_agents:
            return True
        return self.allow_cloud_sessions and harness == CLOUD_SESSION_HARNESS


def refuse_machine_request(reason: str) -> NoReturn:
    """Raise the 403 every machine-grant refusal uses.

    :param reason: What was refused, without naming other users' hosts.
    :raises OmnigentError: Always, with ``FORBIDDEN``.
    """
    from omnigent.errors import ErrorCode, OmnigentError

    raise OmnigentError(f"machine client may not {reason}", code=ErrorCode.FORBIDDEN)
