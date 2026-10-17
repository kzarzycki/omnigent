"""E2E: one process's death on the host must not strand unattended sessions.

Every runner on a host is forked by one zygote. Two journeys, against a real
server + host daemon + zygote + runners and the mock LLM:

- the zygote dies (SIGTERM or SIGKILL): its runners must stay up;
- every runner is SIGTERMed at once while a watched session is mid-turn: the
  watched session's failure notice must revive its subscriber without a
  person opening it.

    pytest tests/e2e/test_host_zygote_death_e2e.py -v
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import httpx
import psutil
import pytest

from tests.e2e.conftest import configure_mock_llm, lookup_agent_id, upload_agent
from tests.e2e.test_host_e2e import (
    _pid_alive,
    _spawn_host_daemon,
    _wait_for_host_online,
    _write_smoke_agent_yaml,
)
from tests.e2e.test_host_runner_leak_5182 import _launches, _runner_online, _wait_for

pytestmark = pytest.mark.timeout(600)


def _zygote_pid(daemon_pid: int) -> int:
    """Return the scratch daemon's zygote pid (a direct child of that daemon)."""
    for child in psutil.Process(daemon_pid).children():
        if "omnigent.runner._zygote" in " ".join(child.cmdline()):
            return child.pid
    raise AssertionError("no zygote child under the scratch daemon")


def _start_host_sessions(
    count: int,
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
    monkeypatch: pytest.MonkeyPatch,
):  # type: ignore[no-untyped-def]
    """Spawn a scratch daemon and *count* host-bound sessions with live runners."""
    # The daemon reads its seeded host id from here, not $HOME/.omnigent.
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / ".omnigent"))
    daemon = _spawn_host_daemon(
        tmp_path=tmp_path, live_server=live_server, mock_llm_server_url=mock_llm_server_url
    )
    try:
        return daemon, *_open_sessions(count, daemon, http_client, tmp_path)
    except BaseException:
        # Callers own the daemon only once this returns.
        daemon.proc.terminate()
        daemon.proc.wait(timeout=30)
        raise


def _open_sessions(  # type: ignore[no-untyped-def]
    count: int, daemon, http_client: httpx.Client, tmp_path: Path
):
    """Open *count* sessions on *daemon* and wait until each runner is online."""
    _wait_for_host_online(http_client, daemon.host_id, timeout=30.0)
    agent_id = lookup_agent_id(
        http_client, upload_agent(http_client, _write_smoke_agent_yaml(tmp_path))
    )
    session_ids = []
    for n in range(count):
        ws = tmp_path / f"ws{n}"
        ws.mkdir()
        resp = http_client.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "host_id": daemon.host_id, "workspace": str(ws)},
            timeout=90.0,
        )
        resp.raise_for_status()
        session_ids.append(resp.json()["id"])
    runners = _wait_for(
        lambda: (lambda ls: ls if len(ls) == count else None)(_launches(daemon.daemon_log)),
        timeout=60.0,
        what=f"{count} runner launches",
    )
    for rid, _pid in runners:
        _wait_for(lambda rid=rid: _runner_online(http_client, rid), timeout=90.0, what=rid)
    return session_ids, runners


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL], ids=["TERM", "KILL"])
def test_runners_outlive_their_zygote(
    sig: int,
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead zygote leaves its runners alive and connected to the server."""
    configure_mock_llm(mock_llm_server_url, [{"text": "OK"}])
    daemon, _sessions, runners = _start_host_sessions(
        2, live_server, http_client, tmp_path, mock_llm_server_url, monkeypatch
    )
    try:
        zpid = _zygote_pid(daemon.proc.pid)
        assert psutil.Process(zpid).ppid() == daemon.proc.pid
        os.kill(zpid, sig)
        _wait_for(lambda: not _pid_alive(zpid) or None, timeout=10.0, what="the zygote to die")
        # Longer than the runner watchdog's poll and its 2 s hard-exit grace.
        time.sleep(8)
        dead = [(rid, pid) for rid, pid in runners if not _pid_alive(pid)]
        log = daemon.daemon_log.read_text(errors="replace")
        assert not dead, f"zygote {sig!r} took runners {dead} down with it:\n{log[-4000:]}"
        assert "died unexpectedly" not in log
        for rid, _pid in runners:
            assert _runner_online(http_client, rid), f"{rid} lost its tunnel"
    finally:
        daemon.proc.terminate()
        daemon.proc.wait(timeout=30)
    for _rid, pid in runners:
        _wait_for(lambda pid=pid: not _pid_alive(pid) or None, timeout=30.0, what=f"{pid} exit")


def test_failure_notice_revives_a_subscriber_whose_runner_died(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every runner SIGTERMed at once: the watched session's failure wakes its subscriber."""
    configure_mock_llm(
        mock_llm_server_url, [{"text": "never finishes", "delay": 300}], match="SLOW_TURN"
    )
    configure_mock_llm(mock_llm_server_url, [{"text": "LEAD_WOKE"}], match="ended its turn")
    daemon, (lead, lane), runners = _start_host_sessions(
        2, live_server, http_client, tmp_path, mock_llm_server_url, monkeypatch
    )
    try:
        http_client.post(
            f"/v1/sessions/{lead}/subscriptions", json={"session_id": lane}
        ).raise_for_status()
        http_client.post(
            f"/v1/sessions/{lane}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "SLOW_TURN"}]},
            },
            timeout=60.0,
        ).raise_for_status()
        _wait_for(
            lambda: (
                http_client.get(f"/v1/sessions/{lane}").json().get("status") == "running" or None
            ),
            timeout=60.0,
            what="the lane to be mid-turn",
        )
        for _rid, pid in runners:
            assert psutil.Process(pid).ppid() == _zygote_pid(daemon.proc.pid)
            os.kill(pid, signal.SIGTERM)

        def _lead_woke() -> bool | None:
            items = http_client.get(f"/v1/sessions/{lead}/items").json().get("data", [])
            return "LEAD_WOKE" in str(items) or None

        _wait_for(_lead_woke, timeout=180.0, what="the lane's failure notice to revive the lead")
        relaunched = [
            p for _r, p in _launches(daemon.daemon_log) if p not in dict(runners).values()
        ]
        assert relaunched, "the lead answered without a relaunched runner"
    finally:
        daemon.proc.terminate()
        daemon.proc.wait(timeout=30)
