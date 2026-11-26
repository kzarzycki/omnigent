"""`omnigent_agent.py ssh` turns a host's SSH check into a card for the owner (#1491)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "skills/omnigent-agents/scripts/omnigent_agent.py"
LINK = "https://login.tailscale.com/a/abc123"


def ssh(
    tmp_path: Path, fake: str, command: tuple[str, ...] = ("local-ci", "7")
) -> subprocess.CompletedProcess[str]:
    """Run the helper with ``fake`` as the body of the ``ssh`` it finds on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text(f'#!/bin/sh\necho "$@" > {tmp_path}/argv\n{fake}\n')
    (bin_dir / "ssh").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OMNIGENT_", "RUNNER_"))}
    env |= {"PATH": f"{bin_dir}:{env['PATH']}", "OMNIGENT_SESSION_ID": "sess-42"}
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--server",
            "http://127.0.0.1:9",
            "ssh",
            "box",
            "--why",
            "run the host gate for PR 7",
            "--",
            *command,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_the_check_line_stops_ssh_and_prints_the_card(tmp_path: Path) -> None:
    # the real check prints its banner and then hangs, BatchMode or not: the helper must stop it
    done = ssh(
        tmp_path,
        "echo '# Tailscale SSH requires an additional check.' >&2\n"
        f"echo '# To authenticate, visit: {LINK}' >&2\nexec sleep 60",
    )
    assert done.returncode == 75, done.stderr
    assert (tmp_path / "argv").read_text().split() == [
        "-o",
        "BatchMode=yes",
        "box",
        "local-ci",
        "7",
    ]
    card = done.stdout
    for part in ("box", "local-ci 7", "run the host gate for PR 7", LINK, "sess-42", "Done"):
        assert part in card, (part, card)


@pytest.mark.parametrize("code", [0, 3])
def test_any_other_ssh_ending_passes_through(tmp_path: Path, code: int) -> None:
    done = ssh(tmp_path, f"echo remote out\necho remote err >&2\nexit {code}")
    assert (done.returncode, done.stdout) == (code, "remote out\n")
    assert "remote err" in done.stderr


def test_several_words_reach_the_remote_shell_as_argv(tmp_path: Path) -> None:
    # OpenSSH joins its arguments into one string for the remote shell: the fake runs it as that shell
    done = ssh(tmp_path, 'sh -c "$4"', ("printf", "<%s>", "a b"))
    assert (done.returncode, done.stdout) == (0, "<a b>")
