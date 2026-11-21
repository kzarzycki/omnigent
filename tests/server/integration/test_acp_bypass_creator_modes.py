"""An ACP session created by another session may bypass approval only under a bypass creator."""

import pytest

from omnigent.acp_permission_mode import ACP_PERMISSION_MODE_LABEL_KEY
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runtime import get_conversation_store
from tests.server.helpers import create_test_agent
from tests.server.integration.test_acp_model_selection import (
    _executor,
    _isolated_provider_config,  # noqa: F401  (autouse fixture)
)
from tests.server.integration.test_session_orchestration import (  # noqa: F401
    auth_app,
    orchestration_environment,
    peers,
    startup_permissions,
)

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("creator_mode", [None, "auto", "bypassPermissions"])
@pytest.mark.parametrize("placement", ["child", "independent"])
async def test_bypass_needs_a_creator_in_bypass(
    auth_client,
    peers,  # noqa: F811
    monkeypatch,
    creator_mode,
    placement,
):
    from omnigent.server.routes.sessions import routes_core

    source, _, _ = peers
    store = get_conversation_store()
    creator = store.get_conversation(source["id"])
    if creator_mode is not None:
        store.set_labels(
            creator.id, {**creator.labels, ACP_PERMISSION_MODE_LABEL_KEY: creator_mode}
        )
    agent = await create_test_agent(auth_client, executor=_executor(), include_llm=False)

    async def skip_launch(**kwargs):
        pass

    monkeypatch.setattr(routes_core, "_run_managed_launch", skip_launch)
    body = {"agent_id": agent["id"], "initial_items": [], "permission_mode": "bypassPermissions"}
    headers = {}
    if placement == "child":
        body["parent_session_id"] = source["id"]
    else:
        body["host_type"] = "managed"
        headers[ORCHESTRATION_SOURCE_HEADER] = source["id"]

    response = await auth_client.post("/v1/sessions", json=body, headers=headers)

    if creator_mode == "bypassPermissions":
        assert response.status_code == 201, response.text
        created = store.get_conversation(response.json()["id"])
        assert created.labels[ACP_PERMISSION_MODE_LABEL_KEY] == "bypassPermissions"
    else:
        assert response.status_code == 400, response.text
        assert "creator" in response.text and "same mode" in response.text
