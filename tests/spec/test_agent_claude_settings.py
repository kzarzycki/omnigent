"""An agent's own Claude Code settings survive upload and reject a non-mapping."""

from pathlib import Path

import pytest
import yaml

from omnigent.chat import _bundle_agent
from omnigent.errors import OmnigentError
from omnigent.spec import load
from omnigent.spec.omnigent import agent_def_to_agent_spec, agent_spec_to_agent_def


def _write_agent(tmp_path: Path, settings: object) -> Path:
    source = tmp_path / "agent"
    source.mkdir()
    config = {
        "spec_version": 1,
        "name": "code-mode",
        "executor": {
            "type": "omnigent",
            "config": {"harness": "claude-native", "settings": settings},
        },
        "prompt": "Work through repl.",
    }
    (source / "config.yaml").write_text(yaml.safe_dump(config))
    return source


def test_settings_survive_bundle_transport_as_a_mapping(tmp_path: Path) -> None:
    settings = {
        "env": {"CODE_MODE_GUIDE": "system"},
        "enabledPlugins": {"code-mode@claude-mods": True, "eval-kernel@claude-mods": False},
    }

    spec = load(_bundle_agent(_write_agent(tmp_path, settings)), dest=tmp_path / "uploaded")

    assert spec.executor.config["settings"] == settings


@pytest.mark.parametrize("value", ["{}", ["env"], 1, True])
def test_settings_reject_a_non_mapping(tmp_path: Path, value: object) -> None:
    with pytest.raises(OmnigentError, match=r"settings.*must be a mapping"):
        load(_write_agent(tmp_path, value))


SETTINGS = {"env": {"CODE_MODE_GUIDE": "system"}, "enabledPlugins": {"code-mode@m": True}}


def _write_single_file_agent(
    tmp_path: Path, settings: object, harness: str = "claude-native"
) -> Path:
    executor = {"model": "claude-opus-5-5", "harness": harness, "settings": settings}
    path = tmp_path / "code-mode.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "code-mode",
                "prompt": "Work through repl.",
                "executor": executor,
                "tools": {
                    "worker": {
                        "type": "agent",
                        "prompt": "Work.",
                        "executor": {**executor, "settings": {"env": {"ROLE": "worker"}}},
                    }
                },
            }
        )
    )
    return path


def test_settings_survive_a_single_file_agent_and_its_upload(tmp_path: Path) -> None:
    path = _write_single_file_agent(tmp_path, SETTINGS)

    assert load(path).executor.config["settings"] == SETTINGS
    uploaded = load(_bundle_agent(path), dest=tmp_path / "uploaded")
    assert uploaded.executor.config["settings"] == SETTINGS


def test_a_single_file_agent_rejects_non_mapping_settings(tmp_path: Path) -> None:
    with pytest.raises(OmnigentError, match=r"settings.*must be a mapping"):
        load(_write_single_file_agent(tmp_path, "{}"))


def test_settings_survive_the_agent_def_round_trip_for_agent_and_sub_agent(
    tmp_path: Path,
) -> None:
    spec = agent_def_to_agent_spec(
        agent_spec_to_agent_def(load(_write_single_file_agent(tmp_path, SETTINGS)))
    )

    assert spec.executor.config["settings"] == SETTINGS
    assert spec.sub_agents[0].executor.config["settings"] == {"env": {"ROLE": "worker"}}


@pytest.mark.parametrize("single_file", [False, True], ids=["bundle", "single-file"])
def test_other_harnesses_ignore_the_settings_key(tmp_path: Path, single_file: bool) -> None:
    if single_file:
        load(_write_single_file_agent(tmp_path, "not a mapping", harness="codex-native"))
        return
    source = _write_agent(tmp_path, "not a mapping")
    config = yaml.safe_load((source / "config.yaml").read_text())
    config["executor"]["config"]["harness"] = "codex-native"
    (source / "config.yaml").write_text(yaml.safe_dump(config))

    load(source)
