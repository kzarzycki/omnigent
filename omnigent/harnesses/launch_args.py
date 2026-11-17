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


def args_before_separator(args: Sequence[str]) -> Sequence[str]:
    """
    Return the args before the first ``--``; the rest is prompt text, not flags.

    :param args: Launch args, possibly containing a ``--`` separator.
    :returns: The leading args up to (excluding) the separator.
    """
    return args[: args.index("--")] if "--" in args else args


def has_flag_before_separator(args: Sequence[str], flag: str) -> bool:
    """
    Return whether ``flag`` (as ``flag`` or ``flag=value``) appears before the first ``--``.

    :param args: Launch args, possibly containing a ``--`` separator.
    :param flag: Long flag name, e.g. ``"--model"``.
    :returns: ``True`` when Omnigent-visible args already carry the flag.
    """
    return any(arg == flag or arg.startswith(f"{flag}=") for arg in args_before_separator(args))


def with_default_flag_value(args: Sequence[str], flag: str, value: str) -> list[str]:
    """
    Add ``flag value`` before any ``--`` unless the caller already passed ``flag`` there.

    :param args: Launch args, e.g. ``["--", "fix the bug"]``.
    :param flag: Long flag name, e.g. ``"--model"``.
    :param value: Default value, e.g. ``"sonnet"``.
    :returns: The args with the default inserted ahead of the separator when absent.
    """
    if has_flag_before_separator(args, flag):
        return list(args)
    return insert_before_separator(args, (flag, value))
