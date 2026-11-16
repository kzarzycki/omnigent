"""Import Claude Code subagent Markdown files as Omnigent agent bundles.

A Claude Code subagent is a Markdown file whose YAML frontmatter carries
``name``, ``description``, ``tools``, ``disallowedTools`` and ``skills`` and
whose body is the system prompt. It maps onto an agent bundle with no harness
or model, so the agent runs on whichever harness the session picks.
"""

from __future__ import annotations

import io
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from omnigent.spec.validator import _AGENT_NAME_PATTERN, _RESERVED_AGENT_NAMES

_FRONTMATTER_FENCE = "---"


class ClaudeCodeAgentError(ValueError):
    """The file is not a usable Claude Code agent."""


@dataclass(frozen=True)
class ClaudeCodeAgent:
    """A parsed Claude Code agent file.

    :param name: The agent's name.
    :param description: Short summary of when to use it.
    :param instructions: The Markdown body, the agent's system prompt.
    :param allowed_tools: Claude Code ``tools``; empty means no restriction.
    :param disallowed_tools: Claude Code ``disallowedTools``.
    :param skills: Claude Code ``skills``; empty means all.
    """

    name: str
    description: str
    instructions: str
    allowed_tools: tuple[str, ...] = field(default_factory=tuple)
    disallowed_tools: tuple[str, ...] = field(default_factory=tuple)
    skills: tuple[str, ...] = field(default_factory=tuple)


def _name_list(raw: object, key: str) -> tuple[str, ...]:
    """A frontmatter tool or skill list: a comma-separated string or a YAML list."""
    if raw is None:
        return ()
    items = raw.split(",") if isinstance(raw, str) else raw
    if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
        raise ClaudeCodeAgentError(f"`{key}` must be a comma-separated string or a list of names.")
    return tuple(dict.fromkeys(i.strip() for i in items if i.strip()))


def _restricting_tools(meta: dict[object, object]) -> tuple[str, ...]:
    """The ``tools`` list; a present-but-empty one is refused rather than read as unrestricted."""
    tools = _name_list(meta.get("tools"), "tools")
    if "tools" in meta and meta["tools"] is not None and not tools:
        raise ClaudeCodeAgentError(
            "`tools` is present but empty; list the tools to allow, or remove the key "
            "for no restriction."
        )
    return tools


def parse_claude_code_agent(text: str) -> ClaudeCodeAgent:
    """Parse a Claude Code agent Markdown file.

    :param text: The file's content.
    :returns: The parsed agent.
    :raises ClaudeCodeAgentError: When the frontmatter is missing or malformed,
        or has no ``name``.
    """
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        raise ClaudeCodeAgentError("no YAML frontmatter (the file must start with `---`).")
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == _FRONTMATTER_FENCE)
    except StopIteration:
        raise ClaudeCodeAgentError("the frontmatter is not closed with `---`.") from None
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as exc:
        raise ClaudeCodeAgentError(f"the frontmatter is not valid YAML: {exc}") from None
    if not isinstance(meta, dict):
        raise ClaudeCodeAgentError("the frontmatter must be a YAML mapping.")
    name = meta.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ClaudeCodeAgentError("the frontmatter has no `name`.")
    name = name.strip()
    if not _AGENT_NAME_PATTERN.match(name) or name in _RESERVED_AGENT_NAMES:
        raise ClaudeCodeAgentError(
            f"agent name {name!r} must match [a-zA-Z0-9_-]+ and not be reserved "
            f"(no dots, slashes or whitespace)."
        )
    description = meta.get("description")
    return ClaudeCodeAgent(
        name=name,
        description=description.strip() if isinstance(description, str) else "",
        instructions="\n".join(lines[end + 1 :]).strip(),
        allowed_tools=_restricting_tools(meta),
        disallowed_tools=_name_list(meta.get("disallowedTools"), "disallowedTools"),
        skills=_name_list(meta.get("skills"), "skills"),
    )


def claude_code_agent_config(agent: ClaudeCodeAgent) -> dict[str, object]:
    """The ``config.yaml`` mapping for ``agent``; it names no harness or model."""
    executor_config: dict[str, object] = {}
    if agent.allowed_tools:
        executor_config["allowed_tools"] = list(agent.allowed_tools)
    if agent.disallowed_tools:
        executor_config["disallowed_tools"] = list(agent.disallowed_tools)
    executor: dict[str, object] = {"type": "omnigent"}
    if executor_config:
        executor["config"] = executor_config
    config: dict[str, object] = {"spec_version": 1, "name": agent.name, "executor": executor}
    if agent.description:
        config["description"] = agent.description
    if agent.skills:
        config["skills"] = list(agent.skills)
    if agent.instructions:
        config["instructions"] = "AGENTS.md"
    return config


def claude_code_agent_bundle(agent: ClaudeCodeAgent) -> bytes:
    """Tar ``agent`` as an agent bundle: ``config.yaml`` and ``AGENTS.md``."""
    files = {"config.yaml": yaml.safe_dump(claude_code_agent_config(agent), sort_keys=False)}
    if agent.instructions:
        files["AGENTS.md"] = agent.instructions + "\n"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for arcname, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def find_plugin_agent_files(root: Path) -> list[Path]:
    """Agent Markdown files shipped by the Claude Code plugins under ``root``.

    A plugin is a directory with ``.claude-plugin/plugin.json``; its agents are
    the ``agents/**/*.md`` files beside it. Any manifest or agent file that
    resolves outside ``root`` (through a symlinked file or directory) is skipped.
    """
    resolved_root = root.resolve()
    found: list[Path] = []
    for manifest in sorted(root.rglob(".claude-plugin/plugin.json")):
        if ".git" in manifest.parts or not manifest.resolve().is_relative_to(resolved_root):
            continue
        agents_dir = manifest.parent.parent / "agents"
        found.extend(
            f
            for f in sorted(agents_dir.rglob("*.md"))
            if f.resolve().is_relative_to(resolved_root)
        )
    return list(dict.fromkeys(found))
