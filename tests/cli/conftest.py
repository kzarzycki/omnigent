"""Shared fixtures for CLI unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.config import global_config_path


@pytest.fixture(autouse=True)
def _own_config_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Give each test its own config home, and honour a test's own config path.

    A command test that starts a real host daemon (``omnigent claude --server ...``)
    leaves a ``host:`` identity in the config home the whole session shares, which
    later tests then read as their starting config. The suite-wide
    ``$OMNIGENT_CONFIG_HOME`` also outranks a test's ``omnigent.cli._GLOBAL_CONFIG_PATH``
    patch, so a test that seeds its own file would never read it back. That patch wins
    only for the CLI's own path, unlike production, where the env var always wins.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config-home"))
    import omnigent.cli as cli_module

    default_path = cli_module._GLOBAL_CONFIG_PATH

    def effective_path() -> Path:
        if default_path != cli_module._GLOBAL_CONFIG_PATH:
            return cli_module._GLOBAL_CONFIG_PATH
        return global_config_path(default_path)

    monkeypatch.setattr(cli_module, "_effective_global_config_path", effective_path)


@pytest.fixture(autouse=True)
def _no_ambient_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the machine's own host identity and runner slice-key env var.

    ``databricks_request_headers`` (reached via ``_remote_headers``,
    ``open_daemon_client``, and the auth flow) resolves the slice key from,
    in order: an explicit ``host_id``, the ``OMNIGENT_RUNNER_SLICE_KEY`` env
    var (set inside a runner process), then the CLI's own host identity. On a
    developer machine that IS a host — a persisted ``~/.omnigent/config.yaml``
    ``host:`` section — that last fallback would leak the machine's host_id
    into requests, so any "no slice key on this call" assertion would flake
    depending on where the suite runs. Force the read-only lookup to ``None``
    and clear the routing settings so tests exercise only the host they name.

    ``autouse=True`` keeps the isolation on by default; tests that need a real
    host identity re-patch ``load_host_identity_if_present`` themselves.
    """
    monkeypatch.setattr(
        "omnigent.host.identity.load_host_identity_if_present",
        lambda *a, **k: None,
    )
    monkeypatch.delenv("OMNIGENT_RUNNER_SLICE_KEY", raising=False)
    monkeypatch.delenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", raising=False)
    monkeypatch.setattr("omnigent.session_host._session_hosts", {})
    # The last fallback, this machine's stored random key, reads as "client_key".
    monkeypatch.setattr("omnigent.routing_key.client_routing_key", lambda: "client_key")
