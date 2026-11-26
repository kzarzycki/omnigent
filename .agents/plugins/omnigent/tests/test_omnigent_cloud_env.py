"""loop.md's `Cloud worker:` line, the claude.ai environment a cloud start or a launch dir pins (named on the line,
else the repository's, else Default, resolved on the host's claude login), and `cloud-env`, which makes that
environment from the committed setup script, against a fake environment API (#1119)."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_omnigent_agent import (  # noqa: F401  (the fixtures apply here by name)
    no_model,
    no_sleep,
    omnigent_agent,
    this_repo,
    trusted_folders,
)

ENVS = [
    {"environment_id": "env_dev", "name": "widget-dev", "kind": "anthropic_cloud"},
    {"environment_id": "env_repo", "name": "widget", "kind": "anthropic_cloud"},
    {"environment_id": "env_default", "name": "Default", "kind": "anthropic_cloud"},
]
SETUP = "#!/bin/bash\nmise install\n"


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def repo(tmp_path: Path, loop: str | None, setup: str | None = SETUP, name: str = "co") -> Path:
    """A checkout of ``widget`` whose commit holds ``loop`` as loop.md and ``setup`` as the setup script."""
    root = tmp_path / name
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    git("remote", "add", "origin", "https://example.com/acme/widget.git", cwd=root)
    if loop is not None:
        (root / "docs/agents").mkdir(parents=True)
        (root / "docs/agents/loop.md").write_text(f"# Loop facts\n\n{loop}\n")
    if setup is not None:
        (root / ".claude").mkdir()
        (root / ".claude/cloud-setup.sh").write_text(setup)
    git("add", "-A", cwd=root)
    git("commit", "-q", "--allow-empty", "-m", "one", cwd=root)
    return root


def pin(root: Path) -> str:
    path = root / ".claude" / "settings.local.json"
    return json.loads(path.read_text())["remote"]["defaultEnvironmentId"] if path.exists() else ""


def test_the_cloud_worker_line_absent_none_and_with_or_without_env() -> None:
    parse = omnigent_agent.cloud_worker
    # absent: claude-cloud, with the policy's gate ("")
    assert parse("# Loop facts\n\nOwner: someone\n") == ("claude-cloud", "", "")
    # none, alone or with its reason: host workers
    assert parse("Cloud worker: none\n") is None
    assert parse("Cloud worker: none (the VM has no Apple toolchain)\n") is None
    # an existing line keeps its meaning
    assert parse("Cloud worker: claude-cloud, gate test\n") == ("claude-cloud", "test", "")
    assert parse("Cloud worker: my-cloud, gate check-light\n") == ("my-cloud", "check-light", "")
    # env <name> is read
    assert parse("Cloud worker: claude-cloud, gate test, env widget-dev\n") == (
        "claude-cloud",
        "test",
        "widget-dev",
    )


def test_a_cloud_start_and_a_launch_dir_pin_the_environment_the_login_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed: list[int] = []
    envs = list(ENVS)

    def environments() -> list[dict]:
        listed.append(1)
        return envs

    monkeypatch.setattr(omnigent_agent, "cloud_environments", environments)
    pin_environment = omnigent_agent.pin_environment

    # env <name> on the line: its id, which replaces another pin
    named = repo(tmp_path, "Cloud worker: claude-cloud, gate test, env widget-dev", name="named")
    pin_environment(named)
    assert pin(named) == "env_dev"
    (named / ".claude/settings.local.json").write_text('{"remote": {"defaultEnvironmentId": "x"}}')
    pin_environment(named)
    assert pin(named) == "env_dev"

    # no env: the environment named after the repository
    plain = repo(tmp_path, "Cloud worker: claude-cloud, gate test", name="plain")
    pin_environment(plain)
    assert pin(plain) == "env_repo"

    # no env and a pin already there: kept, and the login is not asked
    listed.clear()
    (plain / ".claude/settings.local.json").write_text(
        '{"remote": {"defaultEnvironmentId": "own"}}'
    )
    pin_environment(plain)
    assert pin(plain) == "own" and listed == []

    # no line, no environment named after the repository: the account's Default
    envs[:] = [e for e in ENVS if e["name"] != "widget"]
    bare = repo(tmp_path, None, name="noline")
    pin_environment(bare)
    assert pin(bare) == "env_default"

    # a name the login has no environment for is refused, naming cloud-env, and nothing is pinned
    unknown = repo(tmp_path, "Cloud worker: claude-cloud, gate test, env nope", name="unknown")
    with pytest.raises(omnigent_agent.Fail, match=r"no cloud environment named 'nope'.*cloud-env"):
        pin_environment(unknown)
    assert pin(unknown) == ""

    # neither the repository's nor Default: refused, naming cloud-env
    envs[:] = ENVS[:1]
    lonely = repo(tmp_path, None, name="lonely")
    with pytest.raises(omnigent_agent.Fail, match=r"'widget' or 'Default'.*cloud-env"):
        pin_environment(lonely)
    assert pin(lonely) == ""

    # launch-dir: the branch's committed line names the environment; an explicit id still wins
    envs[:] = list(ENVS)
    remote = tmp_path / "widget.git"
    git("clone", "-q", "--bare", str(named), str(remote), cwd=tmp_path)
    url = f"file://{remote}"
    omnigent_agent.launch_dir(url, "main", None, tmp_path / "ld1", here=tmp_path)
    assert pin(tmp_path / "ld1") == "env_dev"
    omnigent_agent.launch_dir(url, "main", "env_given", tmp_path / "ld2", here=tmp_path)
    assert pin(tmp_path / "ld2") == "env_given"
    # a launch dir that is a worktree of a checkout, with no env on the line: the repository's
    git("remote", "add", "local", url, cwd=plain)
    git("push", "-q", "local", "main:plain", cwd=plain)
    git("push", "-q", url, "main:side", cwd=unknown)
    # ...but the checkout pins another environment, which a cloud session there would take (#1152):
    # the worktree takes that pin, as pin_environment gives a worktree with none
    omnigent_agent.launch_dir(url, "plain", None, tmp_path / "ld3", here=plain)
    assert pin(tmp_path / "ld3") == "own"
    # a line that names an environment the checkout's pin shadows (#1152): refused once resolved,
    # naming both, and what it made is taken back
    with pytest.raises(omnigent_agent.Fail, match=r"would pin environment env_dev.*'s own"):
        omnigent_agent.launch_dir(url, "main", None, tmp_path / "ld6", here=plain)
    assert not (tmp_path / "ld6").exists()
    assert git("branch", "--list", "launch/ld6", cwd=plain) == ""
    # a start in a worktree whose line names an environment its main checkout's pin shadows: refused
    git("worktree", "add", "-q", "-b", "wt", str(tmp_path / "wt"), cwd=named)
    (named / ".claude/settings.local.json").write_text('{"remote": {"defaultEnvironmentId": "x"}}')
    with pytest.raises(omnigent_agent.Fail, match=r"would pin environment env_dev.*'s x"):
        pin_environment(tmp_path / "wt")

    # an unknown name: the launch dir it made is taken back, a worktree's branch too
    with pytest.raises(omnigent_agent.Fail, match="cloud-env"):
        omnigent_agent.launch_dir(url, "side", None, tmp_path / "ld4", here=tmp_path)
    assert not (tmp_path / "ld4").exists()
    with pytest.raises(omnigent_agent.Fail, match="cloud-env"):
        omnigent_agent.launch_dir(url, "side", None, tmp_path / "ld5", here=plain)
    assert not (tmp_path / "ld5").exists()
    assert git("branch", "--list", "launch/ld5", cwd=plain) == ""
    assert str(tmp_path / "ld5") not in git("worktree", "list", cwd=plain)


TOKEN = "sk-ant-oat01-" + "Q" * 24 + "secretToken"
REFRESH = "sk-ant-ort01-" + "R" * 24 + "refreshToken"
# a variable already in the environment, longer than the refusal's cut, as a seeded auth.json is
SEEDED = "c2VlZGVkLWxvZ2luLXNlY3JldC12YWx1ZQ" + "S" * 1200 + "seededLoginTail=="


class FakeAPI(BaseHTTPRequestHandler):
    """The environment API: a list, a read, a create and an update, each request kept."""

    world: dict

    def log_message(self, *_: object) -> None:
        pass

    def reply(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_one(self, method: str) -> None:
        size = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(size)) if size else None
        self.world["requests"].append(
            (method, self.path, {k.lower(): v for k, v in self.headers.items()}, body)
        )
        envs = self.world["envs"]
        path = self.path.split("?")[0]
        if method == "GET" and path == "/v1/environment_providers":
            return self.reply(
                200,
                {
                    "environments": [
                        {"environment_id": i, "name": e["name"], "kind": "anthropic_cloud"}
                        for i, e in envs.items()
                    ],
                    "has_more": False,
                    "last_id": None,
                },
            )
        if method == "POST" and path == "/v1/environment_providers/cloud/create":
            envs["env_new"] = {
                "name": body["name"],
                "description": body["description"],
                "config": body["config"],
            }
            return self.reply(200, {"environment_id": "env_new", **envs["env_new"]})
        env = path.rsplit("/", 1)[-1]
        if env not in envs:
            return self.reply(404, {"error": {"message": "not found"}})
        if method == "GET":
            return self.reply(200, {"environment_id": env, **envs[env]})
        if self.world.get("refuse"):  # a refusal that echoes what was sent, the login's token too
            said = f"bad config: {json.dumps(body)} from {self.headers.get('Authorization')}"
            return self.reply(400, {"error": {"message": said}})
        envs[env] = {
            "name": body["name"],
            "description": body["description"],
            "config": body["config"],
        }
        return self.reply(200, {"environment_id": env, **envs[env]})

    def do_GET(self) -> None:
        self.handle_one("GET")

    def do_POST(self) -> None:
        self.handle_one("POST")


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fake API on localhost, a Claude Code login in a fake home, and a `claude` that records its arguments."""
    world: dict = {"requests": [], "envs": {}}
    handler = type("Handler", (FakeAPI,), {"world": world})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("CLAUDE_CLOUD_API_BASE", f"http://127.0.0.1:{server.server_port}")
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude/.credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": TOKEN,
                    "refreshToken": REFRESH,
                    "expiresAt": (time.time() + 3600) * 1000,
                }
            }
        )
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(omnigent_agent.ENVS.sys, "platform", "linux")  # no keychain: the file above
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    (bin_dir / "claude").write_text(
        f'#!/bin/sh\necho "$@" >> "{argv_log}"\necho \'{{"orgId": "org-1"}}\'\n'
    )
    (bin_dir / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    world["argv"] = argv_log
    yield world
    server.shutdown()


def assert_no_secret(text: str) -> None:
    for secret in (TOKEN, REFRESH, SEEDED):
        assert secret not in text and secret[-16:] not in text


def test_cloud_env_makes_the_environment_then_updates_its_setup_script_sending_no_secret(
    tmp_path: Path, api: dict, capfd: pytest.CaptureFixture[str]
) -> None:
    checkout = repo(tmp_path, "Cloud worker: claude-cloud, gate test")

    # none named after the repository: created from the committed script, with no variables
    assert omnigent_agent.main(["cloud-env", str(checkout)]) == 0
    out = capfd.readouterr()
    assert "created cloud environment widget (env_new)" in out.out
    method, path, headers, body = api["requests"][-1]
    assert (method, path) == ("POST", "/v1/environment_providers/cloud/create")
    assert body["name"] == "widget" and body["kind"] == "anthropic_cloud"
    assert body["config"]["init_script"] == SETUP and body["config"]["environment"] == {}
    assert (
        headers["authorization"] == f"Bearer {TOKEN}" and headers["x-organization-uuid"] == "org-1"
    )

    # a seeded login lands in it; a new script is an update with init_script, no kind, the variable kept
    api["envs"]["env_new"]["config"]["environment"] = {"CODEX_AUTH_JSON": SEEDED}
    (checkout / ".claude/cloud-setup.sh").write_text(SETUP + "uv sync\n")
    (checkout / "uncommitted").write_text("x")
    assert omnigent_agent.main(["cloud-env", str(checkout)]) == 0
    assert "already" in capfd.readouterr().out  # only the committed script counts
    git("commit", "-qam", "two", cwd=checkout)
    assert omnigent_agent.main(["cloud-env", str(checkout)]) == 0
    out = capfd.readouterr()
    assert "updated cloud environment widget (env_new)" in out.out
    method, path, _, body = api["requests"][-1]
    assert (method, path) == ("POST", "/v1/environment_providers/env_new")
    assert "kind" not in body and body["config"]["init_script"] == SETUP + "uv sync\n"
    assert body["config"]["environment"] == {"CODEX_AUTH_JSON": SEEDED}
    assert body["name"] == "widget"
    assert len(api["envs"]) == 1

    # a refusal that echoes the config back prints neither the variable nor the token
    api["refuse"] = True
    (checkout / ".claude/cloud-setup.sh").write_text(SETUP + "uv sync --frozen\n")
    git("commit", "-qam", "three", cwd=checkout)
    assert omnigent_agent.main(["cloud-env", str(checkout)]) == 1
    out = capfd.readouterr()
    assert "HTTP 400" in out.err

    # the token never went into an argument list, a log or the output
    assert_no_secret(out.out + out.err + api["argv"].read_text())
    for _, _, _, body in api["requests"]:
        assert TOKEN not in json.dumps(body) and REFRESH not in json.dumps(body)


def test_cloud_env_takes_the_line_s_env_name_and_needs_a_committed_script(
    tmp_path: Path, api: dict, capfd: pytest.CaptureFixture[str]
) -> None:
    named = repo(tmp_path, "Cloud worker: claude-cloud, gate test, env widget-dev", name="named")
    assert omnigent_agent.main(["cloud-env", str(named)]) == 0
    assert "created cloud environment widget-dev" in capfd.readouterr().out
    # a script only in the working tree is not sent
    bare = repo(tmp_path, None, setup=None, name="bare")
    (bare / ".claude").mkdir()
    (bare / ".claude/cloud-setup.sh").write_text(SETUP)
    requests = len(api["requests"])
    assert omnigent_agent.main(["cloud-env", str(bare)]) == 1
    assert "no .claude/cloud-setup.sh committed" in capfd.readouterr().err
    assert len(api["requests"]) == requests


def test_the_listing_follows_its_pages_and_two_environments_of_one_name_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envs = omnigent_agent.ENVS
    pages = {
        "": {"environments": ENVS[:2], "has_more": True, "last_id": "env_repo"},
        "env_repo": {"environments": ENVS[2:], "has_more": False, "last_id": "env_default"},
    }
    asked: list[str] = []

    def call(path: str, body: object, auth: dict, masked: tuple = ()) -> dict:
        after = path.partition("after_id=")[2]
        asked.append(after)
        return pages[after]

    monkeypatch.setattr(envs, "call", call)
    assert [e["environment_id"] for e in envs.environments({})] == [
        "env_dev",
        "env_repo",
        "env_default",
    ]
    assert asked == ["", "env_repo"]
    twice = [*ENVS, {"environment_id": "env_two", "name": "widget"}]
    with pytest.raises(envs.Refused, match="2 cloud environments are named 'widget'"):
        envs.pick(twice, "", "widget")


def pinned(root: Path, env: str) -> None:
    (root / ".claude").mkdir(exist_ok=True)
    (root / ".claude/settings.local.json").write_text(
        json.dumps({"remote": {"defaultEnvironmentId": env}})
    )


def test_cloud_env_applies_to_the_pinned_environment_and_never_makes_one_beside_it(
    tmp_path: Path, api: dict, capfd: pytest.CaptureFixture[str]
) -> None:
    """A checkout pinned to ``widget-dev`` while its name comes from the repository (``widget``) fails naming both,
    rather than create an empty ``widget`` beside the pinned one (#2078)."""
    api["envs"]["env_dev"] = {"name": "widget-dev", "description": "", "config": {}}
    checkout = repo(tmp_path, "Cloud worker: claude-cloud, gate test")
    pinned(checkout, "env_dev")
    assert omnigent_agent.main(["cloud-env", str(checkout)]) == 1
    err = capfd.readouterr().err
    assert (
        "pins environment 'widget-dev' (env_dev), but the repository's name names 'widget'" in err
    )
    assert "add `env widget-dev` to loop.md's Cloud worker line" in err
    assert list(api["envs"]) == ["env_dev"]  # nothing created
    assert not [r for r in api["requests"] if r[0] == "POST"]

    # the line naming the pinned environment updates it in place
    named = repo(tmp_path, "Cloud worker: claude-cloud, gate test, env widget-dev", name="named")
    pinned(named, "env_dev")
    assert omnigent_agent.main(["cloud-env", str(named)]) == 0
    assert "updated cloud environment widget-dev (env_dev)" in capfd.readouterr().out
    assert list(api["envs"]) == ["env_dev"]


def test_cloud_env_refuses_a_pin_the_login_has_no_environment_for(
    tmp_path: Path, api: dict, capfd: pytest.CaptureFixture[str]
) -> None:
    checkout = repo(tmp_path, "Cloud worker: claude-cloud, gate test")
    pinned(checkout, "env_gone")
    assert omnigent_agent.main(["cloud-env", str(checkout)]) == 1
    assert "pins environment env_gone, which this host's claude login has none of" in (
        capfd.readouterr().err
    )
    assert api["envs"] == {}


def test_cloud_env_reads_the_main_checkout_s_pin_from_a_linked_worktree(
    tmp_path: Path, api: dict, capfd: pytest.CaptureFixture[str]
) -> None:
    """A cloud session launched from a worktree takes its main checkout's pin, so that is the one checked."""
    api["envs"]["env_dev"] = {"name": "widget-dev", "description": "", "config": {}}
    main = repo(tmp_path, "Cloud worker: claude-cloud, gate test")
    pinned(main, "env_dev")
    git("worktree", "add", "-q", "--detach", str(tmp_path / "wt"), cwd=main)
    assert omnigent_agent.main(["cloud-env", str(tmp_path / "wt")]) == 1
    assert "pins environment 'widget-dev' (env_dev)" in capfd.readouterr().err
    assert list(api["envs"]) == ["env_dev"]
