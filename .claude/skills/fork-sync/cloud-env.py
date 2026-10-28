#!/usr/bin/env python3
"""Create the Claude cloud environment this skill runs in, with a fresh signing key.

    cloud-env.py ALLOWED_SIGNERS [NAME]     NAME defaults to fork-sync
    cloud-env.py --selftest

Run it on the machine whose `claude` login owns the cloud sessions, with
CLAUDE_CLOUD_BRIDGE set to the claude-cloud-acp directory (its login helper).
It generates an ed25519 key in a temporary directory and puts it, with
ALLOWED_SIGNERS plus the new key's line, into a new environment's variables;
no copy of the private key is kept. It prints the environment id and the
allowed_signers line every machine must add.

ponytail: create only. To rotate a key in place, update the existing environment
with `POST /v1/environment_providers/<id>`, the same headers and its `name`,
`description` and `config`, rather than creating a new one, which changes the
environment id every pin names.
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

COMMENT = "omnigent-cloud-sync"


def signer_list(current: str, line: str) -> str:
    """CURRENT without earlier cloud-sync keys, plus LINE."""
    kept = [entry for entry in current.splitlines() if not entry.rstrip().endswith(COMMENT)]
    return "\n".join([*kept, line]) + "\n"


def body(name: str, signers: str, private_key: bytes) -> dict:
    return {
        "name": name,
        "kind": "anthropic_cloud",
        "description": "omnigent fork-sync - trusted network, signing key",
        "config": {
            "environment_type": "anthropic",
            "cwd": "/home/user",
            "init_script": None,
            "environment": {
                "OMNIGENT_SYNC_ALLOWED_SIGNERS": base64.b64encode(signers.encode()).decode(),
                "OMNIGENT_SYNC_SIGNING_KEY": base64.b64encode(private_key).decode(),
            },
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


def main(signers_path: str, name: str) -> None:
    existing = api("/v1/environment_providers?limit=100").get("environments", [])
    if any(env.get("name") == name for env in existing):
        sys.exit(f"!! an environment named {name!r} exists; pick another name")
    with tempfile.TemporaryDirectory() as tmp:
        key = os.path.join(tmp, "key")
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", COMMENT, "-f", key], check=True
        )
        current = Path(signers_path).read_text()
        email = next(
            e for e in current.splitlines() if e.strip() and not e.startswith("#")
        ).split()[0]
        line = f"{email} {Path(key + '.pub').read_text().strip()}"
        payload = body(name, signer_list(current, line), Path(key).read_bytes())
        created = api("/v1/environment_providers/cloud/create", payload)
    print(f"environment: {created.get('environment_id')} ({name})")
    print(f"allowed_signers line: {line}")


def selftest() -> None:
    old = "# header\na@b ssh-ed25519 AAA host\na@b ssh-ed25519 OLD omnigent-cloud-sync\n"
    assert signer_list(old, "a@b ssh-ed25519 NEW omnigent-cloud-sync") == (
        "# header\na@b ssh-ed25519 AAA host\na@b ssh-ed25519 NEW omnigent-cloud-sync\n"
    )
    env = body("n", "s\n", b"k")["config"]["environment"]
    assert base64.b64decode(env["OMNIGENT_SYNC_SIGNING_KEY"]) == b"k"
    print("selftest ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        selftest()
    else:
        main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "fork-sync")
