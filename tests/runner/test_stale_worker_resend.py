"""A turn or an archive holding a worker client that ended in its shutdown window is resent.

The runner gets a harness client, then does more work before it posts. In that gap an
idle-exiting worker can mark itself as ending and go. The post then reaches no worker (a
refused connection) and was lost; it goes once more through the process manager, which
waits for the marked worker to exit and respawns it (or finds no live worker).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from omnigent.runner.app import create_runner_app
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import _runner_client
from tests.runner.helpers import NullServerClient
from tests.runtime.harnesses.test_acp_idle_exit_resume import (
    _BRIDGE as _IDLE_BRIDGE,
)

CONV = "a136ad3e8265e86eba8564d6cda81a14"

# The bridge answers, then waits for the test to open the gate before it leaves idle: the worker
# stays live and unmarked until the test lets it mark itself as ending and go.
_BRIDGE = _IDLE_BRIDGE.replace(
    "        os._exit(75)",
    "        import time\n"
    "        while not os.path.exists(sys.argv[3]):\n"
    "            time.sleep(0.02)\n"
    "        os._exit(75)",
)


@pytest.fixture
def short_parent() -> Iterator[Path]:
    parent = Path(tempfile.gettempdir()) / f"omni-stale-{uuid.uuid4().hex[:8]}"
    parent.mkdir(mode=0o700)
    try:
        yield parent
    finally:
        shutil.rmtree(parent, ignore_errors=True)


class _PausingManager(HarnessProcessManager):
    """A real process manager that holds one ``get_client`` after it got the client."""

    def __init__(self, env: dict[str, str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.env = env
        self.pause_for: str | None = None
        self.acquired = asyncio.Event()
        self.release_it = asyncio.Event()
        self.calls: list[str] = []

    async def get_client(self, conversation_id: str, harness: str, env: Any = None) -> Any:
        self.calls.append(harness)
        client = await super().get_client(conversation_id, harness, env=self.env)
        if self.pause_for == harness:
            self.pause_for = None
            self.acquired.set()
            await self.release_it.wait()
        return client


def _app(manager: HarnessProcessManager):
    spec = AgentSpec(
        spec_version=1,
        name="cloud",
        executor=ExecutorSpec(type="omnigent", config={"harness": "acp", "command": "bridge"}),
    )

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return spec

    return create_runner_app(
        process_manager=manager,  # type: ignore[arg-type]
        spec_resolver=resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )


def _manager(short_parent: Path, tmp_path: Path) -> tuple[_PausingManager, Path]:
    agent = tmp_path / "bridge.py"
    agent.write_text(_BRIDGE)
    log = tmp_path / "log.jsonl"
    env = {
        "HARNESS_ACP_COMMAND": " ".join(
            [
                sys.executable,
                str(agent),
                str(log),
                str(tmp_path / "state.json"),
                str(tmp_path / "gate"),
            ]
        ),
        "HARNESS_ACP_NAME": "Cloud",
        "HARNESS_ACP_OMNIGENT_MCP": "false",
        "HARNESS_ACP_CWD": str(tmp_path),
        "TMPDIR": str(tmp_path),
    }
    return _PausingManager(env, tmp_parent=short_parent), log


def _prompts(log: Path) -> list[str]:
    if not log.exists():
        return []
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    return [e[1] for e in entries if e[0] == "prompt"]


async def _prompted(log: Path, count: int) -> list[str]:
    for _ in range(600):
        if len(_prompts(log)) >= count:
            break
        await asyncio.sleep(0.1)
    return _prompts(log)


async def _send(client: Any, text: str) -> Any:
    response = await client.post(
        f"/v1/sessions/{CONV}/events",
        json={
            "type": "message",
            "role": "user",
            "model": "agent",
            "content": [{"type": "input_text", "text": text}],
        },
    )
    assert response.status_code < 300, response.text
    return response


async def _first_turn(client: Any, log: Path) -> str:
    created = await client.post("/v1/sessions", json={"session_id": CONV, "agent_id": "agent"})
    assert created.status_code == 201, created.text
    await _send(client, "hi")
    prompts = await _prompted(log, 1)
    assert prompts, "the first turn never reached the bridge"
    return prompts[0]


async def _worker_gone(manager: HarnessProcessManager) -> None:
    entry = manager._entries[CONV]
    await asyncio.wait_for(entry.process.wait(), timeout=20)
    assert entry.ending_file_exists()  # it marked itself before it went


@pytest.mark.asyncio
async def test_a_message_posted_after_the_worker_ended_is_answered_by_the_respawned_worker(
    short_parent: Path,
    tmp_path: Path,
) -> None:
    manager, log = _manager(short_parent, tmp_path)
    await manager.start()
    try:
        async with _runner_client(_app(manager)) as client:
            cloud = await _first_turn(client, log)
            manager.pause_for = "acp"
            second = asyncio.ensure_future(_send(client, "again"))
            await asyncio.wait_for(manager.acquired.wait(), timeout=30)
            # The runner holds a client; its worker marks itself and ends before the post.
            (tmp_path / "gate").touch()
            await _worker_gone(manager)
            manager.release_it.set()
            response = await asyncio.wait_for(second, timeout=60)
            prompts = await _prompted(log, 2)
    finally:
        await manager.shutdown()

    assert response.status_code < 300
    assert len(prompts) == 2, "the held turn was lost: the respawned worker never answered it"
    assert prompts[1] == cloud


@pytest.mark.asyncio
async def test_an_archive_posted_after_the_worker_ended_reaches_the_cloud_session(
    short_parent: Path,
    tmp_path: Path,
) -> None:
    manager, log = _manager(short_parent, tmp_path)
    await manager.start()
    try:
        async with _runner_client(_app(manager)) as client:
            cloud = await _first_turn(client, log)
            manager.pause_for = "any"
            ended = asyncio.ensure_future(
                client.post(f"/v1/sessions/{CONV}/events", json={"type": "session_ended"})
            )
            await asyncio.wait_for(manager.acquired.wait(), timeout=30)
            (tmp_path / "gate").touch()
            await _worker_gone(manager)
            manager.release_it.set()
            response = await asyncio.wait_for(ended, timeout=60)
    finally:
        await manager.shutdown()

    assert response.status_code == 204
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert ["archive", cloud] in events
