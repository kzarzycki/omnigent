"""A claude-cloud session through the real process manager: idle exit, resume, archive.

The bridge leaves idle after each answer, the worker that drove it ends with it, and the
runner holds nothing for the session. The next message starts a worker and a bridge that
re-attach the same cloud session from the replayed transcript; archiving the session after
an idle exit still tells the cloud session.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

CONV = "conv_idle"
MARKER = "cloud session: https://claude.ai/code/"

# Stands in for the claude-cloud bridge: the first prompt makes a cloud session, a prompt that
# carries the replayed ``cloud session:`` line re-attaches it, and it leaves idle after each
# answer. Its state file maps ACP session ids to cloud sessions, as the real bridge's does.
_BRIDGE = r"""
import json, os, re, sys

log, state = sys.argv[1], sys.argv[2]

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def note(entry):
    with open(log, "a") as f:
        f.write(json.dumps(entry) + "\n")

def known():
    try:
        return json.load(open(state))
    except OSError:
        return {}

caps = {"omnigentIdleExit": {"status": 75}, "omnigentSessionEnded": {"method": "_fake/ended"}}
acp_id = "acp-%d" % os.getpid()
for line in sys.stdin:
    msg = json.loads(line)
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        note(["start"])
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": 1, "agentCapabilities": caps}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": acp_id}})
    elif method == "session/prompt":
        text = params["prompt"][0]["text"]
        found = re.findall(r"cloud session: https://claude.ai/code/(\w+)", text)
        cloud = found[-1] if found else "cloud_%d" % os.getpid()
        note(["prompt", cloud])
        table = known()
        table[params["sessionId"]] = cloud
        json.dump(table, open(state, "w"))
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": params["sessionId"],
            "update": {"sessionUpdate": "agent_message_chunk",
                       "content": {"type": "text",
                                   "text": "cloud session: https://claude.ai/code/" + cloud}}}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
        os._exit(75)
    elif method == "_fake/ended":
        note(["archive", known().get(params["sessionId"])])
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
"""


@pytest.fixture
def short_parent() -> AsyncIterator[Path]:
    parent = Path(tempfile.gettempdir()) / f"omni-idle-{uuid.uuid4().hex[:8]}"
    parent.mkdir(mode=0o700)
    try:
        yield parent  # type: ignore[misc]
    finally:
        shutil.rmtree(parent, ignore_errors=True)


async def _turn(client: httpx.AsyncClient, content: list[dict]) -> str:
    async with client.stream(
        "POST",
        f"/v1/sessions/{CONV}/events",
        json={"type": "message", "role": "user", "model": "agent", "content": content},
    ) as response:
        assert response.status_code == 200, await response.aread()
        body = (await response.aread()).decode()
    assert "response.completed" in body, body
    return body


def _message(role: str, text: str) -> dict:
    block = "output_text" if role == "assistant" else "input_text"
    return {"type": "message", "role": role, "content": [{"type": block, "text": text}]}


async def _worker_gone(manager: HarnessProcessManager) -> None:
    import asyncio

    entry = manager._entries.get(CONV)
    assert entry is not None
    await asyncio.wait_for(entry.process.wait(), timeout=20)


@pytest.mark.asyncio
async def test_idle_exit_releases_the_worker_and_the_next_message_resumes_the_cloud_session(
    short_parent: Path, tmp_path: Path
) -> None:
    agent = tmp_path / "bridge.py"
    agent.write_text(_BRIDGE)
    log = tmp_path / "log.jsonl"
    env = {
        "HARNESS_ACP_COMMAND": f"{sys.executable} {agent} {log} {tmp_path / 'state.json'}",
        "HARNESS_ACP_NAME": "Cloud",
        "HARNESS_ACP_OMNIGENT_MCP": "false",
        "HARNESS_ACP_CWD": str(tmp_path),
        "TMPDIR": str(tmp_path),  # the idle handoff lands here, not in the shared temp dir
    }
    manager = HarnessProcessManager(tmp_parent=short_parent)
    await manager.start()
    try:
        client = await manager.get_client(CONV, "acp", env=env)
        first = await _turn(client, [{"type": "input_text", "text": "hi"}])
        match = re.search(r"cloud session: https://claude.ai/code/(\w+)", first)
        assert match, first
        cloud = match.group(1)

        await _worker_gone(manager)  # the idle exit ended the worker with the bridge

        replay = [
            _message("user", "hi"),
            _message("assistant", MARKER + cloud),
            _message("user", "again"),
        ]
        client = await manager.get_client(CONV, "acp", env=env)  # respawns the worker
        await _turn(client, replay)
    finally:
        await manager.shutdown()

    prompts = [e for e in map(json.loads, log.read_text().splitlines()) if e[0] == "prompt"]
    assert [p[1] for p in prompts] == [cloud, cloud]


@pytest.mark.asyncio
async def test_archive_after_an_idle_exit_still_tells_the_cloud_session(
    short_parent: Path, tmp_path: Path
) -> None:
    agent = tmp_path / "bridge.py"
    agent.write_text(_BRIDGE)
    log = tmp_path / "log.jsonl"
    env = {
        "HARNESS_ACP_COMMAND": f"{sys.executable} {agent} {log} {tmp_path / 'state.json'}",
        "HARNESS_ACP_NAME": "Cloud",
        "HARNESS_ACP_OMNIGENT_MCP": "false",
        "HARNESS_ACP_CWD": str(tmp_path),
        "TMPDIR": str(tmp_path),  # the idle handoff lands here, not in the shared temp dir
    }
    manager = HarnessProcessManager(tmp_parent=short_parent)
    await manager.start()
    try:
        client = await manager.get_client(CONV, "acp", env=env)
        first = await _turn(client, [{"type": "input_text", "text": "hi"}])
        cloud = re.search(r"cloud session: https://claude.ai/code/(\w+)", first).group(1)  # type: ignore[union-attr]
        await _worker_gone(manager)

        # What the runner does for session_ended when no worker is live: start one and tell it.
        client = await manager.get_client(CONV, "acp", env=env)
        response = await client.post(f"/v1/sessions/{CONV}/events", json={"type": "session_ended"})
        assert response.status_code < 400
    finally:
        await manager.shutdown()

    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert ["archive", cloud] in events


# Holds the worker in its shutdown window: the idle exit has marked it as ending, its socket is
# still open, and it does not go until the test lets it (it stops itself where it would signal).
_HOLD = (
    "import os, signal, threading\n"
    "stop = lambda: os.kill(os.getpid(), signal.SIGSTOP)\n"
    "real, held = signal.raise_signal, []\n"
    "def hold(sig):\n"
    "    if held:\n"
    "        real(sig)  # uvicorn re-raises the signal it caught once it has shut down\n"
    "    else:\n"
    "        held.append(sig)\n"
    "        threading.Timer(1.0, stop).start()\n"
    "signal.raise_signal = hold\n"
)


async def _into_the_shutdown_window(
    manager: HarnessProcessManager, tmp_path: Path, env: dict[str, str]
) -> tuple[str, int]:
    """Run the first turn and return its cloud session id, with the worker held ending."""
    import asyncio

    hold = tmp_path / "hold"
    hold.mkdir()
    (hold / "sitecustomize.py").write_text(_HOLD)
    env["PYTHONPATH"] = str(hold)
    client = await manager.get_client(CONV, "acp", env=env)
    first = await _turn(client, [{"type": "input_text", "text": "hi"}])
    cloud = re.search(r"cloud session: https://claude.ai/code/(\w+)", first).group(1)  # type: ignore[union-attr]
    entry = manager._entries[CONV]
    for _ in range(200):
        if entry.ending_file_exists():
            break
        await asyncio.sleep(0.05)
    assert entry.ending_file_exists()
    await asyncio.sleep(1.5)  # the worker has stopped itself where it would have signalled
    assert entry.process.returncode is None
    return cloud, entry.process.pid


async def _let_the_worker_go(manager: HarnessProcessManager, pid: int) -> None:
    import asyncio
    import os
    import signal

    entry = manager._entries[CONV]
    os.kill(pid, signal.SIGCONT)
    os.kill(pid, signal.SIGTERM)
    await asyncio.wait_for(entry.process.wait(), timeout=20)


def _env(tmp_path: Path, agent: Path, log: Path) -> dict[str, str]:
    return {
        "HARNESS_ACP_COMMAND": f"{sys.executable} {agent} {log} {tmp_path / 'state.json'}",
        "HARNESS_ACP_NAME": "Cloud",
        "HARNESS_ACP_OMNIGENT_MCP": "false",
        "HARNESS_ACP_CWD": str(tmp_path),
        "TMPDIR": str(tmp_path),
    }


@pytest.mark.asyncio
async def test_archive_in_the_shutdown_window_still_tells_the_cloud_session(
    short_parent: Path, tmp_path: Path
) -> None:
    import asyncio

    from omnigent.runtime.harnesses.process_manager import NoLiveHarnessError

    agent = tmp_path / "bridge.py"
    agent.write_text(_BRIDGE)
    log = tmp_path / "log.jsonl"
    env = _env(tmp_path, agent, log)
    manager = HarnessProcessManager(tmp_parent=short_parent)
    await manager.start()
    try:
        cloud, pid = await _into_the_shutdown_window(manager, tmp_path, env)

        # The runner's archive path asks for any live worker: the held one must not count.
        lookup = asyncio.ensure_future(manager.get_client(CONV, "any"))
        await asyncio.sleep(0.5)
        assert not lookup.done()  # it waits for the worker's exit
        await _let_the_worker_go(manager, pid)
        with pytest.raises(NoLiveHarnessError):
            await lookup

        client = await manager.get_client(CONV, "acp", env=env)  # the idle-worker fallback
        response = await client.post(f"/v1/sessions/{CONV}/events", json={"type": "session_ended"})
        assert response.status_code < 400
    finally:
        await manager.shutdown()

    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert ["archive", cloud] in events


@pytest.mark.asyncio
async def test_a_message_in_the_shutdown_window_is_answered_by_the_respawned_worker(
    short_parent: Path, tmp_path: Path
) -> None:
    import asyncio

    agent = tmp_path / "bridge.py"
    agent.write_text(_BRIDGE)
    log = tmp_path / "log.jsonl"
    env = _env(tmp_path, agent, log)
    manager = HarnessProcessManager(tmp_parent=short_parent)
    await manager.start()
    try:
        cloud, pid = await _into_the_shutdown_window(manager, tmp_path, env)

        next_turn = asyncio.ensure_future(manager.get_client(CONV, "acp", env=env))
        await asyncio.sleep(0.5)
        assert not next_turn.done()  # not handed the worker that refuses new turns
        await _let_the_worker_go(manager, pid)
        client = await next_turn
        assert manager._entries[CONV].process.pid != pid
        await _turn(
            client,
            [_message("user", "hi"), _message("assistant", MARKER + cloud), _message("user", "x")],
        )
    finally:
        await manager.shutdown()

    prompts = [e for e in map(json.loads, log.read_text().splitlines()) if e[0] == "prompt"]
    assert [p[1] for p in prompts] == [cloud, cloud]
