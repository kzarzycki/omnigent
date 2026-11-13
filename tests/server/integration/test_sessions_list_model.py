"""``GET /v1/sessions`` carries each session's model and effective effort.

The list item reads the model the harness already records for the session
(its report, else the user's pick, else the spec default), so "which model
ran which session" is answerable from the listing alone.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def _listed(client: httpx.AsyncClient, session_id: str) -> dict[str, Any]:
    """Return one session's row from the ``GET /v1/sessions`` listing."""
    rows = (await client.get("/v1/sessions")).json()["data"]
    return next(row for row in rows if row["id"] == session_id)


async def test_listing_carries_model_for_claude_native_codex_native_and_claude_cloud(
    client: httpx.AsyncClient,
) -> None:
    """
    Each harness's own record of the model shows on the listing row.

    - claude-native: the forwarder's ``external_model_change`` report wins.
    - codex-native: no report yet, so the spec's model applies.
    - claude-cloud: an own-auth ACP harness, so the spec pin is not claimed;
      the picked ``model_override`` and the picked effort show instead.
    """
    claude_native = await create_test_agent(
        client,
        name="claude-native-ui",
        executor={"type": "omnigent", "config": {"harness": "claude-native"}},
    )
    codex_native = await create_test_agent(
        client,
        name="codex-native-ui",
        executor={"type": "omnigent", "config": {"harness": "codex-native"}},
    )
    claude_cloud = await create_test_agent(
        client,
        name="claude-cloud-ui",
        executor={"type": "omnigent", "config": {"harness": "acp:claude-cloud"}},
    )

    native = (await client.post("/v1/sessions", json={"agent_id": claude_native["id"]})).json()
    reported = await client.post(
        f"/v1/sessions/{native['id']}/events",
        json={"type": "external_model_change", "data": {"model": "claude-opus-4-8[1m]"}},
    )
    assert reported.status_code == 202, reported.text
    codex = (await client.post("/v1/sessions", json={"agent_id": codex_native["id"]})).json()
    cloud_response = await client.post(
        "/v1/sessions",
        json={
            "agent_id": claude_cloud["id"],
            "model_override": "opus",
            "reasoning_effort": "high",
        },
    )
    assert cloud_response.status_code == 201, cloud_response.text
    cloud = cloud_response.json()

    assert (await _listed(client, native["id"]))["llm_model"] == "claude-opus-4-8[1m]"
    assert (await _listed(client, codex["id"]))["llm_model"] == "codex-native-ui"
    cloud_row = await _listed(client, cloud["id"])
    assert cloud_row["llm_model"] == "opus"
    assert cloud_row["effective_reasoning_effort"] == "high"
    # Existing fields keep their meaning: the effort is still the explicit pick.
    assert cloud_row["reasoning_effort"] == "high"
