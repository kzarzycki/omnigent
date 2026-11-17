"""Helpers for Claude CLI launch args shared by the server and the runner."""

from __future__ import annotations

from collections.abc import Sequence

from omnigent.harnesses.launch_args import args_before_separator


def without_claude_session_id_arg(args: Sequence[str]) -> list[str]:
    """
    Drop a ``--session-id`` from Claude launch args before any ``--``.

    The id names one Claude session, so a launch that must not reuse it
    (a cold ``--resume``, or a new session continuing an old one) drops it.
    Everything from a client ``--`` on is prompt text and is kept as given.

    :param args: Claude args, e.g. ``["--session-id", "<uuid>", "--verbose"]``.
    :returns: *args* without ``--session-id <value>`` / ``--session-id=<value>``
        ahead of the separator, e.g. ``["--verbose"]``.
    """
    options = args_before_separator(args)
    kept: list[str] = []
    skip_value = False
    for arg in options:
        if skip_value:
            skip_value = False
            continue
        if arg == "--session-id":
            skip_value = True
            continue
        if arg.startswith("--session-id="):
            continue
        kept.append(arg)
    return [*kept, *args[len(options) :]]


# @deprecated, remove in 0.22.0 with the runner's legacy-prompt handling:
# clients before 0.20.0 stored ``-p`` in the persisted launch args.
def claude_legacy_launch_prompt(args: Sequence[str] | None) -> str | None:
    """
    Return the ``-p`` prompt older clients stored ahead of the launch flags.

    :param args: Persisted pass-through args, e.g.
        ``["hello", "--permission-mode", "auto"]``.
    :returns: The leading positional, e.g. ``"hello"``, or ``None``.
    """
    if not args or args[0].startswith("-"):
        return None
    return args[0]
