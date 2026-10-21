"""New top-level sessions are filed by their workspace.

A session created or first launched on a workspace a project owns is filed into
that project (longest root wins). One that matches no root stays unfiled.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from omnigent.host.frames import (
    HostLaunchRunnerFrame,
    HostLaunchRunnerResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.runtime import _globals
from omnigent.server.routes._session_create_validation import project_for_workspace
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from tests.server.integration.test_host_session_binding import (
    _HOST_ID,
    _connect_host,
    binding_app,  # noqa: F401 - fixture
)

OTHER_HOST = "0" * 32


def _project(pid: str, **config: object) -> SimpleNamespace:
    return SimpleNamespace(id=pid, config=config)


def test_project_for_workspace_picks_the_longest_root_on_the_host() -> None:
    projects = [
        _project("home", host_id=_HOST_ID, workspace="/home/a"),
        _project("repo", host_id=_HOST_ID, workspace="/home/a/dev/repo/"),
        _project(
            "svc",
            host_id=_HOST_ID,
            workspace_roots=[
                {"workspace": "/srv/svc"},
                {"workspace": "/x", "host_id": OTHER_HOST},
            ],
        ),
        _project("elsewhere", host_id=OTHER_HOST, workspace="/home/a/dev"),
    ]
    assert project_for_workspace(projects, _HOST_ID, "/home/a/dev/repo/wt/x") == "repo"
    assert project_for_workspace(projects, _HOST_ID, "/home/a/dev/repo") == "repo"
    assert project_for_workspace(projects, _HOST_ID, "/home/a/dev/repo2") == "home"
    assert project_for_workspace(projects, _HOST_ID, "/srv/svc/worktrees/b") == "svc"
    assert project_for_workspace(projects, OTHER_HOST, "/x/y") == "svc"
    assert project_for_workspace(projects, _HOST_ID, "/x/y") is None
    assert project_for_workspace(projects, _HOST_ID, "/home/ab") is None
    # A tie between two projects files nowhere.
    tie = [*projects, _project("dup", host_id=_HOST_ID, workspace="/home/a")]
    assert project_for_workspace(tie, _HOST_ID, "/home/a/z") is None
    windows = [_project("win", host_id=_HOST_ID, workspace="C:\\Users\\a\\dev")]
    assert project_for_workspace(windows, _HOST_ID, "C:\\Users\\a\\dev\\repo") == "win"
    malformed = [_project("bad", host_id=_HOST_ID, workspace_roots=["/home", {"workspace": 3}])]
    assert project_for_workspace(malformed, _HOST_ID, "/home/a") is None


async def _launch(app, comm, session_id: str, workspace: str, status: str = "launched") -> int:
    async def respond() -> None:
        while True:
            output = await comm.receive_output(timeout=2.0)
            if output["type"] == "websocket.send":
                frame = decode_host_frame(output["text"])
                if isinstance(frame, HostLaunchRunnerFrame):
                    result = HostLaunchRunnerResultFrame(
                        request_id=frame.request_id, status=status, runner_id="runner_token_t"
                    )
                    text = encode_host_frame(result)
                    await comm.send_input({"type": "websocket.receive", "text": text})
                    return

    responder = asyncio.create_task(respond())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/runners",
            json={"session_id": session_id, "workspace": workspace},
        )
    await responder
    return resp.status_code


@pytest.mark.asyncio
async def test_first_launch_files_the_session_by_its_workspace(
    binding_app,  # noqa: F811
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """The REST-script path: create with no host, then launch a runner on a workspace."""
    app, registry, _hs, conv_store = binding_app
    project_store = SqlAlchemyProjectStore(db_uri)
    monkeypatch.setattr(_globals, "_project_store", project_store)
    repo = project_store.create(
        uuid.uuid4().hex, "repo", None, {"host_id": _HOST_ID, "workspace": str(tmp_path)}
    ).id
    other = project_store.create(uuid.uuid4().hex, "other", None).id
    comm = await _connect_host(app, registry)

    stray_parent = conv_store.create_conversation(agent_id=None)
    session = conv_store.create_conversation(agent_id=None)
    assert session.project_id is None
    assert await _launch(app, comm, session.id, str(tmp_path / "wt")) == 200
    assert conv_store.get_conversation(session.id).project_id == repo

    # A session already filed keeps its project.
    filed = conv_store.create_conversation(agent_id=None, project_id=other)
    assert await _launch(app, comm, filed.id, str(tmp_path)) == 200
    assert conv_store.get_conversation(filed.id).project_id == other

    # A sub-agent child lives under its parent, never in a project.
    child = conv_store.create_conversation(agent_id=None, parent_conversation_id=stray_parent.id)
    assert await _launch(app, comm, child.id, str(tmp_path)) == 200
    assert conv_store.get_conversation(child.id).project_id is None

    # A relaunch (the session had a host already) never re-files it.
    bound = conv_store.create_conversation(agent_id=None, host_id=_HOST_ID, workspace="/")
    assert await _launch(app, comm, bound.id, str(tmp_path)) == 200
    assert conv_store.get_conversation(bound.id).project_id is None

    # A refused launch files nothing.
    refused = conv_store.create_conversation(agent_id=None)
    assert await _launch(app, comm, refused.id, str(tmp_path), status="failed") == 502
    assert conv_store.get_conversation(refused.id).project_id is None

    # No root matches: the session stays unfiled.
    stray = conv_store.create_conversation(agent_id=None)
    assert await _launch(app, comm, stray.id, "/") == 200
    assert conv_store.get_conversation(stray.id).project_id is None
    await comm.send_input({"type": "websocket.disconnect", "code": 1000})
