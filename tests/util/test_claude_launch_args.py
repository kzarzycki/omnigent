from __future__ import annotations

import pytest

from omnigent.util.claude_launch_args import without_claude_session_id_arg


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--session-id", "abc", "--verbose"], ["--verbose"]),
        (["--session-id=abc", "--verbose"], ["--verbose"]),
        (["--", "explain", "--session-id", "flag"], ["--", "explain", "--session-id", "flag"]),
        (
            ["--session-id", "abc", "--", "explain", "--session-id=flag"],
            ["--", "explain", "--session-id=flag"],
        ),
    ],
    ids=["split", "joined", "text-after-separator-kept", "flag-before-separator-dropped"],
)
def test_without_claude_session_id_arg_reads_only_before_separator(
    args: list[str], expected: list[str]
) -> None:
    assert without_claude_session_id_arg(args) == expected
