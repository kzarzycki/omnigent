"""The host's own updater: how "Update now" runs it and what it reports back."""

from __future__ import annotations

import sys

import pytest

from omnigent.host.connect import configured_updater, run_host_updater


async def test_runner_passes_the_target_and_reports_exit_status_and_last_lines() -> None:
    script = (
        "import os, sys\n"
        "for i in range(30): print(f'line {i}')\n"
        "print('target', os.environ['OMNIGENT_UPDATE_TARGET'])\n"
        "sys.exit(3)\n"
    )
    exit_code, output = await run_host_updater([sys.executable, "-c", script], "c25ddceb8")

    assert exit_code == 3
    lines = output.splitlines()
    # Only the tail reaches the banner, ending with what the updater said last.
    assert len(lines) == 20
    assert lines[-1] == "target c25ddceb8"


async def test_runner_reports_an_updater_that_cannot_start() -> None:
    exit_code, output = await run_host_updater(["/nonexistent/updater"], "c25ddceb8")

    assert exit_code is None
    assert "could not start /nonexistent/updater" in output


async def test_runner_reports_an_updater_that_hangs() -> None:
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    exit_code, output = await run_host_updater(argv, "c25ddceb8", timeout_s=0.5)

    assert exit_code is None
    assert "did not finish" in output


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", None),
        ("  ", None),
        ("~/bin/update-host.sh --check", ["~/bin/update-host.sh", "--check"]),
    ],
)
def test_configured_updater_reads_a_shell_style_command_line(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: list[str] | None
) -> None:
    monkeypatch.setenv("OMNIGENT_HOST_UPDATER", value)
    assert configured_updater() == expected
