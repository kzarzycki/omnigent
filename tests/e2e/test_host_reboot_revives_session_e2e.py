"""E2E: a session mid-turn is revived after a full host reboot.

A reboot kills the host daemon and every runner it started. When the daemon
comes back it has no runner for the session; the server must launch a new one
and resume the cut turn without any input from the user.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import signal
import sys
from pathlib import Path

import httpx
import pytest

from tests._helpers.host import isolated_host
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import server_runner
from tests._helpers.session import post_session_bundle, send_user_text
from tests.e2e.test_acp_turn_survives_server_restart_e2e import (
    _AGENT,
    _launcher_bundle,
    _session,
    _transcript,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="host reboot e2e finds the host's runner processes through /proc",
)

_TIMEOUT_S = 90.0


def _pids_with_env(marker: str) -> list[int]:
    """Return pids (except ours) whose environment mentions ``marker``."""
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if marker.encode() in (entry / "environ").read_bytes():
                pids.append(int(entry.name))
        except OSError:
            continue
    return pids


def _simulate_reboot(host_home: Path, daemon_pid: int) -> None:
    """Kill the daemon and everything it started, as a power cycle does."""
    victims = [pid for pid in _pids_with_env(str(host_home)) if pid != daemon_pid]
    for pid in [daemon_pid, *victims]:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    poll_until(lambda: not _pids_with_env(str(host_home)), timeout_s=30)


@pytest.mark.timeout(420)
def test_session_revives_after_host_reboot(tmp_path: Path) -> None:
    """The mid-turn session runs again on a new runner after the host comes back."""
    record = tmp_path / "agent"
    script = tmp_path / "fixture_acp_agent.py"
    script.write_text(_AGENT)
    command = shlex.join([sys.executable, str(script), str(record)])
    acp_config = {"acp": {"agents": [{"name": "Fixture", "command": command}]}}
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with (
        server_runner(tmp_path / "stack", poll_interval=0.2) as stack,
        httpx.Client(base_url=stack.base_url, trust_env=False, timeout=30.0) as client,
    ):
        with isolated_host(tmp_path / "host1", stack.base_url, config=acp_config) as host:
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
            first_runner = _session(client, session_id)["runner_id"]
            host_id = host.host_id

            _simulate_reboot(host.home, host.proc.pid)
            Path(f"{record}.started").unlink()

        with isolated_host(
            tmp_path / "host2", stack.base_url, config=acp_config, host_id=host_id
        ) as rebooted:
            rebooted.wait_online(client, timeout=_TIMEOUT_S, poll_interval=0.2)
            # No user input from here on: the server must relaunch by itself.
            # The server holds the turn for the host, then gives its runner one more
            # grace (~90 s) before it relaunches.
            poll_until(Path(f"{record}.started").exists, timeout_s=240, interval_s=0.5)
            Path(f"{record}.release").touch()
            poll_until(
                lambda: _session(client, session_id).get("status") == "idle",
                timeout_s=_TIMEOUT_S,
                interval_s=0.2,
            )
            assert _session(client, session_id)["runner_id"] != first_runner
            assert "after restart" in str(_transcript(client, session_id)), stack.log_tail()
