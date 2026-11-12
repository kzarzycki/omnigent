"""Parsing and matching of the server's model and effort block list."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.errors import OmnigentError
from omnigent.server import model_block_list
from omnigent.server.model_block_list import parse_model_block_list

CATALOG = [
    {"id": "opus", "model": "fixture-model-opus", "isDefault": True},
    {
        "id": "fixture-model-mini",
        "defaultReasoningEffort": "max",
        "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "max"}],
    },
]


@pytest.mark.parametrize("config", [None, {}, {"blocked_models": [], "blocked_efforts": None}])
def test_unset_lists_change_nothing(config: dict[str, Any] | None) -> None:
    block_list = parse_model_block_list(config)

    assert not block_list
    assert block_list.filter_model_options(CATALOG) == CATALOG
    assert block_list.allowed_efforts(["low", "max"]) == ["low", "max"]
    block_list.refuse_blocked(model="fixture-model-opus", effort="max")


def test_entries_match_without_case_or_context_window_suffix() -> None:
    block_list = parse_model_block_list(
        {"blocked_models": ["Fixture-Model-Opus[1m]", 7, ""], "blocked_efforts": "XHIGH, max"}
    )

    assert block_list.models == {"fixture-model-opus"}
    assert block_list.efforts == {"xhigh", "max"}
    assert block_list.model_blocked("fixture-model-opus[200k]")
    assert not block_list.model_blocked("fixture-model-opus-2")
    assert block_list.allowed_efforts(["low", "xhigh", "max"]) == ["low"]


def test_picker_rows_drop_blocked_models_and_efforts() -> None:
    block_list = parse_model_block_list(
        {"blocked_models": ["fixture-model-opus"], "blocked_efforts": ["max"]}
    )

    assert block_list.filter_model_options(CATALOG) == [
        {"id": "fixture-model-mini", "supportedReasoningEfforts": [{"reasoningEffort": "low"}]}
    ]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"model": "fixture-model-opus"},
            "Model 'fixture-model-opus' is blocked by the server setting 'blocked_models'",
        ),
        (
            {"effort": "max"},
            "Reasoning effort 'max' is blocked by the server setting 'blocked_efforts'",
        ),
    ],
)
def test_refusal_names_the_entry_and_the_setting(kwargs: dict[str, str], message: str) -> None:
    block_list = parse_model_block_list(
        {"blocked_models": ["fixture-model-opus"], "blocked_efforts": ["max"]}
    )

    with pytest.raises(OmnigentError, match=message):
        block_list.refuse_blocked(**kwargs)


class _Registry:
    def get(self, host_id: str) -> object | None:
        return object() if host_id == "host_fixture" else None


async def test_a_blocked_host_default_model_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A start that names no model runs the harness default, so a blocked one refuses it."""
    asked: list[str] = []

    async def _host_options(**kwargs: Any) -> dict[str, Any]:
        asked.append(kwargs["harness"])
        return {"status": "ok", "models": CATALOG}

    monkeypatch.setattr(
        "omnigent.server.routes._host_model_options.request_host_model_options", _host_options
    )
    monkeypatch.setattr(
        model_block_list,
        "_current",
        parse_model_block_list({"blocked_models": ["fixture-model-opus"]}),
    )
    start = {
        "host_registry": _Registry(),
        "host_id": "host_fixture",
        "harness": "claude-native",
        "spec": None,
        "inference_snapshot": None,
        "reasoning_effort": None,
    }

    with pytest.raises(OmnigentError, match="'fixture-model-opus' is blocked"):
        await model_block_list.refuse_blocked_start_defaults(**start, model_override=None)
    assert asked == ["claude-native"]
    # A start that names an allowed model does not ask the host.
    await model_block_list.refuse_blocked_start_defaults(
        **start, model_override="fixture-model-mini"
    )
    assert asked == ["claude-native"]


@pytest.mark.parametrize(
    ("host_id", "answer"),
    [
        (None, None),
        ("host_fixture", {"status": "unsupported", "models": []}),
        ("host_fixture", {"status": "ok", "models": CATALOG[1:]}),
        ("host_fixture", TimeoutError()),
    ],
    ids=["no-host", "no-catalog", "no-default-row", "no-answer"],
)
async def test_a_start_whose_default_cannot_be_learned_is_refused(
    monkeypatch: pytest.MonkeyPatch, host_id: str | None, answer: Any
) -> None:
    """An unknown default might be a blocked one, so such a start must name its model."""

    async def _host_options(**_kwargs: Any) -> dict[str, Any]:
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(
        "omnigent.server.routes._host_model_options.request_host_model_options", _host_options
    )
    monkeypatch.setattr(
        model_block_list,
        "_current",
        parse_model_block_list({"blocked_models": ["fixture-model-opus"]}),
    )

    with pytest.raises(OmnigentError, match="Cannot tell whether the default model of harness "):
        await model_block_list.refuse_blocked_start_defaults(
            host_registry=_Registry(),
            host_id=host_id,
            harness="cursor-native",
            spec=None,
            inference_snapshot=None,
            model_override=None,
            reasoning_effort=None,
        )
    # Only a blocked model list makes the default matter.
    monkeypatch.setattr(
        model_block_list, "_current", parse_model_block_list({"blocked_efforts": ["max"]})
    )
    await model_block_list.refuse_blocked_start_defaults(
        host_registry=_Registry(),
        host_id=host_id,
        harness="cursor-native",
        spec=None,
        inference_snapshot=None,
        model_override=None,
        reasoning_effort=None,
    )
