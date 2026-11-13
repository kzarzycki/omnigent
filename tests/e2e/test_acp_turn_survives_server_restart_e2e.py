"""E2E: an ``acp:<slug>`` turn keeps running across a server restart.

The ACP agent runs under the host-launched runner, not the server. A restart
drops only the runner's tunnel: the runner reconnects, the server attaches a new
relay, and the rest of the turn reaches the session. Deploy tooling relies on
this to restart the server without waiting for running ``acp`` turns.

Journey: a host launches a runner for an ``acp:fixture`` session, the fixture
agent streams text and calls an Omnigent tool, the server restarts mid-turn,
then the agent calls the tool again and finishes. The same agent process
completes the turn, nothing is cancelled, and each item is stored once.
"""

from __future__ import annotations

import gzip
import io
import shlex
import sys
import tarfile
import time
from pathlib import Path

import httpx
import pytest

from tests._helpers.host import isolated_host
from tests._helpers.live_server import terminate_process
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import server_runner
from tests._helpers.session import post_session_bundle, send_user_text

# Streams text, calls an Omnigent tool through its MCP relay, waits for the
# release file, then calls the tool again and ends the turn.
_AGENT = r"""
import json, os, subprocess, sys
from pathlib import Path
record = Path(sys.argv[1])
def send(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
def say(sid, text):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid,
          "update": {"sessionUpdate": "agent_message_chunk",
                     "content": {"type": "text", "text": text}}}})
class Mcp:
    def __init__(self, spec):
        env = {**os.environ, **{e["name"]: e["value"] for e in spec.get("env", [])}}
        self.proc = subprocess.Popen([spec["command"], *spec.get("args", [])], env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.next_id = 0
        self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": "fixture", "version": "0"}})
        self.write({"jsonrpc": "2.0", "method": "notifications/initialized"})
    def write(self, o):
        self.proc.stdin.write(json.dumps(o) + "\n"); self.proc.stdin.flush()
    def rpc(self, method, params):
        self.next_id += 1
        self.write({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        while (reply := json.loads(self.proc.stdout.readline())).get("id") != self.next_id:
            pass
        return reply
    def list_sessions(self):
        result = self.rpc("tools/call", {"name": "sys_session_list", "arguments": {}})["result"]
        return "tool failed" if result.get("isError") else "tool ok"
servers = []
for line in sys.stdin:
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        result = {"protocolVersion": 1, "agentCapabilities": {}}
        send({"jsonrpc": "2.0", "id": mid, "result": result})
    elif method == "session/new":
        servers = msg["params"]["mcpServers"]
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "fixture-session"}})
    elif method == "session/prompt":
        sid = msg["params"]["sessionId"]
        if Path(str(record) + ".finished").exists():
            say(sid, "follow-up done.")
            send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
            continue
        relay = Mcp(next(s for s in servers if s["name"] == "omnigent"))
        say(sid, "before restart: " + relay.list_sessions() + ". ")
        Path(str(record) + ".started").write_text(str(os.getpid()))
        while not Path(str(record) + ".release").exists():
            say(sid, "")
            __import__("time").sleep(0.2)
        if Path(str(record) + ".offline").exists():
            say(sid, "finished offline.")
        else:
            say(sid, "after restart: " + relay.list_sessions() + ".")
        Path(str(record) + ".finished").write_text(str(os.getpid()))
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
    elif method == "session/cancel":
        Path(str(record) + ".cancelled").touch()
"""

_TIMEOUT_S = 90.0
# Long enough for the follow-up turn to start, and need the server, while it is down.
_OUTAGE_S = 8.0


def _launcher_bundle(command: str) -> bytes:
    """Gzip-tar the launcher spec ``omni run --harness acp:fixture`` generates."""
    from omnigent.cli import _materialize_harness_launcher_file
    from omnigent.onboarding.acp_auth import AcpAgentEntry

    launcher = _materialize_harness_launcher_file(
        harness="acp:fixture",
        model=None,
        system_prompt=None,
        acp_agent=AcpAgentEntry(slug="fixture", name="Fixture", command=command),
    )
    data = launcher.read_bytes()
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        info = tarfile.TarInfo(name=launcher.name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _session(client: httpx.Client, session_id: str) -> dict:
    response = client.get(f"/v1/sessions/{session_id}")
    response.raise_for_status()
    return response.json()


def _transcript(client: httpx.Client, session_id: str) -> list[str]:
    """Return stored items as ``<role>: <text>`` rows, or the item type."""
    response = client.get(
        f"/v1/sessions/{session_id}/items", params={"limit": 100, "order": "asc"}
    )
    response.raise_for_status()
    rows = []
    for item in response.json()["data"]:
        if item.get("type") == "message":
            text = "".join(part.get("text", "") for part in item.get("content", []))
            rows.append(f"{item['role']}: {text}")
        elif item.get("type") != "resource_event":
            rows.append(item["type"])
    return rows


@pytest.mark.timeout(300)
def test_acp_turn_survives_server_restart(tmp_path: Path) -> None:
    """The running acp turn completes in the same agent process after a restart."""
    record = tmp_path / "agent"
    script = tmp_path / "fixture_acp_agent.py"
    script.write_text(_AGENT)
    command = shlex.join([sys.executable, str(script), str(record)])
    acp_config = {"agents": [{"name": "Fixture", "command": command}]}
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with (
        server_runner(tmp_path / "stack", poll_interval=0.2) as stack,
        httpx.Client(base_url=stack.base_url, trust_env=False, timeout=30.0) as client,
        isolated_host(tmp_path / "host", stack.base_url, config={"acp": acp_config}) as host,
    ):
        host.wait_online(client, timeout=_TIMEOUT_S, poll_interval=0.2)
        created = post_session_bundle(
            client.post,
            "/v1/sessions",
            _launcher_bundle(command),
            metadata={"host_id": host.host_id, "workspace": str(workspace)},
        )
        created.raise_for_status()
        session_id = created.json()["session_id"]
        poll_until(lambda: _session(client, session_id).get("runner_id"), timeout_s=_TIMEOUT_S)
        send_user_text(client, session_id=session_id, text="hello")
        started = Path(f"{record}.started")
        poll_until(started.exists, timeout_s=_TIMEOUT_S, interval_s=0.2)

        stack.restart_server()
        runner_id = _session(client, session_id)["runner_id"]
        poll_until(
            lambda: client.get(f"/v1/runners/{runner_id}/status").json().get("online") is True,
            timeout_s=_TIMEOUT_S,
            interval_s=0.2,
        )
        Path(f"{record}.release").touch()
        poll_until(
            lambda: _session(client, session_id).get("status") == "idle",
            timeout_s=_TIMEOUT_S,
            interval_s=0.2,
        )
        # Items can land just after the idle status; wait for the last one.
        deadline = time.monotonic() + 10.0
        while "after restart" not in str(_transcript(client, session_id)):
            assert time.monotonic() < deadline, _transcript(client, session_id)
            time.sleep(0.2)

        assert Path(f"{record}.finished").read_text() == started.read_text()
        assert not Path(f"{record}.cancelled").exists()
        assert _transcript(client, session_id) == [
            "user: hello",
            "function_call",
            "function_call_output",
            "assistant: before restart: tool ok. ",
            "function_call",
            "function_call_output",
            "assistant: after restart: tool ok.",
        ], stack.log_tail()


@pytest.mark.timeout(300)
def test_acp_follow_up_turn_started_while_server_is_down(tmp_path: Path) -> None:
    """A follow-up turn that must fetch its spec during the outage waits for the server.

    The agent bundle is re-uploaded mid-turn, so the queued follow-up carries a
    new revision and the runner must fetch the spec again. The first turn ends
    while the server is down, which starts the follow-up then. It completes once
    the server is back, and its reply is stored once.
    """
    record = tmp_path / "agent"
    script = tmp_path / "fixture_acp_agent.py"
    script.write_text(_AGENT)
    command = shlex.join([sys.executable, str(script), str(record)])
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with (
        server_runner(tmp_path / "stack", poll_interval=0.2) as stack,
        httpx.Client(base_url=stack.base_url, trust_env=False, timeout=30.0) as client,
        isolated_host(
            tmp_path / "host",
            stack.base_url,
            config={"acp": {"agents": [{"name": "Fixture", "command": command}]}},
        ) as host,
    ):
        host.wait_online(client, timeout=_TIMEOUT_S, poll_interval=0.2)
        created = post_session_bundle(
            client.post,
            "/v1/sessions",
            _launcher_bundle(command),
            metadata={"host_id": host.host_id, "workspace": str(workspace)},
        )
        created.raise_for_status()
        session_id = created.json()["session_id"]
        poll_until(lambda: _session(client, session_id).get("runner_id"), timeout_s=_TIMEOUT_S)
        send_user_text(client, session_id=session_id, text="hello")
        poll_until(Path(f"{record}.started").exists, timeout_s=_TIMEOUT_S, interval_s=0.2)
        client.put(
            f"/v1/sessions/{session_id}/agent",
            files={"bundle": ("agent.tar.gz", _launcher_bundle(command), "application/gzip")},
        ).raise_for_status()
        send_user_text(client, session_id=session_id, text="and then")

        terminate_process(stack.server)
        Path(f"{record}.offline").touch()
        Path(f"{record}.release").touch()
        time.sleep(_OUTAGE_S)
        stack.start_server()
        poll_until(
            lambda: _transcript(client, session_id)[-1:] == ["assistant: follow-up done."],
            timeout_s=_TIMEOUT_S,
            interval_s=0.2,
        )
        # A duplicate turn would add its reply shortly after the first.
        time.sleep(3)  # custom-lint: disable=sleep-then-assert -- duplicate-turn window

        assert _session(client, session_id).get("status") == "idle", stack.log_tail()
        assert _transcript(client, session_id) == [
            "user: hello",
            "function_call",
            "function_call_output",
            "user: and then",
            "assistant: before restart: tool ok. finished offline.",
            "assistant: follow-up done.",
        ]
        # A failed follow-up can be hidden by a later rerun; the runner logs each failure.
        failures = [
            line
            for log in (host.home / ".omnigent" / "logs").rglob("*.log")
            for line in log.read_text(errors="replace").splitlines()
            if "turn surfaced to UI as failed" in line
        ]
        assert failures == []
