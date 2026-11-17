"""Tests for the shared launch-arg helpers."""

from __future__ import annotations

import pytest

from omnigent.harnesses.launch_args import without_flags_before_separator


@pytest.mark.parametrize(
    "args,expected",
    [
        (["--drop", "x", "--keep"], ["--keep"]),
        (["--keep", "--"], ["--keep", "--"]),
        (["--drop", "x", "--"], ["--"]),
        (["--drop", "x", "--", "--drop", "y"], ["--", "--drop", "y"]),
        (["--", "--drop", "y"], ["--", "--drop", "y"]),
        (["--drop=x", "--keep", "--", "text"], ["--keep", "--", "text"]),
        (["--drop", "--keep"], []),
    ],
)
def test_without_flags_before_separator(args: list[str], expected: list[str]) -> None:
    """Flags are dropped only ahead of ``--``; the next token is always its value by default."""
    assert without_flags_before_separator(args, ("--drop",)) == expected


def test_flag_like_value_kept_when_not_allowed() -> None:
    """With ``value_may_be_flag=False`` a following flag survives and ``on_drop`` reports kinds."""
    dropped: list[tuple[str, str]] = []
    result = without_flags_before_separator(
        ["--drop", "--keep", "--drop=x", "--drop", "v"],
        ("--drop",),
        value_may_be_flag=False,
        on_drop=lambda token, kind: dropped.append((token, kind)),
    )
    assert result == ["--keep"]
    assert dropped == [
        ("--drop", "flag"),
        ("--drop=x", "inline"),
        ("--drop", "flag"),
        ("v", "value"),
    ]
