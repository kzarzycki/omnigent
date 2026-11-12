"""Server-wide block list of models and reasoning efforts.

An operator names models and effort levels sessions must not run on. Blocked
entries are left out of every model and effort list the web UI reads, and a
session start or a model/effort switch that names one is refused. A session
already running on an entry that is blocked later keeps running.

The server config keys ``blocked_models`` and ``blocked_efforts`` hold the
lists, e.g. ``["fixture-model"]`` and ``["xhigh", "max"]``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from omnigent.errors import ErrorCode, OmnigentError

if TYPE_CHECKING:
    from omnigent.server.host_registry import HostRegistry
    from omnigent.spec.types import AgentSpec

logger = logging.getLogger(__name__)

# Bounds the host round-trip a start makes to learn a harness's default model.
_HOST_DEFAULT_MODEL_TIMEOUT_S = 5.0

BLOCKED_MODELS_KEY = "blocked_models"
BLOCKED_EFFORTS_KEY = "blocked_efforts"

# A context-window variant such as ``claude-opus-4-8[1m]`` runs the same model.
_VARIANT_SUFFIX = re.compile(r"\[[^\[\]]*\]$")


def _normalized_model(model: str) -> str:
    return _VARIANT_SUFFIX.sub("", model.strip()).casefold()


@dataclass(frozen=True)
class ModelBlockList:
    """Blocked model ids and effort levels, compared case-insensitively."""

    models: frozenset[str] = frozenset()
    efforts: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.models or self.efforts)

    def model_blocked(self, model: str | None) -> bool:
        """
        :param model: Model id or picker id, e.g. ``"claude-opus-4-8[1m]"``.
        :returns: Whether the model, or its base id without a variant suffix,
            is blocked.
        """
        if not model or not self.models:
            return False
        return model.strip().casefold() in self.models or _normalized_model(model) in self.models

    def effort_blocked(self, effort: str | None) -> bool:
        """:param effort: Effort level, e.g. ``"max"``."""
        return effort is not None and effort.strip().casefold() in self.efforts

    def allowed_efforts(self, efforts: Iterable[str]) -> list[str]:
        """Drop blocked levels from an ordered effort ladder."""
        return [effort for effort in efforts if not self.effort_blocked(effort)]

    def filter_model_options(self, options: Iterable[Any]) -> list[Any]:
        """
        Drop blocked models and blocked efforts from model picker rows.

        :param options: Rows shaped like ``NativeModelOption`` dicts, e.g.
            ``{"id": "opus", "model": "claude-opus-4-8",
            "supportedReasoningEfforts": [{"reasoningEffort": "max"}]}``.
            Non-dict rows pass through unchanged.
        :returns: The remaining rows; a row is hidden when its ``id`` or
            ``model`` is blocked.
        """
        if not self:
            return list(options)
        kept: list[Any] = []
        for option in options:
            if not isinstance(option, dict):
                kept.append(option)
                continue
            if self.model_blocked(option.get("id")) or self.model_blocked(option.get("model")):
                continue
            kept.append(self._without_blocked_efforts(option))
        return kept

    def _without_blocked_efforts(self, option: dict[str, Any]) -> dict[str, Any]:
        if not self.efforts:
            return option
        option = dict(option)
        supported = option.get("supportedReasoningEfforts")
        if isinstance(supported, list):
            option["supportedReasoningEfforts"] = [
                entry
                for entry in supported
                if not (
                    isinstance(entry, dict) and self.effort_blocked(entry.get("reasoningEffort"))
                )
            ]
        if self.effort_blocked(option.get("defaultReasoningEffort")):
            option.pop("defaultReasoningEffort")
        return option

    def filter_model_ids(self, models: Iterable[str]) -> list[str]:
        """Drop blocked ids from a plain list of model ids."""
        return [model for model in models if not self.model_blocked(model)]

    def refuse_blocked(self, *, model: str | None = None, effort: str | None = None) -> None:
        """
        Refuse a start or switch that names a blocked model or effort.

        :param model: Requested or default model id, or ``None``.
        :param effort: Requested or default effort level, or ``None``.
        :raises OmnigentError: ``INVALID_INPUT`` naming the entry and the
            setting that blocks it.
        """
        if self.model_blocked(model):
            raise OmnigentError(
                f"Model {model!r} is blocked by the server setting {BLOCKED_MODELS_KEY!r}",
                code=ErrorCode.INVALID_INPUT,
            )
        if self.effort_blocked(effort):
            raise OmnigentError(
                f"Reasoning effort {effort!r} is blocked by the server setting "
                f"{BLOCKED_EFFORTS_KEY!r}",
                code=ErrorCode.INVALID_INPUT,
            )


def _entries(config: Mapping[str, Any], key: str) -> frozenset[str]:
    raw = config.get(key)
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        raw = raw.split(",")  # ``omnigent config set`` stores a comma-separated string
    if not isinstance(raw, (list, tuple)):
        logger.warning("server config %s must be a list of strings; ignoring it", key)
        return frozenset()
    valid = [item for item in raw if isinstance(item, str) and item.strip()]
    if len(valid) != len(raw) and not isinstance(config.get(key), str):
        logger.warning("server config %s has non-string or empty entries; skipping them", key)
    entries = {item.strip().casefold() for item in valid}
    if key == BLOCKED_MODELS_KEY:
        entries = {_normalized_model(entry) for entry in entries}
    return frozenset(entries)


def parse_model_block_list(config: Mapping[str, Any] | None) -> ModelBlockList:
    """
    :param config: Loaded server config, e.g.
        ``{"blocked_models": ["gpt-5.2"], "blocked_efforts": ["max"]}``.
    :returns: The block list; empty when neither key is set.
    """
    if not config:
        return ModelBlockList()
    return ModelBlockList(
        models=_entries(config, BLOCKED_MODELS_KEY),
        efforts=_entries(config, BLOCKED_EFFORTS_KEY),
    )


_current = ModelBlockList()


def configure_model_block_list(config: Mapping[str, Any] | None) -> ModelBlockList:
    """Install the server's block list; ``create_app`` calls this once."""
    global _current
    _current = parse_model_block_list(config)
    return _current


def current_model_block_list() -> ModelBlockList:
    """The block list the server was configured with (empty by default)."""
    return _current


async def _host_default_model(
    host_registry: HostRegistry, host_id: str, harness: str
) -> str | None:
    """The model the host's pre-launch catalog marks as *harness*'s default.

    :returns: The default row's launch id, or ``None`` when the host is not
        connected or cannot answer for this harness.
    """
    from omnigent.server.routes._host_model_options import request_host_model_options

    host_conn = host_registry.get(host_id)
    if host_conn is None:
        return None
    try:
        result = await request_host_model_options(
            host_registry=host_registry,
            host_conn=host_conn,
            harness=harness,
            timeout_s=_HOST_DEFAULT_MODEL_TIMEOUT_S,
        )
    except Exception:
        logger.debug("host default model unavailable for harness %r", harness, exc_info=True)
        return None
    options = result.get("models") if result.get("status") == "ok" else None
    for option in options if isinstance(options, list) else []:
        if isinstance(option, dict) and option.get("isDefault"):
            default = option.get("model") or option.get("id")
            return default if isinstance(default, str) else None
    return None


async def refuse_blocked_start_defaults(
    *,
    host_registry: HostRegistry | None,
    host_id: str | None,
    harness: str | None,
    spec: AgentSpec | None,
    inference_snapshot: Mapping[str, Any] | None,
    model_override: str | None,
    reasoning_effort: str | None,
) -> None:
    """Refuse a start whose default model or effort is blocked.

    A start that names no model runs the agent's model, else its inference
    binding's default, else the harness's own default on the host.

    :param host_registry: Live host connections, or ``None``.
    :param host_id: Target host, or ``None`` (e.g. a sandbox).
    :param harness: Resolved harness id, e.g. ``"claude-native"``.
    :param spec: The agent (or named sub-agent) spec, when it loaded.
    :param inference_snapshot: The session's captured inference configuration.
    :param model_override: The model the request chose, if any.
    :param reasoning_effort: The effort the request chose, if any.
    :raises OmnigentError: When the default is blocked.
    """
    block_list = current_model_block_list()
    if not block_list:
        return
    if reasoning_effort is None and spec is not None:
        block_list.refuse_blocked(effort=spec.executor.reasoning_effort)
    if model_override is not None or not block_list.models:
        return
    default_model = spec.executor.model if spec is not None else None
    if default_model is None and harness and inference_snapshot is not None:
        from omnigent.inference_config import binding_for_harness

        binding = binding_for_harness(inference_snapshot["runtime_config"], harness)
        default_model = binding.default_model if binding is not None else None
    if default_model is None and harness and host_registry is not None and host_id:
        default_model = await _host_default_model(host_registry, host_id, harness)
    block_list.refuse_blocked(model=default_model)
