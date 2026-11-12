#!/usr/bin/env python3
"""landing-guard.py through its hook interface: each refused form exits 2 in the landing
environment, ordinary landing commands pass, and outside it every call passes."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

GUARD = Path(__file__).with_name("landing-guard.py")

REFUSED = [
    "env",
    "env | sort",
    "/usr/bin/env",
    "sudo env",
    "FOO=1 env",
    "env -0",
    "printenv",
    "printenv HOME",
    "echo hi; printenv",
    "true && env",
    "echo $(env)",
    "echo `printenv`",
    "bash -c 'env | grep KEY'",
    "sh -c \"printenv\"",
    "xargs env < /dev/null",
    "timeout 5 env",
    "cat /proc/self/environ",
    "cat /proc/1/environ | tr '\\0' '\\n'",
    "strings /proc/$$/environ",
    "tr '\\0' '\\n' < /proc/*/environ",
    "echo $OMNIGENT_LANDING_SIGNING_KEY",
    "echo \"${OMNIGENT_LANDING_SIGNING_KEY}\"",
    "printf %s \"$LOOP_SIGNING_KEY\" | base64 -d",
    "echo ${!OMNIGENT*}",
    "set",
    "export",
    "export -p",
    "declare -x",
    "typeset -p",
    "compgen -e",
    "compgen -A variable",
    "readonly -p",
    "python3 -c 'import os; print(os.environ)'",
    "node -e 'console.log(process.env)'",
    "python3 -c 'import os; print(os.getenv(\"HOME\"))'",
    "awk 'BEGIN { for (k in ENVIRON) print k }'",
    "ps eww",
    "ps auxe",
]

ALLOWED = [
    "git status",
    "git fetch origin mine && git log --oneline -3 origin/mine",
    "gh api repos/acme/widgets/pulls/7 --jq .head.sha",
    "uv run approvals.py land 7 --wait",
    "uv run approvals.py local-ci 7 --record",
    "git verify-commit HEAD",
    "cat /tmp/omnigent-land-init.log",
    "ls ~/.config/omnigent",
    "echo $HOME",
    "set -euo pipefail; git status",
    "set -e",
    "compgen -c git",
    "export GIT_PAGER=cat",
    "declare -a items=(a b)",
    "docker run --env FOO=1 image",
    "uv run --env-file .env pytest",
    "grep -n environment docs/README.md",
    "ps aux",
    "ps -ef",
    "git commit -m 'describe the landing environment'",
    "echo 'unbalanced",
]


def run(tool_input: dict, marker: str | None = "1", tool: str = "Bash") -> subprocess.CompletedProcess:
    environment = {key: value for key, value in os.environ.items() if key != "OMNIGENT_LANDING_ENVIRONMENT"}
    if marker is not None:
        environment["OMNIGENT_LANDING_ENVIRONMENT"] = marker
    call = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input}
    return subprocess.run(
        [sys.executable, str(GUARD)], input=json.dumps(call), env=environment,
        capture_output=True, text=True, check=False,
    )


class LandingGuard(unittest.TestCase):
    def test_refuses_each_form_in_the_landing_environment(self) -> None:
        for command in REFUSED:
            with self.subTest(command=command):
                result = run({"command": command})
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("landing guard: refused", result.stderr)

    def test_allows_ordinary_commands(self) -> None:
        for command in ALLOWED:
            with self.subTest(command=command):
                result = run({"command": command})
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_refuses_other_tools_reading_the_environment(self) -> None:
        self.assertEqual(run({"file_path": "/proc/self/environ"}, tool="Read").returncode, 2)
        self.assertEqual(run({"pattern": "KEY", "path": "/proc/1/environ"}, tool="Grep").returncode, 2)
        self.assertEqual(run({"file_path": "/home/user/omnigent/README.md"}, tool="Read").returncode, 0)

    def test_passes_everything_outside_the_landing_environment(self) -> None:
        for marker in (None, "", "0"):
            for command in ("env", "printenv", "cat /proc/self/environ", "echo $OMNIGENT_LANDING_SIGNING_KEY"):
                with self.subTest(marker=marker, command=command):
                    self.assertEqual(run({"command": command}, marker=marker).returncode, 0)

    def test_refuses_an_unreadable_call(self) -> None:
        environment = dict(os.environ, OMNIGENT_LANDING_ENVIRONMENT="1")
        result = subprocess.run(
            [sys.executable, str(GUARD)], input="not json", env=environment,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
