"""Read Omnigent's user and project configuration."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias

import yaml

from omnigent.preferences import (
    PreferenceContext,
    PreferenceKey,
    PreferenceKind,
    PreferenceLayer,
    PreferenceScope,
    ResolvedPreference,
    preferences_from_config,
    resolve_preferences,
)

_log = logging.getLogger(__name__)
_cwd_missing_warned = False

_Config: TypeAlias = dict[str, object]

_CONFIG_HOME_ENV_VAR = "OMNIGENT_CONFIG_HOME"
_GLOBAL_CONFIG_PATH = Path.home() / ".omnigent" / "config.yaml"
_LOCAL_CONFIG_RELPATH = Path(".omnigent") / "config.yaml"


def global_config_path(default_path: Path | None = None) -> Path:
    """Return the effective user-level config path."""
    if config_home := os.environ.get(_CONFIG_HOME_ENV_VAR):
        return Path(config_home) / "config.yaml"
    return default_path or _GLOBAL_CONFIG_PATH


def _origin_repo(workspace: Path) -> str | None:
    """``host/owner/repo`` of the git checkout holding *workspace*, from its ``origin`` remote."""
    try:
        url = subprocess.run(
            ["git", "-C", str(workspace), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    # https://host[:port]/o/r(.git), ssh://user@host[:port]/o/r, user@host:o/r
    match = re.fullmatch(
        r"(?:[a-z+]+://)?(?:[^@/]+@)?([^/:]+)(?::\d+)?[:/](.+?)(?:\.git)?/?", url, re.IGNORECASE
    )
    return f"{match.group(1)}/{match.group(2)}".lower() if match else None


def telemetry_endpoint_for(workspace: Path) -> str | None:
    """
    The OTLP endpoint a session in *workspace* opts into, from the machine's allowlist.

    ``telemetry-allowlist.json`` beside the user-level config (dotagents writes
    it on a machine that keeps telemetry off by default; ``telemetry.json`` there
    is Omnigent's own installation id) names an ``endpoint`` and the
    ``repos`` allowed to export to it, as ``host/owner/repo`` matched against the
    checkout's ``origin``, so every worktree and every clone whose origin is the
    listed remote is covered, and nothing is committed to the repo. A clone of a
    local checkout (a path as origin) matches nothing, which fails safe. Without
    the file, harness telemetry is whatever the user's own settings say.

    :param workspace: Session workspace, e.g. a worktree of a listed repo.
    :returns: The endpoint base URL, or ``None`` when the workspace is not opted in.
    """
    path = global_config_path().parent / "telemetry-allowlist.json"
    try:
        allow = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        _log.warning("telemetry allowlist %s unreadable; no session opts in: %s", path, exc)
        return None
    endpoint = allow.get("endpoint") if isinstance(allow, dict) else None
    repos = allow.get("repos") if isinstance(allow, dict) else None
    if not isinstance(endpoint, str) or not endpoint.strip() or not isinstance(repos, list):
        return None
    repo = _origin_repo(workspace)
    listed = {r.lower().strip("/") for r in repos if isinstance(r, str)}
    return endpoint.strip().rstrip("/") if repo in listed else None


HARNESS_TELEMETRY_ENV_VAR = "OMNIGENT_HARNESS_TELEMETRY"


@dataclass(frozen=True)
class HarnessTelemetry:
    """
    What a harness launch does about its own OTLP telemetry.

    :param enabled: ``False`` forces telemetry off over the user's settings.
    :param endpoint: OTLP/HTTP base URL when enabled, e.g. ``"https://otlp.example.test"``.
    :param headers: OTLP request headers, e.g. an ingest token.
    :param resource_attributes: OTel resource attributes, e.g. ``omnigent.session.id``.
    :param from_server: The server decided, so the harness gets complete exporter
        settings; the machine allowlist relies on the user's for the rest.
    """

    enabled: bool
    endpoint: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    resource_attributes: Mapping[str, str] = field(default_factory=dict)
    from_server: bool = False


def harness_telemetry_for(
    workspace: Path, session_id: str | None = None
) -> HarnessTelemetry | None:
    """
    The telemetry a harness launched in *workspace* runs with.

    The server's decision, which the host saved for this runner at
    :data:`HARNESS_TELEMETRY_ENV_VAR`, wins; its repository exclusions are
    checked here against the checkout's ``origin``, and an ``origin`` that
    can't be read while any exist means off, as does an unreadable decision.
    Without one, :func:`telemetry_endpoint_for` decides as before.

    :param workspace: Session workspace.
    :param session_id: The launching session, which may be a sub-session
        co-located in its parent's runner; it becomes ``omnigent.session.id``.
    :returns: The decision, or ``None`` to leave the user's own settings alone.
    """
    path = os.environ.get(HARNESS_TELEMETRY_ENV_VAR)
    if not path:
        endpoint = telemetry_endpoint_for(workspace)
        return HarnessTelemetry(enabled=True, endpoint=endpoint) if endpoint else None
    try:
        decision = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(decision, dict):
            raise ValueError("expected a mapping")
    except (OSError, ValueError) as exc:
        _log.warning("harness telemetry decision %s unreadable; telemetry off: %s", path, exc)
        return HarnessTelemetry(enabled=False, from_server=True)
    endpoint = decision.get("endpoint")
    if decision.get("enabled") is not True or not isinstance(endpoint, str) or not endpoint:
        return HarnessTelemetry(enabled=False, from_server=True)
    excluded_repos = {
        repo for repo in decision.get("exclude_repos") or () if isinstance(repo, str)
    }
    if excluded_repos:
        repo = _origin_repo(workspace)
        if repo is None or repo in excluded_repos:
            return HarnessTelemetry(enabled=False, from_server=True)
    attributes = _str_map(decision.get("resource_attributes"))
    if session_id:
        attributes["omnigent.session.id"] = session_id
    return HarnessTelemetry(
        enabled=True,
        endpoint=endpoint.rstrip("/"),
        headers=_str_map(decision.get("headers")),
        resource_attributes=attributes,
        from_server=True,
    )


def _str_map(value: object) -> dict[str, str]:
    """A JSON object's entries as strings; anything else is empty."""
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def otel_key_values(values: Mapping[str, str]) -> str:
    """Encode *values* as an OTel ``k=v,k2=v2`` env value, percent-encoding each value."""
    from urllib.parse import quote

    return ",".join(f"{key}={quote(value, safe='')}" for key, value in values.items())


def load_global_config(path: Path | None = None) -> _Config:
    """Load the user-level config, returning an empty mapping when absent."""
    resolved_path = path or global_config_path()
    if not resolved_path.exists():
        return {}
    with resolved_path.open() as config_file:
        raw: _Config = yaml.safe_load(config_file) or {}
        return raw


def load_local_config(path: Path | None = None) -> _Config:
    """Load the project-level config, returning an empty mapping when absent."""
    global _cwd_missing_warned
    if path is None:
        try:
            resolved_path = Path.cwd() / _LOCAL_CONFIG_RELPATH
        except OSError:
            # A runner can outlive its working directory; no local config to load.
            if not _cwd_missing_warned:
                _cwd_missing_warned = True
                _log.warning("load_local_config: cwd no longer exists; skipping project config")
            return {}
    else:
        resolved_path = path
    if not resolved_path.exists():
        return {}
    with resolved_path.open() as config_file:
        raw: _Config = yaml.safe_load(config_file) or {}
        return raw


def _ignored_project_harness_keys(cfg: _Config) -> list[str]:
    """Name project harness overrides without exposing their command or arguments."""
    harness = cfg.get("harness")
    if not isinstance(harness, dict):
        return []
    return sorted(f"harness.{name}" for name in harness if name != "default")


def _merge_effective_config(
    global_cfg: _Config,
    local_cfg: _Config,
) -> _Config:
    """Merge project defaults while retaining user-owned harness launch settings.

    A project may select the default harness. Server selection, executables and
    arguments come from user configuration or explicit command-line options.

    :param global_cfg: User-level config (``~/.omnigent/config.yaml``).
    :param local_cfg: Project-level config (``.omnigent/config.yaml``).
    :returns: The merged effective config dict.
    """
    merged: _Config = {
        **global_cfg,
        **{key: value for key, value in local_cfg.items() if key not in {"server", "harness"}},
    }
    g_harness = global_cfg.get("harness")
    l_harness = local_cfg.get("harness")
    ignored = _ignored_project_harness_keys(local_cfg)
    if ignored and l_harness != g_harness:
        _log.warning(
            "Ignoring project harness launch overrides: %s. "
            "Move these settings to user config %s.",
            ", ".join(ignored),
            global_config_path(),
        )
    if isinstance(l_harness, str):
        l_harness = {"default": l_harness}
    if isinstance(l_harness, dict) and "default" in l_harness:
        trusted = g_harness if isinstance(g_harness, dict) else {}
        merged["harness"] = {**trusted, "default": l_harness["default"]}
    return merged


@dataclass(frozen=True)
class ResolvedConfig:
    config: _Config
    preferences: Mapping[PreferenceKey, ResolvedPreference] = field(default_factory=dict)


def resolve_effective_config(
    global_cfg: _Config,
    local_cfg: _Config,
    *,
    preference_context: PreferenceContext | None = None,
    preference_layers: Sequence[PreferenceLayer] = (),
) -> ResolvedConfig:
    """Resolve legacy config, or explicitly adopted workspace preferences.

    Without a context, preserve legacy merging exactly. Supplying a context is
    an explicit adoption boundary: global portable values are no longer inputs.
    Callers must complete import/migration before opting in; this function does
    not infer user intent from YAML or write resolved values back to it.

    Project YAML remains an override, while commands, credentials, and unrelated
    settings retain their existing merge rules. Plugin choices remain in the
    provenance map for native adapters; they are not written into global YAML.
    """
    if preference_context is None:
        if preference_layers:
            raise ValueError("preference layers require an authenticated context")
        return ResolvedConfig(_merge_effective_config(global_cfg, local_cfg))
    if any(layer.scope == PreferenceScope.PROJECT for layer in preference_layers):
        raise ValueError("project preferences must come from the selected project config")
    project = PreferenceLayer(
        PreferenceScope.PROJECT,
        preference_context.identity,
        {
            key: value
            for key, value in preferences_from_config(local_cfg).items()
            if key.kind != PreferenceKind.HARNESS_ARGS
        },
    )
    resolved = resolve_preferences(preference_context, (*preference_layers, project))
    resolved = {
        key: item
        for key, item in resolved.items()
        if item.source in (PreferenceScope.SESSION, PreferenceScope.PROJECT)
        or not _project_masks_preference(local_cfg, key)
    }
    config = _without_portable_preferences(_merge_effective_config(global_cfg, local_cfg))
    _apply_preferences(config, resolved)
    return ResolvedConfig(config, resolved)


def _project_masks_preference(cfg: _Config, key: PreferenceKey) -> bool:
    """Apply explicit project replacements to lower scopes by canonical key."""
    if key.kind == PreferenceKind.MODEL:
        return key.name in cfg
    if key.kind != PreferenceKind.HARNESS:
        # Harness launch arguments are user-owned, matching _merge_effective_config.
        return False
    harness = cfg.get("harness")
    return isinstance(harness, str) or (isinstance(harness, dict) and "default" in harness)


def _apply_preferences(
    config: _Config, resolved: Mapping[PreferenceKey, ResolvedPreference]
) -> None:
    """Project requested values into the existing launch-config shape."""
    for key, preference in resolved.items():
        if key.kind == PreferenceKind.MODEL:
            assert isinstance(key.name, str)
            config[key.name] = preference.value
        elif key.kind in (PreferenceKind.HARNESS, PreferenceKind.HARNESS_ARGS):
            harness = config.setdefault("harness", {})
            assert isinstance(harness, dict)
            if key.kind == PreferenceKind.HARNESS:
                harness["default"] = preference.value
            else:
                entry = harness.get(key.name)
                entry = dict(entry) if isinstance(entry, dict) else {}
                assert isinstance(preference.value, tuple)
                entry["args"] = list(preference.value)
                harness[key.name] = entry


def _without_portable_preferences(cfg: _Config) -> _Config:
    """Keep local-only settings without mutating either source dictionary."""
    config = deepcopy(cfg)
    for name in ("model", "opencode_model"):
        config.pop(name, None)
    harness = config.get("harness")
    if isinstance(harness, dict):
        harness.pop("default", None)
        for entry in harness.values():
            if isinstance(entry, dict):
                entry.pop("args", None)
    else:
        config.pop("harness", None)
    return config


def load_effective_config(
    *,
    workspace: str | Path | None = None,
    preference_context: PreferenceContext | None = None,
    preference_layers: Sequence[PreferenceLayer] = (),
) -> _Config:
    """Merge global and project config, preserving user-owned launch settings.

    Project values take precedence except for server selection and harness
    executables and arguments. When supplied, *workspace* selects the project
    config directory instead of the process cwd.
    """
    local_path = Path(workspace) / _LOCAL_CONFIG_RELPATH if workspace is not None else None
    return resolve_effective_config(
        load_global_config(),
        load_local_config(local_path),
        preference_context=preference_context,
        preference_layers=preference_layers,
    ).config


def save_global_config(
    settings: Mapping[str, object],
    *,
    deep_merge_keys: tuple[str, ...] = (),
    path: Path | None = None,
) -> None:
    """Merge *settings* into the user-level config and write it back atomically.

    A reusable, runtime-safe writer (the runner and host reader persist GitHub
    account preferences through it), mirroring the merge semantics of the CLI's
    ``_save_global_config`` minus its harness-scalar normalization. Every key in
    *settings* replaces its existing value wholesale, except keys in
    *deep_merge_keys*, whose mapping value is merged one level deep into the
    existing mapping for that key.

    :param settings: Key/value pairs to set.
    :param deep_merge_keys: Keys whose mapping value is merged one level deep
        rather than replacing the existing mapping.
    :param path: Config path override (defaults to :func:`global_config_path`).
    """
    resolved = path or global_config_path()
    cfg = load_global_config(resolved)
    for key, value in settings.items():
        if key in deep_merge_keys and isinstance(value, Mapping):
            existing = cfg.get(key)
            merged = dict(existing) if isinstance(existing, Mapping) else {}
            merged.update(value)
            cfg[key] = merged
        else:
            cfg[key] = value
    resolved.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace so a crash mid-write can't truncate the user's config.
    tmp = resolved.with_name(resolved.name + ".tmp")
    with tmp.open("w") as config_file:
        yaml.safe_dump(cfg, config_file, default_flow_style=False, sort_keys=True)
    os.replace(tmp, resolved)


def _github_accounts(cfg: _Config) -> dict[str, object]:
    """Return the ``github.accounts`` mapping from *cfg* (empty when absent)."""
    github = cfg.get("github")
    if not isinstance(github, Mapping):
        return {}
    accounts = github.get("accounts")
    return dict(accounts) if isinstance(accounts, Mapping) else {}


def github_account_preference(workspace_key: str, path: Path | None = None) -> str | None:
    """Return the preferred ``gh`` login for a workspace, or ``None``.

    :param workspace_key: Stable per-workspace key (the main worktree path), used
        verbatim — a filesystem path, so not case-folded.
    :param path: Config path override (defaults to :func:`global_config_path`).
    """
    accounts = _github_accounts(load_global_config(path))
    value = accounts.get(workspace_key)
    return value if isinstance(value, str) and value else None


def set_github_account_preference(
    workspace_key: str,
    login: str | None,
    path: Path | None = None,
) -> None:
    """Persist (or clear, when *login* is falsy) the preferred login for a workspace.

    Stored under ``github.accounts`` keyed by the workspace's main worktree path,
    so the choice is shared across a repo's linked worktrees (which share one
    ``.git``) while separate clones stay distinct.

    :param workspace_key: Stable per-workspace key (the main worktree path).
    :param login: GitHub login to prefer, or ``None``/empty to clear the entry.
    :param path: Config path override (defaults to :func:`global_config_path`).
    """
    resolved = path or global_config_path()
    github = load_global_config(resolved).get("github")
    github_block = dict(github) if isinstance(github, Mapping) else {}
    accounts = _github_accounts({"github": github_block})
    key = workspace_key
    if login:
        accounts[key] = login
    else:
        accounts.pop(key, None)
    github_block["accounts"] = accounts
    save_global_config({"github": github_block}, path=resolved)


__all__ = [
    "github_account_preference",
    "global_config_path",
    "load_effective_config",
    "load_global_config",
    "load_local_config",
    "resolve_effective_config",
    "save_global_config",
    "set_github_account_preference",
]
