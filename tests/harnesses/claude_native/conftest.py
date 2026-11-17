"""Shared fixtures for the Claude-native harness tests."""

from __future__ import annotations

import pytest

from omnigent.harnesses.claude_native import bridge

# The production cap, for the tests of the wait itself.
SUBMIT_RECORD_TIMEOUT_S = bridge._SUBMIT_RECORD_TIMEOUT_S


@pytest.fixture(autouse=True)
def _default_to_personal_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guidance tests assume a person's own host unless they set ``IS_SANDBOX=1``."""
    monkeypatch.delenv("IS_SANDBOX", raising=False)


@pytest.fixture(autouse=True)
def _ignore_ambient_claude_config_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Keep a developer's ``CLAUDE_CONFIG_DIR`` away from transcript reads and writes.

    Tests that need a Claude config dir set one themselves.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


@pytest.fixture(autouse=True)
def _skip_the_submit_record_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deliveries end at the submit: most fixtures have no transcript for Claude to record into."""
    monkeypatch.setattr(bridge, "_SUBMIT_RECORD_TIMEOUT_S", 0.0)
