"""Capability tags: what a host or a cloud sandbox kind has.

A capability is a lowercase namespaced tag such as ``os:macos``, ``tool:xcode``,
``device:iphone-15`` or ``login:browser``. A host qualifies for a session start
when the start's ``needs`` are a subset of the host's capabilities. Tags carry no
versions and no values.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from pathlib import Path

import yaml

# ``namespace:name``, both lowercase; the name may hold ``. _ -``.
_TAG_RE = re.compile(r"^[a-z][a-z0-9_-]*:[a-z0-9][a-z0-9._-]*$")
_MAX_TAG_LEN = 64
_MAX_TAGS = 64

#: What a cloud sandbox kind declares, keyed by kind name. Reported in
#: ``GET /v1/info`` as ``cloud_kind_capabilities``.
CLOUD_KIND_CAPABILITIES: dict[str, list[str]] = {
    "claude-cloud": ["os:linux"],
    "codex-cloud": ["os:linux"],
}


def is_capability_tag(value: object) -> bool:
    """Whether *value* is a well-formed capability tag, e.g. ``"tool:xcode"``."""
    return isinstance(value, str) and len(value) <= _MAX_TAG_LEN and bool(_TAG_RE.match(value))


def normalize_capability_tags(tags: object) -> list[str]:
    """Return the sorted, de-duplicated well-formed tags of *tags*.

    Malformed entries (not a string, wrong shape) are dropped, so an older or
    sloppy host cannot break the listing.
    """
    if not isinstance(tags, (list, tuple, set, frozenset)):
        return []
    found = {t for t in tags if is_capability_tag(t)}
    return sorted(found)[:_MAX_TAGS]


def detected_os_tag() -> str:
    """The ``os:`` tag of the machine running this process."""
    if sys.platform == "darwin":
        return "os:macos"
    if sys.platform.startswith("win"):
        return "os:windows"
    return "os:linux"


def load_host_capability_tags(path: Path) -> list[str]:
    """Read ``host.capabilities`` from the host's ``config.yaml``.

    :param path: The host's config file.
    :returns: The configured tags plus the detected ``os:`` tag, sorted. Only the
        detected ``os:`` tag when the file or key is missing.
    :raises ValueError: If a configured tag is malformed or the value is no list.
    """
    configured: list[object] = []
    if path.exists():
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
        section = cfg.get("host") if isinstance(cfg, dict) else None
        if isinstance(section, dict) and "capabilities" in section:
            value = section["capabilities"]
            if not isinstance(value, list):
                raise ValueError(f"host.capabilities in {path} must be a list, got {value!r}.")
            configured = value
    bad = [t for t in configured if not is_capability_tag(t)]
    if bad:
        raise ValueError(
            f"host.capabilities in {path} must be lowercase namespaced tags such as "
            f"'tool:xcode'; got {bad!r}."
        )
    return normalize_capability_tags([*configured, detected_os_tag()])


def missing_needs(needs: Iterable[str], capabilities: Iterable[str] | None) -> list[str]:
    """The needs that *capabilities* lacks, in sorted order; empty when it qualifies."""
    have = set(capabilities or ())
    return sorted(set(needs) - have)
