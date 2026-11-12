#!/usr/bin/env python3
"""Create the fork's landing-only Claude cloud environment, without a key.

    landing-env.py [NAME]       NAME defaults to omnigent-land
    landing-env.py --print      the request body, sent nowhere
    landing-env.py --setup-script   the setup script it sends, with the guard inlined
    landing-env.py --selftest

Run it on the machine whose `claude` login owns the cloud sessions, with
CLAUDE_CLOUD_BRIDGE set to the claude-cloud-acp directory, as fork-sync's
cloud-env.py, whose API call it reuses. The environment's network reaches GitHub
only, package registries off; its setup script is landing-setup.sh with
landing-guard.py inlined, so the guard is fixed when the environment is created and no
branch a session checks out can replace it. It installs no packages; it sets
OMNIGENT_LANDING_ENVIRONMENT=1, the marker landing-guard.py acts on. It holds no key:
the owner adds the signing key's variable by hand in the environment's settings, and
no script generates or reads it.
"""

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GITHUB_HOSTS = ["github.com", "api.github.com"]
MARKER = "OMNIGENT_LANDING_ENVIRONMENT"
GUARD_LINE = "@LANDING_GUARD_PY@\n"
GUARD_END = "LANDING_GUARD_EOF"


def body(name: str, init_script: str) -> dict:
    return {
        "name": name,
        "kind": "anthropic_cloud",
        "description": "omnigent landing - GitHub only, no registries, owner-added signing key",
        "config": {
            "environment_type": "anthropic",
            "cwd": "/home/user",
            "init_script": init_script,
            "environment": {MARKER: "1"},
            "languages": [{"name": "python", "version": "3.11"}],
            "network_config": {"allowed_hosts": GITHUB_HOSTS, "allow_default_hosts": False},
        },
    }


def setup_script() -> str:
    """landing-setup.sh with landing-guard.py in place of its one placeholder line."""
    template = (HERE / "landing-setup.sh").read_text()
    guard = (HERE / "landing-guard.py").read_text()
    assert template.count(GUARD_LINE) == 1, "landing-setup.sh has one guard placeholder line"
    assert GUARD_END not in guard, "the guard can't hold the heredoc's end line"
    return template.replace(GUARD_LINE, guard if guard.endswith("\n") else guard + "\n")


def request_body(name: str) -> dict:
    return body(name, setup_script())


def main(name: str) -> None:
    spec = importlib.util.spec_from_file_location(
        "cloud_env", HERE.parent / "fork-sync" / "cloud-env.py"
    )
    cloud_env = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cloud_env)
    existing = cloud_env.api("/v1/environment_providers?limit=100").get("environments", [])
    if any(env.get("name") == name for env in existing):
        sys.exit(f"!! an environment named {name!r} exists; pick another name")
    created = cloud_env.api("/v1/environment_providers/cloud/create", request_body(name))
    print(f"environment: {created.get('environment_id')} ({name})")


def selftest() -> None:
    config = request_body("omnigent-land")["config"]
    assert config["environment"] == {MARKER: "1"}, "the marker is the only variable: no key"
    assert config["network_config"] == {
        "allowed_hosts": GITHUB_HOSTS,
        "allow_default_hosts": False,
    }
    guard = (HERE / "landing-guard.py").read_text()
    assert guard in config["init_script"], "the guard is inlined"
    assert GUARD_LINE not in config["init_script"]
    assert "/home/user" not in config["init_script"], "setup reads no checkout"
    assert not any(
        word in config["init_script"] for word in ("apt-get", "pip ", "npm ", "uv tool")
    )
    print("landing-env selftest ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        selftest()
    elif sys.argv[1:] == ["--print"]:
        print(json.dumps(request_body("omnigent-land"), indent=2))
    elif sys.argv[1:] == ["--setup-script"]:
        sys.stdout.write(setup_script())
    else:
        main(sys.argv[1] if len(sys.argv) > 1 else "omnigent-land")
