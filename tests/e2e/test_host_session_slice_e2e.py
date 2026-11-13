"""E2E: session processes run in memory-limited user-systemd slices.

A real server and host daemon, with the mock LLM driving ``sys_terminal_launch``
(a tmux pane) and ``sys_os_shell`` (a memory hog). Proves without sudo that:

- with ``host.session_slice`` on, the runner, its harness and its tmux pane are
  in the slice while the host daemon is not; a session allocating past the
  slice's ``MemoryMax`` is OOM-killed while a process outside the slice
  allocating as much at the same time, and the host daemon, survive;
- with ``os_env.sandbox.resources`` (on ``type: none``), the session gets its
  own slice nested in the host slice, and its own ``MemoryMax`` kills it first.

Needs a Linux user systemd manager that delegates the memory controller::

    pytest tests/e2e/test_host_session_slice_e2e.py -v -s
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.util.user_systemd import missing_user_manager_controllers
from tests._helpers.host import Host, isolated_host
from tests.e2e.conftest import (
    POLL_INTERVAL_S,
    configure_mock_llm,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or missing_user_manager_controllers(["memory"]) != [],
    reason="needs a user systemd manager that delegates the memory controller",
)

_MIB = 1024 * 1024
# Each hog touches every page, so the whole size is charged to its cgroup.
_HOG = (
    "import time; b = b'x' * ({mib} * 1024 * 1024); "
    "print('allocated', flush=True); time.sleep({hold})"
)


@pytest.fixture
def host_slice() -> Iterator[str]:
    """A slice name unique to this test, removed with its runtime limits afterwards."""
    # No dash: a dashed name would nest under a shared parent slice left behind.
    name = f"omnigente2e{uuid.uuid4().hex[:8]}.slice"
    try:
        yield name
    finally:
        subprocess.run(["systemctl", "--user", "stop", name], check=False, capture_output=True)
        subprocess.run(["systemctl", "--user", "revert", name], check=False, capture_output=True)


def _cgroup_of(pid: int) -> str:
    return Path(f"/proc/{pid}/cgroup").read_text().strip().split("::", 1)[1]


def _slice_dir(slice_name: str) -> Path:
    control_group = subprocess.run(
        ["systemctl", "--user", "show", "--property=ControlGroup", "--value", slice_name],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert control_group, f"{slice_name} has no cgroup"
    return Path("/sys/fs/cgroup") / control_group.lstrip("/")


def _oom_kills(cgroup_dir: Path) -> int:
    events = dict(line.split() for line in (cgroup_dir / "memory.events").read_text().splitlines())
    return int(events["oom_kill"])


def _processes_under(cgroup_dir: Path) -> dict[int, str]:
    """Map every pid in *cgroup_dir*'s subtree to its command name."""
    found: dict[int, str] = {}
    for procs in cgroup_dir.rglob("cgroup.procs"):
        for line in procs.read_text().split():
            pid = int(line)
            try:
                found[pid] = Path(f"/proc/{pid}/comm").read_text().strip()
            except OSError:
                continue
    return found


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    except OSError:
        return ""


def _start_host(tmp_path: Path, live_server: str, mock_llm: str, settings: dict[str, Any]) -> Any:
    return isolated_host(
        tmp_path / "host",
        live_server,
        env={"OPENAI_BASE_URL": f"{mock_llm}/v1", "OPENAI_API_KEY": "mock-key"},
        config={"host": {"session_slice": settings}},
    )


def _register_agent(
    client: httpx.Client, mock_llm: str, sandbox: dict[str, Any]
) -> tuple[str, str]:
    slug = uuid.uuid4().hex[:6]
    model = f"mock-session-slice-{slug}"
    agent_name = register_inline_agent(
        client,
        name=f"session-slice-{slug}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="Call exactly the tools requested.",
        mock_llm_base_url=f"{mock_llm}/v1",
        extra_config={
            "os_env": {"type": "caller_process", "cwd": ".", "sandbox": sandbox},
            "terminals": {"bash": {"command": "bash", "os_env": "inherit"}},
        },
    )
    rows = client.get(
        "/v1/sessions", params={"visibility": "all", "agent_name": agent_name, "limit": 1}
    ).json()["data"]
    assert rows, f"agent {agent_name!r} not registered"
    return str(rows[0]["agent_id"]), model


def _launch_session(
    client: httpx.Client, host: Host, agent_id: str, workspace: Path
) -> tuple[str, int]:
    """Create a session on *host*; return its id and its runner's pid."""
    session_id = str(client.post("/v1/sessions", json={"agent_id": agent_id}).json()["id"])
    launch = client.post(
        f"/v1/hosts/{host.host_id}/runners",
        json={"session_id": session_id, "workspace": str(workspace)},
        timeout=90.0,
    )
    assert launch.status_code == 200, launch.text[:500]
    runner_id = launch.json()["runner_id"]
    deadline = time.monotonic() + 60
    while client.get(f"/v1/runners/{runner_id}/status").json().get("online") is not True:
        assert time.monotonic() < deadline, f"runner {runner_id} never came online"
        time.sleep(POLL_INTERVAL_S)
    client.patch(f"/v1/sessions/{session_id}", json={"runner_id": runner_id}).raise_for_status()
    # The host logs each zygote fork; a fresh host forks only this runner.
    forks = re.findall(
        r"Forked runner via zygote \(zygote pid=\d+, runner pid=(\d+)\)", host.log_path.read_text()
    )
    assert len(forks) == 1, f"expected one forked runner, got {forks}"
    return session_id, int(forks[0])


def _turn(
    client: httpx.Client, mock_llm: str, model: str, session_id: str, tool: str, args: dict
) -> str:
    """Run one turn that makes a single tool call; return the tool's output."""
    configure_mock_llm(
        mock_llm,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call_{uuid.uuid4().hex[:8]}",
                        "name": tool,
                        "arguments": json.dumps(args),
                    }
                ]
            },
            {"text": "Done."},
        ],
        key=model,
    )
    response_id = send_user_message_to_session(
        client, session_id=session_id, content=f"run {tool}"
    )
    poll_session_until_terminal(
        client, session_id=session_id, response_id=response_id, timeout=180
    )
    items = client.get(f"/v1/sessions/{session_id}").json().get("items", [])
    return "\n".join(
        str((item.get("data") or {}).get("output", ""))
        for item in items
        if item.get("type") == "function_call_output"
    )


def _outside_hog(mib: int, hold_s: int) -> subprocess.Popen[str]:
    """Allocate *mib* outside any Omnigent slice and hold it for *hold_s*."""
    return subprocess.Popen(
        [sys.executable, "-c", _HOG.format(mib=mib, hold=hold_s) + "; print('survived')"],
        stdout=subprocess.PIPE,
        text=True,
    )


def test_host_slice_kills_the_session_and_spares_the_rest(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
    host_slice: str,
) -> None:
    reset_mock_llm(mock_llm_server_url)
    settings = {"enabled": True, "name": host_slice, "memory_max": "700M", "memory_swap_max": "0"}
    with _start_host(tmp_path, live_server, mock_llm_server_url, settings) as host:
        host.wait_online(http_client, timeout=45)
        agent_id, model = _register_agent(
            http_client, mock_llm_server_url, {"type": "none", "allow_network": True}
        )
        session_id, runner_pid = _launch_session(http_client, host, agent_id, tmp_path)
        _turn(
            http_client,
            mock_llm_server_url,
            model,
            session_id,
            "sys_terminal_launch",
            {"terminal": "bash", "session": "pane"},
        )

        slice_dir = _slice_dir(host_slice)
        in_slice = _processes_under(slice_dir)
        print(
            f"PROOF slice={host_slice} memory.max={(slice_dir / 'memory.max').read_text().strip()}"
        )
        print(f"PROOF processes in slice: {sorted(set(in_slice.values()))}")
        assert runner_pid in in_slice, f"runner {_cgroup_of(runner_pid)} is outside the slice"
        assert "tmux: server" in in_slice.values() and "bash" in in_slice.values(), in_slice
        assert host.proc.pid not in in_slice
        assert f"/{host_slice}/" not in _cgroup_of(host.proc.pid)

        kills_before = _oom_kills(slice_dir)
        baseline_mib = int((slice_dir / "memory.current").read_text()) // _MIB
        print(f"PROOF baseline memory.current={baseline_mib}M")
        outside = _outside_hog(900, hold_s=20)
        output = _turn(
            http_client,
            mock_llm_server_url,
            model,
            session_id,
            "sys_os_shell",
            {"command": f'python3 -c "{_HOG.format(mib=900, hold=5)}"; echo exit=$?'},
        )
        outside_stdout, _ = outside.communicate(timeout=60)
        kills_after = _oom_kills(slice_dir)
        print(
            f"PROOF oom_kill before={kills_before} after={kills_after}; session output={output!r}"
        )
        print(f"PROOF outside hog exit={outside.returncode} stdout={outside_stdout!r}")
        assert kills_after > kills_before
        assert "exit=137" in output
        assert outside.returncode == 0 and "survived" in outside_stdout
        assert host.proc.poll() is None, "the host daemon died"


def test_session_resources_limit_the_session_inside_the_host_slice(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
    host_slice: str,
) -> None:
    reset_mock_llm(mock_llm_server_url)
    settings = {"enabled": True, "name": host_slice, "memory_max": "2G"}
    with _start_host(tmp_path, live_server, mock_llm_server_url, settings) as host:
        host.wait_online(http_client, timeout=45)
        sandbox = {"type": "none", "allow_network": True, "resources": {"memory_max": "500M"}}
        agent_id, model = _register_agent(http_client, mock_llm_server_url, sandbox)
        session_id, runner_pid = _launch_session(http_client, host, agent_id, tmp_path)
        _turn(
            http_client,
            mock_llm_server_url,
            model,
            session_id,
            "sys_terminal_launch",
            {"terminal": "bash", "session": "pane"},
        )

        runner_cgroup = _cgroup_of(runner_pid)
        session_slice = runner_cgroup.rstrip("/").split("/")[-2]
        print(f"PROOF runner cgroup={runner_cgroup}")
        assert f"/{host_slice}/{session_slice}/" in runner_cgroup + "/"
        assert session_slice.startswith(host_slice.removesuffix(".slice") + "-")
        session_dir = _slice_dir(session_slice)
        assert (session_dir / "memory.max").read_text().strip() == str(500 * _MIB)
        in_session = _processes_under(session_dir)
        assert "tmux: server" in in_session.values() and "bash" in in_session.values(), in_session
        # The harness is exec'd from the limited runner, not forked by the host's zygote.
        harnesses = [pid for pid in in_session if "harnesses._runner" in _cmdline(pid)]
        print(f"PROOF harness pids in session slice: {harnesses}")
        assert harnesses, {pid: _cmdline(pid) for pid in in_session}

        output = _turn(
            http_client,
            mock_llm_server_url,
            model,
            session_id,
            "sys_os_shell",
            {"command": f'python3 -c "{_HOG.format(mib=800, hold=2)}"; echo exit=$?'},
        )
        print(
            f"PROOF session oom_kill={_oom_kills(session_dir)} "
            f"host slice oom_kill={_oom_kills(_slice_dir(host_slice))} output={output!r}"
        )
        assert _oom_kills(session_dir) >= 1
        assert "exit=137" in output
        assert host.proc.poll() is None
