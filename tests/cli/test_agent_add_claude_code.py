"""``omnigent agent add`` imports Claude Code subagent Markdown files."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from omnigent import cli as cli_mod
from omnigent.spec import load as load_spec

from .test_agent_commands import _FakeServer, _posted_tars, server  # noqa: F401

_REVIEWER = """---
name: reviewer
description: Reviews a diff for bugs.
tools: Read, Grep
disallowedTools: Bash
skills: [triage]
model: sonnet
---
Review the diff. Report only real bugs.
"""


def _write_plugin(root: Path, reviewer: str = _REVIEWER) -> None:
    (root / "kit" / ".claude-plugin").mkdir(parents=True)
    (root / "kit" / ".claude-plugin" / "plugin.json").write_text('{"name": "kit"}')
    (root / "kit" / "agents").mkdir()
    (root / "kit" / "agents" / "reviewer.md").write_text(reviewer)
    (root / "kit" / "agents" / "scout.md").write_text(
        "---\nname: scout\ndescription: Finds files.\n---\nFind files.\n"
    )


def test_add_a_claude_code_agent_file_keeps_name_restrictions_and_instructions(
    tmp_path: Path, server: _FakeServer
) -> None:
    source = tmp_path / "reviewer.md"
    source.write_text(_REVIEWER)

    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", str(source)])

    assert result.exit_code == 0, result.output
    (bundle,) = _posted_tars(server)
    config = yaml.safe_load(bundle["config.yaml"])
    assert config["name"] == "reviewer"
    assert config["description"] == "Reviews a diff for bugs."
    assert config["executor"]["config"] == {
        "allowed_tools": ["Read", "Grep"],
        "disallowed_tools": ["Bash"],
    }
    assert config["skills"] == ["triage"]
    assert bundle["AGENTS.md"].decode().strip() == "Review the diff. Report only real bugs."
    assert "harness" not in config["executor"].get("config", {}), "the agent stays portable"


def test_an_imported_agent_loads_as_a_spec_with_its_restrictions(tmp_path: Path) -> None:
    from omnigent.claude_code_agent import claude_code_agent_config, parse_claude_code_agent

    agent = parse_claude_code_agent(_REVIEWER)
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "config.yaml").write_text(yaml.safe_dump(claude_code_agent_config(agent)))
    (bundle_dir / "AGENTS.md").write_text(agent.instructions)

    spec = load_spec(bundle_dir)

    assert spec.name == "reviewer"
    assert spec.description == "Reviews a diff for bugs."
    assert spec.executor.config["allowed_tools"] == ["Read", "Grep"]
    assert spec.executor.config["disallowed_tools"] == ["Bash"]
    assert spec.instructions == "Review the diff. Report only real bugs."


@pytest.mark.parametrize(
    "text", ["no frontmatter\n", "---\nname: x\n", "---\ndescription: d\n---\nbody\n"]
)
def test_add_refuses_a_file_that_is_not_a_claude_code_agent(
    tmp_path: Path, server: _FakeServer, text: str
) -> None:
    source = tmp_path / "bad.md"
    source.write_text(text)
    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", str(source)])
    assert result.exit_code != 0
    assert "is not a Claude Code agent" in result.stderr
    assert not [r for r in server.seen if r.method == "POST"]


def _fake_clone(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.setattr(
        cli_mod,
        "_clone_github_source",
        lambda url, ref, dest: shutil.copytree(repo, dest, symlinks=True),
    )


def test_add_repo_installs_each_plugin_agent_and_a_second_run_updates_without_duplicates(
    tmp_path: Path, server: _FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "src"
    _write_plugin(repo)
    _fake_clone(monkeypatch, repo)

    first = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/kit", "-y"])
    assert first.exit_code == 0, first.output
    assert "Found 2 agent(s)" in first.output
    assert sorted(yaml.safe_load(t["config.yaml"])["name"] for t in _posted_tars(server)) == [
        "reviewer",
        "scout",
    ]

    (repo / "kit" / "agents" / "reviewer.md").write_text(
        _REVIEWER.replace("Report only real bugs.", "Be brief.")
    )
    second = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/kit", "-y"])

    assert second.exit_code == 0, second.output
    posted = _posted_tars(server)[2:]
    assert sorted(yaml.safe_load(t["config.yaml"])["name"] for t in posted) == ["reviewer", "scout"]
    reviewer = next(t for t in posted if yaml.safe_load(t["config.yaml"])["name"] == "reviewer")
    assert b"Be brief." in reviewer["AGENTS.md"], "the changed file is re-sent under its name"
    assert len({yaml.safe_load(t["config.yaml"])["name"] for t in posted}) == len(posted)


def test_add_repo_refuses_two_plugin_agents_with_one_name(
    tmp_path: Path, server: _FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "src"
    _write_plugin(repo)
    (repo / "kit" / "agents" / "scout.md").write_text(_REVIEWER)
    _fake_clone(monkeypatch, repo)

    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/kit", "-y"])

    assert result.exit_code != 0
    assert "share a name" in result.stderr


def test_add_repo_at_name_picks_one_plugin_agent(
    tmp_path: Path, server: _FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "src"
    _write_plugin(repo)
    _fake_clone(monkeypatch, repo)

    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/kit@scout", "-y"])

    assert result.exit_code == 0, result.output
    assert [yaml.safe_load(t["config.yaml"])["name"] for t in _posted_tars(server)] == ["scout"]
