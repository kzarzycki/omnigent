"""An agent spec's tool restrictions, as the harness launch and server policies read them."""

from __future__ import annotations

from omnigent.spec.types import AgentSpec

# Tool names (Claude Code's and codex's) that change files; a spec forbidding
# any of them starts codex-native in a read-only sandbox.
EDIT_TOOL_NAMES: frozenset[str] = frozenset(
    {"Edit", "MultiEdit", "Write", "NotebookEdit", "apply_patch"}
)


def executor_tool_names(spec: AgentSpec, key: str) -> tuple[str, ...]:
    """Tool names listed under ``executor.config.<key>`` (an imported agent's restrictions)."""
    value = spec.executor.config.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(name for name in value if isinstance(name, str) and name)


def policy_denied_tools(spec: AgentSpec) -> tuple[str, ...]:
    """
    Plain tool names the spec forbids, for the server's deny policy.

    Entries with an argument pattern (``Bash(git push:*)``) are left to the harness.
    """
    return tuple(name for name in executor_tool_names(spec, "disallowed_tools") if "(" not in name)


def spec_forbids_edits(spec: AgentSpec) -> bool:
    """Whether the spec forbids any file-editing tool."""
    return any(name in EDIT_TOOL_NAMES for name in executor_tool_names(spec, "disallowed_tools"))


def map_claude_tools(names: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """
    Split an imported ``tools`` list into Claude ``--tools`` names and entries it cannot enforce.

    ``--tools`` takes built-in tool names only. ``*`` means no restriction, so both
    lists are empty. ``mcp__`` names and ``Task(x)`` patterns have no ``--tools``
    form (the CLI drops ``Task(x)``), so they are returned as unenforced.

    :param names: The agent's ``tools`` entries.
    :returns: ``(built-in names, unenforced entries)``.
    """
    if "*" in names:
        return [], []
    builtin: list[str] = []
    unenforced: list[str] = []
    for name in names:
        if name.startswith("mcp__") or "(" in name:
            unenforced.append(name)
        elif name not in builtin:
            builtin.append(name)
    return builtin, unenforced
