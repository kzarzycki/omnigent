"""The host daemon replaces idle adopted runners that run an older build."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
import yaml

from omnigent.host import stale_runners
from omnigent.host.connect import HostProcess, _RunnerHandle
from omnigent.host.frames import (
    REPLACE_STALE_RUNNER_BUSY,
    REPLACE_STALE_RUNNER_FAILED,
    REPLACE_STALE_RUNNER_REPLACED,
    SERVER_CAP_REPLACE_STALE_RUNNER,
    HostReplaceStaleRunnerFrame,
    HostReplaceStaleRunnerResultFrame,
    decode_host_frame,
)
from omnigent.host.identity import HostIdentity
from omnigent.native import owner_claim

HOST_COMMIT = "new111111"
RUNNER_COMMIT = "old000000"


class _FakeRunnerProc:
    """A runner process the daemon adopted; alive until ``returncode`` is set."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


class _FakeServer:
    """Answers each ``host.replace_stale_runner`` by session id, recording requests."""

    def __init__(self, host: HostProcess, answers: dict[str, str]) -> None:
        self.host = host
        self.answers = answers
        self.requests: list[HostReplaceStaleRunnerFrame] = []

    async def send_text(self, ws: object, text: str) -> None:
        frame = decode_host_frame(text)
        assert isinstance(frame, HostReplaceStaleRunnerFrame)
        self.requests.append(frame)
        result = HostReplaceStaleRunnerResultFrame(
            request_id=frame.request_id,
            status=self.answers[frame.session_id],
            reason=(
                "host refused the launch"
                if self.answers[frame.session_id] == REPLACE_STALE_RUNNER_FAILED
                else None
            ),
        )
        asyncio.get_running_loop().call_soon(
            lambda: asyncio.ensure_future(self.host._dispatch_host_frame(ws, result))  # type: ignore[arg-type]
        )

    def asked_sessions(self) -> list[str]:
        return [frame.session_id for frame in self.requests]


def _write_config(
    path: Path, *, enabled: bool | None = None, exclude: list[str] | None = None
) -> None:
    host_section: dict[str, object] = {"host_id": "a" * 32, "name": "test-laptop"}
    if enabled is not None:
        host_section["replace_stale_runners"] = enabled
    if exclude is not None:
        host_section["replace_stale_runners_exclude"] = exclude
    path.write_text(yaml.safe_dump({"host": host_section}))


def _host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answers: dict[str, str],
    *,
    attached_pids: frozenset[int] = frozenset(),
) -> tuple[HostProcess, _FakeServer]:
    """A daemon that adopted one stale fake runner per answered session."""
    monkeypatch.setattr("omnigent.host.connect.loaded_commit", lambda: HOST_COMMIT)
    monkeypatch.setattr(
        "omnigent.host.connect.runner_has_attached_terminal", lambda pid: pid in attached_pids
    )
    config = tmp_path / "config.yaml"
    if not config.exists():
        _write_config(config)
    host = HostProcess(
        HostIdentity(host_id="host_test_connect", name="test-laptop"),
        "http://localhost:8000",
        config_path=config,
    )
    host._server_capabilities = frozenset({SERVER_CAP_REPLACE_STALE_RUNNER})
    for index, session_id in enumerate(answers):
        runner_id = f"runner_{index}"
        host._runners[runner_id] = _RunnerHandle(
            proc=_FakeRunnerProc(1000 + index),  # type: ignore[arg-type]
            log_path=tmp_path / f"{runner_id}.log",
            session_id=session_id,
        )
        host._stale_adopted_runner_ids.add(runner_id)
        host._adopted_runner_commits[runner_id] = RUNNER_COMMIT
    server = _FakeServer(host, answers)
    monkeypatch.setattr(host, "_send_text", server.send_text)
    return host, server


async def test_an_idle_stale_runner_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    host, server = _host(tmp_path, monkeypatch, {"session-a": REPLACE_STALE_RUNNER_REPLACED})

    with caplog.at_level(logging.INFO, logger="omnigent.host.connect"):
        await host._replace_one_stale_runner(None)  # type: ignore[arg-type]

    assert [(f.runner_id, f.session_id) for f in server.requests] == [("runner_0", "session-a")]
    assert any(
        "Replaced stale runner runner_0 for session session-a" in r.getMessage()
        for r in caplog.records
    )


async def test_only_one_stale_runner_is_replaced_per_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = {
        "session-a": REPLACE_STALE_RUNNER_REPLACED,
        "session-b": REPLACE_STALE_RUNNER_REPLACED,
    }
    host, server = _host(tmp_path, monkeypatch, answers)

    await host._replace_one_stale_runner(None)  # type: ignore[arg-type]

    assert server.asked_sessions() == ["session-a"]


async def test_a_busy_runner_is_left_and_asked_again_at_the_next_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = {"session-a": REPLACE_STALE_RUNNER_BUSY, "session-b": REPLACE_STALE_RUNNER_BUSY}
    host, server = _host(tmp_path, monkeypatch, answers)

    await host._replace_one_stale_runner(None)  # type: ignore[arg-type]
    await host._replace_one_stale_runner(None)  # type: ignore[arg-type]

    assert server.asked_sessions() == ["session-a", "session-b", "session-a", "session-b"]
    assert set(host._runners) == {"runner_0", "runner_1"}


async def test_a_session_with_an_attached_terminal_is_never_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = {"session-a": REPLACE_STALE_RUNNER_REPLACED}
    host, server = _host(tmp_path, monkeypatch, answers, attached_pids=frozenset({1000}))

    await host._replace_one_stale_runner(None)  # type: ignore[arg-type]

    assert server.requests == []


async def test_a_session_on_the_exclude_list_is_never_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path / "config.yaml", exclude=["session-a"])
    answers = {
        "session-a": REPLACE_STALE_RUNNER_REPLACED,
        "session-b": REPLACE_STALE_RUNNER_REPLACED,
    }
    host, server = _host(tmp_path, monkeypatch, answers)

    await host._replace_one_stale_runner(None)  # type: ignore[arg-type]

    assert server.asked_sessions() == ["session-b"]


async def test_turning_the_setting_off_stops_replacement_at_the_next_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = {"session-a": REPLACE_STALE_RUNNER_BUSY}
    host, server = _host(tmp_path, monkeypatch, answers)

    await host._replace_one_stale_runner(None)  # type: ignore[arg-type]
    _write_config(tmp_path / "config.yaml", enabled=False)
    await host._replace_one_stale_runner(None)  # type: ignore[arg-type]

    assert server.asked_sessions() == ["session-a"]


async def test_a_failed_resume_keeps_the_runner_warns_once_and_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    host, server = _host(tmp_path, monkeypatch, {"session-a": REPLACE_STALE_RUNNER_FAILED})

    with caplog.at_level(logging.WARNING, logger="omnigent.host.connect"):
        for _ in range(3):
            await host._replace_one_stale_runner(None)  # type: ignore[arg-type]

    assert server.asked_sessions() == ["session-a"]
    assert "runner_0" in host._runners
    warnings = [
        r.getMessage() for r in caplog.records if "Replacing stale runner" in r.getMessage()
    ]
    assert len(warnings) == 1
    for part in ("session-a", RUNNER_COMMIT, HOST_COMMIT):
        assert part in warnings[0]


async def test_a_server_without_replacement_is_never_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host, server = _host(tmp_path, monkeypatch, {"session-a": REPLACE_STALE_RUNNER_REPLACED})
    host._server_capabilities = frozenset()

    await host._replace_one_stale_runner(None)  # type: ignore[arg-type]

    assert server.requests == []


@pytest.mark.parametrize(
    ("owner_pid", "attached", "expected"),
    [(4242, True, True), (4242, False, False), (4343, True, False)],
    ids=["attached", "detached", "another-runner"],
)
def test_runner_has_attached_terminal_reads_the_runners_own_terminals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner_pid: int,
    attached: bool,
    expected: bool,
) -> None:
    terminal_dir = tmp_path / "omnigent-terminal-abc"
    terminal_dir.mkdir()
    (terminal_dir / "tmux.sock").touch()
    (terminal_dir / owner_claim.OWNER_PID_FILENAME).write_text(
        f"{owner_pid}\npid_ns={owner_claim.current_pid_namespace()}\n"
    )
    monkeypatch.setattr(
        "omnigent.native.native_cost_popup._tmux_last_client_input_at",
        lambda socket_path, target: 1.0 if attached else None,
    )

    assert stale_runners.runner_has_attached_terminal(4242, tmp_root=tmp_path) is expected
