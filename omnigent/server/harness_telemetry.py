"""The server's harness telemetry setting and the per-session decision it makes.

An admin sets ``harness_telemetry`` in the server config YAML (see
:mod:`omnigent.server.server_config`): an OTLP ``endpoint`` and ``exclude``
lists of ``users``, ``projects``, ``threads`` (root session ids, covering every
sub-session), ``repos`` (``host/owner/repo``, checked on the host against the
checkout's ``origin``) and ``labels`` (``key=value``). The OTLP headers carry an
ingest token, so they come from ``OMNIGENT_HARNESS_TELEMETRY_HEADERS`` in the
OTLP ``k=v,k2=v2`` format rather than the file.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from omnigent.server.server_config import config_str_list, load_server_config

if TYPE_CHECKING:
    import httpx

    from omnigent.entities.conversation import Conversation
    from omnigent.stores.conversation_store import ConversationStore

logger = logging.getLogger(__name__)

HARNESS_TELEMETRY_KEY = "harness_telemetry"
# Server-internal session label: ``on``/``off`` as the host applied the decision.
HARNESS_TELEMETRY_LABEL_KEY = "omnigent.harness_telemetry"
HARNESS_TELEMETRY_HEADERS_ENV_VAR = "OMNIGENT_HARNESS_TELEMETRY_HEADERS"


@dataclass(frozen=True)
class HarnessTelemetrySetting:
    """The admin's setting, parsed from the server config and environment."""

    endpoint: str
    headers: Mapping[str, str]
    users: frozenset[str]
    projects: frozenset[str]
    threads: frozenset[str]
    repos: tuple[str, ...]
    labels: frozenset[tuple[str, str]]


def _parse_otlp_headers(raw: str) -> dict[str, str]:
    """Parse OTLP ``k=v,k2=v2`` headers; values are URL-decoded as the OTLP spec says."""
    headers: dict[str, str] = {}
    for item in raw.split(","):
        key, sep, value = item.partition("=")
        if sep and key.strip():
            headers[key.strip()] = urllib.parse.unquote(value.strip())
    return headers


def harness_telemetry_setting(
    config: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> HarnessTelemetrySetting | None:
    """
    Return the server's harness telemetry setting, or ``None`` when unset.

    An invalid section (no endpoint, not a mapping) logs a warning and counts
    as unset, which leaves every host on its own telemetry settings.

    :param config: Loaded server config; ``None`` reads it now, so an edit
        applies from the next launch.
    :param environ: Environment holding the headers; ``None`` is ``os.environ``.
    :returns: The parsed setting, or ``None``.
    """
    raw = (load_server_config() if config is None else config).get(HARNESS_TELEMETRY_KEY)
    if raw is None:
        return None
    endpoint = raw.get("endpoint") if isinstance(raw, dict) else None
    if not isinstance(endpoint, str) or not endpoint.strip():
        logger.warning("server config %s needs an endpoint — ignoring it", HARNESS_TELEMETRY_KEY)
        return None
    exclude = raw.get("exclude")
    exclude = exclude if isinstance(exclude, dict) else {}
    labels: set[tuple[str, str]] = set()
    for item in config_str_list(exclude.get("labels")):
        key, sep, value = item.partition("=")
        if sep:
            labels.add((key.strip(), value.strip()))
        else:
            logger.warning("harness_telemetry label exclusion %r is not key=value", item)
    env = os.environ if environ is None else environ
    return HarnessTelemetrySetting(
        endpoint=endpoint.strip().rstrip("/"),
        headers=_parse_otlp_headers(env.get(HARNESS_TELEMETRY_HEADERS_ENV_VAR, "")),
        users=frozenset(config_str_list(exclude.get("users"))),
        projects=frozenset(config_str_list(exclude.get("projects"))),
        threads=frozenset(config_str_list(exclude.get("threads"))),
        repos=tuple(repo.lower().strip("/") for repo in config_str_list(exclude.get("repos"))),
        labels=frozenset(labels),
    )


def session_owner(conv: Conversation, conversation_store: ConversationStore) -> str | None:
    """The session's owner, or its root session's for a sub-session without its own grant."""
    owner = conversation_store.get_session_owner(conv.id)
    if owner is None and conv.root_conversation_id != conv.id:
        owner = conversation_store.get_session_owner(conv.root_conversation_id)
    return owner


def session_excluded(
    setting: HarnessTelemetrySetting, conv: Conversation, owner: str | None
) -> bool:
    """Whether the server-side facts exclude *conv*; the repository check is the host's."""
    labels = conv.labels or {}
    return (
        (owner is not None and owner in setting.users)
        or (conv.project_id is not None and conv.project_id in setting.projects)
        or conv.root_conversation_id in setting.threads
        or conv.id in setting.threads
        or any(labels.get(key) == value for key, value in setting.labels)
    )


def harness_telemetry_launch(
    conv: Conversation,
    capabilities: Collection[str],
    conversation_store: ConversationStore,
) -> dict[str, Any] | None:
    """
    The ``harness_telemetry`` field of a launch frame for *conv*.

    :param conv: The session being launched.
    :param capabilities: The target host's advertised capabilities.
    :param conversation_store: Store used to resolve the session's owner.
    :returns: ``None`` when the host predates the field (it then launches as
        today), else :func:`harness_telemetry_decision`.
    """
    from omnigent.host.frames import CAP_HARNESS_TELEMETRY

    if CAP_HARNESS_TELEMETRY not in capabilities:
        return None
    return harness_telemetry_decision(conv, conversation_store)


def harness_telemetry_decision(
    conv: Conversation, conversation_store: ConversationStore
) -> dict[str, Any] | None:
    """
    The server's harness telemetry decision for *conv*, as the host applies it.

    It rides both the runner launch frame (the runner-wide file) and every
    session-init envelope, which a sub-session co-located in its parent's
    runner applies to its own harness launch.

    :param conv: The session being launched or initialized.
    :param conversation_store: Store used to resolve the session's owner.
    :returns: ``None`` when no setting exists; ``{"enabled": False}`` for an
        excluded session; else the endpoint, headers, repository exclusions
        and the ``omnigent.*`` resource attributes.
    """
    setting = harness_telemetry_setting()
    if setting is None:
        return None
    if session_excluded(setting, conv, session_owner(conv, conversation_store)):
        return {"enabled": False}
    attributes = {
        "omnigent.session.id": conv.id,
        "omnigent.root_session.id": conv.root_conversation_id,
    }
    if conv.project_id:
        attributes["omnigent.project"] = conv.project_id
    return {
        "enabled": True,
        "endpoint": setting.endpoint,
        "headers": dict(setting.headers),
        "exclude_repos": list(setting.repos),
        "resource_attributes": attributes,
    }


def record_host_harness_telemetry(
    conv: Conversation, response: httpx.Response, conversation_store: ConversationStore
) -> None:
    """
    Save the host's on/off for *conv* from its runner's session-init *response*.

    :param conv: The session the runner initialized.
    :param response: The runner's reply, carrying ``harness_telemetry_enabled``
        when the session had a server decision.
    :param conversation_store: Store holding the session's labels.
    """
    try:
        payload = response.json()
    except ValueError:
        return
    enabled = payload.get("harness_telemetry_enabled") if isinstance(payload, dict) else None
    if not isinstance(enabled, bool):
        return
    value = "on" if enabled else "off"
    if (conv.labels or {}).get(HARNESS_TELEMETRY_LABEL_KEY) == value:
        return
    try:
        conversation_store.set_labels(conv.id, {HARNESS_TELEMETRY_LABEL_KEY: value})
    except Exception:
        logger.warning("could not save harness telemetry %s for %s", value, conv.id, exc_info=True)


def usage_metrics_excluded(
    setting: HarnessTelemetrySetting, conv: Conversation, owner: str | None
) -> bool:
    """
    Whether the server's usage metrics skip *conv*, by the same rule as its harness.

    The host decides the repository exclusions; until it has said ``on``
    while any exist, the session is skipped, as the host fails closed.
    """
    host = (conv.labels or {}).get(HARNESS_TELEMETRY_LABEL_KEY)
    return (
        session_excluded(setting, conv, owner)
        or host == "off"
        or (bool(setting.repos) and host != "on")
    )
