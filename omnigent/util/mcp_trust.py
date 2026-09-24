"""Exact-config approval for Omnigent-managed MCP servers."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict

from omnigent.spec.types import MCPServerConfig


def mcp_config_digest(config: MCPServerConfig) -> str:
    """Hash every declared config field without exposing its secret-bearing values."""
    payload = json.dumps(asdict(config), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def mcp_is_allowed(config: MCPServerConfig) -> bool:
    """Allow any server unless guarded mode requires an exact config approval."""
    if os.environ.get("OMNIGENT_DISABLE_MCP") != "1":
        return True
    approved = os.environ.get("OMNIGENT_TRUSTED_MCP_SHA256", "")
    return bool(approved) and mcp_config_digest(config) in (
        entry.strip() for entry in approved.split(",")
    )
