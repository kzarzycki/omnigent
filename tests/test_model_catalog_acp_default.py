"""``sys_list_models`` lists the default model an ACP agent declares."""

from __future__ import annotations

from omnigent.models.model_catalog import list_models_for_worker
from omnigent.spec.types import AgentSpec, ExecutorSpec


def test_acp_agent_default_model_is_listed() -> None:
    spec = AgentSpec(
        spec_version=1,
        name="acp-fixture",
        executor=ExecutorSpec(
            type="omnigent", model="fixture-model-mini", config={"harness": "acp:fixture"}
        ),
    )
    listing = list_models_for_worker(spec, "acp:fixture")
    assert [model.id for model in listing.models] == ["fixture-model-mini"]
