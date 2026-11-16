"""A user's default harness and model for a session whose agent names none."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from omnigent.configuration_profile import PortableConfiguration
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.preference_requests import PreferencePolicy
from omnigent.spec.types import AgentSpec
from omnigent.stores.preference_store import PreferenceStore

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionDefaults:
    """
    The harness and model a user picked as defaults.

    :param harness: Default harness id, e.g. ``"claude-native"``; ``None`` when unset.
    :param model: Default model id, e.g. ``"claude-opus-4-6"``; ``None`` when unset.
    """

    harness: str | None = None
    model: str | None = None


def spec_names_harness(spec: AgentSpec) -> bool:
    """
    Return whether an ``executor.type: omnigent`` spec leaves its harness to the session.

    :param spec: A parsed agent or sub-agent spec.
    :returns: ``True`` when the spec runs on a harness but names none.
    """
    from omnigent.spec._omnigent_compat import OMNIGENT_EXECUTOR_TYPE

    return spec.executor.type != OMNIGENT_EXECUTOR_TYPE or bool(
        spec.executor.config.get("harness")
    )


def _configured_defaults(configuration: PortableConfiguration | None) -> SessionDefaults:
    if configuration is None:
        return SessionDefaults()
    return SessionDefaults(harness=configuration.default_harness, model=configuration.model)


def read_session_defaults(
    *,
    user_id: str | None,
    preference_store: PreferenceStore | None,
    preference_policy: PreferencePolicy | None,
) -> SessionDefaults:
    """
    Read a user's default harness and model.

    The configuration a user saved to the server wins. A single-user server
    (``user_id`` ``None`` or ``"local"``) runs as its user, so its own user config file
    (``harness.default`` and ``model``) is that user's config.

    :param user_id: Authenticated user id, or ``None`` in single-user mode.
    :param preference_store: The server's preference store, if configured.
    :param preference_policy: Gate for the preference store, if configured.
    :returns: The defaults found; fields stay ``None`` when nothing is set.
    """
    if preference_store is not None and (
        preference_policy is None or preference_policy.is_enabled()
    ):
        try:
            saved = preference_store.read_configuration(user_id=user_id)
        except OmnigentError:
            _logger.warning("session defaults: saved configuration unreadable", exc_info=True)
        else:
            defaults = _configured_defaults(saved.configuration)
            if defaults.harness is not None:
                return defaults
    if user_id not in (None, RESERVED_USER_LOCAL):
        return SessionDefaults()
    from omnigent.config import load_global_config

    try:
        return _configured_defaults(PortableConfiguration.from_config(load_global_config()))
    except (OSError, ValueError):
        _logger.warning("session defaults: user config unreadable", exc_info=True)
        return SessionDefaults()


def require_default_harness(defaults: SessionDefaults, agent_name: str) -> str:
    """
    Return the default harness, or refuse a session nothing can place.

    :param defaults: The user's defaults from :func:`read_session_defaults`.
    :param agent_name: The agent being started, e.g. ``"reviewer"``.
    :returns: The default harness id.
    :raises OmnigentError: ``invalid_input`` when no default harness is set.
    """
    if defaults.harness is None:
        raise OmnigentError(
            f"agent {agent_name!r} names no harness: pass harness_override, or set "
            "a default harness (`harness: <name>` in your Omnigent config, saved "
            "to this server with `omnigent config cloud save`)",
            code=ErrorCode.INVALID_INPUT,
        )
    return defaults.harness
