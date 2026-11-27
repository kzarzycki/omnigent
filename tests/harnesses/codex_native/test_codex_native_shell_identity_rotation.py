"""A native /clear rotation moves the shell identity file to the new session."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.harnesses.codex_native.shell_identity import (
    read_shell_identity,
    write_shell_identity,
)


async def _rotate(bridge_dir: Path) -> str:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_old":
            return httpx.Response(200, json={"id": "conv_old", "agent_id": "ag_codex"})
        if request.method == "POST" and request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"id": "conv_new"})
        return httpx.Response(200, json=json.loads(request.content or "{}"))

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        return await fwd._create_thread_replacement_session(
            client=client,
            old_session_id="conv_old",
            bridge_dir=bridge_dir,
            app_server_url="ws://127.0.0.1:1",
            new_thread_id="thread_new",
        )


@pytest.mark.parametrize(
    ("primary_before", "primary_after"),
    [
        ("conv_old", "conv_new"),  # top-level session: its own primary
        ("conv_parent", "conv_parent"),  # sub-agent: primary stays its parent
    ],
)
async def test_rotation_updates_shell_identity(
    tmp_path: Path, primary_before: str, primary_after: str
) -> None:
    write_shell_identity(tmp_path, session_id="conv_old", primary_session_id=primary_before)

    assert await _rotate(tmp_path) == "conv_new"

    assert read_shell_identity(tmp_path) == {
        "session_id": "conv_new",
        "primary_session_id": primary_after,
    }
