"""scripts/claude_cloud.py: `setup` makes a colleague's cloud environment, its network secrets and pin, and `start`
runs a cloud session with no terminal, against a fake environment API and fake `fnox`, `gh` and `claude` (#1278)."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills/omnigent-agents/scripts/claude_cloud.py"
spec = importlib.util.spec_from_file_location("claude_cloud", SCRIPT)
assert spec and spec.loader
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

TOKEN = "ghp_" + "Zq" * 18 + "tokenTail"
B64 = base64.b64encode(TOKEN.encode()).decode()
HOST = "ghcr.io"
DEFINITION = {
    "network": {"access": "default", "allowed_hosts": ["pypi.org"]},
    "variables": {"REGISTRY_USER": "example"},
    "registries": [{"host": HOST, "secret": "GHCR_TOKEN"}],
    "proof": ["docker pull ghcr.io/example/private-image", "mise run test"],
}
SETUP = "#!/bin/bash\nmise install\n"


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def make_repo(tmp_path: Path, definition: dict | None = DEFINITION, name: str = "co") -> Path:
    bare = tmp_path / "origin" / "widget.git"
    bare.parent.mkdir(exist_ok=True)
    git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    root = tmp_path / name
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    git("remote", "add", "origin", str(bare), cwd=root)
    (root / ".claude").mkdir()
    (root / ".claude/claude-cloud-setup.sh").write_text(SETUP)
    if definition is not None:
        (root / ".claude/claude-cloud-environment.json").write_text(json.dumps(definition))
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "one", cwd=root)
    return root


class World:
    """The fake environment API and the network-secret endpoint, every request kept."""

    def __init__(self) -> None:
        self.envs: dict[str, dict] = {}
        self.creds: list[dict] = []  # {id, host, environment_id}
        self.requests: list[tuple[str, str, Any]] = []
        self.fail_credentials = ""

    def call(self, path, body, auth, masked=(), method=""):
        verb = method or ("GET" if body is None else "POST")
        self.requests.append((verb, path, body))
        route, _, _query = path.partition("?")
        if route == "/v1/environment_providers":
            return {
                "environments": [
                    {"environment_id": i, "name": e["name"], "kind": "anthropic_cloud"}
                    for i, e in self.envs.items()
                ],
                "has_more": False,
            }
        if route == "/v1/environment_providers/cloud/create":
            self.envs["env_new"] = {k: body[k] for k in ("name", "description", "config")}
            return {"environment_id": "env_new"}
        if route.startswith("/v1/environment_providers/"):
            env = route.rsplit("/", 1)[-1]
            if body is None:
                return {"environment_id": env, **self.envs[env]}
            self.envs[env] = {k: body[k] for k in ("name", "description", "config")}
            return {"environment_id": env}
        if route.startswith(cc.CREDENTIALS):
            if self.fail_credentials:
                raise cc.Refused(f"{verb} {cc.CREDENTIALS}: HTTP {self.fail_credentials}: nope")
            if verb == "GET":
                return {"credentials": list(self.creds)}
            if verb == "DELETE":
                ident = route.rsplit("/", 1)[-1]
                self.creds = [c for c in self.creds if c["id"] != ident]
                return {}
            self.creds.append({"id": f"cred{len(self.requests)}", "host": body["host"]})
            return {}
        raise AssertionError(path)


FAKES = {
    "fnox": """#!/bin/sh
echo "$FNOX_AGE_KEY_FILE" >> "$FAKE_LOG/fnox-key"
echo "$@" >> "$FAKE_LOG/fnox-args"
[ -n "$FAKE_FNOX_TOKEN" ] && echo "$FAKE_FNOX_TOKEN" && exit 0
exit 1
""",
    "gh": "#!/bin/sh\necho ana\n",
    "claude": """#!/bin/sh
echo "$@" >> "$FAKE_LOG/claude-args"
if [ -n "$FAKE_PROOF" ]; then
  c=$(git commit-tree 'HEAD^{tree}' -p HEAD -m "$FAKE_PROOF")
  git push -q origin "$c:refs/heads/claude-cloud-proof/ana"
fi
printf '\\033[1mSession\\033[0m https://claude.ai/code/session_01Abc23 started\\r\\n'
exit ${FAKE_EXIT:-0}
""",
}


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    w = World()
    monkeypatch.setattr(cc.ENVS, "call", w.call)
    monkeypatch.setattr(cc.ENVS, "headers", lambda *a, **k: {"Authorization": "Bearer login"})
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, text in FAKES.items():
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(stat.S_IRWXU)
    log = tmp_path / "log"
    log.mkdir()
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.delenv("FNOX_AGE_KEY_FILE", raising=False)
    monkeypatch.setenv("FAKE_FNOX_TOKEN", TOKEN)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for who in ("AUTHOR", "COMMITTER"):  # the fake session commits
        monkeypatch.setenv(f"GIT_{who}_NAME", "t")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "t@example.com")
    w.log = log  # type: ignore[attr-defined]
    return w


def no_wait() -> dict:
    return {"sleep": lambda _s: None}


def run_setup(root: Path, flag: bool = False, **kw: Any) -> int:
    return cc.setup(flag, path=str(root), ask=kw.pop("ask", lambda _p: ""), **no_wait(), **kw)


def assert_no_token(text: str) -> None:
    assert TOKEN not in text and B64 not in text and TOKEN[-12:] not in text


def test_setup_makes_the_environment_the_secret_and_the_pin_and_hides_the_token(
    tmp_path: Path, world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(tmp_path)
    (root / ".claude/settings.local.json").write_text('{"model": "x", "remote": {"a": 1}}')
    assert run_setup(root, proof=False) == 0
    out = capfd.readouterr()
    assert "created cloud environment widget (env_new)" in out.out
    config = world.envs["env_new"]["config"]
    assert config["init_script"] == SETUP
    assert config["environment"] == {"REGISTRY_USER": "example"}
    assert config["network_config"] == {
        "allowed_hosts": ["pypi.org", HOST],
        "allow_default_hosts": True,
    }
    # the host's secret: a bearer of the token's base64, on the registry host
    posts = [b for v, p, b in world.requests if v == "POST" and p == cc.CREDENTIALS]
    assert posts == [
        {
            "environment_id": "env_new",
            "host": HOST,
            "bearer": {"header_name": "Authorization", "prefix": "Bearer ", "value": B64},
        }
    ]
    # no other body, no printed line, no argument list holds it, in plain text or base64
    others = [json.dumps(b) for v, p, b in world.requests if p != cc.CREDENTIALS]
    assert_no_token("\n".join(others) + out.out + out.err)
    assert TOKEN not in json.dumps(posts)
    # the pin keeps the file's other keys
    pin = json.loads((root / ".claude/settings.local.json").read_text())
    assert pin == {"model": "x", "remote": {"a": 1, "defaultEnvironmentId": "env_new"}}


def test_a_secret_already_on_the_host_is_deleted_then_posted_and_an_update_keeps_other_variables(
    tmp_path: Path, world: World
) -> None:
    root = make_repo(tmp_path)
    world.envs["env_old"] = {
        "name": "widget",
        "description": "d",
        "config": {
            "init_script": "old",
            "environment": {"MINE": "kept", "REGISTRY_USER": "stale"},
            "network_config": {"allowed_hosts": [], "allow_default_hosts": True},
        },
    }
    world.creds = [{"id": "c1", "host": HOST}, {"id": "c2", "host": "other.example"}]
    assert run_setup(root, proof=False) == 0
    cred = [(v, p) for v, p, _ in world.requests if p.startswith(cc.CREDENTIALS)]
    assert cred == [
        ("GET", f"{cc.CREDENTIALS}?environment_id=env_old"),
        ("DELETE", f"{cc.CREDENTIALS}/c1?environment_id=env_old"),
        ("POST", cc.CREDENTIALS),
    ]
    assert [c["id"] for c in world.creds if c["host"] == HOST] != ["c1"]
    assert any(c["id"] == "c2" for c in world.creds)
    config = world.envs["env_old"]["config"]
    assert config["init_script"] == SETUP
    assert config["environment"] == {"MINE": "kept", "REGISTRY_USER": "example"}
    assert pin_of(root) == "env_old"
    # a second run changes nothing in the environment
    before = json.dumps(world.envs)
    assert run_setup(root, proof=False) == 0
    assert json.dumps(world.envs) == before


def pin_of(root: Path) -> str:
    return json.loads((root / ".claude/settings.local.json").read_text())["remote"][
        "defaultEnvironmentId"
    ]


def test_the_token_comes_from_fnox_with_the_default_key_file_else_a_hidden_prompt(
    tmp_path: Path, world: World, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(tmp_path)
    asked: list[str] = []
    assert run_setup(root, proof=False, ask=lambda p: asked.append(p) or "never") == 0
    assert asked == []
    key = (world.log / "fnox-key").read_text().strip()  # type: ignore[attr-defined]
    assert key == str(tmp_path / "home/.ssh/id_ed25519")
    assert (world.log / "fnox-args").read_text().strip() == "-P claude-cloud get GHCR_TOKEN"  # type: ignore[attr-defined]
    # a key file already set is kept
    monkeypatch.setenv("FNOX_AGE_KEY_FILE", "/keys/mine")
    (world.log / "fnox-key").unlink()  # type: ignore[attr-defined]
    assert run_setup(root, proof=False) == 0
    assert (world.log / "fnox-key").read_text().strip() == "/keys/mine"  # type: ignore[attr-defined]
    # no token from fnox: the prompt, whose answer is used, never printed
    monkeypatch.setenv("FAKE_FNOX_TOKEN", "")
    world.requests.clear()
    capfd.readouterr()
    assert run_setup(root, proof=False, ask=lambda p: asked.append(p) or f" {TOKEN} ") == 0
    assert len(asked) == 1 and HOST in asked[0]
    out = capfd.readouterr()
    assert_no_token(out.out + out.err)
    post = [b for v, p, b in world.requests if v == "POST" and p == cc.CREDENTIALS]
    assert post[0]["bearer"]["value"] == B64
    # neither: refused, nothing sent
    world.requests.clear()
    assert run_setup(root, proof=False, ask=lambda _p: "") == 1
    assert "no token for ghcr.io" in capfd.readouterr().err
    assert world.requests == []


def test_a_failed_secrets_endpoint_stops_setup_and_only_the_flag_puts_the_token_in_a_variable(
    tmp_path: Path, world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(tmp_path)
    world.fail_credentials = "403"
    assert run_setup(root, proof=False) == 1
    out = capfd.readouterr()
    assert "HTTP 403" in out.err and "--secret-as-variable" in out.err
    assert_no_token(json.dumps(world.envs) + out.out + out.err)
    assert not (root / ".claude/settings.local.json").exists()
    # with the flag: no endpoint call, the token is the variable `<secret>`, and setup says so
    world.requests.clear()
    world.envs.clear()
    assert run_setup(root, flag=True, proof=False) == 0
    out = capfd.readouterr()
    assert world.envs["env_new"]["config"]["environment"]["GHCR_TOKEN"] == TOKEN
    assert not [p for _, p, _ in world.requests if p.startswith(cc.CREDENTIALS)]
    assert "token is now inside the VM" in out.out
    assert_no_token(out.out + out.err)


def test_a_variable_that_looks_like_a_token_is_refused_before_any_request(
    tmp_path: Path, world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    for bad in (
        "ghp_" + "a" * 36,
        "gho_abc",
        "github_pat_11AAAA",
        "sk-ant-api03-xyz",
        "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5",
        "0123456789abcdef0123456789abcdef01234567",
    ):
        root = make_repo(
            tmp_path, {**DEFINITION, "variables": {"X": bad}}, name=f"r{len(bad)}{bad[:3]}"
        )
        assert run_setup(root, proof=False) == 1
        assert "looks like a token" in capfd.readouterr().err
    assert world.requests == []
    assert not cc.ENVS.looks_secret("https://example.com/a/b")
    assert not cc.ENVS.looks_secret("example")


def test_proof_polls_the_branch_prints_each_exit_code_and_deletes_it(
    tmp_path: Path, world: World, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(tmp_path)
    monkeypatch.setenv(
        "FAKE_PROOF", "docker pull ghcr.io/example/private-image: exit 0\nmise run test: exit 0"
    )
    assert run_setup(root) == 0
    out = capfd.readouterr().out
    assert "session session_01Abc23" in out
    assert "proof: docker pull ghcr.io/example/private-image: exit 0" in out
    assert "proof: mise run test: exit 0" in out
    bare = tmp_path / "origin/widget.git"
    assert git("branch", "--list", "claude-cloud-proof/*", cwd=bare) == ""
    assert "claude --cloud" not in out  # the task is the claude argument, never echoed
    args = (world.log / "claude-args").read_text()  # type: ignore[attr-defined]
    assert "--cloud" in args and "docker pull ghcr.io/example/private-image" in args
    assert "claude-cloud-proof/ana" in args
    # a failing command makes setup exit 1; a branch that never appears is refused
    monkeypatch.setenv("FAKE_PROOF", "docker pull ghcr.io/example/private-image: exit 1")
    assert run_setup(root) == 1
    assert "proof: docker pull ghcr.io/example/private-image: exit 1" in capfd.readouterr().out
    monkeypatch.delenv("FAKE_PROOF")
    ticks = iter(range(0, 10_000, 400))
    code = cc.setup(
        False,
        path=str(root),
        sleep=lambda _s: None,
        clock=lambda: next(ticks),
    )
    assert code == 1 and "did not appear in 10 minutes" in capfd.readouterr().err


@pytest.mark.skipif(shutil.which("script") is None, reason="needs util-linux or BSD script")
def test_start_pushes_runs_claude_under_a_pseudo_terminal_prints_the_session_and_exits_with_its_code(
    tmp_path: Path, world: World, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(tmp_path)
    git("checkout", "-q", "-b", "feature", cwd=root)
    (root / ".claude/settings.local.json").write_text(
        '{"remote": {"defaultEnvironmentId": "env_x"}}'
    )
    task = tmp_path / "task.md"
    task.write_text("run the suite\n")
    assert cc.start(str(task), path=str(root)) == 0
    out = capfd.readouterr().out.splitlines()
    assert out == [
        "session session_01Abc23",
        "url https://claude.ai/code/session_01Abc23",
    ]
    assert "feature" in git("branch", "--list", cwd=tmp_path / "origin/widget.git")
    args = (world.log / "claude-args").read_text()  # type: ignore[attr-defined]
    assert args.startswith("--cloud run the suite") and "pull request" in args
    # claude's exit code is the command's
    monkeypatch.setenv("FAKE_EXIT", "7")
    assert cc.start(str(task), path=str(root)) == 7


def test_start_wraps_claude_in_script_as_each_platform_takes_it(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / ".claude/settings.local.json").write_text(
        '{"remote": {"defaultEnvironmentId": "env_x"}}'
    )
    seen: list[tuple[str, ...]] = []

    def run(*cmd: str, check: bool = True, **kw: Any) -> subprocess.CompletedProcess:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 3, "https://claude.ai/code/session_Z9\n", "")

    say = cc.Out()
    assert cc.start_session("do it", root, "main", run, say, platform="darwin") == 3
    assert seen[0] == ("git", "-C", str(root), "push", "-u", "origin", "main")
    assert seen[1] == ("script", "-q", "/dev/null", "claude", "--cloud", "do it")
    seen.clear()
    assert cc.start_session("do it", root, "main", run, say, platform="linux") == 3
    assert seen[1] == ("script", "-qec", "claude --cloud 'do it'", "/dev/null")


def test_a_project_without_an_environment_gets_one_line_naming_setup_and_nothing_hangs(
    tmp_path: Path, world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(tmp_path)
    task = tmp_path / "task.md"
    task.write_text("x")
    assert cc.start(str(task), path=str(root)) == 2
    err = capfd.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "claude-cloud:setup" in err[0] and "locally" in err[0]
    assert not (tmp_path / "origin/widget.git/refs/heads/main").exists()  # nothing pushed
    # the environment named after the repository is found and pinned instead
    world.envs["env_w"] = {"name": "widget", "description": "", "config": {}}
    seen: list[tuple[str, ...]] = []

    def run(*cmd: str, check: bool = True, **kw: Any) -> subprocess.CompletedProcess:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert cc.start_session("x", root, "main", run, cc.Out()) == 0
    assert pin_of(root) == "env_w"


def test_claude_cloud_env_and_its_old_name_run_the_shared_code_without_secrets(
    tmp_path: Path, world: World
) -> None:
    from test_omnigent_cloud_env import omnigent_agent

    root = make_repo(tmp_path)
    ENVS = omnigent_agent.ENVS
    for name in ("claude-cloud-env", "cloud-env"):
        world.envs.clear()
        world.requests.clear()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ENVS, "call", world.call)
            mp.setattr(ENVS, "headers", lambda *a, **k: {"Authorization": "Bearer login"})
            assert omnigent_agent.main([name, str(root)]) == 0
        config = world.envs["env_new"]["config"]
        assert config["environment"] == {"REGISTRY_USER": "example"}
        assert config["network_config"]["allowed_hosts"] == ["pypi.org", HOST]
        assert not [p for _, p, _ in world.requests if p.startswith(cc.CREDENTIALS)]


def test_access_none_allows_only_the_listed_hosts(tmp_path: Path, world: World) -> None:
    root = make_repo(
        tmp_path, {**DEFINITION, "network": {"access": "none", "allowed_hosts": ["pypi.org"]}}
    )
    assert run_setup(root, proof=False) == 0
    network = world.envs["env_new"]["config"]["network_config"]
    assert network == {"allowed_hosts": ["pypi.org", HOST], "allow_default_hosts": False}
    bad = make_repo(tmp_path, {**DEFINITION, "network": {"access": "full"}}, name="bad")
    assert run_setup(bad, proof=False) == 1


def test_the_output_masks_a_token_and_its_base64_and_the_prompt_is_hidden_input() -> None:
    import inspect

    say = cc.Out()
    say.hide(TOKEN)
    assert say.clean(f"a {TOKEN} b {B64} c") == "a [secret] b [secret] c"
    assert inspect.signature(cc.setup).parameters["ask"].default is cc.getpass.getpass


def test_the_session_url_is_read_through_terminal_escapes(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / ".claude/settings.local.json").write_text(
        '{"remote": {"defaultEnvironmentId": "env_x"}}'
    )

    def run(*cmd: str, check: bool = True, **kw: Any) -> subprocess.CompletedProcess:
        out = "\x1b[1mhttps://claude.ai/code/\x1b[0msession_Z9\x1b[0m\r\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    lines: list[str] = []
    say = cc.Out()
    say.stream = type("S", (), {"write": lambda _s, t: lines.append(t), "flush": lambda _s: None})()
    assert cc.start_session("x", root, "main", run, say) == 0
    assert "session session_Z9" in "".join(lines)
