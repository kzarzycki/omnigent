"""omnigent_agent.py start reads loop.md on the host the session will run on, not this machine's (#1084)."""

import subprocess
from pathlib import Path
from urllib.error import HTTPError

import pytest
from test_omnigent_agent import omnigent_agent

OTHER = "far"
REPO = "/srv/repo"
LOOP = f"{REPO}/docs/agents/loop.md"


def not_found(path: str) -> omnigent_agent.Fail:
    error = HTTPError("http://x" + path, 404, "Not Found", None, None)  # type: ignore[arg-type]
    fail = omnigent_agent.Fail(f"GET {path}: HTTP 404 Not Found")
    fail.__cause__ = error
    return fail


class Server(omnigent_agent.Omnigent):
    """The real create and loop.md read over a stubbed API: host ``far``'s files and sessions."""

    def __init__(self, files: dict[str, str | None], live: bool = True) -> None:
        self.host_id, self.loops = "h", {}
        self.files = files  # absolute path -> text; a folder's entries are derived from it
        self.live = live
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[dict] = []
        self.broken: str | None = None  # a host path whose listing answers 500

    def hosts(self) -> dict[str, str]:
        return {"h": "here", OTHER: "far-name"}

    def agent_id(self, agent: str) -> str:
        return agent

    def harness(self, agent: str) -> str:
        return agent

    def home(self, name: str) -> dict | None:
        return {"id": "p"}

    def call(self, method: str, path: str, body: object = None) -> dict:
        self.calls.append((method, path))
        if (
            path == "/v1/hosts"
        ):  # a session row's host is matched by any name its row carries (#1693)
            return {"hosts": [{"host_id": h, "name": n} for h, n in self.hosts().items()]}
        if path.startswith(f"/v1/hosts/{OTHER}/files/"):  # no file route on this server
            raise not_found(path)
        if path.startswith(f"/v1/hosts/{OTHER}/filesystem/"):
            folder = ("/" + path.split("/filesystem/")[1].split("?")[0]).rstrip("/")
            if folder == self.broken:
                raise omnigent_agent.Fail(f"GET {path}: HTTP 500")
            names = {
                p[len(folder) :].lstrip("/").split("/")[0]
                for p in self.files
                if p.startswith(folder + "/")
            }
            if not names:
                raise not_found(path)
            return {"data": [{"name": n} for n in sorted(names)]}
        if path.startswith("/v1/sessions?"):
            live = [{"id": "far-s", "host_id": OTHER}] if self.live else []
            return {"data": live}
        if path == "/v1/sessions":
            self.bodies.append(body)  # type: ignore[arg-type]
            return {"id": "new"}
        raise AssertionError(path)

    def read_file(self, sid: str, path: str) -> str:
        assert sid == "far-s"
        self.calls.append(("READ", path))
        if self.files.get(path) is None:
            raise omnigent_agent.Gone(f"{path}: not found")
        return str(self.files[path])


CHECKOUT = {f"{REPO}/.git/HEAD": "", f"{REPO}/docs/agents/gatekeeper.md": ""}


def lead(og: Server, workspace: str = REPO, **kwargs: object) -> None:
    omnigent_agent.start(
        og, "claude", "t", "do X", Path("r.md"), None, 0,
        top_level=True, workspace=workspace, host_id=OTHER, subscribe=False, **kwargs,
    )  # fmt: skip


@pytest.fixture(autouse=True)
def quiet(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(omnigent_agent, "parent_id", lambda: "")
    monkeypatch.setattr(omnigent_agent.Path, "home", lambda: tmp_path / "home")
    monkeypatch.delenv("OMNIGENT_PERMISSION_MODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)


def test_a_lead_on_another_host_gets_the_autocompact_its_loop_md_there_names() -> None:
    og = Server({**CHECKOUT, LOOP: "# Loop\n\nLead autocompact: 150k\nWorker tools: full\n"})
    og.create("claude", "t", top_level=True, workspace=REPO, host_id=OTHER)
    og.create("claude", "t", workspace=REPO, host_id=OTHER, role="/x/worker.md")
    assert og.bodies[0]["terminal_launch_args"] == ["--autocompact", "150k"]
    assert "terminal_launch_args" not in og.bodies[1]  # `Worker tools: full`, read from there too
    assert [c for c in og.calls if c[0] == "READ"] == [("READ", LOOP)]  # one read, not one per key


@pytest.mark.parametrize(
    "files",
    [
        {**CHECKOUT},
        {f"{REPO}/.git/HEAD": ""},
        {f"{REPO}/src/x": "", "/docs/agents/loop.md": "Lead autocompact: 150k\n"},
    ],
    ids=["no loop.md", "no docs/agents", "outside a checkout"],
)
def test_a_remote_checkout_with_no_loop_md_reads_as_no_line(files: dict[str, str | None]) -> None:
    og = Server(files)
    og.create("claude", "t", top_level=True, workspace=REPO, host_id=OTHER)
    assert "terminal_launch_args" not in og.bodies[0]


def test_a_workspace_absent_on_the_host_fails_the_start_naming_both() -> None:
    og = Server({})
    with pytest.raises(omnigent_agent.Fail) as e:
        lead(og)
    assert "far-name" in str(e.value)
    assert REPO in str(e.value)
    assert og.bodies == []


class Stop(Exception):
    pass


@pytest.mark.parametrize(
    ("agent", "host", "top_level", "role", "reads"),
    [
        ("claude", OTHER, True, None, 1),
        ("claude", OTHER, False, "/x/worker.md", 1),
        ("claude", OTHER, False, "/x/lead.md", 0),  # a role that keeps every tool
        ("claude", OTHER, False, None, 0),
        ("claude", "h", True, None, 0),  # this machine, named
        ("codex", OTHER, True, None, 0),
    ],
)
def test_a_start_reads_the_remote_loop_md_once_and_only_when_a_claude_session_needs_it(
    agent: str,
    host: str,
    top_level: bool,
    role: str | None,
    reads: int,
) -> None:
    og = Server({**CHECKOUT, LOOP: "Lead autocompact: 150k\n"})

    def create(*args: object, **kwargs: object) -> str:
        raise Stop

    og.create = create  # type: ignore[method-assign]
    with pytest.raises(Stop):
        omnigent_agent.start(
            og, agent, "t", "do X", Path("r.md"), role, 0,
            top_level=top_level, workspace=REPO, host_id=host, subscribe=False,
        )  # fmt: skip
    assert [c for c in og.calls if c == ("READ", LOOP)] == [("READ", LOOP)] * reads


@pytest.mark.parametrize("why", ["live", "unreadable", "listing", "404 on a listed file"])
def test_an_unreadable_remote_loop_md_fails_the_start_naming_host_and_path_with_no_create(
    why: str,
) -> None:
    text = (
        None if why == "404 on a listed file" else "Lead autocompact: 150k\n"
    )  # an older host's 404
    og = Server({**CHECKOUT, LOOP: text}, live=why != "live")
    if why == "unreadable":
        og.read_file = lambda sid, path: (_ for _ in ()).throw(omnigent_agent.Fail("HTTP 502"))  # type: ignore[method-assign]
    if why == "listing":
        og.broken = f"{REPO}/docs/agents"
    with pytest.raises(omnigent_agent.Fail) as e:
        lead(og)
    assert "far-name" in str(e.value)
    assert REPO in str(e.value)
    assert ("POST", "/v1/sessions") not in og.calls
    assert og.bodies == []


def test_a_local_workspace_reads_loop_md_from_disk_as_before(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "docs" / "agents").mkdir(parents=True)
    (repo / "docs" / "agents" / "loop.md").write_text("Lead autocompact: 200k\n")
    og = Server({})
    for host in (None, "h"):  # no --host, and this machine named
        og.create("claude", "t", top_level=True, workspace=str(repo), host_id=host)
        assert og.bodies[-1]["terminal_launch_args"] == ["--autocompact", "200k"]
    assert og.calls == [("POST", "/v1/sessions")] * 2  # no host listing, no session read
