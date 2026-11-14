#!/usr/bin/env python3
"""Create the Claude cloud environment this skill runs in: trusted network, no secrets.

    cloud-env.py [NAME]     NAME defaults to fork-sync
    cloud-env.py --selftest

Run it on the machine whose `claude` login owns the cloud sessions, with
CLAUDE_CLOUD_BRIDGE set to the claude-cloud-acp directory (its login helper).
It prints the new environment's id.
"""

import json
import os
import subprocess
import sys
import urllib.request


def body(name: str) -> dict:
    return {
        "name": name,
        "kind": "anthropic_cloud",
        "description": "omnigent fork-sync - trusted network",
        "config": {
            "environment_type": "anthropic",
            "cwd": "/home/user",
            "init_script": None,
            "environment": {},
            "languages": [
                {"name": "python", "version": "3.11"},
                {"name": "node", "version": "20"},
            ],
            "network_config": {"allowed_hosts": [], "allow_default_hosts": True},
        },
    }


def api(path: str, payload: dict | None = None) -> dict:
    sys.path.insert(0, os.environ["CLAUDE_CLOUD_BRIDGE"])
    from claude_cloud_acp import API_BASE, _headers

    status = json.loads(subprocess.check_output(["claude", "auth", "status", "--json"]))
    request = urllib.request.Request(
        f"{API_BASE}{path}",
        data=None if payload is None else json.dumps(payload).encode(),
        headers=_headers() | {"x-organization-uuid": status["orgId"]},
    )
    with urllib.request.urlopen(request, timeout=30) as resp:
        return json.load(resp)


def main(name: str) -> None:
    existing = api("/v1/environment_providers?limit=100").get("environments", [])
    if any(env.get("name") == name for env in existing):
        sys.exit(f"!! an environment named {name!r} exists; pick another name")
    created = api("/v1/environment_providers/cloud/create", body(name))
    print(f"environment: {created.get('environment_id')} ({name})")


def selftest() -> None:
    config = body("n")["config"]
    assert config["environment"] == {}
    print("selftest ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        selftest()
    else:
        main(sys.argv[1] if len(sys.argv) > 1 else "fork-sync")
