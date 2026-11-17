"""Helpers for placing Omnigent's flags in a native CLI's launch args."""

from __future__ import annotations

from collections.abc import Sequence


def insert_before_separator(args: Sequence[str], extra: Sequence[str]) -> list[str]:
    """
    Insert ``extra`` before the first ``--``, which the CLIs read as the start of prompt text.

    :param args: Launch args, possibly containing a ``--`` separator.
    :param extra: Flags to place ahead of the separator (or at the end without one).
    :returns: The combined args.
    """
    split = args.index("--") if "--" in args else len(args)
    return [*args[:split], *extra, *args[split:]]
