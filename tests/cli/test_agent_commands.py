"""``omnigent agent add|list|remove`` against a mocked ``/v1/agents``."""

from __future__ import annotations

import gzip
import io
import shutil
import tarfile
from pathlib import Path

import click
import httpx
import pytest
import yaml
from click.testing import CliRunner

from omnigent import cli as cli_mod
from omnigent.spec import load as load_spec


class _FakeServer:
    """In-memory ``/v1/info`` + ``/v1/agents`` behind the real CLI client."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.agent_install = True
        self.sessions_in_use: str | None = None
        # The caller's own agents (GET /v1/agents?scope=user), one page per entry.
        self.pages = [
            {
                "data": [{"id": "ag_orion", "name": "orion", "version": 2, "harness": "codex"}],
                "has_more": True,
                "last_id": "ag_row50",
            },
            {
                "data": [{"id": "ag_vega", "name": "vega", "version": 1}],
                "has_more": False,
                "last_id": "ag_vega",
            },
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if request.url.path == "/v1/info":
            return httpx.Response(
                200, json={"agent_install": self.agent_install} if self.agent_install else {}
            )
        if request.method == "POST":
            return httpx.Response(200, json={"id": "ag_orion", "name": "orion", "version": 3})
        if request.method == "DELETE":
            if self.sessions_in_use and request.url.params.get("force") != "true":
                return httpx.Response(
                    409,
                    json={
                        "error": {"code": "agent_in_use"},
                        "sessions_in_use": self.sessions_in_use,
                    },
                )
            return httpx.Response(200, json={"id": "ag_orion", "deleted": True})
        assert request.url.params.get("scope") == "user", "only the caller's agents are listed"
        page = 1 if request.url.params.get("after") == "ag_row50" else 0
        return httpx.Response(200, json=self.pages[page])

    def calls(self) -> list[tuple[str, str, str]]:
        return [(r.method, r.url.path, r.url.query.decode()) for r in self.seen]


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch) -> _FakeServer:
    """Route the CLI's real API client to :class:`_FakeServer`."""
    from omnigent.util.server_url import ServerUrl

    fake = _FakeServer()
    real_client = httpx.Client
    monkeypatch.setattr(
        cli_mod, "_resolve_attach_server_url", lambda server, configured: ServerUrl("http://t")
    )
    monkeypatch.setattr("omnigent.chat._remote_headers", lambda **kwargs: {})
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(fake), base_url=kwargs["base_url"]
        ),
    )
    return fake


def test_add_uploads_the_whole_directory(tmp_path: Path, server: _FakeServer) -> None:
    (tmp_path / "config.yaml").write_text("spec_version: 1\nname: orion\n")
    (tmp_path / "agents" / "worker").mkdir(parents=True)
    (tmp_path / "agents" / "worker" / "config.yaml").write_text("name: worker\n")

    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Installed orion (version 3" in result.output
    assert "terminal sessions do when they relaunch" in result.output
    (bundle,) = _posted_tars(server)
    assert {"config.yaml", "agents/worker/config.yaml"} <= {n.lstrip("./") for n in bundle}


def _posted_tars(server: _FakeServer) -> list[dict[str, bytes]]:
    """The files of each uploaded bundle, in upload order."""
    out = []
    for post in (r for r in server.seen if r.method == "POST"):
        body = post.read()
        boundary = post.headers["content-type"].split("boundary=")[1].encode()
        start = body.index(b"\x1f\x8b")
        tar_bytes = body[start : body.index(b"\r\n--" + boundary, start)]
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(tar_bytes)), mode="r:") as tf:
            out.append({m.name: tf.extractfile(m).read() for m in tf.getmembers() if m.isfile()})
    return out


_FakeRepo = tuple[Path, list[tuple[str, str | None]]]


@pytest.fixture()
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _FakeRepo:
    """A repo with two agents (one with a worker); "cloning" copies it."""
    repo = tmp_path / "src"
    (repo / "orion" / "agents" / "worker").mkdir(parents=True)
    (repo / "orion" / "config.yaml").write_text(
        "name: orion\nllm: {connection: {api_key: ${SECRET}}}\n"
    )
    (repo / "orion" / "agents" / "worker" / "config.yaml").write_text("name: worker\n")
    (repo / "vega").mkdir()
    (repo / "vega" / "config.yaml").write_text("name: vega\n")
    clones: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        cli_mod,
        "_clone_github_source",
        lambda url, ref, dest: (
            clones.append((url, ref)),
            shutil.copytree(repo, dest, symlinks=True),
        ),
    )
    monkeypatch.setenv("SECRET", "leaked")
    return repo, clones


def test_add_github_repo_installs_each_agent_unexpanded(
    fake_repo: _FakeRepo, server: _FakeServer
) -> None:
    result = CliRunner().invoke(
        cli_mod.cli, ["agent", "add", "https://github.com/acme/agents/tree/v1"], input="y\n"
    )

    assert result.exit_code == 0, result.output
    assert fake_repo[1] == [("https://github.com/acme/agents.git", "v1")]
    assert "Found 2 agent(s)" in result.output
    orion, vega = sorted(_posted_tars(server), key=len, reverse=True)
    assert set(orion) == {"config.yaml", "agents/worker/config.yaml"}, "worker stays in its parent"
    assert b"${SECRET}" in orion["config.yaml"], "a remote repo must not read this shell's env"
    assert set(vega) == {"config.yaml"}


def test_add_github_repo_at_name_picks_one_agent(
    fake_repo: _FakeRepo, server: _FakeServer
) -> None:
    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/agents@Vega", "-y"])
    assert result.exit_code == 0, result.output
    assert [set(t) for t in _posted_tars(server)] == [{"config.yaml"}]
    assert "Found 1 agent(s)" in result.output


def test_add_github_repo_list_installs_nothing(fake_repo: _FakeRepo, server: _FakeServer) -> None:
    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/agents", "--list"])
    assert result.exit_code == 0, result.output
    assert "orion  (orion)" in result.output and "vega  (vega)" in result.output
    assert not [r for r in server.seen if r.method == "POST"]


def test_add_github_repo_keeps_going_past_a_conflict(
    fake_repo: _FakeRepo, server: _FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = iter([httpx.Response(409, json={"detail": "'orion' is a server agent"})])
    real = server.__call__
    monkeypatch.setattr(
        _FakeServer,
        "__call__",
        lambda self, r: next(replies, None) or real(r) if r.method == "POST" else real(r),
    )
    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/agents", "-y"])
    assert result.exit_code != 0
    assert "Skipped orion: 409" in result.stderr
    assert "1 of 2 agent(s) not installed" in result.stderr
    assert "Installed" in result.output, "vega is still uploaded after orion's conflict"


def test_add_github_repo_declined_uploads_nothing(
    fake_repo: _FakeRepo, server: _FakeServer
) -> None:
    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/agents"], input="n\n")
    assert result.exit_code != 0
    assert not [r for r in server.seen if r.method == "POST"]


def test_add_github_repo_refuses_symlinks(fake_repo: _FakeRepo, server: _FakeServer) -> None:
    (fake_repo[0] / "vega" / "key").symlink_to(Path.home() / ".ssh" / "id_rsa")
    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/agents@vega", "-y"])
    assert result.exit_code != 0
    assert "symlink" in result.output
    assert not [r for r in server.seen if r.method == "POST"]


@pytest.mark.parametrize(
    ("source", "hinted"), [("acme/agents", True), ("https://github.com/acme/agents", False)]
)
def test_add_github_clone_failure_hints_a_missing_local_path(
    source: str, hinted: bool, server: _FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.cli_diagnostics import suppresses_recovery_hint

    def fail_clone(url: str, ref: str | None, dest: Path) -> None:
        raise cli_mod._agent_cli_error(f"Could not clone {url}.")

    monkeypatch.setattr(cli_mod, "_clone_github_source", fail_clone)
    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", source], standalone_mode=False)
    assert isinstance(result.exception, click.ClickException), result.output
    message = result.exception.message
    assert message.startswith("Could not clone https://github.com/acme/agents.git.")
    assert (f"(If {source!r} is a local path, it doesn't exist.)" in message) is hinted
    assert suppresses_recovery_hint(result.exception)


def test_add_github_repo_with_symlinked_tmpdir(
    fake_repo: _FakeRepo, server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS's /var -> /private/var: discovered paths must still compare to the checkout."""
    (tmp_path / "realtmp").mkdir()
    (tmp_path / "linktmp").symlink_to(tmp_path / "realtmp")
    monkeypatch.setattr(cli_mod.tempfile, "tempdir", str(tmp_path / "linktmp"))
    listed = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/agents", "--list"])
    assert listed.exit_code == 0, listed.output
    added = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/agents", "-y"])
    assert added.exit_code == 0, added.output
    assert len(_posted_tars(server)) == 2


@pytest.mark.parametrize("other_name", ["vega", "VEGA"])
def test_add_github_repo_rejects_duplicate_names(
    fake_repo: _FakeRepo, server: _FakeServer, other_name: str
) -> None:
    (fake_repo[0] / "vega2").mkdir()
    (fake_repo[0] / "vega2" / "config.yaml").write_text(f"name: {other_name}\n")
    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/agents@vega", "-y"])
    assert result.exit_code != 0
    assert "share a name: vega, vega2" in result.output
    assert not _posted_tars(server)


def test_add_local_list_does_not_upload(tmp_path: Path, server: _FakeServer) -> None:
    (tmp_path / "config.yaml").write_text("name: orion\n")
    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", str(tmp_path), "--list"])
    assert result.exit_code != 0
    assert "--list only applies to a GitHub repo" in result.output
    assert not [r for r in server.seen if r.method == "POST"]


def test_add_github_repo_escapes_control_chars_in_names(
    fake_repo: _FakeRepo, server: _FakeServer
) -> None:
    (fake_repo[0] / "vega" / "config.yaml").write_text(
        'name: "evil\\x1b[2K\\rsafe\\x1b]8;;https://x\\x07"\n'
    )
    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/agents", "--list"])
    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.output and "\r" not in result.output
    assert "evil\\x1b[2K\\rsafe" in result.output


def test_add_github_repo_root_agent_skips_dot_git(
    fake_repo: _FakeRepo, server: _FakeServer
) -> None:
    (fake_repo[0] / "config.yaml").write_text("name: root\n")
    (fake_repo[0] / ".git").mkdir()
    (fake_repo[0] / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/agents", "-y"])
    assert result.exit_code == 0, result.output
    (bundle,) = _posted_tars(server)
    assert "config.yaml" in bundle and "orion/config.yaml" in bundle
    assert not [n for n in bundle if n.startswith(".git")]


def test_add_github_repo_refuses_lfs_pointers(fake_repo: _FakeRepo, server: _FakeServer) -> None:
    (fake_repo[0] / "vega" / "model.bin").write_text(
        "version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 1\n"
    )
    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/agents@vega", "-y"])
    assert result.exit_code != 0
    assert "model.bin is stored in Git LFS" in result.output


def test_add_github_repo_list_applies_the_name_filter(
    fake_repo: _FakeRepo, server: _FakeServer
) -> None:
    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/agents@vega", "--list"])
    assert result.exit_code == 0, result.output
    assert "Found 1 agent(s)" in result.output and "orion" not in result.output
    bad = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/agents@nope", "--list"])
    assert bad.exit_code != 0 and "No agent named 'nope'" in bad.output


def test_add_github_repo_refuses_a_symlinked_subpath(
    fake_repo: _FakeRepo, server: _FakeServer
) -> None:
    (fake_repo[0] / "alias").symlink_to(fake_repo[0] / "vega")
    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/agents/alias", "-y"])
    assert result.exit_code != 0
    assert "goes through a symlink" in result.output


@pytest.mark.parametrize(
    ("source", "parsed"),
    [
        ("acme/agents", ("https://github.com/acme/agents.git", None, "", None)),
        (
            "github:acme/agents/sub/x@orion",
            ("https://github.com/acme/agents.git", None, "sub/x", "orion"),
        ),
        (
            "https://github.com/acme/agents/tree/v1@orion",
            ("https://github.com/acme/agents.git", "v1", "", "orion"),
        ),
        (
            "https://github.com/acme/agents/tree/v1/sub@orion",
            ("https://github.com/acme/agents.git", "v1", "sub", "orion"),
        ),
        ("./orion", None),
        ("acme/../etc", None),
    ],
)
def test_parse_github_source(source: str, parsed: object) -> None:
    assert cli_mod._parse_github_source(source) == parsed


def test_clone_github_source_is_hardened(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[tuple[list[str], dict[str, str]]] = []

    def run(cmd: list[str], **kwargs: object) -> object:
        seen.append((cmd, kwargs["env"]))  # type: ignore[arg-type]
        return type("P", (), {"returncode": 128, "stderr": "fatal: not found\n"})()

    monkeypatch.setattr(cli_mod.subprocess, "run", run)
    with pytest.raises(click.ClickException, match=r"Could not clone .*: fatal: not found"):
        cli_mod._clone_github_source("https://github.com/a/b.git", "v1", tmp_path / "r")
    cmd, env = seen[0]
    assert {"core.hooksPath=/dev/null", "protocol.allow=never", "--no-recurse-submodules"} <= set(
        cmd
    )
    assert cmd[cmd.index("--branch") + 1] == "v1" and cmd[-3] == "--"
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_LFS_SKIP_SMUDGE"] == "1"
    # A hex-looking name can be a real branch or tag, so git decides; a failure hints at SHAs.
    with pytest.raises(click.ClickException, match="not commit SHAs"):
        cli_mod._clone_github_source("https://github.com/a/b.git", "deadbee", tmp_path / "r")
    assert seen[1][0][seen[1][0].index("--branch") + 1] == "deadbee"


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (FileNotFoundError("git"), "needs git on your PATH"),
        (cli_mod.subprocess.TimeoutExpired("git", 300), "Timed out cloning"),
    ],
)
def test_clone_github_source_reports_tool_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception, message: str
) -> None:
    def run(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(cli_mod.subprocess, "run", run)
    with pytest.raises(click.ClickException, match=message):
        cli_mod._clone_github_source("https://github.com/a/b.git", None, tmp_path / "r")


def test_add_github_repo_refuses_a_symlinked_config_before_reading_it(
    fake_repo: _FakeRepo, server: _FakeServer, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_text("name: stolen-secret\n")
    (fake_repo[0] / "vega" / "config.yaml").unlink()
    (fake_repo[0] / "vega" / "config.yaml").symlink_to(outside)
    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/agents", "--list"])
    assert result.exit_code != 0
    assert "vega/config.yaml is a symlink" in result.output
    assert "stolen-secret" not in result.output


def test_add_github_repo_tolerates_a_non_utf8_config(
    fake_repo: _FakeRepo, server: _FakeServer
) -> None:
    (fake_repo[0] / "vega" / "config.yaml").write_bytes(b"name: \xff\xfe\n")
    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/agents", "--list"])
    assert result.exit_code == 0, result.output
    assert "vega  (vega)" in result.output, "falls back to the directory name"


def test_add_github_repo_escapes_server_names_and_errors(
    fake_repo: _FakeRepo, server: _FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = iter(
        [
            httpx.Response(422, json={"detail": "bad key \x1b]8;;https://x\x07"}),
            httpx.Response(200, json={"id": "ag_1", "name": "ok\x1b[2K\r", "version": 1}),
        ]
    )
    real = server.__call__
    monkeypatch.setattr(
        _FakeServer, "__call__", lambda self, r: next(replies) if r.method == "POST" else real(r)
    )
    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/agents", "-y"])
    assert "\x1b" not in result.output + result.stderr and "\r" not in result.output
    assert "Installed ok\\x1b[2K\\r" in result.output
    assert "bad key \\x1b]8;;" in result.stderr


def test_list_pages_through_your_agents(server: _FakeServer) -> None:
    result = CliRunner().invoke(cli_mod.cli, ["agent", "list"])
    assert result.exit_code == 0, result.output
    rows = [line.split() for line in result.output.splitlines()]
    header = rows.index(["Name", "Version", "Harness", "ID"])
    assert [row for row in rows[header + 1 :] if len(row) == 4] == [
        ["orion", "2", "codex", "ag_orion"],
        ["vega", "1", "-", "ag_vega"],
    ]
    assert [q for m, _, q in server.calls() if m == "GET" and "scope" in q] == [
        "scope=user&limit=50",
        "scope=user&limit=50&after=ag_row50",
    ]


@pytest.mark.parametrize("configured", [None, "local"])
def test_server_local_means_the_local_server(
    server: _FakeServer, monkeypatch: pytest.MonkeyPatch, configured: str | None
) -> None:
    """``local`` (the flag, or the configured default) is the local server, as for
    ``omnigent run``, not a remote URL to reject."""
    from types import SimpleNamespace

    def refuse(_server: str | None, _configured: object) -> None:
        raise AssertionError("'local' must not be resolved as a remote URL")

    monkeypatch.setattr(cli_mod, "_resolve_attach_server_url", refuse)
    monkeypatch.setattr(cli_mod, "_load_effective_config", lambda: {"server": configured})
    monkeypatch.setattr(
        cli_mod,
        "ensure_local_omnigent_server",
        lambda: SimpleNamespace(url="http://127.0.0.1:6767"),
    )
    args = ["agent", "list"] + (["--server", "local"] if configured is None else [])
    result = CliRunner().invoke(cli_mod.cli, args)
    assert result.exit_code == 0, result.output
    assert {r.url.host for r in server.seen} == {"127.0.0.1"}


def test_list_without_agents_says_how_to_add_one(server: _FakeServer) -> None:
    server.pages = [{"data": [], "has_more": False, "last_id": None}]
    result = CliRunner().invoke(cli_mod.cli, ["agent", "list"])
    assert result.exit_code == 0, result.output
    assert "No agents yet. Install one with: omnigent agent add <path>" in result.output


def test_agent_is_a_registered_subcommand() -> None:
    assert "agent" in cli_mod._CLICK_SUBCOMMANDS


def test_remove_deletes_by_id(server: _FakeServer) -> None:
    result = CliRunner().invoke(cli_mod.cli, ["agent", "remove", "orion"])
    assert result.exit_code == 0, result.output
    assert server.calls()[-1] == ("DELETE", "/v1/agents/ag_orion", "")


def test_remove_refuses_an_ambiguous_name_and_takes_an_id(server: _FakeServer) -> None:
    server.pages[1]["data"].append({"id": "ag_orion_old", "name": "orion", "version": 1})

    ambiguous = CliRunner().invoke(cli_mod.cli, ["agent", "remove", "orion"])
    assert ambiguous.exit_code != 0
    assert "2 agents named 'orion'; remove one by id: ag_orion, ag_orion_old" in ambiguous.output
    assert not any(m == "DELETE" for m, _, _ in server.calls())

    by_id = CliRunner().invoke(cli_mod.cli, ["agent", "remove", "ag_orion_old"])
    assert by_id.exit_code == 0, by_id.output
    assert server.calls()[-1] == ("DELETE", "/v1/agents/ag_orion_old", "")


def test_remove_unknown_name_fails(server: _FakeServer) -> None:
    result = CliRunner().invoke(cli_mod.cli, ["agent", "remove", "polly"])
    assert result.exit_code != 0
    assert "You have no agent named 'polly'" in result.output


def test_remove_in_use_asks_before_breaking_sessions(server: _FakeServer) -> None:
    server.sessions_in_use = "2"
    declined = CliRunner().invoke(cli_mod.cli, ["agent", "remove", "orion"], input="n\n")
    assert declined.exit_code != 0
    assert "2 session(s) still use orion; removing it will break them" in declined.output
    assert not any("force=true" in q for _, _, q in server.calls())

    accepted = CliRunner().invoke(cli_mod.cli, ["agent", "remove", "orion"], input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert server.calls()[-1] == ("DELETE", "/v1/agents/ag_orion", "force=true")


def test_remove_yes_skips_the_prompt(server: _FakeServer) -> None:
    server.sessions_in_use = "2"
    result = CliRunner().invoke(cli_mod.cli, ["agent", "remove", "orion", "--yes"])
    assert result.exit_code == 0, result.output
    assert server.calls()[-1] == ("DELETE", "/v1/agents/ag_orion", "force=true")


@pytest.mark.parametrize("args", [["add"], ["list"], ["remove", "orion"]])
def test_older_server_is_reported_not_treated_as_empty(
    server: _FakeServer, args: list[str], tmp_path: Path
) -> None:
    server.agent_install = False
    if args == ["add"]:
        (tmp_path / "config.yaml").write_text("spec_version: 1\nname: orion\n")
        args = ["add", str(tmp_path)]
    result = CliRunner().invoke(cli_mod.cli, ["agent", *args])
    assert result.exit_code != 0
    assert "does not support installing agents" in result.output
    assert [path for _, path, _ in server.calls()] == ["/v1/info"]


def test_agent_errors_skip_the_stale_host_hint(server: _FakeServer) -> None:
    """A 409 or unknown name has nothing to do with stale host processes."""
    from omnigent.cli_diagnostics import suppresses_recovery_hint

    result = CliRunner().invoke(cli_mod.cli, ["agent", "remove", "polly"], standalone_mode=False)
    assert isinstance(result.exception, click.ClickException)
    assert suppresses_recovery_hint(result.exception)


# Claude Code subagent import

_REVIEWER = """---
name: reviewer
description: Reviews a diff for bugs.
tools: Read, Grep
disallowedTools: Bash
skills: [triage]
model: sonnet
---
Review the diff. Report only real bugs.
"""


def _write_plugin(root: Path, reviewer: str = _REVIEWER) -> None:
    (root / "kit" / ".claude-plugin").mkdir(parents=True)
    (root / "kit" / ".claude-plugin" / "plugin.json").write_text('{"name": "kit"}')
    (root / "kit" / "agents").mkdir()
    (root / "kit" / "agents" / "reviewer.md").write_text(reviewer)
    (root / "kit" / "agents" / "scout.md").write_text(
        "---\nname: scout\ndescription: Finds files.\n---\nFind files.\n"
    )


def test_add_a_claude_code_agent_file_keeps_name_restrictions_and_instructions(
    tmp_path: Path, server: _FakeServer
) -> None:
    source = tmp_path / "reviewer.md"
    source.write_text(_REVIEWER)

    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", str(source)])

    assert result.exit_code == 0, result.output
    (bundle,) = _posted_tars(server)
    config = yaml.safe_load(bundle["config.yaml"])
    assert config["name"] == "reviewer"
    assert config["description"] == "Reviews a diff for bugs."
    assert config["executor"]["config"] == {
        "allowed_tools": ["Read", "Grep"],
        "disallowed_tools": ["Bash"],
    }
    assert config["skills"] == ["triage"]
    assert bundle["AGENTS.md"].decode().strip() == "Review the diff. Report only real bugs."
    assert "harness" not in config["executor"].get("config", {}), "the agent stays portable"


def test_an_imported_agent_loads_as_a_spec_with_its_restrictions(tmp_path: Path) -> None:
    from omnigent.claude_code_agent import claude_code_agent_config, parse_claude_code_agent

    agent = parse_claude_code_agent(_REVIEWER)
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "config.yaml").write_text(yaml.safe_dump(claude_code_agent_config(agent)))
    (bundle_dir / "AGENTS.md").write_text(agent.instructions)

    spec = load_spec(bundle_dir)

    assert spec.name == "reviewer"
    assert spec.description == "Reviews a diff for bugs."
    assert spec.executor.config["allowed_tools"] == ["Read", "Grep"]
    assert spec.executor.config["disallowed_tools"] == ["Bash"]
    assert spec.instructions == "Review the diff. Report only real bugs."


@pytest.mark.parametrize(
    "text", ["no frontmatter\n", "---\nname: x\n", "---\ndescription: d\n---\nbody\n"]
)
def test_add_refuses_a_file_that_is_not_a_claude_code_agent(
    tmp_path: Path, server: _FakeServer, text: str
) -> None:
    source = tmp_path / "bad.md"
    source.write_text(text)
    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", str(source)])
    assert result.exit_code != 0
    assert "is not a Claude Code agent" in result.stderr
    assert not [r for r in server.seen if r.method == "POST"]


def _fake_clone(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.setattr(
        cli_mod,
        "_clone_github_source",
        lambda url, ref, dest: shutil.copytree(repo, dest, symlinks=True),
    )


def test_add_repo_installs_each_plugin_agent_and_a_second_run_updates_without_duplicates(
    tmp_path: Path, server: _FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "src"
    _write_plugin(repo)
    _fake_clone(monkeypatch, repo)

    first = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/kit", "-y"])
    assert first.exit_code == 0, first.output
    assert "Found 2 agent(s)" in first.output
    assert sorted(yaml.safe_load(t["config.yaml"])["name"] for t in _posted_tars(server)) == [
        "reviewer",
        "scout",
    ]

    (repo / "kit" / "agents" / "reviewer.md").write_text(
        _REVIEWER.replace("Report only real bugs.", "Be brief.")
    )
    second = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/kit", "-y"])

    assert second.exit_code == 0, second.output
    posted = _posted_tars(server)[2:]
    assert sorted(yaml.safe_load(t["config.yaml"])["name"] for t in posted) == [
        "reviewer",
        "scout",
    ]
    reviewer = next(t for t in posted if yaml.safe_load(t["config.yaml"])["name"] == "reviewer")
    assert b"Be brief." in reviewer["AGENTS.md"], "the changed file is re-sent under its name"
    assert len({yaml.safe_load(t["config.yaml"])["name"] for t in posted}) == len(posted)


def test_add_repo_refuses_two_plugin_agents_with_one_name(
    tmp_path: Path, server: _FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "src"
    _write_plugin(repo)
    (repo / "kit" / "agents" / "scout.md").write_text(_REVIEWER)
    _fake_clone(monkeypatch, repo)

    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/kit", "-y"])

    assert result.exit_code != 0
    assert "share a name" in result.stderr


def test_add_repo_at_name_picks_one_plugin_agent(
    tmp_path: Path, server: _FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "src"
    _write_plugin(repo)
    _fake_clone(monkeypatch, repo)

    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/kit@scout", "-y"])

    assert result.exit_code == 0, result.output
    assert [yaml.safe_load(t["config.yaml"])["name"] for t in _posted_tars(server)] == ["scout"]


def test_add_says_tool_restrictions_apply_on_claude_native_only(
    tmp_path: Path, server: _FakeServer
) -> None:
    source = tmp_path / "reviewer.md"
    source.write_text(_REVIEWER)
    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", str(source)])
    assert result.exit_code == 0, result.output
    assert "tool restrictions are enforced on claude-native sessions only" in result.stderr

    plain = tmp_path / "scout.md"
    plain.write_text("---\nname: scout\n---\nFind files.\n")
    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", str(plain)])
    assert result.exit_code == 0, result.output
    assert "claude-native" not in result.stderr


@pytest.mark.parametrize("name", ["../../x", "a.b", "a b", "ui"])
def test_add_refuses_an_agent_name_the_server_would_reject(
    tmp_path: Path, server: _FakeServer, name: str
) -> None:
    source = tmp_path / "bad.md"
    source.write_text(f"---\nname: {name!r}\n---\nbody\n")
    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", str(source)])
    assert result.exit_code != 0
    assert "agent name" in result.stderr
    assert not [r for r in server.seen if r.method == "POST"]


def _add_repo_listing(
    repo: Path, monkeypatch: pytest.MonkeyPatch, server: _FakeServer
) -> tuple[list[str], str]:
    _fake_clone(monkeypatch, repo)
    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", "acme/kit", "-y"])
    names = [yaml.safe_load(t["config.yaml"])["name"] for t in _posted_tars(server)]
    return names, result.output + result.stderr


def _outside_agent(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("---\nname: leaked\n---\nOutside the checkout.\n")
    return outside


def test_add_repo_ignores_a_symlinked_agents_dir(
    tmp_path: Path, server: _FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "src"
    (repo / "kit" / ".claude-plugin").mkdir(parents=True)
    (repo / "kit" / ".claude-plugin" / "plugin.json").write_text("{}")
    (repo / "kit" / "agents").symlink_to(_outside_agent(tmp_path))
    names, output = _add_repo_listing(repo, monkeypatch, server)
    assert "leaked" not in names and "leaked" not in output


def test_add_repo_ignores_a_symlinked_plugin_manifest(
    tmp_path: Path, server: _FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "src"
    (repo / "kit" / ".claude-plugin").mkdir(parents=True)
    outside = _outside_agent(tmp_path)
    (outside / "plugin.json").write_text("{}")
    (repo / "kit" / ".claude-plugin" / "plugin.json").symlink_to(outside / "plugin.json")
    (repo / "kit" / "agents").mkdir()
    (repo / "kit" / "agents" / "scout.md").write_text("---\nname: scout\n---\nx\n")
    names, _ = _add_repo_listing(repo, monkeypatch, server)
    assert names == []


def test_add_repo_refuses_a_symlinked_agent_file(
    tmp_path: Path, server: _FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "src"
    _write_plugin(repo)
    (repo / "kit" / "agents" / "scout.md").unlink()
    (repo / "kit" / "agents" / "scout.md").symlink_to("reviewer.md")
    names, output = _add_repo_listing(repo, monkeypatch, server)
    assert names == []
    assert "is a symlink" in output


def test_find_plugin_agent_files_skips_git_dirs(tmp_path: Path) -> None:
    from omnigent.claude_code_agent import find_plugin_agent_files

    for plugin in ("kit", ".git/modules/kit"):
        base = tmp_path / plugin
        (base / ".claude-plugin").mkdir(parents=True)
        (base / ".claude-plugin" / "plugin.json").write_text("{}")
        (base / "agents").mkdir()
        (base / "agents" / "a.md").write_text("x")
    assert find_plugin_agent_files(tmp_path) == [tmp_path / "kit" / "agents" / "a.md"]
