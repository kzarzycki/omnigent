"""``OMNIGENT_AGENT_DIRS`` expands to the agent sources the image registers at startup."""

from __future__ import annotations

import os
from pathlib import Path

from deploy.docker.entrypoint import agent_sources


def test_parent_directory_expands_to_its_agents(tmp_path: Path) -> None:
    repo = tmp_path / "agents"
    for name in ("b-agent", "a-agent"):
        (repo / name).mkdir(parents=True)
        (repo / name / "config.yaml").write_text("name: x\n")
    (repo / "README.md").write_text("not an agent\n")
    (repo / "notes").mkdir()

    assert agent_sources(str(repo)) == [repo / "a-agent", repo / "b-agent"]


def test_agent_directory_and_yaml_file_pass_through(tmp_path: Path) -> None:
    agent = tmp_path / "one"
    agent.mkdir()
    (agent / "config.yaml").write_text("name: one\n")
    yaml_file = tmp_path / "two.yaml"

    assert agent_sources(os.pathsep.join([str(agent), str(yaml_file), ""])) == [agent, yaml_file]


def test_unset_registers_nothing() -> None:
    assert agent_sources("") == []
