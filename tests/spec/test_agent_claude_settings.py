"""An agent's own Claude Code settings survive upload and reject a non-mapping."""

from pathlib import Path

import pytest
import yaml

from omnigent.chat import _bundle_agent
from omnigent.errors import OmnigentError
from omnigent.spec import load


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
