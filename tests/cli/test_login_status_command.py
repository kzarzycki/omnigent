"""``omnigent login-status`` exit codes and credential-free output."""

from __future__ import annotations

import time

from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.cli_auth import store_token


def test_login_status_exit_codes_and_no_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path", lambda: tmp_path / "auth_tokens.json"
    )
    server = "http://localhost:6767"
    runner = CliRunner()

    missing = runner.invoke(cli, ["login-status", server])
    assert missing.exit_code == 2 and "no_login" in missing.output

    store_token(
        server,
        token="secret-jwt",
        user_id="a@x",
        expires_at=time.time() + 3600,
        refresh_token="secret-refresh",
        refresh_expires_at=time.time() + 3600,
    )
    expiring = runner.invoke(cli, ["login-status", server, "--json"])
    assert expiring.exit_code == 1 and '"state": "expiring"' in expiring.output
    assert "secret-" not in expiring.output

    healthy = runner.invoke(cli, ["login-status", server, "--warn-hours", "0.1"])
    assert healthy.exit_code == 0 and "secret-" not in healthy.output
