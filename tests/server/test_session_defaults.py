"""A user's default harness and model for an agent spec that names none."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.configuration_profile import ConfigurationSnapshot, PortableConfiguration
from omnigent.preference_sync import PreferenceNamespace
from omnigent.server.preference_requests import PreferencePolicy
from omnigent.server.session_defaults import SessionDefaults, read_session_defaults


class _SavedConfiguration:
    """Preference-store stand-in holding one saved configuration."""

    def __init__(self, configuration: PortableConfiguration | None) -> None:
        self._configuration = configuration
        self.reads: list[str | None] = []

    def read_configuration(self, *, user_id: str | None) -> ConfigurationSnapshot:
        self.reads.append(user_id)
        return ConfigurationSnapshot(
            identity=PreferenceNamespace(authority_id="a", user_id="u", workspace_id="1"),
            configuration=self._configuration,
        )


@pytest.fixture()
def user_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the server's user config at a file declaring ``pi`` and ``model-file``."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"harness": {"default": "pi"}, "model": "model-file"}))
    return path


@pytest.mark.parametrize(
    ("user_id", "saved", "policy", "expected"),
    [
        # A saved configuration wins over the server's own config file.
        (None, PortableConfiguration(default_harness="codex-native", model="m"), None,
         SessionDefaults("codex-native", "m")),
        # One with no harness falls through to the file in single-user mode.
        ("local", PortableConfiguration(model="m"), None, SessionDefaults("pi", "model-file")),
        # Disabled shared preferences are not read.
        (None, PortableConfiguration(default_harness="codex-native"),
         PreferencePolicy(enabled=lambda: False), SessionDefaults("pi", "model-file")),
        # Another user never gets the server's own config.
        ("alice@example.com", None, None, SessionDefaults()),
        ("alice@example.com", PortableConfiguration(default_harness="pi"), None,
         SessionDefaults("pi", None)),
    ],
)  # fmt: skip
def test_read_session_defaults_sources(
    user_config: Path,
    user_id: str | None,
    saved: PortableConfiguration | None,
    policy: PreferencePolicy | None,
    expected: SessionDefaults,
) -> None:
    store = _SavedConfiguration(saved)
    defaults = read_session_defaults(
        user_id=user_id, preference_store=store, preference_policy=policy
    )
    assert defaults == expected
    assert store.reads == ([] if policy is not None else [user_id])


def test_read_session_defaults_without_store_or_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    assert (
        read_session_defaults(user_id=None, preference_store=None, preference_policy=None)
        == SessionDefaults()
    )


def test_read_session_defaults_ignores_a_malformed_config_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("harness: [unclosed\n")
    assert (
        read_session_defaults(user_id=None, preference_store=None, preference_policy=None)
        == SessionDefaults()
    )
