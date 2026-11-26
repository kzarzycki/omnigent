"""scripts/omnigent_agent.py with the Omnigent server faked."""

import errno
import importlib.util
import inspect
import io
import json
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from urllib.error import HTTPError, URLError
from urllib.parse import unquote
from urllib.request import Request

import pytest

spec = importlib.util.spec_from_file_location(
    "omnigent_agent",
    Path(__file__).resolve().parents[1]
    / "skills"
    / "omnigent-agents"
    / "scripts"
    / "omnigent_agent.py",
)
assert spec and spec.loader
omnigent_agent = importlib.util.module_from_spec(spec)
sys.modules["omnigent_agent"] = omnigent_agent
spec.loader.exec_module(omnigent_agent)


# tests replace the class; a script without the lookup passes every id through
REAL_SESSION_ID = getattr(omnigent_agent.Omnigent, "session_id", lambda _, sid: sid)
REAL_USERS = getattr(omnigent_agent.Omnigent, "users", lambda *_: [])
SESSIONS_EVERY = "/v1/sessions?kind=any&include_archived=true&include_side_chats=true&limit=1000"
PINNED = '{"remote": {"defaultEnvironmentId": "env_1"}}'
SESSIONS_LIVE = "/v1/sessions?kind=any&limit=1000"


class FakeOmnigent:
    """Sessions whose sends land only when `deaf` is not set for that session number."""

    def __init__(
        self, deaf: set[int] = frozenset(), reply: str = "", status: str = "idle", hold: int = 0
    ) -> None:
        self.deaf, self.reply, self.final = deaf, reply, status
        self.hold = hold  # polls of info() a send stays pending: a turn busy in a long tool call
        self.held: list[tuple[str, str, int]] = []
        self.posts = 0
        self.sessions: dict[str, list[dict]] = {}
        self.archived: list[str] = []
        self.kids: list[str] = []  # sub-agents children() lists
        self.tasks: list[dict] = []  # background tasks info() reports
        self.interrupted: list[str] = []
        self.labels: dict[str, dict[str, str]] = {}  # each session's labels
        self.prompts: list[dict] = []
        self.fail: set[str] = set()  # method names that raise Fail
        self.titles: dict[str, str] = {}
        self.created: dict[str, float] = {}  # when each session was made, as child_sessions says
        self.top_level: list[bool] = []  # per create, and per lookup after a failed one
        self.roles: list[str | None] = []  # per create
        self.prs: list[str] = []
        self.workspaces: list[str | None] = []
        self.hosts_of: dict[str, str] = {}  # each created session's host
        self.others: list[dict] = []  # sessions this script did not create, as the list shows them
        self.taken: set[str] = set()  # titles an earlier child holds: create answers 409
        self.subscriptions: list[tuple[str, str]] = []
        self.parents: dict[str, str] = {}  # a sub-agent's parent_session_id, as the server says
        self.roots: dict[str, str] = {}  # a session's root_conversation_id, when not its own
        self.resolved: list[tuple[str, str, str, dict | None]] = []
        self.host_id = "h"  # this host, named "host"
        self.host_names: dict[str, str] = {}  # other hosts' names, by id
        self.host_rows: list[dict] = []  # GET /v1/hosts rows, when a test sets them
        self.procs: dict[str, dict[str, str]] = {}  # each host's /proc files, by host id
        self.harnesses: dict[str, str] = {}  # a registered agent's harness, by its name
        self.listed = [
            "opus",
            "claude-opus-5-5",
            "claude-sonnet-5-5",
            "gpt-6.1-sol",
            "gpt-6.1-terra",
        ]
        self.overrides: dict[str, str | None] = {}  # the model_override each session took
        self.efforts: dict[str, str | None] = {}  # the reasoning_effort each session took
        self.modes: dict[str, str | None] = {}  # the permission_mode each session was created with
        # the levels a Codex model lists, by model (None: the default one); unlisted: none named
        self.levels: dict[str | None, list[str]] = {}

    def session_id(self, sid: str) -> str:  # the real lookup, over these sessions
        return REAL_SESSION_ID(self, sid)

    def users(self, workspace: str, host_id: str) -> list[dict]:  # the real filter, over these
        return REAL_USERS(self, workspace, host_id)

    def call(self, method: str, path: str, body: object = None) -> dict:
        if path == "/v1/hosts":  # hosts as an older server lists them: no harnesses, no memory
            rows = [{"host_id": i, "name": n, "status": "online"} for i, n in self.hosts().items()]
            return {"hosts": self.host_rows or rows}
        if path.startswith("/v1/hosts/") and "/files/" in path:  # no file route on this server
            raise omnigent_agent.Fail("GET file") from HTTPError(path, 404, "", Message(), None)
        if path.startswith("/v1/hosts/"):  # a host's folder listing: the names under it in procs
            host, folder = path.split("?")[0].removeprefix("/v1/hosts/").split("/filesystem/")
            under = "/" + unquote(folder).strip("/") + "/"
            names = {
                k[len(under) :].split("/")[0]
                for k in self.procs.get(host, {})
                if k.startswith(under)
            }
            if not names:
                raise omnigent_agent.Fail("GET listing") from HTTPError(
                    path, 404, "", Message(), None
                )
            return {"data": [{"name": n} for n in sorted(names)]}
        if (method, path) == ("GET", SESSIONS_LIVE):
            made = [
                {
                    "id": s,
                    "title": self.titles.get(s),
                    "host_id": self.hosts_of.get(s, self.host_id),
                    "workspace": self.workspaces[int(s[1:])],
                    "status": self.final,
                    "archived": s in self.archived,
                }
                for s in self.sessions
                if s[1:].isdigit() and int(s[1:]) < len(self.workspaces)
            ]
            return {"data": made + self.others}
        assert (method, path.split("&after=")[0]) == ("GET", SESSIONS_EVERY), path
        return {"data": [{"id": s, "title": self.titles.get(s)} for s in self.sessions]}

    def harness(self, agent: str) -> str:
        return self.harnesses.get(agent, "")

    def creations(self, sid: str) -> list[str]:
        return []

    def hosts(self) -> dict[str, str]:
        return {self.host_id: "host", **self.host_names}

    def read_file(self, sid: str, path: str) -> str:  # a host's /proc, by host, else none
        proc = self.procs.get(self.hosts_of.get(sid) or self.host_of(sid), {})
        if proc.get(path) is None:  # None: listed, but an older host answers 404 to the read
            raise omnigent_agent.Gone(f"{path}: not found")
        return proc[path]

    def loop_text(self, workspace: str, host_id: str | None) -> str:  # no loop.md on another host
        return ""

    def host_of(self, sid: str) -> str:
        return next((str(o.get("host_id")) for o in self.others if o.get("id") == sid), "")

    def models(self, agent: str, host_id: str | None = None) -> list[str]:
        if "models" in self.fail:
            raise omnigent_agent.Fail("GET model-options: HTTP 502 Bad Gateway")
        # an ACP agent's list comes from its own session; claude-cloud runs acp even unregistered
        if agent == "claude-cloud" or self.harness(agent).startswith("acp:"):
            raise omnigent_agent.Fail("HTTP 502: model options are unsupported for harness 'acp'")
        return self.listed

    def effort_levels(
        self, agent: str, model: str | None = None, host_id: str | None = None
    ) -> list[str]:
        if "models" in self.fail:
            raise omnigent_agent.Fail("GET model-options: HTTP 502 Bad Gateway")
        return self.levels.get(model, [])

    def create(
        self,
        agent: str,
        title: str,
        model: str | None = None,
        top_level: bool = False,
        workspace: str | None = None,
        permission_mode: str | None = None,
        host_id: str | None = None,
        role: str | None = None,
        effort: str | None = None,
        pr: str = "",
    ) -> str:
        if title in self.taken:
            error = HTTPError("http://x/v1/sessions", 409, "Conflict", Message(), None)
            raise omnigent_agent.Fail("POST /v1/sessions: HTTP 409 Conflict") from error
        sid = f"s{len(self.sessions)}"
        self.overrides[sid] = omnigent_agent.model_for(agent, model)[0]  # as the real body
        self.top_level.append(top_level)
        self.roles.append(role)
        self.prs.append(pr)
        self.efforts[sid] = effort
        self.modes[sid] = permission_mode
        self.workspaces.append(workspace)
        self.hosts_of[sid] = host_id or self.host_id
        self.sessions[sid] = [{"type": "resource_event"}]
        self.titles[sid] = title
        self.created[sid] = time.time()
        if "create" in self.fail:  # the server made it; the answer timed out
            raise omnigent_agent.Fail("POST /v1/sessions: TimeoutError: timed out")
        return sid

    def siblings(self, title: str) -> list[dict]:
        if not omnigent_agent.parent_id():
            return []
        return [
            {"id": sid, "title": t, "created_at": self.created.get(sid, 0)}
            for sid, t in self.titles.items()
            if sid not in self.archived and (t == title or t.startswith(f"{title}-r"))
        ]

    def subscribe(self, subscriber: str, sid: str) -> None:
        if "subscribe" in self.fail:
            raise omnigent_agent.Fail("POST subscriptions: HTTP 404 Not Found")
        self.subscriptions.append((subscriber, sid))

    def find(self, title: str, after: float, top_level: bool = False) -> str | None:
        self.top_level.append(top_level)
        return next((s for s, t in self.titles.items() if t == title), None)

    def send(self, sid: str, text: str) -> None:
        if "send" in self.fail:
            raise omnigent_agent.Fail("POST events: HTTP 503")
        self.posts += 1
        if sid[1:].isdigit() and int(sid[1:]) in self.deaf:
            return
        if self.hold:
            self.held.append((sid, text, self.hold))
            return
        self.land(sid, text)

    def land(self, sid: str, text: str) -> None:
        self.sessions[sid].append(
            {
                "id": f"i{len(self.sessions[sid])}",
                "type": "message",
                "role": "user",
                "created_at": time.time(),
                "content": [{"type": "input_text", "text": text}],
            }
        )
        if self.reply:
            self.sessions[sid].append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": self.reply}],
                }
            )

    def items(self, sid: str) -> list[dict]:
        return self.sessions[sid]

    def first_items(self, sid: str, n: int = 20) -> list[dict]:  # oldest first, as the API's asc
        return self.items(sid)[:n]

    def older(self, sid: str, after: str = "") -> dict:  # a page of 100, newest first
        newest = list(reversed(self.sessions[sid]))
        ids = [str(i.get("id")) for i in newest]
        start = ids.index(after) + 1 if after else 0
        data = newest[start : start + 100]
        more = start + 100 < len(newest)
        return {"data": data, "last_id": data[-1].get("id") if data else None, "has_more": more}

    def info(self, sid: str) -> dict:
        for held in list(self.held):
            self.held.remove(held)
            if held[2] > 1:
                self.held.append((held[0], held[1], held[2] - 1))
            else:
                self.land(held[0], held[1])
        pending = [
            {"content": [{"type": "input_text", "text": t}]} for s, t, _ in self.held if s == sid
        ]
        status = "running" if pending else self.final
        info = {"status": status, "pending_elicitations": self.prompts, "pending_inputs": pending}
        if self.tasks:
            info["background_tasks"] = self.tasks
        info["model_override"] = self.overrides.get(sid)
        info["reasoning_effort"] = self.efforts.get(sid)
        if sid in self.labels:
            info["labels"] = self.labels[sid]
        if sid in self.roots:
            info["root_conversation_id"] = self.roots[sid]
        if sid in self.parents:
            info["parent_session_id"] = self.parents[sid]
        if sid in self.archived:
            info["archived"] = True
        if sid[1:].isdigit() and int(sid[1:]) < len(self.workspaces):
            info["workspace"] = self.workspaces[int(sid[1:])]
        return info

    def resolve(self, sid: str, eid: str, action: str, content: dict | None = None) -> None:
        if eid in self.fail:
            raise omnigent_agent.Fail(f"POST resolve {eid}: HTTP 404 Not Found")
        self.resolved.append((sid, eid, action, content))
        self.prompts = [e for e in self.prompts if e["elicitation_id"] != eid]

    def interrupt(self, sid: str) -> None:
        self.interrupted.append(sid)

    def children(self, sid: str) -> list[str]:
        return [] if sid in self.kids else list(self.kids)  # a sub-agent has none of its own

    def label(self, sid: str, labels: dict[str, str]) -> None:
        self.labels.setdefault(sid, {}).update(labels)

    def archive(self, sid: str) -> None:
        if "archive" in self.fail:
            raise omnigent_agent.Fail(f"PATCH {sid}: HTTP 503")
        self.archived.append(sid)

    def unarchive(self, sid: str) -> None:
        self.archived.remove(sid)


REAL_TRIAGE = omnigent_agent.triage


REAL_TRUSTED = omnigent_agent.folder_trusted


@pytest.fixture(autouse=True)
def trusted_folders(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every folder trusted, so a cloud start never reads the host's Claude config (#980)."""
    monkeypatch.setattr(omnigent_agent, "folder_trusted", lambda *_a, **_k: True)


@pytest.fixture(autouse=True)
def this_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """The checkout's GitHub repository, so a `--pr` start labels its worker without `gh`."""
    monkeypatch.setattr(omnigent_agent, "repo_slug", lambda cwd=None: "o/r")


@pytest.fixture(autouse=True)
def no_model(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The small model faked: never a real call. Each event it is asked about is recorded, and
    it answers ``none`` (mid-work)."""
    asked: list[str] = []
    monkeypatch.setattr(
        omnigent_agent, "triage", lambda event, decisions: asked.append(event) or ("none", "fake")
    )
    return asked


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(omnigent_agent, "POLL_SECONDS", 0)
    monkeypatch.setattr(omnigent_agent, "LATE_BRIEF_SECONDS", 0)  # its tests set their own
    monkeypatch.setattr(omnigent_agent, "TURN_END_SECONDS", 0)
    monkeypatch.setattr(omnigent_agent, "COLD_START_SECONDS", 0)  # else start polls 20 s
    monkeypatch.delenv("OMNIGENT_SUPERVISE_REGISTRY", raising=False)  # else run arms a real one


def test_start_sends_preamble_and_confirms(tmp_path: Path) -> None:
    og = FakeOmnigent()
    sid, _ = omnigent_agent.start(
        og, "claude", "t", "do X", tmp_path / "r.md", "roles/worker.md", 0
    )
    message = omnigent_agent.text_of(og.sessions[sid][1])
    brief_file = tmp_path / "r.md.brief"
    assert message.startswith("Work on t:") and str(brief_file) in message and "do X" not in message
    text = brief_file.read_text()
    assert sid == "s0" and text.endswith("do X")
    assert (
        "owner authorized" in text
        and LONG_RUN_POINTER in text
        and "setsid" not in text
        and str(tmp_path / "r.md") in text
        and "roles/worker.md" in text
    )


@pytest.mark.parametrize(
    ("agent", "model", "env", "expected"),
    [
        ("claude", "claude-sonnet-5-5", None, "cc/sonnet-5.5/?"),
        ("codex", "gpt-6.1-terra", None, "codex/terra-6.1/?"),
        ("codex", None, "gpt-6.1-sol", "codex/sol-6.1/?"),
        ("claude", None, None, "cc/?/?"),
    ],
)
def test_start_gives_the_child_its_signature_from_what_it_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: str,
    model: str | None,
    env: str | None,
    expected: str,
) -> None:
    monkeypatch.delenv(f"OMNIGENT_MODEL_{agent.upper()}", raising=False)
    if env:
        monkeypatch.setenv(f"OMNIGENT_MODEL_{agent.upper()}", env)
    og = FakeOmnigent()
    omnigent_agent.start(og, agent, "t", "do X", tmp_path / "r.md", None, 0, model=model)
    assert f"AGENT_SIGNATURE={expected}\n" in (tmp_path / "r.md.brief").read_text()


@pytest.mark.parametrize(
    ("model", "env", "named"),
    [
        ("gpt-5.5-terra", None, "--model gpt-5.5-terra"),
        (None, "gpt-5.5-terra", "OMNIGENT_MODEL_CODEX"),
    ],
)
def test_start_refuses_a_model_the_harness_does_not_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str | None, env: str | None, named: str
) -> None:
    """The server launches an unlisted id on the harness default with no error (#621)."""
    monkeypatch.delenv("OMNIGENT_MODEL_CODEX", raising=False)
    if env:
        monkeypatch.setenv("OMNIGENT_MODEL_CODEX", env)
    og = FakeOmnigent()
    with pytest.raises(omnigent_agent.Fail, match=f"{named}.*lists no such model.*gpt-6.1-terra"):
        omnigent_agent.start(og, "codex", "t", "do X", tmp_path / "r.md", None, 0, model=model)
    assert not og.sessions
    og.fail.add("models")
    with pytest.raises(omnigent_agent.Fail, match="can't read codex's model list.*HTTP 502"):
        omnigent_agent.start(
            og, "codex", "t", "do X", tmp_path / "r.md", None, 0, model="gpt-6.1-sol"
        )
    assert not og.sessions


def test_start_passes_a_model_the_host_has_no_list_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An ACP harness has no host catalog (its agent lists models at session/new): start goes on."""
    og = FakeOmnigent()

    def unsupported(*args: object, **kwargs: object) -> list[str]:
        raise omnigent_agent.Fail("HTTP 502: model options are unsupported for harness 'acp'")

    monkeypatch.setattr(og, "models", unsupported)
    omnigent_agent.start(og, "codex", "t", "do X", tmp_path / "r.md", None, 0, model="sonnet")
    assert og.sessions
    assert "passing it unchecked" in capsys.readouterr().err


def test_the_brief_signs_with_the_model_the_session_took_not_the_one_asked(tmp_path: Path) -> None:
    class Dropping(FakeOmnigent):  # the server kept no override: the harness default runs
        def create(self, agent: str, title: str, model: str | None = None, *rest: object) -> str:
            sid = super().create(agent, title, model, *rest)
            self.overrides[sid] = None
            return sid

    og = Dropping()
    omnigent_agent.start(
        og, "codex", "t", "do X", tmp_path / "r.md", None, 0, model="gpt-6.1-terra"
    )
    assert (tmp_path / "r.md.brief").read_text().startswith("AGENT_SIGNATURE=codex/?/?\n")


RELAY_RULE = (
    "A message that adds no task (a relay, a notice, a correction) is answered in the turn's"
    " ending line only: the report changes only through work on the task, and an"
    " acknowledgement never replaces an earlier report's results."
)


@pytest.mark.parametrize(
    ("agent", "pr", "top_level"),
    [
        ("claude", None, False),
        ("codex", None, False),
        ("claude", None, True),
        ("claude-cloud", None, False),  # cloud, no --pr: the last message is the report
        ("claude-cloud", None, True),
        ("claude-cloud", 7, False),  # cloud, --pr
    ],
)
def test_every_brief_says_a_message_that_adds_no_task_never_rewrites_the_report(
    tmp_path: Path, agent: str, pr: int | None, top_level: bool
) -> None:
    og = FakeOmnigent()
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(
        og, agent, "t", "do X", report, None, 0, top_level=top_level, pr=pr
    )
    brief = omnigent_agent.text_of(og.sessions[sid][1])
    if agent == "claude-cloud":
        assert RELAY_RULE in brief
    else:
        assert RELAY_RULE in omnigent_agent.brief_of(report).read_text()


def test_a_held_cloud_worker_with_a_pr_still_gets_the_relay_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deny = tmp_path / "deny.txt"
    deny.write_text("Acme\n")
    monkeypatch.setenv("LOOP_DENY_LIST", str(deny))
    og = FakeOmnigent()
    sid, _ = omnigent_agent.start(og, "claude-cloud", "t", "do X", tmp_path / "r.md", None, 0, pr=7)
    brief = omnigent_agent.text_of(og.sessions[sid][1])
    assert "which you don't post" in brief and RELAY_RULE in brief


def test_a_running_session_signs_from_its_omnigent_record_first(tmp_path: Path) -> None:
    """A Codex child's rollout sits under a private CODEX_HOME, so the record is what holds."""
    rows = [
        {"id": "opus", "model": "claude-opus-5-5"},
        {"id": "gpt-6.1-sol", "model": "gpt-6.1-sol"},
    ]
    read = omnigent_agent.running_signature
    codex = {"harness": "codex-native", "llm_model": "gpt-6.1-sol", "reasoning_effort": "medium"}
    assert read({}, tmp_path, {**codex, "model_options": rows}) == "codex/sol-6.1/med"
    # before the harness reports, llm_model is the agent spec's, not a model: the harness files decide
    assert (
        read({}, tmp_path, {**codex, "llm_model": "codex-native-ui", "model_options": rows})
        == "?/?/?"
    )
    claude = {"harness": "claude-native", "llm_model": "claude-opus-5-5", "model_options": rows}
    assert (
        read({"CLAUDECODE": "1", "CLAUDE_EFFORT": "xhigh"}, tmp_path, claude) == "cc/opus-5.5/xhigh"
    )


def test_a_running_session_reads_its_signature_without_launch_arguments(tmp_path: Path) -> None:
    """A session started with no model or effort (Omnigent's own tools, a default model) still signs in full."""
    rollout = tmp_path / ".codex/sessions/2026/10/27/rollout-2026-10-27T11-53-35-T1.jsonl"
    rollout.parent.mkdir(parents=True)
    turns = [
        {"model": "gpt-6.1-terra", "effort": "high"},
        {"model": "gpt-6.1-sol", "effort": "medium"},
    ]
    rollout.write_text(
        '{"type":"session_meta","payload":{}}\nnot json\n'
        + "".join(json.dumps({"type": "turn_context", "payload": t}) + "\n" for t in turns)
    )
    transcript = tmp_path / ".claude/projects/-repo/S1.jsonl"
    transcript.parent.mkdir(parents=True)
    replies = [
        {"type": "assistant", "message": {"model": "claude-opus-5-5"}},
        {"type": "user", "message": {}},
    ]
    transcript.write_text("".join(json.dumps(r) + "\n" for r in replies))
    read = omnigent_agent.running_signature
    assert read({"CODEX_THREAD_ID": "T1"}, tmp_path) == "codex/sol-6.1/med"
    assert read(
        {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "S1", "CLAUDE_EFFORT": "xhigh"}, tmp_path
    ) == ("cc/opus-5.5/xhigh")
    assert read({"CLAUDECODE": "1"}, tmp_path) == "cc/?/?"
    assert read({"CODEX_THREAD_ID": "gone"}, tmp_path) == "codex/?/?"
    assert read({}, tmp_path) == "?/?/?"


def test_start_abandons_deaf_session_and_retries(tmp_path: Path) -> None:
    og = FakeOmnigent(deaf={0})
    assert omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0) == (
        "s1",
        [],
    )
    assert og.archived == ["s0"]


def test_an_interrupt_while_confirming_a_found_session_keeps_it_and_prints_its_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    og.fail.add("create")

    def interrupted(*_a: object) -> bool:
        raise omnigent_agent.Interrupted(15)

    monkeypatch.setattr(omnigent_agent, "delivered", interrupted)
    with pytest.raises(omnigent_agent.Interrupted):
        omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
    out = capsys.readouterr()
    assert og.archived == []
    assert out.out.strip() == "s0"
    assert "status s0" in out.err


class LateBrief(FakeOmnigent):
    """Session s0's brief is taken late: it lands as an item on the ``land_at``-th history read,
    and waits in ``pending_inputs`` until then when ``queued``."""

    def __init__(self, land_at: int | None, queued: bool, brief: bool = True) -> None:
        super().__init__(deaf={0})
        self.land_at, self.queued, self.reads, self.text = land_at, queued, 0, ""
        self.brief = brief  # False: only an agent item arrives, not the brief

    def send(self, sid: str, text: str) -> None:
        super().send(sid, text)
        if sid == "s0":
            self.text = text

    def items(self, sid: str) -> list[dict]:
        if sid == "s0" and self.text and self.land_at is not None:
            self.reads += 1
            if self.reads == self.land_at:
                if self.brief:
                    self.land(sid, self.text)
                else:
                    self.sessions[sid].append({"id": "t1", "type": "function_call"})
        return super().items(sid)

    def info(self, sid: str) -> dict:
        info = super().info(sid)
        if sid == "s0" and self.queued and self.text and self.land_at != 0:
            landed = any(i.get("role") == "user" for i in self.sessions[sid])
            info["pending_inputs"] = [] if landed else [{"content": [{"text": self.text}]}]
        return info


@pytest.fixture
def quick_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(omnigent_agent, "LATE_BRIEF_SECONDS", 0.5)
    monkeypatch.setattr(omnigent_agent, "POLL_SECONDS", 0.01)


@pytest.mark.parametrize(
    ("land_at", "queued"),
    [(3, True), (4, True), (2, False)],  # pending at the deadline then an item; an item at once
)
def test_start_keeps_a_session_whose_brief_lands_within_the_grace(
    tmp_path: Path, quick_grace: None, land_at: int, queued: bool
) -> None:
    og = LateBrief(land_at, queued)
    assert omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0) == (
        "s0",
        [],
    )
    assert og.archived == [] and list(og.sessions) == ["s0"]


def test_a_brief_landing_late_exits_0_through_main_with_one_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick_grace: None
) -> None:
    og = LateBrief(3, True)
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "brief.md").write_text("do X")
    argv = ["start", "claude", "t", "brief.md", "r.md", "--confirm-seconds", "0"]
    assert omnigent_agent.main(argv) == 0
    assert list(og.sessions) == ["s0"] and og.archived == []


def test_start_keeps_a_queued_session_that_shows_an_item_during_the_grace(
    tmp_path: Path, quick_grace: None
) -> None:
    og = LateBrief(3, True, brief=False)
    assert omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)[0] == "s0"
    assert og.archived == []


def test_a_brief_that_never_arrives_waits_the_grace_then_exits_1_through_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick_grace: None
) -> None:
    og = FakeOmnigent(deaf={0, 1})  # nothing pending, history empty
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "brief.md").write_text("do X")
    argv = ["start", "claude", "t", "brief.md", "r.md", "--confirm-seconds", "0"]
    began = time.monotonic()
    assert omnigent_agent.main(argv) == 1
    assert time.monotonic() - began >= 0.9  # the 0.5 s grace, once per attempt
    assert og.archived == ["s0", "s1"]


def test_a_brief_landing_in_the_grace_with_nothing_pending_at_the_deadline_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick_grace: None
) -> None:
    og = LateBrief(6, False)  # empty history and nothing pending until it lands
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "brief.md").write_text("do X")
    argv = ["start", "claude", "t", "brief.md", "r.md", "--confirm-seconds", "0"]
    assert omnigent_agent.main(argv) == 0
    assert list(og.sessions) == ["s0"] and og.archived == []


def test_a_host_over_capacity_past_the_wait_exits_1_through_main_and_makes_no_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    og = FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.setattr(omnigent_agent, "capacity", lambda **_: (False, "cpu 2% idle"))
    monkeypatch.setattr(omnigent_agent, "CAPACITY_WAIT_SECONDS", 0.05)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "brief.md").write_text("do X")
    argv = ["start", "claude", "t", "brief.md", "r.md", "--confirm-seconds", "0"]
    assert omnigent_agent.main(argv) == 1
    assert "over capacity for 0s: cpu 2% idle" in capsys.readouterr().err
    assert og.sessions == {}


def test_a_runner_launch_error_is_no_start_and_is_abandoned_after_the_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick_grace: None
) -> None:
    og = FakeOmnigent(deaf={0, 1})
    real_items = og.items
    monkeypatch.setattr(og, "items", lambda sid: [*real_items(sid), {"type": "error"}])
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "brief.md").write_text("do X")
    argv = ["start", "claude", "t", "brief.md", "r.md", "--confirm-seconds", "0"]
    assert omnigent_agent.main(argv) == 1
    assert og.archived == ["s0", "s1"]


def test_start_abandons_a_brief_that_stops_pending_without_landing_after_a_whole_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick_grace: None
) -> None:
    og = LateBrief(None, True)  # pending, then gone without ever being an item
    real_info, calls = og.info, [0]

    def info(sid: str) -> dict:
        calls[0] += 1
        out = real_info(sid)
        if sid == "s0" and calls[0] > 80:  # past one grace (0.5 s / 0.01 s): the grace restarted
            out["pending_inputs"] = []
        return out

    monkeypatch.setattr(og, "info", info)
    began = time.monotonic()
    assert omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)[0] == "s1"
    assert og.archived == ["s0"]
    assert time.monotonic() - began >= 0.9  # the pending time plus a whole grace after it


def test_a_brief_pending_past_the_grace_then_landing_is_kept_through_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick_grace: None
) -> None:
    og = LateBrief(200, True)  # pending ~2 s, four graces, then an item
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "brief.md").write_text("do X")
    argv = ["start", "claude", "t", "brief.md", "r.md", "--confirm-seconds", "0"]
    assert omnigent_agent.main(argv) == 0
    assert list(og.sessions) == ["s0"] and og.archived == []


@pytest.mark.parametrize(("flags", "seconds"), [([], 300.0), (["--confirm-seconds", "7"], 7.0)])
def test_start_confirms_for_300_seconds_unless_the_flag_overrides_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flags: list[str], seconds: float
) -> None:
    og = FakeOmnigent()
    waits: list[float] = []

    def delivered(_og: object, _sid: str, _text: str, secs: float, *_a: object) -> bool:
        waits.append(secs)
        return True

    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.setattr(omnigent_agent, "delivered", delivered)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "brief.md").write_text("do X")
    assert omnigent_agent.main(["start", "claude", "t", "brief.md", "r.md", *flags]) == 0
    assert waits[-1] == seconds


def test_a_failed_start_with_no_attempt_started_exits_non_zero_through_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(deaf={0, 1})
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "brief.md").write_text("do X")
    argv = ["start", "claude", "t", "brief.md", "r.md", "--confirm-seconds", "0"]
    assert omnigent_agent.main(argv) == 1
    assert og.archived == ["s0", "s1"]


def test_start_fails_after_two_deaf_sessions(tmp_path: Path) -> None:
    with pytest.raises(
        omnigent_agent.Fail, match="s0: brief not in history.*s1: brief not in history"
    ):
        omnigent_agent.start(
            FakeOmnigent(deaf={0, 1}), "claude", "t", "do X", tmp_path / "r.md", None, 0
        )


def test_wait_returns_report(tmp_path: Path) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "t")
    (tmp_path / "r.md").write_text("ok")
    assert omnigent_agent.wait(og, sid, tmp_path / "r.md") == str(tmp_path / "r.md")


def git_checkout(root: Path) -> Path:
    """A checkout with one committed file, ``a.txt``."""
    root.mkdir(parents=True, exist_ok=True)
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(root), *args], check=True)
    (root / "a.txt").write_text("a\n")
    subprocess.run(["git", "-C", str(root), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "a"], check=True)
    return root


def test_a_host_child_that_reports_with_a_tracked_change_fails_the_wait_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = git_checkout(tmp_path / "w")
    og = FakeOmnigent()
    sid = og.create("claude", "t", workspace=str(repo))
    report = repo / "tmp" / "r.md"
    report.parent.mkdir()
    report.write_text("ok")
    (repo / "repro.py").write_text("untracked: a verifier's repro file")
    assert omnigent_agent.wait(og, sid, report) == str(report)  # clean: unchanged

    (repo / "a.txt").write_text("mutated\n")
    with pytest.raises(omnigent_agent.Fail, match=r"changed while it ran: a\.txt \(its own"):
        omnigent_agent.wait(og, sid, report)
    # a change already there when run started the child is not its own
    start = omnigent_agent.tracked_changes(repo)
    assert omnigent_agent.wait(og, sid, report, baseline=start) == str(report)
    # but the child editing that file further is
    (repo / "a.txt").write_text("mutated again\n")
    with pytest.raises(omnigent_agent.Fail, match=r"changed while it ran: a\.txt"):
        omnigent_agent.wait(og, sid, report, baseline=start)
    # a name git would quote (a space, a quote, a non-ASCII letter) keeps its hash too
    for name in ("b c.txt", 'q"d.txt', "é.txt"):
        (repo / name).write_text("x\n")
        subprocess.run(["git", "-C", str(repo), "add", name], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", name], check=True)
        (repo / name).write_text("first\n")
        start = omnigent_agent.tracked_changes(repo)
        (repo / name).write_text("second\n")
        with pytest.raises(omnigent_agent.Fail, match=f"changed while it ran: .*{re.escape(name)}"):
            omnigent_agent.wait(og, sid, report, baseline=start)
    # a cloud child's tree is not this one
    monkeypatch.setattr(omnigent_agent, "pr_report", lambda said, pr, since=None: "from the PR")
    og.reply = "https://github.com/o/r/pull/7#issuecomment-1"
    og.land(sid, "go")
    assert omnigent_agent.wait(og, sid, tmp_path / "pr.md", pr=7) == str(tmp_path / "pr.md")


def test_run_fails_only_on_a_tracked_change_the_child_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = git_checkout(tmp_path / "w")
    (repo / "a.txt").write_text("the lead's own edit\n")
    (repo / "b.md").write_text("do X")
    og = FakeOmnigent(reply="done")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(repo)
    info = og.info
    og.info = lambda s: ((repo / "r.md").write_text("ok"), info(s))[1]  # type: ignore[method-assign]
    argv = ["run", "claude", "t", "b.md", "r.md", "--confirm-seconds", "0"]
    assert omnigent_agent.main([*argv, "--keep"]) == 0
    # a follow-up, too, holds only the child to what changed since it was sent
    (repo / "f.md").write_text("fix Y")
    sid = next(iter(og.sessions))
    assert omnigent_agent.main(["send", sid, "f.md", "r.md", "--confirm-seconds", "0"]) == 0

    (repo / "c.txt").write_text("c\n")
    subprocess.run(["git", "-C", str(repo), "add", "c.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "c"], check=True)
    og.info = lambda s: (  # type: ignore[method-assign]
        (repo / "r.md").write_text("ok"),
        (repo / "c.txt").write_text("mutated\n"),
        info(s),
    )[2]
    assert omnigent_agent.main(argv) == 1


def notify_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``$LOOP_NOTIFY`` that appends one line of its arguments per call to the returned file."""
    calls = tmp_path / "calls"
    tell = tmp_path / "tell.sh"
    tell.write_text(f'#!/bin/sh\necho "$@" >> {calls}\n')
    tell.chmod(0o755)
    monkeypatch.setenv("LOOP_NOTIFY", str(tell))
    return calls


def notify_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, og: FakeOmnigent
) -> tuple[str, Path]:
    repo = git_checkout(tmp_path / "w")
    (repo / "f.md").write_text("fix Y")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(repo)
    return og.create("claude", "t"), repo


def test_send_notify_tells_loop_notify_once_the_exit_and_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = notify_stub(tmp_path, monkeypatch)
    og = FakeOmnigent(reply="done")
    sid, repo = notify_session(tmp_path, monkeypatch, og)
    info = og.info
    og.info = lambda s: ((repo / "r.md").write_text("ok"), info(s))[1]  # type: ignore[method-assign]
    argv = ["send", sid, "f.md", "r.md", "--confirm-seconds", "0"]
    assert omnigent_agent.main([*argv, "--notify"]) == 0
    assert calls.read_text().splitlines() == [f"exit 0: {repo / 'r.md'}"]


def test_send_notify_names_the_exit_and_the_first_line_of_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = notify_stub(tmp_path, monkeypatch)
    og = FakeOmnigent(deaf={0})
    sid, _ = notify_session(tmp_path, monkeypatch, og)
    argv = ["send", sid, "f.md", "r.md", "--confirm-seconds", "0"]
    code = omnigent_agent.main([*argv, "--notify"])
    assert code != 0
    (line,) = calls.read_text().splitlines()
    assert line.startswith(f"exit {code}: ") and "neither pending nor in history" in line


def test_wait_notify_tells_loop_notify_once_the_report_is_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = notify_stub(tmp_path, monkeypatch)
    og = FakeOmnigent()
    sid, repo = notify_session(tmp_path, monkeypatch, og)
    (repo / "r.md").write_text("ok")
    assert omnigent_agent.main(["wait", sid, "r.md", "--notify"]) == 0
    assert calls.read_text().splitlines() == [f"exit 0: {repo / 'r.md'}"]


def test_wait_takes_no_report_older_than_the_sessions_last_message_until_it_is_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent()
    sid, repo = notify_session(tmp_path, monkeypatch, og)
    report = repo / "r.md"
    report.write_text("old run")
    past = time.time() - 600
    os.utime(report, (past, past))
    og.land(sid, "second brief")  # a later message the old report does not answer
    og.sessions[sid][-1]["created_at"] = time.time() - 300
    assert (
        omnigent_agent.main(["wait", sid, "r.md", "--timeout", "1"]) == 1
    )  # the old one is not taken
    info = og.info
    og.info = lambda s: (report.write_text("new run"), info(s))[1]  # type: ignore[method-assign]
    assert omnigent_agent.main(["wait", sid, "r.md"]) == 0
    assert report.read_text() == "new run"


def test_wait_takes_a_report_written_after_the_sessions_last_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent()
    sid, repo = notify_session(tmp_path, monkeypatch, og)
    og.sessions[sid][-1]["created_at"] = time.time() - 300
    (repo / "r.md").write_text("ok")
    assert omnigent_agent.main(["wait", sid, "r.md"]) == 0


def stale_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, og: FakeOmnigent
) -> tuple[str, Path]:
    """A session and its report file, written ten minutes ago."""
    sid, repo = notify_session(tmp_path, monkeypatch, og)
    report = repo / "r.md"
    report.write_text("old run")
    past = time.time() - 600
    os.utime(report, (past, past))
    return sid, report


def say(og: FakeOmnigent, sid: str, role: str, text: str, ago: float, **more: object) -> None:
    kind = "output_text" if role == "assistant" else "input_text"
    og.sessions[sid].append(
        {
            "id": f"m{len(og.sessions[sid])}",
            "type": "message",
            "role": role,
            "created_at": time.time() - ago,
            "content": [{"type": kind, "text": text}],
            **more,
        }
    )


def test_wait_floors_at_the_newest_of_two_messages_not_the_oldest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent()
    sid, _ = stale_report(tmp_path, monkeypatch, og)
    og.sessions[sid][:] = []
    say(og, sid, "user", "brief", 900)  # before the report
    say(og, sid, "user", "follow-up", 300)  # after it
    assert omnigent_agent.main(["wait", sid, "r.md", "--timeout", "1"]) == 1


def test_wait_does_not_floor_at_the_supervisors_nudge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent()
    sid, report = stale_report(tmp_path, monkeypatch, og)
    og.sessions[sid][:] = []
    say(og, sid, "user", "brief", 900)
    nudge = omnigent_agent.NUDGE.format(report=report.resolve())
    say(og, sid, "user", nudge, 10)  # after the report: it does not make it stale
    assert omnigent_agent.main(["wait", sid, "r.md"]) == 0


def test_wait_floors_at_the_sessions_creation_when_no_message_is_in_the_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent()
    sid, _ = stale_report(tmp_path, monkeypatch, og)
    og.sessions[sid][:] = []
    info = og.info
    og.info = lambda s: {**info(s), "created_at": time.time() - 300}  # type: ignore[method-assign]
    assert omnigent_agent.main(["wait", sid, "r.md", "--timeout", "1"]) == 1


def test_wait_finds_a_message_behind_the_newest_page_of_the_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent()
    sid, _ = stale_report(tmp_path, monkeypatch, og)
    og.sessions[sid][:] = []
    say(og, sid, "user", "second brief", 300)
    for n in range(100):  # the turn's steps since: the real items() page holds only these
        say(og, sid, "assistant", f"step {n}", 200)
    og.items = lambda s: og.sessions[s][-100:]  # type: ignore[method-assign]
    assert omnigent_agent.main(["wait", sid, "r.md", "--timeout", "1"]) == 1


def test_wait_takes_a_far_report_only_if_written_after_the_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = far_verifier(tmp_path, monkeypatch, "Verifier hosts: box, mac\n")
    assert omnigent_agent.main([*VERIFY_FAR, "--file", "gate.log"]) == 0
    path = "/there/repo/tmp/loop/pr7-v1.md"
    og.files[path] = "VERDICT: 0 blocker\nSATISFIED: yes\n"
    og.times[path] = time.time() - 600  # an earlier pass's, before this brief
    argv = ["wait", "s0", path, "--verdict", "--timeout", "1"]
    assert omnigent_agent.main(argv) == 1
    og.sessions["s0"][-1]["created_at"] = time.time() - 300
    og.times[path] = time.time() - 60  # the verifier wrote it after its brief
    assert omnigent_agent.main(argv) == 0


def test_run_notify_tells_loop_notify_once_the_report_is_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = notify_stub(tmp_path, monkeypatch)
    repo = git_checkout(tmp_path / "w")
    (repo / "b.md").write_text("do X")
    og = FakeOmnigent(reply="done")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(repo)
    info = og.info
    og.info = lambda s: ((repo / "r.md").write_text("ok"), info(s))[1]  # type: ignore[method-assign]
    argv = ["run", "claude", "t", "b.md", "r.md", "--confirm-seconds", "0", "--notify"]
    assert omnigent_agent.main(argv) == 0
    assert calls.read_text().splitlines() == [f"exit 0: {repo / 'r.md'}"]


REAL_CREATE = omnigent_agent.Omnigent.create


class Posting(FakeOmnigent):
    """A fake whose sessions come from the real ``create``: its POST body says who the parent is, and
    the server's notices follow from that body, as Omnigent's do."""

    def __init__(self, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self.bodies: list[dict] = []
        self.making: tuple[tuple, dict] = ((), {})

    def agent_id(self, agent: str) -> str:
        return agent

    def home(self, name: str) -> None:
        return None

    def create(self, *args: object, **kw: object) -> str:
        self.making = (args, kw)
        return REAL_CREATE(self, *args, **kw)  # type: ignore[arg-type]

    def call(self, method: str, path: str, body: object = None) -> dict:
        if (method, path) == ("POST", "/v1/sessions"):
            assert isinstance(body, dict)
            self.bodies.append(body)
            sid = FakeOmnigent.create(self, *self.making[0], **self.making[1])  # type: ignore[arg-type]
            if parent := body.get("parent_session_id"):
                self.parents[sid] = str(parent)
            return {"id": sid}
        if method == "GET" and path.endswith("/subscriptions"):
            who = path.split("/")[3]
            return {"data": [{"session_id": t} for w, t in self.subscriptions if w == who]}
        return super().call(method, path, body)

    def wakes(self, calls: Path) -> int:
        """How often the lead is told a child ended: Omnigent's notice to a sub-agent's parent, a
        subscription's, and the wait's own ``$LOOP_NOTIFY`` line."""
        told = len(calls.read_text().splitlines()) if calls.exists() else 0
        return len(self.parents) + len(self.subscriptions) + told


def lead_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, og: Posting, *argv: str, writes: bool = True
) -> tuple[int, Path]:
    calls = notify_stub(tmp_path, monkeypatch)
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "lead")
    monkeypatch.delenv("OMNIGENT_SESSION_ID", raising=False)
    monkeypatch.delenv("OMNIGENT_TERMINAL_LAUNCH_SESSION_ID", raising=False)
    repo = git_checkout(tmp_path / "w")
    (repo / "b.md").write_text("do X")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(repo)
    info = og.info
    if writes:  # the child's report lands while it is polled
        og.info = lambda s: ((repo / "r.md").write_text("ok"), info(s))[1]  # type: ignore[method-assign]
    return omnigent_agent.main([*argv]), calls


def test_a_sub_agent_under_a_background_run_wakes_its_lead_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = Posting(reply="done")
    argv = ["run", "claude", "t", "b.md", "r.md", "--confirm-seconds", "0", "--notify"]
    code, calls = lead_run(tmp_path, monkeypatch, og, *argv)
    assert code == 0
    assert [b.get("parent_session_id") for b in og.bodies] == ["lead"]  # Omnigent's own notice
    assert og.wakes(calls) == 1 and not calls.exists()
    assert "not telling $LOOP_NOTIFY" in capsys.readouterr().err


def test_a_subscribed_top_level_child_under_a_background_wait_wakes_its_lead_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = Posting()
    code, calls = lead_run(tmp_path, monkeypatch, og, "start", "claude", "t", "b.md", "r.md")
    assert code == 0
    sid = "s0"  # started top-level by a lead's helper, which subscribed its root
    og.parents.clear()
    og.subscriptions.append(("lead", sid))
    og.final = "idle"
    assert omnigent_agent.main(["wait", sid, "r.md", "--notify"]) == 0
    assert og.wakes(calls) == 1 and not calls.exists()
    assert "not telling $LOOP_NOTIFY" in capsys.readouterr().err


def test_a_child_another_session_subscribes_to_still_gets_the_notify_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = Posting()
    code, calls = lead_run(tmp_path, monkeypatch, og, "start", "claude", "t", "b.md", "r.md")
    assert code == 0
    og.parents.clear()
    og.subscriptions.append(("chief", "s0"))  # Omnigent tells chief, not the caller
    assert omnigent_agent.main(["wait", "s0", "r.md", "--notify"]) == 0
    assert calls.read_text().startswith("exit 0:")


def test_a_sub_agent_caller_still_gets_the_line_for_a_child_its_primary_hears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = Posting()
    code, calls = lead_run(tmp_path, monkeypatch, og, "start", "claude", "t", "b.md", "r.md")
    assert code == 0 and og.parents == {"s0": "lead"}  # the primary session is told
    monkeypatch.setenv("OMNIGENT_SESSION_ID", "worker")  # the caller is a sub-agent of the lead
    assert omnigent_agent.main(["wait", "s0", "r.md", "--notify"]) == 0
    assert calls.read_text().startswith("exit 0:")


def test_a_covered_child_that_never_ends_its_turn_still_gets_the_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = Posting(status="running")
    code, calls = lead_run(
        tmp_path, monkeypatch, og, "start", "claude", "t", "b.md", "r.md", writes=False
    )
    assert code == 0 and og.parents == {"s0": "lead"}  # covered: its turn end would tell the lead
    assert omnigent_agent.main(["wait", "s0", "r.md", "--timeout", "0", "--notify"]) != 0
    assert calls.read_text().startswith("exit 1:")
    assert "not telling" not in capsys.readouterr().err


def test_a_covered_child_a_send_never_reaches_still_gets_the_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = Posting()
    code, calls = lead_run(
        tmp_path, monkeypatch, og, "start", "claude", "t", "b.md", "r.md", writes=False
    )
    assert code == 0 and og.parents == {"s0": "lead"}
    og.deaf = {0}  # the follow-up never lands
    argv = ["send", "s0", "b.md", "r.md", "--confirm-seconds", "0", "--notify"]
    assert omnigent_agent.main(argv) != 0
    assert calls.read_text().startswith("exit ")


def test_a_child_nothing_tells_the_lead_about_still_gets_the_notify_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = Posting()
    code, calls = lead_run(tmp_path, monkeypatch, og, "start", "claude", "t", "b.md", "r.md")
    assert code == 0
    og.parents.clear()  # top level, no subscription: the line is the only wake
    assert omnigent_agent.main(["wait", "s0", "r.md", "--notify"]) == 0
    assert og.wakes(calls) == 1 and calls.read_text().startswith("exit 0:")


def test_create_files_a_sub_agent_under_the_primary_session_and_a_lead_under_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "lead")
    og = Posting()
    og.create("claude", "child")
    og.create("claude", "helper", None, True)
    assert [b.get("parent_session_id") for b in og.bodies] == ["lead", None]


def test_notify_tells_loop_notify_of_an_unexpected_exception_and_reraises_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = notify_stub(tmp_path, monkeypatch)
    og = FakeOmnigent()
    sid, _ = notify_session(tmp_path, monkeypatch, og)

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("client broke\nsecond line")

    og.info = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        omnigent_agent.main(["wait", sid, "r.md", "--notify"])
    assert calls.read_text().splitlines() == ["exit 1: client broke"]


def test_without_notify_loop_notify_is_never_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = notify_stub(tmp_path, monkeypatch)
    og = FakeOmnigent()
    sid, repo = notify_session(tmp_path, monkeypatch, og)
    (repo / "r.md").write_text("ok")
    assert omnigent_agent.main(["wait", sid, "r.md"]) == 0
    assert not calls.exists()


def test_wait_until_exits_0_once_the_command_does_and_its_own_code_at_the_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    count = tmp_path / "count"
    third = f"n=$(cat {count} 2>/dev/null || echo 0); echo $((n+1)) > {count}; [ $n -ge 2 ]"
    argv = ["wait-until", "--every", "0.01", "--limit", "30", "--", "sh", "-c", third]
    assert omnigent_agent.main(argv) == 0
    assert count.read_text().strip() == "3"

    argv = ["wait-until", "--every", "0.01", "--limit", "0.05", "--", "false"]
    assert omnigent_agent.main(argv) == omnigent_agent.LIMIT != 0
    assert "did not hold within" in capsys.readouterr().err
    # a sleep past a minute could outlast a tool call
    assert omnigent_agent.main(["wait-until", "--every", "61", "--limit", "5", "--", "true"]) == 2


def test_wait_until_refuses_pr_poll(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ran = tmp_path / "ran"
    for cmd in (
        ["sh", "-c", f"touch {ran}; gh pr view 7 --json state"],
        ["gh", "pr", "checks", "7"],
        ["gh", "pr", "status"],
        ["gh", "api", "repos/o/r/pulls/7"],
        ["gh", "api", "-H", "Accept: x", "repos/o/r/pulls/7/reviews"],
    ):
        assert (
            omnigent_agent.main(["wait-until", "--every", "5", "--limit", "5", "--", *cmd]) == 2
        ), cmd
        assert "wait-for --pr" in capsys.readouterr().err
    assert not ran.exists()
    # only a single-PR read has a free replacement
    assert omnigent_agent.main(["wait-until", "--every", "5", "--limit", "5", "--", "true"]) == 0
    assert not omnigent_agent.PR_POLL.search("gh pr list --state open")
    # one line of a script: a later line naming a PR does not make an earlier gh api call a PR read
    assert not omnigent_agent.PR_POLL.search("gh api repos/o/r/issues/1\necho pulls/7")


def test_wait_for_a_gate_returns_at_its_exit_line_or_still_running_with_its_last_log_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(omnigent_agent, "WAIT_FOR_EVERY", 0.01)
    log = tmp_path / "gate.log"
    log.write_text("collecting\nexample progress 50%\n")
    assert omnigent_agent.main(["wait-for", "--log", str(log), "--budget", "0.05"]) == 6
    assert capsys.readouterr().out == "still running\ncollecting\nexample progress 50%\n"

    def finish() -> None:
        time.sleep(0.1)
        log.write_text(log.read_text() + "12 passed in 3s\nexit 0\n")

    threading.Thread(target=finish).start()
    assert omnigent_agent.main(["wait-for", "--log", str(log), "--budget", "30"]) == 0
    assert capsys.readouterr().out == "exit 0\n12 passed in 3s\n"

    log.write_text("".join(f"line {i}\n" for i in range(50)) + "FAILED test_x\nexit 2\n")
    assert omnigent_agent.main(["wait-for", "--log", str(log)]) == 2
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "exit 2" and out[-1] == "FAILED test_x" and len(out) == 1 + 30


def test_wait_for_reads_a_lock_wait_as_waiting_and_a_held_lock_past_budget_as_a_timeout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(omnigent_agent, "WAIT_FOR_EVERY", 0.01)
    log, lock = tmp_path / "gate.log", tmp_path / "gate.lock"
    lock.write_text(f"{os.getpid()}\n")
    argv = ["wait-for", "--log", str(log), "--budget", "0.05"]
    log.write_text(f"run 1\nlock {lock}\n")  # queued: the run's budget has not started
    assert omnigent_agent.main(argv) == 6
    assert capsys.readouterr().out == f"waiting on the gate lock (held by {os.getpid()})\n"
    log.write_text(f"run 1\nlock {lock}\nlocked\nrunning\n")  # holds it: out of budget
    assert omnigent_agent.main(argv) == 6
    assert capsys.readouterr().out == "still running\nrunning\n"  # with its progress line
    # the lock arrives mid-wait: the full budget runs from `locked`, not from entry
    log.write_text(f"run 1\nlock {lock}\n")
    locked_at: list[float] = []

    def take_the_lock() -> None:
        locked_at.append(
            time.monotonic()
        )  # before the write: the wait can see `locked` the moment it lands
        log.write_text(f"run 1\nlock {lock}\nlocked\nrunning\n")

    # a loaded or niced host fires the timer late, so the budget is measured from the write
    threading.Timer(0.05, take_the_lock).start()
    began = time.monotonic()
    assert omnigent_agent.main(["wait-for", "--log", str(log), "--budget", "0.3"]) == 6
    ended = time.monotonic()
    # the lock came inside the entry's budget (else nothing is proven), and the budget ran on from it
    assert locked_at and locked_at[0] - began < 0.3
    assert ended - locked_at[0] >= 0.3
    assert capsys.readouterr().out == "still running\nrunning\n"
    log.write_text(f"lock {lock}\n")  # waiting past the lock ceiling: no verdict
    os.utime(log, (time.time() - 7300, time.time() - 7300))
    assert omnigent_agent.main(argv) == 6
    assert capsys.readouterr().out.startswith("gave up waiting on the gate lock")


def test_wait_for_a_pid_ends_when_the_run_is_gone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(omnigent_agent, "WAIT_FOR_EVERY", 0.01)
    run = subprocess.Popen(["sleep", "0.2"])
    threading.Thread(target=run.wait).start()  # reaped, so the pid goes rather than lingering
    pid = tmp_path / "pid"
    pid.write_text(f"{run.pid}\n")
    assert omnigent_agent.main(["wait-for", "--pid", str(pid), "--budget", "30"]) == 0
    assert capsys.readouterr().out == f"pid {pid} ended\n"
    # a live process younger than the pid file got the run's pid after it ended
    other = subprocess.Popen(["sleep", "30"])
    pid.write_text(f"{other.pid}\n")
    os.utime(pid, (time.time() - 3600, time.time() - 3600))
    try:
        assert omnigent_agent.main(["wait-for", "--pid", str(pid), "--budget", "30"]) == 0
    finally:
        other.kill()
        other.wait()
    capsys.readouterr()
    # a run killed before its exit line: the wait ends on it, not at the budget
    log = tmp_path / "gate.log"
    log.write_text("running\n")
    argv = ["wait-for", "--log", str(log), "--pid", str(tmp_path / "none"), "--budget", "30"]
    assert omnigent_agent.main(argv) == 1
    assert capsys.readouterr().out.startswith("gone: ")
    assert omnigent_agent.main(["wait-for", "--budget", "1"]) == 2


def test_wait_for_takes_an_exit_line_only_from_the_run_its_pid_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(omnigent_agent, "WAIT_FOR_EVERY", 0.01)
    log, pid = tmp_path / "gate.log", tmp_path / "pid"
    log.write_text("run 1\nprevious run passed\nexit 0\n")  # a log path reused, not yet rewritten
    run = subprocess.Popen(["sleep", "30"])
    try:
        pid.write_text(f"{run.pid}\n")
        argv = ["wait-for", "--log", str(log), "--pid", str(pid), "--budget", "0.05"]
        assert omnigent_agent.main(argv) == 6
        assert capsys.readouterr().out == "still running\n"
        argv[4] = str(run.pid)  # a bare pid restarted at once: no time slack lets the old line in
        assert omnigent_agent.main(argv) == 6
        assert capsys.readouterr().out == "still running\n"
        log.write_text(f"run {run.pid}\n3 passed\nexit 0\n")  # this run's own line counts
        assert omnigent_agent.main(argv) == 0
        assert capsys.readouterr().out == "exit 0\n3 passed\n"
        log.write_text("run 1\nprevious run passed\nexit 0\n")
    finally:
        run.kill()
        run.wait()
    # the run died before it rewrote the log: gone, not the old run's success, however old the log
    os.utime(log, (time.time() - 60, time.time() - 60))
    for named in (str(pid), str(run.pid), str(tmp_path / "missing.pid")):
        argv = ["wait-for", "--log", str(log), "--pid", named, "--budget", "30"]
        assert omnigent_agent.main(argv) == 1
        assert capsys.readouterr().out.startswith("gone: ")


def test_wait_for_fails_when_it_cannot_tell_a_pid_is_the_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    denied = tmp_path / "bin" / "ps"
    denied.parent.mkdir()
    denied.write_text("#!/bin/sh\necho 'ps: operation not permitted' >&2\nexit 1\n")
    denied.chmod(0o755)
    monkeypatch.setenv("PATH", f"{denied.parent}{os.pathsep}{os.environ['PATH']}")
    run = subprocess.Popen(["sleep", "30"])
    try:
        pid = tmp_path / "pid"
        pid.write_text(f"{run.pid}\n")
        assert omnigent_agent.main(["wait-for", "--pid", str(pid), "--budget", "0.05"]) == 1
        captured = capsys.readouterr()
        assert captured.out == "" and "operation not permitted" in captured.err
    finally:
        run.kill()
        run.wait()


def rest_like(view: Callable[[], dict]) -> Callable[..., str]:
    """A fake ``gh`` answering wait-for's REST reads (#1984) from ``view()``, a ``gh pr view`` shape read once a round
    (at the round's pull read): the pull's state, then its check runs and statuses. No headers, so no ETag is kept."""
    now: dict = {}

    def gh(*args: str, **kw: object) -> str:
        path = args[-1]
        if path.endswith("/pulls/7"):
            now.clear()
            now.update(view())
            state = now.get("state", "OPEN")
            pull = {"state": "open" if state == "OPEN" else "closed", "merged": state == "MERGED"}
            return json.dumps({**pull, "head": {"sha": "s"}})
        rollup = now.get("statusCheckRollup") or []
        if "check-runs" in path:
            runs = [
                {
                    "name": c["name"],
                    "status": c["status"].lower(),
                    "conclusion": (c.get("conclusion") or "").lower(),
                }
                for c in rollup
                if "name" in c
            ]
            return json.dumps({"check_runs": runs})
        statuses = [
            {"context": c["context"], "state": c["state"].lower()} for c in rollup if "context" in c
        ]
        return json.dumps({"statuses": statuses})

    return gh


def pr_rest(
    calls: list | None = None, state: str = "open", repo_seen: list | None = None
) -> Callable[..., str]:
    """A fake ``gh`` answering the supervisor's PR read through REST (#2068): the pull (branches, head), no check runs.
    Every call is kept in ``calls``; the repo each pull read names, in ``repo_seen`` ("" for the clone's own)."""

    def gh(*args: str, **kw: object) -> str:
        if calls is not None:
            calls.append(args)
        path = args[-1]
        if m := re.fullmatch(r"repos/([^/]+/[^/]+|\{owner\}/\{repo\})/pulls/\d+", path):
            if repo_seen is not None:
                repo_seen.append("" if m[1].startswith("{") else m[1])
            return json.dumps(
                {
                    "state": state,
                    "head": {"sha": "s1", "ref": "feat"},
                    "base": {"ref": "main"},
                }
            )
        if "check-runs" in path:
            return json.dumps({"check_runs": []})
        return json.dumps({"statuses": []})

    return gh


def test_wait_for_a_pr_waits_until_its_checks_settle(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    views = iter(
        [
            {"state": "OPEN", "statusCheckRollup": []},
            # a check still running has no conclusion yet: the wait goes on past it
            {"state": "OPEN", "statusCheckRollup": [{"name": "build", "status": "IN_PROGRESS"}]},
            {
                "state": "OPEN",
                "statusCheckRollup": [
                    {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
                    {"name": "test", "status": "COMPLETED", "conclusion": "FAILURE"},
                    {"context": "lint", "state": "SUCCESS"},
                ],
            },
        ]
    )
    monkeypatch.setattr(omnigent_agent, "gh", rest_like(lambda: next(views)))
    monkeypatch.setattr(omnigent_agent, "WAIT_FOR_PR_EVERY", 0.01)
    assert omnigent_agent.main(["wait-for", "--pr", "7", "--budget", "30"]) == 1
    assert capsys.readouterr().out == "PR #7 checks red: test\n"
    assert next(views, None) is None  # it read the settled view, not the one still running

    green = {
        "state": "OPEN",
        "statusCheckRollup": [
            {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"context": "lint", "state": "SUCCESS"},
        ],
    }
    monkeypatch.setattr(omnigent_agent, "gh", rest_like(lambda: green))
    assert omnigent_agent.main(["wait-for", "--pr", "7"]) == 0
    assert capsys.readouterr().out == "PR #7 checks green (2)\n"

    monkeypatch.setattr(omnigent_agent, "gh", rest_like(lambda: {"state": "CLOSED"}))
    assert omnigent_agent.main(["wait-for", "--pr", "7", "--until", "merged"]) == 1
    assert omnigent_agent.main(["wait-for", "--pr", "7", "--until", "closed"]) == 0


def test_wait_for_a_pr_keeps_its_budget_when_github_is_slow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    slow = tmp_path / "gh"
    slow.write_text('#!/bin/sh\nsleep 2\necho \'{"state": "OPEN", "statusCheckRollup": []}\'\n')
    slow.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    began = time.monotonic()
    assert omnigent_agent.main(["wait-for", "--pr", "7", "--budget", "0.2"]) == 6
    assert time.monotonic() - began < 1.5  # the query is cut at the budget, not gh's own timeout
    assert capsys.readouterr().out == "still running\n"


def test_a_detached_gate_tells_the_session_that_started_it_once(tmp_path: Path) -> None:
    told = tmp_path / "told"
    tell = tmp_path / "tell"
    tell.write_text(f'#!/bin/sh\nprintf "%s|%s\\n" "$1" "$2" >> {told}\n')
    tell.chmod(0o755)
    log, pid = tmp_path / "gate.log", tmp_path / "pid"
    script = Path(omnigent_agent.__file__)
    # verifier.md's detached recipe with its notify step
    wrapper = (
        f'echo $$ > {pid}; echo "run $$" > {log}; sh -c "echo 4 passed; exit 3" >> {log} 2>&1;'
        f' echo "exit $?" >> {log}; {sys.executable} {script} wait-for --log {log} --pid $$ --notify'
    )
    env = {**os.environ, "LOOP_NOTIFY": f"{tell} session-1"}
    subprocess.run(["sh", "-c", wrapper], env=env, check=False, capture_output=True, timeout=30)
    assert told.read_text() == "session-1|exit 3\n4 passed\n"  # the red run's last lines too
    # a run's own exit 6 is an outcome, not wait-for's budget passing
    six = wrapper.replace("exit 3", "exit 6")
    subprocess.run(["sh", "-c", six], env=env, check=False, capture_output=True, timeout=30)
    assert told.read_text().endswith("session-1|exit 6\n4 passed\n")
    del env["LOOP_NOTIFY"]  # unset: the outcome is only printed
    subprocess.run(["sh", "-c", wrapper], env=env, check=False, capture_output=True, timeout=30)
    assert told.read_text().count("session-1|") == 2


def test_verifier_wait_returns_only_after_the_verdict(tmp_path: Path) -> None:
    og = FakeOmnigent(reply="checking", status="running")
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(og, "codex", "t", "do X", report, None, 0)
    report.write_text("## Standards\n\nFULL_GATE_PENDING\n")
    with pytest.raises(omnigent_agent.Fail, match="no verdict after 0s"):
        omnigent_agent.wait(og, sid, report, timeout=0, verdict=True)
    polls = 0

    def info(s: str) -> dict:
        nonlocal polls
        polls += 1
        if polls == 3:
            report.write_text(report.read_text() + "SATISFIED: yes\n")
        if polls == 4:  # it revises the verdict before its turn ends
            report.write_text(report.read_text().replace("yes", "no"))
        return {"status": "running" if polls < 5 else "idle", "pending_elicitations": []}

    og.info = info  # type: ignore[method-assign]
    assert omnigent_agent.wait(og, sid, report, verdict=True) == str(report)
    assert polls == 5
    assert report.read_text().endswith("SATISFIED: no\n")


def test_a_report_written_while_the_turn_runs_waits_for_the_turn_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(reply="landed #1", status="running")
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(og, "claude", "t", "do X", report, None, 0)
    report.write_text("progress: #1 landed")  # a lead writing progress at its report path
    with pytest.raises(omnigent_agent.Fail, match="no report after 0s"):
        omnigent_agent.wait(og, sid, report, timeout=0)
    og.final = "idle"
    assert omnigent_agent.wait(og, sid, report, timeout=0) == str(report)


def test_one_idle_look_is_no_turn_end_and_the_deadline_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(reply="checking")  # reads idle mid-turn, its draft long written
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(og, "codex", "t", "do X", report, None, 0)
    report.write_text("draft\nSATISFIED: yes\n")
    old = time.time() - 120
    os.utime(report, (old, old))
    monkeypatch.setattr(omnigent_agent, "TURN_END_SECONDS", 60)
    looks = 0
    info = og.info

    def rewriting(s: str) -> dict:
        nonlocal looks
        looks += 1
        assert looks < 5, "the wait ran past its deadline"
        report.write_text(f"draft {looks}\nSATISFIED: yes\n")  # still revising
        return info(s)

    og.info = rewriting  # type: ignore[method-assign]
    with pytest.raises(omnigent_agent.Fail, match="no verdict after 0s"):
        omnigent_agent.wait(og, sid, report, timeout=0, verdict=True)
    og.info = info  # type: ignore[method-assign]
    monkeypatch.setattr(omnigent_agent, "TURN_END_SECONDS", 0)
    assert omnigent_agent.wait(og, sid, report, timeout=0, verdict=True) == str(report)


def test_a_lost_turn_end_counts_the_report_once_it_and_the_history_are_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent(reply="done", status="running")  # Omnigent never shows the turn end
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(og, "claude", "t", "do X", report, None, 0)
    report.write_text("final")
    old = time.time() - 120
    os.utime(report, (old, old))
    for item in og.sessions[sid]:
        item["created_at"] = old
    monkeypatch.setattr(omnigent_agent, "SETTLE_SECONDS", 180)
    with pytest.raises(omnigent_agent.Fail, match="no report after 0s"):
        omnigent_agent.wait(og, sid, report, timeout=0)
    monkeypatch.setattr(omnigent_agent, "SETTLE_SECONDS", 60)
    og.sessions[sid].append({"type": "message", "role": "assistant", "created_at": time.time()})
    with pytest.raises(omnigent_agent.Fail, match="no report after 0s"):  # its history is not
        omnigent_agent.wait(og, sid, report, timeout=0)
    og.sessions[sid][-1]["created_at"] = old
    assert omnigent_agent.wait(og, sid, report, timeout=0) == str(report)
    assert "a lost turn end" in capsys.readouterr().err


def test_verifier_wait_fails_when_turn_ends_without_verdict(tmp_path: Path) -> None:
    og = FakeOmnigent(reply="done")
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(og, "codex", "t", "do X", report, None, 0)
    report.write_text("VERDICT: 0 blocker, 0 major, 0 minor\n")
    with pytest.raises(omnigent_agent.Fail, match=r"ended \(idle\) without a verdict"):
        omnigent_agent.wait(og, sid, report, verdict=True)


def test_worker_wait_needs_no_verdict(tmp_path: Path) -> None:
    og = FakeOmnigent(reply="checking")
    sid = og.create("claude", "t")
    (tmp_path / "r.md").write_text("partial")
    assert omnigent_agent.wait(og, sid, tmp_path / "r.md", timeout=0) == str(tmp_path / "r.md")


def test_wait_fails_with_last_message_when_turn_ends_without_report(
    tmp_path: Path,
) -> None:
    og = FakeOmnigent(reply="Shall I go ahead?")
    sid, _ = omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
    with pytest.raises(omnigent_agent.Fail, match="(?s)without .*Shall I go ahead"):
        omnigent_agent.wait(og, sid, tmp_path / "r.md")


def test_wait_fails_on_failed_session_before_reply(tmp_path: Path) -> None:
    og = FakeOmnigent(status="failed")
    sid, _ = omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
    with pytest.raises(omnigent_agent.Fail, match=r"ended \(failed\)"):
        omnigent_agent.wait(og, sid, tmp_path / "r.md")


def test_wait_fails_at_once_on_pending_prompt(tmp_path: Path) -> None:
    og = FakeOmnigent(status="running")
    sid, _ = omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
    og.prompts = [
        {
            "type": "response.elicitation_request",
            "params": {"message": "Allow Bash: rm -rf x?"},
        }
    ]
    with pytest.raises(omnigent_agent.Fail, match="blocked on a prompt: Allow Bash: rm -rf x"):
        omnigent_agent.wait(og, sid, tmp_path / "r.md")


def test_wait_times_out_with_last_message(tmp_path: Path) -> None:
    og = FakeOmnigent(reply="still working", status="running")
    sid, _ = omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
    with pytest.raises(omnigent_agent.Fail, match="(?s)no report after 0s.*still working"):
        omnigent_agent.wait(og, sid, tmp_path / "r.md", timeout=0)


def test_wait_keeps_waiting_through_a_mid_turn_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(status="idle")  # the brief is the last item while the child works
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(og, "claude", "t", "do X", report, None, 0)
    monkeypatch.setattr(omnigent_agent, "IDLE_GRACE_SECONDS", 3600)
    statuses = iter(["running", "idle", "idle", "running"])

    def info(s: str) -> dict:
        status = next(statuses, None)
        if status is None and not report.exists():
            report.write_text("done")
            status = "running"
        return {"status": status or "idle", "pending_elicitations": []}

    og.info = info  # type: ignore[method-assign]
    assert omnigent_agent.wait(og, sid, report, timeout=5) == str(report)


def test_wait_keeps_waiting_through_launch_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(status="idle")
    sid, _ = omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
    monkeypatch.setattr(omnigent_agent, "IDLE_GRACE_SECONDS", 3600)
    with pytest.raises(omnigent_agent.Fail, match="no report after 0s"):  # timeout, not "ended"
        omnigent_agent.wait(og, sid, tmp_path / "r.md", timeout=0)
    monkeypatch.setattr(omnigent_agent, "IDLE_GRACE_SECONDS", 0)
    with pytest.raises(omnigent_agent.Fail, match=r"ended \(idle\)"):
        omnigent_agent.wait(og, sid, tmp_path / "r.md")


def test_a_taken_title_moves_to_the_next_free_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    og.taken = {"t", "t-r2"}  # an earlier pass's children, archived or not
    sid, _ = omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
    assert og.titles[sid] == "t-r3"
    err = capsys.readouterr().err
    assert "title t is taken" in err and f"session {sid} is titled t-r3" in err


def test_a_taken_title_and_a_deaf_session_share_one_suffix_sequence(tmp_path: Path) -> None:
    og = FakeOmnigent(deaf={0})
    og.taken = {"t-r2"}
    sid, _ = omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
    assert [og.titles[s] for s in og.sessions] == ["t", "t-r3"]
    assert og.titles[sid] == "t-r3"


@pytest.mark.parametrize("inline", [False, True])
def test_a_second_start_of_the_same_brief_attaches_to_the_live_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    inline: bool,
) -> None:
    """A rerun after a `run` that failed on its own while the child worked on: one pass, paid once."""
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "lead")
    monkeypatch.setattr(omnigent_agent, "briefed_by_file", lambda *_: not inline)
    og = FakeOmnigent()
    first, _ = omnigent_agent.start(og, "codex", "t", "do X", tmp_path / "r.md", None, 0)
    og.taken = {"t"}  # Omnigent's 409 would retitle a second create t-r2
    assert omnigent_agent.start(og, "codex", "t", "do X", tmp_path / "r.md", None, 0) == (first, [])
    assert list(og.sessions) == [first]
    assert f"session {first} already holds this brief" in capsys.readouterr().err


@pytest.mark.parametrize(
    "change", ["brief", "archived", "failed", "report path", "top level", "no parent"]
)
def test_a_changed_brief_or_a_gone_child_starts_a_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "lead")
    og = FakeOmnigent()
    first, _ = omnigent_agent.start(og, "codex", "t", "do X", tmp_path / "r.md", None, 0)
    brief, report, top = "do X", tmp_path / "r.md", False
    if change == "brief":
        brief = "do X on the new head"
    elif change == "archived":
        og.archived.append(first)
    elif change == "failed":
        monkeypatch.setattr(og, "final", "failed")
    elif change == "report path":
        report = tmp_path / "r2.md"
    elif change == "top level":
        top = True
    else:
        monkeypatch.delenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID")
    second, _ = omnigent_agent.start(og, "codex", "t", brief, report, None, 0, top_level=top)
    assert second != first


@pytest.mark.parametrize("inline", [False, True])
def test_a_rerun_attaches_to_the_child_briefed_with_that_text_not_an_earlier_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inline: bool
) -> None:
    """Brief A, then B, then B again: the file now holds B and both children are live."""
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "lead")
    monkeypatch.setattr(omnigent_agent, "briefed_by_file", lambda *_: not inline)
    og = FakeOmnigent()
    report = tmp_path / "r.md"
    a, _ = omnigent_agent.start(og, "codex", "t", "review A and check speed", report, None, 0)
    og.taken = {"t"}
    b, _ = omnigent_agent.start(og, "codex", "t", "review A", report, None, 0)  # a prefix of A
    assert b != a
    assert omnigent_agent.start(og, "codex", "t", "review A", report, None, 0) == (b, [])


def test_a_child_whose_brief_left_its_history_page_is_still_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "lead")
    og = FakeOmnigent()
    first, _ = omnigent_agent.start(og, "codex", "t", "do X", tmp_path / "r.md", None, 0)
    og.sessions[first] = [{"type": "resource_event"}] * 100  # the newest page, no brief in it
    og.taken = {"t"}
    assert omnigent_agent.start(og, "codex", "t", "do X", tmp_path / "r.md", None, 0) == (first, [])


def test_a_child_that_already_wrote_the_report_is_not_waited_on_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "lead")
    og = FakeOmnigent()
    first, _ = omnigent_agent.start(og, "codex", "t", "do X", tmp_path / "r.md", None, 0)
    og.created[first] -= 10
    (tmp_path / "r.md").write_text("done")
    with pytest.raises(omnigent_agent.Fail, match=f"session {first} already wrote .*: read it"):
        omnigent_agent.start(og, "codex", "t", "do X", tmp_path / "r.md", None, 0)
    assert list(og.sessions) == [first]


def test_start_gives_up_when_every_title_is_taken(tmp_path: Path) -> None:
    og = FakeOmnigent()
    og.taken = {"t"} | {f"t-r{n}" for n in range(2, omnigent_agent.TITLES + 1)}
    with pytest.raises(
        omnigent_agent.Fail, match=f"titles t to t-r{omnigent_agent.TITLES} are taken"
    ):
        omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
    assert og.sessions == {}


def test_failed_archive_is_reported_and_retry_proceeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent(deaf={0})
    og.fail.add("archive")
    assert omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0) == (
        "s1",
        ["s0"],
    )
    assert "abandoned session s0 survives" in capsys.readouterr().err


def test_send_failure_after_create_archives_session(tmp_path: Path) -> None:
    og = FakeOmnigent()
    og.fail.add("send")
    with pytest.raises(omnigent_agent.Fail, match="s0: POST events: HTTP 503"):
        omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
    assert og.archived == ["s0"]


def test_main_exits_nonzero_when_abandoned_session_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(deaf={0})
    og.fail.add("archive")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("do X")
    assert (
        omnigent_agent.main(
            [
                "start",
                "claude",
                "t",
                str(tmp_path / "b.md"),
                str(tmp_path / "r.md"),
                "--confirm-seconds",
                "0",
            ]
        )
        == 1
    )


def test_token_lookup_timeout_is_a_clear_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def hang(*a: object, **k: object) -> None:
        raise omnigent_agent.subprocess.TimeoutExpired("python", k["timeout"])  # type: ignore[arg-type]

    monkeypatch.setattr(omnigent_agent.subprocess, "run", hang)
    og = object.__new__(omnigent_agent.Omnigent)
    og.base = "https://omnigent.test"
    with pytest.raises(omnigent_agent.Fail, match="timed out after 30s"):
        og.token()


def test_stalled_server_timeout_reaches_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Stall(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            time.sleep(0.3)  # accepted the event, answers after the client gave up
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    class Local(omnigent_agent.Omnigent):
        def __init__(self, base: str) -> None:
            self.base, self._token, self.archived = base, "synthetic", []
            self.host_id = "h"

        def create(self, *args: object) -> str:
            return "s0"

        def archive(self, sid: str) -> None:
            self.archived.append(sid)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Stall)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        og = Local(f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setattr(omnigent_agent, "HTTP_SECONDS", 0.05)
        # the timed-out POST leaves delivery to the history, which this server cannot show
        with pytest.raises(omnigent_agent.Fail, match="s0: GET .*HTTP 501"):
            omnigent_agent.start(og, "codex", "t", "do X", tmp_path / "r.md", None, 0)
        assert og.archived == ["s0"]
    finally:
        server.shutdown()


def test_a_start_whose_send_timed_out_keeps_the_session_the_history_shows_took_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib.error import URLError

    class Late(FakeOmnigent):
        def send(self, sid: str, text: str) -> None:
            super().send(sid, text)  # the server took it, after the client gave up
            raise omnigent_agent.Fail("POST events: timed out") from URLError(
                TimeoutError("timed out")
            )

    og = Late()
    sid = omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)[0]
    assert og.archived == [] and og.posts == 1

    # a timeout only: a reset still fails, as Omnigent.send itself does on any error (the
    # supervisor's sends advance their state on its return)
    def reset(sid: str, text: str) -> None:
        raise omnigent_agent.Fail("POST events: reset") from ConnectionResetError("reset")

    monkeypatch.setattr(og, "send", reset)
    with pytest.raises(omnigent_agent.Fail, match="reset"):
        omnigent_agent.send_to_confirm(og, sid, "go")


def test_start_waits_while_the_host_is_over_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.undo()  # the real capacity check, on a faked host
    monkeypatch.setattr(omnigent_agent, "POLL_SECONDS", 0)
    monkeypatch.setattr(omnigent_agent, "TURN_END_SECONDS", 0)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 16000000 kB\nMemAvailable: 1048576 kB\n")
    monkeypatch.setattr(omnigent_agent, "MEMINFO", meminfo)
    monkeypatch.setattr(omnigent_agent.os, "getloadavg", lambda: (300.0, 0.0, 0.0))  # not CPU
    monkeypatch.setattr(omnigent_agent, "cpu_idle", lambda: 40.0)
    assert omnigent_agent.main(["capacity"]) == 1  # short of memory
    assert capsys.readouterr().out == "cpu 40% idle (min 10), 1024 MB available (min 1536)\n"
    meminfo.write_text("MemAvailable: 4194304 kB\n")
    assert omnigent_agent.main(["capacity"]) == 0  # a high load average with idle CPU has room
    monkeypatch.setattr(omnigent_agent, "cpu_idle", lambda: 3.0)
    monkeypatch.setattr(omnigent_agent, "CAPACITY_WAIT_SECONDS", 0)
    og = FakeOmnigent()
    with pytest.raises(omnigent_agent.Fail, match="host over capacity for 0s: cpu 3% idle"):
        omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
    assert og.sessions == {}

    idles = iter([3.0, 25.0])
    monkeypatch.setattr(omnigent_agent, "cpu_idle", lambda: next(idles))
    monkeypatch.setattr(omnigent_agent, "CAPACITY_WAIT_SECONDS", 60)
    omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
    assert "host over capacity, waiting: cpu 3% idle" in capsys.readouterr().err
    assert og.sessions  # started once the CPU freed up


def test_capacity_reads_macos_memory_or_says_it_is_not_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.undo()  # the real capacity check, on a faked host
    monkeypatch.setattr(omnigent_agent, "MEMINFO", tmp_path / "absent")
    monkeypatch.setattr(omnigent_agent, "cpu_idle", lambda: None)  # load decides
    monkeypatch.setattr(omnigent_agent.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(omnigent_agent.os, "getloadavg", lambda: (8.0, 0.0, 0.0))
    sysctl = subprocess.CompletedProcess([], 0, stdout="4\n34359738368\n")
    monkeypatch.setattr(omnigent_agent.subprocess, "run", lambda *a, **k: sysctl)
    assert omnigent_agent.capacity() == (
        False,
        "load 1.0 per core (max 4), 1311 MB available (min 1536)",
    )
    sysctl.stdout = ""
    assert omnigent_agent.capacity() == (
        True,
        "load 1.0 per core (max 4), memory not measured (min 1536)",
    )


def test_cpu_idle_reads_proc_stat_or_iostats_second_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.undo()  # the real measurement, on a faked host
    stat = tmp_path / "stat"
    stat.write_text("cpu 100 0 100 700 100 0 0 0 50 0\ncpu0 1 2 3 4 5 6 7 8\n")
    monkeypatch.setattr(omnigent_agent, "PROC_STAT", stat)
    later = "cpu 160 0 140 760 140 0 0 0 90 0\n"  # a second later; guest is counted in user
    monkeypatch.setattr(omnigent_agent.time, "sleep", lambda _: stat.write_text(later))
    assert omnigent_agent.cpu_idle() == 50.0  # (60 idle + 40 iowait) of 200 ticks
    monkeypatch.setattr(omnigent_agent, "PROC_STAT", tmp_path / "absent")
    head = "      cpu    load average\n us sy id   1m   5m   15m\n 33 19 48  131.50 100.60 107.07\n"
    iostat = subprocess.CompletedProcess([], 0, stdout=head)
    monkeypatch.setattr(omnigent_agent.subprocess, "run", lambda *a, **k: iostat)
    assert omnigent_agent.cpu_idle() is None  # one sample counts since boot: not measured
    iostat.stdout += " 41 21 38  131.50 100.60 107.07\n"
    assert omnigent_agent.cpu_idle() == 38.0


def test_stalled_error_body_reaches_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class StallBody(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(503)
            self.send_header("Content-Length", "100")
            self.end_headers()
            time.sleep(0.3)  # the error body never arrives before the client's timeout

        def log_message(self, *args: object) -> None:
            pass

    class Local(omnigent_agent.Omnigent):
        def __init__(self, base: str) -> None:
            self.base, self._token, self.archived = base, "synthetic", []
            self.host_id = "h"

        def create(self, *args: object) -> str:
            return "s0"

        def archive(self, sid: str) -> None:
            self.archived.append(sid)

    server = ThreadingHTTPServer(("127.0.0.1", 0), StallBody)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        og = Local(f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setattr(omnigent_agent, "HTTP_SECONDS", 0.05)
        with pytest.raises(omnigent_agent.Fail, match="s0: POST .*HTTP 503"):
            omnigent_agent.start(og, "codex", "t", "do X", tmp_path / "r.md", None, 0)
        assert og.archived == ["s0"]
    finally:
        server.shutdown()


def test_create_files_the_session_under_the_project(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, str, dict | None]] = []

    def call(self, method: str, path: str, body: dict | None = None) -> object:
        calls.append((method, path, body))
        return {
            "/v1/agents": [{"name": omnigent_agent.AGENTS["claude"], "id": "a1"}],
            "/v1/agents?scope=user": [],
            "/v1/projects": {"data": [{"name": "other", "id": "p0"}, {"name": "shop", "id": "p1"}]},
            "/v1/sessions": {"id": "s1"},
            "/v1/sessions/coord": {},  # a lead filed nowhere
        }[path.removesuffix(omnigent_agent.SLIM)]

    monkeypatch.setattr(omnigent_agent.Omnigent, "call", call)
    monkeypatch.setattr(
        omnigent_agent.Omnigent, "__init__", lambda self: setattr(self, "host_id", "h")
    )
    monkeypatch.setattr(omnigent_agent, "repo_name", lambda: "shop")
    monkeypatch.delenv("OMNIGENT_PROJECT", raising=False)
    monkeypatch.delenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", raising=False)
    assert omnigent_agent.Omnigent().create("claude", "t") == "s1"
    assert calls[-1][2] and calls[-1][2]["project_id"] == "p1"
    assert "parent_session_id" not in calls[-1][2]  # run outside a session: top level
    assert capsys.readouterr().err == ""

    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "coord")
    omnigent_agent.Omnigent().create("claude", "t")
    assert calls[-1][2] and calls[-1][2]["parent_session_id"] == "coord"

    monkeypatch.setenv("OMNIGENT_PROJECT", "no-such-group")  # projects are only sidebar groups
    assert omnigent_agent.Omnigent().create("claude", "t") == "s1"
    assert calls[-1][2] and "project_id" not in calls[-1][2]
    assert capsys.readouterr().err == ""  # a child hides under its lead: no warning

    omnigent_agent.Omnigent().create("claude", "t", top_level=True)
    assert calls[-1][2] and "project_id" not in calls[-1][2]
    err = capsys.readouterr().err
    assert "'no-such-group'" in err and "OMNIGENT_PROJECT" in err

    # as the repo voice_note files under "Voice note"
    monkeypatch.setenv("OMNIGENT_PROJECT", "S_hop")
    omnigent_agent.Omnigent().create("claude", "t")
    assert calls[-1][2] and calls[-1][2]["project_id"] == "p1"


def test_create_files_the_session_under_its_caller_s_project(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A scheduled run is filed in its task's project, whose name its repo's need not match."""
    bodies: list[dict] = []
    sessions = {"lead": {"project_id": "p1"}, "child": {}}

    def call(self, method: str, path: str, body: dict | None = None) -> object:
        if method == "POST":
            bodies.append(body or {})
            return {"id": "s1"}
        if path.startswith("/v1/sessions/"):
            sid = path.removeprefix("/v1/sessions/").removesuffix(omnigent_agent.SLIM)
            if sid not in sessions:
                raise omnigent_agent.Fail(f"GET {path}: HTTP 404 Not Found")
            return sessions[sid]
        return {
            "/v1/agents": [{"name": omnigent_agent.AGENTS["claude"], "id": "a1"}],
            "/v1/agents?scope=user": [],
            "/v1/projects": {"data": [{"name": "other", "id": "p0"}, {"name": "eng", "id": "p1"}]},
        }[path]

    monkeypatch.setattr(omnigent_agent.Omnigent, "call", call)
    monkeypatch.setattr(
        omnigent_agent.Omnigent, "__init__", lambda self: setattr(self, "host_id", "h")
    )
    monkeypatch.setattr(omnigent_agent, "repo_name", lambda: "eng-repo")  # matches no project
    monkeypatch.delenv("OMNIGENT_PROJECT", raising=False)
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "lead")
    # a workspace outside a checkout, so no project line of this repo's applies
    omnigent_agent.Omnigent().create("claude", "t", top_level=True, workspace=str(tmp_path))
    assert bodies[-1]["project_id"] == "p1"
    assert capsys.readouterr().err == ""

    monkeypatch.setenv("OMNIGENT_SESSION_ID", "child")  # filed nowhere itself: its lead's
    omnigent_agent.Omnigent().create("claude", "t")
    assert bodies[-1]["project_id"] == "p1"

    monkeypatch.setenv("OMNIGENT_SESSION_ID", "gone")  # unreadable: falls through to its lead
    omnigent_agent.Omnigent().create("claude", "t")
    assert bodies[-1]["project_id"] == "p1"

    monkeypatch.setenv("OMNIGENT_SESSION_ID", "child")
    sessions["child"] = {"project_id": "p0"}  # its own project comes first
    omnigent_agent.Omnigent().create("claude", "t")
    assert bodies[-1]["project_id"] == "p0"

    monkeypatch.setenv("OMNIGENT_PROJECT", "eng")  # the override wins over both
    omnigent_agent.Omnigent().create("claude", "t")
    assert bodies[-1]["project_id"] == "p1"


def test_create_labels_the_session_with_its_loop_role_from_the_role_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies: list[dict] = []

    def call(self, method: str, path: str, body: dict | None = None) -> object:
        bodies.append(body or {})
        agents = [{"name": n, "id": a} for a, n in omnigent_agent.AGENTS.items()]
        return {
            "/v1/agents": agents,
            "/v1/agents?scope=user": [],
            "/v1/projects": {"data": []},
            "/v1/sessions": {"id": "s1"},
        }[path]

    monkeypatch.setattr(omnigent_agent.Omnigent, "call", call)
    monkeypatch.setattr(
        omnigent_agent.Omnigent, "__init__", lambda self: setattr(self, "host_id", "h")
    )
    og = omnigent_agent.Omnigent()
    for role in ("lead", "worker", "verifier"):
        og.create("codex", "t", role=f"/x/engineering-loop/{role}.md")
        assert bodies[-1]["labels"] == {"omni.role": role}
    for role in (None, "/x/reviewer.md"):
        og.create("codex", "t", role=role)
        assert "labels" not in bodies[-1]


def test_create_sets_a_claude_session_s_permission_mode_from_the_flag_or_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bodies: list[dict] = []

    def call(self, method: str, path: str, body: dict | None = None) -> object:
        bodies.append(body or {})
        agents = [{"name": n, "id": a} for a, n in omnigent_agent.AGENTS.items()]
        return {
            "/v1/agents": agents,
            "/v1/agents?scope=user": [],
            "/v1/projects": {"data": []},
            "/v1/sessions": {"id": "s1"},
        }[path]

    monkeypatch.setattr(omnigent_agent.Omnigent, "call", call)
    monkeypatch.setattr(
        omnigent_agent.Omnigent, "__init__", lambda self: setattr(self, "host_id", "h")
    )
    monkeypatch.delenv("OMNIGENT_PERMISSION_MODE", raising=False)
    og = omnigent_agent.Omnigent()
    og.create("claude", "t")
    assert "terminal_launch_args" not in bodies[-1]  # the harness default
    og.create("claude", "t", permission_mode="plan")
    assert bodies[-1]["terminal_launch_args"] == ["--permission-mode", "plan"]
    monkeypatch.setenv("OMNIGENT_PERMISSION_MODE", "acceptEdits")
    og.create("claude", "t")
    assert bodies[-1]["terminal_launch_args"] == ["--permission-mode", "acceptEdits"]
    # codex-native hands launch args to the codex CLI: Claude's word would fail it, so the default is
    # codex's own no-approvals, full-access flag whatever the variable says
    og.create("codex", "t")
    assert bodies[-1]["terminal_launch_args"] == ["--dangerously-bypass-approvals-and-sandbox"]
    og.create(
        "codex", "t", permission_mode="default"
    )  # an explicit stricter ask keeps codex's own stance
    assert "terminal_launch_args" not in bodies[-1]
    with pytest.raises(omnigent_agent.Fail, match="for claude sessions"):
        omnigent_agent.start(
            FakeOmnigent(), "codex", "t", "do X", tmp_path / "r.md", None, 0, permission_mode="plan"
        )


def test_create_asks_for_the_effort_given_and_the_brief_signs_with_the_one_taken(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bodies: list[dict] = []

    def call(self, method: str, path: str, body: dict | None = None) -> object:
        bodies.append(body or {})
        agents = [{"name": n, "id": a} for a, n in omnigent_agent.AGENTS.items()]
        return {
            "/v1/agents": agents,
            "/v1/agents?scope=user": [],
            "/v1/projects": {"data": []},
            "/v1/sessions": {"id": "s1"},
        }[path]

    monkeypatch.setattr(omnigent_agent.Omnigent, "call", call)
    monkeypatch.setattr(
        omnigent_agent.Omnigent, "__init__", lambda self: setattr(self, "host_id", "h")
    )
    og = omnigent_agent.Omnigent()
    og.create("claude", "t")
    assert "reasoning_effort" not in bodies[-1]  # the harness default
    og.create("claude", "t", effort="max")
    assert bodies[-1]["reasoning_effort"] == "max"
    assert "--effort" not in bodies[-1].get("terminal_launch_args", [])  # Omnigent adds it
    fake = FakeOmnigent()
    omnigent_agent.start(fake, "claude", "t", "do X", tmp_path / "r.md", None, 0, effort="max")
    assert fake.efforts == {"s0": "max"}
    assert (tmp_path / "r.md.brief").read_text().startswith("AGENT_SIGNATURE=cc/?/max\n")


def test_start_takes_the_effort_from_omnigent_effort_agent_unless_one_is_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OMNIGENT_MODEL_CODEX", raising=False)
    monkeypatch.setenv("OMNIGENT_EFFORT_CODEX", "high")
    fake = FakeOmnigent()
    omnigent_agent.start(fake, "codex", "a", "do X", tmp_path / "a.md", None, 0)
    omnigent_agent.start(fake, "codex", "b", "do X", tmp_path / "b.md", None, 0, effort="low")
    monkeypatch.delenv("OMNIGENT_EFFORT_CODEX")
    omnigent_agent.start(fake, "codex", "c", "do X", tmp_path / "c.md", None, 0)
    assert list(fake.efforts.values()) == ["high", "low", None]
    assert [(tmp_path / f"{n}.md.brief").read_text().split("\n")[0] for n in "abc"] == [
        "AGENT_SIGNATURE=codex/?/high",
        "AGENT_SIGNATURE=codex/?/low",
        "AGENT_SIGNATURE=codex/?/?",
    ]


def test_start_refuses_an_effort_for_antigravity_and_ignores_the_variables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_EFFORT_AGY", "max")  # no alias, so not a variable start reads
    og = FakeOmnigent()
    og.harnesses["agy"] = "antigravity-native"
    with pytest.raises(omnigent_agent.Fail, match="applies no effort"):
        omnigent_agent.start(og, "agy", "t", "x", tmp_path / "r.md", None, 0, effort="max")
    assert og.efforts == {}
    omnigent_agent.start(og, "agy", "t", "x", tmp_path / "r.md", None, 0)
    assert list(og.efforts.values()) == [None]


@pytest.mark.parametrize(
    ("agent", "harness", "effort", "refused"),
    [
        ("cloud", "acp:claude-cloud", "ultra", "runs only low, medium, high, xhigh, max"),
        ("cloud", "acp:claude-cloud", "max", None),
        ("cloud", "acp:claude-cloud", "low", None),
        ("agy", "antigravity-native", "high", "applies no effort"),
        ("claude", "", "ultra", "runs only low, medium, high, xhigh, max"),  # its launch drops it
        ("native", "claude-native", "none", "runs only low"),
        ("claude", "", "max", None),
        ("native", "claude-native", "low", None),
        ("codex", "", "ultra", None),
        ("gpt", "codex-native", "minimal", None),
    ],
)
def test_start_refuses_an_effort_the_child_would_not_run(
    tmp_path: Path, agent: str, harness: str, effort: str, refused: str | None
) -> None:
    og = FakeOmnigent()
    og.harnesses[agent] = harness
    if refused:
        with pytest.raises(omnigent_agent.Fail, match=refused):
            omnigent_agent.start(og, agent, "t", "x", tmp_path / "r.md", None, 0, effort=effort)
        assert og.efforts == {}
    else:
        omnigent_agent.start(og, agent, "t", "x", tmp_path / "r.md", None, 0, effort=effort)
        assert og.efforts == {"s0": effort}


def test_start_refuses_a_codex_effort_the_chosen_model_does_not_list(tmp_path: Path) -> None:
    og = FakeOmnigent()  # Codex launches such a level as another one, with no error
    og.levels = {None: ["low", "medium", "high"], "gpt-6.1-sol": ["low", "high", "xhigh"]}
    start = omnigent_agent.start
    with pytest.raises(omnigent_agent.Fail, match="the default model on codex runs only low, med"):
        start(og, "codex", "t", "x", tmp_path / "r.md", None, 0, effort="xhigh")
    with pytest.raises(
        omnigent_agent.Fail, match="gpt-6.1-sol on codex runs only low, high, xhigh"
    ):
        start(
            og, "codex", "t", "x", tmp_path / "r.md", None, 0, model="gpt-6.1-sol", effort="minimal"
        )
    assert og.efforts == {}
    start(og, "codex", "t", "x", tmp_path / "r.md", None, 0, model="gpt-6.1-sol", effort="xhigh")
    start(og, "codex", "t", "x", tmp_path / "r.md", None, 0, model="gpt-6.1-terra", effort="ultra")
    assert list(og.efforts.values()) == ["xhigh", "ultra"]  # terra's row names no levels
    og.fail.add("models")
    with pytest.raises(omnigent_agent.Fail, match="can't read codex's model list"):
        start(og, "codex", "t", "x", tmp_path / "r.md", None, 0, effort="high")
    start(og, "claude", "t", "x", tmp_path / "r.md", None, 0, effort="high")  # its levels are fixed


def test_effort_levels_reads_the_named_models_row_or_the_default_one() -> None:
    class Listing(FakeOmnigent):
        model_options = omnigent_agent.Omnigent.model_options
        effort_levels = omnigent_agent.Omnigent.effort_levels
        models = omnigent_agent.Omnigent.models

        def call(self, method: str, path: str, body: object = None) -> dict:
            assert (method, path) == ("GET", "/v1/hosts/h/harnesses/codex-native/model-options")
            levels = [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}]
            return {
                "models": [
                    {"id": "sol", "model": "gpt-6.1-sol", "supportedReasoningEfforts": levels},
                    {
                        "id": "terra",
                        "model": "gpt-6.1-terra",
                        "isDefault": True,
                        "supportedReasoningEfforts": levels[:1],
                    },
                    {"id": "luna"},
                    "stray",
                ],
                "routable_models": ["routed"],
            }

    og = Listing()
    og.harnesses["codex"] = "codex-native"
    assert og.effort_levels("codex", "gpt-6.1-sol") == ["low", "high"]
    assert og.effort_levels("codex") == ["low"]
    assert og.effort_levels("codex", "luna") == og.effort_levels("codex", "routed") == []
    assert og.models("codex") == ["sol", "gpt-6.1-sol", "terra", "gpt-6.1-terra", "luna", "routed"]


def test_start_names_the_variable_whose_effort_it_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_EFFORT_CLAUDE", "ultra")  # a Codex level copied over
    og = FakeOmnigent()
    with pytest.raises(
        omnigent_agent.Fail, match="^OMNIGENT_EFFORT_CLAUDE=ultra: claude runs only"
    ):
        omnigent_agent.start(og, "claude", "t", "x", tmp_path / "r.md", None, 0)
    assert og.efforts == {}


def test_the_brief_signs_with_the_effort_the_session_took_not_the_one_asked(tmp_path: Path) -> None:
    class Seeding(FakeOmnigent):  # the server seeds a session sent no effort from its agent's spec
        def create(self, agent: str, title: str, *rest: object) -> str:
            sid = super().create(agent, title, *rest)
            self.efforts[sid] = self.efforts[sid] or "high"
            return sid

    og = Seeding()
    omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
    assert (tmp_path / "r.md.brief").read_text().startswith("AGENT_SIGNATURE=cc/?/high\n")


def test_the_cli_passes_effort_over_the_variable_and_refuses_one_with_no_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent(reply="done")
    og.harnesses["agy"] = "antigravity-native"
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "brief.md").write_text("do X")

    def start(agent: str, report: str, *flags: str) -> int:
        return omnigent_agent.main(
            ["start", agent, report, "brief.md", report, "--confirm-seconds", "0", *flags]
        )

    assert start("claude", "a.md", "--effort", "max") == 0
    monkeypatch.setenv("OMNIGENT_EFFORT_CLAUDE", "low")
    assert start("claude", "b.md", "--effort", "xhigh") == 0
    assert list(og.efforts.values()) == ["max", "xhigh"]
    assert (tmp_path / "a.md.brief").read_text().startswith("AGENT_SIGNATURE=cc/?/max\n")
    sessions = dict(og.sessions)
    assert start("agy", "c.md", "--effort", "max") != 0
    assert og.sessions == sessions and "applies no effort" in capsys.readouterr().err


def test_create_starts_a_claude_worker_or_verifier_lean_with_what_the_project_names_beyond(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bodies: list[dict] = []

    def call(self, method: str, path: str, body: dict | None = None) -> object:
        bodies.append(body or {})
        agents = [{"name": n, "id": a} for a, n in omnigent_agent.AGENTS.items()]
        return {
            "/v1/agents": agents,
            "/v1/agents?scope=user": [],
            "/v1/projects": {"data": []},
            "/v1/sessions": {"id": "s1"},
        }[path]

    monkeypatch.setattr(omnigent_agent.Omnigent, "call", call)
    monkeypatch.setattr(
        omnigent_agent.Omnigent, "__init__", lambda self: setattr(self, "host_id", "h")
    )
    monkeypatch.delenv("OMNIGENT_PERMISSION_MODE", raising=False)
    monkeypatch.setattr(omnigent_agent.Path, "home", lambda: tmp_path / "home")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "sub").mkdir()
    ws = str(repo / "sub")  # a workspace below the root reads the root's project files
    tools = "Bash,Read,Edit,Write,Agent,Skill,ToolSearch,WebFetch,WebSearch"  # what the roles call
    lean = ["--strict-mcp-config", "--tools", tools]
    og = omnigent_agent.Omnigent()
    for role in ("worker", "verifier"):
        og.create("claude", "t", workspace=ws, role=f"/x/engineering-loop/{role}.md")
        assert bodies[-1]["terminal_launch_args"] == lean
    og.create("claude", "t", workspace=ws, role="/x/worker.md", permission_mode="plan")
    assert bodies[-1]["terminal_launch_args"] == ["--permission-mode", "plan", *lean]
    for role in ("/x/lead.md", None):  # a lead and a plain child start as before
        og.create("claude", "t", workspace=ws, role=role)
        assert "terminal_launch_args" not in bodies[-1]
    og.create(
        "codex", "t", workspace=ws, role="/x/worker.md"
    )  # codex's own flag, not claude's lean ones
    assert bodies[-1]["terminal_launch_args"] == ["--dangerously-bypass-approvals-and-sandbox"]
    og.create("claude", "t", workspace=str(tmp_path), role="/x/worker.md")  # outside a checkout
    assert bodies[-1]["terminal_launch_args"] == lean
    (repo / "docs" / "agents").mkdir(parents=True)
    (repo / "docs" / "agents" / "loop.md").write_text(
        "# Loop\n\nVerifier tools: full\nWorker tools: NotebookEdit, docs, mail, tracker\n"
    )
    docs = {"command": "docs-mcp"}
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"docs": docs}}))
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {"mail": {"url": "u"}, "calendar": {"url": "c"}},
                "projects": {str(repo.resolve()): {"mcpServers": {"tracker": {"url": "t"}}}},
            }
        )
    )
    og.create("claude", "t", workspace=ws, role="/x/verifier.md")
    assert "terminal_launch_args" not in bodies[-1]  # the role keeps every tool and server
    og.create("claude", "t", workspace=ws, role="/x/worker.md")
    assert bodies[-1]["terminal_launch_args"] == [
        "--strict-mcp-config",
        "--tools",
        tools + ",NotebookEdit",
        "--mcp-config",
        json.dumps({"mcpServers": {"docs": docs, "mail": {"url": "u"}, "tracker": {"url": "t"}}}),
    ]


def test_create_starts_a_claude_lead_with_the_project_s_autocompact_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bodies: list[dict] = []

    def call(self, method: str, path: str, body: dict | None = None) -> object:
        bodies.append(body or {})
        agents = [{"name": n, "id": a} for a, n in omnigent_agent.AGENTS.items()]
        return {
            "/v1/agents": agents,
            "/v1/agents?scope=user": [],
            "/v1/projects": {"data": []},
            "/v1/sessions": {"id": "s1"},
        }[path]

    monkeypatch.setattr(omnigent_agent.Omnigent, "call", call)
    monkeypatch.setattr(
        omnigent_agent.Omnigent, "__init__", lambda self: setattr(self, "host_id", "h")
    )
    monkeypatch.setattr(omnigent_agent, "parent_id", lambda: "")
    monkeypatch.setattr(omnigent_agent.Path, "home", lambda: tmp_path / "home")
    monkeypatch.delenv("OMNIGENT_PERMISSION_MODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    ws = str(repo)
    og = omnigent_agent.Omnigent()
    og.create("claude", "t", workspace=ws, top_level=True)
    assert "terminal_launch_args" not in bodies[-1]  # no line: the lead starts as before
    (repo / "docs" / "agents").mkdir(parents=True)
    loop = repo / "docs" / "agents" / "loop.md"
    loop.write_text("# Loop\n\nLead autocompact: 150k\n")
    og.create("claude", "t", workspace=ws, top_level=True)
    assert bodies[-1]["terminal_launch_args"] == ["--autocompact", "150k"]
    assert "outranks" not in capsys.readouterr().err
    og.create("claude", "t", workspace=ws, top_level=True, permission_mode="plan")
    assert bodies[-1]["terminal_launch_args"] == [
        "--permission-mode",
        "plan",
        "--autocompact",
        "150k",
    ]
    og.create("claude", "t", workspace=ws)  # a child keeps the user's window
    assert "terminal_launch_args" not in bodies[-1]
    og.create("codex", "t", workspace=ws, top_level=True)  # codex's own flag, not claude's
    assert bodies[-1]["terminal_launch_args"] == ["--dangerously-bypass-approvals-and-sandbox"]
    (tmp_path / "home" / ".claude").mkdir(parents=True)
    (tmp_path / "home" / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "330000"}})
    )
    og.create("claude", "t", workspace=ws, top_level=True)
    assert bodies[-1]["terminal_launch_args"] == ["--autocompact", "150k"]
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW=330000 outranks" in capsys.readouterr().err
    (tmp_path / "home" / ".claude" / "settings.json").write_text("{}")
    # the caller's env never reaches the lead, which gets the runner's
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "250000")
    og.create("claude", "t", workspace=ws, top_level=True)
    assert bodies[-1]["terminal_launch_args"] == ["--autocompact", "150k"]
    assert "outranks" not in capsys.readouterr().err
    for size, ok in (
        ("150000", True),
        ("200", True),
        ("1M", True),
        ("0.15M", True),
        ("150.5k", True),
        ("99k", False),
        ("1.1M", False),
        ("1001k", False),
        ("lots", False),
    ):
        loop.write_text(f"Lead autocompact: {size}\n")
        if ok:
            og.create("claude", "t", workspace=ws, top_level=True)
            assert bodies[-1]["terminal_launch_args"] == ["--autocompact", size]
        else:
            with pytest.raises(omnigent_agent.Fail, match="not a size from 100k to 1M"):
                og.create("claude", "t", workspace=ws, top_level=True)


@pytest.mark.parametrize(
    ("names", "name", "picked"),
    [
        (["Voice-note", "voice_note"], "voice_note", 1),  # the exact name wins over a loose one
        (["工程", "日本語"], "日本語", 1),  # letters in any script count
        (["café"], "caf", None),
        (["!!"], "--", None),  # nothing left to compare matches nothing
        (["Voice note"], "voice_note", 0),
    ],
)
def test_a_project_matches_its_exact_name_first_then_loosely(
    monkeypatch: pytest.MonkeyPatch, names: list[str], name: str, picked: int | None
) -> None:
    projects = [{"name": n, "id": f"p{i}"} for i, n in enumerate(names)]
    monkeypatch.setattr(omnigent_agent.Omnigent, "call", lambda *a: {"data": projects})
    found = object.__new__(omnigent_agent.Omnigent).project(name)
    assert found == (None if picked is None else projects[picked])


def test_two_loose_project_matches_fail_rather_than_pick_by_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects = [{"name": "Voice-note", "id": "p0"}, {"name": "Voice note", "id": "p1"}]
    monkeypatch.setattr(omnigent_agent.Omnigent, "call", lambda *a: {"data": projects})
    with pytest.raises(omnigent_agent.Fail, match="OMNIGENT_PROJECT"):
        object.__new__(omnigent_agent.Omnigent).project("voice_note")


def test_the_model_is_the_flag_else_the_agents_environment_variable_else_the_harness_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies: list[dict] = []

    def call(self, method: str, path: str, body: dict | None = None) -> object:
        if body is not None:
            bodies.append(body)
        return {
            "/v1/agents": [{"name": "codex-native-ui", "id": "a1"}],
            "/v1/agents?scope=user": [],
            "/v1/projects": [],
        }.get(path, {"id": "s1"})

    monkeypatch.setattr(omnigent_agent.Omnigent, "call", call)
    monkeypatch.setattr(
        omnigent_agent.Omnigent, "__init__", lambda self: setattr(self, "host_id", "h")
    )
    monkeypatch.delenv("OMNIGENT_MODEL_CODEX", raising=False)
    omnigent_agent.Omnigent().create("codex", "t")
    monkeypatch.setenv("OMNIGENT_MODEL_CODEX", "from-env")
    monkeypatch.setenv("OMNIGENT_MODEL_CLAUDE", "not-codex")
    omnigent_agent.Omnigent().create("codex", "t")
    omnigent_agent.Omnigent().create("codex", "t", "from-flag")
    assert [body.get("model_override") for body in bodies] == [None, "from-env", "from-flag"]


@pytest.mark.parametrize(
    ("agent", "flag", "env", "expected"),
    [
        ("claude-cloud", None, None, "sonnet"),  # the server refuses acp without a model (#896)
        ("claude-cloud", None, "from-env", "from-env"),
        ("claude-cloud", "from-flag", "from-env", "from-flag"),
        ("claude", None, None, None),
        ("codex", None, None, None),
        ("antigravity", None, "from-env", None),  # only the aliases and claude-cloud read one
    ],
)
def test_a_cloud_session_always_names_a_model_and_the_others_only_when_given_one(
    monkeypatch: pytest.MonkeyPatch,
    agent: str,
    flag: str | None,
    env: str | None,
    expected: str | None,
) -> None:
    bodies: list[dict] = []

    def call(self, method: str, path: str, body: dict | None = None) -> object:
        if body is not None:
            bodies.append(body)
        return {
            "/v1/agents": [{"name": n, "id": n} for n in ("claude-native-ui", "codex-native-ui")],
            "/v1/agents?scope=user": [
                {"name": n, "id": n} for n in ("claude-cloud", "antigravity")
            ],
            "/v1/projects": [],
        }.get(path, {"id": "s1"})

    monkeypatch.setattr(omnigent_agent.Omnigent, "call", call)
    monkeypatch.setattr(
        omnigent_agent.Omnigent, "__init__", lambda self: setattr(self, "host_id", "h")
    )
    for name in ("CLAUDE", "CODEX", "CLAUDE_CLOUD"):
        monkeypatch.delenv(f"OMNIGENT_MODEL_{name}", raising=False)
    if env:
        monkeypatch.setenv(f"OMNIGENT_MODEL_{agent.upper().replace('-', '_')}", env)
    omnigent_agent.Omnigent().create(agent, "t", flag)
    assert bodies[-1].get("model_override") == expected


def test_start_claude_cloud_checks_its_default_model_and_passes_it_unchecked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An acp harness has no host model list, so the default goes on as the env one would."""
    monkeypatch.delenv("OMNIGENT_MODEL_CLAUDE_CLOUD", raising=False)
    og = FakeOmnigent()
    og.harnesses = {"claude-cloud": "acp:claude-cloud"}
    omnigent_agent.start(og, "claude-cloud", "t", "do X", tmp_path / "r.md", None, 0, pr=7)
    assert "default sonnet: the host has no model list for claude-cloud" in capsys.readouterr().err
    assert list(og.overrides.values()) == ["sonnet"]


@pytest.mark.parametrize(
    ("origin", "name"),
    [
        ("git@github.com:example/shop.git", "shop"),
        ("https://github.com/example/shop", "shop"),
        ("https://github.com/example/shop.git/", "shop"),
        (None, "checkout"),
    ],
)
def test_the_project_defaults_to_the_repo_name(
    origin: str | None, name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", origin], check=True)
    monkeypatch.chdir(repo)
    assert omnigent_agent.repo_name() == name


def test_wait_returns_question_instead_of_failing(tmp_path: Path) -> None:
    og = FakeOmnigent(reply="r.md.question")
    report = tmp_path / "r.md"
    og.send(og.create("claude", "t"), "brief")
    omnigent_agent.question_of(report).write_text("which base?")
    with pytest.raises(omnigent_agent.Asked, match=r"r\.md\.question"):
        omnigent_agent.wait(og, "s0", report, timeout=5)


def test_send_answers_live_session_and_clears_question(tmp_path: Path) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "t")
    report = tmp_path / "r.md"
    omnigent_agent.question_of(report).write_text("which base?")
    omnigent_agent.send(og, sid, "use main", report, 0)
    assert not omnigent_agent.question_of(report).exists()
    assert omnigent_agent.text_of(og.sessions[sid][-1]) == "use main"


def test_send_and_relay_post_an_expiry_and_the_senders_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted: list[dict] = []

    class Posting(FakeOmnigent):
        send = omnigent_agent.Omnigent.send  # type: ignore[assignment]

        def call(self, method: str, path: str, body: dict) -> dict:
            posted.append(body["data"])
            return {}

    og = Posting()
    sid = og.create("claude", "t")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.setenv("OMNIGENT_SESSION_ID", "sender-1")
    (tmp_path / "m.md").write_text("hi")
    monkeypatch.chdir(tmp_path)
    for argv in (
        ["relay", sid, "m.md"],
        ["relay", sid, "m.md", "--expires", "0"],
        ["send", sid, "m.md", "r.md", "--timeout", "0"],
        ["send", sid, "m.md", "r.md", "--timeout", "0", "--expires", "0"],
    ):
        posted.clear()
        before = time.time()
        omnigent_agent.main([*argv, "--confirm-seconds", "0"])
        assert posted, argv
        assert posted[-1]["sender_session_id"] == "sender-1"
        if "--expires" in argv:
            assert "expires_at" not in posted[-1]
        else:
            assert before + 29 * 60 < posted[-1]["expires_at"] < time.time() + 31 * 60


def test_a_plain_send_posts_neither_an_expiry_nor_a_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the send and relay commands tag a message: start's first brief and supervise's lines
    (run outside a session, so own_id() falls back to the parent) are never dropped."""
    posted: list[dict] = []

    class Posting(FakeOmnigent):
        send = omnigent_agent.Omnigent.send  # type: ignore[assignment]
        expires = omnigent_agent.Omnigent.expires

        def call(self, method: str, path: str, body: dict) -> dict:
            posted.append(body["data"])
            return {}

    monkeypatch.setenv("OMNIGENT_SESSION_ID", "sender-1")
    og = Posting()
    og.send(og.create("claude", "t"), "hi")
    assert "expires_at" not in posted[-1]
    assert "sender_session_id" not in posted[-1]


def reaped(og: FakeOmnigent, reply: dict, online_after: int = 1) -> list[str]:
    """Make every session reaped until a retry_session answering ``reply``, then online after
    ``online_after`` more info() reads; returns the retried session ids."""
    retried: list[str] = []
    polls = [0]
    info = og.info

    def reaped_info(s: str) -> dict:
        polls[0] += 1
        return {**info(s), "runner_online": bool(retried) and polls[0] > online_after}

    def retry(s: str) -> dict:
        retried.append(s)
        polls[0] = 0
        return reply

    og.info, og.retry = reaped_info, retry  # type: ignore[method-assign,attr-defined]
    return retried


def test_send_revives_a_reaped_runner_then_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(omnigent_agent, "POLL_SECONDS", 0)
    og = FakeOmnigent()
    sid = og.create("claude", "t")
    retried = reaped(og, {"queued": False, "recovered": True, "recovery": "runner_relaunched"}, 2)
    omnigent_agent.send(og, sid, "use main", tmp_path / "r.md", 0)
    assert retried == [sid]
    assert omnigent_agent.text_of(og.sessions[sid][-1]) == "use main"


def test_send_exits_gone_naming_the_reply_when_the_revive_fails(tmp_path: Path) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "t")
    reaped(og, {"queued": False, "recovered": False, "recovery": "no_runner"})
    with pytest.raises(omnigent_agent.Gone, match='revive failed: .*"no_runner"'):
        omnigent_agent.send(og, sid, "use main", tmp_path / "r.md", 0)
    assert og.sessions[sid] == [{"type": "resource_event"}]


def test_a_revive_whose_runner_never_comes_online_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(omnigent_agent, "POLL_SECONDS", 0)
    og = FakeOmnigent()
    sid = og.create("claude", "t")
    reaped(og, {"recovered": True, "recovery": "runner_relaunched"}, online_after=10**9)
    with pytest.raises(omnigent_agent.Gone, match="no runner online after 0s"):
        omnigent_agent.revive(og, sid, seconds=0)


def test_send_fails_when_answer_never_arrives(tmp_path: Path) -> None:
    og = FakeOmnigent(deaf={0})
    sid = og.create("claude", "t")
    with pytest.raises(omnigent_agent.Gone, match="neither pending nor in history"):
        omnigent_agent.send(og, sid, "use main", tmp_path / "r.md", 0)


def test_send_to_a_busy_session_waits_while_the_harness_holds_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent(hold=5)  # pending longer than confirm_seconds: a long tool call
    sid = og.create("claude", "t")
    report = tmp_path / "r.md"
    omnigent_agent.question_of(report).write_text("which base?")
    omnigent_agent.send(og, sid, "use main", report, 0)
    assert "queued" in capsys.readouterr().err
    assert not omnigent_agent.question_of(report).exists()
    assert omnigent_agent.text_of(og.sessions[sid][-1]) == "use main"


def test_a_retried_send_never_delivers_twice(tmp_path: Path) -> None:
    og = FakeOmnigent(hold=3)
    sid = og.create("claude", "t")
    report = tmp_path / "r.md"
    omnigent_agent.question_of(report).write_text("which base?")
    og.send(sid, "use main")  # the first send, still pending when it gave up
    omnigent_agent.send(og, sid, "use main", report, 0)  # retry while pending
    omnigent_agent.question_of(report).write_text(
        "which base?"
    )  # as if the first send's unlink never ran
    og.sessions[sid][-1]["created_at"] = time.time() + 1  # the answer landed after the question
    omnigent_agent.send(og, sid, "use main", report, 0)  # retry after it landed
    assert og.posts == 1
    assert [omnigent_agent.text_of(i) for i in og.sessions[sid][1:]] == ["use main"]
    assert not omnigent_agent.question_of(report).exists()


def test_an_answer_inside_an_older_message_is_still_sent(tmp_path: Path) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "t")
    report = tmp_path / "r.md"
    omnigent_agent.question_of(report).write_text("which base?")
    og.land(sid, "Do not use main; use the release branch")
    og.sessions[sid][-1]["created_at"] = time.time() + 1
    omnigent_agent.send(og, sid, "use main", report, 0)
    assert omnigent_agent.text_of(og.sessions[sid][-1]) == "use main"


def test_a_retry_sends_nothing_when_the_first_copy_lands_between_its_reads(
    tmp_path: Path,
) -> None:
    og = FakeOmnigent(hold=1)  # lands at the retry's first info() call
    sid = og.create("claude", "t")
    og.send(sid, "use main")  # the first send; its process already removed the question
    omnigent_agent.send(og, sid, "use main", tmp_path / "r.md", 0)
    assert og.posts == 1


def test_a_retry_keeps_its_question_time_when_the_first_sender_removes_it(
    tmp_path: Path,
) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "t")
    report = tmp_path / "r.md"
    question = omnigent_agent.question_of(report)
    question.write_text("which base?")
    og.land(sid, "use main")
    og.sessions[sid][-1]["created_at"] = time.time() + 1
    info = og.info
    og.info = lambda s: (question.unlink(missing_ok=True), info(s))[1]  # type: ignore[method-assign]
    omnigent_agent.send(og, sid, "use main", report, 0)
    assert og.posts == 0


def test_a_different_message_containing_the_answer_does_not_confirm_it(
    tmp_path: Path,
) -> None:
    og = FakeOmnigent(deaf={0})
    sid = og.create("claude", "t")
    items = og.items
    og.items = (
        lambda s: (  # type: ignore[method-assign]
            og.posts
            and not og.sessions[s][1:]
            and og.land(s, "Do not use main; use the release branch"),
            items(s),
        )[1]
    )
    with pytest.raises(omnigent_agent.Gone):
        omnigent_agent.send(og, sid, "use main", tmp_path / "r.md", 0)


def test_send_ignores_the_report_of_the_turn_before(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(reply="done")
    sid = og.create("claude", "t")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    report = tmp_path / "r.md"
    report.write_text("old findings\nSATISFIED: yes\n")
    os.utime(report, (1, 1))
    (tmp_path / "a.md").write_text("fix C1")
    argv = ["send", sid, "a.md", "r.md", "--verdict"]
    assert omnigent_agent.main(argv) == 1  # the turn ended and only the stale report is there
    assert "without a verdict" in capsys.readouterr().err

    info = og.info
    og.info = lambda s: (report.write_text("new\nSATISFIED: yes\n"), info(s))[1]  # type: ignore[method-assign]
    assert omnigent_agent.main(argv) == 0
    assert capsys.readouterr().out.strip() == str(report)


def test_a_rerun_after_the_answer_landed_returns_the_report_it_produced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(reply="done")
    sid = og.create("claude", "t")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    report = tmp_path / "r.md"
    question = omnigent_agent.question_of(report)
    question.write_text("which base?")
    os.utime(question, (1, 1))
    monkeypatch.setenv("OMNIGENT_SESSION_ID", "cos")
    og.send(sid, "[relayed by cos]\nuse main")  # the first send died before it removed the question
    report.write_text("done\nSATISFIED: yes\n")  # and the child answered with its report
    (tmp_path / "a.md").write_text("use main")
    assert omnigent_agent.main(["send", sid, "a.md", "r.md", "--verdict"]) == 0
    assert og.posts == 1


def test_a_follow_up_is_marked_as_posted_so_owner_said_skips_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(reply="done")
    sid = og.create("claude", "t")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.setenv("OMNIGENT_SESSION_ID", "cos")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.md").write_text("yes, force-push the branch")
    (tmp_path / "r.md").write_text("")
    os.utime(tmp_path / "r.md", (1, 1))
    omnigent_agent.main(["send", sid, "a.md", "r.md", "--timeout", "0"])
    sent = [omnigent_agent.text_of(i) for i in og.sessions[sid] if i.get("role") == "user"]
    follow_up = next(t for t in sent if "force-push" in t)
    assert follow_up == "[relayed by cos]\nyes, force-push the branch"
    assert omnigent_agent.agent_posted(follow_up)  # owner-said rejects a quote found only here


def test_a_follow_up_sent_as_the_turn_before_ends_is_waited_for(tmp_path: Path) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "t")
    report = tmp_path / "r.md"
    og.sessions[sid].append({"type": "message", "role": "assistant", "content": []})
    # the turn before ends, the follow-up lands in the idle gap, then its own turn runs
    script = iter(["running", "idle", "idle", "running", "running"])

    def info(s: str) -> dict:
        status = next(script, "idle")
        if status == "idle" and og.sessions[s][-1].get("role") != "user":
            og.land(s, "fix C1")
        if (
            status == "running"
            and og.sessions[s][-1].get("role") == "user"
            and not next(iter([report.exists()]))
        ):
            report.write_text("done")
        return {"status": status, "pending_elicitations": []}

    og.info = info  # type: ignore[method-assign]
    assert omnigent_agent.wait(og, sid, report, timeout=5) == str(report)


def test_same_family_verifier_needs_break_glass(
    tmp_path: Path,
    role_file: Callable[[str], str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brief = tmp_path / "b.md"
    brief.write_text("review")
    argv = [
        "run",
        "claude",
        "t",
        str(brief),
        str(tmp_path / "r.md"),
        "--role",
        role_file("verifier.md"),
        "--author",
        "claude",
    ]
    monkeypatch.setattr(omnigent_agent, "Omnigent", FakeOmnigent)
    monkeypatch.chdir(tmp_path)
    assert omnigent_agent.main(argv) == 1
    assert "--same-family" in capsys.readouterr().err

    og = FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    assert omnigent_agent.main(["start", *argv[1:], "--same-family"]) == 0
    brief = (tmp_path / "r.md.brief").read_text()
    assert (
        brief.endswith("\n\nreview") and "same-family" not in brief.lower()
    )  # who wrote it, and why, stay out


def test_report_outside_the_workspace_is_refused_before_a_session_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    (tmp_path / "work").mkdir()
    monkeypatch.chdir(tmp_path / "work")
    (tmp_path / "b.md").write_text("do X")
    argv = ["run", "claude", "t", str(tmp_path / "b.md"), str(tmp_path / "r.md")]
    assert omnigent_agent.main(argv) == 1
    assert "outside the child's workspace" in capsys.readouterr().err
    assert og.sessions == {}


def test_workspace_names_the_child_s_directory_and_bounds_its_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A lead for a change across two repos works in their parent; its report sits in the
    # starter's own tmp/loop/, inside both workspaces.
    og = FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    (tmp_path / "a" / "tmp").mkdir(parents=True)
    (tmp_path / "b").mkdir()
    monkeypatch.chdir(tmp_path / "a")
    (tmp_path / "brief.md").write_text("do X")
    argv = ["start", "claude", "t", str(tmp_path / "brief.md"), "tmp/lead.md", "--top-level"]
    assert omnigent_agent.main([*argv, "--workspace", ".."]) == 0
    assert og.workspaces == [str(tmp_path.resolve())]
    outside = [
        "start",
        "claude",
        "t2",
        str(tmp_path / "brief.md"),
        "tmp/x.md",
        "--workspace",
        "../b",
    ]
    assert omnigent_agent.main(outside) == 1
    assert f"outside the child's workspace {(tmp_path / 'b').resolve()}" in capsys.readouterr().err
    assert len(og.sessions) == 1


def test_send_bounds_the_report_by_the_session_s_own_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A follow-up sent from another repo's checkout: the child works where it was started.
    og = FakeOmnigent(reply="ok")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    (tmp_path / "child").mkdir()
    (tmp_path / "elsewhere").mkdir()
    sid = og.create("claude", "t", workspace=str(tmp_path / "child"))
    monkeypatch.chdir(tmp_path / "elsewhere")
    (tmp_path / "m.md").write_text("fix it")
    report = tmp_path / "child" / "r.md"
    monkeypatch.setattr(
        omnigent_agent, "wait", lambda *a, **k: (report.write_text("done"), str(report))[1]
    )
    assert omnigent_agent.main(["send", sid, str(tmp_path / "m.md"), str(report)]) == 0
    outside = tmp_path / "elsewhere" / "r.md"
    assert omnigent_agent.main(["send", sid, str(tmp_path / "m.md"), str(outside)]) == 1
    assert (
        f"outside the child's workspace {(tmp_path / 'child').resolve()}" in capsys.readouterr().err
    )


def test_send_confirms_answer_on_a_full_rolling_page(tmp_path: Path) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "t")
    og.sessions[sid] = [{"id": f"old{n}", "type": "function_call"} for n in range(100)]
    newest = og.items
    og.items = lambda s: newest(s)[-100:]  # type: ignore[method-assign]
    report = tmp_path / "r.md"
    omnigent_agent.question_of(report).write_text("which base?")
    omnigent_agent.send(og, sid, "use main", report, 0)
    assert not omnigent_agent.question_of(report).exists()


def test_undelivered_answer_keeps_the_question(tmp_path: Path) -> None:
    og = FakeOmnigent(deaf={0})
    sid = og.create("claude", "t")
    report = tmp_path / "r.md"
    omnigent_agent.question_of(report).write_text("which base?")
    with pytest.raises(omnigent_agent.Gone):
        omnigent_agent.send(og, sid, "use main", report, 0)
    assert omnigent_agent.question_of(report).read_text() == "which base?"


def test_retry_after_surviving_child_uses_a_new_title(tmp_path: Path) -> None:
    og = FakeOmnigent(deaf={0})
    titles: list[str] = []
    create = og.create
    og.create = lambda agent, title, *_: titles.append(title) or create(agent, title)  # type: ignore[method-assign]
    og.fail.add("archive")
    omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
    assert titles == ["t", "t-r2"]


def test_outside_a_session_the_server_is_named_or_the_one_login_never_config_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, tokens = tmp_path / "config.yaml", tmp_path / "auth_tokens.json"
    config.write_text("host:\n  host_id: host_1\nlocal_server_port: 6767\n")
    monkeypatch.setattr(omnigent_agent, "CONFIG", config)
    monkeypatch.setattr(omnigent_agent, "AUTH_TOKENS", tokens)
    monkeypatch.delenv("RUNNER_SERVER_URL", raising=False)
    tokens.write_text('{"https://omnigent.example/": {}}')
    assert omnigent_agent.Omnigent().base == "https://omnigent.example"
    tokens.write_text('{"https://a.example": {}, "https://b.example": {}}')  # a guess: refuse
    config.write_text("server: https://named.example\nhost:\n  host_id: host_1\n")
    with pytest.raises(omnigent_agent.Fail, match="which Omnigent server"):
        omnigent_agent.Omnigent()  # config.yaml's server: is a default, not an answer
    assert omnigent_agent.Omnigent("https://a.example").base == "https://a.example"


def test_a_prefixed_host_id_in_config_is_held_bare_so_this_host_s_sessions_are_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("host:\n  host_id: host_ab12\n")
    monkeypatch.setattr(omnigent_agent, "CONFIG", config)
    monkeypatch.setenv("RUNNER_SERVER_URL", "https://work.example")
    og = omnigent_agent.Omnigent()
    assert og.host_id == "ab12"  # the server's form (#1133)
    assert not omnigent_agent.elsewhere(og, {"host_id": "ab12"})
    assert omnigent_agent.elsewhere(og, {"host_id": "cd34"})
    config.write_text("host:\n  host_id: ab12\n")
    assert omnigent_agent.Omnigent().host_id == "ab12"  # a bare one stays as it is


def test_a_session_acts_on_its_own_server_over_the_config_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, tokens = tmp_path / "config.yaml", tmp_path / "auth_tokens.json"
    config.write_text("server: https://personal.example\nhost:\n  host_id: host_1\n")
    tokens.write_text('{"https://login.example": {}}')  # a lone login loses to the session too
    monkeypatch.setattr(omnigent_agent, "CONFIG", config)
    monkeypatch.setattr(omnigent_agent, "AUTH_TOKENS", tokens)
    monkeypatch.setenv("RUNNER_SERVER_URL", "https://work.example/")
    assert omnigent_agent.Omnigent().base == "https://work.example"
    assert omnigent_agent.Omnigent("https://personal.example").base == "https://personal.example"


def test_a_get_that_times_out_once_is_retried_and_wait_returns_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import json

    report = tmp_path / "r.md"
    answers = iter(
        [
            TimeoutError("timed out"),
            {"status": "running", "pending_elicitations": []},
            {"data": [{"type": "message", "role": "user", "content": []}]},
        ]
    )

    def urlopen(req: object, timeout: float) -> io.BytesIO:
        answer = next(answers, None)
        if isinstance(answer, Exception):
            raise answer
        report.write_text("done")  # written while the read was retried
        return io.BytesIO(json.dumps(answer or {}).encode())

    monkeypatch.setattr(omnigent_agent, "urlopen", urlopen)
    monkeypatch.setattr(omnigent_agent, "RETRY_SECONDS", (0, 0))
    og = object.__new__(omnigent_agent.Omnigent)
    og.base, og._token = "http://omnigent.test", "synthetic"
    assert omnigent_agent.wait(og, "s0", report, timeout=5) == str(report)


def test_a_post_that_times_out_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    posts = []

    def urlopen(req: object, timeout: float) -> None:
        posts.append(req)
        raise TimeoutError("timed out")

    monkeypatch.setattr(omnigent_agent, "urlopen", urlopen)
    monkeypatch.setattr(omnigent_agent, "RETRY_SECONDS", (0, 0))
    og = object.__new__(omnigent_agent.Omnigent)
    og.base, og._token = "http://omnigent.test", "synthetic"
    with pytest.raises(omnigent_agent.Fail, match="POST /x: TimeoutError"):
        og.call("POST", "/x", {})
    assert len(posts) == 1


@pytest.mark.parametrize(
    "error",
    [
        ssl.SSLEOFError(8, "EOF occurred in violation of protocol"),
        URLError(ConnectionRefusedError(61, "Connection refused")),
        HTTPError("http://omnigent.test/x", 503, "Service Unavailable", Message(), None),
    ],
)
def test_a_get_retries_a_server_restarting_and_a_post_does_not(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    calls: list[str] = []

    def urlopen(req: Request, timeout: float) -> io.BytesIO:
        calls.append(req.get_method())
        if len(calls) == 1:
            raise error
        return io.BytesIO(b'{"ok": true}')

    monkeypatch.setattr(omnigent_agent, "urlopen", urlopen)
    monkeypatch.setattr(omnigent_agent, "RETRY_SECONDS", (0, 0))
    og = object.__new__(omnigent_agent.Omnigent)
    og.base, og._token = "http://omnigent.test", "synthetic"
    assert og.call("GET", "/x") == {"ok": True}
    calls.clear()
    with pytest.raises(omnigent_agent.Fail):
        og.call("POST", "/x", {})
    assert calls == ["POST"]


def test_a_get_retries_a_name_resolution_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def urlopen(req: Request, timeout: float) -> io.BytesIO:
        calls.append(req.get_method())
        if len(calls) == 1:
            raise URLError(socket.gaierror(-3, "Temporary failure in name resolution"))
        return io.BytesIO(b'{"ok": true}')

    monkeypatch.setattr(omnigent_agent, "urlopen", urlopen)
    monkeypatch.setattr(omnigent_agent, "RETRY_SECONDS", (0, 0))
    og = object.__new__(omnigent_agent.Omnigent)
    og.base, og._token = "http://omnigent.test", "synthetic"
    assert og.call("GET", "/x") == {"ok": True}
    assert calls == ["GET", "GET"]


def test_send_exits_7_when_the_post_went_out_and_the_next_read_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "lead")
    info = og.info
    reads = [0]

    def flaky(s: str) -> dict:
        reads[0] += 1
        if og.posts and reads[0] > 1:  # the poll after the post
            raise omnigent_agent.Fail("GET info: Temporary failure in name resolution")
        return info(s)

    og.info = flaky  # type: ignore[method-assign]
    assert relay(og, sid, tmp_path, monkeypatch) == omnigent_agent.SENT_UNCONFIRMED == 7
    err = capsys.readouterr().err
    assert "the message was posted" in err and "`send` again is safe" in err
    assert og.posts == 1
    og.info = info  # type: ignore[method-assign]
    assert relay(og, sid, tmp_path, monkeypatch) == 0  # the rerun finds the copy
    assert og.posts == 1


def test_send_with_a_report_exits_7_when_the_confirming_history_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "lead")
    items = og.items
    seen = [0]

    def flaky(s: str) -> list[dict]:
        seen[0] += 1
        if og.posts:
            raise omnigent_agent.Fail("GET items: ConnectionResetError")
        return items(s)

    og.items = flaky  # type: ignore[method-assign]
    with pytest.raises(omnigent_agent.SentUnconfirmed) as caught:
        omnigent_agent.send(og, sid, "use main", tmp_path / "r.md", 0)
    assert caught.value.code == 7
    assert og.posts == 1


def test_send_keeps_exit_1_when_the_post_itself_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "lead")
    og.fail.add("send")
    assert relay(og, sid, tmp_path, monkeypatch) == 1
    assert og.posts == 0


def test_send_keeps_exit_1_when_a_read_before_the_post_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "lead")

    def down(s: str) -> list[dict]:
        raise omnigent_agent.Fail("GET items: Temporary failure in name resolution")

    og.items = down  # type: ignore[method-assign]
    assert relay(og, sid, tmp_path, monkeypatch) == 1
    assert og.posts == 0


def test_a_broadcast_target_posted_but_unconfirmed_is_no_failure() -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "lead")
    items = og.items
    og.items = lambda s: (_ for _ in ()).throw(omnigent_agent.Fail("x")) if og.posts else items(s)  # type: ignore[method-assign]
    assert omnigent_agent.broadcast(og, [(sid, "lead")], "hi", 0, "cos") is False


def test_a_get_answered_404_fails_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def urlopen(req: Request, timeout: float) -> None:
        calls.append(req)
        raise HTTPError("http://omnigent.test/x", 404, "Not Found", Message(), None)

    monkeypatch.setattr(omnigent_agent, "urlopen", urlopen)
    monkeypatch.setattr(omnigent_agent, "RETRY_SECONDS", (0, 0))
    og = object.__new__(omnigent_agent.Omnigent)
    og.base, og._token = "http://omnigent.test", "synthetic"
    with pytest.raises(omnigent_agent.Fail, match="HTTP 404"):
        og.call("GET", "/x")
    assert len(calls) == 1


def test_an_http_error_shows_its_body_s_reason_and_a_stalled_body_costs_only_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stall = threading.Event()

    class Stalled(io.RawIOBase):
        def readable(self) -> bool:
            return True

        def readinto(self, b: bytearray) -> int:
            stall.wait(5)
            return 0

    for body, line in (
        (b'{"error": {"code": "invalid_input", "message": "bad role path"}}', "bad role path"),
        (b'{"detail": [{"loc": ["body", "agent_id"]}]}', '[{"loc": ["body", "agent_id"]}]'),
        (b"plain\n  text " + b"x" * 3000, "plain text " + "x" * (2048 - 13)),
        (b"", None),
        (None, "(body not read within 0.3s)"),
    ):
        fp = io.BufferedReader(Stalled()) if body is None else io.BytesIO(body)

        def urlopen(req: Request, timeout: float, fp: object = fp) -> None:
            raise HTTPError("http://omnigent.test/x", 400, "Bad Request", Message(), fp)

        monkeypatch.setattr(omnigent_agent, "urlopen", urlopen)
        monkeypatch.setattr(omnigent_agent, "ERROR_BODY_SECONDS", 0.3)
        og = object.__new__(omnigent_agent.Omnigent)
        og.base, og._token = "http://omnigent.test", "synthetic"
        began = time.monotonic()
        with pytest.raises(omnigent_agent.Fail) as e:
            og.call("POST", "/v1/sessions", {})
        assert time.monotonic() - began < 1.5
        head = "POST /v1/sessions: HTTP 400 Bad Request"
        assert str(e.value) == (
            head if line is None else f"{head}{':' * (body is not None)} {line}"
        )
    stall.set()


def test_a_create_that_failed_after_the_server_made_the_session_briefs_that_session(
    tmp_path: Path,
) -> None:
    og = FakeOmnigent()
    og.fail.add("create")
    sid, _ = omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
    assert sid == "s0" and list(og.sessions) == ["s0"] and og.posts == 1


def test_a_failed_create_with_no_session_made_fails_the_start(tmp_path: Path) -> None:
    og = FakeOmnigent()
    og.fail.add("create")
    og.find = lambda title, after, top_level=False: None  # type: ignore[method-assign]
    with pytest.raises(omnigent_agent.Fail, match="start failed: POST /v1/sessions"):
        omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
    assert og.posts == 0


@pytest.mark.parametrize(
    ("helpers", "subscriptions_fail", "expected"),
    [
        (
            {"cloud": (300, False)},
            False,
            300.0,
        ),  # a running cloud worker, no child: its updates count
        (
            {"cloud": (50, False)},
            False,
            100.0,
        ),  # an idle helper holds nothing off: the child is newer
        ({"done": (500, True)}, False, 100.0),  # an archived helper counts for nothing
        ({"gone": (500, False)}, False, 100.0),  # a deleted one too
        ({"cloud": (300, False)}, True, 100.0),  # no subscriptions endpoint: the children alone
    ],
)
def test_active_counts_a_subscribed_top_level_helper_like_a_child(
    monkeypatch: pytest.MonkeyPatch,
    helpers: dict[str, tuple[int, bool]],
    subscriptions_fail: bool,
    expected: float,
) -> None:
    def call(self, method: str, path: str, body: object = None) -> object:
        if path == "/v1/sessions/lead/child_sessions?limit=100":
            return {"data": [{"id": "kid", "updated_at": 100}]}
        if path == "/v1/sessions/lead/subscriptions":
            if subscriptions_fail:
                raise omnigent_agent.Fail("GET subscriptions: HTTP 404 Not Found")
            return {"data": [{"subscriber_id": "lead", "session_id": h} for h in helpers]}
        sid = path.removeprefix("/v1/sessions/").removesuffix(omnigent_agent.SLIM)
        if sid == "gone":
            raise omnigent_agent.Gone("GET: HTTP 404 Not Found")
        updated, archived = helpers[sid]
        return {"id": sid, "updated_at": updated, "archived": archived}

    monkeypatch.setattr(omnigent_agent.Omnigent, "call", call)
    assert object.__new__(omnigent_agent.Omnigent).active("lead") == expected  # no login read


def test_the_lookup_matches_the_title_under_the_parent_created_after_the_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def call(self, method: str, path: str, body: object = None) -> object:
        assert path == "/v1/sessions/coord/child_sessions?limit=100"
        return {
            "data": [
                {"id": "old", "title": "t", "created_at": 99},
                {"id": "other", "title": "t-x", "created_at": 200},
                {"id": "new", "title": "t", "created_at": 100},
            ]
        }

    monkeypatch.setattr(omnigent_agent.Omnigent, "call", call)
    og = object.__new__(omnigent_agent.Omnigent)
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "coord")
    assert og.find("t", 100) == "new"
    monkeypatch.delenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID")
    assert og.find("t", 100) is None  # top level: no parent to look under


def test_a_top_level_lookup_reads_the_projects_top_level_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths: list[str] = []

    def call(self, method: str, path: str, body: object = None) -> object:
        paths.append(path)
        if path == "/v1/projects":
            return {"data": [{"name": "my shop", "id": "p1"}]}
        return {
            "data": [
                {"id": "old", "title": "t", "created_at": 99},
                {"id": "new", "title": "t", "created_at": 100},
            ]
        }

    monkeypatch.setattr(omnigent_agent.Omnigent, "call", call)
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "coord")
    og = object.__new__(omnigent_agent.Omnigent)
    monkeypatch.setenv("OMNIGENT_PROJECT", "my_shop")  # lists under the project's exact name
    assert og.find("t", 100, top_level=True) == "new"
    assert paths[-1] == "/v1/sessions?kind=default&project=my%20shop&limit=100"
    monkeypatch.setenv("OMNIGENT_PROJECT", "no-such-group")  # created unfiled: look there
    og.find("t", 100, top_level=True)
    assert paths[-1] == "/v1/sessions?kind=default&project=&limit=100"


# The one line a brief keeps of the long-run rule, which lives in a role file (#902).
LONG_RUN_POINTER = (
    "A long run follows engineering-loop's verifier.md, A long or locked run is detached"
)
LONG_RUN_RULE_START = "A long run starts detached"  # the paragraph briefs carried before #902
VERIFIER_MD = (
    Path(omnigent_agent.__file__).parents[4]
    / "loop"
    / "skills"
    / "engineering-loop"
    / "verifier.md"
)


def long_run_rule() -> str:
    """verifier.md's bullet `A long or locked run is detached`, joined onto one line."""
    text = " ".join(VERIFIER_MD.read_text().split())
    start = text.index("**A long or locked run is detached.**")
    return text[start : text.index(" - **", start)]


# The default preamble as it was before --top-level existed: a worker's or verifier's brief.
DEFAULT_PREAMBLE = """\
The owner authorized this task; do it now, without asking for a go-ahead. This file is your brief.
{role}A long run follows engineering-loop's verifier.md, A long or locked run is detached, whatever your role.
Write your final report to {report}, once, when done: keep any progress notes in another file, since the report path is read as your answer.
A message that adds no task (a relay, a notice, a correction) is answered in the turn's ending line only: the report changes only through work on the task, and an acknowledgement never replaces an earlier report's results.
Never address the owner. If you need a decision the brief, the spec and the repo do not settle, write the question to {report}.question, reply with only that path, and stop: the answer comes as your next message.
When done, reply with only the report path.

"""


def create_bodies(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    bodies: list[dict] = []

    def call(self, method: str, path: str, body: dict | None = None) -> object:
        if body is not None:
            bodies.append(body)
        return {
            "/v1/agents": [{"name": omnigent_agent.AGENTS["claude"], "id": "a1"}],
            "/v1/agents?scope=user": [],
            "/v1/projects": {"data": [{"name": "shop", "id": "p1"}]},
        }.get(path, {"id": "s1"})

    monkeypatch.setattr(omnigent_agent.Omnigent, "call", call)
    monkeypatch.setattr(
        omnigent_agent.Omnigent, "__init__", lambda self: setattr(self, "host_id", "h")
    )
    monkeypatch.setattr(omnigent_agent, "repo_name", lambda: "shop")
    monkeypatch.delenv("OMNIGENT_PROJECT", raising=False)
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "coord")
    return bodies


def test_without_top_level_the_create_body_and_brief_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bodies = create_bodies(monkeypatch)
    omnigent_agent.Omnigent().create("claude", "t")
    omnigent_agent.Omnigent().create("claude", "t", workspace="/w")
    assert bodies[1]["workspace"] == "/w" and bodies.pop()["project_id"] == "p1"
    assert bodies == [
        {
            "agent_id": "a1",
            "title": "t",
            "host_id": "h",
            "workspace": os.getcwd(),
            "project_id": "p1",
            "parent_session_id": "coord",
        }
    ]
    assert omnigent_agent.PREAMBLE == DEFAULT_PREAMBLE
    og = FakeOmnigent()
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", "roles/worker.md", 0)
    lock = f"LOOP_HEAVY_LOCK={tmp_path / 'engineering-loop/heavy-coord.lock'}\n"
    assert (
        tmp_path / "r.md.brief"
    ).read_text() == "AGENT_SIGNATURE=cc/?/?\n" + lock + DEFAULT_PREAMBLE.format(
        report=tmp_path / "r.md", role="Your role file is roles/worker.md; read it first.\n"
    ).removesuffix("\n") + omnigent_agent.HOST_RUN_NOTE + "\n" + "do X"  # a host child (#1233)
    assert og.top_level == [False] and og.roles == ["roles/worker.md"]


def test_children_of_one_lead_share_one_heavy_lock_and_another_lead_s_get_their_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    def lock_line(name: str, agent: str = "claude") -> str | None:
        og = FakeOmnigent()
        report = tmp_path / f"{name}.md"
        sid, _ = omnigent_agent.start(og, agent, name, "do X", report, None, 0)
        sent = next(i for i in og.items(sid) if i["type"] == "message")["content"][0]["text"]
        text = (
            omnigent_agent.brief_of(report).read_text()
            if report.with_suffix(".md.brief").exists()
            else sent
        )
        return next((x for x in text.splitlines() if x.startswith("LOOP_HEAVY_LOCK=")), None)

    assert lock_line("outside") is None  # no lead, nothing changes
    monkeypatch.setenv("OMNIGENT_TERMINAL_LAUNCH_SESSION_ID", "lead1")
    first, second = lock_line("w1"), lock_line("w2")
    assert (
        first == second == f"LOOP_HEAVY_LOCK={tmp_path / 'cache/engineering-loop/heavy-lead1.lock'}"
    )
    assert lock_line("cloud", "claude-cloud") is None  # its tools run in a VM with its own cores
    og = FakeOmnigent()  # another lead: it caps its own children
    omnigent_agent.start(og, "claude", "l", "x", tmp_path / "l.md", None, 0, top_level=True)
    assert "LOOP_HEAVY_LOCK" not in omnigent_agent.brief_of(tmp_path / "l.md").read_text()
    monkeypatch.setenv("OMNIGENT_TERMINAL_LAUNCH_SESSION_ID", "lead2")
    assert lock_line("w3") not in (first, None)
    monkeypatch.setenv("LOOP_HEAVY_LOCK", "/l/lead1.lock")  # a child starting children keeps it
    assert lock_line("w4") == "LOOP_HEAVY_LOCK=/l/lead1.lock"
    # a worker of lead1 starting its own child, with no LOOP_HEAVY_LOCK of its own: still lead1's
    monkeypatch.delenv("LOOP_HEAVY_LOCK")
    monkeypatch.setenv("OMNIGENT_SESSION_ID", "worker1")
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "lead1")
    assert lock_line("w5") == first


@pytest.mark.needs_tool("perl")
def test_worker_md_s_heavy_lock_command_waits_for_the_lock_and_passes_the_exit_through(
    tmp_path: Path,
) -> None:
    import fcntl

    text = (
        Path(__file__).resolve().parents[2] / "loop/skills/engineering-loop/worker.md"
    ).read_text()
    command = re.search(
        r"`(perl -MFcntl=:flock .*?)\s+<path> uv run <gatekeeper>/scripts/approvals\.py heavy\s+mise run test:changed`",
        text,
        re.DOTALL,
    )
    assert command, "worker.md's gate step names the lock command"
    lock = tmp_path / "heavy.lock"
    argv = ["sh", "-c", command.group(1) + ' "$0" sh -c "exit 7"', str(lock)]
    with lock.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(subprocess.TimeoutExpired):
            subprocess.run(argv, timeout=1, check=False)
    assert subprocess.run(argv, timeout=10, check=False).returncode == 7


def test_a_top_level_start_has_no_parent_and_the_lead_preamble(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bodies = create_bodies(monkeypatch)
    omnigent_agent.Omnigent().create("claude", "t", top_level=True)
    assert "parent_session_id" not in bodies[-1] and bodies[-1]["project_id"] == "p1"
    og = FakeOmnigent()
    og.fail.add("create")  # the lookup after a failed create is the top-level one too
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    sid, _ = omnigent_agent.start(
        og, "claude", "t", "do X", tmp_path / "r.md", None, 0, top_level=True
    )
    assert og.top_level == [True, True]
    text = (tmp_path / "r.md.brief").read_text()
    assert (
        text
        == "AGENT_SIGNATURE=cc/?/?\n"
        + omnigent_agent.LEAD_PREAMBLE.format(report=tmp_path / "r.md", role="")
        + "do X"
    )
    assert "owner's own words" in text and "your own question card" in text  # #2170
    assert "needs-owner" not in text
    assert (
        LONG_RUN_POINTER in text
        and "setsid" not in text
        and f"final report to {tmp_path / 'r.md'}" in text
    )
    assert "authorized" not in text and "Never address" not in text and ".question" not in text
    assert omnigent_agent.text_of(og.sessions[sid][1]).startswith("Work on t: read ")


def test_a_top_level_start_subscribes_its_starter_to_the_lead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("OMNIGENT_SESSION_ID", "OMNIGENT_TERMINAL_LAUNCH_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "chief")
    og = FakeOmnigent()
    lead, _ = omnigent_agent.start(
        og, "claude", "t", "do X", tmp_path / "a.md", None, 0, top_level=True
    )
    monkeypatch.setenv("OMNIGENT_SESSION_ID", "child")  # a child of the chief: it, not the chief
    own, _ = omnigent_agent.start(
        og, "codex", "s", "do V", tmp_path / "e.md", None, 0, top_level=True
    )
    assert og.subscriptions.pop() == ("child", own)
    monkeypatch.delenv("OMNIGENT_SESSION_ID")
    omnigent_agent.start(og, "claude", "w", "do Y", tmp_path / "b.md", None, 0)
    assert og.subscriptions == [("chief", lead)]  # a child's parent hears it already
    og.fail.add("subscribe")  # an Omnigent without subscriptions: the lead still starts
    other, _ = omnigent_agent.start(
        og, "claude", "u", "do Z", tmp_path / "c.md", None, 0, top_level=True
    )
    assert other in og.sessions and og.archived == []
    assert f"not subscribed to {other}" in capsys.readouterr().err
    # started from no session, or from a shell that does not name it (a native Codex one)
    monkeypatch.delenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID")
    og.fail.discard("subscribe")
    last, _ = omnigent_agent.start(
        og, "claude", "v", "do W", tmp_path / "d.md", None, 0, top_level=True
    )
    assert og.subscriptions == [("chief", lead)]
    assert f"not subscribed to {last}" in capsys.readouterr().err


def test_a_top_level_start_from_a_leads_helper_subscribes_the_lead_not_the_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lead's host helper, a child on a runner of its own, starts a worker on another host and
    goes idle; the lead parks. The worker's turn end must reach the lead, the helper's root."""
    monkeypatch.setenv("OMNIGENT_SESSION_ID", "helper")
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "helper")
    og = FakeOmnigent()
    og.roots["helper"] = "lead"
    worker, _ = omnigent_agent.start(
        og, "claude", "acceptance", "do X", tmp_path / "a.md", None, 0, top_level=True
    )
    assert og.subscriptions == [("lead", worker)]
    monkeypatch.setattr(og, "info", lambda sid: (_ for _ in ()).throw(omnigent_agent.Fail("503")))
    other, _ = omnigent_agent.start(
        og, "claude", "other", "do Y", tmp_path / "b.md", None, 0, top_level=True
    )
    assert og.subscriptions[-1] == ("helper", other)  # a failed lookup still subscribes someone


def test_run_refuses_top_level_before_a_session_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("do X")
    argv = ["run", "claude", "t", str(tmp_path / "b.md"), str(tmp_path / "r.md"), "--top-level"]
    assert omnigent_agent.main(argv) == 1
    assert not og.sessions and "use start --top-level" in capsys.readouterr().err


def test_a_found_session_that_already_has_the_brief_is_not_briefed_twice(tmp_path: Path) -> None:
    og = FakeOmnigent()
    og.fail.add("create")
    find = og.find

    def briefed(title: str, after: float, top_level: bool = False) -> str | None:
        sid = find(title, after)
        brief = tmp_path / "r.md.brief"
        og.land(
            sid,
            f"Work on t: read {brief} and do what it says.",
        )
        return sid

    og.find = briefed  # type: ignore[method-assign]
    omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
    assert og.posts == 0


def test_a_turn_that_ends_without_the_report_is_nudged_once_then_the_report_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(status="idle")  # the brief stays last: the turn ended with no output
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(og, "claude", "t", "do X", report, None, 0)
    monkeypatch.setattr(omnigent_agent, "IDLE_GRACE_SECONDS", 0)
    send = og.send

    def answer(s: str, text: str) -> None:
        send(s, text)
        report.write_text("done")

    og.send = answer  # type: ignore[method-assign]
    assert omnigent_agent.wait(og, sid, report) == str(report)
    assert og.posts == 2
    assert omnigent_agent.text_of(og.sessions[sid][-1]) == omnigent_agent.NUDGE.format(
        report=report
    )


def test_a_second_turn_that_ends_without_the_report_fails(tmp_path: Path) -> None:
    og = FakeOmnigent(reply="waiting for the background command")
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(og, "claude", "t", "do X", report, None, 0)
    with pytest.raises(omnigent_agent.Fail, match="(?s)ended \\(idle\\) without .*background"):
        omnigent_agent.wait(og, sid, report)
    assert og.posts == 2  # the brief and one nudge


def fake_gh(monkeypatch: pytest.MonkeyPatch, comments: dict[str, dict]) -> list[tuple[str, ...]]:
    """``gh`` faked for a checkout of ``o/r``: ``repo view`` names it, ``api`` answers a comment
    of ``comments`` by its id. Returns the calls."""
    calls: list[tuple[str, ...]] = []

    def gh(*args: str) -> str:
        calls.append(args)
        if args[:2] == ("repo", "view"):
            return "o/r\n"
        return json.dumps(comments[args[1].rsplit("/", 1)[1]])

    monkeypatch.setattr(omnigent_agent, "gh", gh)
    return calls


def on_pr(n: int, body: str = "", repo: str = "o/r") -> dict:
    return {"issue_url": f"https://api.github.com/repos/{repo}/issues/{n}", "body": body}


def test_a_cloud_worker_s_report_is_the_pr_comment_its_last_message_links(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(reply="https://github.com/o/r/pull/7#issuecomment-42")
    og.harnesses = {"claude-cloud": "acp:claude-cloud"}
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.local.json").write_text(
        '{"remote": {"defaultEnvironmentId": "env_a"}}'
    )
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    calls = fake_gh(monkeypatch, {"42": on_pr(7, "Head: abc\nGates: 0\n")})
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("do X")
    argv = ["run", "claude-cloud", "t", "b.md", "r.md", "--pr", "7", "--confirm-seconds", "0"]
    assert omnigent_agent.main(argv) == 0
    assert capsys.readouterr().out.splitlines()[-1] == str(tmp_path / "r.md")
    assert (tmp_path / "r.md").read_text() == "Head: abc\nGates: 0\n"
    assert ("api", "repos/o/r/issues/comments/42") in calls and og.posts == 1  # no nudge
    brief = omnigent_agent.text_of(og.sessions["s0"][1])
    assert "PR #7" in brief and "gh api repos/{owner}/{repo}/issues/7/comments" in brief
    # a cloud brief swaps the detach pointer for the in-turn rule (#1002)
    assert "A run may not outlive the turn" in brief and "setsid" not in brief
    assert "detached" in omnigent_agent.NUDGE and "foreground" not in omnigent_agent.NUDGE
    assert (
        "restart it only if it did not" in omnigent_agent.PR_NUDGE
        and "foreground" not in omnigent_agent.PR_NUDGE
    )
    assert str(tmp_path / "r.md") not in brief  # a host path the cloud can't write


def checkout_with_worktree(tmp_path: Path, pin: dict | None) -> tuple[Path, Path]:
    """A main checkout, with ``pin`` as its settings.local.json when given, and a worktree of it."""
    main, tree = (
        tmp_path / "main checkout",
        tmp_path / "the tree",
    )  # spaces: paths split on lines only
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(main)]
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "i"], check=True)
    subprocess.run([*git, "worktree", "add", "-q", "-b", "w", str(tree)], check=True)
    if pin is not None:
        (main / ".claude").mkdir()
        (main / ".claude" / "settings.local.json").write_text(json.dumps(pin))
    return main, tree


def start_cloud(
    og: FakeOmnigent, workspace: Path, agent: str = "claude-cloud", title: str = "t"
) -> int:
    (workspace / "b.md").write_text("do X")
    argv = ["start", agent, title, "b.md", f"tmp/{title}.md", "--confirm-seconds", "0"]
    return omnigent_agent.main([*argv, "--workspace", str(workspace)])


def test_pr_is_refused_to_an_agent_that_is_not_a_cloud_agent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # a native worker given --pr ran its build on the orchestration host, starving its sessions
    og = FakeOmnigent()
    og.harnesses = {"claude-cloud": "acp:claude-cloud"}
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.local.json").write_text(
        '{"remote": {"defaultEnvironmentId": "env_a"}}'
    )
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("do X")
    for cmd in ("start", "run"):
        argv = [cmd, "claude", "t", "b.md", "r.md", "--pr", "1", "--confirm-seconds", "0"]
        assert omnigent_agent.main(argv) == 1
        assert "claude-cloud" in capsys.readouterr().err and og.sessions == {}
    argv = ["start", "claude-cloud", "t", "b.md", "r.md", "--pr", "1", "--confirm-seconds", "0"]
    assert omnigent_agent.main(argv) == 0 and list(og.sessions) == ["s0"]


def test_the_pr_refusal_names_the_verifier_route_for_a_codex_harness_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hint follows the agent's resolved harness, never its registered name."""
    og = FakeOmnigent()
    og.harnesses = {
        "claude-cloud": "acp:claude-cloud",
        "codex": "codex-native-ui",
        "release-verifier": "codex-native-ui",  # a Codex harness under another name
        "codex-review-coordinator": "claude-native-ui",  # a Claude harness under a Codex-like name
        "claude": "claude-native-ui",
    }
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("do X")
    base = ["t", "b.md", "r.md", "--pr", "1", "--confirm-seconds", "0"]
    for cmd in ("start", "run"):
        for agent in ("codex", "release-verifier"):
            assert omnigent_agent.main([cmd, agent, *base]) == 1
            err = capsys.readouterr().err
            assert f"{agent} is not a cloud agent; start claude-cloud for a cloud worker" in err
            assert "cloud_verifier.py" in err
            assert "`verify --pr 1` in the worker's VM" in err  # where verify runs
            assert "or `thread` from a host" in err  # where thread runs
            assert "per loop.md's Placement: line" in err
            assert "SKILL.md, Cloud worker, Starting a PR's cloud worker" in err
            assert err.count("\n") == 1 and og.sessions == {}  # one line, nothing started
        for agent in ("claude", "codex-review-coordinator"):  # the message it had
            assert omnigent_agent.main([cmd, agent, *base]) == 1
            err = capsys.readouterr().err
            assert "cloud_verifier.py" not in err and og.sessions == {}
            assert err.endswith("or drop --pr for one on this host\n")


def test_a_cloud_start_from_a_worktree_takes_its_main_checkout_s_environment_pin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent()
    og.harnesses = {"claude-cloud": "acp:claude-cloud"}
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    _, tree = checkout_with_worktree(tmp_path, {"remote": {"defaultEnvironmentId": "env_a"}})
    monkeypatch.chdir(tree)
    assert start_cloud(og, tree) == 0
    settings = tree / ".claude" / "settings.local.json"
    assert json.loads(settings.read_text()) == {"remote": {"defaultEnvironmentId": "env_a"}}
    assert "env_a" in capsys.readouterr().err and len(og.sessions) == 1
    # a worktree pinned to another environment is refused: its main checkout's pin would apply
    settings.write_text(json.dumps({"remote": {"defaultEnvironmentId": "env_b"}}))
    assert start_cloud(og, tree, title="t2") == 1
    err = capsys.readouterr().err
    assert "env_b" in err and "env_a" in err and len(og.sessions) == 1


def test_a_worktree_s_other_settings_stay_when_it_gains_the_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent()
    og.harnesses = {"claude-cloud": "acp:claude-cloud"}
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    _, tree = checkout_with_worktree(tmp_path, {"remote": {"defaultEnvironmentId": "env_a"}})
    (tree / ".claude").mkdir()
    settings = tree / ".claude" / "settings.local.json"
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "remote": {"x": 1}}))
    monkeypatch.chdir(tree)
    assert start_cloud(og, tree) == 0
    assert json.loads(settings.read_text()) == {
        "permissions": {"allow": ["Bash(ls)"]},
        "remote": {"x": 1, "defaultEnvironmentId": "env_a"},
    }


def test_a_cloud_start_with_no_environment_pin_is_refused_before_a_session_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent()
    og.harnesses = {"claude-cloud": "acp:claude-cloud"}
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    # the login holds no environment named after the repository, nor Default
    monkeypatch.setattr(omnigent_agent, "cloud_environments", list)
    main, tree = checkout_with_worktree(tmp_path, None)
    for workspace in (tree, main):  # a worktree, then a checkout that is no worktree
        monkeypatch.chdir(workspace)
        assert start_cloud(og, workspace) == 1
        err = capsys.readouterr().err
        assert "defaultEnvironmentId" in err and "'Default'" in err and "cloud-env" in err
        assert not (workspace / ".claude" / "settings.local.json").exists()
    assert og.sessions == {}


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_a_local_agent_is_refused_in_the_main_checkout_cloud_sessions_launch_from(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, agent: str
) -> None:
    """A local agent there switched the shared checkout's branch under every later start, and
    carried another worker's uncommitted edits along. A worktree's copied pin is its own."""
    og = FakeOmnigent()
    og.harnesses = {"claude-cloud": "acp:claude-cloud"}
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    main, tree = checkout_with_worktree(tmp_path, {"remote": {"defaultEnvironmentId": "env_a"}})
    monkeypatch.chdir(main)
    (main / "b.md").write_text("do X")
    (main / "sub").mkdir()
    for cmd, ws in (("start", "."), ("run", "."), ("start", "sub")):  # a folder in it is it
        argv = [cmd, agent, "t", "b.md", f"{ws}/t.md", "--confirm-seconds", "0", "--workspace", ws]
        assert omnigent_agent.main(argv) == 1
        err = capsys.readouterr().err
        assert "start claude-cloud" in err and "worktree" in err and og.sessions == {}
        assert "launch-dir <remote> <branch> <path>" in err and err.count("\n") == 1
    assert start_cloud(og, main, title="c") == 0 and len(og.sessions) == 1
    apart = tmp_path / "apart"  # its git dir lives outside it
    subprocess.run(
        ["git", "init", "-q", "--separate-git-dir", str(tmp_path / "gd"), str(apart)], check=True
    )
    shutil.copytree(main / ".claude", apart / ".claude")
    (apart / "b.md").write_text("do X")
    monkeypatch.chdir(apart)
    argv = ["start", agent, "t", "b.md", "t.md", "--confirm-seconds", "0", "--workspace", "."]
    assert omnigent_agent.main(argv) == 1 and len(og.sessions) == 1
    monkeypatch.chdir(tree)
    assert start_cloud(og, tree, title="c") == 0  # pins the worktree
    assert start_cloud(og, tree, agent, title="n") == 0 and len(og.sessions) == 3


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_a_native_start_needs_no_environment_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: str
) -> None:
    og = FakeOmnigent()
    og.harnesses = {"claude-cloud": "acp:claude-cloud"}
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    _, tree = checkout_with_worktree(tmp_path, {"remote": {"defaultEnvironmentId": "env_a"}})
    monkeypatch.chdir(tree)
    assert start_cloud(og, tree, agent) == 0
    assert not (tree / ".claude").exists() and len(og.sessions) == 1


@pytest.mark.parametrize(
    "marker", ["UNPOSTED REPORT", "`UNPOSTED REPORT`", "**UNPOSTED REPORT**", "__UNPOSTED REPORT__"]
)
def test_a_cloud_worker_that_could_not_post_its_report_has_it_posted_from_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    # the report links an earlier comment on its PR, as a pass-2 report does
    body = "Rerun https://github.com/o/r/pull/7#issuecomment-5\nBranch: claude/x at abc\nGates: 0\n"
    og = FakeOmnigent(reply=f"{marker}\n{body}")
    calls = fake_gh(
        monkeypatch,
        {
            "5": on_pr(7, "an old report"),
            "comments": "https://github.com/o/r/pull/7#issuecomment-9",
        },
    )
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(og, "claude-cloud", "t", "do X", report, None, 0, pr=7)
    brief = omnigent_agent.text_of(og.sessions[sid][1])
    assert "`claude/…` branch this session started on" in brief and omnigent_agent.UNPOSTED in brief
    assert omnigent_agent.wait(og, sid, report, pr=7) == str(report)
    assert report.read_text() == body and og.posts == 1  # no nudge
    assert calls[-1] == (
        "api",
        "repos/o/r/issues/7/comments",
        "-f",
        f"body={body}",
        "--jq",
        ".html_url",
    )
    posted = len(calls)
    assert omnigent_agent.wait(og, sid, report, pr=7) == str(report)  # a re-attached wait
    assert len(calls) == posted  # posts nothing again


def test_with_a_deny_list_a_cloud_worker_replies_with_its_report_and_the_host_checks_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker can't read the host's list, so it never posts: its reply carries the report, which wait checks
    before posting, and a listed term posts nothing."""
    deny = tmp_path / "deny.txt"
    deny.write_text("Acme-Widgets\n")
    monkeypatch.setenv("LOOP_DENY_LIST", str(deny))
    body = "Branch: claude/x at abc\nSeen in acme-widgets.\n"
    og = FakeOmnigent(reply=f"UNPOSTED REPORT\n{body}")
    calls = fake_gh(monkeypatch, {"comments": "https://github.com/o/r/pull/7#issuecomment-9"})
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(og, "claude-cloud", "t", "do X", report, None, 0, pr=7)
    brief = omnigent_agent.text_of(og.sessions[sid][1])
    assert "which you don't post: reply with the line `UNPOSTED REPORT`" in brief
    assert "/comments" not in brief and "comment's URL" not in brief
    assert (
        "<!-- file: tmp/loop/<name> -->" in brief
    )  # its files still travel in the report it replies with
    with pytest.raises(omnigent_agent.Fail, match="line 2 names `Acme-Widgets` from the deny list"):
        omnigent_agent.wait(og, sid, report, pr=7)
    assert not [call for call in calls if "body=" in " ".join(call)]
    monkeypatch.delenv("LOOP_DENY_LIST")
    assert "gh api repos/{owner}/{repo}/issues/7/comments" in omnigent_agent.pr_preamble().format(
        pr=7, report=report, unposted="U", role=""
    )


def test_with_a_deny_list_a_held_worker_is_never_told_to_post_and_a_posted_report_is_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The nudge after a turn without the report keeps the worker held; a report it posted anyway is checked before
    it or its files land on the host."""
    deny = tmp_path / "deny.txt"
    deny.write_text("Acme\n")
    monkeypatch.setenv("LOOP_DENY_LIST", str(deny))
    report = tmp_path / "r.md"
    og = FakeOmnigent(reply="gate still running")
    sid, _ = omnigent_agent.start(og, "claude-cloud", "t", "do X", report, None, 0, pr=7)
    with pytest.raises(omnigent_agent.Fail, match="without"):
        omnigent_agent.wait(og, sid, report, pr=7)
    [nudge] = [
        text
        for item in og.sessions[sid]
        if (text := omnigent_agent.text_of(item)).startswith(omnigent_agent.NO_REPORT)
    ]
    assert "`UNPOSTED REPORT`" in nudge and "without posting it" in nudge
    assert "comment's URL" not in nudge
    og = FakeOmnigent(reply="https://github.com/o/r/pull/7#issuecomment-9")
    body = "Head: abc\n<!-- file: tmp/loop/x.py -->\n```\nprint('Acme')\n```\n"
    fake_gh(monkeypatch, {"9": on_pr(7, body)})
    sid, _ = omnigent_agent.start(og, "claude-cloud", "t", "do X", report, None, 0, pr=7)
    with pytest.raises(omnigent_agent.Fail, match="names `Acme` from the deny list"):
        omnigent_agent.wait(og, sid, report, pr=7)
    assert not report.exists() and not (tmp_path / "tmp").exists()


def test_a_cloud_report_brings_the_repro_files_it_carries_and_lists_the_missing_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    body = (
        "Repro: tmp/loop/test_repro_7.py and tmp/loop/repro-7.sh; gate log tmp/loop/gate-7.log;"
        " also tmp/loop/lost.txt and tmp/loop/x.\n\n<!-- file: tmp/loop/test_repro_7.py -->\n```python\n"
        "def test_x():\n    assert 1\n```\n\n<!-- file: tmp/loop/repro-7.sh -->\n````sh\n"
        "echo ```\n````\n<!-- file: tmp/loop/../../evil -->\n```\nx\n```\n"
        "<!-- file: tmp/loop/link.txt -->\n```\nnot through the link\n```\n"
        "<!-- file: tmp/loop/unclosed.py -->\n```\nno closing fence\n"
        "<!-- file: tmp/loop/after.py -->\n```\nb = 1\n```\n"
        "<!-- file: tmp/loop/adir -->\n```\ncan't land\n```\n"
        "<!-- file: tmp/loop/big.log (last lines) -->\n```\nthe end\n```\n"
    )
    og = FakeOmnigent(reply="https://github.com/o/r/pull/7#issuecomment-9")
    fake_gh(monkeypatch, {"9": on_pr(7, body)})
    loop = tmp_path / "tmp" / "loop"
    (loop / "adir").mkdir(parents=True)
    (loop / "gate-7.log").write_text("the lead's own")
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched")
    (loop / "link.txt").symlink_to(outside)
    report = loop / "r.md"
    sid, _ = omnigent_agent.start(og, "claude-cloud", "t", "do X", report, None, 0, pr=7)
    brief = omnigent_agent.text_of(og.sessions[sid][1])
    assert "<!-- file: tmp/loop/<name> -->" in brief and "65,536" in brief
    assert omnigent_agent.wait(og, sid, report, pr=7) == str(report)  # whatever the files do
    assert report.read_text() == body
    assert (loop / "test_repro_7.py").read_text() == "def test_x():\n    assert 1\n"
    assert (loop / "repro-7.sh").read_text() == "echo ```\n"  # a longer fence holds a shorter one
    assert (loop / "after.py").read_text() == "b = 1\n"  # an unclosed block ends at the next file
    assert outside.read_text() == "untouched" and not (loop / "link.txt").is_symlink()
    assert (loop / "link.txt").read_text() == "not through the link\n"
    assert (loop / "gate-7.log").read_text() == "the lead's own"
    assert not (tmp_path / "evil").exists() and not (tmp_path / "tmp" / "evil").exists()
    err = capsys.readouterr().err
    assert f"brought {loop / 'test_repro_7.py'}" in err and f"brought {loop / 'repro-7.sh'}" in err
    for name in ("lost.txt", "x", "unclosed.py", "gate-7.log", "adir"):
        assert f"missing: tmp/loop/{name} (" in err, name
    assert "this host's own copy stays" in err and "not written here" in err
    assert not (loop / "big.log").exists() and (loop / "big.log.tail").read_text() == "the end\n"
    assert (
        f"missing: tmp/loop/big.log (too long for a comment; its last lines are {loop / 'big.log'}.tail)"
        in err
    )


@pytest.mark.parametrize(
    "link",
    [
        "https://github.com/o/r/pull/8#issuecomment-1",  # another PR
        "https://github.com/another/repo/pull/7#issuecomment-2",  # another repo, the same number
        "https://github.com/o/r/pull/7#issuecomment-3",  # a comment GitHub files on PR 8
    ],
)
def test_a_cloud_worker_s_second_turn_without_a_comment_on_its_pr_fails(
    link: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(reply=f"pushed; see {link}")
    fake_gh(monkeypatch, {"1": on_pr(8), "2": on_pr(7, repo="another/repo"), "3": on_pr(8)})
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(og, "claude-cloud", "t", "do X", report, None, 0, pr=7)
    with pytest.raises(omnigent_agent.Fail, match="without a report comment on PR #7"):
        omnigent_agent.wait(og, sid, report, pr=7)
    assert og.posts == 2 and not report.exists()
    assert omnigent_agent.PR_NUDGE.format(pr=7) in [
        omnigent_agent.text_of(i) for i in og.sessions[sid]
    ]


@pytest.mark.parametrize(
    "reply, code",
    [
        ("fixed; https://github.com/o/r/pull/7#issuecomment-42", 1),  # the turn before's comment
        ("fixed, pushed", 1),  # no comment
        ("https://github.com/o/r/pull/7#issuecomment-43", 0),  # a fresh one
    ],
)
def test_a_cloud_worker_s_follow_up_takes_only_a_report_comment_newer_than_its_report(
    reply: str, code: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(reply=reply)
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    old, fresh = on_pr(7, "the first report"), on_pr(7, "the fix report")
    old["created_at"], fresh["created_at"] = "2001-01-01T00:00:00Z", "2001-09-09T02:00:00Z"
    fake_gh(monkeypatch, {"42": old, "43": fresh})
    monkeypatch.chdir(tmp_path)
    sid = og.create("claude-cloud", "t")
    report = tmp_path / "r.md"
    report.write_text("the first report")
    os.utime(report, (1e9, 1e9))  # 2001-09-09T01:46:40Z, written by the first turn's wait
    (tmp_path / "fix.md").write_text("fix ST1, read the review on PR #7")
    argv = ["send", sid, "fix.md", "r.md", "--pr", "7", "--confirm-seconds", "0"]
    assert omnigent_agent.main(argv) == code
    assert report.read_text() == ("the fix report" if code == 0 else "the first report")
    assert og.posts == 1 + code  # the follow-up, and a nudge for a turn without a fresh one


def test_a_top_level_cloud_worker_on_another_host_gets_the_pr_preamble_and_reports_on_its_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # started from a host without the cloud harness: --host needs --top-level, which kept the lead
    # preamble and refused --pr
    og = FakeOmnigent(reply="https://github.com/o/r/pull/7#issuecomment-42")
    og.host = lambda name: "far"  # type: ignore[method-assign]
    og.harnesses = {"claude-cloud": "acp:claude-cloud"}
    og.procs["far"] = {"/there/repo/.claude/settings.local.json": PINNED}
    og.others = [
        {"id": "x1", "host_id": "far", "status": "idle", "updated_at": 1}
    ]  # a file reads through it
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    fake_gh(monkeypatch, {"42": on_pr(7, "Branch: claude/x at abc\n")})
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("do X")
    report = str(tmp_path / "there" / "r.md")  # a path on box, never made here
    argv = ["start", "claude-cloud", "t", "b.md", report, "--top-level", "--host", "box"]
    argv += ["--workspace", "/there/repo", "--pr", "o/r#7", "--confirm-seconds", "0"]
    assert omnigent_agent.main(argv) == 0
    assert og.top_level == [True] and og.workspaces == ["/there/repo"]
    brief = omnigent_agent.text_of(og.sessions["s0"][1])
    assert brief.startswith("Work on t. Your brief:") and "issues/7/comments" in brief
    assert "owner's own words" not in brief  # not the lead preamble
    # what the clone holds; the foreground limit is the role file's long-run rule, not the brief's
    assert "checked out in the workspace this session started from" in brief
    assert "git fetch origin pull/7/head" in brief and "10 minutes" not in brief
    assert "A run may not outlive the turn" in brief and LONG_RUN_POINTER not in brief
    # its report is the PR comment, written here under tmp/loop/, as a far report comes home
    info = og.info
    og.info = lambda sid: {**info(sid), "host_id": og.hosts_of[sid]}  # type: ignore[method-assign]
    assert omnigent_agent.main(["wait", "s0", report, "--pr", "7"]) == 0
    assert (tmp_path / "tmp/loop/r.md").read_text() == "Branch: claude/x at abc\n"
    # a follow-up takes that copy as the report before: a turn ending on its comment is not a new one
    fake_gh(monkeypatch, {"42": {**on_pr(7, "old"), "created_at": "2001-01-01T00:00:00Z"}})
    (tmp_path / "fix.md").write_text("fix it")
    send = ["send", "s0", "fix.md", report, "--pr", "7", "--confirm-seconds", "0"]
    assert omnigent_agent.main(send) == 1
    assert not (tmp_path / "there").exists()


def test_wait_keep_leaves_a_delivered_worker_open_for_a_relay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent(reply="https://github.com/o/r/pull/7#issuecomment-42")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    fake_gh(monkeypatch, {"42": on_pr(7, "done")})
    monkeypatch.chdir(tmp_path)
    relay = ["relay", "fix the finding", "--confirm-seconds", "0"]
    for title, keep, archived, code in (
        ("t0", [], [], 0),
        ("t1", ["--no-keep"], ["s1"], omnigent_agent.GONE),
    ):
        sid, _ = omnigent_agent.start(
            og, "claude-cloud", title, "do X", tmp_path / "r", None, 0, pr=7
        )
        assert omnigent_agent.main(["wait", sid, f"{title}.md", "--pr", "7", *keep]) == 0
        assert og.archived == archived
        assert omnigent_agent.main([relay[0], sid, *relay[1:]]) == code
    assert "s1 is archived" in capsys.readouterr().err


def test_run_keep_leaves_a_delivered_worker_open_for_a_relay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(reply="https://github.com/o/r/pull/7#issuecomment-42")
    og.harnesses = {"claude-cloud": "acp:claude-cloud"}
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.local.json").write_text(
        '{"remote": {"defaultEnvironmentId": "env_a"}}'
    )
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    fake_gh(monkeypatch, {"42": on_pr(7, "done")})
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("do X")
    for title, keep, archived in (("t0", [], []), ("t1", ["--no-keep"], ["s1"])):
        argv = ["run", "claude-cloud", title, "b.md", f"{title}.md", "--pr", "7", *keep]
        assert omnigent_agent.main([*argv, "--confirm-seconds", "0"]) == 0
        assert og.archived == archived


def test_send_pr_delivers_to_a_session_whose_workspace_is_on_another_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(reply="https://github.com/o/r/pull/7#issuecomment-43")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    fake_gh(monkeypatch, {"43": on_pr(7, "the fix report")})
    monkeypatch.chdir(tmp_path)
    sid = og.create("claude-cloud", "t", workspace="/home/elsewhere/repo")  # no such path here
    (tmp_path / "fix.md").write_text("fix ST1")
    argv = ["send", sid, "fix.md", "tmp/r.md", "--pr", "7", "--confirm-seconds", "0"]
    assert omnigent_agent.main(argv) == 0
    assert (tmp_path / "tmp" / "r.md").read_text() == "the fix report"


def test_a_start_waits_on_the_host_its_load_lands_on_a_cloud_bridge_on_memory_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def capacity(cpu: bool = True, **_: object) -> tuple[bool, str]:
        return (False, "cpu 2% idle") if cpu else (True, "cloud start, cpu not judged")

    monkeypatch.setattr(omnigent_agent, "capacity", capacity)
    monkeypatch.setattr(omnigent_agent, "CAPACITY_WAIT_SECONDS", 0)
    og = FakeOmnigent()
    og.host_names = {"far": "far"}
    sid, _ = omnigent_agent.start(  # this host's load is not far's, which nothing here can read
        og, "claude-cloud", "t", "do X", tmp_path / "r.md", None, 0, top_level=True, host_id="far"
    )
    assert sid in og.sessions
    assert "far: load and memory not readable (" in capsys.readouterr().err
    with pytest.raises(omnigent_agent.Fail, match="host over capacity"):
        omnigent_agent.start(
            og, "claude", "u", "do Y", tmp_path / "u.md", None, 0, top_level=True, host_id="h"
        )
    og.harnesses = {"claude-cloud": "acp:claude-cloud"}  # its bridge runs here, its work in a VM
    sid, _ = omnigent_agent.start(og, "claude-cloud", "c", "do Z", tmp_path / "c.md", None, 0)
    assert sid in og.sessions  # a busy CPU holds no cloud start back (#868)
    monkeypatch.setattr(omnigent_agent, "capacity", lambda **_: (False, "200 MB available"))
    with pytest.raises(omnigent_agent.Fail, match="host over capacity for 0s: 200 MB available"):
        omnigent_agent.start(og, "claude-cloud", "m", "do W", tmp_path / "m.md", None, 0)


def test_a_cloud_start_is_judged_on_memory_alone_here_and_on_another_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.undo()  # the real capacity check, on a faked host
    monkeypatch.setattr(omnigent_agent, "cpu_idle", lambda: 1.0)
    monkeypatch.setattr(omnigent_agent, "available_mb", lambda: 4000.0)
    assert omnigent_agent.capacity()[0] is False  # a local start waits on 1% idle
    assert omnigent_agent.capacity(max_load=40, cpu=False) == (
        True,
        "cloud start, cpu not judged, 4000 MB available (min 1200)",
    )
    monkeypatch.setattr(omnigent_agent, "available_mb", lambda: 1000.0)
    assert omnigent_agent.capacity(cpu=False)[0] is False  # memory still refuses
    og = FakeOmnigent()
    og.others = [{"id": "x1", "host_id": "far", "status": "idle", "updated_at": 1}]
    read: list[str] = []

    def read_file(sid: str, path: str) -> str:
        read.append(path)
        return {"/proc/meminfo": mem(4000), "/proc/loadavg": "99.0 1 1"}[path]

    og.read_file = read_file  # type: ignore[method-assign]
    assert omnigent_agent.far_capacity(og, "far", 1536, 5, cpu=False) == (
        True,
        "cloud start, cpu not judged, 4000 MB available (min 1536)",
    )
    assert read == ["/proc/meminfo"]  # no load read for a cloud start
    read.clear()
    assert omnigent_agent.far_capacity(og, "far", 1536, None, cpu=False)[0] is True
    assert read == ["/proc/meminfo"]  # nor /proc/stat, with no max_load in the row
    assert omnigent_agent.far_capacity(og, "far", 1536, 5)[0] is False  # a local one: load 99


def far_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, **proc: str
) -> FakeOmnigent:
    """A FakeOmnigent run from host ``h``, whose own load is too high, beside host ``far`` called
    ``name`` with a live session ``x1`` whose ``/proc`` files are ``proc`` (``meminfo``,
    ``loadavg``, ``stat``)."""
    monkeypatch.setattr(omnigent_agent, "capacity", lambda **_: (False, "cpu 1% idle"))
    monkeypatch.setattr(omnigent_agent, "CAPACITY_WAIT_SECONDS", 0)
    og = FakeOmnigent()
    og.host = lambda n: {name: "far"}[n]  # type: ignore[method-assign]
    og.host_names = {"far": name}
    og.harnesses = {"claude-cloud": "acp:claude-cloud"}
    og.others = [{"id": "x1", "host_id": "far", "status": "idle", "updated_at": 1}]
    og.procs = {"far": {f"/proc/{k}": v for k, v in proc.items()}}
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("do X")
    return og


def far_start(agent: str, host: str) -> list[str]:
    return ["start", agent, "t", "b.md", "/there/repo/r.md", "--top-level", "--host", host]


def mem(mb: int) -> str:
    return f"MemTotal: 64000000 kB\nMemAvailable: {mb * 1024} kB\n"


def test_a_cloud_start_waits_on_its_bridge_host_s_memory_by_the_cloud_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = far_host(monkeypatch, tmp_path, "cc-experiments", meminfo=mem(1000), loadavg="3.00 2 1")
    og.procs.setdefault("far", {})["/there/repo/.claude/settings.local.json"] = PINNED
    argv = [*far_start("claude-cloud", "cc-experiments"), "--workspace", "/there/repo"]
    assert omnigent_agent.main(argv) == 1
    err = capsys.readouterr().err
    assert (  # the cloud floor, not the row's 2500 (#1671)
        "cc-experiments over capacity for 0s: cloud start, cpu not judged,"
        " 1000 MB available (min 1200)" in err
    )
    assert og.sessions == {}
    og.procs["far"]["/proc/meminfo"] = mem(1200)
    assert omnigent_agent.main(argv) == 0  # the minimum itself is room
    og.archived = ["s0"]
    og.procs["far"]["/proc/meminfo"] = mem(3000)
    assert omnigent_agent.main(argv) == 0  # this host's own load is not the bridge host's
    assert og.hosts_of == {"s0": "far", "s1": "far"}


def test_a_far_pinned_main_checkout_is_refused_before_any_session_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = far_host(monkeypatch, tmp_path, "oz", meminfo=mem(9000), loadavg="0.10 0 0")
    pin = '{"remote": {"defaultEnvironmentId": "env_1"}}'
    files, settings = og.procs["far"], "/there/repo/.claude/settings.local.json"
    argv = [*far_start("claude", "oz"), "--workspace", "/there/repo"]
    files[settings], files["/there/repo/.git"] = pin, "HEAD\nconfig"  # .git a folder: main
    assert omnigent_agent.main(argv) == 1
    assert "claude-cloud" in capsys.readouterr().err and og.sessions == {}
    # a folder inside the checkout is the checkout
    files["/there/repo/engineering/x"] = ""
    sub = [*far_start("claude", "oz"), "--workspace", "/there/repo/engineering"]
    assert omnigent_agent.main(sub) == 1 and og.sessions == {}
    err = capsys.readouterr().err
    assert "/there/repo is the main checkout" in err  # the root it found
    assert "launch-dir <remote> <branch> <path>" in err and err.count("\n") == 1
    started = 0
    other = "/there/repo/.claude/settings.json"
    for settings_text, git, also in (
        (pin, "gitdir: /there/main/.git/worktrees/repo", None),  # a worktree
        ('{"permissions": {}}', "HEAD\nconfig", None),  # settings with no pin
        (None, "HEAD\nconfig", "{}"),  # .claude holds another file, not the settings
        (None, "HEAD\nconfig", None),  # no .claude folder at all
    ):
        files["/there/repo/.git"] = git
        files.pop(settings, None)
        files.pop(other, None)
        if settings_text:
            files[settings] = settings_text
        if also:
            files[other] = also
        assert omnigent_agent.main(argv) == 0
        started += 1
        assert len(og.sessions) == started and "skipped" not in capsys.readouterr().err
    files["/there/repo/.git"] = "HEAD\nconfig"
    for bad in ("{", None):  # not JSON; listed but the read answers 404 (an older host)
        files[settings] = bad  # type: ignore[assignment]
        assert omnigent_agent.main(argv) == 0
        started += 1
        err = capsys.readouterr().err
        assert len(og.sessions) == started and "pinned-checkout check skipped on oz" in err
    og.others, og.archived = [], list(og.sessions)  # no live session there to read through
    assert omnigent_agent.main(argv) == 0
    assert "pinned-checkout check skipped on oz" in capsys.readouterr().err


def test_a_host_s_row_overridden_by_its_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = far_host(monkeypatch, tmp_path, "cc-experiments", meminfo=mem(2000), loadavg="3.00 2 1")
    og.procs.setdefault("far", {})["/there/repo/.claude/settings.local.json"] = PINNED
    monkeypatch.setenv("OMNIGENT_MIN_AVAILABLE_MB_CC_EXPERIMENTS", "1500")
    argv = [*far_start("claude-cloud", "cc-experiments"), "--workspace", "/there/repo"]
    assert omnigent_agent.main(argv) == 0 and og.sessions
    monkeypatch.setenv("OMNIGENT_MAX_LOAD_CC_EXPERIMENTS", "2.5")
    assert omnigent_agent.main(argv) == 0  # a cloud start judges no load (#868)
    local = [*far_start("claude", "cc-experiments"), "--workspace", "/there/repo"]
    assert omnigent_agent.main(local) == 1  # a local one: load 3 over its overridden max


def test_a_start_on_a_linux_host_waits_on_its_row_s_absolute_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = far_host(monkeypatch, tmp_path, "oz", meminfo=mem(9000), loadavg="10.50 9 8 3/400 7")
    argv = [*far_start("claude", "oz"), "--workspace", "/there/repo"]
    assert omnigent_agent.main(argv) == 1
    assert "oz over capacity for 0s: load 10.5 (max 10), 9000 MB available (min 1536)" in (
        capsys.readouterr().err
    )
    og.procs["far"]["/proc/loadavg"] = "10.00 9 8 3/400 7"
    assert omnigent_agent.main(argv) == 1  # a load of 10 or more refuses
    og.procs["far"]["/proc/loadavg"] = "9.50 9 8 3/400 7"
    assert omnigent_agent.main(argv) == 0 and og.sessions


def test_a_host_without_a_row_is_judged_per_core_and_waited_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stat = "cpu  1 2 3 4\ncpu0 1 2 3 4\ncpu1 1 2 3 4\nintr 5\n"
    og = far_host(monkeypatch, tmp_path, "box", meminfo=mem(1000), loadavg="10.0 1 1", stat=stat)
    argv = [*far_start("claude", "box"), "--workspace", "/there/repo"]
    assert omnigent_agent.main(argv) == 1
    assert "box over capacity for 0s: load 5.0 per core (max 4), 1000 MB available (min 1536)" in (
        capsys.readouterr().err
    )
    og.procs["far"]["/proc/meminfo"] = mem(2000)
    assert omnigent_agent.main(argv) == 1  # 5 per core over 2 cores
    og.procs["far"]["/proc/loadavg"] = "8.0 1 1"
    monkeypatch.setattr(omnigent_agent, "CAPACITY_WAIT_SECONDS", 60)
    reads = iter(["9.0 1 1", "8.0 1 1"])  # over, then exactly the most per core
    real = og.read_file
    og.read_file = lambda sid, path: (  # type: ignore[method-assign]
        next(reads) if path == "/proc/loadavg" else real(sid, path)
    )
    assert omnigent_agent.main(argv) == 0
    assert "box over capacity, waiting: load 4.5 per core (max 4)" in capsys.readouterr().err


def test_a_host_without_proc_starts_unchecked_saying_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = far_host(monkeypatch, tmp_path, "mac")  # a Mac: no /proc
    argv = [*far_start("claude", "mac"), "--workspace", "/there/repo"]
    assert omnigent_agent.main(argv) == 0 and og.sessions
    err = capsys.readouterr().err
    assert "mac: load and memory not readable (no /proc/meminfo); starting unchecked" in err
    og.others, og.archived = [], ["s0"]  # no live session there now
    assert omnigent_agent.main([*argv[:2], "u", *argv[3:]]) == 0
    assert "mac: load and memory not readable (no live session there)" in capsys.readouterr().err


def test_another_host_is_read_through_its_newest_live_sessions_up_to_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    og = FakeOmnigent()
    og.others = [
        {"id": "old", "host_id": "far", "status": "idle", "updated_at": 1},
        {"id": "gone", "host_id": "far", "status": "idle", "updated_at": 9, "archived": True},
        {"id": "dead", "host_id": "far", "status": "failed", "updated_at": 9},
        {"id": "here", "host_id": "h", "status": "idle", "updated_at": 9},
        {"id": "a", "host_id": "far", "status": "idle", "updated_at": 5},
        {"id": "b", "host_id": "far", "status": "running", "created_at": 4},
        {"id": "c", "host_id": "far", "status": "idle", "updated_at": 3},
    ]
    files = {"/proc/meminfo": mem(4000), "/proc/loadavg": "1.0 1 1", "/proc/stat": "cpu0 1\n"}
    tried: list[str] = []
    broken = {"a", "b", "c"}

    def read_file(sid: str, path: str) -> str:
        tried.append(sid)
        if sid in broken:
            raise omnigent_agent.Fail(f"GET {path}: HTTP 502")
        return files[path]

    og.read_file = read_file  # type: ignore[method-assign]
    assert omnigent_agent.far_capacity(og, "far", 1536, None) == (
        None,
        "read failed: GET /proc/meminfo: HTTP 502",
    )
    assert tried == ["a", "b", "c"]  # newest first, three at most, none archived, failed or here
    broken, tried[:] = {"a"}, []
    assert omnigent_agent.far_capacity(og, "far", 1536, None) == (
        True,
        "load 1.0 per core (max 4), 4000 MB available (min 1536)",
    )
    assert tried == ["a", "b", "b", "b"]
    files["/proc/meminfo"] = "MemTotal: 1 kB\n"
    assert omnigent_agent.far_capacity(og, "far", 1536, 5) == (
        True,
        "load 1.0 (max 5), memory not measured (min 1536)",
    )
    files["/proc/loadavg"] = ""
    assert omnigent_agent.far_capacity(og, "far", 1536, 5) == (None, "/proc/loadavg unreadable")


def test_capacity_host_judges_another_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = far_host(monkeypatch, tmp_path, "oz", meminfo=mem(9000), loadavg="10.50 9 8")
    assert omnigent_agent.main(["capacity", "--host", "oz"]) == 1
    assert capsys.readouterr().out == "oz: load 10.5 (max 10), 9000 MB available (min 1536)\n"
    og.procs["far"]["/proc/loadavg"] = "2.0 1 1"
    assert omnigent_agent.main(["capacity", "--host", "oz"]) == 0
    og.procs = {}
    assert omnigent_agent.main(["capacity", "--host", "oz"]) == 0
    assert "oz: load and memory not readable (no /proc/meminfo)" in capsys.readouterr().out


def test_an_epic_burst_box_is_refused_over_its_memory_load_or_session_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = far_host(monkeypatch, tmp_path, "epic-t1", meminfo=mem(1000), loadavg="1.0 1 1")
    argv = [*far_start("claude", "epic-t1"), "--workspace", "/there/repo"]
    assert omnigent_agent.main(argv) == 1  # short of memory
    assert "epic-t1 over capacity for 0s: load 1.0 (max 6), 1000 MB available (min 1500)" in (
        capsys.readouterr().err
    )
    og.procs["far"]["/proc/meminfo"] = mem(6000)
    og.procs["far"]["/proc/loadavg"] = "6.5 5 4"
    assert omnigent_agent.main(argv) == 1  # over the load cap
    assert "load 6.5 (max 6)" in capsys.readouterr().err
    assert og.sessions == {}
    og.procs["far"]["/proc/loadavg"] = "1.0 1 1"
    assert omnigent_agent.main(argv) == 0 and og.sessions  # room: starts
    capsys.readouterr()
    og.sessions = {}
    og.others = [  # four live sessions; an archived one doesn't count
        {"id": f"x{i}", "host_id": "far", "status": "idle", "updated_at": i} for i in range(4)
    ] + [{"id": "old", "host_id": "far", "status": "idle", "archived": True}]
    assert omnigent_agent.main(argv) == 1  # a fifth is refused
    assert "epic-t1 over capacity for 0s: 4 live sessions (max 4); load 1.0" in (
        capsys.readouterr().err
    )
    assert og.sessions == {}
    og.procs = {}  # load and memory unreadable: the session count still refuses
    assert omnigent_agent.main(argv) == 1
    assert "4 live sessions (max 4)" in capsys.readouterr().err


def test_bridged_cloud_sessions_neither_hit_nor_fill_the_session_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = far_host(monkeypatch, tmp_path, "epic-t1", meminfo=mem(6000), loadavg="1.0 1 1")
    cloud = {"harness": omnigent_agent.CLOUD_HARNESS}
    og.others = [
        {"id": f"x{i}", "host_id": "far", "status": "idle", "updated_at": i} for i in range(4)
    ]
    ok, line = omnigent_agent.host_capacity(og, "far", "epic-t1", cpu=False)
    assert ok and "live sessions" not in line  # a cloud start isn't capped
    og.others = [{**r, **cloud} for r in og.others]
    assert omnigent_agent.host_capacity(og, "far", "epic-t1")[0]  # four bridges don't fill it


def test_a_host_with_no_row_has_no_session_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = far_host(monkeypatch, tmp_path, "burst-1", meminfo=mem(6000), loadavg="1.0 1 1")
    og.others = [{"id": f"x{i}", "host_id": "far", "status": "idle"} for i in range(9)]
    argv = [*far_start("claude", "burst-1"), "--workspace", "/there/repo"]
    assert omnigent_agent.main(argv) == 0 and og.sessions


def test_this_host_s_row_adds_its_absolute_load_and_memory_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.undo()  # the real capacity check, on a faked host
    monkeypatch.setattr(omnigent_agent, "POLL_SECONDS", 0)
    monkeypatch.setattr(omnigent_agent, "TURN_END_SECONDS", 0)
    monkeypatch.setattr(omnigent_agent, "CAPACITY_WAIT_SECONDS", 0)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(mem(2400))
    monkeypatch.setattr(omnigent_agent, "MEMINFO", meminfo)
    monkeypatch.setattr(omnigent_agent.os, "getloadavg", lambda: (41.3, 0.0, 0.0))
    monkeypatch.setattr(omnigent_agent, "cpu_idle", lambda: 40.0)
    og = FakeOmnigent()
    og.host_names = {"h": "cc-experiments"}
    with pytest.raises(omnigent_agent.Fail) as refused:
        omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
    assert str(refused.value) == (
        "cc-experiments over capacity for 0s:"
        " cpu 40% idle (min 10), load 41.3 (max 40), 2400 MB available (min 2500)"
    )
    meminfo.write_text(mem(2600))
    with pytest.raises(omnigent_agent.Fail, match="load 41.3 \\(max 40\\), 2600 MB"):
        omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
    monkeypatch.setattr(omnigent_agent.os, "getloadavg", lambda: (40.0, 0.0, 0.0))
    with pytest.raises(omnigent_agent.Fail, match="load 40.0 \\(max 40\\)"):  # 40 or more refuses
        omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
    monkeypatch.setattr(omnigent_agent.os, "getloadavg", lambda: (39.0, 0.0, 0.0))
    monkeypatch.setattr(omnigent_agent, "cpu_idle", lambda: 3.0)
    with pytest.raises(omnigent_agent.Fail, match="cpu 3% idle"):  # idle CPU still decides
        omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
    monkeypatch.setattr(omnigent_agent, "cpu_idle", lambda: None)  # the row's load alone
    assert omnigent_agent.capacity(min_mb=2500, max_load=40) == (
        True,
        "load 39.0 (max 40), 2600 MB available (min 2500)",
    )
    og.host_names = {}
    og.hosts = lambda: (_ for _ in ()).throw(omnigent_agent.Fail("GET /v1/hosts: HTTP 502"))  # type: ignore[method-assign]
    assert omnigent_agent.host_name(og, "h") == "h"


def test_run_with_a_report_older_than_the_launch_waits_for_a_new_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(reply="done")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    report = tmp_path / "r.md"
    report.write_text("the pass before")
    os.utime(report, (1, 1))
    (tmp_path / "b.md").write_text("fix C1")
    argv = ["run", "claude", "t", "b.md", "r.md", "--confirm-seconds", "0"]
    assert omnigent_agent.main(argv) == 1  # the old report is not taken
    assert "without" in capsys.readouterr().err

    info = og.info
    og.info = lambda s: (report.write_text("fixed"), info(s))[1]  # type: ignore[method-assign]
    assert omnigent_agent.main(argv) == 0
    assert capsys.readouterr().out.splitlines()[-1] == str(report)


def test_run_takes_a_report_written_within_a_clock_tick_of_the_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The kernel stamps files from a coarse clock that lags time.time_ns() by up to a tick.
    monkeypatch.setattr(omnigent_agent.time, "time_ns", lambda: 2**62)
    og = FakeOmnigent(reply="done")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("do X")
    info = og.info
    og.info = lambda s: ((tmp_path / "r.md").write_text("ok"), info(s))[1]  # type: ignore[method-assign]
    argv = ["run", "claude", "t", "b.md", "r.md", "--confirm-seconds", "0"]
    assert omnigent_agent.main(argv) == 0


def killed_mid_run(og: FakeOmnigent, at: int, kill: Callable[[], None]) -> None:
    """``og.info`` runs ``kill`` before its ``at``-th look."""
    info, looks = og.info, [0]

    def look(sid: str) -> dict:
        looks[0] += 1
        if looks[0] == at:
            kill()
        return info(sid)

    og.info = look  # type: ignore[method-assign]


def test_a_child_killed_mid_run_leaves_a_failed_line_at_the_report_within_one_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(status="running")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("do X")
    polls: list[float] = []  # the poll sleeps after the kill
    error = {"code": "runner_disconnected", "message": "runner lost"}
    info = og.info
    og.info = lambda s: {**info(s), "last_task_error": error} if og.final == "failed" else info(s)  # type: ignore[method-assign]

    def kill() -> None:  # the runner disconnects: the session fails with its error code
        og.final = "failed"
        monkeypatch.setattr(omnigent_agent.time, "sleep", polls.append)

    killed_mid_run(og, 4, kill)
    argv = ["run", "claude", "t", "b.md", "r.md", "--confirm-seconds", "0"]
    assert omnigent_agent.main(argv) == 1
    assert (tmp_path / "r.md").read_text() == (
        "FAILED: failed/runner_disconnected: session s0 ended (failed): runner lost\n"
    )
    assert len(polls) <= 1


def test_a_run_stopped_by_a_signal_writes_its_failed_line_and_restores_the_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(status="running")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("do X")
    before = signal.getsignal(signal.SIGTERM)
    killed_mid_run(og, 4, lambda: os.kill(os.getpid(), signal.SIGTERM))
    argv = ["run", "claude", "t", "b.md", "r.md", "--confirm-seconds", "0"]
    assert omnigent_agent.main(argv) == 128 + signal.SIGTERM
    assert (tmp_path / "r.md").read_text() == "FAILED: interrupted: interrupted by SIGTERM\n"
    assert signal.getsignal(signal.SIGTERM) == before


@pytest.mark.skip(reason="Flaky: #591, skipped from the blocking gate after its third occurrence")
def test_a_run_process_killed_mid_wait_leaves_its_failed_line_within_one_poll(
    tmp_path: Path,
) -> None:
    # a real run client, its child a session that never reports, stopped as a harness stops it
    (tmp_path / "b.md").write_text("do X")
    driver = (
        f"import sys; sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
        "import test_omnigent_agent as t\n"
        "t.omnigent_agent.Omnigent = lambda: t.FakeOmnigent(status='running')\n"
        "t.omnigent_agent.capacity = lambda **_: (True, '')\n"  # its real 5 s poll stays
        "sys.exit(t.omnigent_agent.main(['run', 'claude', 't', 'b.md', 'r.md',"
        " '--confirm-seconds', '0']))\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", driver], cwd=tmp_path, stdout=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout and proc.stdout.readline().strip() == "s0"  # launched: now it waits
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5.0) == 128 + signal.SIGTERM  # within one POLL_SECONDS
    finally:
        proc.kill()
    assert (tmp_path / "r.md").read_text() == "FAILED: interrupted: interrupted by SIGTERM\n"


def test_a_start_that_fails_and_a_rerun_after_a_failure_each_write_their_own_failed_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(deaf={0, 1})  # the brief never lands: the launch fails
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("do X")
    report = tmp_path / "r.md"
    assert omnigent_agent.main(["start", "claude", "t", "b.md", "r.md", "--confirm-seconds", "0"])
    first = report.read_text()
    assert first.startswith("FAILED: ")
    # a rerun whose child fails replaces the earlier launch's line with its own
    og.deaf, og.final = set(), "failed"
    argv = ["run", "claude", "t2", "b.md", "r.md", "--confirm-seconds", "0"]
    assert omnigent_agent.main(argv) == 1
    assert report.read_text().startswith("FAILED: failed") and report.read_text() != first


def test_a_failed_run_never_overwrites_a_report_its_child_wrote(
    tmp_path: Path, role_file: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(reply="done")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("verify X")
    report = tmp_path / "r.md"
    killed_mid_run(og, 2, lambda: report.write_text("C1: findings, gate still running\n"))
    argv = [
        "run",
        "claude",
        "t",
        "b.md",
        "r.md",
        "--role",
        role_file("verifier.md"),
        "--confirm-seconds",
        "0",
    ]
    assert omnigent_agent.main(argv + ["--same-family"]) == 1  # no SATISFIED: line, no verdict
    assert report.read_text() == "C1: findings, gate still running\n"
    # nor an earlier pass's report a fix pass reuses the path of
    report.write_text("pass 1 report: C1 major\n")
    og.final = "failed"
    argv = ["run", "claude", "t2", "b.md", "r.md", "--confirm-seconds", "0"]
    assert omnigent_agent.main(argv) == 1
    assert report.read_text() == "pass 1 report: C1 major\n"


def test_a_re_attached_wait_takes_the_child_s_report_over_run_s_failed_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(reply="done")
    sid = og.create("claude", "t")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    report = tmp_path / "r.md"
    report.write_text("FAILED: interrupted: interrupted by SIGTERM\n")
    killed_mid_run(og, 3, lambda: report.write_text("the real report"))
    assert omnigent_agent.main(["wait", sid, "r.md"]) == 0
    assert capsys.readouterr().out.strip() == str(report)
    assert report.read_text() == "the real report"


@pytest.mark.skip(reason="red on main, tracked in #1935")
def test_a_failed_line_is_no_report_to_the_supervisor_a_start_a_follow_up_or_a_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "r.md"
    report.write_text("FAILED: timeout: session a gave no report after 10800s\n")
    og = Owing([told("brief", 120)], {"status": "idle"})
    event, _ = omnigent_agent.check(og, "a", report, 3600, {}, set(), print)
    assert "report written" not in event

    fake = FakeOmnigent(reply="done")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: fake)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("do X")
    assert omnigent_agent.main(["start", "claude", "t", "b.md", "r.md"]) == 0
    # stamped later than the next write: what the kernel's coarse clock can do within one tick
    os.utime(report, (2**31, 2**31))
    # send's first look is off_request's (#1835), before the message: the kill comes during the wait after it
    killed_mid_run(fake, 3, lambda: report.write_text("fixed"))
    assert omnigent_agent.main(["send", "s0", "b.md", "r.md"]) == 0
    assert report.read_text() == "fixed"

    report.write_text("FAILED: interrupted: interrupted by SIGTERM\n")
    os.utime(report, (2**31, 2**31))
    killed_mid_run(fake, 1, lambda: report.write_text("fixed again"))
    argv = ["run", "claude", "t2", "b.md", "r.md", "--confirm-seconds", "0"]
    assert omnigent_agent.main(argv) == 0


def test_a_brief_with_an_empty_backtick_span_is_refused_before_a_session_exists(
    tmp_path: Path,
) -> None:
    og = FakeOmnigent()
    with pytest.raises(omnigent_agent.Fail, match="line 2 has an empty `` span"):
        omnigent_agent.start(og, "claude", "t", "edit\nthe `` file", tmp_path / "r.md", None, 0)
    assert og.sessions == {}
    omnigent_agent.start(og, "claude", "t", "```sh\nls\n```", tmp_path / "r.md", None, 0)


def test_a_double_backtick_literal_passes_but_a_real_empty_span_still_fails(
    tmp_path: Path,
) -> None:
    og = FakeOmnigent()
    literal = "```diff\n # the name upper-cased, ``-`` as ``_``) override\n```\nuse ``-`` here"
    omnigent_agent.start(og, "claude", "t", literal, tmp_path / "r.md", None, 0)
    # an empty pair after a literal on the same line is still a lost name, at its own line
    with pytest.raises(omnigent_agent.Fail, match="line 3 has an empty `` span"):
        omnigent_agent.start(
            og, "claude", "t", "a\nb\nuse ``-`` and the `` file", tmp_path / "r.md", None, 0
        )
    with pytest.raises(omnigent_agent.Fail, match="line 1 has an empty `` span"):
        omnigent_agent.start(
            og, "claude", "t", "the `` file and a `` helper", tmp_path / "r.md", None, 0
        )


def test_a_failed_session_reports_its_last_task_error_and_runner_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(status="failed")
    sid, _ = omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
    info = og.info
    og.info = lambda s: {  # type: ignore[method-assign]
        **info(s),
        "id": s,
        "parent_session_id": "coord",
        "last_task_error": {"message": "claude exited: too many open files"},
    }
    monkeypatch.setattr(omnigent_agent, "RUNNER_LOGS", tmp_path)
    (tmp_path / "runner-coord-1.log").write_text("")
    with pytest.raises(
        omnigent_agent.Fail,
        match=f"(?s)ended \\(failed\\): claude exited: too many open files.*{tmp_path}/runner-coord-1.log",
    ):
        omnigent_agent.wait(og, sid, tmp_path / "r.md")
    assert og.posts == 1  # a failed session is not nudged


SETUP_ERROR = (
    "inner executor error: cloud session setup failed: Setup script failed with exit code -1"
)
STALL_ERROR = (
    "cloud session https://claude.ai/code/x made no progress for 1800s; it is left as it is,"
    " and the next message continues it"
)


def failing(og: FakeOmnigent, errors: dict[str, list[str]]) -> None:
    """Each session in ``errors`` reads failed with its next error per look (``running``: running,
    an empty one: as it is), and the last for good."""
    info = og.info

    def look(s: str) -> dict:
        snap = info(s)
        if not (left := errors.get(s)):
            return snap
        error = left.pop(0) if len(left) > 1 else left[0]
        if error == "running":
            return {**snap, "status": "running"}
        return (
            {**snap, "status": "failed", "last_task_error": {"message": error}} if error else snap
        )

    og.info = look  # type: ignore[method-assign]


def capped(og: FakeOmnigent, posts: int) -> None:
    """A send past ``posts`` fails the test: a recovery repeated would loop for good."""
    send = og.send

    def once(sid: str, text: str) -> None:
        assert og.posts < posts, f"a send past {posts}: {text}"
        send(sid, text)

    og.send = once  # type: ignore[method-assign]


def test_run_retries_a_setup_failure_once_in_a_fresh_session_with_the_same_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("do X")
    failing(og, {"s0": [SETUP_ERROR]})
    look = og.info
    og.info = lambda s: ((tmp_path / "r.md").write_text("ok") if s == "s1" else None, look(s))[1]  # type: ignore[method-assign]
    argv = ["run", "claude", "t", "b.md", "r.md", "--confirm-seconds", "0"]
    assert omnigent_agent.main(argv) == 0
    out, err = capsys.readouterr()
    assert out.split()[:2] == ["s0", "s1"] and out.split()[-1] == str(tmp_path / "r.md")
    assert re.search(
        r"session s0 failed in setup before its first turn \(.*setup failed.*\).*session s1", err
    )
    assert [omnigent_agent.text_of(i) for i in og.items("s1")[1:]] == [
        omnigent_agent.text_of(i) for i in og.items("s0")[1:]
    ]
    assert og.archived == ["s1"]


def test_run_fails_on_a_second_setup_failure_naming_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("do X")
    failing(og, {"s0": [SETUP_ERROR], "s1": [SETUP_ERROR]})
    argv = ["run", "claude", "t", "b.md", "r.md", "--confirm-seconds", "0"]
    assert omnigent_agent.main(argv) == 1
    err = capsys.readouterr().err
    assert re.search(
        r"(?s)session s0 failed in setup.*session s1 ended \(failed\): .*setup failed", err
    )
    assert len(og.sessions) == 2


def test_wait_continues_a_stalled_child_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent(reply="done")
    sid, _ = omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
    # the stall, and after the continue one look still at its end, then the new turn's report
    looks = [STALL_ERROR, STALL_ERROR, ""]
    failing(og, {sid: looks})
    look = og.info
    og.info = lambda s: (  # type: ignore[method-assign]
        (tmp_path / "r.md").write_text("ok") if looks == [""] else None,
        look(s),
    )[1]
    assert omnigent_agent.wait(og, sid, tmp_path / "r.md") == str(tmp_path / "r.md")
    assert og.posts == 2  # the brief and one continue
    assert omnigent_agent.text_of(og.items(sid)[-2]) == omnigent_agent.CONTINUE
    assert f"session {sid} stalled (" in capsys.readouterr().err

    og = FakeOmnigent(reply="done")
    sid, _ = omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "s.md", None, 0)
    failing(og, {sid: [STALL_ERROR, "running", STALL_ERROR]})  # stalls again once its new turn ran
    capped(og, 2)
    with pytest.raises(
        omnigent_agent.Fail,
        match=f"(?s)session {sid} stalled .*session {sid} ended \\(failed\\): .*made no progress",
    ):
        omnigent_agent.wait(og, sid, tmp_path / "s.md")
    assert og.posts == 2

    # another error just after the continue is a new failure, not the stall's end
    og = FakeOmnigent(reply="done")
    sid, _ = omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "u.md", None, 0)
    failing(og, {sid: [STALL_ERROR, "claude exited: boom"]})
    capped(og, 2)
    began = time.monotonic()
    with pytest.raises(omnigent_agent.Fail, match=f"(?s)session {sid} stalled .*boom"):
        omnigent_agent.wait(og, sid, tmp_path / "u.md")
    assert time.monotonic() - began < omnigent_agent.CONTINUE_SECONDS / 4


def test_wait_neither_retries_nor_continues_a_failure_after_the_first_turn_or_any_other(
    tmp_path: Path,
) -> None:
    relaunched = []
    for error, reply in ((SETUP_ERROR, "working on it"), ("claude exited: boom", "")):
        og = FakeOmnigent(reply=reply)
        sid, _ = omnigent_agent.start(og, "claude", "t", "do X", tmp_path / "r.md", None, 0)
        failing(og, {sid: [error]})
        capped(og, 1)
        with pytest.raises(omnigent_agent.Fail, match="ended \\(failed\\)"):
            omnigent_agent.wait(
                og, sid, tmp_path / "r.md", relaunch=lambda: relaunched.append(1) or "x"
            )
        assert og.posts == 1
    assert relaunched == []


def test_a_codex_verifier_defaults_to_its_model_unless_one_is_named(
    tmp_path: Path, role_file: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    models = []
    og = FakeOmnigent(reply="done")
    og.listed = ["named", "flag"]
    create = og.create
    og.create = lambda a, t, m=None, *_: (
        models.append(m),
        create(a, t, m),
    )[1]  # type: ignore[method-assign]
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.delenv("OMNIGENT_MODEL_CODEX", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("verify")
    base = ["start", "codex", "t", "b.md", "r.md", "--confirm-seconds", "0"]
    omnigent_agent.main([*base, "--role", role_file("verifier.md")])
    omnigent_agent.main([*base[:2], "t2", *base[3:]])
    monkeypatch.setenv("OMNIGENT_MODEL_CODEX", "named")
    omnigent_agent.main([*base[:2], "t3", *base[3:], "--role", role_file("verifier.md")])
    omnigent_agent.main(
        [*base[:2], "t4", *base[3:], "--role", role_file("verifier.md"), "--model", "flag"]
    )
    assert models == [None, None, None, "flag"]  # Omnigent.create reads OMNIGENT_MODEL_CODEX
    assert not hasattr(omnigent_agent, "CODEX_VERIFIER_MODEL")


def test_start_refuses_a_report_already_there(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(reply="done")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "r.md").write_text("the pass before")
    (tmp_path / "b.md").write_text("fix C1")
    argv = ["start", "claude", "t", "b.md", "r.md", "--confirm-seconds", "0"]
    assert omnigent_agent.main(argv) == 1  # its `wait` would take the old report
    assert "already exists" in capsys.readouterr().err and og.sessions == {}


def test_the_nudge_is_awaited_before_the_next_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(status="idle")
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(og, "claude", "t", "do X", report, None, 0)
    monkeypatch.setattr(omnigent_agent, "IDLE_GRACE_SECONDS", 0)
    monkeypatch.setattr(omnigent_agent, "POLL_SECONDS", 0)
    late: list[str] = []
    og.send = lambda s, text: late.append(text)  # type: ignore[method-assign]
    items = og.items

    def landing(s: str) -> list[dict]:  # the nudge shows up a few reads after its send
        if late and len(late) < 4:
            late.append("")
        elif late:
            og.land(s, late.pop(0))
            late.clear()
            report.write_text("done")
        return items(s)

    og.items = landing  # type: ignore[method-assign]
    assert omnigent_agent.wait(og, sid, report) == str(report)


class Roster:
    """Sessions for `watch`, each a script of info() snapshots; the last one repeats."""

    def __init__(self, **scripts: list[dict]) -> None:
        self.scripts = {sid: list(s) for sid, s in scripts.items()}
        self.kids: list[str] = []

    def info(self, sid: str) -> dict:
        script = self.scripts[sid]
        return dict(script.pop(0) if len(script) > 1 else script[0], title=f"t-{sid}")

    def items(self, sid: str) -> list[dict]:
        return []

    def children(self, parent: str) -> list[str]:
        return list(self.kids)


def said(role: str, text: str, i: str) -> dict:
    return {"id": i, "type": "message", "role": role, "content": [{"type": "x", "text": text}]}


def test_watch_wakes_on_a_turn_end_with_title_and_last_message() -> None:
    og = Roster(a=[{"status": "running"}, {"status": "running"}, {"status": "idle"}])
    msgs = [[said("user", "go", "1")], [said("user", "go", "1")], [said("assistant", "done", "2")]]
    og.items = lambda sid: msgs.pop(0) if len(msgs) > 1 else msgs[0]  # type: ignore[method-assign]
    assert omnigent_agent.watch(og, ["a"], timeout=5) == ["a t-a: turn ended\nlast message: done"]


def test_watch_ignores_states_it_starts_in_and_the_launch_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(omnigent_agent, "IDLE_GRACE_SECONDS", 3600)
    og = Roster(
        a=[{"status": "idle"}], b=[{"status": "failed", "last_task_error": {"message": "boom"}}]
    )
    with pytest.raises(omnigent_agent.Fail, match="no session changed in 0s: a, b"):
        omnigent_agent.watch(og, ["a", "b"], timeout=0)


def test_watch_reports_an_end_only_once_it_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    # A native Claude session reads idle while quiet mid-turn: the end it shows then is not one.
    monkeypatch.setattr(omnigent_agent, "TURN_END_SECONDS", 30)
    now = [0.0]
    monkeypatch.setattr(omnigent_agent.time, "monotonic", lambda: now[0])
    states = iter(["running", "idle", "running", "idle", "idle", "idle"])
    og = FakeOmnigent()
    sid = og.create("claude", "lead")
    og.info = lambda s: {"status": next(states), "title": "lead"}  # type: ignore[method-assign]
    og.items = lambda s: [{"id": "m1", "type": "message", "role": "assistant", "content": []}]  # type: ignore[method-assign]

    def tick(seconds: float) -> None:
        now[0] += 20

    monkeypatch.setattr(omnigent_agent.time, "sleep", tick)
    assert omnigent_agent.watch(og, [sid]) == [f"{sid} lead: turn ended\nlast message: "]
    assert now[0] == 100  # the first idle passed; the second held for 40s


def test_watch_reports_a_prompt_at_once_with_the_tool_call() -> None:
    prompt = {
        "elicitation_id": "e1",
        "params": {"message": "Claude wants to call **Bash**", "content_preview": "Bash(rm x)"},
    }
    og = Roster(a=[{"status": "running", "pending_elicitations": [prompt]}])
    assert omnigent_agent.watch(og, ["a"], timeout=0) == [
        "a t-a: blocked on a prompt: e1: Bash(rm x)\nlast message: (none)"
    ]


def test_watch_reports_a_failure_with_its_error() -> None:
    og = Roster(
        a=[
            {"status": "running"},
            {"status": "failed", "last_task_error": {"code": "x", "message": "runner died"}},
        ]
    )
    assert omnigent_agent.watch(og, ["a"], timeout=5)[0].startswith("a t-a: failed: runner died")


def test_watch_picks_up_a_child_started_after_it(monkeypatch: pytest.MonkeyPatch) -> None:
    og = Roster(k=[{"status": "idle"}])
    og.items = lambda sid: [said("assistant", "report at r.md", "9")]  # type: ignore[method-assign]
    polls = iter([[], ["k"]])
    og.children = lambda parent: next(polls, ["k"])  # type: ignore[method-assign]
    assert omnigent_agent.watch(og, [], parent="p", timeout=5) == [
        "k t-k: turn ended\nlast message: report at r.md"
    ]


class Clock:
    """`time.monotonic` for `watch`, moved on by each poll's sleep."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, step: float) -> None:
        self.now = 1000.0
        monkeypatch.setattr(omnigent_agent.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(
            omnigent_agent.time, "sleep", lambda s: setattr(self, "now", self.now + step)
        )


def test_watch_keeps_an_idle_it_starts_in_past_the_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    Clock(monkeypatch, step=20)
    og = Roster(a=[{"status": "idle"}])
    og.items = lambda sid: [said("user", "go", "1")]  # type: ignore[method-assign]
    with pytest.raises(omnigent_agent.Fail, match="no session changed"):
        omnigent_agent.watch(og, ["a"], timeout=200)


def test_watch_ignores_a_launch_idle_within_the_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    Clock(monkeypatch, step=5)
    flap = [{"status": "running"}, {"status": "idle"}, {"status": "idle"}, {"status": "running"}]
    og = Roster(a=flap)
    og.items = lambda sid: [said("user", "go", "1")]  # type: ignore[method-assign]
    with pytest.raises(omnigent_agent.Fail, match="no session changed"):
        omnigent_agent.watch(og, ["a"], timeout=100)


def test_watch_reports_an_empty_turn_once_past_the_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    Clock(monkeypatch, step=20)
    og = Roster(a=[{"status": "running"}, {"status": "idle"}])
    og.items = lambda sid: [said("user", "go", "1")]  # type: ignore[method-assign]
    assert omnigent_agent.watch(og, ["a"], timeout=200) == [
        "a t-a: turn ended\nlast message: (none)"
    ]


def test_watch_gives_a_follow_up_to_an_idle_baseline_its_own_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Clock(monkeypatch, step=5)
    # idle after a turn (baseline); the follow-up lands while still idle; running; its answer.
    og = Roster(
        a=[{"status": "idle"}, {"status": "idle"}, {"status": "running"}, {"status": "idle"}]
    )
    done = [said("user", "go", "1"), said("assistant", "report at r.md", "2")]
    follow_up = [*done, said("user", "pass 2: fix F1", "3")]
    history = [done, follow_up, follow_up, [*follow_up, said("assistant", "fixed", "4")]]
    og.items = lambda sid: history.pop(0) if len(history) > 1 else history[0]  # type: ignore[method-assign]
    assert omnigent_agent.watch(og, ["a"], timeout=100) == [
        "a t-a: turn ended\nlast message: fixed"
    ]


def test_watch_wakes_when_a_running_session_gains_a_prompt() -> None:
    prompt = {"elicitation_id": "e2", "params": {"message": "Claude wants to call **Bash**"}}
    og = Roster(a=[{"status": "running"}, {"status": "running", "pending_elicitations": [prompt]}])
    assert omnigent_agent.watch(og, ["a"], timeout=5) == [
        "a t-a: blocked on a prompt: e2: Claude wants to call **Bash**\nlast message: (none)"
    ]


def test_a_sub_agent_is_archived_like_a_top_level_session(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(
        omnigent_agent.Omnigent, "call", lambda self, *c, **kw: calls.append((*c, kw)) or {}
    )
    og = object.__new__(omnigent_agent.Omnigent)
    og.archive("kid")  # the server leaves the parent's shared runner up (omnigent 4dd0af32)
    # a cloud archive ends its VM through the bridge, up to about 65 s: no 30 s cut (#2052)
    timeout = {"timeout": omnigent_agent.ARCHIVE_SECONDS}
    assert calls == [("PATCH", "/v1/sessions/kid", {"archived": True}, timeout)]
    assert omnigent_agent.ARCHIVE_SECONDS > 65 > omnigent_agent.HTTP_SECONDS


def test_reap_finishes_done_children_and_keeps_the_busy_ones() -> None:
    prompt = {"elicitation_id": "e1", "params": {"message": "Claude wants to call **Bash**"}}
    states = {
        "idle": {"status": "idle"},
        "failed": {"status": "failed"},
        "running": {"status": "running"},
        "blocked": {"status": "idle", "pending_elicitations": [prompt]},
        "queued": {"status": "idle", "pending_inputs": [{"content": []}]},
        "gone": {"status": "idle", "archived": True},
    }
    og = FakeOmnigent()
    og.info = lambda s: dict(states[s], title=f"t-{s}")  # type: ignore[method-assign]
    og.children = lambda p: list(states) if p == "p" else []  # type: ignore[attr-defined]
    lines, failed = omnigent_agent.reap(og, [], parent="p")
    assert not failed
    assert og.archived == ["idle", "failed"]
    assert lines == [
        "idle t-idle: archived (idle)",
        "failed t-failed: archived (failed)",
        "running t-running: kept (running)",
        "blocked t-blocked: kept (blocked on a prompt)",
        "queued t-queued: kept (input pending)",
        "gone t-gone: already archived",
    ]

    def unknown(s: str) -> dict:
        raise omnigent_agent.Gone(f"GET /v1/sessions/{s}: HTTP 404 Not Found")

    og.info = unknown  # type: ignore[method-assign]
    with pytest.raises(omnigent_agent.Gone):  # an unknown session still fails, as before
        omnigent_agent.reap(og, ["nope"])


def test_reap_goes_on_past_a_finish_that_fails() -> None:
    og = FakeOmnigent()
    og.info = lambda s: {"status": "idle", "title": f"t-{s}"}  # type: ignore[method-assign]
    archive = og.archive

    def refuse_a(s: str) -> str:
        if s == "a":
            raise omnigent_agent.Fail("PATCH a: HTTP 503")
        return archive(s)

    og.archive = refuse_a  # type: ignore[method-assign]
    assert omnigent_agent.reap(og, ["a", "b"]) == (
        ["a t-a: finish failed: PATCH a: HTTP 503", "b t-b: archived (idle)"],
        True,
    )
    assert og.archived == ["b"]


def test_reap_exits_1_when_a_finish_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    title = "investigate: finish failed: PATCH"  # a title is not an outcome
    og.info = lambda s: {"status": "idle", "title": title}  # type: ignore[method-assign]
    assert omnigent_agent.main(["reap", "b"]) == 0
    og.info = lambda s: {"status": "idle", "title": f"t-{s}"}  # type: ignore[method-assign]
    capsys.readouterr()
    archive = og.archive

    def refuse_a(s: str) -> None:
        if s == "a":
            raise omnigent_agent.Fail("PATCH a: HTTP 503")
        archive(s)

    og.archive = refuse_a  # type: ignore[method-assign]
    assert omnigent_agent.main(["reap", "a", "c"]) == 1
    assert og.archived == ["b", "c"]
    assert capsys.readouterr().out.splitlines() == [
        "a t-a: finish failed: PATCH a: HTTP 503",
        "c t-c: archived (idle)",
    ]


def test_reap_drops_each_session_it_finishes_from_the_registry_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supervisor would post `archived` into the session that ran the reap (#249)."""
    og = FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    states = {"done": "idle", "busy": "running", "env": "idle"}
    og.info = lambda s: {"status": states[s], "title": f"t-{s}"}  # type: ignore[method-assign]
    registry = tmp_path / "registry"
    registry.write_text("".join(f"{s}=/r/{s}.md\tfollow\n" for s in [*states, "other"]))
    at_archive: list[str] = []
    archive = og.archive

    def archiving(s: str) -> None:
        at_archive.append(registry.read_text())
        archive(s)

    og.archive = archiving  # type: ignore[method-assign]
    assert omnigent_agent.main(["reap", "done", "busy", "--registry", str(registry)]) == 0
    assert "done=" not in at_archive[0]
    monkeypatch.setenv("OMNIGENT_SUPERVISE_REGISTRY", str(registry))
    assert omnigent_agent.main(["reap", "env"]) == 0
    assert og.archived == ["done", "env"]
    assert registry.read_text() == "busy=/r/busy.md\tfollow\nother=/r/other.md\tfollow\n"


def test_run_archives_on_its_report_but_not_on_a_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(reply="done")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("do X")
    info = og.info
    og.info = lambda s: ((tmp_path / "r.md").write_text("ok"), info(s))[1]  # type: ignore[method-assign]
    assert (
        omnigent_agent.main(["run", "claude", "t", "b.md", "r.md", "--confirm-seconds", "0"]) == 0
    )
    assert og.archived == ["s0"]
    og.info = lambda s: ((tmp_path / "q.md.question").write_text("which?"), info(s))[1]  # type: ignore[method-assign]
    argv = ["run", "claude", "t2", "b.md", "q.md", "--confirm-seconds", "0"]
    assert omnigent_agent.main(argv) == omnigent_agent.ASKED
    assert og.archived == ["s0"]  # the answer goes to s1


def test_wait_archives_the_session_whose_report_it_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(reply="done")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    sid = og.create("claude", "t")
    (tmp_path / "r.md").write_text("ok")
    assert omnigent_agent.main(["wait", sid, "r.md"]) == 0
    assert og.archived == [sid]


class Owing:
    """One session, ``a``, owing a report: ``info`` is a script of snapshots whose last one repeats,
    and its history is ``items``, to which a send appends a user message."""

    def __init__(self, items: list[dict], *infos: dict) -> None:
        self.items_, self.infos, self.sent, self.retried = items, list(infos), [], []
        self.kids = 0.0  # when a child of the session was last updated

    def active(self, sid: str) -> float:
        return self.kids

    def creations(self, sid: str) -> list[str]:
        return []

    def info(self, sid: str) -> dict:
        snap = self.infos.pop(0) if len(self.infos) > 1 else self.infos[0]
        return {"status": "idle", "runner_online": True, "title": "coord", **snap}

    def items(self, sid: str) -> list[dict]:
        return self.items_

    def send(self, sid: str, text: str) -> None:
        self.sent.append(text)
        self.items_.append({**said("user", text, f"r{len(self.sent)}"), "created_at": time.time()})

    def retry(self, sid: str) -> dict:
        self.retried.append(sid)
        return {"recovered": True, "recovery": "runner_relaunched"}


def told(text: str, ago: float) -> dict:
    return {**said("user", text, text), "created_at": time.time() - ago}


def aged(path: Path, ago: float) -> Path:
    path.write_text("x")
    os.utime(path, (time.time() - ago, time.time() - ago))
    return path


class Paged(Owing):
    """Owing whose history is longer than the newest 100 items ``items`` returns."""

    def __init__(self, history: list[dict], *infos: dict) -> None:
        super().__init__(history[-100:], *infos)
        self.history = history

    def older(self, sid: str, after: str = "") -> dict:
        newest = list(reversed(self.history))
        ids = [str(i.get("id")) for i in newest]
        start = ids.index(after) + 1 if after else 0
        data = newest[start : start + 100]
        last = data[-1]["id"] if data else None
        return {"data": data, "last_id": last, "has_more": start + 100 < len(newest)}


def steps(n: int, ago: float) -> list[dict]:
    return [
        {**said("assistant", f"s{k}", f"a{k}"), "created_at": time.time() - ago} for k in range(n)
    ]


def test_check_counts_a_report_written_after_an_instruction_behind_the_page(tmp_path: Path) -> None:
    report = aged(tmp_path / "r.md", 300)  # after the brief, before the page's oldest item
    og = Paged([told("brief", 600), *steps(150, 100)], {})
    event, since = omnigent_agent.check(og, "a", report, 3600, {}, set(), print)
    assert event == f"a coord: report written: {report}"
    assert abs(since - (time.time() - 600)) < 5


def test_check_refuses_a_report_older_than_an_instruction_behind_the_page(tmp_path: Path) -> None:
    report = aged(tmp_path / "r.md", 900)
    og = Paged([told("brief", 600), *steps(150, 100)], {})
    event, _ = omnigent_agent.check(og, "a", report, 3600, {}, set(), print)
    assert event == ""


def test_check_with_no_instruction_in_the_history_floors_at_the_pages_oldest_item(
    tmp_path: Path,
) -> None:
    report = aged(tmp_path / "r.md", 150)
    og = Paged(steps(150, 100), {})
    _, since = omnigent_agent.check(og, "a", report, 3600, {}, set(), print)
    assert abs(since - (time.time() - 100)) < 5


def test_a_page_short_of_100_items_is_read_without_a_page_back(tmp_path: Path) -> None:
    og = Paged([told("brief", 600), *steps(5, 100)], {})
    og.older = lambda *a, **k: pytest.fail("read back a whole history")  # type: ignore[method-assign]
    assert omnigent_agent.last_instruction(og, "a", og.items("a"), "") is not None
    assert omnigent_agent.last_instruction(og, "a", steps(5, 1), "") is None


def test_supervise_returns_a_report_written_since_the_last_instruction(tmp_path: Path) -> None:
    report = aged(tmp_path / "r.md", 60)
    og = Owing([told("brief", 120)], {})
    assert (
        omnigent_agent.supervise(og, {"a": report}, timeout=0)
        == f"a coord: report written: {report}"
    )
    # an earlier phase's report at the same path is not the one owed
    og = Owing([told("brief", 120), told("next phase", 30)], {})
    with pytest.raises(omnigent_agent.Fail, match="no event in 0s: a"):
        omnigent_agent.supervise(og, {"a": report}, timeout=0)


def test_settled_needs_the_turn_ended_at_every_look_for_turn_end_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(omnigent_agent, "TURN_END_SECONDS", 60)
    report = aged(tmp_path / "r.md", 120)  # a draft written long before
    idle, running = {"status": "idle"}, {"status": "running"}
    ended: dict[str, float] = {}
    assert not omnigent_agent.settled(
        report.stat().st_mtime_ns, idle, [], ended, "a"
    )  # one idle look is no turn end
    ended["a"] = time.time() - 61
    assert omnigent_agent.settled(report.stat().st_mtime_ns, idle, [], ended, "a")
    assert not omnigent_agent.settled(
        report.stat().st_mtime_ns, running, [told("x", 0)], ended, "a"
    )
    assert "a" not in ended  # a running look restarts the count
    assert not omnigent_agent.settled(report.stat().st_mtime_ns, idle, [], ended, "a")
    ended["a"] = time.time() - 61
    aged(report, 10)  # rewritten while out of its turn
    assert not omnigent_agent.settled(report.stat().st_mtime_ns, idle, [], ended, "a")


def test_check_reads_the_report_again_after_it_settled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "r.md"
    report.write_text("SATISFIED: yes\n")
    og = Owing([told("brief", 120)], {})

    def rewritten(*_: object) -> bool:
        report.write_text("revising")  # the verdict is gone by the time it settled
        return True

    monkeypatch.setattr(omnigent_agent, "settled", rewritten)
    event, _ = omnigent_agent.check(og, "a", report, 3600, {}, set(), print, verdict=True)
    assert event == ""


def test_supervise_counts_a_report_only_once_its_turn_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = aged(tmp_path / "r.md", 60)  # progress, while the lead still works
    og = Owing(
        [told("brief", 120), told("progress", 1) | {"role": "assistant"}], {"status": "running"}
    )
    with pytest.raises(omnigent_agent.Fail, match="no event in 0s: a"):
        omnigent_agent.supervise(og, {"a": report}, timeout=0)
    og.infos = [{"status": "idle"}]
    assert (
        omnigent_agent.supervise(og, {"a": report}, timeout=0)
        == f"a coord: report written: {report}"
    )


def test_supervise_returns_a_question_a_prompt_and_a_failure(tmp_path: Path) -> None:
    report = tmp_path / "r.md"
    aged(tmp_path / "r.md.question", 10)
    og = Owing([told("brief", 60)], {})
    assert (
        omnigent_agent.supervise(og, {"a": report}, timeout=0)
        == f"a coord: asked: {tmp_path / 'r.md.question'}"
    )
    report = tmp_path / "s.md"
    prompt = {"params": {"content_preview": "Bash(rm x)"}}
    og = Owing([told("brief", 60)], {"pending_elicitations": [prompt]})
    assert (
        omnigent_agent.supervise(og, {"a": report}, timeout=0)
        == "a coord: blocked on a prompt: Bash(rm x)"
    )
    og = Owing([told("brief", 60)], {"status": "failed", "last_task_error": {"message": "boom"}})
    assert omnigent_agent.supervise(og, {"a": report}, timeout=0) == "a coord: failed: boom"


def test_supervise_revives_a_reaped_runner_and_carries_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "r.md"
    og = Owing([told("brief", 60)], {"runner_online": False}, {"runner_online": True}, {})
    og.infos.append({"status": "failed", "last_task_error": {"message": "later"}})
    assert omnigent_agent.supervise(og, {"a": report}, timeout=5) == "a coord: failed: later"
    assert og.retried == ["a"]
    assert "a coord: revived (no live runner)" in capsys.readouterr().out


def test_supervise_gives_up_when_the_revive_fails(tmp_path: Path) -> None:
    og = Owing([told("brief", 60)], {"runner_online": False})
    og.retry = lambda sid: {"recovered": False, "recovery": "no_host"}  # type: ignore[method-assign]
    line = omnigent_agent.supervise(og, {"a": tmp_path / "r.md"}, timeout=0)
    assert line.startswith("a coord: session a has no live runner and the revive failed")
    assert line.endswith("; gave up")


def test_supervise_resumes_a_silent_session_once_then_gives_up(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "r.md"
    # silent for an hour, its status still `running`: a turn end Omnigent lost
    og = Owing([told("brief", 3600)], {"status": "running"})
    clock = [time.time()]
    monkeypatch.setattr(omnigent_agent.time, "time", lambda: clock[0])

    def later(_: float) -> None:
        clock[0] += 600

    monkeypatch.setattr(omnigent_agent.time, "sleep", later)
    line = omnigent_agent.supervise(og, {"a": report}, silent=1800, timeout=3600)
    assert og.sent == [omnigent_agent.NUDGE.format(report=report)]
    assert (
        "a coord: silent 3600s (running), report owed; resume line sent" in capsys.readouterr().out
    )
    assert line == "a coord: silent 1800s after the resume line; gave up"


def test_supervise_never_nudges_a_busy_session_and_the_resume_line_is_no_instruction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "r.md"
    og = Owing(
        [told("brief", 3600), {**said("assistant", "gates", "g"), "created_at": time.time()}], {}
    )
    with pytest.raises(omnigent_agent.Fail):
        omnigent_agent.supervise(og, {"a": report}, silent=1800, timeout=0)
    assert og.sent == []
    # the report written before the resume line still counts: the resume line asked for it again
    aged(report, 1800)
    og = Owing([told("brief", 3600), told(omnigent_agent.NUDGE.format(report=report), 60)], {})
    assert (
        omnigent_agent.supervise(og, {"a": report}, timeout=0)
        == f"a coord: report written: {report}"
    )


def test_supervise_cli_needs_session_report_pairs(capsys: pytest.CaptureFixture[str]) -> None:
    assert omnigent_agent.main(["supervise", "a"]) == 1
    assert "a: expected <session>=<report>" in capsys.readouterr().err


def test_supervise_gives_up_when_the_resume_line_stays_undelivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A harness stuck mid-step holds the resume line in pending_inputs: that is silence too."""
    report = tmp_path / "r.md"
    og = Owing([told("brief", 3600)], {"status": "running"})
    og.send = lambda sid, text: og.sent.append(text)  # type: ignore[method-assign]
    info = og.info
    og.info = lambda sid: {  # type: ignore[method-assign]
        **info(sid),
        "pending_inputs": [said("user", t, "p") for t in og.sent],
    }
    clock = [time.time()]
    monkeypatch.setattr(omnigent_agent.time, "time", lambda: clock[0])
    monkeypatch.setattr(
        omnigent_agent.time, "sleep", lambda _: clock.__setitem__(0, clock[0] + 600)
    )
    assert (
        omnigent_agent.supervise(og, {"a": report}, silent=1800, timeout=3700)
        == "a coord: silent 1800s after the resume line; gave up"
    )
    assert len(og.sent) == 1


def test_supervise_gives_up_at_once_on_a_silence_with_an_input_already_undelivered(
    tmp_path: Path,
) -> None:
    og = Owing([told("brief", 3600)], {"pending_inputs": [said("user", "answer", "p")]})
    assert (
        omnigent_agent.supervise(og, {"a": tmp_path / "r.md"}, silent=1800, timeout=0)
        == "a coord: silent 3600s with an input undelivered; gave up"
    )
    assert og.sent == []


def test_supervise_wakes_its_caller_before_reviving_another_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = aged(tmp_path / "a.md", 10)
    og = Owing([told("brief", 60)], {})
    info = og.info
    og.info = lambda sid: {**info(sid), "runner_online": sid == "a"}  # type: ignore[method-assign]
    line = omnigent_agent.supervise(og, {"a": report, "b": tmp_path / "b.md"}, timeout=0)
    assert line == f"a coord: report written: {report}"
    assert og.retried == []
    assert capsys.readouterr().out == f"a coord: report written: {report}\n"


def test_supervise_reads_no_other_session_after_a_caller_event(tmp_path: Path) -> None:
    """#156: the event wakes its caller at once, not after another session's read stalls."""
    report = aged(tmp_path / "a.md", 10)
    og = Owing([told("brief", 60)], {})
    reads: list[str] = []
    info, items = og.info, og.items
    og.info = lambda sid: (reads.append(sid), info(sid))[1]  # type: ignore[method-assign]
    og.items = lambda sid: (reads.append(sid), items(sid))[1]  # type: ignore[method-assign]
    line = omnigent_agent.supervise(og, {"a": report, "b": tmp_path / "b.md"}, timeout=0)
    assert line == f"a coord: report written: {report}"
    assert "b" not in reads


def test_a_restarted_supervisor_gives_up_on_a_silence_after_the_resume_line_in_the_history(
    tmp_path: Path,
) -> None:
    report = tmp_path / "r.md"
    resume = omnigent_agent.NUDGE.format(report=report)
    og = Owing([told("brief", 7200), told(resume, 3600)], {"status": "running"})
    assert (
        omnigent_agent.supervise(og, {"a": report}, silent=1800, timeout=0)
        == "a coord: silent 3600s after the resume line; gave up"
    )
    assert og.sent == []


class Fleet:
    """Sessions owing reports, each with its own history. A session in ``broken`` raises on every
    read; one in ``offline`` has no live runner until retried, and a message to it is lost.
    Messages that land go to ``inbox``; the next ``refuse`` sends fail."""

    def __init__(self, **histories: list[dict]) -> None:
        self.histories, self.reads, self.inbox, self.retried = histories, [], [], []
        self.kids: dict[str, float] = {}  # when a child of each session was last updated
        self.kin_of: dict[
            str, list[str] | Exception
        ] = {}  # each session's live children (or a read error)
        self.created_of: dict[str, list[str]] = {}  # the top-level sessions each one created
        self.broken: dict[str, Exception] = {}
        self.offline: set[str] = set()
        self.refuse = 0
        self.labels: list[tuple[str, dict]] = []  # each label write
        self.archived: list[str] = []  # each session finished once its result was told
        self.unlabelled = 0  # the next label writes that fail
        self.files: dict[str, str] = {}  # what read_file finds, by path
        self.written: dict[str, float | Exception] = {}  # what modified finds (or raises), by path
        self.stats: list[str] = []  # each modified read
        self.pages = 0  # history pages read back
        self.host_id = "h"  # this host
        self.harnesses = {"claude-cloud": omnigent_agent.CLOUD_HARNESS}  # an agent's, by its name
        self.rows: list[dict] = []  # the server's session listing, as ``listing`` returns it

    def listing(self) -> list[dict]:
        return self.rows

    def harness(self, agent: str) -> str:
        if agent not in self.harnesses:
            raise omnigent_agent.Fail(f"agent {agent} not registered")
        return self.harnesses[agent]

    def read_file(self, sid: str, path: str) -> str:
        if path not in self.files:
            raise omnigent_agent.Gone(f"{path}: not found")
        return self.files[path]

    def modified(self, sid: str, path: str) -> float:
        self.stats.append(path)
        at = self.written.get(path, 0.0)
        if isinstance(at, Exception):
            raise at
        return at

    def older(self, sid: str, after: str = "") -> dict:
        self.pages += 1
        newest = list(reversed(self.histories.get(sid, [])))
        ids = [str(i.get("id")) for i in newest]
        start = ids.index(after) + 1 if after else 0
        data = newest[start : start + 100]
        more = start + 100 < len(newest)
        return {"data": data, "last_id": data[-1]["id"] if data else None, "has_more": more}

    def active(self, sid: str) -> float:
        return self.kids.get(sid, 0.0)

    def creations(self, sid: str) -> list[str]:
        return self.created_of.get(sid, [])

    def kin(self, sid: str) -> dict[str, float]:
        found = self.kin_of.get(sid, [])
        if isinstance(found, Exception):
            raise found
        return dict.fromkeys(found, time.time())

    def hosts(self) -> dict[str, str]:
        return {"h": "here"}

    def archive(self, sid: str) -> None:
        self.archived.append(sid)

    def children(self, sid: str) -> list[str]:
        return []

    def label(self, sid: str, labels: dict[str, str]) -> None:
        if self.unlabelled:
            self.unlabelled -= 1
            raise omnigent_agent.Fail(f"PATCH /v1/sessions/{sid}: HTTP 503")
        self.labels.append((sid, labels))

    def info(self, sid: str) -> dict:
        self.reads.append(sid)
        if sid in self.broken:
            raise self.broken[sid]
        return {"status": "idle", "runner_online": sid not in self.offline, "title": f"t-{sid}"}

    def items(self, sid: str) -> list[dict]:
        return self.histories.get(sid, [])

    def send(self, sid: str, text: str) -> None:
        if self.refuse:
            self.refuse -= 1
            raise omnigent_agent.Fail(f"POST /v1/sessions/{sid}/events: HTTP 503")
        if sid not in self.offline:
            self.inbox.append((sid, text))

    def retry(self, sid: str) -> dict:
        self.retried.append(sid)
        self.offline.discard(sid)
        return {"recovered": True, "recovery": "runner_relaunched"}


class Stop(BaseException):
    """Ends the supervisor's loop: not an Exception, which the loop logs and survives."""


# The sleep before any test patched it: a second `supervised` in one test would otherwise take the first
# one's wrapper for the real one, and a git call's poll-sleep under load would count as a poll of the first.
REAL_SLEEP = time.sleep


def supervised(
    og: Fleet,
    registry: Path,
    monkeypatch: pytest.MonkeyPatch,
    polls: int,
    between=lambda poll: None,
    **kwargs: object,
) -> None:
    """Run ``supervise_registry`` for ``polls`` polls, calling ``between`` after each but the last."""
    count, real = [0], REAL_SLEEP

    def sleep(seconds: float) -> None:
        if sys._getframe(1).f_globals["__name__"] == "subprocess":
            return real(
                seconds
            )  # a git call with a timeout polls with sleep: it is no supervisor poll
        count[0] += 1
        if count[0] >= polls:
            raise Stop
        between(count[0])

    monkeypatch.setattr(omnigent_agent.time, "sleep", sleep)
    with pytest.raises(Stop):
        omnigent_agent.supervise_registry(og, registry, **kwargs)  # type: ignore[arg-type]


def test_a_second_supervised_run_counts_only_its_own_polls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "registry"
    registry.write_text(f"a={tmp_path / 'a.md'}\n")
    og = Fleet(a=[told("brief", 60)])
    supervised(og, registry, monkeypatch, 2)
    seen: list[int] = []

    def between(poll: int) -> None:
        seen.append(poll)
        subprocess.run(
            ["sleep", "0.05"], timeout=5, check=False
        )  # a call with a timeout poll-sleeps, as git does under load

    supervised(og, registry, monkeypatch, 3, between)
    assert seen == [1, 2]


STAMP = r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d "


def test_a_session_added_to_the_registry_between_polls_is_supervised_from_the_next_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = tmp_path / "registry"
    registry.write_text(f"a={tmp_path / 'a.md'}\n")
    report = aged(tmp_path / "b.md", 10)
    og = Fleet(a=[told("brief", 60)], b=[told("brief", 60)])
    with_b = lambda poll: registry.write_text(registry.read_text() + f"b={report}\n")
    supervised(og, registry, monkeypatch, 2, with_b)
    assert og.reads == ["a", "a", "b", "b"]  # the last one the finish of b, whose report is told
    assert re.fullmatch(f"{STAMP}b t-b: report written: {report}\n", capsys.readouterr().out)


def test_a_verifier_line_counts_its_report_only_once_it_has_its_satisfied_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = tmp_path / "registry"
    verdict, plain = aged(tmp_path / "v.md", 10), aged(tmp_path / "w.md", 10)
    for report in (verdict, plain):
        report.write_text("Head: abc\nVERDICT: approve\n")
    registry.write_text(f"v={verdict}\tverdict\nw={plain}\n")
    og = Fleet(v=[told("brief", 60)], w=[told("brief", 60)])
    finish = lambda poll: verdict.write_text(verdict.read_text() + "SATISFIED: yes\n")
    supervised(og, registry, monkeypatch, 2, finish)
    out = capsys.readouterr().out
    assert re.fullmatch(
        f"{STAMP}w t-w: report written: {plain}\n{STAMP}v t-v: report written: {verdict}\n", out
    )


def test_a_terminal_event_is_posted_to_the_notify_session_and_drops_only_its_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = tmp_path / "registry"
    report = aged(tmp_path / "a.md", 10)
    registry.write_text(f"# leads\na={report}\nb={tmp_path / 'b.md'}\n")
    og = Fleet(a=[told("brief", 60)], b=[told("brief", 60)])
    supervised(og, registry, monkeypatch, 1, notify="cos")
    assert og.inbox == [("cos", f"Supervisor: a t-a: report written: {report}")]
    assert registry.read_text() == f"# leads\nb={tmp_path / 'b.md'}\n"
    assert re.fullmatch(f"{STAMP}a t-a: report written: {report}\n", capsys.readouterr().out)


class Lead(Fleet):
    """One followed lead, ``a``, whose ``snap`` fields (status, prompts, error, archived) the test sets."""

    def __init__(self, *items: dict) -> None:
        super().__init__(a=list(items))
        self.snap: dict = {}

    def info(self, sid: str) -> dict:
        return {**super().info(sid), **self.snap}

    def send(self, sid: str, text: str) -> None:  # a message to a lead lands in its history
        super().send(sid, text)
        if sid in self.histories and sid not in self.offline:
            n = len(self.histories[sid])
            self.histories[sid].append({**said("user", text, f"u{n}"), "created_at": time.time()})


def ending(text: str, ago: float) -> dict:
    return {**said("assistant", text, f"m{text}{ago}"), "created_at": time.time() - ago}


def following(tmp_path: Path) -> Path:
    registry = tmp_path / "registry"
    registry.write_text(f"a={tmp_path / 'a.md'}\t{tmp_path / 'a.md.brief'}\tfollow\n")
    return registry


def posted(og: Fleet) -> list[str]:
    return [t for s, t in og.inbox if s == "cos"]


def fyis(og: Fleet) -> list[str]:
    """The lines posted as without --console, which --fyi post keeps."""
    return [t for t in posted(og) if t.startswith("Supervisor: ")]


def skewed(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The wall and monotonic clocks, ahead of the real ones by the number in the returned list."""
    offset, real, mono = [0.0], time.time, time.monotonic
    monkeypatch.setattr(omnigent_agent.time, "time", lambda: real() + offset[0])
    monkeypatch.setattr(omnigent_agent.time, "monotonic", lambda: mono() + offset[0])
    return offset


def test_a_followed_lead_that_ends_still_working_is_news_only_after_it_is_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    # its last message is 600 s old when the end is seen: quiet counts from that end, not from it
    og = Lead(told("brief", 800), ending("STILL WORKING: verifier running", 600))
    # it writes again at 100 s: the quiet count restarts, so 301 s from the first end is not news
    at = {1: 100.0, 2: 301.0, 3: 401.0, 4: 402.0}

    def later(poll: int) -> None:
        offset[0] = at[poll]
        if poll == 1:
            og.histories["a"].append(ending("STILL WORKING: verifier running", 0))

    supervised(og, registry, monkeypatch, 5, later, notify="cos")
    assert len(posted(og)) == 1  # once for the message, not once per poll
    assert re.fullmatch(
        r"Supervisor: a t-a: turn ended, quiet 30\ds\nlast message: STILL WORKING: verifier running",
        posted(og)[0],
    )
    assert "follow" in registry.read_text()


@pytest.mark.parametrize("word", ["DONE", "USER NEEDED", "PARKED"])
def test_a_followed_lead_ending_with_a_named_ending_is_news_at_once_and_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, word: str
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 60), ending(f"**{word}** the result", 1))
    supervised(og, registry, monkeypatch, 3, notify="cos")
    turn = (
        omnigent_agent.UNASKED_LINE if word == "USER NEEDED" else "turn ended"
    )  # no card is open (#1551)
    assert posted(og) == [f"Supervisor: a t-a: {turn}\nlast message: **{word}** the result"]


def test_a_live_child_updated_within_the_window_keeps_a_followed_lead_quiet_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    og = Lead(told("brief", 800), ending("STILL WORKING: its worker runs", 400))
    og.kids["a"] = time.time() + 100  # its worker wrote at 100, after the end seen at 0
    at = {1: 350.0, 2: 399.0, 3: 401.0}  # 250 s, then 299 s, then 301 s since the worker wrote
    before: list[int] = []  # notices after the looks at 0, 350 and 399

    def later(poll: int) -> None:
        before.append(len(posted(og)))
        offset[0] = at[poll]

    supervised(og, registry, monkeypatch, 4, later, notify="cos")
    assert before == [0, 0, 0]  # past the end's 300 s at 350 and 399, but the child is younger
    assert len(posted(og)) == 1  # news only at 401: the child, not the end, held it


def test_a_named_ending_already_told_stays_told_while_the_runner_flaps_to_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 60), ending("DONE: finished", 1))
    states = {1: "running", 2: "idle", 3: "idle"}

    def flap(poll: int) -> None:
        og.snap = {"status": states.get(poll, "idle")}
        if poll == 4:
            og.histories["a"].append(ending("DONE: again", 0))  # a new message, a new ending

    supervised(og, registry, monkeypatch, 6, flap, notify="cos")
    assert [p.splitlines()[1] for p in posted(og)] == [
        "last message: DONE: finished",
        "last message: DONE: again",
    ]


def test_a_followed_leads_prompt_posts_once_it_holds_a_failure_at_once_and_archived_drops_its_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    og = Lead(told("brief", 60), ending("STILL WORKING", 5))
    blocked = {
        "status": "running",
        "pending_elicitations": [{"elicitation_id": "e1", "message": "ok?"}],
    }
    og.snap = blocked
    steps = [
        blocked,
        {"status": "running"},
        {"status": "failed", "last_task_error": {"message": "boom"}},
        {"status": "running"},
        {"status": "idle", "archived": True},
    ]

    def next_state(poll: int) -> None:
        og.snap = steps[poll - 1]
        if poll == 1:
            offset[0] = omnigent_agent.PROMPT_CONFIRM_SECONDS

    supervised(og, registry, monkeypatch, 6, next_state, notify="cos")
    lines = posted(og)
    assert len(lines) == 3
    assert "blocked on a prompt" in lines[0] and ": failed: boom" in lines[1]
    assert ": archived" in lines[2]
    assert registry.read_text() == ""  # archived dropped it; the other events kept it


def blocked_on(eid: str) -> dict:
    prompt = {"elicitation_id": eid, "params": {"content_preview": "Bash(ls)"}}
    return {"status": "running", "pending_elicitations": [prompt]}


def test_a_prompt_that_clears_within_the_confirm_wait_is_never_posted_nor_routed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a child's prompt Omnigent mirrors onto its lead, which the lead answers itself
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    og = Lead(told("brief", 60), ending("STILL WORKING: its worker runs", 5))
    og.snap = blocked_on("e1")
    at = {1: 30.0, 2: 200.0}

    def later(poll: int) -> None:
        offset[0] = at[poll]
        og.snap = {"status": "running"}

    supervised(og, registry, monkeypatch, 3, later, notify="cos", console=True)
    assert posted(og) == []
    events = registry.with_suffix(".events.jsonl")
    assert not events.exists() or "blocked" not in events.read_text()


def test_a_prompt_still_pending_after_the_confirm_wait_is_posted_to_the_owner_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    og = Lead(told("brief", 60), ending("STILL WORKING: its worker runs", 5))
    og.snap = blocked_on("e1")
    wait = omnigent_agent.PROMPT_CONFIRM_SECONDS
    at = {1: 30.0, 2: wait - 1, 3: wait, 4: wait + 600}
    before: list[int] = []

    def later(poll: int) -> None:
        before.append(len(posted(og)))
        offset[0] = at[poll]

    supervised(og, registry, monkeypatch, 5, later, notify="cos", console=True)
    assert before == [0, 0, 0, 1]
    [post] = posted(og)
    assert "[owner] a t-a: blocked on a prompt: e1: Bash(ls) (an approval prompt" in post


def test_a_new_prompt_id_restarts_the_confirm_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a lead blocked on e1, then on e2 after e1 was answered: e2 is timed from when it is first seen
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    og = Lead(told("brief", 60), ending("STILL WORKING: its worker runs", 5))
    og.snap = blocked_on("e1")
    wait = omnigent_agent.PROMPT_CONFIRM_SECONDS
    at = {1: 90.0, 2: 90 + wait - 1, 3: 90 + wait, 4: 90 + wait + 600}
    before: list[int] = []

    def later(poll: int) -> None:
        before.append(len(posted(og)))
        offset[0] = at[poll]
        og.snap = blocked_on("e2")

    supervised(og, registry, monkeypatch, 5, later, notify="cos", console=True)
    assert before == [0, 0, 0, 1]  # e2's wait, not e1's, which would have passed by poll 3
    assert len(posted(og)) == 1


def test_without_console_the_supervisor_writes_no_state_labels_or_deadline_and_reposts_on_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    report = aged(tmp_path / "b.md", 10)
    registry.write_text(registry.read_text() + f"b={report}\n")
    offset = skewed(monkeypatch)
    og = Lead(told("brief", 60), ending("STILL WORKING: on it", 50))
    og.histories["b"] = [told("brief", 60)]
    og.snap = {"status": "running"}  # stuck: --console would raise its deadline at 1300 s
    supervised(og, registry, monkeypatch, 2, lambda poll: offset.__setitem__(0, 1300), notify="cos")
    assert posted(og) == [f"Supervisor: b t-b: report written: {report}"]
    og.inbox.clear()
    og.snap = {"status": "idle"}
    og.histories["a"].append(ending("DONE: shipped", 0))
    for _ in range(2):  # a restart posts an end it finds again
        supervised(og, registry, monkeypatch, 1, notify="cos")
    assert posted(og) == ["Supervisor: a t-a: turn ended\nlast message: DONE: shipped"] * 2
    assert og.labels == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["b.md", "registry"]


def state_of(registry: Path) -> dict:
    return json.loads(registry.with_suffix(".state.json").read_text())


def test_one_poll_writes_the_state_and_labels_and_an_unchanged_poll_within_a_minute_writes_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    owing = tmp_path / "b.md"
    registry.write_text(registry.read_text() + f"b={owing}\n")
    offset = skewed(monkeypatch)
    og = Lead(told("brief", 60), ending("DONE: shipped\nPR #1 merged", 5))
    og.histories["b"] = [told("brief", 60)]
    og.snap = {"host_id": "h"}
    at = {1: 30.0, 2: 61.0}
    snapshots: list[dict] = []

    def later(poll: int) -> None:
        snapshots.append(state_of(registry))
        offset[0] = at[poll]

    supervised(og, registry, monkeypatch, 3, later, console=True)
    first = snapshots[0]
    assert first["schema"] == "cos-state/1" and first["written_at"] >= time.time() - 120
    lead = first["leads"]["a"]
    assert {k: lead[k] for k in ("title", "host", "report", "follow", "state", "wait")} == {
        "title": "t-a",
        "host": "here",
        "report": str(tmp_path / "a.md"),
        "follow": True,
        "state": "idle",
        "wait": "unknown",
    }
    assert (lead["ending"], lead["last_line"], lead["waits_on"]) == ("DONE", "DONE: shipped", [])
    assert (lead["nudges"], lead["revives"], lead["briefs"]) == (
        0,
        0,
        [str(tmp_path / "a.md.brief")],
    )
    assert first["leads"]["b"]["report"] == str(owing) and first["leads"]["b"]["follow"] is False
    assert first["settled"]["a"] == ["ended:a:mDONE: shipped\nPR #1 merged5"]
    assert (first["intents"], first["attention"], first["pending_posts"]) == ([], [], [])
    assert first["rate"]["next_post_at"] > time.time()  # its DONE printed as a post would go
    a_labels = [labels for sid, labels in og.labels if sid == "a"]
    assert [labels["status.line"] for labels in a_labels] == ["turn ended: DONE: shipped"] * 2
    assert int(a_labels[1]["status.at"]) - int(a_labels[0]["status.at"]) >= 60  # none at 30 s
    assert lead["label"] == {"line": a_labels[0]["status.line"], "at": a_labels[0]["status.at"]}
    events = [json.loads(e) for e in registry.with_suffix(".events.jsonl").read_text().splitlines()]
    assert [(e["id"], e["session"], e["kind"], e["settled"], e["action"]) for e in events] == [
        (first["settled"]["a"][0], "a", "ended", "rule", "done")
    ]


def test_the_console_labels_its_own_session_as_the_chief_of_staff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "cos1")
    og = Lead(told("brief", 60))
    og.snap = {"host_id": "h"}
    supervised(og, following(tmp_path), monkeypatch, 1, console=True)
    assert og.labels[0] == ("cos1", {"omni.role": "cos"})

    og = Lead(told("brief", 60))
    og.snap, og.unlabelled = {"host_id": "h"}, 1
    supervised(og, following(tmp_path), monkeypatch, 2, console=True)  # a failed write: it runs on
    assert "cos1: role label not written" in capsys.readouterr().err


def test_the_console_labels_a_project_manager_by_its_registry_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "me1")
    for name, labels in (
        ("agent-skills-leads.registry", {"omni.role": "pm", "omni.repos": "agent-skills"}),
        ("leads.registry", {"omni.role": "cos"}),
    ):
        registry = tmp_path / name
        registry.write_text(f"a={tmp_path / 'a.md'}\t{tmp_path / 'a.md.brief'}\tfollow\n")
        og = Lead(told("brief", 60))
        og.snap = {"host_id": "h"}
        supervised(og, registry, monkeypatch, 1, console=True)
        assert og.labels[0] == ("me1", labels), name


def test_a_status_line_stays_under_200_characters() -> None:
    lead = {"state": "idle", "wait": "verifier", "last_line": "x" * 300}
    line = omnigent_agent.status_line(lead)
    assert len(line) == 199 and line.startswith("turn ended · waits on verifier: xx")


def test_a_restart_raises_no_settled_event_again_and_keeps_budgets_and_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    og = Lead(told("brief", 800), ending("STILL WORKING: carrying on", 600))
    at = {1: 301.0, 2: 601.0}  # its last message was 600 s old at the start
    supervised(
        og,
        registry,
        monkeypatch,
        3,
        lambda poll: offset.__setitem__(0, at[poll]),
        notify="cos",
        console=True,
        fyi="post",
    )
    assert [p.split("\n")[0] for p in fyis(og)] == [
        "Supervisor: a t-a: turn ended, quiet 301s",
        "Supervisor: a t-a: no progress 20m (unknown)",
    ]
    state = state_of(registry)
    progress = state["leads"]["a"]["progress_at"]
    state["leads"]["a"].update(nudges=1, revives=1)  # what the routing will count
    registry.with_suffix(".state.json").write_text(json.dumps(state))
    og.inbox.clear()
    at = {1: 1205.0, 2: 1210.0}  # the end found again at 601; at 1205, 30 min since the progress
    supervised(
        og,
        registry,
        monkeypatch,
        3,
        lambda poll: offset.__setitem__(0, at[poll]),
        notify="cos",
        console=True,
        fyi="post",
    )
    assert [p.split("\n")[0] for p in fyis(og)] == [
        "Supervisor: a t-a: no progress 30m after a nudge (unknown)"
    ]
    lead = state_of(registry)["leads"]["a"]
    assert (lead["nudges"], lead["revives"], lead["progress_at"]) == (1, 1, progress)


def test_a_lead_stuck_running_raises_one_deadline_for_its_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    og = Lead(told("brief", 60), ending("STILL WORKING: on it", 0))
    og.snap = {"status": "running"}
    at = {1: 1199.0, 2: 1201.0, 3: 1300.0, 4: 2500.0}

    def later(poll: int) -> None:
        offset[0] = at[poll]
        og.refuse = 2 if poll == 1 else og.refuse  # its post fails at two polls: it waits, once

    supervised(og, registry, monkeypatch, 5, later, notify="cos", console=True, fyi="post")
    assert fyis(og) == [  # raised once each: the deadline, then (nudged at it) the one after
        "Supervisor: a t-a: no progress 20m (unknown)\nlast message: STILL WORKING: on it",
        "Supervisor: a t-a: no progress 42m after a nudge (unknown)\nlast message: STILL WORKING: on it",
    ]
    assert state_of(registry)["settled"]["a"][0].startswith("deadline:a:")


def test_the_progress_deadline_ignores_heartbeats_as_stuck_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    skewed(monkeypatch)
    beat = {"id": "hb", "type": "heartbeat", "created_at": time.time()}
    og = Lead(told("brief", 3000), ending("STILL WORKING: on it", 1300), beat)
    og.snap = {"status": "running"}
    supervised(
        og, registry, monkeypatch, 1, lambda poll: None, notify="cos", console=True, fyi="post"
    )
    assert [p.split("\n")[0] for p in fyis(og)] == ["Supervisor: a t-a: no progress 22m (unknown)"]


@pytest.mark.parametrize(
    ("said_ago", "child_ago", "deadline"),
    [
        (3600.0, 0.0, "no progress 60m (unknown)"),
        (3600.0, 1300.0, "no progress 22m (unknown)"),
        (0.0, 0.0, None),  # no activity at all: progress counts from first sight
    ],
)
def test_progress_starts_from_the_leads_real_activity_not_from_first_sight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    said_ago: float,
    child_ago: float,
    deadline: str | None,
) -> None:
    registry = following(tmp_path)
    items = [ending("STILL WORKING: building", said_ago)] if said_ago else []
    og = Lead(told("brief", 4000), *items)
    og.snap = {"status": "running"}
    if child_ago:
        og.kids["a"] = time.time() - child_ago
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True, fyi="post")
    assert [p.split("\n")[0] for p in fyis(og)] == (
        [f"Supervisor: a t-a: {deadline}"] if deadline else []
    )
    progress = state_of(registry)["leads"]["a"]["progress_at"]
    if items:
        assert progress == max(items[-1]["created_at"], og.kids.get("a", 0.0))
    else:
        assert time.time() - 60 < progress <= time.time()


@pytest.mark.parametrize(
    ("written_ago", "deadline"),
    [
        (1300.0, "no progress 22m (merge)"),  # written after its last message: counts from then
        (100.0, None),  # written while its messages were quiet: progress, no deadline
        (None, "no progress 60m (merge)"),  # not listed
        (omnigent_agent.Fail("GET: HTTP 409"), "no progress 60m (merge)"),  # its host is offline
    ],
)
def test_a_report_being_written_is_progress_once_the_lead_is_due(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    written_ago: float | Exception | None,
    deadline: str | None,
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 4000), ending("STILL WORKING: merge queue", 3600))
    og.snap = {"status": "running"}
    report = str(tmp_path / "a.md")
    if written_ago is not None:
        og.written[report] = (
            written_ago if isinstance(written_ago, Exception) else time.time() - written_ago
        )
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True, fyi="post")
    assert [p.split("\n")[0] for p in fyis(og)] == (
        [f"Supervisor: a t-a: {deadline}"] if deadline else []
    )
    assert og.stats == [report]
    if written_ago == 100.0:
        assert state_of(registry)["leads"]["a"]["progress_at"] == og.written[report]


# A claude-cloud session's record as the server gives it: its own harness is the transport, and the
# cloud harness is its agent's (#706).
CLOUD_RECORD = {"harness": "acp", "agent_name": "claude-cloud", "agent_id": "agt_cloud"}


def test_a_due_cloud_lead_never_stats_its_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its file stays in the VM: a stat here would read a stale host file, or fail (#706)."""
    registry = following(tmp_path)
    og = Lead(told("brief", 4000), ending("STILL WORKING: merge queue", 3600))
    og.snap = {"status": "running", **CLOUD_RECORD}
    og.written[str(tmp_path / "a.md")] = time.time() - 100  # a host file would read as progress
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True, fyi="post")
    assert og.stats == []
    assert [p.split("\n")[0] for p in fyis(og)] == ["Supervisor: a t-a: no progress 60m (merge)"]


def test_a_lead_not_yet_due_has_its_report_left_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 120), ending("STILL WORKING: merge queue", 60))
    og.snap = {"status": "running"}
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True, fyi="post")
    assert og.stats == [] and fyis(og) == []


LIVE_TIMED = (
    "STILL WORKING: waiting on the day of real traffic that measures CoS wakes per lead event. A timer"
    " (`cos-measure-day1`) runs the count at 2026-10-21 23:17 UTC and wakes this session"
)


@pytest.mark.parametrize(("due_in", "woken"), [(3600.0, False), (-60.0, True), (-1300.0, True)])
def test_a_timed_wait_wakes_the_lead_once_its_time_has_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, due_in: float, woken: bool
) -> None:
    assert omnigent_agent.wait_of(LIVE_TIMED) == "until 2026-10-21T23:17Z"
    # its named wait, not "lead" from "per lead event"
    assert omnigent_agent.wait_of(LIVE_TIMED.split(". A timer")[0]) == "day"
    # the lead's real last message: a bare time, and "lead" in "wakes this lead"
    real = "/r/a.md\n\nSTILL WORKING: waiting on the timer (2026-10-21 23:17 UTC), which wakes this lead"
    assert omnigent_agent.wait_of(real) == "until 2026-10-21T23:17Z"
    assert (
        omnigent_agent.wait_of(real.replace(" (2026-10-21 23:17 UTC)", "")) == "timer"
    )  # not "lead"
    assert (
        omnigent_agent.wait_of("STILL WORKING: until 2026-10-21T23:17:59Z")
        == "until 2026-10-21T23:17:59Z"
    )
    assert omnigent_agent.wait_of("STILL WORKING: until 2026-02-30T23:17Z") == "unknown"
    # lead 99a79f4d's message: a rule's "Until <time>" comes before the time its wait names
    rule = (
        "STILL WORKING: I've recorded the temporary review rule in the run notes. Until 2026-10-27"
        " 06:32 UTC, new verifiers run as Claude Opus with `--same-family`. I'm waiting on the"
        " measurement timer `cos-measure-day1`, which fires at 2026-10-21 23:17 UTC."
    )
    assert omnigent_agent.wait_of(rule) == "until 2026-10-21T23:17Z"
    # no sentence says "wait": the earliest still to come when written, else the earliest
    two = "STILL WORKING: a timer wakes me at 2026-10-21 23:17 UTC. The trial ends 2026-10-27 06:32 UTC."
    written = 1792540800.0  # 2026-10-21 00:00Z
    assert omnigent_agent.wait_of(two, written) == "until 2026-10-21T23:17Z"
    assert omnigent_agent.wait_of(two) == "until 2026-10-21T23:17Z"
    past = "STILL WORKING: a timer wakes me at 2026-10-27 06:32 UTC. The last run ended 2026-10-21 10:00 UTC."
    assert omnigent_agent.wait_of(past, written + 12 * 3600) == "until 2026-10-27T06:32Z"
    item = {**ending(past, 0), "created_at": written + 12 * 3600}  # fixed: no wall clock
    status = omnigent_agent.lead_status({"status": "idle"}, [item])
    assert status["wait"] == "until 2026-10-27T06:32Z"  # judged at its created_at, not later
    listed = "STILL WORKING:\n- the rule holds until 2026-10-27 06:32 UTC\n- waiting on the timer at 2026-10-21 23:17 UTC"
    assert omnigent_agent.wait_of(listed) == "until 2026-10-21T23:17Z"  # a line is a sentence
    assert omnigent_agent.wait_of("At 2026-10-21 23:17 UTC. STILL WORKING on CI") == "CI"
    due = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(time.time() + due_in))
    registry = following(tmp_path)
    og = Lead(told("brief", 4000), ending(f"STILL WORKING: a timer wakes me until {due}", 3600))
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True, fyi="post")
    assert [p.split("\n")[0] for p in fyis(og)] == (
        [f"Supervisor: a t-a: no progress 60m (until {due})"] if woken else []
    )
    wake = omnigent_agent.LEAD_WAKE.format(time=due)
    assert [t for s, t in og.inbox if s == "a"] == ([wake] if woken else [])


def timed(due_in: float, ago: float) -> tuple[str, Lead]:
    due = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(time.time() + due_in))
    return due, Lead(
        told("brief", 4000), ending(f"STILL WORKING: a timer wakes me until {due}", ago)
    )


def test_a_timed_wait_wakes_at_its_time_then_raises_one_deadline_twenty_minutes_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its time comes before DEADLINE_SECONDS from its progress: the wake is due at the time."""
    base = 1800000000.0  # a whole minute, so the named time is exact
    clock = [base]
    monkeypatch.setattr(omnigent_agent.time, "time", lambda: clock[0])
    monkeypatch.setattr(omnigent_agent.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(omnigent_agent, "triage", lambda *args: ("none", "not asked"))
    registry = following(tmp_path)
    due, og = timed(60.0, 10)  # its time a minute ahead, its progress 10s old
    moments = {1: 59.0, 2: 60.0, 3: 1259.0, 4: 1260.0, 5: 1400.0}
    seen = []
    log = registry.with_suffix(".events.jsonl")
    deadlines = lambda: [
        e["action"]
        for e in (events_of(registry) if log.exists() else [])
        if e["kind"] == "deadline"
    ]

    def later(poll: int) -> None:
        seen.append(deadlines())
        clock[0] = base + moments[poll]

    supervised(og, registry, monkeypatch, 6, later, notify="cos", console=True)
    seen.append(deadlines())
    # polled at 0, 59, 60, 1259, 1260 and 1400s: nothing before its time, one wake at it, nothing
    # more before DEADLINE_SECONDS after it, then one deadline for the chief of staff
    assert seen == [[], [], ["wake"], ["wake"], ["wake", "act"], ["wake", "act"]]
    assert len(sent := [t for s, t in og.inbox if s == "a"]) == 1 and due[11:16] in sent[0]


def test_progress_after_a_timed_waits_time_raises_no_wake_and_counts_from_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    due, og = timed(-120.0, 10)  # it wrote after its time
    supervised(og, registry, monkeypatch, 3, lambda poll: offset.__setitem__(0, 1000.0 + 100 * poll),
               notify="cos", console=True, fyi="post")  # fmt: skip
    lines = [p.split("\n")[0] for p in fyis(og)]
    # nothing at 1100s, before DEADLINE_SECONDS from the progress; at 1200s the deadline, no wake
    assert [p for p in lines if "no progress" in p] == [
        f"Supervisor: a t-a: no progress 20m (until {due})"
    ]
    assert [t for s, t in og.inbox if s == "a"] == []


def test_an_offline_lead_whose_time_passed_is_revived_not_woken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    due, og = timed(-60.0, 3600)
    og.snap = {"runner_online": False}
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True, fyi="post")
    assert og.retried == ["a"]
    assert omnigent_agent.LEAD_WAKE.format(time=due) not in [t for s, t in og.inbox if s == "a"]


def test_a_background_task_id_is_a_named_wait() -> None:
    """`b` and 8 lowercase letters and digits, one a digit at least, opening the wait or after "on"/"for"."""
    assert omnigent_agent.wait_of("STILL WORKING bz3jp5aod") == "bz3jp5aod"
    assert omnigent_agent.wait_of("STILL WORKING bz3jp5aod: the queue runs for #12") == "bz3jp5aod"
    assert omnigent_agent.wait_of("STILL WORKING: waiting on bz3jp5aod") == "bz3jp5aod"
    assert omnigent_agent.wait_of("STILL WORKING: on it") == "unknown"
    assert omnigent_agent.wait_of("STILL WORKING backports") == "unknown"
    # ten characters: no task id
    assert omnigent_agent.wait_of("STILL WORKING on bz3jp5aodx") == "unknown"


def test_a_childs_activity_reaches_the_state_once_a_minute_before_any_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    og = Lead(told("brief", 60), ending("STILL WORKING: waiting on worker", 50))
    og.snap = {"status": "running"}
    start, reads, progress = time.time(), [], []
    real = og.active
    og.active = lambda sid: reads.append(sid) or real(sid)  # type: ignore[method-assign]
    at = {1: 30.0, 2: 61.0}

    def later(poll: int) -> None:
        progress.append(state_of(registry)["leads"]["a"]["progress_at"])
        offset[0] = at[poll]
        og.kids["a"] = start + 20 * poll  # its worker writes at 20, then at 40

    supervised(og, registry, monkeypatch, 3, later, console=True)
    progress.append(state_of(registry)["leads"]["a"]["progress_at"])
    assert reads == ["a", "a"]  # at 0 and at 61, none at 30
    assert progress[1] == progress[0] and progress[2] == start + 40


def test_the_same_failure_again_after_a_recovery_is_news_and_a_restart_posts_neither_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 60), ending("STILL WORKING: building", 50))
    og.snap = {"status": "failed", "last_task_error": {"message": "the gate found a failing test"}}

    def recover_then_fail(poll: int) -> None:
        og.snap["status"] = "running" if poll == 1 else "failed"

    supervised(
        og, registry, monkeypatch, 3, recover_then_fail, notify="cos", console=True, fyi="post"
    )
    failures = [p.split("\n")[0] for p in fyis(og)]
    assert failures == ["Supervisor: a t-a: failed: the gate found a failing test"] * 2
    og.inbox.clear()
    supervised(
        og, registry, monkeypatch, 2, notify="cos", console=True, fyi="post"
    )  # a restart, still failed
    assert fyis(og) == []
    assert [i for i in state_of(registry)["settled"]["a"] if i.startswith("failed:")] == [
        "failed:a:1:the gate found a failing test",
        "failed:a:2:the gate found a failing test",
    ]


def test_a_second_quiet_spell_with_the_same_ending_raises_a_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    og = Lead(told("brief", 800), ending("STILL WORKING: waiting on the verifier", 600))
    steps = {1: (301.0, "idle"), 2: (400.0, "running"), 3: (500.0, "idle"), 4: (601.0, "idle")}

    def later(poll: int) -> None:
        offset[0], status = steps[poll]
        og.snap = {"status": status}

    supervised(og, registry, monkeypatch, 5, later, notify="cos", console=True, fyi="post")
    assert [p.split("\n")[0] for p in fyis(og)] == [
        "Supervisor: a t-a: turn ended, quiet 301s",  # the first spell, told once
        "Supervisor: a t-a: no progress 20m (verifier)",  # the second, the same message
    ]


@pytest.mark.parametrize("status", ["idle", "running"])  # running: its turn end was lost
def test_a_parked_lead_has_no_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    og = Lead(
        told("brief", 60),
        told(omnigent_agent.PARK + "the host is full", 30),
        ending("PARKED: the host is full", 5),
    )
    og.snap = {"status": status}
    at = {1: 1300.0, 2: 5000.0}
    supervised(
        og,
        registry,
        monkeypatch,
        3,
        lambda poll: offset.__setitem__(0, at[poll]),
        notify="cos",
        console=True,
        fyi="post",
    )
    ends = ["Supervisor: a t-a: turn ended"] if status == "idle" else []
    assert [p.split("\n")[0] for p in fyis(og)] == ends
    assert state_of(registry)["leads"]["a"]["wait"] == "parked"


def test_a_lead_that_parked_itself_is_seen_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    skewed(monkeypatch)
    og = Lead(told("brief", 60), ending("PARKED: waiting for the owner's pick", 5))
    supervised(
        og, registry, monkeypatch, 1, lambda poll: None, notify="cos", console=True, fyi="post"
    )
    assert [(e["action"], e["why"]) for e in events_of(registry) if e["kind"] == "ended"] == [
        ("act", "ended PARKED with no park message before it")
    ]


@pytest.mark.parametrize(
    ("early", "action"),
    [
        (lambda: [told(omnigent_agent.PARK + "the host is full", 30)], "record"),
        (
            lambda: [told("[relayed by m]\n" + omnigent_agent.PARK + "the host is full", 30)],
            "record",
        ),
        # the park line behind 100 later messages is out of the newest page of 100 items
        (
            lambda: (
                [told(omnigent_agent.PARK + "full", 900)]
                + [ending(f"STILL WORKING: step {n}", 800 - n) for n in range(100)]
            ),
            "record",
        ),
        (
            lambda: (
                [told(omnigent_agent.PARK + "full", 900)]
                + [ending(f"STILL WORKING: step {n}", 800 - n) for n in range(250)]
            ),
            "record",
        ),
        # a worker's notice, or a line quoted inside a message, is not the manager's park
        (
            lambda: [
                told("[relay from w: not the owner's words]\n" + omnigent_agent.PARK + "x", 30)
            ],
            "act",
        ),
        (lambda: [told("fyi the manager wrote:\n" + omnigent_agent.PARK + "x", 30)], "act"),
        (list, "act"),
    ],
)
def test_only_the_managers_park_message_makes_a_parked_ending_routine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, early: Callable[[], list[dict]], action: str
) -> None:
    registry = following(tmp_path)
    skewed(monkeypatch)

    class Paged(Lead):  # the server's newest page holds 100 items
        def items(self, sid: str) -> list[dict]:
            return self.histories[sid][-100:]

    og = Paged(told("brief", 1000), *early(), ending("PARKED: the host is full", 5))
    supervised(
        og, registry, monkeypatch, 1, lambda poll: None, notify="cos", console=True, fyi="post"
    )
    assert [e["action"] for e in events_of(registry) if e["kind"] == "ended"] == [action]


def test_a_park_still_queued_does_not_excuse_an_earlier_self_park(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    skewed(monkeypatch)
    og = Lead(told("brief", 60), ending("PARKED: waiting for the owner's pick", 5))
    og.snap = {"pending_inputs": [{"content": [{"type": "x", "text": omnigent_agent.PARK + "x"}]}]}
    supervised(
        og, registry, monkeypatch, 1, lambda poll: None, notify="cos", console=True, fyi="post"
    )
    assert [e["action"] for e in events_of(registry) if e["kind"] == "ended"] == ["act"]


@pytest.mark.parametrize(("parked", "action"), [(True, "record"), (False, "act")])
def test_a_report_path_lead_is_parked_by_the_report_and_the_park_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parked: bool, action: str
) -> None:
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    path = str(tmp_path / "a.md")
    early = [told(omnigent_agent.PARK + "the host is full", 30)] if parked else []
    og = Lead(told("brief", 60), *early, ending(path, 5))
    og.files[path] = "PARKED: the host is full"
    supervised(
        og,
        registry,
        monkeypatch,
        3,
        lambda poll: offset.__setitem__(0, 2000.0 * poll),
        notify="cos",
        console=True,
        fyi="post",
    )
    assert [e["action"] for e in events_of(registry) if e["kind"] == "ended"] == [action]


@pytest.mark.parametrize("task", ["monitor", "shell"])
def test_a_lead_that_ended_with_only_a_monitor_live_has_no_deadline_and_a_shell_keeps_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task: str
) -> None:
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    reply = {**ending("DONE: the page is published", 60), "status": "completed"}
    og = Lead(told("brief", 120), reply)
    og.snap = {"status": "running", "background_tasks": [{"type": task, "status": "running"}]}
    at = {1: 1300.0, 2: 5000.0}
    supervised(
        og,
        registry,
        monkeypatch,
        3,
        lambda poll: offset.__setitem__(0, at[poll]),
        notify="cos",
        console=True,
        fyi="post",
    )
    firsts = [p.split("\n")[0] for p in fyis(og)]
    if task == "monitor":  # its turn ended: told once, then it waits on its follower
        assert firsts == ["Supervisor: a t-a: turn ended"]
    else:  # a shell is work in flight: watched as running, so the deadline still holds
        assert firsts and all("no progress" in f for f in firsts)


def test_a_live_child_is_progress_and_the_deadline_counts_from_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    og = Lead(told("brief", 60), ending("STILL WORKING: its worker runs", 50))
    og.snap = {"status": "running"}
    og.kids["a"] = time.time() + 1100  # its worker wrote at 1100
    at = {1: 1201.0, 2: 2299.0, 3: 2301.0}
    before: list[int] = []

    def later(poll: int) -> None:
        before.append(len(fyis(og)))
        offset[0] = at[poll]

    supervised(og, registry, monkeypatch, 4, later, notify="cos", console=True, fyi="post")
    assert before == [0, 0, 0]
    assert [p.split("\n")[0] for p in fyis(og)] == ["Supervisor: a t-a: no progress 20m (worker)"]


def path_only_lead(tmp_path: Path, report: str | None) -> tuple[Path, Lead, list[str]]:
    """A followed lead whose last message is only its report's path; ``report`` is the file's text,
    or None for one that cannot be read. The list holds each read of the file."""
    registry = following(tmp_path)
    path = str(tmp_path / "a.md")
    og = Lead(told("brief", 60), ending(path, 5))
    reads: list[str] = []
    if report is not None:
        og.files[path] = report
    real = og.read_file

    def read_file(sid: str, p: str) -> str:
        if p == path:  # the report's reads, not the landing watch's look for a checkout
            reads.append(p)
        return real(sid, p)

    og.read_file = read_file  # type: ignore[method-assign]
    return registry, og, reads


def test_a_path_only_lead_whose_report_says_done_raises_no_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, og, _ = path_only_lead(tmp_path, "DONE: merged\nmore")
    offset = skewed(monkeypatch)
    supervised(
        og,
        registry,
        monkeypatch,
        4,
        lambda poll: offset.__setitem__(0, 1300.0 * poll),
        notify="cos",
        console=True,
    )
    assert "deadline" not in [e["kind"] for e in events_of(registry)]
    assert state_of(registry)["leads"]["a"]["ending"] == "DONE"


def test_a_path_only_lead_whose_report_names_its_wait_shows_it_in_state_and_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, og, reads = path_only_lead(tmp_path, "STILL WORKING: waiting on the verifier\n")
    supervised(og, registry, monkeypatch, 3, notify="cos", console=True)
    lead = state_of(registry)["leads"]["a"]
    assert lead["wait"] == "verifier"
    assert "waits on verifier" in lead["label"]["line"]
    assert len(reads) == 1


def test_a_reports_wait_is_judged_from_when_its_path_was_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    later = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(time.time() + 6 * 86400))
    past = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(time.time() - 2 * 3600))
    report = f"STILL WORKING: a timer wakes me at {later}. The last run ended {past}.\n"
    registry, og, _ = path_only_lead(tmp_path, report)
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True)
    due = later.replace(" ", "T", 1).replace(" UTC", "Z")
    assert state_of(registry)["leads"]["a"]["wait"] == f"until {due}"


def test_a_new_last_message_is_read_again_and_an_unread_report_stays_unknown_and_is_logged_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry, og, reads = path_only_lead(tmp_path, None)
    supervised(og, registry, monkeypatch, 3, notify="cos", console=True)
    assert state_of(registry)["leads"]["a"]["wait"] == "unknown"
    assert capsys.readouterr().err.count("not found") == 1
    assert len(reads) == 1
    path = str(tmp_path / "a.md")
    og.files[path] = "STILL WORKING: waiting on CI"
    og.histories["a"].append(ending(path + " ", 1))  # a new last message, the same path
    supervised(og, registry, monkeypatch, 2, notify="cos", console=True)
    assert state_of(registry)["leads"]["a"]["wait"] == "CI"
    assert len(reads) == 2


def test_a_failed_read_stands_for_routing_too_and_the_quiet_end_acts_without_reading_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, og, reads = path_only_lead(tmp_path, None)
    offset = skewed(monkeypatch)

    def later(poll: int) -> None:
        og.files[str(tmp_path / "a.md")] = "DONE: merged"  # readable after the first failure
        offset[0] = 301.0

    supervised(og, registry, monkeypatch, 3, later, console=True)
    assert len(reads) == 2  # track's failed one, and the post's own report lines; routing's none
    assert state_of(registry)["leads"]["a"]["wait"] == "unknown"
    [ended] = [e for e in events_of(registry) if e["kind"] == "ended"]
    assert ended["action"] == "act" and "not read" in ended["why"]


def test_a_cloud_lead_routes_on_its_last_message_and_reads_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its report file stays in the VM: the ending and wait come from the message (#706)."""
    registry, og, reads = path_only_lead(tmp_path, "DONE: a host file the cloud never wrote")
    og.snap = dict(CLOUD_RECORD)
    text = "# R\n\n> DONE: an old line, quoted\n\nSTILL WORKING: waiting on the verifier.\n"
    og.histories["a"].append(ending(text, 1))
    supervised(og, registry, monkeypatch, 2, notify="cos", console=True)
    lead = state_of(registry)["leads"]["a"]
    assert lead["cloud"] and lead["ending"] is None and lead["wait"] == "verifier"
    assert (
        lead["report_read"]["head"].startswith("STILL WORKING") and not lead["report_read"]["error"]
    )
    assert reads == [] and str(tmp_path / "a.md") not in og.stats


def test_a_cloud_lead_whose_last_message_is_only_a_path_stays_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, og, reads = path_only_lead(tmp_path, "DONE: merged")
    og.snap = dict(CLOUD_RECORD)
    supervised(og, registry, monkeypatch, 2, notify="cos", console=True)
    lead = state_of(registry)["leads"]["a"]
    assert reads == [] and lead["wait"] == "unknown" and lead["report_read"]["head"] is None
    assert "stays in its VM" in lead["report_read"]["error"]


def test_without_console_a_path_only_lead_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, og, reads = path_only_lead(tmp_path, "DONE: merged")
    supervised(og, registry, monkeypatch, 2, notify="cos")
    assert reads == []


def test_a_reader_never_sees_a_half_written_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 60), ending("DONE: shipped", 5))
    real, renames = os.replace, []

    def replace(src: Path, dst: Path) -> None:
        assert Path(src).parent == Path(dst).parent  # a rename, never a copy across filesystems
        json.loads(Path(src).read_text())  # complete before it takes the name
        if Path(dst).exists():
            json.loads(Path(dst).read_text())  # what a reader sees meanwhile: the old one, whole
        renames.append(dst)
        real(src, dst)

    monkeypatch.setattr(omnigent_agent.os, "replace", replace)
    supervised(og, registry, monkeypatch, 2, console=True)
    assert set(renames) == {registry.with_suffix(".state.json")} and len(renames) >= 2
    assert not list(tmp_path.glob("*.tmp"))


def test_a_failed_label_write_is_logged_once_and_the_poll_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 60), ending("DONE: shipped", 5))
    og.unlabelled = 2
    supervised(og, registry, monkeypatch, 3, notify="cos", console=True, fyi="post")
    assert [p.split("\n")[0] for p in fyis(og)] == ["Supervisor: a t-a: turn ended"]
    assert og.labels == [
        (
            "a",
            {"status.line": "turn ended: DONE: shipped", "status.at": og.labels[0][1]["status.at"]},
        )
    ]
    err = capsys.readouterr().err
    assert err.count("a: labels not written: PATCH /v1/sessions/a: HTTP 503") == 1


def test_a_followed_lead_is_never_revived_or_nudged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 9000))  # silent far past --silent-seconds, its runner gone
    og.offline.add("a")
    supervised(og, registry, monkeypatch, 3, notify="cos", silent=1)
    assert og.inbox == [] and og.retried == []


def test_a_top_level_start_with_a_registry_arms_a_follow_line_and_does_not_subscribe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.setattr(omnigent_agent, "own_id", lambda: "chief")
    monkeypatch.chdir(tmp_path)
    registry, brief = tmp_path / "registry", tmp_path / "b.md"
    brief.write_text("do X")
    args = ["start", "claude", "t", str(brief), str(tmp_path / "a.md"), "--top-level"]
    assert omnigent_agent.main([*args, "--registry", str(registry)]) == 0
    assert registry.read_text() == f"s0={tmp_path / 'a.md'}\t{tmp_path / 'a.md.brief'}\tfollow\n"
    assert og.subscriptions == []
    args[4] = str(tmp_path / "c.md")
    assert omnigent_agent.main(args) == 0  # no registry: subscribes as before
    assert og.subscriptions == [("chief", "s1")] and "s1" not in registry.read_text()


def test_a_failed_post_keeps_the_line_and_the_event_is_posted_once_when_a_post_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = tmp_path / "registry"
    report = aged(tmp_path / "a.md", 10)
    registry.write_text(f"a={report}\n")
    og = Fleet(a=[told("brief", 60)])
    og.refuse = 2
    kept: list[str] = []
    supervised(
        og, registry, monkeypatch, 3, lambda poll: kept.append(registry.read_text()), notify="cos"
    )
    assert kept == [f"a={report}\n"] * 2
    assert og.inbox == [("cos", f"Supervisor: a t-a: report written: {report}")]
    assert registry.read_text() == ""
    out, err = capsys.readouterr()
    assert out.count("report written") == 1
    assert re.fullmatch(f"{STAMP}post to cos failed: POST /v1/sessions/cos/events: HTTP 503\n", err)


def test_a_revive_line_whose_post_failed_is_posted_at_the_next_poll_before_newer_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "registry"
    report = tmp_path / "a.md"
    registry.write_text(f"a={report}\n")
    og = Fleet(a=[told("brief", 60)])
    og.offline.add("a")
    og.refuse = 1
    supervised(og, registry, monkeypatch, 2, lambda poll: report.write_text("x"), notify="cos")
    assert [m for m in og.inbox if m[0] == "cos"] == [
        ("cos", "Supervisor: a t-a: revived (no live runner)"),
        ("cos", f"Supervisor: a t-a: report written: {report}"),
    ]
    assert registry.read_text() == ""


def test_a_brief_armed_while_the_last_event_is_posted_keeps_its_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "registry"
    report = aged(tmp_path / "a.md", 10)
    registry.write_text(f"a={report}\n")
    og = Fleet(a=[told("brief", 60)])
    post = og.send

    def send(sid: str, text: str) -> None:  # the next brief lands and is armed mid-post
        post(sid, text)
        og.histories["a"].append(told("next brief", 0))
        omnigent_agent.arm(registry, "a", report)

    og.send = send  # type: ignore[method-assign]
    supervised(og, registry, monkeypatch, 2, notify="cos")
    assert registry.read_text() == f"a={report}\n"
    assert og.inbox == [("cos", f"Supervisor: a t-a: report written: {report}")]


def test_a_revived_session_gets_the_revive_line_naming_its_brief_files_and_its_report_still_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry, report = tmp_path / "registry", tmp_path / "a.md"
    first, then = tmp_path / "a.md.brief", tmp_path / "answer.md"
    registry.write_text(f"a={report}\t{first}\t{then}\n")
    og = Fleet(a=[told("brief", 60)])
    og.offline.add("a")

    def written_before_the_revive_line(poll: int) -> None:
        aged(report, 30)  # the report its brief asked for, then the runner was reaped
        og.histories["a"] += [told(text, 0) for sid, text in og.inbox if sid == "a"]

    supervised(og, registry, monkeypatch, 2, written_before_the_revive_line, notify="cos")
    assert [text for sid, text in og.inbox if sid == "a"] == [
        (
            "Your session was restarted and may have lost its context."
            f" Read your brief: {first}, {then}. Write your report to {report}."
        )
    ]
    assert og.inbox[-1] == ("cos", f"Supervisor: a t-a: report written: {report}")
    assert registry.read_text() == ""  # the revive line renews nothing


def test_a_revive_line_whose_send_failed_is_sent_at_the_next_poll_once_the_runner_is_online(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, report = tmp_path / "registry", tmp_path / "a.md"
    registry.write_text(f"a={report}\n")
    og = Fleet(a=[told("brief", 60)])
    og.offline.add("a")
    og.refuse = 1  # the revive succeeds, the revive line's send answers 503
    supervised(og, registry, monkeypatch, 3)
    assert [text for sid, text in og.inbox if sid == "a"] == [
        f"Your session was restarted and may have lost its context. Write your report to {report}."
    ]


@pytest.mark.parametrize("child_ago", [600, 3600])
def test_a_session_waiting_on_a_working_child_is_not_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child_ago: int
) -> None:
    """Its own history quiet for an hour: a child updated 10 min ago keeps it live, one quiet for an
    hour too lets the resume line go."""
    registry, report = tmp_path / "registry", tmp_path / "a.md"
    registry.write_text(f"a={report}\n")
    og = Fleet(a=[told("brief", 3600)])
    og.kids["a"] = time.time() - child_ago
    supervised(og, registry, monkeypatch, 1, silent=1800)
    sent = [text for sid, text in og.inbox if sid == "a"]
    assert sent == ([] if child_ago < 1800 else [omnigent_agent.NUDGE.format(report=report)])


@pytest.mark.parametrize("brief", [True, False])
def test_one_shot_supervise_names_the_brief_start_wrote_in_the_revive_line(
    tmp_path: Path, brief: bool
) -> None:
    report = tmp_path / "r.md"
    if brief:
        (tmp_path / "r.md.brief").write_text("do X")
    og = Owing([told("brief", 60)], {"runner_online": False}, {"runner_online": True}, {})
    og.infos.append({"status": "failed", "last_task_error": {"message": "later"}})
    omnigent_agent.supervise(og, {"a": report}, timeout=5)
    read = f" Read your brief: {tmp_path / 'r.md.brief'}." if brief else ""
    assert og.sent == [
        (
            f"Your session was restarted and may have lost its context.{read}"
            f" Write your report to {report}."
        )
    ]


def test_an_error_reading_one_session_is_logged_once_and_the_others_are_still_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = tmp_path / "registry"
    report = aged(tmp_path / "b.md", 10)
    registry.write_text(f"a={tmp_path / 'a.md'}\njunk\nb={report}\n")
    og = Fleet(a=[told("brief", 60)], b=[told("brief", 60)])
    og.broken["a"] = omnigent_agent.Fail("GET /v1/sessions/a: HTTP 404 Not Found")
    supervised(og, registry, monkeypatch, 3)
    assert og.reads.count("a") == 3  # the loop went on
    assert registry.read_text() == f"a={tmp_path / 'a.md'}\njunk\n"
    out, err = capsys.readouterr()
    assert out.count("b t-b: report written") == 1
    assert err.count("a: GET /v1/sessions/a: HTTP 404 Not Found") == 1
    assert err.count("'junk': expected <session>=<report>; skipped") == 1


def test_a_quiet_timeout_window_is_logged_and_the_loop_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = tmp_path / "registry"
    registry.write_text(f"a={tmp_path / 'a.md'}\n")
    og = Fleet(a=[told("brief", 60)])
    supervised(og, registry, monkeypatch, 2, timeout=0)
    assert og.reads == ["a", "a"]
    assert re.fullmatch(f"({STAMP}no event in 0s: a\n){{2}}", capsys.readouterr().err)


def test_a_notify_session_with_no_live_runner_is_revived_before_the_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "registry"
    report = aged(tmp_path / "a.md", 10)
    registry.write_text(f"a={report}\n")
    og = Fleet(a=[told("brief", 60)])
    og.offline.add("cos")
    supervised(og, registry, monkeypatch, 1, notify="cos")
    assert og.retried == ["cos"]
    assert og.inbox == [("cos", f"Supervisor: a t-a: report written: {report}")]
    assert registry.read_text() == ""


def test_start_and_run_arm_the_registry_with_their_brief_file_and_run_disarms_it_on_the_report(
    tmp_path: Path, role_file: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("do X")
    og = FakeOmnigent(reply="done")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    argv = ["start", "claude", "t", "b.md", "r.md", "--confirm-seconds", "0"]
    assert omnigent_agent.main(argv) == 0
    assert not (tmp_path / "registry").exists()  # no registry configured: nothing armed
    monkeypatch.setenv("OMNIGENT_SUPERVISE_REGISTRY", str(tmp_path / "registry"))
    assert (
        omnigent_agent.main(["start", "claude", "t2", "b.md", "s.md", "--confirm-seconds", "0"])
        == 0
    )
    s1 = f"s1={tmp_path / 's.md'}\t{tmp_path / 's.md.brief'}\n"
    assert (tmp_path / "registry").read_text() == s1
    (tmp_path / "vw").mkdir()  # a role child's own worktree: s0 and s1 use this one (#750)
    verifier = ["start", "codex", "v", "b.md", "vw/v.md", "--confirm-seconds", "0"]
    verifier += ["--workspace", "vw"]
    assert (
        omnigent_agent.main([*verifier, "--role", role_file("verifier.md"), "--author", "claude"])
        == 0
    )
    v = f"s2={tmp_path / 'vw/v.md'}\t{tmp_path / 'vw/v.md.brief'}\tverdict\n"
    assert (tmp_path / "registry").read_text() == s1 + v
    s1 += v
    seen: list[str] = []
    info = og.info

    def reporting(sid: str) -> dict:
        seen.append((tmp_path / "registry").read_text())
        (tmp_path / "u.md").write_text("ok")
        return info(sid)

    og.info = reporting  # type: ignore[method-assign]
    assert (
        omnigent_agent.main(["run", "claude", "t3", "b.md", "u.md", "--confirm-seconds", "0"]) == 0
    )
    # seen[0] is start reading the new session back to sign its brief, before it is armed
    assert seen[1] == s1 + f"s3={tmp_path / 'u.md'}\t{tmp_path / 'u.md.brief'}\n"
    assert (tmp_path / "registry").read_text() == s1


def test_send_arms_the_registry_appending_its_message_file_and_disarms_it_on_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    registry, report = tmp_path / "registry", tmp_path / "r.md"
    monkeypatch.setenv("OMNIGENT_SUPERVISE_REGISTRY", str(registry))
    og = FakeOmnigent(reply="done")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    sid = og.create("claude", "t")
    brief = tmp_path / "old.md.brief"
    registry.write_text(f"{sid}={tmp_path / 'old.md'}\t{brief}\nx={tmp_path / 'x.md'}\n")
    (tmp_path / "a.md").write_text("use main")
    seen: list[str] = []
    info = og.info

    def reporting(s: str) -> dict:
        if f"{sid}={report}" in registry.read_text():
            seen.append(registry.read_text())
            report.write_text("ok")
        return info(s)

    og.info = reporting  # type: ignore[method-assign]
    assert omnigent_agent.main(["send", sid, "a.md", "r.md", "--confirm-seconds", "0"]) == 0
    assert seen[0] == f"x={tmp_path / 'x.md'}\n{sid}={report}\t{brief}\t{tmp_path / 'a.md'}\n"
    assert registry.read_text() == f"x={tmp_path / 'x.md'}\n"


def test_wait_disarms_on_a_report_or_a_question_and_a_failed_wait_keeps_the_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    registry = tmp_path / "registry"
    og = FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    a, b, c = (og.create("claude", t) for t in "abc")
    registry.write_text(
        f"{a}={tmp_path / 'a.md'}\n{b}={tmp_path / 'b.md'}\n{c}={tmp_path / 'c.md'}\n"
    )
    (tmp_path / "a.md").write_text("ok")
    (tmp_path / "b.md.question").write_text("which?")
    flag = ["--registry", str(registry)]
    assert omnigent_agent.main(["wait", a, "a.md", *flag]) == 0
    assert omnigent_agent.main(["wait", b, "b.md", *flag]) == omnigent_agent.ASKED
    assert omnigent_agent.main(["wait", c, "c.md", "--timeout", "0", *flag]) == 1
    assert registry.read_text() == f"{c}={tmp_path / 'c.md'}\n"


def test_send_arms_only_once_its_message_is_in_the_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Armed before, the report left from the previous brief would count as written and the
    supervisor would drop the session at once."""
    monkeypatch.chdir(tmp_path)
    registry, report = tmp_path / "registry", aged(tmp_path / "r.md", 60)
    monkeypatch.setenv("OMNIGENT_SUPERVISE_REGISTRY", str(registry))
    og = FakeOmnigent(reply="done")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    sid = og.create("claude", "t")
    og.sessions[sid].append(told("brief", 120))
    (tmp_path / "a.md").write_text("fix it")
    at_send: list[str] = []
    stale: list[bool] = []
    send, info = og.send, og.info

    def sending(s: str, text: str) -> None:
        at_send.append(registry.read_text() if registry.exists() else "")
        send(s, text)

    def reporting(s: str) -> dict:
        if not stale and f"{sid}=" in (registry.read_text() if registry.exists() else ""):
            since, _ = omnigent_agent.instructed(og, sid, og.items(sid), "")
            stale.append(report.stat().st_mtime > since)  # the supervisor's view once armed
            report.write_text("fixed")
        return info(s)

    og.send, og.info = sending, reporting  # type: ignore[method-assign]
    assert omnigent_agent.main(["send", sid, "a.md", "r.md", "--confirm-seconds", "0"]) == 0
    assert at_send == [""]
    assert stale == [False]


QUESTION = {
    "elicitation_id": "e1",
    "params": {
        "content_preview": 'AskUserQuestion({"questions": [{"question": "Which base?"}]})',
        "ask_user_question": {"questions": [{"question": "Which base?"}]},
    },
}
APPROVAL = {"elicitation_id": "e2", "params": {"content_preview": "Bash(rm -rf build)"}}


def relay(
    og: FakeOmnigent,
    sid: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str = "go",
    *flags: str,
) -> int:
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.setattr(omnigent_agent, "QUEUED_SECONDS", 0)
    monkeypatch.setenv("OMNIGENT_SESSION_ID", "cos")  # the relaying session its header names
    (tmp_path / "m.md").write_text(text)
    return omnigent_agent.main(["relay", sid, str(tmp_path / "m.md"), *flags])


def test_relay_to_an_idle_session_is_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "lead")
    assert relay(og, sid, tmp_path, monkeypatch) == 0
    assert capsys.readouterr().out.strip() == f"session {sid}: delivered"
    assert omnigent_agent.text_of(og.sessions[sid][-1]) == "[relayed by cos]\ngo"


def test_relay_held_only_by_a_running_step_exits_0_with_the_queued_line_and_a_rerun_sends_no_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent(hold=10**6)  # a foreground command that outlasts the call
    sid = og.create("claude", "lead")
    assert relay(og, sid, tmp_path, monkeypatch) == 0
    out = capsys.readouterr()
    assert (
        out.out == f"session {sid}: queued until the session's current step ends\nit lands then\n"
    )
    assert out.err == ""
    assert relay(og, sid, tmp_path, monkeypatch) == 0
    assert og.posts == 1


def test_relay_rerun_after_the_copy_landed_unanswered_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "lead")
    relay(og, sid, tmp_path, monkeypatch)
    assert relay(og, sid, tmp_path, monkeypatch) == 0
    assert capsys.readouterr().out.splitlines()[-1] == f"session {sid}: already delivered"
    assert og.posts == 1


def test_relay_to_an_archived_session_names_its_state_and_where_its_work_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "sched")
    og.archive(sid)
    og.kids = ["s9"]
    og.tasks = [  # a monitor and an ended shell are no work left running
        {"type": "monitor", "status": "running"},
        {"type": "shell", "status": "done"},
        {"type": "shell", "status": "running"},
    ]
    assert relay(og, sid, tmp_path, monkeypatch) == omnigent_agent.GONE
    err = capsys.readouterr().err
    assert f"session {sid} is archived (title '?', status idle; 1 background task(s)" in err
    assert "its work continues in sub-agents s9: relay there" in err
    og.kids = []
    relay(og, sid, tmp_path, monkeypatch)
    assert "no sub-agent or successor session is recorded" in capsys.readouterr().err
    assert og.posts == 0


def test_relay_unarchive_brings_an_archived_session_back_then_delivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "lead")
    og.archive(sid)
    assert relay(og, sid, tmp_path, monkeypatch, "go", "--unarchive") == 0
    assert sid not in og.archived
    assert capsys.readouterr().out.splitlines()[-1].startswith(f"session {sid}: delivered")
    assert og.posts == 1


@pytest.mark.parametrize("runner_online", [False, True])
def test_relay_to_a_session_on_an_offline_host_sends_nothing_and_exits_host_offline(
    runner_online: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "lead")
    retried = reaped(og, {"recovered": True, "recovery": "runner_relaunched"})
    info = og.info
    # runner_online True: a runner not yet timed out on a host already reported down.
    og.info = lambda s: {**info(s), "host_online": False, "runner_online": runner_online}  # type: ignore[method-assign]
    assert issubclass(omnigent_agent.HostOffline, omnigent_agent.Gone)  # `except Gone` callers
    assert relay(og, sid, tmp_path, monkeypatch) == omnigent_agent.HOST_OFFLINE
    assert "its host is offline" in capsys.readouterr().err
    assert (retried, og.posts) == ([], 0)


def test_a_dormant_resumable_host_is_revived_not_reported_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(omnigent_agent, "POLL_SECONDS", 0)
    og = FakeOmnigent()
    sid = og.create("claude", "lead")
    retried = reaped(og, {"recovered": True, "recovery": "runner_relaunched"})
    info = og.info
    og.info = lambda s: {**info(s), "host_online": False, "host_resumable": True}  # type: ignore[method-assign]
    assert relay(og, sid, tmp_path, monkeypatch) == 0
    assert retried == [sid]


def test_relay_of_the_same_words_after_the_session_worked_through_tools_is_a_new_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "lead")
    assert relay(og, sid, tmp_path, monkeypatch, "status?") == 0
    # the session took the first copy up with tool calls and wrote no reply message
    og.sessions[sid].append({"id": "fc", "type": "function_call"})
    capsys.readouterr()
    assert relay(og, sid, tmp_path, monkeypatch, "status?") == 0
    assert capsys.readouterr().out.strip() == f"session {sid}: delivered"
    assert (
        og.posts == 2
    )  # on main the second relay exits 0 with "already delivered", posting nothing


def test_relay_of_the_same_words_after_the_session_replied_is_a_new_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(reply="STILL WORKING")
    sid = og.create("claude", "lead")
    relay(og, sid, tmp_path, monkeypatch, "carry on")
    assert relay(og, sid, tmp_path, monkeypatch, "carry on") == 0
    assert og.posts == 2


def test_relay_holds_behind_an_open_question_unless_told_to_answer_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent(hold=10**6)  # the question holds every message behind it
    sid = og.create("claude", "cos")
    og.prompts = [QUESTION]
    assert relay(og, sid, tmp_path, monkeypatch, "sync done") == omnigent_agent.QUEUED
    assert og.resolved == []
    assert "Which base?" in capsys.readouterr().err


def test_relay_beside_an_open_question_card_delivers_and_names_it_without_answering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()  # a card blocks nothing: the message lands at once
    sid = og.create("claude", "lead")
    og.prompts = [
        {
            "elicitation_id": "q1",
            "params": {
                "policy_name": "sys_ask_user_question",
                "ask_user_question": {"questions": [{"question": "Which base?"}]},
                "message": "Which base?",
            },
        }
    ]
    assert relay(og, sid, tmp_path, monkeypatch) == 0
    assert capsys.readouterr().out.strip() == (
        f"session {sid}: delivered; its question is still open (relay --answer answers it):"
        " q1: Which base?"
    )
    assert og.resolved == []
    assert og.posts == 1


def test_relay_behind_a_blocking_question_is_queued_naming_it_and_answer_and_resolves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent(hold=10**6)  # the harness holds every message until it is answered
    sid = og.create("claude", "lead")
    og.prompts = [
        {
            "elicitation_id": "e1",
            "params": {
                "policy_name": "claude_native_permission",
                "ask_user_question": {"questions": [{"question": "Which base?"}]},
                "message": "Which base?",
            },
        }
    ]
    assert relay(og, sid, tmp_path, monkeypatch) == omnigent_agent.QUEUED
    err = capsys.readouterr().err
    assert "Which base?" in err and "relay --answer" in err
    assert og.resolved == []


def test_send_beside_an_open_question_card_delivers_and_names_it_without_answering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = git_checkout(tmp_path / "w")
    (repo / "f.md").write_text("fix Y")
    og = FakeOmnigent(reply="done")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(repo)
    sid = og.create("claude", "t")
    og.prompts = [
        {
            "elicitation_id": "q1",
            "params": {
                "policy_name": "sys_ask_user_question",
                "ask_user_question": {"questions": [{"question": "Which base?"}]},
                "message": "Which base?",
            },
        }
    ]
    info = og.info
    og.info = lambda s: ((repo / "r.md").write_text("ok"), info(s))[1]  # type: ignore[method-assign]
    assert omnigent_agent.main(["send", sid, "f.md", "r.md", "--confirm-seconds", "0"]) == 0
    assert (
        f"session {sid}: delivered; its question is still open (relay --answer answers it):"
        " q1: Which base?"
    ) in capsys.readouterr().out
    assert og.resolved == []
    assert og.posts == 1


def test_relay_answers_a_question_with_the_message_instead_of_posting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent(hold=10**6)  # an interrupt would leave it held (omnigent#14)
    sid = og.create("claude", "lead")
    og.prompts = [QUESTION]
    assert relay(og, sid, tmp_path, monkeypatch, "use main\n", "--answer") == 0
    assert og.resolved == [(sid, "e1", "accept", {"Which base?": "use main"})]
    assert og.posts == 0
    assert "answered its open question" in capsys.readouterr().out


def test_relay_rerun_answers_a_question_raised_behind_its_own_pending_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Its own copy is no other input: waiting on it would exit 5 behind the question for good."""
    og = FakeOmnigent(hold=10**6)
    sid = og.create("claude", "lead")
    assert relay(og, sid, tmp_path, monkeypatch) == 0
    og.prompts = [QUESTION]
    assert relay(og, sid, tmp_path, monkeypatch, "go", "--answer") == 0
    assert og.resolved == [(sid, "e1", "accept", {"Which base?": "go"})]
    assert og.posts == 1
    assert "answered its open question" in capsys.readouterr().out


def test_relay_revives_a_reaped_runner_then_delivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "lead")
    retried = reaped(og, {"recovered": True, "recovery": "runner_relaunched"}, 2)
    assert relay(og, sid, tmp_path, monkeypatch) == 0
    assert retried == [sid]
    assert omnigent_agent.text_of(og.sessions[sid][-1]) == "[relayed by cos]\ngo"


def test_relay_of_a_message_neither_pending_nor_landed_is_not_deliverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent(deaf={0})
    sid = og.create("claude", "lead")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    (tmp_path / "m.md").write_text("go")
    args = ["relay", sid, str(tmp_path / "m.md"), "--confirm-seconds", "0"]
    assert omnigent_agent.main(args) == omnigent_agent.GONE
    assert "neither pending nor in history" in capsys.readouterr().err


def test_relay_never_answers_an_approval_prompt_and_says_what_holds_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent(hold=10**6)
    sid = og.create("claude", "lead")
    og.prompts = [QUESTION, APPROVAL]
    assert relay(og, sid, tmp_path, monkeypatch) == omnigent_agent.QUEUED
    assert og.resolved == []
    assert capsys.readouterr().err.strip() == (
        f"session {sid}: queued behind an approval prompt (`approve` answers it): e2: Bash(rm -rf build)"
        f" and a question: e1: {QUESTION['params']['content_preview']}\n{LANDS_AFTER_A_QUESTION}"
    )


# Nothing withdraws a queued input, so the line never invites a rerun of this message with
# --answer: the queued copy would land after the answer, the same words twice.
LANDS_AFTER_A_QUESTION = (
    "this message stays queued and lands once the question is answered (next time, to answer a"
    " question, use `relay --answer` instead of a plain relay, never after one)"
)


def test_relay_behind_only_a_question_names_relay_answer_for_next_time_not_approve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent(hold=10**6)
    sid = og.create("claude", "lead")
    og.prompts = [QUESTION]
    assert relay(og, sid, tmp_path, monkeypatch) == omnigent_agent.QUEUED
    assert capsys.readouterr().err.strip() == (
        f"session {sid}: queued behind a question: e1: {QUESTION['params']['content_preview']}\n"
        f"{LANDS_AFTER_A_QUESTION}"
    )
    assert [omnigent_agent.text_of(p) for p in og.info(sid)["pending_inputs"]] == [
        f"{omnigent_agent.RELAYED.format(who='cos')}\ngo"
    ]  # the copy that lands once the question is answered


def test_relay_from_a_task_behind_a_question_says_only_the_owner_answers_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent(hold=10**6)
    sid = og.create("claude", "lead")
    og.prompts = [QUESTION]
    assert relay(og, sid, tmp_path, monkeypatch, "synced", "--from", "nightly") == (
        omnigent_agent.QUEUED
    )
    assert capsys.readouterr().err.strip() == (
        f"session {sid}: queued behind a question: e1: {QUESTION['params']['content_preview']}\n"
        "this message stays queued and lands once the question is answered"
        " (only the owner's own relay answers a question)"
    )


def test_approve_resolves_each_approval_prompt_never_a_question(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "lead")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    og.prompts = [QUESTION, APPROVAL]
    assert omnigent_agent.main(["approve", sid]) == 0
    assert og.resolved == [(sid, "e2", "accept", None)]
    assert capsys.readouterr().out == f"session {sid}: accepted e2: Bash(rm -rf build)\n"
    assert omnigent_agent.main(["approve", sid]) == 1  # only the question is left
    assert "no approval prompt pending" in capsys.readouterr().err
    og.prompts = [APPROVAL, {**APPROVAL, "elicitation_id": "e3"}]
    og.fail = {"e2"}
    assert omnigent_agent.main(["approve", sid, "--decline"]) == 1  # e3 still answered
    assert og.resolved[-1] == (sid, "e3", "decline", None)
    out = capsys.readouterr().out
    assert "not answered: e2: Bash(rm -rf build): POST resolve e2: HTTP 404" in out
    assert f"session {sid}: declined e3: Bash(rm -rf build)" in out


def test_approve_with_only_a_question_pending_names_it_and_relay_answer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "lead")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    og.prompts = [QUESTION]
    assert omnigent_agent.main(["approve", sid]) == 1
    assert og.resolved == []
    err = capsys.readouterr().err
    assert "only a question (`relay --answer` answers it): " in err
    assert "e1: " + QUESTION["params"]["content_preview"] in err


def test_relay_to_an_archived_session_is_not_deliverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "lead")
    og.archive(sid)
    assert relay(og, sid, tmp_path, monkeypatch) == omnigent_agent.GONE
    assert f"session {sid} is archived" in capsys.readouterr().err
    assert og.posts == 0


class BroadcastFake(FakeOmnigent):
    """Sessions with a project, a host and an update time, listed as ``GET /v1/sessions`` lists
    them: top-level, archived ones left out."""

    def __init__(self, **kw: object) -> None:
        super().__init__(**kw)
        self.meta: dict[str, dict] = {}

    def add(self, title: str, project: str = "p1", age_hours: float = 0, **meta: object) -> str:
        sid = self.create("claude", title)
        self.meta[sid] = {
            "id": sid,
            "title": title,
            "project_id": project,
            "host_id": "h",
            "updated_at": time.time() - age_hours * 3600,
            **meta,
        }
        return sid

    project = omnigent_agent.Omnigent.project

    def info(self, sid: str) -> dict:
        if sid not in self.sessions:
            raise omnigent_agent.Fail(f"GET /v1/sessions/{sid}: TimeoutError: timed out")
        return {**self.meta.get(sid, {}), **super().info(sid)}

    def call(self, method: str, path: str, body: object = None) -> dict:
        if path.startswith("/v1/projects"):
            names = {"p1": "Agent skills", "p2": "Omnigent", "p3": "Omnigent · automations"}
            return {"data": [{"id": k, "name": v} for k, v in names.items()]}
        assert path.startswith("/v1/sessions?") and "sort_by=updated_at" in path
        assert "visibility=mine" in path  # never a session another user shared
        live = [self.info(s) for s in self.meta if s not in self.archived]
        return {"data": [s for s in live if not s.get("parent_session_id")]}

    def hosts(self) -> dict[str, str]:
        return {"h": "cc-experiments", "h2": "macbook"}


def broadcast(
    og: FakeOmnigent,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *args: str,
    text: str = "rule",
) -> int:
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.setattr(omnigent_agent, "QUEUED_SECONDS", 0)
    (tmp_path / "m.md").write_text(text)
    live = [] if "--decisions" in args else ["--stop"]
    return omnigent_agent.main(["broadcast", str(tmp_path / "m.md"), *live, *args])


def test_a_broadcast_without_stop_writes_the_decisions_file_and_posts_to_no_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = BroadcastFake()
    lead = og.add("lead")
    board = tmp_path / "board.md"
    board.write_text("# Board\n\n## Standing decisions\n- merge on green\n\n## Closed\n- x\n")
    before = list(og.sessions[lead])
    args = ("--include", lead, "--decisions", str(board))
    assert broadcast(og, tmp_path, monkeypatch, *args, text="pause\nto save tokens") == 0
    assert "posted to no session" in capsys.readouterr().out
    assert og.sessions[lead] == before
    assert omnigent_agent.standing(board) == "- merge on green"  # the owner's section, unchanged
    text = board.read_text()
    assert "## Chief of staff decisions\n- pause to save tokens\n" in text
    assert "## Standing decisions\n- merge on green\n\n## Closed\n- x" in text
    fresh = tmp_path / "fresh.md"
    assert broadcast(og, tmp_path, monkeypatch, "--decisions", str(fresh)) == 0
    assert "- rule" in fresh.read_text()
    assert omnigent_agent.standing(fresh) == ""  # not the owner's section


def test_a_dry_run_without_stop_writes_nothing_and_no_flag_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = BroadcastFake()
    lead = og.add("lead")
    board = tmp_path / "board.md"
    args = ("--include", lead, "--decisions", str(board), "--dry-run")
    assert broadcast(og, tmp_path, monkeypatch, *args) == 0
    assert not board.exists()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    (tmp_path / "m.md").write_text("rule")
    assert omnigent_agent.main(["broadcast", str(tmp_path / "m.md"), "--include", lead]) == 1
    assert "only a stop order" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        omnigent_agent.main(["broadcast", "x", "--stop", "--decisions", str(board)])


def test_the_loop_contract_names_the_file_a_lead_reads_and_a_decisions_target_is_that_file() -> (
    None
):
    repository = Path(omnigent_agent.__file__).resolve().parents[4]
    loop = repository / "loop/skills/engineering-loop"
    for doc in (
        loop / "SKILL.md",
        loop / "lead.md",
        repository / "chief-of-staff/skills/chief-of-staff/SKILL.md",
    ):
        assert "Standing decisions:" in doc.read_text()
    # only the new text has these: the lead reads the remote copy, the chief of staff writes its own section
    assert "git show origin/<default branch>:<path>" in (loop / "lead.md").read_text()
    cos = (repository / "chief-of-staff/skills/chief-of-staff/SKILL.md").read_text()
    assert "git show origin/<default branch>:<path>" in cos
    assert "under `## Chief of staff decisions`" in cos


def test_a_stop_order_still_posts_to_the_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = BroadcastFake()
    lead = og.add("lead")
    assert broadcast(og, tmp_path, monkeypatch, "--include", lead, text="STOP") == 0
    assert "delivered" in capsys.readouterr().out
    assert omnigent_agent.text_of(og.sessions[lead][-1]).endswith("STOP")


def test_broadcast_targets_followed_leads_and_recent_sessions_never_its_own_archived_or_automations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = BroadcastFake()
    lead, old_lead = og.add("lead"), og.add("old lead", age_hours=30)
    worker = og.add("worker", age_hours=30)  # owes a report: never followed, too old to be recent
    child = og.add("child", parent_session_id=lead)
    recent, stale = og.add("recent", "p2", 1), og.add("stale", "p2", 10)
    me, auto, gone = og.add("me"), og.add("cron", "p3"), og.add("gone lead")
    remote, extra = og.add("remote", host_id="h2"), og.add("extra", age_hours=30)
    og.archive(gone)
    monkeypatch.setenv("OMNIGENT_SESSION_ID", me)
    registry = tmp_path / "leads.registry"
    registry.write_text(
        f"{lead}=r.md\tfollow\n{worker}=w.md\n{old_lead}=o.md\tfollow\n{gone}=g.md\tfollow\n"
    )
    assert (
        broadcast(
            og,
            tmp_path,
            monkeypatch,
            "--registry",
            str(registry),
            "--dry-run",
            "--active-hours",
            "6",
        )
        == 0
    )
    out = capsys.readouterr().out
    assert og.posts == 0
    listed = {line.split()[0]: line for line in out.splitlines()}
    assert set(listed) == {lead, old_lead, recent, me, auto, gone, remote}
    assert listed[lead] == f"{lead} lead (Agent skills, cc-experiments)"
    assert listed[remote] == f"{remote} remote (Agent skills, macbook)"
    assert listed[me].endswith(": skipped: this session")
    assert listed[gone].endswith(": skipped: archived")
    assert listed[auto].endswith(": skipped: automations project 'Omnigent · automations'")
    assert stale not in out

    args = [
        "--registry",
        str(registry),
        "--active-hours",
        "6",
        "--project",
        "agent_skills",
        "--host",
        "cc-experiments",
        "--exclude",
        old_lead,
        "--include",
        extra,
        "--include",
        stale,
        "--include",
        child,
    ]
    assert broadcast(og, tmp_path, monkeypatch, *args) == 0
    lines = capsys.readouterr().out.splitlines()
    assert sorted(lines) == sorted(
        [
            f"{lead} lead (Agent skills, cc-experiments): delivered",
            f"{extra} extra (Agent skills, cc-experiments): delivered",
            f"{me} me (Agent skills, cc-experiments): skipped: this session",
            f"{gone} gone lead (Agent skills, cc-experiments): skipped: archived",
            f"{child} child (Agent skills, cc-experiments): skipped: a sub-agent",
        ]
    )
    assert omnigent_agent.text_of(og.sessions[lead][-1]) == f"[relayed by {me}]\nrule"
    assert og.posts == 2

    assert broadcast(og, tmp_path, monkeypatch, "--include", lead, "--project", "nope") == 1
    assert "--project nope names no project" in capsys.readouterr().err
    assert og.posts == 2


def test_broadcast_counts_a_target_it_could_not_read_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = BroadcastFake()
    lead = og.add("lead")
    for args in ([], ["--dry-run"], ["--project", "Agent skills"]):
        assert (
            broadcast(og, tmp_path, monkeypatch, "--include", lead, "--include", "s9", *args) == 1
        )
        out = capsys.readouterr().out
        assert "s9  (no project, ): failed: GET /v1/sessions/s9: TimeoutError: timed out\n" in out


def test_broadcast_relays_concurrently_one_line_each_and_exits_1_on_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = BroadcastFake(deaf={0})
    lost, *slow = [og.add(f"s{n}") for n in range(5)]
    send = og.send

    def slow_send(sid: str, text: str) -> None:
        time.sleep(0.5)
        send(sid, text)

    og.send = slow_send  # type: ignore[method-assign]
    started = time.monotonic()
    ids = [a for s in [lost, *slow] for a in ("--include", s)]
    assert broadcast(og, tmp_path, monkeypatch, *ids, "--confirm-seconds", "0") == 1
    assert time.monotonic() - started < 1.5  # five sends of 0.5 s, at once
    lines = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()}
    assert lines[lost].endswith("neither pending nor in history for 0s; start a fresh run instead")
    assert ": failed: session " in lines[lost]
    assert all(lines[s].endswith(": delivered") for s in slow)


def test_broadcast_reports_a_held_message_queued_and_skips_a_pending_prompt_unanswered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = BroadcastFake(hold=10**6)
    sid = og.add("busy")
    assert broadcast(og, tmp_path, monkeypatch, "--include", sid) == 0
    assert capsys.readouterr().out.endswith(
        ": queued until the session's current step ends\nit lands then\n"
    )
    for prompts in ([QUESTION], [APPROVAL], [QUESTION, APPROVAL]):
        asked = BroadcastFake()
        sid = asked.add("asking")
        asked.prompts = prompts
        assert broadcast(asked, tmp_path, monkeypatch, "--include", sid) == 0
        assert capsys.readouterr().out.endswith(": skipped: waiting on a question\n")
        assert asked.resolved == [] and asked.posts == 0


def test_status_prints_each_session_s_state_queued_inputs_and_last_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent(reply="DONE: #12 merged")
    ended, busy, gone = og.create("claude", "a"), og.create("codex", "b"), og.create("claude", "c")
    og.send(ended, "go")
    og.archive(gone)
    og.hold = 10**6
    og.send(busy, "go")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    assert omnigent_agent.main(["status", ended, busy, gone]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"{ended}: turn ended",
        "last message: DONE: #12 merged",
        f"{busy} (1 queued input): running",
        "last message: (none)",
        f"{gone}: archived",
        "last message: (none)",
    ]


def test_status_never_prints_a_user_message_as_the_last_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "lead")
    og.send(sid, "<system-reminder>be brief</system-reminder>")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    assert omnigent_agent.main(["status", sid]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "last message: (none)"
    og.reply = "on it"
    og.send(sid, "go")
    assert omnigent_agent.main(["status", sid]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "last message: on it"


HOSTS = {
    "hosts": [
        {"host_id": "h", "name": "here", "status": "online"},
        {"host_id": "m1", "name": "mac", "status": "online"},
        {"host_id": "t1", "name": "twin", "status": "offline"},
        {"host_id": "t2", "name": "twin", "status": "online"},
    ]
}


def test_host_resolves_a_name_or_an_id_and_refuses_none_or_several(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(omnigent_agent.Omnigent, "call", lambda self, m, p, b=None: HOSTS)
    og = object.__new__(omnigent_agent.Omnigent)
    assert og.host("mac") == og.host("m1") == "m1"
    with pytest.raises(omnigent_agent.Fail, match=r"names no host; hosts: here \(online\), mac"):
        og.host("nosuch")
    with pytest.raises(omnigent_agent.Fail, match="names several hosts"):
        og.host("twin")


def test_a_lead_on_another_host_gets_its_host_and_the_brief_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bodies = create_bodies(monkeypatch)
    monkeypatch.setattr(omnigent_agent.Omnigent, "loop_text", lambda self, ws, host: "")
    omnigent_agent.Omnigent().create("claude", "t", top_level=True, host_id="m1")
    assert bodies[-1]["host_id"] == "m1"
    og = FakeOmnigent()
    sid, _ = omnigent_agent.start(
        og, "claude", "t", "do X", tmp_path / "r.md", None, 0, top_level=True, host_id="m1"
    )
    text = omnigent_agent.text_of(og.sessions[sid][1])
    assert text.startswith("Work on t. Your brief:") and text.endswith("do X")
    assert not (tmp_path / "r.md.brief").exists()
    omnigent_agent.start(
        og, "claude", "u", "do Y", tmp_path / "u.md", None, 0, top_level=True, host_id="h"
    )
    assert (tmp_path / "u.md.brief").exists()  # this host: the brief stays a file


def test_host_needs_top_level_and_another_host_needs_a_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(omnigent_agent.Omnigent, "call", lambda self, m, p, b=None: HOSTS)
    monkeypatch.setattr(
        omnigent_agent.Omnigent, "__init__", lambda self: setattr(self, "host_id", "h")
    )
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("do X")
    argv = ["start", "claude", "t", "b.md", "r.md", "--host", "mac"]
    assert omnigent_agent.main(argv) == 1
    assert "--host needs --top-level" in capsys.readouterr().err
    assert omnigent_agent.main([*argv, "--top-level"]) == 1
    assert "absolute paths there" in capsys.readouterr().err


def test_a_remote_start_neither_resolves_nor_checks_its_paths_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(omnigent_agent.Omnigent, "call", lambda self, m, p, b=None: HOSTS)
    monkeypatch.setattr(
        omnigent_agent.Omnigent, "__init__", lambda self: setattr(self, "host_id", "h")
    )
    started: list[tuple[str, str]] = []
    monkeypatch.setattr(
        omnigent_agent,
        "start",
        lambda og, a, t, b, report, *rest, workspace=None, **kw: (
            started.append((str(report), str(workspace))) or ("s1", [])
        ),
    )
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("do X")
    (tmp_path / "w").symlink_to(tmp_path)  # a link here must not rewrite the path there
    (tmp_path / "w" / "r.md").write_text("an unrelated file here")
    w = tmp_path / "w"
    argv = ["start", "claude", "t", "b.md", str(w / "r.md"), "--top-level", "--host", "mac"]
    assert omnigent_agent.main([*argv, "--workspace", "w"]) == 1  # relative: whose cwd?
    assert omnigent_agent.main([*argv, "--workspace", str(w)]) == 0
    assert started == [(str(w / "r.md"), str(w))]


def test_resolve_posts_the_verdict_to_omnigent_s_elicitation_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict | None]] = []
    monkeypatch.setattr(omnigent_agent.Omnigent, "__init__", lambda self: None)
    monkeypatch.setattr(
        omnigent_agent.Omnigent, "call", lambda self, *a: calls.append(a) or {"queued": False}
    )
    omnigent_agent.Omnigent().resolve("s1", "e1", "accept", {"Which base?": "main"})
    omnigent_agent.Omnigent().resolve("s1", "e2", "decline")
    assert calls == [
        (
            "POST",
            "/v1/sessions/s1/elicitations/e1/resolve",
            {"action": "accept", "content": {"Which base?": "main"}},
        ),
        ("POST", "/v1/sessions/s1/elicitations/e2/resolve", {"action": "decline"}),
    ]


def serve_files(monkeypatch: pytest.MonkeyPatch, files: dict[str, dict]) -> list[str]:
    """A file API on localhost answering `files` by request path (404 for the rest); returns the
    request paths it saw. `main` talks to it through the real `Omnigent.call`."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: list[str] = []
    real = omnigent_agent.Omnigent

    class Files(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.removesuffix(omnigent_agent.SLIM)
            seen.append(path)
            body = files.get(path)
            self.send_response(404 if body is None else 200)
            self.end_headers()
            self.wfile.write(json.dumps(body or {}).encode())

        def do_PUT(self) -> None:  # and DELETE: seen as "<method> <path> <body>"
            sent = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
            seen.append(f"{self.command} {self.path} {sent}".rstrip())
            self.send_response(200 if f"{self.command} {self.path}" in files else 404)
            self.end_headers()
            self.wfile.write(b"{}")

        do_DELETE = do_PUT

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Files)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def make() -> omnigent_agent.Omnigent:
        og = object.__new__(real)
        og.base, og._token = f"http://127.0.0.1:{server.server_port}", "synthetic"
        return og

    monkeypatch.setattr(omnigent_agent, "Omnigent", make)
    return seen


FS = "/v1/sessions/s9/resources/environments/default/filesystem"


def test_report_reads_an_absolute_path_from_the_host_with_each_segment_encoded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = f"{FS}/Users/k/my%20dir/r%23.md?limit=1000&base=host"
    seen = serve_files(monkeypatch, {path: {"content": "# done\n"}})
    assert omnigent_agent.main(["report", "s9", "/Users/k/my dir/r#.md"]) == 0
    assert seen[-1:] == [path]  # no leading %2F, '/' kept between segments
    assert capsys.readouterr().out == "# done\n\n"


def test_report_reads_a_relative_path_in_the_workspace_without_base(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = serve_files(monkeypatch, {f"{FS}/tmp/r.md?limit=1000": {"content": "hi"}})
    assert omnigent_agent.main(["report", "s9", "tmp/r.md"]) == 0
    assert seen == ["/v1/sessions/s9", f"{FS}/tmp/r.md?limit=1000"]  # its record names no cloud


def test_report_reads_an_absolute_path_inside_the_workspace_relative_to_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # a host on an older Omnigent answers 404 to an absolute file read it serves relative
    files = {
        "/v1/sessions/s9": {"workspace": "/Users/k/ws"},
        f"{FS}/tmp/r.md?limit=1000": {"content": "ok"},
    }
    seen = serve_files(monkeypatch, files)
    assert omnigent_agent.main(["report", "s9", "/Users/k/ws/tmp/r.md"]) == 0
    # the record twice: report's look for a cloud harness, then the read's for the workspace
    assert seen == ["/v1/sessions/s9", "/v1/sessions/s9", f"{FS}/tmp/r.md?limit=1000"]
    assert (
        omnigent_agent.main(["report", "s9", "/Users/k/wsx/r.md"]) == 4
    )  # a sibling stays absolute
    assert seen[-1] == f"{FS}/Users/k/wsx/r.md?limit=1000&base=host"


def test_report_lists_a_directory_one_entry_per_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    listing = {"data": [{"name": "a.md", "type": "file"}, {"name": "sub", "type": "directory"}]}
    serve_files(monkeypatch, {f"{FS}/tmp?limit=1000&base=host": listing})
    assert omnigent_agent.main(["report", "s9", "/tmp"]) == 0
    assert capsys.readouterr().out == "a.md\nsub/\n"


def test_modified_reads_the_files_time_from_its_folders_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [{"name": "r.md.brief", "modified_at": 5}, {"name": "r.md", "modified_at": 7}]
    seen = serve_files(monkeypatch, {f"{FS}/tmp/loop?limit=1000&base=host": {"data": entries}})
    og = omnigent_agent.Omnigent()
    assert og.modified("s9", "/tmp/loop/r.md") == 7.0
    assert seen[-1] == f"{FS}/tmp/loop?limit=1000&base=host"  # one listing, not the file
    assert og.modified("s9", "/tmp/loop/gone.md") == 0.0


def test_a_remote_sessions_file_is_written_and_deleted_on_its_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # a write that went here lands there
    files = {
        "/v1/sessions/s9": {"workspace": "/Users/k/ws"},
        f"PUT {FS}/tmp/r.md.followup": {},
        f"DELETE {FS}/tmp/r.md.question?base=host": {},
    }
    seen = serve_files(monkeypatch, files)
    og = omnigent_agent.Omnigent()
    omnigent_agent.file_write(og, "s9", Path("tmp/r.md.followup"), "use main", far=True)
    omnigent_agent.file_unlink(og, "s9", Path("/tmp/r.md.question"), far=True)
    omnigent_agent.file_unlink(og, "s9", Path("/tmp/gone"), far=True)  # 404: nothing there
    assert seen == [
        f'PUT {FS}/tmp/r.md.followup {{"content": "use main"}}',
        "/v1/sessions/s9",
        f"DELETE {FS}/tmp/r.md.question?base=host",
        "/v1/sessions/s9",
        f"DELETE {FS}/tmp/gone?base=host",
        "/v1/sessions/s9",
        f"{FS}/tmp/gone?limit=1000&base=host",  # the 404 is believed once a read agrees
    ]


def test_report_exits_4_naming_a_missing_file_and_1_for_other_statuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    serve_files(monkeypatch, {})
    assert omnigent_agent.main(["report", "s9", "/nope.md"]) == 4
    assert "/nope.md" in capsys.readouterr().err
    monkeypatch.setattr(omnigent_agent, "RETRY_SECONDS", ())
    og = omnigent_agent.Omnigent()
    og.base = "http://127.0.0.1:1"  # nothing listens: not a 404
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    assert omnigent_agent.main(["report", "s9", "/x.md"]) == 1


def test_report_without_a_path_reads_the_registry_line_else_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = tmp_path / "leads.registry"
    registry.write_text("s9=/Users/k/r.md\n")
    path = f"{FS}/Users/k/r.md?limit=1000&base=host"
    seen = serve_files(monkeypatch, {path: {"content": "x"}})
    assert omnigent_agent.main(["report", "s9", "--registry", str(registry)]) == 0
    assert seen[-1:] == [path]
    assert omnigent_agent.main(["report", "s8", "--registry", str(registry)]) == 2
    assert omnigent_agent.main(["report", "s9"]) == 2
    assert "OMNIGENT_SUPERVISE_REGISTRY" in capsys.readouterr().err


# Every row of the design's routing table (§ Routing an event): the event, what the small model
# would answer if asked, and the expected (settled by, action). None: the model is never asked.
ROUTES = [
    ("archived", {"kind": "archived"}, None, ("rule", "absorb")),
    (
        "parked after a park message",
        {"kind": "ended", "last": "PARKED: the host is full", "parked": True},
        None,
        ("rule", "record"),
    ),
    (
        "parked with no park message",
        {"kind": "ended", "last": "PARKED: waiting for the owner's pick"},
        None,
        ("rule", "act"),
    ),
    (
        "still working, named wait",
        {"kind": "ended", "last": "STILL WORKING: waiting on the verifier"},
        None,
        ("rule", "record"),
    ),
    ("exact repeat", {"kind": "ended", "last": "on it", "repeat": True}, None, ("rule", "record")),
    (
        "quiet again after a nudge, the same words",
        {"kind": "ended", "last": "on it", "repeat": True, "nudged": True},
        None,
        ("rule", "act"),
    ),
    ("quiet, mid-work", {"kind": "ended", "last": "on it"}, "none", ("model", "record")),
    ("quiet, stalled", {"kind": "ended", "last": "on it"}, "act", ("model", "nudge")),
    ("quiet, for the owner", {"kind": "ended", "last": "hm"}, "owner", ("model", "owner")),
    ("quiet, no model answer", {"kind": "ended", "last": "hm"}, "unsure", ("model", "nudge")),
    ("deadline, unknown", {"kind": "deadline", "last": "on it"}, None, ("rule", "nudge")),
    (
        "deadline, after a nudge",
        {"kind": "deadline", "last": "on it", "nudged": True},
        None,
        ("rule", "act"),
    ),
    (
        "deadline, named wait",
        {"kind": "deadline", "last": "STILL WORKING: CI is running"},
        None,
        ("rule", "act"),
    ),
    (
        "deadline, parked",
        {"kind": "deadline", "last": "PARKED: full", "parked": True},
        None,
        ("rule", "record"),
    ),
    # the two ordinary-word waits leads wrote, pinned so a change to WAIT or NAMED can't bring back
    # the repeat nudges (#508); a bare STILL WORKING names none and gets its one nudge
    (
        "still working, a queue in ordinary words",
        {
            "kind": "ended",
            "last": "STILL WORKING: waiting on the queue lead's runs for #12 and #13",
        },
        None,
        ("rule", "record"),
    ),
    (
        "still working, a heavy slot in ordinary words",
        {"kind": "ended", "last": "STILL WORKING: #12 and #13 all still waiting for a heavy slot"},
        None,
        ("rule", "record"),
    ),
    (
        "deadline, bare still working",
        {"kind": "deadline", "last": "STILL WORKING."},
        None,
        ("rule", "nudge"),
    ),
    (
        "deadline, bare still working after a nudge",
        {"kind": "deadline", "last": "STILL WORKING.", "nudged": True},
        None,
        ("rule", "act"),
    ),
    (
        "quiet again after a nudge",
        {"kind": "ended", "last": "on it", "nudged": True},
        None,
        ("rule", "act"),
    ),
    ("failed, first", {"kind": "failed"}, None, ("rule", "revive")),
    ("failed, again", {"kind": "failed", "revived": True}, None, ("rule", "act")),
    ("done", {"kind": "ended", "last": "**DONE** PR #1 merged"}, None, ("rule", "done")),
    ("report path", {"kind": "ended", "last": "/r/a.md"}, None, ("rule", "read-report")),
    (
        "report head, done",
        {"kind": "ended", "last": "/r/a.md", "head": "DONE: merged"},
        None,
        ("rule", "done"),
    ),
    (
        "report head, still working on CI",
        {"kind": "ended", "last": "/r/a.md", "head": "STILL WORKING: CI running"},
        None,
        ("rule", "record"),
    ),
    (
        "report head, no ending",
        {"kind": "ended", "last": "/r/a.md", "head": "BLOCKED: no access"},
        None,
        ("rule", "act"),
    ),
    (  # a USER NEEDED with no card is its manager's, whatever the decisions say (#2170)
        "user needed, standing decisions",
        {"kind": "ended", "last": "USER NEEDED: delete prod?", "decisions": True},
        None,
        ("rule", "act"),
    ),
    (
        "user needed, no standing decisions",
        {"kind": "ended", "last": "USER NEEDED: x"},
        None,
        ("rule", "act"),
    ),
    ("approval prompt", {"kind": "blocked"}, None, ("rule", "owner")),
    ("question", {"kind": "blocked", "question": True}, None, ("rule", "act")),
    (
        "owed report, outcome",
        {"kind": "terminal", "line": "a t: report written: /r.md"},
        None,
        ("rule", "act"),
    ),
    (
        "owed report, prompt",
        {"kind": "terminal", "line": "a t: blocked on a prompt: Bash(x)"},
        None,
        ("rule", "owner"),
    ),
    ("owed report, a notice", {"kind": "notice"}, None, ("rule", "record")),
]


@pytest.mark.parametrize(("row", "event", "answer", "routed"), ROUTES, ids=[r[0] for r in ROUTES])
def test_each_row_of_the_routing_table_settles_its_event(
    row: str, event: dict, answer: str | None, routed: tuple[str, str]
) -> None:
    asked: list[bool] = []

    def ask() -> tuple[str, str]:
        asked.append(True)
        return answer or "act", "the model's why"

    by, action, why = omnigent_agent.route(event, ask)
    assert (by, action) == routed and why
    assert asked == ([True] if answer else [])


@pytest.mark.parametrize("answer", ["none", "act", "owner"])
def test_no_model_answer_routes_an_approval_prompt_anywhere_but_the_owner(answer: str) -> None:
    for event in ({"kind": "blocked"}, {"kind": "terminal", "line": "a: blocked on a prompt: x"}):
        assert omnigent_agent.route(event, lambda: (answer, "x"))[1] == "owner"


def model_answers(monkeypatch: pytest.MonkeyPatch, answer: object) -> list[dict]:
    """``claude -p`` faked: each call is recorded; ``answer`` is its stdout, an exit code, or an
    exception to raise."""
    calls: list[dict] = []

    def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess:
        calls.append({"cmd": cmd, **kw})
        if isinstance(answer, BaseException):
            raise answer
        code, out = (answer, "") if isinstance(answer, int) else (0, str(answer))
        return subprocess.CompletedProcess(cmd, code, out, "")

    monkeypatch.setattr(omnigent_agent.subprocess, "run", run)
    return calls


def test_the_small_model_is_one_headless_call_with_the_event_cut_on_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = model_answers(monkeypatch, 'thinking\n{"route": "none", "why": "mid-work"}\n')
    assert REAL_TRIAGE("x" * 5000, "- merge on green") == ("none", "mid-work")
    cmd, kw = calls[0]["cmd"], calls[0]
    assert cmd[:4] == ["claude", "-p", "--model", "claude-haiku-5-5"]
    for flag, value in (("--tools", ""), ("--setting-sources", "")):
        assert cmd[cmd.index(flag) + 1] == value
    assert "--strict-mcp-config" in cmd
    assert "- merge on green" in cmd[cmd.index("--system-prompt") + 1]
    assert kw["input"] == "x" * 4000 and kw["timeout"] == 20
    assert kw["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "1000"
    monkeypatch.setenv("OMNIGENT_SMALL_MODEL", "claude-other")
    REAL_TRIAGE("e", "")
    assert calls[1]["cmd"][3] == "claude-other"
    assert "(none)" in calls[1]["cmd"][calls[1]["cmd"].index("--system-prompt") + 1]


@pytest.mark.parametrize(
    "answer",
    [
        "route: none",
        '{"route": "approve"}',
        '["none"]',
        "",
        1,
        subprocess.TimeoutExpired("claude", 20),
        FileNotFoundError("claude"),
    ],
    ids=[
        "unparseable",
        "no such route",
        "not an object",
        "empty",
        "failed",
        "timed out",
        "missing",
    ],
)
def test_an_unparseable_failed_or_timed_out_model_answer_routes_a_quiet_end_to_a_nudge(
    monkeypatch: pytest.MonkeyPatch, answer: object
) -> None:
    model_answers(monkeypatch, answer)
    route, why = REAL_TRIAGE("an event", "")
    assert route == "unsure" and why
    ask = lambda: REAL_TRIAGE("an event", "d")
    event = {"kind": "ended", "last": "on it", "decisions": True}
    assert omnigent_agent.route(event, ask)[:2] == ("model", "nudge")


def test_standing_decisions_are_their_section_else_the_whole_file_else_none(
    tmp_path: Path,
) -> None:
    board = tmp_path / "board.md"
    board.write_text("# Board\n## Standing decisions (owner's)\n- merge on green\n## Next\nx\n")
    assert omnigent_agent.standing(board) == "- merge on green"
    board.write_text("- approve reversible progress\n")
    assert omnigent_agent.standing(board) == "- approve reversible progress"
    assert omnigent_agent.standing(None) == "" == omnigent_agent.standing(tmp_path / "gone.md")


class Leads(Lead):
    """Followed leads, each with its own history and ``snaps`` fields (status, prompts, ...)."""

    def __init__(self, **histories: list[dict]) -> None:
        Fleet.__init__(self, **histories)
        self.snap: dict = {}
        self.snaps: dict[str, dict] = {}

    def info(self, sid: str) -> dict:
        return {**Fleet.info(self, sid), **self.snaps.get(sid, {})}


def follows(tmp_path: Path, *sids: str, after: dict[str, str] | None = None) -> Path:
    registry = tmp_path / "registry"
    after = after or {}
    registry.write_text(
        "".join(
            f"{s}={tmp_path / s}.md\tfollow" + (f"\tafter={after[s]}" if s in after else "") + "\n"
            for s in sids
        )
    )
    return registry


def events_of(registry: Path) -> list[dict]:
    lines = registry.with_suffix(".events.jsonl").read_text().splitlines()
    return [json.loads(e) for e in lines]


WORKER = "c" * 32  # a session id a lead's STILL WORKING names: one of its own children
COS = "d" * 32  # the chief of staff's session, which --notify names
LEAD = "e" * 32  # a lead of the manager's, named by its id
OTHER = "f" * 32  # a running session outside the manager's leads and children


def manager(tmp_path: Path, wait: str, **snaps: dict) -> tuple[Path, Leads]:
    """A followed manager, ``m``, quiet an hour with ``wait`` as its last message, its leads ``x``,
    ``y`` and ``LEAD`` in its own registry (its line's ``leads=``) and ``WORKER`` its child;
    ``snaps`` overrides each session's info, where every one of them is running and was updated a
    minute ago. The registry's ``z``, an idle helper owing a report, is no lead."""
    leads = tmp_path / "app-leads.registry"
    leads.write_text(
        "".join(f"{s}={tmp_path / s[:1]}.md\tfollow\n" for s in ("x", "y", LEAD)) + "z=z.md\n"
    )
    registry = tmp_path / "registry"
    registry.write_text(f"m={tmp_path / 'm.md'}\tfollow\tleads={leads}\n")
    og = Leads(m=[told("brief", 4000), ending(wait, 3600)])
    og.kin_of["m"] = [WORKER]
    for sid in ("m", "x", "y", WORKER, COS, LEAD, OTHER):
        og.snaps[sid] = {"status": "running", "updated_at": time.time() - 60, **snaps.get(sid, {})}
    return registry, og


def stalls(og: Fleet) -> list[str]:
    """The deadline lines posted as without --console, which --fyi post keeps."""
    return [t.split("\n")[0] for _, t in og.inbox if t.startswith("Supervisor: m t-m: no progress")]


@pytest.mark.parametrize(
    "wait",
    [
        "STILL WORKING: waiting on my leads. Report: m.md",
        f"STILL WORKING: waiting on {WORKER}. Report: m.md",
        f"STILL WORKING: waiting on {LEAD}. Report: m.md",
    ],
)
def test_a_quiet_waiter_whose_named_sessions_all_run_raises_no_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wait: str
) -> None:
    registry, og = manager(tmp_path, wait)
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    assert stalls(og) == [] and deadlines(registry) == []


@pytest.mark.parametrize(
    ("wait", "snaps", "why"),
    [
        (
            "waiting on my leads",
            {"y": {"status": "idle", "updated_at": time.time() - 1300}},
            "lead",
        ),
        ("waiting on my leads", {"y": {"archived": True}}, "lead"),
        ("waiting on my leads", {"y": {"status": "failed"}}, "lead"),
        ("waiting on my leads", {"y": {"status": "idle", "updated_at": None}}, "lead"),
        (f"waiting on {WORKER}", {WORKER: {"status": "idle", "updated_at": 1.0}}, WORKER),
        (f"waiting on {LEAD}", {LEAD: {"status": "idle", "updated_at": 1.0}}, LEAD),
        (f"waiting on {OTHER}", {OTHER: {"status": "idle", "updated_at": 1.0}}, OTHER),
        ("waiting on the chief of staff for a host", {}, "chief"),
        (f"waiting on {COS} for a host", {COS: {"status": "idle", "updated_at": 1.0}}, COS),
        ("waiting on the owner's pick", {}, "owner's"),
    ],
)
def test_a_quiet_waiter_on_a_session_not_moving_or_outside_its_follow_set_raises_its_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wait: str, snaps: dict, why: str
) -> None:
    registry, og = manager(tmp_path, f"STILL WORKING: {wait}. Report: m.md", **snaps)
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    assert stalls(og) == [f"Supervisor: m t-m: no progress 60m ({why})"]


def test_a_quiet_waiter_on_the_chief_of_staff_raises_its_deadline_though_it_subscribes_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet = {"status": "idle", "updated_at": 1.0}  # a notified session that shows life is progress
    wait = f"STILL WORKING: waiting on {COS} for a host. Report: m.md"
    registry, og = manager(tmp_path, wait, **{COS: quiet})
    og.kin_of["m"] = [WORKER, COS]
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    assert stalls(og) == [f"Supervisor: m t-m: no progress 60m ({COS})"]


def test_a_quiet_waiter_on_a_session_id_when_its_children_are_unread_raises_its_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gone = {"status": "idle", "updated_at": 1.0}  # a named session that shows life is progress
    registry, og = manager(
        tmp_path, f"STILL WORKING: waiting on {WORKER}. Report: m.md", **{WORKER: gone}
    )
    og.kin_of["m"] = omnigent_agent.Fail("GET /v1/sessions/m/child_sessions: HTTP 503")
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    assert stalls(og) == [f"Supervisor: m t-m: no progress 60m ({WORKER})"]
    assert "m: children not read" in capsys.readouterr().err


def test_a_quiet_waiter_on_its_leads_with_one_lead_unread_raises_its_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, og = manager(tmp_path, "STILL WORKING: waiting on my leads. Report: m.md")
    og.broken["x"] = omnigent_agent.Gone("GET /v1/sessions/x: HTTP 404")
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    assert stalls(og) == ["Supervisor: m t-m: no progress 60m (lead)"]


def test_a_waiter_on_its_leads_without_a_leads_registry_or_with_one_unread_raises_its_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry, og = manager(tmp_path, "STILL WORKING: waiting on my leads. Report: m.md")
    (tmp_path / "app-leads.registry").write_text("z=z.md\n")  # it follows no lead
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    assert stalls(og) == ["Supervisor: m t-m: no progress 60m (lead)"]
    og.inbox.clear()
    registry.with_suffix(".state.json").unlink()
    (tmp_path / "app-leads.registry").unlink()
    (tmp_path / "app-leads.registry").mkdir()  # unreadable as a registry
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    assert stalls(og) == ["Supervisor: m t-m: no progress 60m (lead)"]
    assert "m: leads registry not read" in capsys.readouterr().err
    registry.write_text(f"m={tmp_path / 'm.md'}\tfollow\n")
    og.inbox.clear()
    registry.with_suffix(".state.json").unlink()
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    assert stalls(og) == ["Supervisor: m t-m: no progress 60m (lead)"]


STALE = 1300.0  # seconds since a session's last item: past DEADLINE_SECONDS
JOB = [{"type": "shell", "status": "running"}]  # a declared background job


@pytest.mark.parametrize(
    ("wait", "snaps"),
    [
        # X named by kind or title word, its title holding the word: running, a recent item
        ("waiting on the worker", {WORKER: {"title": "s898-parser-worker"}}),
        ("waiting on deployment", {WORKER: {"title": "Deployment check"}}),
        ("waiting on cccccccc", {}),  # a short id opening X's
        # X named by its id: busy in a long tool call (running, an old item)
        (f"waiting on {WORKER}", {WORKER: {"updated_at": time.time() - STALE}}),
        # idle between its own background jobs: a recent item, or a job declared
        (f"waiting on {WORKER}", {WORKER: {"status": "idle"}}),
        (
            f"waiting on {WORKER}",
            {WORKER: {"status": "idle", "updated_at": None, "background_tasks": JOB}},
        ),
        ("waiting on my leads", {"y": {"status": "idle", "background_tasks": JOB}}),
        # its own background job, declared and live
        ("waiting on bz3jp5aod", {"m": {"status": "idle", "background_tasks": JOB}}),
    ],
)
def test_a_quiet_waiter_on_a_session_that_shows_life_raises_no_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wait: str, snaps: dict
) -> None:
    registry, og = manager(tmp_path, f"STILL WORKING: {wait}. Report: m.md", **snaps)
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    assert stalls(og) == [] and deadlines(registry) == []


def test_a_waiters_named_session_with_an_old_item_shows_life_through_its_live_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wait = f"STILL WORKING: waiting on {WORKER}. Report: m.md"
    registry, og = manager(tmp_path, wait, **{WORKER: {"status": "idle", "updated_at": 1.0}})
    og.kids[WORKER] = time.time() - 60  # its own worker wrote a minute ago
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    assert stalls(og) == []
    og.kids[WORKER] = time.time() - STALE
    registry.with_suffix(".state.json").unlink()
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    assert stalls(og) == [f"Supervisor: m t-m: no progress 60m ({WORKER})"]


ASKING = {"pending_elicitations": [{"params": {"message": "pick", "ask_user_question": True}}]}
APPROVING = {"pending_elicitations": [{"params": {"content_preview": "Bash: rm x"}}]}


@pytest.mark.parametrize(
    ("wait", "snaps", "why"),
    [
        # dead: archived, failed or gone, though running and recent otherwise
        ("waiting on the worker", {WORKER: {"title": "w-worker", "archived": True}}, "worker"),
        ("waiting on the worker", {WORKER: {"title": "w-worker", "status": "failed"}}, "worker"),
        (f"waiting on {WORKER}", {WORKER: {"archived": True, "background_tasks": JOB}}, WORKER),
        (f"waiting on {WORKER}", {WORKER: {"status": "failed"}}, WORKER),
        # stuck on a question or a permission, though its turn reads running
        (f"waiting on {WORKER}", {WORKER: ASKING}, WORKER),
        (f"waiting on {WORKER}", {WORKER: APPROVING}, WORKER),
        ("waiting on the worker", {WORKER: {"title": "w-worker", **APPROVING}}, "worker"),
        # no kin answers the word, or the short id opens no kin's id
        ("waiting on the worker", {}, "worker"),
        ("waiting on dddddddd", {}, "dddddddd"),
    ],
)
def test_a_quiet_waiter_on_a_dead_or_prompted_session_raises_its_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wait: str, snaps: dict, why: str
) -> None:
    registry, og = manager(tmp_path, f"STILL WORKING: {wait}. Report: m.md", **snaps)
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    assert stalls(og) == [f"Supervisor: m t-m: no progress 60m ({why})"]


def test_a_quiet_waiter_on_a_gone_session_raises_its_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for wait, why in ((f"waiting on {WORKER}", WORKER), ("waiting on cccccccc", "cccccccc")):
        registry, og = manager(tmp_path, f"STILL WORKING: {wait}. Report: m.md")
        og.broken[WORKER] = omnigent_agent.Gone(f"GET /v1/sessions/{WORKER}: HTTP 404")
        supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
        assert stalls(og) == [f"Supervisor: m t-m: no progress 60m ({why})"]
        registry.with_suffix(".state.json").unlink()


def test_a_word_wait_never_resolves_to_the_chief_of_staff_or_an_unknown_wait_to_a_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wait = "STILL WORKING: waiting on the chief of staff for a host. Report: m.md"
    registry, og = manager(tmp_path, wait, **{COS: {"title": "chief of staff"}})
    og.kin_of["m"] = [WORKER, COS]  # it subscribes to the chief of staff, who shows life
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    assert stalls(og) == ["Supervisor: m t-m: no progress 60m (chief)"]
    # an unknown wait stays on its nudge path, though a live child's title holds "unknown"
    (tmp_path / "u").mkdir()
    registry, og = manager(tmp_path / "u", "on it", **{WORKER: {"title": "unknown probe"}})
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    assert [e["kind"] for e in events_of(registry)].count("deadline") == 1


def test_a_lead_word_without_a_leads_registry_names_a_child_titled_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wait = "STILL WORKING: waiting on the lead. Report: m.md"
    assert omnigent_agent.wait_of(wait) == "lead"
    registry, og = manager(tmp_path, wait, **{WORKER: {"title": "queue-lead"}})
    registry.write_text(f"m={tmp_path / 'm.md'}\tfollow\n")  # no leads= registry
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    assert stalls(og) == [] and deadlines(registry) == []


@pytest.mark.parametrize(
    ("wait", "snaps", "why"),
    [
        (
            f"waiting on {WORKER}",
            {WORKER: {"status": "idle", "updated_at": time.time() - STALE}},
            WORKER,
        ),
        (f"waiting on {WORKER}", {WORKER: {"status": "idle", "updated_at": None}}, WORKER),
        (
            "waiting on the worker",
            {WORKER: {"title": "w-worker", "status": "idle", "updated_at": time.time() - STALE}},
            "worker",
        ),
        (  # a monitor is no declared job
            f"waiting on {WORKER}",
            {
                WORKER: {
                    "status": "idle",
                    "updated_at": time.time() - STALE,
                    "background_tasks": [{"type": "monitor", "status": "running"}],
                }
            },
            WORKER,
        ),
        (  # a job that finished is none either
            f"waiting on {WORKER}",
            {
                WORKER: {
                    "status": "idle",
                    "updated_at": time.time() - STALE,
                    "background_tasks": [{"type": "shell", "status": "completed"}],
                }
            },
            WORKER,
        ),
        ("waiting on bz3jp5aod", {}, "bz3jp5aod"),  # its own job, none declared
    ],
)
def test_a_quiet_waiter_on_a_session_with_an_old_item_no_turn_and_no_job_raises_its_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wait: str, snaps: dict, why: str
) -> None:
    registry, og = manager(tmp_path, f"STILL WORKING: {wait}. Report: m.md", **snaps)
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    assert stalls(og) == [f"Supervisor: m t-m: no progress 60m ({why})"]


def test_the_nudge_and_revive_end_on_a_wait_the_supervisor_accepts() -> None:
    for text in (omnigent_agent.LEAD_NUDGE, omnigent_agent.LEAD_REVIVE):
        example = re.search(r'For example: "(STILL WORKING: [^"]+)"\.$', text)
        assert example and omnigent_agent.wait_of(example.group(1)) == "verifier", text
        assert text.startswith(omnigent_agent.OWN_LINES[0])  # a restart knows it by this prefix
    assert omnigent_agent.wait_of("STILL WORKING: waiting on the queue lead's runs") == "queue"
    assert omnigent_agent.wait_of("STILL WORKING: all still waiting for a heavy slot") == "heavy"
    assert omnigent_agent.wait_of("STILL WORKING.") == "unknown"


def test_a_second_stall_without_progress_escalates_and_sends_no_second_nudge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    monkeypatch.setattr(omnigent_agent, "triage", lambda event, decisions: ("act", "stalled"))
    og = Lead(told("brief", 60), ending("Looked at the logs.", 0))
    # quiet 300 s: the model says stalled, so the nudge; it lands, and the lead stays quiet
    at = {1: 301.0, 2: 302.0, 3: 333.0, 4: 634.0}
    supervised(
        og,
        registry,
        monkeypatch,
        5,
        lambda p: offset.__setitem__(0, at[p]),
        notify="cos",
        console=True,
    )
    assert [t for s, t in og.inbox if s == "a"] == [omnigent_agent.LEAD_NUDGE]
    [post] = posted(og)
    assert post.startswith("Supervisor [act] b-")
    assert "[act] a t-a: turn ended, quiet 301s (quiet again after its nudge)" in post
    assert [(e["settled"], e["action"]) for e in events_of(registry)] == [
        ("model", "nudge"),
        ("rule", "act"),
    ]
    lead = state_of(registry)["leads"]["a"]
    assert lead["nudges"] == 1 and lead["nudged_at"] == lead["progress_at"]
    assert [i["status"] for i in state_of(registry)["intents"]] == ["done"]


def test_a_quiet_lead_the_model_finds_mid_work_is_recorded_and_not_posted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_model: list[str]
) -> None:
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    og = Lead(told("brief", 60), ending("Reading the diff.", 0))
    supervised(
        og,
        registry,
        monkeypatch,
        2,
        lambda p: offset.__setitem__(0, 301.0),
        notify="cos",
        console=True,
    )
    assert og.inbox == [] and len(no_model) == 1 and "Reading the diff." in no_model[0]
    assert [(e["settled"], e["action"], e["why"]) for e in events_of(registry)] == [
        ("model", "record", "fake")
    ]


@pytest.mark.parametrize("landed", [True, False])
def test_a_crash_between_an_actions_send_and_done_never_sends_it_again_after_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, landed: bool
) -> None:
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    og = Lead(told("brief", 60), ending("STILL WORKING: on it", 0))
    og.snap = {"status": "running"}
    real = og.send

    def crash(sid: str, text: str) -> None:
        if sid == "a":
            if landed:
                real(sid, text)
            raise Stop  # the supervisor dies before it marks the nudge done
        real(sid, text)

    og.send = crash  # type: ignore[method-assign]
    supervised(
        og,
        registry,
        monkeypatch,
        3,
        lambda p: offset.__setitem__(0, 1201.0),
        notify="cos",
        console=True,
    )
    [intent] = state_of(registry)["intents"]
    assert (intent["action"], intent["status"]) == ("nudge", "open")
    assert state_of(registry)["leads"]["a"]["nudges"] == 1  # counted before the send
    og.send = real  # type: ignore[method-assign]
    supervised(og, registry, monkeypatch, 2, notify="cos", console=True)  # the restart
    assert [t for s, t in og.inbox if s == "a"] == [omnigent_agent.LEAD_NUDGE] * landed
    [intent] = state_of(registry)["intents"]
    assert intent["status"] == ("done" if landed else "escalated")
    escalated = [p for p in posted(og) if "may not have reached it" in p]
    assert len(escalated) == (0 if landed else 1)


@pytest.mark.parametrize("landed", [True, False])
def test_a_crash_right_after_a_post_is_accepted_posts_it_once_after_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, landed: bool
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 60), ending("DONE: shipped", 5))
    og.histories["cos"] = []
    real = og.send

    def crash(sid: str, text: str) -> None:
        if sid == "cos":
            if landed:
                real(sid, text)
                for n in range(150):  # newer items push it past the newest page of 100
                    og.histories["cos"].append(
                        {**said("assistant", "ok", f"c{n}"), "created_at": time.time()}
                    )
            raise Stop  # accepted, and the supervisor dies before it marks it delivered
        real(sid, text)

    og.send = crash  # type: ignore[method-assign]
    supervised(og, registry, monkeypatch, 2, notify="cos", console=True)
    [pending] = state_of(registry)["pending_posts"]
    assert pending["id"].startswith("b-") and state_of(registry)["attention"]
    og.send = real  # type: ignore[method-assign]
    supervised(og, registry, monkeypatch, 2, notify="cos", console=True)  # the restart
    posts = posted(og)
    assert len(posts) == 1 and posts[0].startswith(f"Supervisor [act] {pending['id']}: 1 event")
    if landed:
        assert og.pages >= 2  # found on the second page of the history
    state = state_of(registry)
    assert (state["pending_posts"], state["attention"]) == ([], [])


def test_a_restart_inside_the_post_window_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = follows(tmp_path, "a", "b")
    offset = skewed(monkeypatch)
    og = Leads(
        a=[told("brief", 60), ending("DONE: one", 5)],
        b=[told("brief", 60), ending("STILL WORKING: waiting on CI", 5)],
    )
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True)
    assert len(posted(og)) == 1
    offset[0] = 60.0
    og.histories["b"].append(ending("DONE: two", 0))
    supervised(og, registry, monkeypatch, 2, notify="cos", console=True)  # a restart at 60 s
    assert len(posted(og)) == 1  # the window from the first post holds across it
    assert [e["session"] for e in state_of(registry)["attention"]] == ["b"]
    offset[0] = 301.0
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True)
    assert len(posted(og)) == 2 and "[act] b t-b: turn ended" in posted(og)[1]


def test_a_restart_inside_the_model_window_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_model: list[str]
) -> None:
    registry = follows(tmp_path, "a", "b")
    board = tmp_path / "board.md"
    board.write_text("## Standing decisions\n- merge on green\n")
    offset = skewed(monkeypatch)
    og = Leads(a=[told("brief", 60)], b=[told("brief", 60)])
    supervised(og, registry, monkeypatch, 1, console=True, decisions=board)
    state = state_of(registry)
    state["rate"]["model_calls"] = [time.time() - 3000] * 30  # the hour's calls, spent
    registry.with_suffix(".state.json").write_text(json.dumps(state))
    # a quiet end, the one event the small model reads (#2170): news once it stays quiet past FOLLOW_QUIET_SECONDS
    quiet = lambda base: lambda poll: offset.__setitem__(0, base + 301.0 * poll)
    og.histories["a"].append(ending("paused for now", 0))
    supervised(
        og, registry, monkeypatch, 2, quiet(0.0), console=True, decisions=board
    )  # the restart
    assert no_model == []
    assert events_of(registry)[-1]["why"] == "over 30 small-model calls an hour"
    offset[0] = 700.0  # the oldest calls are past the hour
    og.histories["b"].append(ending("paused for now", 0))
    supervised(og, registry, monkeypatch, 2, quiet(700.0), console=True, decisions=board)
    assert len(no_model) == 1 and len(state_of(registry)["rate"]["model_calls"]) == 1


def test_twenty_events_in_a_minute_make_one_post_then_one_combined_post_with_every_owner_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sids = [f"l{i}" for i in range(20)]
    registry = follows(tmp_path, *sids)
    offset = skewed(monkeypatch)
    monkeypatch.setattr(omnigent_agent, "PROMPT_CONFIRM_SECONDS", 0.0)  # batching, not the wait
    og = Leads(**{s: [told("brief", 60), ending("STILL WORKING: on it", 50)] for s in sids})

    def blocked(i: int) -> dict:  # an approval prompt for an even lead, a question for an odd one
        params = (
            {"ask_user_question": {"questions": [{"question": "which?"}]}, "message": "which?"}
            if i % 2
            else {"content_preview": f"Bash(rm {i})"}
        )
        return {"pending_elicitations": [{"elicitation_id": f"e{i}", "params": params}]}

    og.snaps["l0"] = blocked(0)
    waiting: list[str] = []

    def later(poll: int) -> None:
        if poll < 20:
            offset[0] = poll * 3.0
            og.snaps[f"l{poll}"] = blocked(poll)
        else:
            waiting.extend(line["status.line"] for s, line in og.labels if s == "cos")
            offset[0] = 301.0

    supervised(og, registry, monkeypatch, 21, later, notify="cos", console=True)
    first, combined = posted(og)
    assert first.startswith("Supervisor [owner] b-") and "1 event\n" in first
    assert combined.startswith("Supervisor [owner] b-") and "19 events\n" in combined
    for i in range(1, 20):
        to = "act" if i % 2 else "owner"
        assert f"[{to}] l{i} t-l{i}: blocked on a prompt: " in combined
    assert waiting[-1] == "needs you: 9"  # the owner events waiting for the second post
    assert [line["status.line"] for s, line in og.labels if s == "cos"][-1] == ""
    assert state_of(registry)["attention"] == []


def test_the_supervisor_never_approves_a_prompt() -> None:
    source = inspect.getsource(omnigent_agent.supervise_registry)
    assert "approve(" not in source and "resolve(" not in source


def test_an_archived_lead_is_absorbed_with_no_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 60), ending("DONE: shipped", 5))
    og.snap = {"archived": True}
    supervised(og, registry, monkeypatch, 2, notify="cos", console=True)
    assert posted(og) == [] and registry.read_text() == ""
    assert state_of(registry)["leads"] == {}
    assert [(e["kind"], e["action"]) for e in events_of(registry)] == [("archived", "absorb")]


def test_a_done_is_told_to_each_lead_waiting_on_it_and_posted_with_its_report_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = follows(tmp_path, "a", "b", "c", after={"b": "a"})
    og = Leads(
        a=[told("brief", 60)],
        b=[told("brief", 60), ending("STILL WORKING: waiting on lead a", 5)],
        c=[told("brief", 60), ending("STILL WORKING: waiting on CI", 5)],
    )
    og.files[f"{tmp_path / 'a'}.md"] = "DONE: merged\n" + "".join(f"l{n}\n" for n in range(9))

    def done(poll: int) -> None:  # its turn ends during the run, so its row gives the turn end
        if poll == 1:
            og.histories["a"].append(ending("DONE: PR #7 merged\nmore", 0))

    supervised(og, registry, monkeypatch, 3, done, notify="cos", console=True)
    told_b = "Supervisor: a t-a, the lead you wait on, ended: DONE: PR #7 merged"
    assert [(s, t) for s, t in og.inbox if s != "cos"] == [("b", told_b)]
    [post] = posted(og)
    assert (
        f"a t-a: turn ended: DONE: PR #7 merged\nreport {tmp_path / 'a'}.md:\nDONE: merged\n"
        in post
    )
    assert post.endswith("l3")  # the report's first 5 lines
    assert state_of(registry)["leads"]["b"]["waits_on"] == ["a"]


def test_a_failed_lead_is_revived_once_then_goes_to_the_chief_of_staff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 60), ending("STILL WORKING: building", 50))
    og.snap = {"status": "failed", "last_task_error": {"message": "the gate found a failing test"}}
    states = {1: "running", 2: "failed"}
    supervised(
        og,
        registry,
        monkeypatch,
        3,
        lambda p: og.snap.update(status=states[p]),
        notify="cos",
        console=True,
    )
    assert [t for s, t in og.inbox if s == "a"] == [omnigent_agent.LEAD_REVIVE]
    [post] = posted(og)
    assert (
        "[act] a t-a: failed: the gate found a failing test (failed again after its revive)" in post
    )
    assert state_of(registry)["leads"]["a"]["revives"] == 1


# A failure whose text is a transient provider error is revived again with backoff (#904).


def failing_lead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str, polls: int
) -> tuple[Lead, Path, list[float], list[float]]:
    """A followed lead that fails with ``error`` each time it runs, on a clock one minute a poll:
    a revive sets it running for one poll, then it fails again. Returns the times each failure
    was first seen and each revive was sent."""
    clock = [time.time()]
    monkeypatch.setattr(omnigent_agent.time, "time", lambda: clock[0])
    registry = following(tmp_path)
    og = Lead(told("brief", 60), ending("STILL WORKING: building", 50))
    og.snap = {"status": "failed", "last_task_error": {"message": error}}
    failed, revived = [clock[0]], []

    def between(poll: int) -> None:
        sent = [
            i for i in og.histories["a"] if omnigent_agent.text_of(i) == omnigent_agent.LEAD_REVIVE
        ]
        clock[0] += 60
        if len(sent) > len(revived):  # revived this poll: it runs one poll, then fails again
            revived.append(float(sent[-1]["created_at"]))
            og.snap["status"] = "running"
            og.histories["a"].append(ending(f"working {poll}", 0))
        elif og.snap["status"] == "running":
            og.snap["status"] = "failed"
            failed.append(clock[0])

    supervised(og, registry, monkeypatch, polls, between, notify="cos", console=True)
    return og, registry, failed, revived


@pytest.mark.parametrize(
    "error",
    [
        "API error (authentication_failed)",
        "403 Attention Required! | Cloudflare",
        "claude --cloud create: HTTP 503 Service Unavailable",
        "HTTP 500 Internal Server Error",
        "API error: Overloaded",
        "runner lost",  # a turn whose host went away: a reboot, a restart or a lost runner
        "host rebooted",
        "cloud session x made no progress for 1800s; it is left as it is",
        "API error (rate_limit)",  # a rate limit with no reset time: the backoff chain (#932)
    ],
)
def test_a_transient_provider_failure_is_revived_with_backoff_and_reported_only_at_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    og, registry, failed, revived = failing_lead(tmp_path, monkeypatch, error, 150)
    gaps = [round((r - f) / 60) for f, r in zip(failed, revived)]
    assert gaps == [2, 5, 10, 20, 20, 20, 20]  # then every 20, while it fits in about 2 hours
    assert len(failed) == len(revived) + 1  # its last failure is reported, not revived
    assert failed[-1] - failed[0] + 20 * 60 > 2 * 3600 >= revived[-1] - failed[0]
    [post] = posted(og)  # nothing reached the parent before the cap
    assert f"[act] a t-a: failed: {error} (" in post
    assert "transient" in post
    assert state_of(registry)["leads"]["a"]["retrying"]["count"] == 7


def test_a_failure_that_is_no_transient_provider_error_is_still_reported_after_one_revive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og, registry, failed, revived = failing_lead(
        tmp_path, monkeypatch, "the gate found a failing test", 10
    )
    assert len(revived) == 1 and len(failed) == 2
    [post] = posted(og)
    assert (
        "[act] a t-a: failed: the gate found a failing test (failed again after its revive)" in post
    )
    assert "retrying" not in state_of(registry)["leads"]["a"]


def test_a_transient_chain_sends_no_revive_to_a_lead_running_again_and_restarts_after_a_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [time.time()]
    monkeypatch.setattr(omnigent_agent.time, "time", lambda: clock[0])
    registry = following(tmp_path)
    og = Lead(told("brief", 60), ending("STILL WORKING: building", 50))
    og.snap = {"status": "failed", "last_task_error": {"message": "HTTP 502 Bad Gateway"}}
    # poll 1 fails; someone else revives it before its backoff passes; poll 4 its turn ends;
    # poll 5 it fails again, a new chain
    steps = {1: "running", 3: "idle", 4: "failed"}

    def between(poll: int) -> None:
        clock[0] += 60
        if poll in steps:
            og.snap["status"] = steps[poll]
        if poll == 3:
            og.histories["a"].append(ending("STILL WORKING: on CI", 0))

    supervised(og, registry, monkeypatch, 6, between, notify="cos", console=True)
    assert [t for s, t in og.inbox if s == "a"] == []  # running at its due time: not revived
    retrying = state_of(registry)["leads"]["a"]["retrying"]
    assert (retrying["count"], retrying["at"] - retrying["since"]) == (1, 120)
    assert retrying["since"] == clock[0] - 60  # from its second failure, not its first


# A usage limit fails every session alike: they park together, one probe at a time (#932).

WEEKLY = "You've hit your weekly limit · resets Nov 16, 6am (UTC)"


def limited_leads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    polls: int,
    lifts: int | None,
    error: str | Callable[[float], str] = WEEKLY,
    end: str = "idle",
) -> SimpleNamespace:
    """Leads a, b and c, all failing on ``error`` (or ``error(start)``), on a clock one minute a
    poll from a whole minute. A revived lead runs one poll, then fails on the limit again; revive
    number ``lifts`` (the limit lifted) ends as ``end`` says: ``idle``, ``failed`` (another
    error), ``question`` (blocked on one) or ``hold`` (still running), and every revive after
    it ends idle. Returns the fleet ``og``, the ``registry``, the leads ``revived`` in order with
    the clock each revive was sent at (``sent``), the clock's ``start``, and each post with the
    clock it was seen at (``timeline``)."""
    start = time.time() // 60 * 60
    clock = [start]
    monkeypatch.setattr(omnigent_agent.time, "time", lambda: clock[0])
    text = error(start) if callable(error) else error
    registry = follows(tmp_path, "a", "b", "c")
    og = Leads(**{s: [told("brief", 60), ending("STILL WORKING: building", 50)] for s in "abc"})
    for s in "abc":
        og.snaps[s] = {"status": "failed", "last_task_error": {"message": text}}
    run = SimpleNamespace(og=og, registry=registry, revived=[], sent=[], start=start, timeline=[])
    seen, ends = {s: 0 for s in "abc"}, {}

    def between(poll: int) -> None:
        run.timeline += [(clock[0], p) for p in posted(og)[len(run.timeline) :]]
        for s in "abc":
            snap = og.snaps[s]
            sent = sum(
                omnigent_agent.text_of(i) == omnigent_agent.LEAD_REVIVE for i in og.histories[s]
            )
            if sent > seen[s]:  # revived this poll: it runs one poll
                seen[s] = sent
                run.revived.append(s)
                run.sent.append(clock[0])
                if lifts is not None and len(run.revived) >= lifts:
                    ends[s] = end if len(run.revived) == lifts else "idle"
                snap["status"] = "running"
                og.histories[s].append(ending(f"working {poll}", 0))
            elif snap["status"] == "running":
                how = ends.pop(s, "limit")
                if how == "idle":
                    snap["status"] = "idle"
                    og.histories[s].append(ending("STILL WORKING: on CI", 0))
                elif how == "failed":
                    snap.update(status="failed", last_task_error={"message": "a failing test"})
                elif how == "question":
                    snap.update(ASKING, status="idle")
                elif how == "hold":
                    ends[s] = "hold"
                else:
                    snap.update(status="failed", last_task_error={"message": text})
        clock[0] += 60

    supervised(og, registry, monkeypatch, polls, between, notify="cos", console=True)
    run.timeline += [(clock[0], p) for p in posted(og)[len(run.timeline) :]]
    return run


def acts(post: str) -> list[str]:
    return re.findall(r"^\[act\] .*", post, re.MULTILINE)


def resets_text(template: str, after: float) -> Callable[[float], str]:
    """``template`` with ``{time}`` a reset ``after`` seconds past the start, as 6:05am (UTC)."""

    def text(start: float) -> str:
        at = time.gmtime(start + after)
        half = "am" if at.tm_hour < 12 else "pm"
        return template.format(time=f"{at.tm_hour % 12 or 12}:{at.tm_min:02d}{half} (UTC)")

    return text


def test_sessions_failing_on_a_usage_limit_are_reported_once_and_kept_parked_by_a_failed_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = limited_leads(tmp_path, monkeypatch, 12, None)
    resets = omnigent_agent.reset_time(WEEKLY, run.start)
    assert resets is not None
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(resets))
    post = "\n".join(posted(run.og))
    assert acts(post) == [  # no session reported on its own
        f"[act] usage limit: 3 sessions parked, resets {when} (one probe revives them)"
    ]
    # one probe at a time: the first after 2 minutes, the next 5 minutes after the first
    assert run.revived == ["a", "b"]
    leads = state_of(run.registry)["leads"]
    assert all(leads[s]["retrying"]["limit"] for s in "abc")
    assert [e["action"] for e in events_of(run.registry)] == ["park"] * 5  # 3, then 2 probes


LIFTED = "[act] usage limit lifted: 3 sessions resumed (a probe got past the limit)"


def test_a_usage_limit_probe_that_passes_revives_every_parked_session_and_reports_it_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = limited_leads(tmp_path, monkeypatch, 14, 2)
    assert run.revived == ["a", "b", "a", "c"]  # a probe failed, the next passed: the rest revived
    post = "\n".join(posted(run.og))
    assert [line for line in acts(post) if "lifted" in line] == [LIFTED]
    assert all(line.startswith("[act] usage limit") for line in acts(post))
    assert not any("retrying" in lead for lead in state_of(run.registry)["leads"].values())


@pytest.mark.parametrize(
    ("end", "own"),
    [
        ("failed", ("failed", "revive", "failed the first time: revive once")),
        ("question", None),  # its question routes as any other once held past the turn end
    ],
)
def test_a_probe_past_the_limit_that_then_fails_or_asks_still_lifts_it_for_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, end: str, own: tuple[str, str, str] | None
) -> None:
    run = limited_leads(tmp_path, monkeypatch, 14, 2, end=end)
    assert run.revived[:2] == ["a", "b"]  # a's probe failed on the limit; b's got past it
    assert {"a", "c"} <= set(run.revived[2:])  # the other two revived
    post = "\n".join(posted(run.og))
    assert [line for line in acts(post) if "lifted" in line] == [LIFTED]
    lead = state_of(run.registry)["leads"]["b"]
    assert "retrying" not in lead  # out of the parked set: its own ending routes as any other
    if own is None:
        assert lead["state"] == "blocked"
    else:  # a failure past the limit: revived once, as any first failure is
        [last] = [e for e in events_of(run.registry) if e["session"] == "b"][-1:]
        assert (last["kind"], last["action"]) == own[:2] and last["why"].startswith(own[2])


@pytest.mark.parametrize(
    "template",
    [
        "You've hit your session limit · resets {time}",
        "API error (rate_limit) · resets {time}",  # a rate limit naming its reset parks too
    ],
)
def test_a_limit_still_failing_past_its_reset_is_reported_once_the_reset_and_margin_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, template: str
) -> None:
    # the reset 3h in, past the 2h cap a limit with no reset has: probes keep failing past it,
    # and the stored reset, not the 2h, caps them
    after = 3 * 3600
    assert after > omnigent_agent.RETRY_CAP_SECONDS
    run = limited_leads(tmp_path, monkeypatch, 300, None, resets_text(template, after))
    cap = run.start + after + omnigent_agent.LIMIT_MARGIN_SECONDS
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(run.start + after))
    timed = [(at, line) for at, post in run.timeline for line in acts(post)]
    assert timed[0][1].startswith(f"[act] usage limit: 3 sessions parked, resets {when} (")
    own = [(at, line) for at, line in timed if not line.startswith("[act] usage limit")]
    assert len(own) == 1 and "still failing past its reset" in own[0][1]
    # none before the reset + margin, and no later than the first probe to fail after it: one
    # probe interval (the longest backoff), the poll its failure shows on, and the post's wait
    late = omnigent_agent.backoff(len(omnigent_agent.BACKOFF_MINUTES)) + 60
    assert cap < own[0][0] <= cap + late + omnigent_agent.POST_SECONDS
    assert run.start + after in run.sent  # one probe at the reset itself


def test_a_reset_before_the_first_backoff_is_probed_at_the_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = resets_text("You've hit your session limit · resets {time}", 60)
    run = limited_leads(tmp_path, monkeypatch, 4, None, error)
    assert run.sent[0] == run.start + 60  # at the reset, not the backoff's 2 minutes


def test_a_parked_lead_coming_due_while_a_probe_still_runs_starts_no_second_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = limited_leads(tmp_path, monkeypatch, 18, 1, end="hold")
    # the others come due 5 minutes after the probe; it runs on: no second probe within 20 min
    assert run.revived == ["a"]
    leads = state_of(run.registry)["leads"]
    assert all(leads[s]["retrying"]["at"] < run.start + 17 * 60 for s in "bc")


@pytest.mark.parametrize(
    ("text", "now", "resets"),
    [
        (WEEKLY, "2026-11-13T12:00:00+00:00", "2026-11-16T06:00:00+00:00"),
        (WEEKLY, "2026-11-16T07:00:00+00:00", "2027-11-16T06:00:00+00:00"),  # passed: next year
        (
            "hit your limit · resets Jan 2, 6:30pm (Europe/Warsaw)",
            "2026-12-30T00:00:00+00:00",
            "2027-01-02T17:30:00+00:00",
        ),
        (
            "usage limit · resets 6am (UTC)",
            "2026-11-13T07:00:00+00:00",
            "2026-11-14T06:00:00+00:00",
        ),
        (
            "usage limit · resets 12am (UTC)",
            "2026-11-13T07:00:00+00:00",
            "2026-11-14T00:00:00+00:00",
        ),
        ("usage limit · resets Nov 16, 6am (Nowhere/Else)", "2026-11-13T07:00:00+00:00", None),
        ("API error (rate_limit)", "2026-11-13T07:00:00+00:00", None),
    ],
)
def test_a_usage_limits_reset_time_parses_to_the_next_such_utc_instant(
    text: str, now: str, resets: str | None
) -> None:
    at = omnigent_agent.reset_time(text, datetime.fromisoformat(now).timestamp())
    assert at == (resets and datetime.fromisoformat(resets).timestamp())


@pytest.mark.parametrize("runner", ["offline", "revive fails", "online"])
def test_a_lead_past_its_named_wait_with_no_live_runner_is_revived_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: str
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 4000), ending("STILL WORKING: merge queue", 3600))
    if runner != "online":
        og.offline.add("a")  # its info reads runner_online: False
    if runner == "revive fails":
        monkeypatch.setattr(og, "retry", lambda sid: {"recovered": False, "recovery": "no_host"})
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True)
    sent = [t for s, t in og.inbox if s == "a"]
    acts = [line for post in posted(og) for line in post.splitlines() if line.startswith("[act]")]
    if runner == "offline":
        assert sent == [omnigent_agent.LEAD_REVIVE] and acts == []
    elif runner == "revive fails":
        [act] = acts
        assert "the supervisor's revive was not delivered: session a has no live runner and" in act
        assert '"no_host"' in act
    else:
        assert sent == [] and acts == [
            "[act] a t-a: no progress 60m (merge) (no progress while it waits on merge: inspect the wait)"
        ]
    assert state_of(registry)["leads"]["a"]["revives"] == 0  # a lost runner is no failure


def test_fyi_and_decisions_need_console_and_after_needs_a_top_level_start_with_a_registry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = str(tmp_path / "registry")
    assert omnigent_agent.main(["supervise", "--registry", registry, "--fyi", "post"]) == 1
    assert "--fyi and --decisions need --console" in capsys.readouterr().err
    brief = tmp_path / "b.md"
    brief.write_text("x")
    args = ["start", "claude", "t", str(brief), str(tmp_path / "a.md"), "--after", "s9"]
    assert omnigent_agent.main(args) == 1
    assert "--after needs --top-level and a registry" in capsys.readouterr().err


def test_a_top_level_start_after_another_lead_writes_the_after_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    registry, brief = tmp_path / "registry", tmp_path / "b.md"
    brief.write_text("do X")
    args = ["start", "claude", "t", str(brief), str(tmp_path / "a.md"), "--top-level"]
    assert omnigent_agent.main([*args, "--registry", str(registry), "--after", "s9"]) == 0
    line = registry.read_text().strip()
    assert line.endswith("\tfollow\tafter=s9")
    assert omnigent_agent.entry(line)[2:] == (
        [tmp_path / "a.md.brief"],
        False,
        True,
        "s9",
        False,
        "",
    )


def test_a_top_level_start_of_a_manager_writes_its_leads_registry_and_a_re_arm_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    registry, brief = tmp_path / "registry", tmp_path / "b.md"
    brief.write_text("do X")
    args = ["start", "claude", "t", str(brief), str(tmp_path / "a.md"), "--top-level"]
    leads = tmp_path / "app-leads.registry"
    assert omnigent_agent.main([*args, "--registry", str(registry), "--leads", leads.name]) == 0
    line = registry.read_text().strip()
    assert line.endswith(f"\tfollow\tleads={leads}")
    assert omnigent_agent.entry(line)[2:] == (
        [tmp_path / "a.md.brief"],
        False,
        True,
        "",
        False,
        str(leads),
    )
    sid = line.partition("=")[0]
    omnigent_agent.arm(registry, sid, tmp_path / "a.md", followed=True)
    assert omnigent_agent.entry(registry.read_text().strip())[7] == str(leads)
    assert omnigent_agent.main([*args[:-1], "--leads", str(leads)]) == 1  # needs --top-level


def test_a_waiting_lead_on_a_question_is_not_told_so_its_question_gets_no_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = follows(tmp_path, "a", "b", after={"b": "a"})
    og = Leads(a=[told("brief", 60), ending("DONE: merged", 5)], b=[told("brief", 60)])
    question = {"ask_user_question": {"questions": [{"question": "go?"}]}, "message": "go?"}
    og.snaps["b"] = {"pending_elicitations": [{"elicitation_id": "e", "params": question}]}
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True)
    assert [s for s, t in og.inbox] == ["cos"]
    assert "the supervisor's tell was not delivered: it waits on a prompt" in posted(og)[0]


def test_a_supervisor_action_posted_but_unconfirmed_is_not_reported_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = follows(tmp_path, "a", "b", after={"b": "a"})
    og = Leads(a=[told("brief", 60), ending("DONE: merged", 5)], b=[told("brief", 60)])

    def unconfirmed(*a: object, **k: object) -> str:
        raise omnigent_agent.SentUnconfirmed("session b: the message was posted but unconfirmed")

    monkeypatch.setattr(omnigent_agent, "relay", unconfirmed)
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True)
    assert not any("was not delivered" in p for p in posted(og))


def test_a_relay_queued_after_its_own_post_exits_7_when_the_status_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(hold=10**6)
    sid = og.create("claude", "lead")
    info = og.info
    reads = [0]

    def flaky(s: str) -> dict:
        if og.posts:
            reads[0] += 1
            if reads[0] == 2:  # the read that says why it is queued
                raise omnigent_agent.Fail("GET info: down")
        return info(s)

    og.info = flaky  # type: ignore[method-assign]
    assert relay(og, sid, tmp_path, monkeypatch) == omnigent_agent.SENT_UNCONFIRMED
    assert og.posts == 1


def test_a_relay_queued_behind_an_earlier_copy_keeps_exit_1_when_the_status_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(hold=10**6)
    sid = og.create("claude", "lead")
    og.send(sid, "[relayed by cos]\ngo")  # an earlier copy, pending
    info = og.info
    reads = [0]

    def flaky(s: str) -> dict:
        reads[0] += 1
        if reads[0] == 4:  # relay's, send's, its poll's, then the one after the queued return
            raise omnigent_agent.Fail("GET info: down")
        return info(s)

    og.info = flaky  # type: ignore[method-assign]
    assert relay(og, sid, tmp_path, monkeypatch) == 1
    assert reads[0] == 4 and og.posts == 1


def test_a_send_whose_copy_was_already_there_and_whose_poll_fails_is_exit_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(hold=10**6)
    sid = og.create("claude", "lead")
    og.send(sid, "[relayed by cos]\ngo")  # an earlier copy, pending
    og.items = lambda s: (_ for _ in ()).throw(omnigent_agent.Fail("GET items: down"))  # type: ignore[method-assign]
    info = og.info
    calls = [0]

    def flaky(s: str) -> dict:
        calls[0] += 1
        if calls[0] > 2:
            raise omnigent_agent.Fail("GET info: down")
        return info(s)

    og.items = lambda s: og.sessions[s]  # type: ignore[method-assign]
    og.info = flaky  # type: ignore[method-assign]
    assert relay(og, sid, tmp_path, monkeypatch) == 1
    assert og.posts == 1


@pytest.mark.parametrize("why", ["model failed", "allowance spent"])
def test_a_quiet_end_the_model_could_not_route_is_nudged_and_posts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, why: str
) -> None:
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    monkeypatch.setattr(omnigent_agent, "triage", REAL_TRIAGE)
    model_answers(monkeypatch, FileNotFoundError("claude"))
    if why == "allowance spent":
        state = omnigent_agent.fresh_state()
        state["rate"]["model_calls"] = [time.time()] * 30
        registry.with_suffix(".state.json").write_text(json.dumps(state))
    og = Lead(told("brief", 60), ending("Reading code.", 0))
    supervised(
        og,
        registry,
        monkeypatch,
        2,
        lambda p: offset.__setitem__(0, 301.0),
        notify="cos",
        console=True,
    )
    assert [s for s, t in og.inbox] == ["a"]  # a nudge to the lead, no post to the chief of staff
    assert events_of(registry)[-1]["action"] == "nudge"
    assert posted(og) == []


@pytest.mark.parametrize("needed_by", ["action", "model call", "post"])
def test_a_failed_state_write_stops_the_side_effect_it_guards_and_the_next_poll_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_model: list[str], needed_by: str
) -> None:
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    last = {"action": "STILL WORKING: on it", "model call": "Reading code.", "post": "DONE: x"}
    og = Lead(told("brief", 60), ending(last[needed_by], 0))
    og.snap = {"status": "running"} if needed_by == "action" else {}
    real, full = omnigent_agent.write_state, [False]

    def write_state(path: Path, state: dict) -> None:
        if full[0]:
            raise OSError("disk full")
        real(path, state)

    monkeypatch.setattr(omnigent_agent, "write_state", write_state)
    at = {"action": 1201.0, "model call": 301.0, "post": 0.0}[needed_by]
    seen: list[tuple] = []

    def later(poll: int) -> None:
        seen.append((list(og.inbox), list(no_model)))
        offset[0] = at
        full[0] = poll == 1  # the disk is full for the poll that needs the write

    if needed_by == "post":
        full[0] = True
    supervised(og, registry, monkeypatch, 4, later, notify="cos", console=True)
    if needed_by == "post":
        assert seen[0] == ([], [])  # the post's pending write failed: nothing sent
        assert posted(og)[0].startswith("Supervisor [act] b-") and len(posted(og)) == 1
    else:
        assert seen[1] == ([], [])  # the poll with the full disk sent nothing, asked nothing
        if needed_by == "action":
            assert [t for s, t in og.inbox if s == "a"] == [omnigent_agent.LEAD_NUDGE]
        else:
            assert len(no_model) == 1  # asked once the write succeeds
    assert [i["status"] for i in state_of(registry)["intents"]] == ["done"] * (
        needed_by == "action"
    )


def test_a_crash_while_telling_the_first_waiting_lead_leaves_the_others_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = follows(tmp_path, "a", "b", "c", after={"b": "a", "c": "a"})
    og = Leads(
        a=[told("brief", 60), ending("DONE: shipped", 5)],
        b=[told("brief", 60)],
        c=[told("brief", 60)],
    )
    real = og.send

    def crash(sid: str, text: str) -> None:
        real(sid, text)
        if sid == "b":
            raise Stop  # b was told; the supervisor dies before c's turn

    og.send = crash  # type: ignore[method-assign]
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True)
    assert sorted(i["session"] for i in state_of(registry)["intents"]) == ["b", "c"]
    og.send = real  # type: ignore[method-assign]
    supervised(og, registry, monkeypatch, 2, notify="cos", console=True)  # the restart
    assert [s for s, t in og.inbox if s != "cos"] == ["b"]  # never sent again
    status = {i["session"]: i["status"] for i in state_of(registry)["intents"]}
    assert status == {"b": "done", "c": "escalated"}
    assert re.search(
        r"^\[act\] c( t-c)?: the supervisor's tell may not have reached it",
        posted(og)[0],
        re.MULTILINE,
    )


def test_a_late_retry_of_a_pending_post_reserves_its_own_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = follows(tmp_path, "a", "b")
    offset = skewed(monkeypatch)
    og = Leads(a=[told("brief", 60), ending("DONE: one", 5)], b=[told("brief", 60)])
    og.histories["cos"] = []
    real, at = og.send, []

    def crash(sid: str, text: str) -> None:
        if sid == "cos":
            raise Stop  # the first attempt never lands

    og.send = crash  # type: ignore[method-assign]
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True)

    def record(sid: str, text: str) -> None:
        at.append(offset[0]) if sid == "cos" else None
        real(sid, text)

    og.send = record  # type: ignore[method-assign]
    offset[0] = 299.0
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True)  # the restart: a retry
    og.histories["b"].append(ending("DONE: two", 0))
    for t in (301.0, 598.0, 600.0):
        offset[0] = t
        supervised(og, registry, monkeypatch, 1, notify="cos", console=True)
    assert at == [299.0, 600.0]


def test_a_crash_right_after_a_terminal_line_is_dropped_keeps_its_owner_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "registry"
    registry.write_text(f"a={tmp_path / 'a.md'}\n")
    og = Lead(told("brief", 60))
    prompt = {"elicitation_id": "e1", "params": {"content_preview": "Bash(rm example)"}}
    og.snap = {"pending_elicitations": [prompt]}
    arm = omnigent_agent.arm

    def crash(*args: object, **kwargs: object) -> None:
        arm(*args, **kwargs)  # type: ignore[arg-type]
        raise Stop  # the line is gone; the supervisor dies before the poll's end

    monkeypatch.setattr(omnigent_agent, "arm", crash)
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True)
    assert registry.read_text() == ""
    monkeypatch.setattr(omnigent_agent, "arm", arm)
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True)  # the restart
    [post] = posted(og)
    assert post.startswith("Supervisor [owner] b-")
    assert "[owner] a t-a: blocked on a prompt: e1: Bash(rm example)" in post


def test_an_older_identical_message_does_not_close_an_open_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    og = Lead(told(omnigent_agent.LEAD_NUDGE, 600), ending("STILL WORKING: waiting on CI", 5))
    state = omnigent_agent.fresh_state()
    intent = {"id": "nudge:a:e2", "session": "a", "action": "nudge", "event": "e2"}
    intent.update(text=omnigent_agent.LEAD_NUDGE, at=time.time(), status="open")
    state["intents"] = [intent]  # a crash before its send; the nudge in the history is 10 min old
    registry.with_suffix(".state.json").write_text(json.dumps(state))
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True)
    assert state_of(registry)["intents"][0]["status"] == "escalated"
    assert "the supervisor's nudge may not have reached it" in "\n".join(posted(og))
    assert [s for s, t in og.inbox if s == "a"] == []  # never sent again


@pytest.mark.parametrize("held", ["post", "intent"])
def test_a_copy_still_pending_after_a_crash_counts_as_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held: str
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 60), ending("DONE: shipped" if held == "post" else "on it", 5))
    og.histories["cos"] = []
    to = "cos" if held == "post" else "a"
    pending: list[dict] = []  # the session took the copy and has not folded it in yet

    def hold(sid: str, text: str) -> None:
        assert sid == to  # nothing else is sent
        pending.append(
            {"pending_id": f"p{len(pending)}", "content": [{"type": "input_text", "text": text}]}
        )
        raise Stop

    if held == "intent":
        state = omnigent_agent.fresh_state()
        intent = {"id": "nudge:a:e1", "session": "a", "action": "nudge", "event": "e1"}
        intent.update(text=omnigent_agent.LEAD_NUDGE, at=time.time(), status="open")
        state["intents"] = [intent]  # a crash after its send
        registry.with_suffix(".state.json").write_text(json.dumps(state))
        pending.append(
            {"pending_id": "p0", "content": [{"type": "input_text", "text": intent["text"]}]}
        )
    info = og.info
    og.info = lambda sid: {**info(sid), **({"pending_inputs": pending} if sid == to else {})}  # type: ignore[method-assign]
    og.send = hold  # type: ignore[method-assign]
    for _ in range(2 if held == "post" else 1):  # a post: the crash, then the restart
        supervised(og, registry, monkeypatch, 1, notify="cos", console=True)
    assert len(pending) == 1
    final = state_of(registry)
    assert final["pending_posts"] == [] and final["attention"] == []
    if held == "intent":
        assert final["intents"][0]["status"] == "done"


def test_a_write_failing_after_the_model_answered_keeps_the_spent_call_and_its_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    state = omnigent_agent.fresh_state()
    state["rate"]["model_calls"] = [time.time()] * 29
    registry.with_suffix(".state.json").write_text(json.dumps(state))
    og = Lead(told("brief", 60), ending("Reading code.", 0))
    calls: list[tuple] = []
    monkeypatch.setattr(
        omnigent_agent, "triage", lambda *args: calls.append(args) or ("act", "stalled")
    )
    real, failed = omnigent_agent.write_state, []

    def write_state(path: Path, state: dict) -> None:
        if state["intents"] and not failed:  # the nudge's intent, once: the model has answered
            failed.append(path)
            raise OSError("disk full")
        real(path, state)

    monkeypatch.setattr(omnigent_agent, "write_state", write_state)
    seen: list[tuple] = []

    def later(poll: int) -> None:
        seen.append((len(calls), len(state_of(registry)["rate"]["model_calls"]), list(og.inbox)))
        offset[0] = 301.0

    supervised(og, registry, monkeypatch, 3, later, notify="cos", console=True)
    assert seen[1] == (1, 30, [])  # the write failed: one call, spent and kept, nothing sent
    assert len(calls) == 1  # the retry acts on the answer it has
    assert [t for s, t in og.inbox] == [omnigent_agent.LEAD_NUDGE]
    assert len(state_of(registry)["rate"]["model_calls"]) == 30


def test_an_ask_after_a_done_line_is_the_ending_and_goes_to_the_manager() -> None:
    text = "DONE: #271 merged.\nUSER NEEDED: pick option A or B"
    assert omnigent_agent.ending_of(text) == "USER NEEDED"
    assert omnigent_agent.ending_of("DONE: merged\nPARKED: later") == "DONE"
    assert omnigent_agent.ending_of("no ending") is None
    assert (
        omnigent_agent.lead_status({"status": "idle"}, [ending(text, 0)])["ending"] == "USER NEEDED"
    )
    event = {"kind": "ended", "last": text}
    assert omnigent_agent.route(event, lambda: ("none", "x"))[:2] == ("rule", "act")


def _agent_server(monkeypatch: pytest.MonkeyPatch, pages: dict[str, object]) -> list[tuple]:
    """``Omnigent.call`` answering from ``pages`` by path; the calls it saw are returned."""
    calls: list[tuple] = []

    def call(self, method: str, path: str, body: dict | None = None) -> object:
        path = path.removesuffix(omnigent_agent.SLIM)
        calls.append((method, path, body))
        return pages[path]

    monkeypatch.setattr(omnigent_agent.Omnigent, "call", call)
    monkeypatch.setattr(
        omnigent_agent.Omnigent, "__init__", lambda self: setattr(self, "host_id", "h")
    )
    monkeypatch.delenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", raising=False)
    return calls


def test_a_turn_ended_with_only_an_artifact_monitor_reads_idle_and_a_shell_keeps_it_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor = {"type": "monitor", "status": "running", "description": "live updates for artifact"}
    shell = {"type": "shell", "status": "running", "command": "sleep 120"}
    old = time.time() - omnigent_agent.IDLE_GRACE_SECONDS - 1
    reply = {"id": "i2", "type": "message", "role": "assistant", "status": "completed"}
    reply["created_at"] = old
    fresh = {**reply, "created_at": time.time()}  # narration a running turn persisted just now
    call = {"id": "i3", "type": "function_call", "status": "completed", "created_at": old}
    for tasks, last, status in (
        ([monitor], reply, "idle"),
        ([monitor], fresh, "running"),  # its tool call is still to land
        ([monitor, shell], reply, "running"),
        ([shell], reply, "running"),
        ([monitor], call, "running"),  # mid-turn: a tool call is the last item
        ([], reply, "running"),
    ):
        _agent_server(
            monkeypatch,
            {
                "/v1/sessions/m": {"status": "running", "title": "t", "background_tasks": tasks},
                "/v1/sessions/m/items?limit=100&order=desc": {"data": [last]},
            },
        )
        og = omnigent_agent.Omnigent()
        # the snapshot stays as the server says, so wait and reap, which a long foreground tool
        # misread as ended would end or archive, never take it
        assert og.info("m")["status"] == "running"
        _, line = omnigent_agent.observe(og, "m", {})
        assert line.startswith("m t: turn ended" if status == "idle" else "m t: running")
        # the supervisor's deadline rule reads this state: an idle lead that ended its turn waits
        assert omnigent_agent.lead_status(og.info("m"), [last])["state"] == status, (tasks, last)


def test_a_users_own_agent_is_found_after_the_server_s_pages_and_gets_its_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _agent_server(
        monkeypatch,
        {
            "/v1/agents": {"data": [{"name": "s1", "id": "x"}], "has_more": True, "last_id": "x"},
            "/v1/agents?after=x": {"data": [{"name": "s2", "id": "y"}], "has_more": False},
            "/v1/agents?scope=user": {
                "data": [{"name": "mine0", "id": "m0"}],
                "has_more": True,
                "last_id": "m0",
            },
            "/v1/agents?scope=user&after=m0": {"data": [{"name": "claude-cloud", "id": "cc"}]},
            "/v1/projects": {"data": []},
            "/v1/sessions": {"id": "s1"},
        },
    )
    omnigent_agent.Omnigent().create("claude-cloud", "t", permission_mode="plan")
    assert calls[-1][2]["agent_id"] == "cc"
    assert "terminal_launch_args" not in calls[-1][2]  # a claude flag, not this agent's


def test_an_unknown_agent_fails_naming_it_before_any_session_is_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _agent_server(
        monkeypatch, {"/v1/agents": {"data": []}, "/v1/agents?scope=user": {"data": []}}
    )
    with pytest.raises(omnigent_agent.Fail, match="agent nope not registered"):
        omnigent_agent.Omnigent().create("nope", "t")
    assert all(path != "/v1/sessions" for _, path, _ in calls)


def _copies(
    config_b: dict, listed: tuple[str, str] = ("c1", "c2"), record_b: dict | None = None
) -> dict[str, object]:
    """Pages for user agents c1 (older) and c2 (newer), both called claude-cloud, listed in
    ``listed`` order, c2's config and record carrying ``config_b`` and ``record_b`` on top of
    c1's."""
    same = {"policies": [], "skills": ["s"], "terminals": [], "mcp_servers_editable": True}
    record = {
        "c1": {"created_at": 100, **same},
        "c2": {"created_at": 200, **same, **(record_b or {})},
    }
    first, second = ({"name": "claude-cloud", "id": i, **record[i]} for i in listed)
    config = {"name": "claude-cloud", "harness": "acp:claude-cloud", "instructions": None}
    return {
        "/v1/agents": {"data": []},
        "/v1/agents?scope=user": {"data": [first, second, {"name": "solo", "id": "c3"}]},
        "/v1/agents/c1/config": {"id": "c1", "version": 1, **config},
        "/v1/agents/c2/config": {"id": "c2", "version": 2, **config, **config_b},
        "/v1/projects": {"data": []},
        "/v1/sessions": {"id": "s1"},
    }


def test_a_name_held_by_identical_copies_resolves_to_the_newest_in_any_listing_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for listed in (("c1", "c2"), ("c2", "c1")):
        calls = _agent_server(monkeypatch, _copies({}, listed))
        omnigent_agent.Omnigent().create("claude-cloud", "t")
        assert calls[-1][2]["agent_id"] == "c2"
    bookkeeping = {"updated_at": 300, "object": "agent"}
    for config_b, record_b in (({"created_at": 200, "updated_at": 300}, {}), ({}, bookkeeping)):
        calls = _agent_server(monkeypatch, _copies(config_b, record_b=record_b))
        omnigent_agent.Omnigent().create("claude-cloud", "t")
        assert calls[-1][2]["agent_id"] == "c2"


def test_a_name_held_by_agents_that_differ_is_refused_with_their_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for config_b, record_b in (
        ({"instructions": "other"}, {}),
        ({"harness": "claude-native"}, {}),
        (
            {},
            {"policies": [{"name": "p"}]},
        ),  # policies, skills and terminals are only on the record
        ({}, {"skills": ["s", "t"]}),
        ({}, {"mcp_servers_editable": False}),  # a server flag, not bookkeeping
    ):
        calls = _agent_server(monkeypatch, _copies(config_b, record_b=record_b))
        with pytest.raises(omnigent_agent.Fail, match="agents c1, c2 are all called claude-cloud"):
            omnigent_agent.Omnigent().create("claude-cloud", "t")
        assert all(path != "/v1/sessions" for _, path, _ in calls)


def test_an_alias_takes_the_server_s_native_agent_and_other_shared_names_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("claude-native-ui", "custom"):
        record = {"name": name, "builtin": name != "custom", "created_at": 100}
        calls = _agent_server(
            monkeypatch,
            {
                "/v1/agents": {"data": [{"id": "srv", **record}]},
                # newer and listed second; only a user agent has a config, the server's is 404
                "/v1/agents?scope=user": {"data": [{"id": "usr", **record, "created_at": 200}]},
                "/v1/projects": {"data": []},
                "/v1/sessions": {"id": "s1"},
            },
        )
        if name == "custom":  # a user's own agent may share a server name on purpose
            with pytest.raises(omnigent_agent.Fail, match="agents srv, usr are all called custom"):
                omnigent_agent.Omnigent().create(name, "t")
        else:
            omnigent_agent.Omnigent().create("claude", "t")
            assert calls[-1][2]["agent_id"] == "srv"
        assert all("/config" not in path for _, path, _ in calls)


def test_an_agent_is_taken_by_its_id_even_when_others_share_its_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _agent_server(monkeypatch, _copies({}))
    omnigent_agent.Omnigent().create("c1", "t")
    assert calls[-1][2]["agent_id"] == "c1"
    omnigent_agent.Omnigent().create("solo", "t")
    assert calls[-1][2]["agent_id"] == "c3"
    assert all("/config" not in path for _, path, _ in calls)  # configs only for an ambiguous name


def test_an_agent_outside_the_aliases_gets_its_brief_inline(tmp_path: Path) -> None:
    og = FakeOmnigent()
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(og, "claude-cloud", "t", "do X", report, None, 0)
    sent = next(i for i in og.items(sid) if i["type"] == "message")["content"][0]["text"]
    assert sent.startswith("Work on t. Your brief:") and sent.endswith("do X")
    assert not omnigent_agent.brief_of(report).exists()
    with pytest.raises(omnigent_agent.Fail, match="for claude sessions"):
        omnigent_agent.start(og, "claude-cloud", "t", "x", report, None, 0, permission_mode="plan")


def test_start_says_when_the_environment_s_permission_mode_is_not_applied_and_creates_as_before(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OMNIGENT_PERMISSION_MODE", "bypassPermissions")
    og = FakeOmnigent()
    og.harnesses["agy"] = "antigravity-native"
    sid, _ = omnigent_agent.start(og, "codex", "t", "do X", tmp_path / "r.md", None, 0)
    assert sid in og.sessions
    monkeypatch.setenv(
        "OMNIGENT_PERMISSION_MODE", "acceptEdits"
    )  # a Claude word, which codex never takes
    omnigent_agent.start(og, "codex", "t2", "do X", tmp_path / "r.md", None, 0)
    for mode in ("default", "bypassPermissions"):  # the two words codex's start takes
        omnigent_agent.start(
            og, "codex", f"t-{mode}", "do X", tmp_path / "r.md", None, 0, permission_mode=mode
        )
    monkeypatch.setenv("OMNIGENT_PERMISSION_MODE", "bypassPermissions")
    # codex starts in no-approvals whatever it says, so it is never told a mode was dropped
    assert "not applied" not in capsys.readouterr().err
    for agent in ("claude", "agy"):  # each takes this mode, so nothing is dropped
        omnigent_agent.start(og, agent, f"t-{agent}", "do X", tmp_path / "r.md", None, 0)
        assert "not applied" not in capsys.readouterr().err
    monkeypatch.setenv("OMNIGENT_PERMISSION_MODE", "plan")  # agy takes bypass alone
    omnigent_agent.start(og, "agy", "t-plan", "do X", tmp_path / "r.md", None, 0)
    assert "agy takes no permission mode: plan was not applied" in capsys.readouterr().err
    # A codex create gets codex's own no-approvals flag and no mode field.
    calls = _agent_server(
        monkeypatch,
        {
            "/v1/agents": [{"name": n, "id": a} for a, n in omnigent_agent.AGENTS.items()],
            "/v1/agents?scope=user": [],
            "/v1/projects": {"data": []},
            "/v1/sessions": {"id": "s1"},
        },
    )
    omnigent_agent.Omnigent().create("codex", "t")
    body = calls[-1][2]
    assert body["terminal_launch_args"] == ["--dangerously-bypass-approvals-and-sandbox"]
    assert "permission_mode" not in body


def test_a_claude_cloud_start_sends_the_permission_mode_in_the_create_body_and_warns_of_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    og.harnesses = {"claude-cloud": omnigent_agent.CLOUD_HARNESS}
    monkeypatch.setenv("OMNIGENT_PERMISSION_MODE", "bypassPermissions")
    omnigent_agent.start(og, "claude-cloud", "t", "do X", tmp_path / "r.md", None, 0)
    assert "OMNIGENT_PERMISSION_MODE=bypassPermissions is not applied" in capsys.readouterr().err
    omnigent_agent.start(
        og, "claude-cloud", "t2", "do X", tmp_path / "r.md", None, 0, permission_mode="auto"
    )
    assert "not applied" not in capsys.readouterr().err
    monkeypatch.setenv(
        "OMNIGENT_PERMISSION_MODE", "plan"
    )  # the variable alone never sets a cloud mode
    omnigent_agent.start(og, "claude-cloud", "t3", "do X", tmp_path / "r.md", None, 0)
    assert "OMNIGENT_PERMISSION_MODE=plan is not applied" in capsys.readouterr().err
    assert og.modes["s2"] is None
    calls = _agent_server(monkeypatch, {"/v1/projects": {"data": []}, "/v1/sessions": {"id": "s1"}})
    harnesses = {"claude-cloud": omnigent_agent.CLOUD_HARNESS, "custom": "acp:other"}
    monkeypatch.setattr(
        omnigent_agent.Omnigent,
        "agent",
        lambda self, name: {"id": name, "harness": harnesses[name]},
    )

    def sent(agent: str, mode: str | None) -> dict:
        omnigent_agent.Omnigent().create(agent, "t", permission_mode=mode)
        return calls[-1][2]

    monkeypatch.delenv("OMNIGENT_PERMISSION_MODE")
    assert sent("claude-cloud", "bypassPermissions")["permission_mode"] == "bypassPermissions"
    assert sent("claude-cloud", "auto")["permission_mode"] == "auto"
    for agent, mode in (("claude-cloud", None), ("claude-cloud", "plan"), ("custom", "auto")):
        body = sent(agent, mode)
        assert "permission_mode" not in body and "terminal_launch_args" not in body
    monkeypatch.setenv("OMNIGENT_PERMISSION_MODE", "bypassPermissions")  # the env never gives it
    assert "permission_mode" not in sent("claude-cloud", None)


def test_a_claude_cloud_start_sends_no_permission_mode_unless_told(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OMNIGENT_PERMISSION_MODE", raising=False)
    og = FakeOmnigent()
    og.harnesses = {"claude-cloud": omnigent_agent.CLOUD_HARNESS}
    omnigent_agent.start(og, "claude-cloud", "t", "do X", tmp_path / "r.md", None, 0)
    assert og.modes["s0"] is None
    omnigent_agent.start(
        og, "claude-cloud", "t2", "do X", tmp_path / "r.md", None, 0, permission_mode="auto"
    )
    assert og.modes["s1"] == "auto"


def test_antigravity_runs_a_role_only_with_its_approvals_bypassed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OMNIGENT_PERMISSION_MODE", raising=False)
    og = FakeOmnigent()
    og.harnesses["agy"] = "antigravity-native"
    with pytest.raises(omnigent_agent.Fail, match="blocks unattended"):
        omnigent_agent.start(og, "agy", "t", "x", tmp_path / "r.md", "verifier.md", 0)
    with pytest.raises(omnigent_agent.Fail, match="for claude sessions"):
        omnigent_agent.start(
            og, "agy", "t", "x", tmp_path / "r.md", None, 0, permission_mode="plan"
        )
    omnigent_agent.start(
        og,
        "agy",
        "t",
        "x",
        tmp_path / "r.md",
        "verifier.md",
        0,
        permission_mode="bypassPermissions",
    )
    calls = _agent_server(
        monkeypatch,
        {
            "/v1/agents": {"data": [{"name": "agy", "id": "a", "harness": "antigravity-native"}]},
            "/v1/agents?scope=user": [],
            "/v1/projects": {"data": []},
            "/v1/sessions": {"id": "s1"},
        },
    )
    omnigent_agent.Omnigent().create("agy", "t", permission_mode="bypassPermissions")
    assert calls[-1][2]["terminal_launch_args"] == ["--dangerously-skip-permissions"]
    # Effort is the user's agy settings; the launcher passes none, whatever the environment says.
    monkeypatch.setenv("OMNIGENT_EFFORT_ANTIGRAVITY", "high")
    omnigent_agent.Omnigent().create("agy", "t")
    assert "terminal_launch_args" not in calls[-1][2]


def test_a_native_agent_outside_the_aliases_reads_its_brief_from_a_file(tmp_path: Path) -> None:
    og = FakeOmnigent()
    og.harnesses["antigravity-native-ui"] = "antigravity-native"
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(og, "antigravity-native-ui", "t", "do X", report, None, 0)
    sent = next(i for i in og.items(sid) if i["type"] == "message")["content"][0]["text"]
    brief = omnigent_agent.brief_of(report)
    assert sent == f"Work on t: read {brief} and do what it says."
    assert brief.read_text().endswith("do X")


def test_reloadable_takes_a_replaced_script_once_it_is_stable_and_compiles(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "omnigent_agent.py"
    script.write_text("x = 1\n")
    ran = script.stat().st_mtime_ns
    seen = {"ran": ran, "last": ran}
    assert not omnigent_agent.reloadable(script, seen)  # unchanged
    os.utime(script, ns=(ran + 10**9, ran + 10**9))
    assert not omnigent_agent.reloadable(script, seen)  # changed: wait one look
    assert omnigent_agent.reloadable(script, seen)  # stable and compiles
    script.write_text("def broken(:\n")
    os.utime(script, ns=(ran + 2 * 10**9, ran + 2 * 10**9))
    assert not omnigent_agent.reloadable(script, seen)
    assert not omnigent_agent.reloadable(script, seen)
    assert "does not load" in capsys.readouterr().err
    assert not omnigent_agent.reloadable(script, seen)  # logged once, kept running
    assert capsys.readouterr().err == ""
    script.unlink()
    assert not omnigent_agent.reloadable(script, seen)  # an install replacing it


def test_the_supervisor_re_executes_itself_when_its_script_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "omnigent_agent.py"
    script.write_text("x = 1\n")
    registry = following(tmp_path)
    og = Lead(told("brief", 60))
    execs: list[tuple[str, list[str]]] = []

    def execv(path: str, argv: list[str]) -> None:
        execs.append((path, argv))
        raise Stop

    monkeypatch.setattr(omnigent_agent.os, "execv", execv)
    monkeypatch.setattr(omnigent_agent.sys, "argv", ["agent.py", "supervise", "--registry", "r"])
    bump = lambda poll: os.utime(script, ns=(10**18, 10**18)) if poll == 1 else None
    supervised(og, registry, monkeypatch, 5, bump, console=True, script=script)
    assert execs == [
        (sys.executable, [sys.executable, str(script), "supervise", "--registry", "r"])
    ]
    assert f"restarting: {script} changed" in capsys.readouterr().out
    assert state_of(registry)["leads"]["a"]  # written before the exec


def test_a_failed_state_write_before_the_restart_keeps_the_old_process_until_one_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "omnigent_agent.py"
    script.write_text("x = 1\n")
    registry = following(tmp_path)
    og = Lead(told("brief", 60))
    real_reload, real_write = omnigent_agent.reloadable, omnigent_agent.write_state
    ready, seen_order = [False], []  # "fail", "ok" per write once the script changed, "exec"

    def reloadable(path: Path, seen: dict[str, int]) -> bool:
        ready[0] = real_reload(path, seen)
        return ready[0]

    def write_state(path: Path, state: dict) -> None:
        if ready[0] and "fail" not in seen_order:  # the first write at the restart fails
            seen_order.append("fail")
            raise OSError("disk full")
        real_write(path, state)
        if "fail" in seen_order:
            seen_order.append("ok")

    def execv(path: str, argv: list[str]) -> None:
        seen_order.append("exec")
        raise Stop

    monkeypatch.setattr(omnigent_agent, "reloadable", reloadable)
    monkeypatch.setattr(omnigent_agent, "write_state", write_state)
    monkeypatch.setattr(omnigent_agent.os, "execv", execv)
    bump = lambda poll: os.utime(script, ns=(10**18, 10**18)) if poll == 1 else None
    supervised(og, registry, monkeypatch, 6, bump, console=True, script=script)
    assert seen_order[0] == "fail"  # the restart's write failed once
    assert seen_order[-1] == "exec" and seen_order.count("exec") == 1
    assert seen_order[-2] == "ok"  # no exec after the failure; one right after a write succeeded


def test_the_supervisor_restarts_only_once_its_unposted_lines_are_posted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "omnigent_agent.py"
    script.write_text("x = 1\n")
    registry = tmp_path / "registry"
    report = aged(tmp_path / "a.md", 10)
    registry.write_text(f"a={report}\n")
    og = Fleet(a=[told("brief", 60)])
    og.refuse = 4  # the notify session refuses the posts of the first polls
    inbox_at_exec: list[list[tuple[str, str]]] = []

    def execv(path: str, argv: list[str]) -> None:
        inbox_at_exec.append(list(og.inbox))
        raise Stop

    monkeypatch.setattr(omnigent_agent.os, "execv", execv)
    bump = lambda poll: os.utime(script, ns=(10**18, 10**18)) if poll == 1 else None
    supervised(og, registry, monkeypatch, 10, bump, notify="cos", script=script)
    assert og.refuse == 0
    assert inbox_at_exec == [[("cos", f"Supervisor: a t-a: report written: {report}")]]
    waits = f"{script} changed; the restart waits for its lines to post"
    assert capsys.readouterr().err.count(waits) == 1  # once, while the posts were refused


def test_a_restart_waiting_on_an_event_to_settle_says_so_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "omnigent_agent.py"
    script.write_text("x = 1\n")
    registry = tmp_path / "registry"
    registry.write_text(f"a={aged(tmp_path / 'a.md', 10)}\n")
    og = Fleet(a=[told("brief", 60)])
    real_write, failing, execs = omnigent_agent.write_state, [4], []

    def write_state(path: Path, state: dict) -> None:
        if failing[0]:  # the report's event is not settled until the state is written
            failing[0] -= 1
            raise OSError("disk full")
        real_write(path, state)

    def execv(path: str, argv: list[str]) -> None:
        execs.append(path)
        raise Stop

    monkeypatch.setattr(omnigent_agent, "write_state", write_state)
    monkeypatch.setattr(omnigent_agent.os, "execv", execv)
    bump = lambda poll: os.utime(script, ns=(10**18, 10**18)) if poll == 1 else None
    supervised(og, registry, monkeypatch, 10, bump, console=True, script=script)
    assert execs == [sys.executable]
    err = capsys.readouterr().err
    assert err.count(f"{script} changed; the restart waits for its events to settle") == 1
    assert "lines to post" not in err


def test_a_restart_with_nothing_waiting_logs_no_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "omnigent_agent.py"
    script.write_text("x = 1\n")

    def execv(path: str, argv: list[str]) -> None:
        raise Stop

    monkeypatch.setattr(omnigent_agent.os, "execv", execv)
    bump = lambda poll: os.utime(script, ns=(10**18, 10**18)) if poll == 1 else None
    supervised(
        Lead(told("brief", 60)),
        following(tmp_path),
        monkeypatch,
        5,
        bump,
        console=True,
        script=script,
    )
    assert "the restart waits" not in capsys.readouterr().err


def test_a_re_exec_drops_the_session_pairs_its_registry_already_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, report = tmp_path / "registry", tmp_path / "a.md"
    argv = ["agent.py", "supervise", "--registry", str(registry), f"a={report}"]
    monkeypatch.setattr(omnigent_agent.sys, "argv", argv)
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: Fleet())
    replayed: list[list[str]] = []

    def supervise_registry(*_: object, **__: object) -> None:  # it never returns
        replayed.append(sys.argv[1:])
        raise Stop

    monkeypatch.setattr(omnigent_agent, "supervise_registry", supervise_registry)
    with pytest.raises(Stop):
        omnigent_agent.main(argv[1:])
    assert f"a={report.resolve()}" in registry.read_text()  # armed
    assert replayed == [["supervise", "--registry", str(registry)]]  # a restart re-arms nothing


def test_relay_from_a_task_never_answers_an_open_question_and_says_whose_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent(hold=10**6)  # the question holds every message behind it
    sid = og.create("claude", "cos")
    og.prompts = [QUESTION]
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.setattr(omnigent_agent, "QUEUED_SECONDS", 0)
    (tmp_path / "m.md").write_text("ACTION NEEDED: the sync printed a note")
    argv = ["relay", sid, str(tmp_path / "m.md"), "--from", "skills-sync", "--answer"]
    assert omnigent_agent.main(argv) == omnigent_agent.QUEUED
    assert og.resolved == []
    assert og.posts == 1
    header = omnigent_agent.RELAY_FROM.format(source="skills-sync")
    assert "Which base?" in capsys.readouterr().err  # it names the question that holds it
    sent = [omnigent_agent.text_of(p) for p in og.info(sid)["pending_inputs"]]
    assert sent == [f"{header}\nACTION NEEDED: the sync printed a note"]


def test_relay_from_a_task_to_an_idle_session_is_delivered_with_its_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "cos")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    (tmp_path / "m.md").write_text("synced")
    assert omnigent_agent.main(["relay", sid, str(tmp_path / "m.md"), "--from", "sync"]) == 0
    assert omnigent_agent.text_of(og.sessions[sid][-1]) == (
        omnigent_agent.RELAY_FROM.format(source="sync") + "\nsynced"
    )


def deadlines(registry: Path) -> list[dict]:
    log = registry.with_suffix(".events.jsonl")
    return [e for e in events_of(registry) if e["kind"] == "deadline"] if log.exists() else []


def direct_lead(tmp_path: Path) -> Path:
    registry = following(tmp_path)
    assert omnigent_agent.mark_direct(registry, "a")
    return registry


def test_a_lead_the_owner_drives_directly_gets_no_deadline_and_no_nudge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = direct_lead(tmp_path)
    og = Lead(told("brief", 4000), ending("STILL WORKING: building", 3600))
    og.snap = {"status": "running"}
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True, fyi="post", timeout=0)
    assert fyis(og) == []
    assert deadlines(registry) == []
    assert "no event in" not in capsys.readouterr().err  # only a direct lead is registered
    assert state_of(registry)["leads"]["a"]["direct"] is True


def test_a_direct_lead_s_done_tells_its_waiters_and_a_quiet_end_is_recorded() -> None:
    def route(last: str) -> tuple[str, str, str]:
        event = {"kind": "ended", "last": last, "direct": True, "decisions": True}
        return omnigent_agent.route(event, lambda: pytest.fail("no small model for it"))

    assert route("DONE: merged #5")[1] == "done"
    assert route("Here is what I found.")[:2] == ("rule", "record")
    assert route("Which one?\n\nUSER NEEDED")[:2] == ("rule", "record")
    deadline = {"kind": "deadline", "last": "", "direct": True}
    assert omnigent_agent.route(deadline, lambda: ("act", ""))[1] == "record"


def test_a_direct_lead_s_question_and_quiet_report_stay_with_the_owner() -> None:
    no_model = lambda: pytest.fail("no model call")
    question = {"kind": "blocked", "question": True, "direct": True}
    assert omnigent_agent.route(question, no_model)[1] == "record"
    prompt = {"kind": "blocked", "direct": True}  # the owner sees it in its session (#1163)
    assert omnigent_agent.route(prompt, no_model)[1] == "record"
    quiet = {"kind": "ended", "direct": True, "last": "/tmp/f.md", "head": "Here is what I found."}
    assert omnigent_agent.route(quiet, no_model)[1] == "record"
    done = {"kind": "ended", "direct": True, "last": "/tmp/f.md", "head": "DONE: shipped"}
    assert omnigent_agent.route(done, no_model)[1] == "done"


def test_a_later_instruction_to_a_lead_with_no_output_starts_no_new_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    og = Lead(told("brief", 4000))
    og.snap = {"status": "running"}

    def later(poll: int) -> None:
        offset[0] = {1: 1140.0, 2: 1201.0}[poll]
        if poll == 1:
            og.histories["a"].append(told("carry on with the revised task", 0))

    supervised(og, registry, monkeypatch, 3, later, notify="cos", console=True, fyi="post")
    # agent_progress_at is the one definition: a lead whose agent never wrote has no progress
    # for a new brief to extend, so its deadline runs from first sight
    assert len(deadlines(registry)) == 1


def test_direct_marks_and_clears_a_registry_line_keeping_its_other_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = following(tmp_path)
    before = registry.read_text()
    argv = ["direct", "a", "--registry", str(registry)]
    assert omnigent_agent.main(argv) == 0
    assert registry.read_text() == before.rstrip("\n") + "\tdirect\n"
    assert omnigent_agent.entry(registry.read_text().strip())[6] is True
    omnigent_agent.arm(registry, "a", tmp_path / "a.md", followed=True)  # a re-arm keeps it
    assert omnigent_agent.entry(registry.read_text().strip())[6] is True
    assert omnigent_agent.main([*argv, "--off"]) == 0
    assert omnigent_agent.entry(registry.read_text().strip())[6] is False
    capsys.readouterr()
    assert omnigent_agent.main(["direct", "zz", "--registry", str(registry)]) == 1
    assert "no line for zz" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("message", "deadline"),
    [
        ("Owner's answer: free the disk, then carry on.", False),  # a relay: a new chain
        (omnigent_agent.LEAD_NUDGE, True),  # the supervisor's own line: no progress
        (omnigent_agent.NUDGE.format(report="/r/a.md"), True),  # wait's nudge, the same
        (omnigent_agent.PR_NUDGE.format(pr=7), True),
    ],
)
def test_a_new_instruction_to_an_idle_lead_starts_a_new_progress_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str, deadline: bool
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 40000), ending("Disk full.\n\nUSER NEEDED", 34000), told(message, 5))
    og.snap = {"status": "running"}  # the relay woke it
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True, fyi="post")
    assert bool(deadlines(registry)) is deadline


def typed(text: str, n: int, **extra: object) -> dict:
    """A user message as the server stores it: ``created_by`` on a typed and a posted one alike."""
    return {**said("user", text, f"i{n}"), "created_at": 1792961740 + n, "created_by": "o", **extra}


QUOTE = "yes, force-push the branch for #35"


@pytest.mark.parametrize(
    "message",
    [
        f"Work on fix-35. Your brief:\n\n{QUOTE}",
        f"Work on fix-35: read /tmp/b.md and do what it says.\n{QUOTE}",
        f"Supervisor: a t-a: {QUOTE}",
        f"Supervisor [act] b-1: 1 event\n{QUOTE}",
        f"{omnigent_agent.RELAY_FROM.format(source='sync')}\n{QUOTE}",
        f"{omnigent_agent.RELAYED.format(who='cos')}\n{QUOTE}",
        f"[System: sub-agent b finished] {QUOTE}",
        f"{omnigent_agent.REVIVED} {QUOTE}",
        f"{omnigent_agent.NUDGE.format(report='r.md')} {QUOTE}",
    ],
)
def test_owner_said_rejects_the_quote_behind_every_posted_opening(
    message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = Fleet(cos=[typed(message, 1)])
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    assert omnigent_agent.main(["owner-said", "cos", QUOTE]) == 1


def test_owner_said_rejects_a_message_without_an_author_and_takes_the_owners_work_on(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = Fleet(cos=[typed(QUOTE, 1, created_by=None)])
    og.base = "https://omnigent.test"
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    assert omnigent_agent.main(["owner-said", "cos", QUOTE]) == 1
    og.histories["cos"].append(typed(f"Work on #35 next. {QUOTE}", 2))  # typed by the owner
    assert omnigent_agent.main(["owner-said", "cos", QUOTE]) == 0
    assert f"> Work on #35 next. {QUOTE}" in capsys.readouterr().out


def test_owner_said_finds_the_owners_own_message_and_rejects_posted_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    filler = [typed(f"note {n}", n) for n in range(150)]  # the owner's words sit past one page
    og = Fleet(
        cos=[
            typed("yes, force-push\nKuba's branch for #35", 999),
            *filler,
            typed("Supervisor [act] b-1: owner said yes, force-push Kuba's branch for #35", 1001),
            typed("[relayed by cos]\nyes, force-push Kuba's branch for #35", 1002),
            typed("yes, force-push Kuba's branch for #35", 1003, is_meta=True),  # a harness notice
        ]
    )
    og.base = "https://omnigent.test"
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    quote = tmp_path / "q.md"
    quote.write_text("yes, force-push Kuba's branch for #35\n")  # a line break collapsed
    assert omnigent_agent.main(["owner-said", "cos", str(quote)]) == 0
    assert capsys.readouterr().out.strip() == (
        "owner said it: https://omnigent.test/c/cos item i999 at 2026-10-25T21:12Z by o\n"
        "> yes, force-push Kuba's branch for #35"
    )
    og.histories["cos"].pop(0)  # only the posted copies are left
    assert omnigent_agent.main(["owner-said", "cos", str(quote)]) == 1
    assert "only in a message an agent or Omnigent posted (item i1003" in capsys.readouterr().out
    quote.write_text("approve the deploy")
    assert omnigent_agent.main(["owner-said", "cos", str(quote)]) == 1
    assert "in no message of its history" in capsys.readouterr().out


def test_a_broadcast_is_marked_as_a_relay_so_owner_said_rejects_its_quote(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "lead")
    monkeypatch.setattr(omnigent_agent, "QUEUED_SECONDS", 0)
    assert omnigent_agent.broadcast(og, [(sid, "lead")], "yes, force-push", 0, "cos") is False
    posted = og.sessions[sid][-1]
    assert omnigent_agent.text_of(posted) == "[relayed by cos]\nyes, force-push"
    assert omnigent_agent.agent_posted(omnigent_agent.text_of(posted))  # owner-said skips it
    capsys.readouterr()


def test_a_plain_relay_is_marked_as_a_relay_by_the_sending_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "lead")
    assert relay(og, sid, tmp_path, monkeypatch, "Owner (cos, 22:10): yes") == 0
    assert (
        omnigent_agent.text_of(og.sessions[sid][-1]) == "[relayed by cos]\nOwner (cos, 22:10): yes"
    )


def test_a_message_argument_is_a_file_or_the_text_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "lead")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMNIGENT_SESSION_ID", "cos")
    assert omnigent_agent.main(["relay", sid, "carry on with step 3"]) == 0
    assert omnigent_agent.text_of(og.sessions[sid][-1]) == "[relayed by cos]\ncarry on with step 3"
    long = "Do X.\n" + "word " * 100  # past NAME_MAX: OSError File name too long before
    assert omnigent_agent.main(["relay", sid, long]) == 0
    assert omnigent_agent.text_of(og.sessions[sid][-1]) == f"[relayed by cos]\n{long}"
    (tmp_path / "m.md").write_text("from a file")
    assert omnigent_agent.main(["relay", sid, "m.md"]) == 0
    assert omnigent_agent.text_of(og.sessions[sid][-1]) == "[relayed by cos]\nfrom a file"
    capsys.readouterr()
    assert omnigent_agent.main(["relay", sid, "missing.md"]) == 2
    assert capsys.readouterr().err.strip() == (
        "missing.md: no such file; pass a file or the text itself"
    )


def test_relay_send_status_and_report_take_a_unique_session_id_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    a, b, c = (p + "0" * 26 for p in ("abc123", "abc124", "abd999"))
    for sid in (a, b, c):
        og.sessions[sid], og.titles[sid] = [{"type": "resource_event"}], f"t-{sid[:6]}"
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMNIGENT_SESSION_ID", "cos")
    assert omnigent_agent.main(["relay", "abc123", "carry on"]) == 0
    assert omnigent_agent.text_of(og.sessions[a][-1]) == "[relayed by cos]\ncarry on"
    capsys.readouterr()
    assert omnigent_agent.main(["status", "abd"]) == 0
    assert capsys.readouterr().out.startswith(f"{c}: turn ended")
    assert omnigent_agent.main(["relay", "abc12", "x"]) == 1
    err = capsys.readouterr().err
    assert f"abc12: 2 sessions match:\n  {a} t-abc123\n  {b} t-abc124" in err
    many = [f"abe{n:02d}" + "0" * 27 for n in range(11)]
    for sid in many:
        og.sessions[sid] = []
    assert omnigent_agent.main(["status", "abe"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("abe: 11 sessions match:") and all(f"\n  {sid}" in err for sid in many)
    for cmd in (["relay", "fff0", "x"], ["send", "fff0", "x", "r.md"], ["report", "fff0", "r.md"]):
        assert omnigent_agent.main(cmd) == 1
        assert "fff0: no session matches" in capsys.readouterr().err
    assert len(og.sessions[a]) == 2 and len(og.sessions[b]) == 1  # nothing else was sent


def test_a_session_id_prefix_pages_every_session_and_a_whole_id_needs_no_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    whole = "ab" * 16
    calls = _agent_server(
        monkeypatch,
        {
            SESSIONS_EVERY: {"data": [{"id": "ab" + "0" * 30}], "has_more": True, "last_id": "x"},
            SESSIONS_EVERY + "&after=x": {"data": [{"id": whole, "archived": True}]},
        },
    )
    og = omnigent_agent.Omnigent()
    assert og.session_id(whole) == whole and og.session_id("s9") == "s9" and not calls
    assert og.session_id("abab") == whole  # found on the second page, archived
    assert [path for _, path, _ in calls] == [SESSIONS_EVERY, SESSIONS_EVERY + "&after=x"]


def test_a_directory_or_a_path_through_a_file_names_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "briefs").mkdir()
    (tmp_path / "brief folder").mkdir()
    (tmp_path / "brief.md").write_text("a file")
    for text in ("brief folder", "brief.md/do this next"):
        assert omnigent_agent.text_or_file(text) == (text, None)
    for word in ("briefs", "brief.md/missing.md"):
        with pytest.raises(omnigent_agent.Fail) as e:
            omnigent_agent.text_or_file(word)
        assert (str(e.value), e.value.code) == (
            f"{word}: no such file; pass a file or the text itself",
            2,
        )
    og = FakeOmnigent()
    sid = og.create("claude", "lead")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    capsys.readouterr()
    assert omnigent_agent.main(["relay", sid, "briefs"]) == 2
    assert "briefs: no such file" in capsys.readouterr().err
    if os.geteuid():  # root reads any file
        (tmp_path / "locked.md").write_text("x")
        (tmp_path / "locked.md").chmod(0)
        with pytest.raises(omnigent_agent.Fail) as e:
            omnigent_agent.text_or_file("locked.md")
        assert e.value.code == 1  # a file that exists stays a read error


def test_send_given_text_lists_it_as_a_followup_file_in_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent(reply="done")
    sid = og.create("claude", "lead")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    report, registry = tmp_path / "r.md", tmp_path / "leads.registry"
    seen: list[str] = []
    monkeypatch.setattr(
        omnigent_agent,
        "wait",
        lambda *a, **k: seen.append(registry.read_text()) or str(report),
    )
    argv = ["send", sid, "use main as the base", str(report), "--registry", str(registry)]
    assert omnigent_agent.main(argv) == 0
    followup = tmp_path / "r.md.followup"
    assert followup.read_text() == "use main as the base"
    assert str(followup) in seen[0]


def test_a_report_s_ending_is_read_past_its_title_and_a_report_without_one_still_acts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = omnigent_agent.report_head
    assert head("# Report\n\nDONE: merged #1\n- notes") == "DONE: merged #1"
    assert head("# R\n\nbody\n\nSTILL WORKING: waiting on\nthe worker\n\nlater") == (
        "STILL WORKING: waiting on\nthe worker\n\nlater"
    )
    assert head("# R\n\nbody") == "# R" and head("") == ""
    for report, action in (
        ("# Report\n\nDONE: merged", "done"),
        ("# Report\n\nnotes\n\nSTILL WORKING on the verifier", "record"),
        ("# Report\n\nBLOCKED: no access", "act"),
    ):
        event = {"kind": "ended", "last": "/r/a.md", "head": head(report)}
        assert omnigent_agent.route(event, lambda: ("act", ""))[1] == action, report
    registry, og, _ = path_only_lead(tmp_path, "# Title\n\nDONE: merged\nmore")
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True)
    assert state_of(registry)["leads"]["a"]["ending"] == "DONE"


def test_a_child_s_session_id_is_a_named_wait() -> None:
    sid = "ef38cdf6" + "0" * 24
    assert (
        omnigent_agent.wait_of("STILL WORKING: waiting on ef38cdf6 (s1806-r3: round 2)")
        == "ef38cdf6"
    )
    assert omnigent_agent.wait_of(f"STILL WORKING on {sid}") == sid
    assert omnigent_agent.wait_of("STILL WORKING: on deadbeefcafe-ish things") == "unknown"


def test_a_failed_small_model_call_names_what_it_printed(monkeypatch: pytest.MonkeyPatch) -> None:
    said = "API Error: Claude's response exceeded the 1000 output token maximum."
    monkeypatch.setattr(
        omnigent_agent.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, said + "\n", ""),
    )
    assert REAL_TRIAGE("an event", "") == ("unsure", f"small model: exit 1: {said}")
    monkeypatch.setattr(
        omnigent_agent.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 2, "", ""),
    )
    assert REAL_TRIAGE("an event", "") == ("unsure", "small model: exit 2")


def test_a_top_level_start_from_a_followed_lead_subscribes_that_lead_and_arms_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.setattr(omnigent_agent, "own_id", lambda: "lead")
    monkeypatch.chdir(tmp_path)
    registry, brief = tmp_path / "registry", tmp_path / "b.md"
    registry.write_text(f"lead={tmp_path / 'l.md'}\tfollow\n")
    brief.write_text("do X")
    args = ["start", "claude", "t", str(brief), str(tmp_path / "a.md"), "--top-level"]
    assert omnigent_agent.main([*args, "--registry", str(registry)]) == 0
    assert registry.read_text() == f"lead={tmp_path / 'l.md'}\tfollow\n"
    assert og.subscriptions == [("lead", "s0")]
    assert "started from lead lead" in capsys.readouterr().err
    registry.write_text(
        f"lead={tmp_path / 'l.md'}\n"
    )  # owes a report, not followed: arms as before
    args[4] = str(tmp_path / "c.md")
    assert omnigent_agent.main([*args, "--registry", str(registry)]) == 0
    assert "s1=" in registry.read_text() and og.subscriptions == [("lead", "s0")]


def test_a_helper_started_from_a_followed_leads_child_subscribes_the_lead_not_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.setattr(omnigent_agent, "own_id", lambda: "child")
    monkeypatch.setattr(omnigent_agent, "parent_id", lambda: "lead")
    monkeypatch.chdir(tmp_path)
    registry, brief = tmp_path / "registry", tmp_path / "b.md"
    registry.write_text(f"lead={tmp_path / 'l.md'}\tfollow\n")
    brief.write_text("do X")
    args = ["start", "claude", "t", str(brief), str(tmp_path / "a.md"), "--top-level"]
    assert omnigent_agent.main([*args, "--registry", str(registry)]) == 0
    assert og.subscriptions == [("lead", "s0")]
    assert registry.read_text() == f"lead={tmp_path / 'l.md'}\tfollow\n"


@pytest.mark.parametrize(
    ("text", "wait"),
    [
        ("STILL WORKING: waiting on deployment", "deployment"),
        ("STILL WORKING: the gate waits for the database", "database"),
        ("STILL WORKING: waiting on the verifier", "verifier"),  # a wait word still wins
        ("STILL WORKING: on it", "unknown"),  # a bare "on" names nothing
        ("STILL WORKING: waiting on ef38cdf6 (s1806-r3)", "ef38cdf6"),  # a child's session id
        ("STILL WORKING: verifier pass 2 on 188a4278", "verifier"),  # a commit is not the wait
        ("STILL WORKING: worker fixing R3 on 68cafc1a", "worker"),
        ("STILL WORKING: rebased on 188a4278 and running the gate", "unknown"),
        ("STILL WORKING: waiting on it", "unknown"),
    ],
)
def test_a_wait_named_after_waiting_on_or_for_is_that_wait(text: str, wait: str) -> None:
    assert omnigent_agent.wait_of(text) == wait


@pytest.mark.parametrize(
    ("report", "ending", "wait"),
    [
        # a quoted example ending is not the report's
        (
            "# R\n\nThe test uses:\n```text\nDONE: merged\n```\n\nSTILL WORKING: waiting on the verifier\n",
            "STILL WORKING",
            "verifier",
        ),
        (
            "# R\n\nThe old route printed:\n~~~\nUSER NEEDED: x\n~~~\n\nDONE: merged\n",
            "DONE",
            "unknown",
        ),
        (
            "# R\n\n> DONE: an old pass\n\n`USER NEEDED: quoted`\n\n**PARKED:** no slot\n",
            "PARKED",
            "parked",
        ),
        # a quoted ending inside the STILL WORKING tail is not the report's
        (
            (
                "# R\n\nSTILL WORKING: waiting on the verifier. The old route printed:\n\n"
                "`USER NEEDED: no standing decision`\n\nso the test now pins it.\n"
            ),
            "STILL WORKING",
            "verifier",
        ),
        (
            (
                "# R\n\nThe old supervisor post read:\n\n> STILL WORKING: checking\n"
                "> USER NEEDED: pick a base\n\nDONE: merged #12\n"
            ),
            "DONE",
            "unknown",
        ),
        # a wait named after a paragraph break still counts
        ("# R\n\nSTILL WORKING: checking status\n\nWaiting on worker\n", "STILL WORKING", "worker"),
    ],
)
def test_report_head_routes_the_reports_own_ending_and_wait(
    report: str, ending: str, wait: str
) -> None:
    head = omnigent_agent.report_head(report)
    assert (omnigent_agent.ending_of(head) or "STILL WORKING") == ending
    assert omnigent_agent.wait_of(head) == wait


def test_report_head_keeps_little_of_a_long_report() -> None:
    report = "# R\n\nSTILL WORKING: waiting on the verifier\n" + "- proof detail\n" * 5000
    head = omnigent_agent.report_head(report)
    assert len(head) <= omnigent_agent.REPORT_WAIT_CHARS and head.endswith("- proof detail")
    assert omnigent_agent.wait_of(head) == "verifier"


def test_the_cloud_preamble_forbids_a_question_tool_and_a_page() -> None:
    """Each waits on a prompt nobody in the cloud sees."""
    preamble = omnigent_agent.PR_PREAMBLE
    assert "never call an interactive question tool, since its prompt waits on someone" in preamble
    assert "Never publish a page or artifact, since it waits on a permission nobody" in preamble
    assert "session link" not in preamble


def test_the_cloud_preamble_reads_issues_and_prs_through_rest() -> None:
    """GraphQL answers 403 in a cloud VM, so `gh issue view` and `gh pr view` fail there (#1011)."""
    preamble = omnigent_agent.PR_PREAMBLE
    assert "Read issues and PRs through REST too, since GraphQL answers 403 here" in preamble
    assert "`gh api repos/{{owner}}/{{repo}}/issues/<n>` and `.../pulls/<n>`" in preamble
    assert "never `gh issue view` or `gh pr view`" in preamble


class CloudFake(FakeOmnigent):
    """Sessions of ``claude-cloud``, whose record names the agent (``CLOUD_RECORD``), not its
    harness."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.harnesses = {"claude-cloud": omnigent_agent.CLOUD_HARNESS}
        self.puts = []

    def info(self, sid: str) -> dict:
        info = {**super().info(sid), **CLOUD_RECORD}
        return {**info, "host_id": self.hosts_of.get(sid, self.host_id)}

    def filesystem(self, sid: str, path: str, method: str = "GET", body: object = None) -> dict:
        self.puts.append((sid, path, method, body))  # a write on the session's host
        return {}

    def modified(self, sid: str, path: str) -> float:
        raise omnigent_agent.Gone(f"{path}: not found")  # no question, no report on that host

    puts: list[tuple[str, str, str, object]]


CLOUD_REPORT = (
    "DONE: merged #5. Report: r.md\n\n# Report\n\n> STILL WORKING: an old line, quoted\nGates: 0"
)


@pytest.mark.parametrize(("top_level", "pr"), [(True, None), (False, None), (True, 7), (False, 7)])
def test_every_cloud_preamble_keeps_out_of_git_and_one_without_pr_reports_in_its_last_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, top_level: bool, pr: int | None
) -> None:
    """A fresh clone's untracked report tempts a write to .git/info/exclude, which prompts (#706)."""
    monkeypatch.delenv("LOOP_DENY_LIST", raising=False)
    og = CloudFake()
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(
        og, "claude-cloud", "t", "do X", report, None, 0, top_level=top_level, pr=pr
    )
    brief = omnigent_agent.text_of(og.sessions[sid][1])
    assert "never write under `.git/`" in brief and "`tmp/loop/` is scratch" in brief
    assert (
        "or a `claude/ready-*` branch the grant above lets you create" in brief
        and "whatever other branch the brief names" in brief
        and "Never ask the owner which branch" in brief
    )
    assert "claude/ready-demo-<pr>" in brief and "the demo media branch it names" in brief
    assert "never committed.\n" in brief and brief.endswith(
        omnigent_agent.CLOUD_NO_PUSH
        + omnigent_agent.CLOUD_GH_QUOTA
        + omnigent_agent.pack_line("do X")
        + "\ndo X"
    )
    assert (
        "curl -sS -X POST -H 'Content-Type: application/json'" in brief
        and "api.github.com/repos/<owner>/<repo>/pulls" in brief
    )
    if pr:
        assert "comment on PR #7" in brief and "your last message" not in brief
        return
    assert "your final report is" in brief and "last message" in brief
    assert f"writes to {report}" in brief or f"writes it to {report}" in brief
    assert "reply with only the report path" not in brief and ".question" not in brief
    assert "Write your final report to" not in brief
    if top_level:
        assert "opening with its ending line" in brief and f"Report: {report}`" in brief


@pytest.mark.parametrize(
    ("cloud", "top_level", "pr"),
    [(True, True, None), (True, False, 7), (False, True, None), (False, False, None)],
)
def test_only_a_cloud_start_s_first_prompt_grants_claude_ready_branches_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cloud: bool, top_level: bool, pr: int | None
) -> None:
    """A cloud session takes a branch grant only in its first message, so every start carries it (#1485)."""
    monkeypatch.delenv("LOOP_DENY_LIST", raising=False)
    og = CloudFake() if cloud else FakeOmnigent()
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(
        og,
        "claude-cloud" if cloud else "claude",
        "t",
        "do X",
        report,
        None,
        0,
        top_level=top_level,
        pr=pr,
    )
    text = (
        omnigent_agent.text_of(og.sessions[sid][1])
        if cloud
        else omnigent_agent.brief_of(report).read_text()
    )
    assert text.count(omnigent_agent.CLOUD_BRANCHES) == (1 if cloud else 0)


@pytest.mark.parametrize("top_level", [True, False])
@pytest.mark.parametrize("pr", [None, 7])
def test_every_cloud_preamble_never_pushes_a_report_or_asks_where(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, top_level: bool, pr: int | None
) -> None:
    """A report, log or verifier output travels in a comment, never a push; a push question never reaches the owner (#1534)."""
    monkeypatch.delenv("LOOP_DENY_LIST", raising=False)
    og = CloudFake()
    sid, _ = omnigent_agent.start(
        og, "claude-cloud", "t", "do X", tmp_path / "r.md", None, 0, top_level=top_level, pr=pr
    )
    brief = omnigent_agent.text_of(og.sessions[sid][1])
    assert "Reports, logs and verifier output are never committed or pushed, to any branch" in brief
    assert "A push target or branch question never goes to the owner" in brief
    assert "put the file in your comment and end the turn" in brief


def test_a_host_session_s_preamble_is_unchanged(tmp_path: Path) -> None:
    for top_level in (False, True):
        og = FakeOmnigent()
        report = tmp_path / f"{top_level}.md"
        omnigent_agent.start(og, "claude", "t", "do X", report, None, 0, top_level=top_level)
        text = omnigent_agent.brief_of(report).read_text()
        assert f"Write your final report to {report}" in text and ".git/" not in text


@pytest.mark.parametrize(
    ("cloud", "top_level", "pr"),
    [
        (False, False, None),  # a worker's or verifier's
        (False, True, None),  # a lead's
        (True, False, None),  # cloud, no --pr
        (True, True, None),  # a cloud lead
        (True, False, 7),  # cloud, --pr
        (True, True, 7),  # a top-level cloud worker, --pr
    ],
)
def test_every_brief_points_to_the_role_file_s_long_run_rule_and_carries_no_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cloud: bool, top_level: bool, pr: int | None
) -> None:
    """The long-run rule lives once, in a role file; a brief keeps one line naming it (#902)."""
    monkeypatch.delenv("LOOP_DENY_LIST", raising=False)
    og = CloudFake() if cloud else FakeOmnigent()
    report = tmp_path / "r.md"
    role = tmp_path / "worker.md"  # a cloud brief reads the file, so it must exist
    role.write_text("# Worker\n")
    agent = "claude-cloud" if cloud else "claude"
    sid, _ = omnigent_agent.start(
        og, agent, "t", "do X", report, str(role), 0, top_level=top_level, pr=pr
    )
    brief_file = omnigent_agent.brief_of(report)
    brief = (
        brief_file.read_text()
        if brief_file.is_file()
        else omnigent_agent.text_of(og.sessions[sid][1])
    )
    # a cloud brief swaps it for the in-turn rule (#1002)
    assert brief.count(LONG_RUN_POINTER) == (0 if cloud else 1)
    assert ("A run may not outlive the turn" in brief) == cloud
    assert LONG_RUN_RULE_START not in brief and "setsid" not in brief
    assert "10 minutes" not in brief and "never short polls" not in brief
    if top_level and not pr:
        assert "Only the owner's own words approve" in brief
    # the rule the pointer names is there, with both variant conditions
    rule = long_run_rule()
    assert "since macOS has no `setsid` command" in rule
    assert "Where a foreground command stops at 10 minutes" in rule
    assert "the run is waited on within the same turn" in rule
    assert "stops at 10 minutes" in rule
    assert (
        "Never end a turn saying you wait on a detached run alone, since its end never wakes you"
        in rule
    )


def test_wait_writes_a_cloud_session_s_last_message_to_the_report_path(tmp_path: Path) -> None:
    """Its report file stays in the VM, which no file API reaches: the message is the report."""
    og = CloudFake(reply=CLOUD_REPORT)
    report = tmp_path / "loop" / "r.md"
    sid, _ = omnigent_agent.start(og, "claude-cloud", "t", "do X", report, None, 0, top_level=True)
    assert omnigent_agent.wait(og, sid, report) == str(report)
    assert report.read_text() == CLOUD_REPORT + "\n"
    assert og.posts == 1  # no nudge


def test_a_cloud_report_file_already_on_the_host_never_counts(tmp_path: Path) -> None:
    og = CloudFake(reply="still going")
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(og, "claude-cloud", "t", "do X", report, None, 0, top_level=True)
    report.write_text("DONE: an old report\n")
    og.sessions[sid].append(said("assistant", CLOUD_REPORT, "late"))
    assert omnigent_agent.wait(og, sid, report) == str(report)
    assert report.read_text() == CLOUD_REPORT + "\n"


def test_a_cloud_turn_ending_on_only_a_path_is_nudged_to_reply_with_the_report(
    tmp_path: Path,
) -> None:
    og = CloudFake(reply="tmp/loop/r.md")
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(og, "claude-cloud", "t", "do X", report, None, 0)
    with pytest.raises(omnigent_agent.Fail, match="ended \\(idle\\) without"):
        omnigent_agent.wait(og, sid, report)
    nudge = omnigent_agent.text_of(og.sessions[sid][-2])
    assert nudge == omnigent_agent.CLOUD_NUDGE and "full report" in nudge
    assert not report.exists()


def test_a_failed_cloud_turn_is_no_report_whatever_it_said(tmp_path: Path) -> None:
    og = CloudFake(reply="REPORT\nStep 1 of 3 done.", status="failed")
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(og, "claude-cloud", "t", "do X", report, None, 0)
    with pytest.raises(omnigent_agent.Fail, match="ended \\(failed\\)"):
        omnigent_agent.wait(og, sid, report)
    assert not report.exists()


def test_a_cloud_lead_on_another_host_has_its_report_written_on_that_host(tmp_path: Path) -> None:
    og = CloudFake(reply=CLOUD_REPORT)
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(
        og, "claude-cloud", "t", "do X", report, None, 0, top_level=True, host_id="far"
    )
    assert omnigent_agent.wait(og, sid, report) == str(report)
    assert og.puts == [(sid, str(report), "PUT", {"content": CLOUD_REPORT + "\n"})]
    assert not report.exists()


def test_a_cloud_session_s_report_ignores_its_seed_workspace_s_tracked_changes(
    tmp_path: Path,
) -> None:
    """The VM works on its own clone: an edit in the host's seed checkout is none of its doing."""
    ws = tmp_path / "ws"
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(ws)]
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    (ws / "f.txt").write_text("a\n")
    subprocess.run([*git, "add", "f.txt"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "i"], check=True)
    (ws / "f.txt").write_text("b\n")
    og = CloudFake(reply=CLOUD_REPORT)
    report = ws / "tmp" / "r.md"
    sid, _ = omnigent_agent.start(
        og, "claude-cloud", "t", "do X", report, None, 0, top_level=True, workspace=str(ws)
    )
    assert omnigent_agent.wait(og, sid, report, baseline=[]) == str(report)


def test_report_writes_a_relative_path_in_the_cloud_session_s_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ws, elsewhere = tmp_path / "ws", tmp_path / "cwd"
    ws.mkdir(), elsewhere.mkdir()
    og = CloudFake(reply=CLOUD_REPORT)
    sid, _ = omnigent_agent.start(
        og, "claude-cloud", "t", "do X", ws / "x.md", None, 0, workspace=str(ws)
    )
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(elsewhere)
    assert omnigent_agent.main(["report", sid, "tmp/loop/r.md"]) == 0
    assert (ws / "tmp/loop/r.md").read_text() == CLOUD_REPORT + "\n"
    assert not (elsewhere / "tmp").exists()


def test_report_writes_a_far_cloud_session_s_report_on_its_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = CloudFake(reply=CLOUD_REPORT)
    sid, _ = omnigent_agent.start(
        og, "claude-cloud", "t", "do X", tmp_path / "x.md", None, 0, top_level=True, host_id="far"
    )
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    assert omnigent_agent.main(["report", sid, "/far/r.md"]) == 0
    assert og.puts == [(sid, "/far/r.md", "PUT", {"content": CLOUD_REPORT + "\n"})]


def test_report_reads_the_file_while_a_cloud_session_s_last_message_is_a_new_instruction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = CloudFake(reply=CLOUD_REPORT)
    sid, _ = omnigent_agent.start(og, "claude-cloud", "t", "do X", tmp_path / "x.md", None, 0)
    og.sessions[sid].append(said("user", "a follow-up, not answered yet", "u"))
    og.read_file = lambda s, p: "the file"  # type: ignore[attr-defined]
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    path = tmp_path / "r.md"
    assert omnigent_agent.main(["report", sid, str(path)]) == 0
    assert capsys.readouterr().out == "the file\n" and not path.exists()


def test_report_writes_a_cloud_session_s_last_message_to_the_path_and_prints_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = CloudFake(reply=CLOUD_REPORT)
    sid, _ = omnigent_agent.start(og, "claude-cloud", "t", "do X", tmp_path / "x.md", None, 0)
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    path = tmp_path / "out" / "r.md"
    assert omnigent_agent.main(["report", sid, str(path)]) == 0
    assert path.read_text() == CLOUD_REPORT + "\n"
    assert capsys.readouterr().out == CLOUD_REPORT + "\n"


def test_a_cloud_session_is_known_by_its_agent_s_harness_not_its_record_s() -> None:
    """The record's ``harness`` is the transport, ``acp``: only the agent has the cloud one (#706)."""
    og = CloudFake()
    assert omnigent_agent.cloud_session(og, CLOUD_RECORD)
    assert omnigent_agent.cloud_session(og, {"harness": "acp", "agent_id": "claude-cloud"})
    assert not omnigent_agent.cloud_session(og, {"harness": omnigent_agent.CLOUD_HARNESS})
    assert not omnigent_agent.cloud_session(og, {"harness": "acp", "agent_name": "claude"})
    fleet = Fleet()  # an agent that can't be looked up is no cloud one
    assert not omnigent_agent.cloud_session(fleet, {"agent_name": "gone"})


def test_an_agent_s_harness_is_looked_up_once() -> None:
    """``wait`` and the supervisor ask it at every look: one agent listing per name, not per poll."""
    og = object.__new__(omnigent_agent.Omnigent)
    looked: list[str] = []
    og.agent = lambda name: looked.append(name) or {"harness": "acp:claude-cloud"}  # type: ignore[method-assign]
    assert og.harness("claude-cloud") == og.harness("claude-cloud") == "acp:claude-cloud"
    assert looked == ["claude-cloud"]


@pytest.mark.parametrize(
    "last",
    [
        "Committing the fix...",
        "Which option should I choose?",
        "Gates: 0\nDONE: merged. Report: r.md",  # a lead's ending closes a turn, never a report
        "REPORT",  # the marker and nothing after it
        "tmp/loop/r.md",
    ],
)
def test_a_cloud_turn_ending_on_an_unmarked_message_is_nudged_not_taken(
    tmp_path: Path, last: str
) -> None:
    """A turn ending is no proof the task is done: only a marked message is the report (#706)."""
    og = CloudFake(reply=last)
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(og, "claude-cloud", "t", "do X", report, None, 0)
    with pytest.raises(omnigent_agent.Fail, match="ended \\(idle\\) without"):
        omnigent_agent.wait(og, sid, report)
    assert omnigent_agent.text_of(og.sessions[sid][-2]) == omnigent_agent.CLOUD_NUDGE
    assert not report.exists() and not omnigent_agent.question_of(report).exists()


def test_a_marked_message_is_a_cloud_reply_only_as_the_session_s_own_last() -> None:
    """A brief or a relayed message that quotes the marker is no report."""
    mine = said("assistant", "REPORT\nGates: 0", "1")
    assert omnigent_agent.cloud_reply([mine]) == ("report", "Gates: 0\n")
    assert omnigent_agent.cloud_reply([mine, said("user", "REPORT\nGates: 0", "2")]) is None


def test_a_cloud_child_s_report_follows_its_report_line(tmp_path: Path) -> None:
    og = CloudFake(reply="**REPORT**\n\n# Report\n\nGates: 0")
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(og, "claude-cloud", "t", "do X", report, None, 0)
    assert omnigent_agent.wait(og, sid, report) == str(report)
    assert report.read_text() == "# Report\n\nGates: 0\n"


def test_a_cloud_child_s_question_is_asked_beside_its_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """As a host child's ``<report>.question``: exit 3, the question written beside the report."""
    og = CloudFake(reply="QUESTION\nWhich option should I choose: A or B?")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(og, "claude-cloud", "t", "do X", report, None, 0)
    assert omnigent_agent.main(["wait", sid, str(report)]) == omnigent_agent.ASKED
    question = omnigent_agent.question_of(report)
    assert question.read_text() == "Which option should I choose: A or B?\n"
    out = capsys.readouterr()
    assert str(question) in out.out + out.err
    assert not report.exists() and og.posts == 1  # no nudge


def test_a_far_cloud_child_s_question_is_written_on_its_host(tmp_path: Path) -> None:
    og = CloudFake(reply="QUESTION\nA or B?")
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(
        og, "claude-cloud", "t", "do X", report, None, 0, top_level=True, host_id="far"
    )
    with pytest.raises(omnigent_agent.Asked):
        omnigent_agent.wait(og, sid, report)
    assert og.puts == [(sid, f"{report}.question", "PUT", {"content": "A or B?\n"})]


def test_report_takes_no_unmarked_cloud_message_as_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = CloudFake(reply="REPORT\nGates: 0")
    sid, _ = omnigent_agent.start(og, "claude-cloud", "t", "do X", tmp_path / "x.md", None, 0)
    og.read_file = lambda s, p: "the file"  # type: ignore[attr-defined]
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    path = tmp_path / "r.md"
    assert omnigent_agent.main(["report", sid, str(path)]) == 0
    assert capsys.readouterr().out == "Gates: 0\n" and path.read_text() == "Gates: 0\n"
    for later in ("Committing the fix...", "QUESTION\nA or B?"):  # a progress line, a question
        path.unlink(missing_ok=True)
        og.sessions[sid].append(said("assistant", later, later))
        assert omnigent_agent.main(["report", sid, str(path)]) == 0
        assert capsys.readouterr().out == "the file\n" and not path.exists()


def test_a_cloud_child_s_preamble_names_its_report_and_question_lines(tmp_path: Path) -> None:
    og = CloudFake()
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(og, "claude-cloud", "t", "do X", report, None, 0)
    brief = omnigent_agent.text_of(og.sessions[sid][1])
    assert "a first line `REPORT`, then the full report" in brief
    assert "a first line `QUESTION`, then the question" in brief
    assert "read as unfinished and nudged" in brief


class CloudOwing(Owing):
    """``Owing``'s session, a ``claude-cloud`` one on this host."""

    host_id = "h"

    def info(self, sid: str) -> dict:
        return {**super().info(sid), **CLOUD_RECORD, "host_id": "h"}

    def harness(self, agent: str) -> str:
        return omnigent_agent.CLOUD_HARNESS if agent == "claude-cloud" else ""


def test_the_supervisor_takes_a_cloud_child_s_report_from_its_marked_last_message(
    tmp_path: Path,
) -> None:
    """Its file stays in the VM: the supervisor reads no file and writes the message (#706)."""
    report = tmp_path / "r.md"
    history = [told("brief", 120), ending("REPORT\nGates: 0", 60)]
    og = CloudOwing(history, {"status": "running"}, {"status": "idle"})
    event, _ = omnigent_agent.check(og, "a", report, 3600, {}, set(), print)
    assert event == "" and not report.exists()  # still in its turn: it may say more
    event, _ = omnigent_agent.check(og, "a", report, 3600, {}, set(), print)
    assert event == f"a coord: report written: {report}"
    assert report.read_text() == "Gates: 0\n"


def test_the_supervisor_takes_a_cloud_verifier_s_report_only_with_its_verdict(
    tmp_path: Path,
) -> None:
    """A verifier's marked reply without its ``SATISFIED:`` line is no report, as a host file
    without one isn't (#706)."""
    report = tmp_path / "r.md"
    og = CloudOwing(
        [told("brief", 120), ending("REPORT\nStill running checks.", 60)], {"status": "idle"}
    )
    event, _ = omnigent_agent.check(og, "a", report, 3600, {}, set(), print, verdict=True)
    assert "report written" not in event and not report.exists()
    og = CloudOwing(
        [told("brief", 120), ending("REPORT\nVERDICT: 0\nSATISFIED: yes", 60)], {"status": "idle"}
    )
    event, _ = omnigent_agent.check(og, "a", report, 3600, {}, set(), print, verdict=True)
    assert event == f"a coord: report written: {report}"


@pytest.mark.parametrize("reply", ["REPORT\nStill running checks.", "DONE: checks still running"])
def test_wait_takes_a_cloud_verifier_s_reply_only_with_its_verdict(
    tmp_path: Path, reply: str
) -> None:
    og = CloudFake(reply=reply)
    report = tmp_path / "r.md"
    sid, _ = omnigent_agent.start(og, "claude-cloud", "t", "do X", report, None, 0)
    with pytest.raises(omnigent_agent.Fail, match="ended \\(idle\\) without"):
        omnigent_agent.wait(og, sid, report, verdict=True)
    assert not report.exists()


def test_the_supervisor_ignores_a_stale_host_file_of_a_cloud_child(tmp_path: Path) -> None:
    report = tmp_path / "r.md"
    report.write_text("an old report, newer than the brief")
    og = CloudOwing([told("brief", 120), ending("Committing the fix...", 60)], {"status": "idle"})
    event, _ = omnigent_agent.check(og, "a", report, 3600, {}, set(), print)
    assert event == "" and og.sent == []


def test_the_supervisor_routes_a_cloud_child_s_marked_question(tmp_path: Path) -> None:
    report = tmp_path / "r.md"
    og = CloudOwing([told("brief", 120), ending("QUESTION\nA or B?", 60)], {"status": "idle"})
    event, _ = omnigent_agent.check(og, "a", report, 3600, {}, set(), print)
    question = omnigent_agent.question_of(report)
    assert event == f"a coord: asked: {question}" and question.read_text() == "A or B?\n"


def test_the_supervisor_never_sends_a_cloud_child_the_file_path_lines(tmp_path: Path) -> None:
    """Its resume and revive lines name no report file: it can't write one the host reads."""
    report = tmp_path / "r.md"
    og = CloudOwing(
        [told("brief", 4000), ending("Committing the fix...", 3900)], {"status": "idle"}
    )
    event, _ = omnigent_agent.check(og, "a", report, 3600, {}, set(), print)
    assert event == "" and og.sent == [omnigent_agent.CLOUD_NUDGE]
    og = CloudOwing([told("brief", 60)], {"runner_online": False}, {"runner_online": True}, {})
    unsent: set[str] = set()
    omnigent_agent.check(og, "a", report, 3600, {}, unsent, print)
    omnigent_agent.check(og, "a", report, 3600, {}, unsent, print)
    assert og.retried == ["a"] and og.sent and all(str(report) not in m for m in og.sent)


def test_the_cloud_preamble_asks_for_a_demo_line_and_the_gate_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A visible change reports its demo, and an unpushed gate pass ends the report with its record."""
    demo = "Demo: for a user-visible change, state in the report the command you ran and its output"
    record = "end the report with the `local-ci --record` JSON (head, tree, exit, command, env) as its last lines"
    for held in (False, True):
        if held:
            monkeypatch.setenv("LOOP_DENY_LIST", str(tmp_path / "deny.txt"))
        else:
            monkeypatch.delenv("LOOP_DENY_LIST", raising=False)
        brief = omnigent_agent.pr_preamble()
        assert demo in brief and "`Demo: none`" in brief
        assert record in brief and "`loop-gate/` ref" in brief
        assert ("which you don't post" in brief) is held


def test_a_cloud_worker_s_delivered_brief_freezes_its_branch_after_hand_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cloud subscribes a worker to its PR's events; a review comment must not wake it to push."""
    for held in (False, True):
        if held:
            monkeypatch.setenv("LOOP_DENY_LIST", str(tmp_path / "deny.txt"))
        else:
            monkeypatch.delenv("LOOP_DENY_LIST", raising=False)
        og = FakeOmnigent()
        fake_gh(monkeypatch, {})
        sid, _ = omnigent_agent.start(
            og, "claude-cloud", "t", "do X", tmp_path / "r.md", None, 0, pr=7
        )
        brief = omnigent_agent.text_of(og.sessions[sid][1])
        assert "your branch is frozen" in brief and "read-only, so push nothing" in brief
        assert "a follow-up goes in a new PR" in brief


def test_the_lead_preamble_points_heavy_runs_to_the_placement_rule() -> None:
    """A lead brief naming a host worktree for a test run is read against the one rule (#1119)."""
    rule = (
        "Every heavy run (a test suite, a build, an e2e, a gate, a mutation or proof run) goes"
        " where engineering-loop's SKILL.md, Where heavy steps run says, by default a Claude cloud"
        " session: a host worktree this brief names for a test run says where the code is, not"
        " where the run goes.\n"
    )
    lead = omnigent_agent.LEAD_PREAMBLE.format(report="r.md", role="")
    assert lead.count(rule) == 1
    assert rule in omnigent_agent.cloud_preamble(omnigent_agent.LEAD_PREAMBLE, None)
    assert "Where heavy steps run" not in omnigent_agent.PREAMBLE  # a child's brief says its run
    skill = (VERIFIER_MD.parent / "SKILL.md").read_text()
    assert "\n## Where heavy steps run\n" in skill  # the section the line names


def test_the_lead_preamble_asks_for_the_ending_line_with_the_report_path() -> None:
    """One ending contract: the ending word and the report path on one line, which routes as is."""
    text = omnigent_agent.LEAD_PREAMBLE.format(report="r.md", role="")
    assert "only its path" not in text
    for word in ("DONE", "STILL WORKING", "USER NEEDED", "PARKED"):
        assert f"`{word}: " in text and ". Report: r.md`" in text
    assert "ends `USER NEEDED`" in text and f"starts with `{omnigent_agent.PARK}`" in text
    for line in (omnigent_agent.LEAD_NUDGE, omnigent_agent.LEAD_WAKE, omnigent_agent.LEAD_REVIVE):
        assert "PARKED" not in line
    line = "DONE: merged #5. Report: r.md"
    assert omnigent_agent.ending_of(line) == "DONE"
    assert omnigent_agent.REPORT_PATH.fullmatch(line) is None  # not the bare-path fallback
    event = {"kind": "ended", "last": line, "direct": True, "decisions": True}
    routed = omnigent_agent.route(event, lambda: pytest.fail("no small model for it"))
    assert routed[1] == "done"  # no nudge, no model call
    wait = "STILL WORKING: waiting on verifier. Report: r.md"
    assert omnigent_agent.wait_of(wait) == "verifier"


@pytest.mark.needs_tool("perl")
def test_the_role_files_detach_runs_here_and_records_its_exit_code(tmp_path: Path) -> None:
    """The launcher the long-run rule names starts the command in its own session and returns at once."""
    rule = long_run_rule()
    assert f"`{omnigent_agent.DETACH} sh -c" in rule
    flat = " ".join(rule.split())  # the relocated clauses, each the only copy now
    # one wake per run: one tracked wait as long as the run, never a poll loop (waste audit, finding 1)
    assert (
        "--budget <the run's expected length>` started as a background call your harness tracks"
        in flat
    )
    assert "Never a short `wait-for`, an `until grep` or a `sleep` loop" in flat
    assert (
        "up to a total budget you state" not in flat
        and "a sleep loop that checks the log" not in flat
    )
    assert "starts detached (in its own session)" in flat
    assert (
        "Where the harness tracks no background call, each foreground `wait-for` blocks close to the tool's limit"
        in flat
    )
    assert "a gate your role file says to run bare" in flat
    pid, log = tmp_path / "pid", tmp_path / "log"
    script = f'echo $$ > {pid}; sleep 1; (exit 3); echo "exit $?" > {log}'
    began = time.monotonic()
    subprocess.run(
        ["sh", "-c", f"{omnigent_agent.DETACH} sh -c '{script}'"], check=True, timeout=10
    )
    assert time.monotonic() - began < 1  # the caller's shell is gone before the run ends
    deadline = time.monotonic() + 10
    while not (log.is_file() and log.read_text()) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert log.read_text() == "exit 3\n"
    assert int(pid.read_text()) != os.getpid()


@pytest.mark.parametrize(
    "path",
    [
        "/v1/scheduled-tasks",
        "/v1/scheduled-tasks/t1",
        "/v1/policies/p1",
        "/v1/sessions/s1/policies",
    ],
)
@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
def test_a_write_to_an_approval_gated_resource_waits_for_the_owner(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    """A lead whose held tool call it read as a hang made the same change over REST (#696)."""
    sent: list[str] = []

    def urlopen(req: Request, timeout: float) -> io.BytesIO:
        sent.append(req.get_method())
        return io.BytesIO(b"{}")

    monkeypatch.setattr(omnigent_agent, "urlopen", urlopen)
    og = object.__new__(omnigent_agent.Omnigent)
    og.base, og._token = "http://omnigent.test", "synthetic"
    with pytest.raises(omnigent_agent.Fail, match="the owner's to make"):
        og.call(method, path, {})
    assert sent == []
    og.call("GET", path)
    og.call(method, path, {}, owner=True)
    og.call(method, "/v1/sessions/s1", {})  # a session's own write stays open
    assert sent == ["GET", method, method]


def test_wait_reads_a_remote_sessions_report_on_its_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    files = {"tmp/loop/r.md": "done"}

    class Far(FakeOmnigent):
        def info(self, sid: str) -> dict:
            return {**super().info(sid), "host_id": "mac"}

        def read_file(self, sid: str, path: str) -> str:
            return files[path]

        def modified(self, sid: str, path: str) -> float:
            if path not in files:
                raise omnigent_agent.Gone(path)
            return time.time() - 3600

    og = Far()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)  # nothing at tmp/loop/r.md here
    sid = og.create("claude", "t")
    assert omnigent_agent.main(["wait", sid, "tmp/loop/r.md", "--keep"]) == 0
    assert capsys.readouterr().out.strip() == "tmp/loop/r.md"  # not resolved on this host


def test_wait_reads_a_remote_sessions_question_and_report_text_on_its_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is at these paths on this host: a question, a report without its verdict and a launcher's failure
    line are each seen only through the session's file API."""
    files: dict[str, str] = {}

    class Far(FakeOmnigent):
        def info(self, sid: str) -> dict:
            return {**super().info(sid), "host_id": "mac"}

        def read_file(self, sid: str, path: str) -> str:
            return files[path]

        def modified(self, sid: str, path: str) -> float:
            if path not in files:
                raise omnigent_agent.Gone(path)
            return time.time() - 3600

    og = Far()
    monkeypatch.chdir(tmp_path)
    sid, report = og.create("codex", "t"), Path("tmp/loop/r.md")
    files["tmp/loop/r.md.question"] = "which base?"
    with pytest.raises(omnigent_agent.Asked, match=r"r\.md\.question"):
        omnigent_agent.wait(og, sid, report, timeout=0)
    del files["tmp/loop/r.md.question"]
    files["tmp/loop/r.md"] = "## Standards\n"
    with pytest.raises(omnigent_agent.Fail, match="no verdict after 0s"):
        omnigent_agent.wait(og, sid, report, timeout=0, verdict=True)
    files["tmp/loop/r.md"] += "SATISFIED: yes\n"
    assert omnigent_agent.wait(og, sid, report, timeout=0, verdict=True) == str(report)
    files["tmp/loop/r.md"] = "FAILED: codex: no such model\n"
    with pytest.raises(omnigent_agent.Fail, match="no report after 0s"):
        omnigent_agent.wait(og, sid, report, timeout=0)


class FarFiles:
    """A session on another host (``mac``), whose files are only in ``files``, each written at
    ``times`` (else a minute ago); nothing is at those paths on this host."""

    host_id = "h"  # this host

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[call-arg]
        self.files: dict[str, str] = {}
        self.times: dict[str, float] = {}

    def info(self, sid: str) -> dict:
        return {**super().info(sid), "host_id": "mac"}  # type: ignore[misc]

    def read_file(self, sid: str, path: str) -> str:
        if path not in self.files:
            raise omnigent_agent.Gone(path)
        return self.files[path]

    def modified(self, sid: str, path: str) -> float:
        if path not in self.files:
            raise omnigent_agent.Gone(path)
        return self.times.get(path, time.time() - 60)

    def filesystem(self, sid: str, path: str, method: str = "GET", body: object = None) -> dict:
        if method == "PUT":
            self.files[path], self.times[path] = body["content"], time.time()  # type: ignore[index]
        elif method != "DELETE" or self.files.pop(path, None) is None:
            raise omnigent_agent.Gone(path)
        return {}


def test_check_reads_a_remote_sessions_report_and_question_on_its_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supervisor sees a remote session's question, a launcher's failure line and its report
    only through the session's file API: it nudged or gave up on a session that had finished."""

    class Far(FarFiles, Owing):
        pass

    monkeypatch.chdir(tmp_path)
    og, report = Far([told("brief", 120)], {}), Path("tmp/loop/r.md")

    def look() -> str:
        return omnigent_agent.check(og, "a", report, 3600, {}, set(), print)[0]

    og.files["tmp/loop/r.md.question"] = "which base?"
    assert look() == "a coord: asked: tmp/loop/r.md.question"
    del og.files["tmp/loop/r.md.question"]
    og.files["tmp/loop/r.md"] = "FAILED: codex: no such model\n"
    assert look() == ""
    og.files["tmp/loop/r.md"] = "done"
    assert look() == "a coord: report written: tmp/loop/r.md"
    og.times["tmp/loop/r.md"] = time.time() - 600  # older than the brief: the turn before's
    assert look() == ""


def test_check_counts_a_report_that_is_not_utf8(tmp_path: Path) -> None:
    report = tmp_path / "r.md"
    report.write_bytes(b"\xff report")
    event, _ = omnigent_agent.check(
        Owing([told("brief", 120)], {}), "a", report, 3600, {}, set(), print
    )
    assert event == f"a coord: report written: {report}"


def test_send_dedupes_against_a_remote_sessions_question_on_its_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy that landed after the question on the session's host is the first one: a retry
    sends nothing. A file at that path on this host is not that question, so it stays."""

    class Far(FarFiles, FakeOmnigent):
        pass

    og, report = Far(), Path("tmp/loop/r.md")
    monkeypatch.chdir(tmp_path)
    sid = og.create("claude", "t")
    og.files["tmp/loop/r.md.question"] = "which base?"
    og.send(sid, "use main")  # the first copy, which landed
    here = omnigent_agent.question_of(report)
    here.parent.mkdir(parents=True)
    here.write_text("another session's")
    assert omnigent_agent.send(og, sid, "use main", report, 0, far=True)[0] == "already"
    assert og.posts == 1
    assert "tmp/loop/r.md.question" not in og.files  # answered: deleted on its host
    assert omnigent_agent.send(og, sid, "use dev", report, 0, far=True)[0] == "delivered"
    assert here.read_text() == "another session's"


def test_send_fails_when_a_remote_question_survives_its_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 404 on the delete while a read still finds the question (a host without the route) is a
    failure: passed as gone, send reported delivered and the next wait read the answered question.
    The folder's listing is no witness: it pages, so a question past its first page is missing
    from it. The CLI still arms the follow-up, as the answer landed, and then fails."""

    class Far(FarFiles, FakeOmnigent):
        def filesystem(self, sid: str, path: str, method: str = "GET", body: object = None) -> dict:
            if method == "DELETE":
                raise omnigent_agent.Gone("404")
            return super().filesystem(sid, path, method, body)

        def modified(self, sid: str, path: str) -> float:
            raise omnigent_agent.Gone(path)  # past the listing's first page

    og, report = Far(), Path("tmp/loop/r.md")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    sid = og.create("claude", "t", workspace="/elsewhere/repo")
    og.files["tmp/loop/r.md.question"] = "which base?"
    with pytest.raises(omnigent_agent.Undeleted, match="not deleted"):
        omnigent_agent.send(og, sid, "use main", report, 0, far=True)
    registry = tmp_path / "registry"
    argv = ["send", sid, "use dev", "tmp/loop/r.md", "--confirm-seconds", "0", "--timeout", "5"]
    assert omnigent_agent.main([*argv, "--registry", str(registry)]) == 1
    assert "was not deleted on the session's host" in capsys.readouterr().err
    assert registry.read_text() == f"{sid}=tmp/loop/r.md\ttmp/loop/r.md.followup\n"


def test_send_to_a_remote_session_answers_its_question_on_its_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Once the answer lands, the question goes on the session's host, as a local one does: left
    there, a plain wait reads it as asked again, and a later follow-up took it as its baseline and
    returned the report before. The follow-up's file goes there too, for the revive line to name,
    and nothing is written beside the report path here."""

    class Far(FarFiles, FakeOmnigent):
        reporting = True

        def land(self, sid: str, text: str) -> None:
            super().land(sid, text)
            if self.reporting:
                self.files["tmp/loop/r.md"], self.times["tmp/loop/r.md"] = "done", time.time()

    og = Far(reply="done")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    sid = og.create("claude", "t", workspace="/elsewhere/repo")  # no such path here
    og.files["tmp/loop/r.md.question"] = "which base?"
    og.times["tmp/loop/r.md.question"] = time.time() - 120
    (tmp_path / "a.md").write_text("use main")
    registry = tmp_path / "registry"
    argv = ["send", sid, "a.md", "tmp/loop/r.md", "--confirm-seconds", "0", "--timeout", "5"]
    argv += ["--registry", str(registry)]
    assert omnigent_agent.main(argv) == 0
    assert capsys.readouterr().out.strip() == "tmp/loop/r.md"
    assert not (tmp_path / "tmp").exists()
    assert "tmp/loop/r.md.question" not in og.files
    # a brief file here names nothing its session can read: its text goes there
    assert og.files["tmp/loop/r.md.followup"] == "use main"
    assert omnigent_agent.wait(og, sid, Path("tmp/loop/r.md"), timeout=0) == "tmp/loop/r.md"
    # a later follow-up whose turn writes no report is not answered by the one before; a brief
    # file at that path here is another session's
    og.reporting = False
    here = tmp_path / omnigent_agent.brief_of(Path("tmp/loop/r.md"))
    here.parent.mkdir(parents=True)
    here.write_text("another session's")
    argv[2] = "use dev"  # inline
    assert omnigent_agent.main(argv) == 1
    assert "ended (idle) without tmp/loop/r.md\n" in capsys.readouterr().err
    assert og.files["tmp/loop/r.md.followup"] == "use dev"
    # still armed, as the wait failed: a revive names the file there
    assert registry.read_text() == f"{sid}=tmp/loop/r.md\ttmp/loop/r.md.followup\n"
    # a launcher's failure line there is no report, though stamped later than the next write
    og.reporting = True
    og.files["tmp/loop/r.md"], og.times["tmp/loop/r.md"] = "FAILED: timeout: x\n", 2**31
    argv[2] = "use prod"
    assert omnigent_agent.main(argv) == 0
    assert list((tmp_path / "tmp").rglob("*.*")) == [here]


def test_a_revived_remote_session_is_named_only_the_files_on_its_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remote session was briefed inline: a brief file at that path here is another session's.
    The follow-up's file the registry lists is on its host, and named."""

    class Far(FarFiles, Owing):
        pass

    monkeypatch.chdir(tmp_path)
    report = Path("tmp/loop/r.md")
    omnigent_agent.brief_of(report).parent.mkdir(parents=True)
    omnigent_agent.brief_of(report).write_text("another session's")
    for listed, read in (
        ([], ""),
        ([Path("tmp/loop/r.md.followup")], " Read your brief: tmp/loop/r.md.followup."),
    ):
        og = Far([told("brief", 60)], {"runner_online": False}, {"runner_online": True}, {})
        omnigent_agent.check(og, "a", report, 3600, {}, set(), print, listed)
        assert og.sent == [f"{omnigent_agent.REVIVED}{read} Write your report to {report}."]
    registry = tmp_path / "registry"
    registry.write_text(f"a={report}\n")
    omnigent_agent.arm(registry, "a", report, then=Path("tmp/loop/r.md.followup"), far=True)
    assert registry.read_text() == f"a={report}\ttmp/loop/r.md.followup\n"


def test_a_project_managers_board_adds_the_root_boards_standing_decisions(tmp_path: Path) -> None:
    (tmp_path / "board.md").write_text("## Standing decisions\n- merge on green\n## Next\nx\n")
    node = tmp_path / "projects" / "app"
    node.mkdir(parents=True)
    (node / "board.md").write_text("## Standing decisions (app only)\n- ship nightly\n")
    assert omnigent_agent.standing(node / "board.md") == "- merge on green\n\n- ship nightly"
    assert omnigent_agent.standing(tmp_path / "board.md") == "- merge on green"  # the root alone
    (node / "board.md").unlink()  # its own unread: the root's still apply
    assert omnigent_agent.standing(node / "board.md") == "- merge on green"
    (node / "notes.md").write_text("- local\n")  # another file there is not a manager's board
    assert omnigent_agent.standing(node / "notes.md") == "- local"


# --- a landing reaches the manager, whatever the lead writes (#784) ---

OLD, NEW = "a" * 40, "b" * 40
GIT_CONFIG = '[core]\n\tbare = false\n[remote "origin"]\n\turl = git@github.com:o/r.git\n'


def in_checkout(og: Fleet) -> None:
    """Each session's workspace is a checkout whose origin is ``o/r``."""
    og.files[".git"] = "HEAD\nconfig\nobjects/"
    og.files[".git/config"] = GIT_CONFIG


S1, S2, S3 = "1" * 40, "2" * 40, "3" * 40
COMPARE = f"{S1}\t2026-11-01T10:00:00Z\n{S2}\t2026-11-02T10:00:00Z\n{S3}\t2026-11-03T10:00:00Z\n"


def pr_list(*merged: tuple[int, str]) -> str:
    """What ``gh pr list --json number,mergeCommit`` prints for PRs merged as the given shas."""
    return json.dumps([{"number": n, "mergeCommit": {"oid": sha}} for n, sha in merged])


def github(
    monkeypatch: pytest.MonkeyPatch, head: list[str], prs: str | None = None
) -> list[tuple[str, ...]]:
    """``gh`` faked: the default branch's head is ``head[0]``, which a test moves between polls;
    a comparison lists three commits, and the login's merged PRs (``prs``, by default #12 as the
    first commit's squash merge and #13 as the second's) come from one ``pr list``. Any lookup of
    a single PR or of the login raises. Each call is recorded."""
    monkeypatch.setattr(omnigent_agent, "HEAD_SECONDS", 0)
    calls: list[tuple[str, ...]] = []
    mine = prs if prs is not None else pr_list((13, S2), (12, S1), (99, "f" * 40))

    def gh(*args: str, timeout: float = 0) -> str:
        # default_head's `--cache <n>s` is gh's own on-disk cache: not part of what these tests read
        args = tuple(
            x for i, x in enumerate(args) if x != "--cache" and not (i and args[i - 1] == "--cache")
        )
        calls.append(args)
        if args[1] == "repos/o/r/commits/HEAD":
            return head[0] + "\n"
        if args[1] == f"repos/o/r/compare/{OLD}...{NEW}":
            return COMPARE
        if args[:2] == ("pr", "list"):
            return mine
        raise AssertionError(args)

    monkeypatch.setattr(omnigent_agent, "gh", gh)
    return calls


def landed(registry: Path) -> list[dict]:
    path = registry.with_suffix(".events.jsonl")
    return (
        [e for e in events_of(registry) if e["id"].startswith("landed:")] if path.exists() else []
    )


def test_a_moved_default_branch_raises_one_act_event_for_its_new_head_and_then_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 60), ending("STILL WORKING: spec filed\nWaits: #12", 5))
    og.snap = {"status": "idle"}
    in_checkout(og)
    head = [OLD]
    github(monkeypatch, head)
    monkeypatch.setattr(omnigent_agent, "pr_settled", lambda pr, repo="": None)
    seen: list[int] = []

    def later(poll: int) -> None:
        seen.append(len(landed(registry)))
        if poll == 2:
            head[0] = NEW  # a PR landed between the second and third poll

    supervised(og, registry, monkeypatch, 4, later, notify="cos", console=True)
    assert seen == [0, 0, 1]  # nothing for the same head twice, one once it moved, none after
    (event,) = landed(registry)
    assert event["id"] == f"landed:o/r:{NEW}:{event['session']}"  # only the lead waiting on #12
    assert (event["settled"], event["action"]) == ("rule", "tell")
    assert NEW in event["line"] and "#12, #13" in event["line"]
    assert state_of(registry)["heads"] == {"o/r": NEW}


def test_a_range_of_foreign_numbers_lists_the_repos_own_merges_in_a_fixed_number_of_calls() -> None:
    """A fork's range holds the upstream's `(#n)` subjects, none a PR of the fork: the notice
    lists only the fork's own merges (a squash, then a queue's merge commit) and reads the
    repo twice however many commits there are, never once per number."""
    queue = "4" * 40
    commits = "".join(f"{c}\t2026-11-0{n}T10:00:00Z\n" for n, c in enumerate([S1, S2, queue], 1))
    commits += "".join(f"{n:040x}\t2026-11-09T10:00:00Z\n" for n in range(80))
    calls: list[tuple[str, ...]] = []

    def gh(*args: str, timeout: float = 0) -> str:
        calls.append(args)
        if args[1].startswith("repos/o/r/compare/"):
            return commits
        if args[:2] == ("pr", "list"):
            return pr_list((9, queue), (7, S1), (8, "e" * 40))  # #8 merged outside the range
        raise AssertionError(args)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(omnigent_agent, "gh", gh)
        assert omnigent_agent.merged_since("o/r", OLD, NEW) == "#7, #9"
    assert len(calls) == 2
    listing = calls[1]
    assert listing[:4] == ("pr", "list", "--repo", "o/r")
    assert "--author" in listing and listing[listing.index("--author") + 1] == "@me"
    assert "merged:>=2026-11-01" in listing[listing.index("--search") + 1]


def test_a_range_none_of_whose_commits_is_a_merged_pr_of_the_login_lists_nothing() -> None:
    def gh(*args: str, timeout: float = 0) -> str:
        return COMPARE if args[0] == "api" else pr_list((5, "e" * 40))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(omnigent_agent, "gh", gh)
        assert omnigent_agent.merged_since("o/r", OLD, NEW) == ""


def test_a_merged_pr_list_that_cannot_be_read_is_named_so_the_landing_is_still_raised() -> None:
    def gh(*args: str, timeout: float = 0) -> str:
        if args[0] == "api":
            return COMPARE
        raise omnigent_agent.Fail("gh pr list: HTTP 502")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(omnigent_agent, "gh", gh)
        got = omnigent_agent.merged_since("o/r", OLD, NEW)
    assert got == "not read: gh pr list: HTTP 502"


def test_the_first_head_seen_for_a_repo_is_stored_and_raises_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 60))
    in_checkout(og)
    calls = github(monkeypatch, [OLD])
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True)
    assert state_of(registry)["heads"] == {"o/r": OLD}
    assert landed(registry) == [] and posted(og) == []
    # no comparison: no old head (the PR poll's reads aside: the PR-stall sweep's open PRs, #1819, and the queue's
    # comments, #2085)
    swept = ("pulls?state=open", "issues/comments?")
    read = [c[1] for c in calls if not any(sweep in c[2] for sweep in swept)]
    assert read == ["repos/o/r/commits/HEAD"]


def test_a_restart_reports_a_landing_made_while_it_was_down_but_not_one_it_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 60), ending("STILL WORKING: x\nWaits: #12", 5))
    og.snap = {"status": "idle"}
    in_checkout(og)
    head = [OLD]
    github(monkeypatch, head)
    monkeypatch.setattr(omnigent_agent, "pr_settled", lambda pr, repo="": None)
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True)
    head[0] = NEW
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True)
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True)
    assert [e["id"] for e in landed(registry)] == [
        f"landed:o/r:{NEW}:{landed(registry)[0]['session']}"
    ]


def test_two_leads_in_one_repo_raise_one_event_for_one_new_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "registry"
    registry.write_text(f"a={tmp_path / 'a.md'}\tfollow\nb={tmp_path / 'b.md'}\tfollow\n")
    og = Leads(
        a=[told("brief", 60)], b=[told("brief", 60), ending("STILL WORKING: x\nWaits: #13", 5)]
    )
    og.snaps["b"] = {"status": "idle"}
    monkeypatch.setattr(omnigent_agent, "pr_settled", lambda pr, repo="": None)
    in_checkout(og)
    head = [OLD]
    calls = github(monkeypatch, head)
    supervised(
        og,
        registry,
        monkeypatch,
        2,
        lambda poll: head.__setitem__(0, NEW),
        notify="cos",
        console=True,
    )
    (event,) = landed(registry)  # a waits on nothing: only b, whose wait names #13, is told
    assert (event["session"], event["id"]) == ("b", f"landed:o/r:{NEW}:b")
    assert [c[1] for c in calls].count("repos/o/r/commits/HEAD") == 2  # one read per poll


def test_a_landing_the_lead_wrote_under_a_named_wait_reaches_the_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The issue's first case: the turn end routes `record` on its named wait, and the landing
    below it reaches the notified session anyway, as one post naming the new sha and the PRs."""
    registry = following(tmp_path)
    end = (
        "STILL WORKING: waiting on the second full gate run for #B\n- #A: landed. Restart, rebuild."
    )
    og = Lead(told("brief", 600), ending(end, 400))
    in_checkout(og)
    head = [OLD]
    github(monkeypatch, head)
    own = omnigent_agent.route({"kind": "ended", "last": end}, lambda: ("none", "unasked"))
    assert own == ("rule", "record", "names its wait: second")  # its own words reach no one
    moved = lambda poll: head.__setitem__(0, NEW)
    supervised(og, registry, monkeypatch, 2, moved, notify="cos", console=True)
    assert posted(og) == [] and landed(registry) == []  # no lead waits on #12 or #13: recorded only


FAR = "until 2099-01-01T00:00Z"  # a timed wait's end: the supervisor raises no deadline before it


def heads_moving(monkeypatch: pytest.MonkeyPatch, head: list[str]) -> None:
    """``gh`` faked: the default branch's head is ``head[0]``, any comparison one commit, the
    squash merge of a PR the login it runs as opened."""
    monkeypatch.setattr(omnigent_agent, "HEAD_SECONDS", 0)

    def gh(*args: str, timeout: float = 0) -> str:
        if args[:2] == ("pr", "list"):
            return pr_list((12, S1))
        if "repos/o/r/commits/HEAD" in args:
            return head[0] + "\n"
        return f"{S1}\t2026-11-01T10:00:00Z\n"

    monkeypatch.setattr(omnigent_agent, "gh", gh)


def landings_posted(og: Fleet) -> list[str]:
    """The posts to the chief of staff that carry a landing (a lead waiting on the supervisor
    also raises its own ends and deadlines, which these tests leave aside)."""
    return [p for p in posted(og) if "its default branch moved" in p]


def test_a_turn_end_from_before_the_run_is_in_no_post_though_its_label_shows_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#877's first case: a lead that never settles, its last message weeks old, is the row of
    two landings' posts; neither gives that message as its turn end."""
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    # a wait timed far ahead: no deadline, so it never settles
    old = "STILL WORKING: an outage notice from weeks ago\nWaits: supervisor"
    first_line = old.splitlines()[0]
    og = Lead(told("brief", 30 * 86400), ending(old, 29 * 86400))
    og.snap = {"labels": {omnigent_agent.PR_LABEL: "o/r#12"}}  # its PR: a landing is its news
    in_checkout(og)
    head = ["1" * 40]
    heads_moving(monkeypatch, head)

    def land(poll: int) -> None:  # a landing at polls 1 and 3, a post window apart
        offset[0] = (poll // 2) * 301.0
        if poll in (1, 3):
            head[0] = str(poll + 1) * 40

    supervised(og, registry, monkeypatch, 5, land, notify="cos", console=True)
    posts = landings_posted(og)
    assert len(posts) == 2 and all("its default branch moved" in p for p in posts)
    assert all("\na t-a: idle · " in p and old not in p and "turn ended ·" not in p for p in posts)
    assert [l["status.line"] for s, l in og.labels if s == "a"][-1].endswith(f": {first_line}")


def test_a_turn_end_in_the_run_is_posted_once_also_across_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#877's second case: a turn ending after the run that first saw the lead started, while the
    supervisor was down, is the row of the next post only, the post after it and one after
    another restart give it no more; the key it was told under is kept in the state."""
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    og = Lead(told("brief", 600), ending("STILL WORKING: x\nWaits: supervisor", 500))
    og.snap = {"labels": {omnigent_agent.PR_LABEL: "o/r#12"}}  # its PR: a landing is its news
    in_checkout(og)
    head = ["1" * 40]
    heads_moving(monkeypatch, head)
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True)  # the run that saw it
    offset[0] = 60.0
    new = "STILL WORKING: the second fix\nWaits: supervisor"
    og.histories["a"].append(ending(new, 0))  # written while the supervisor is down
    offset[0], head[0] = 120.0, "2" * 40

    def land(poll: int) -> None:  # one more landing, a post window later
        offset[0], head[0] = 120.0 + 301.0, "3" * 40

    supervised(og, registry, monkeypatch, 2, land, notify="cos", console=True)  # a restart
    offset[0], head[0] = 120.0 + 602.0, "4" * 40
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True)  # another restart
    posts = landings_posted(og)
    assert len(posts) == 3 and all("its default branch moved" in p for p in posts)
    assert [new.splitlines()[0] in p for p in posts] == [True, False, False]
    assert f"\na t-a: turn ended · waits on supervisor: {new.splitlines()[0]}" in posts[0]
    lead = state_of(registry)["leads"]["a"]
    assert lead["told_turn"] == lead["turn"] == f"m{new}0"


def test_a_turn_end_without_an_id_is_keyed_by_its_time_and_each_is_posted_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A last message with no id is keyed by its ``created_at``: two of them, written at two
    times, are each the row of one post, and neither again after a restart."""
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    anon = lambda text, ago: {k: v for k, v in ending(text, ago).items() if k != "id"}
    og = Lead(told("brief", 600), anon("STILL WORKING: x\nWaits: supervisor", 500))
    og.snap = {"labels": {omnigent_agent.PR_LABEL: "o/r#12"}}  # its PR: a landing is its news
    in_checkout(og)
    head = ["1" * 40]
    heads_moving(monkeypatch, head)
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True)  # the run that saw it
    first, second = (
        "STILL WORKING: first\nWaits: supervisor",
        "STILL WORKING: second\nWaits: supervisor",
    )
    offset[0] = 60.0
    og.histories["a"].append(anon(first, 0))  # written while the supervisor is down
    offset[0], head[0] = 120.0, "2" * 40

    def land(poll: int) -> None:  # the second turn ends, then a landing a post window later
        og.histories["a"].append(anon(second, 0))
        offset[0], head[0] = 120.0 + 301.0, "3" * 40

    supervised(og, registry, monkeypatch, 2, land, notify="cos", console=True)  # a restart
    offset[0], head[0] = 120.0 + 602.0, "4" * 40
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True)  # another restart
    posts = landings_posted(og)
    assert len(posts) == 3 and all("its default branch moved" in p for p in posts)
    assert [(first.splitlines()[0] in p, second.splitlines()[0] in p) for p in posts] == [
        (True, False),
        (False, True),
        (False, False),
    ]
    lead = state_of(registry)["leads"]["a"]
    assert lead["told_turn"] == lead["turn"] == str(og.histories["a"][-1]["created_at"])


def test_a_state_kept_before_seen_from_reports_no_old_turn_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state file written before ``seen_from`` was kept starts the lead's cutoff at the run that
    reads it, so its old last message is not the turn end of the next post."""
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    old = "STILL WORKING: since last month\nWaits: supervisor"
    og = Lead(told("brief", 30 * 86400), ending(old, 29 * 86400))
    og.snap = {"labels": {omnigent_agent.PR_LABEL: "o/r#12"}}  # its PR: a landing is its news
    in_checkout(og)
    head = ["1" * 40]
    heads_moving(monkeypatch, head)
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True)
    state = state_of(registry)
    for key in ("seen_from", "told_turn"):  # as an older supervisor wrote it
        state["leads"]["a"].pop(key, None)
    registry.with_suffix(".state.json").write_text(json.dumps(state))
    offset[0], head[0] = 120.0 + 301.0, "2" * 40
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True)  # a restart
    [post] = landings_posted(og)
    assert "its default branch moved" in post
    assert "\na t-a: idle · " in post and old not in post and "turn ended ·" not in post


def test_without_console_a_landing_is_posted_as_a_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 60))
    in_checkout(og)
    head = [OLD]
    github(monkeypatch, head)
    supervised(og, registry, monkeypatch, 2, lambda poll: head.__setitem__(0, NEW), notify="cos")
    assert posted(og) == []  # no lead waits on a merged PR: recorded, not posted


def test_a_lead_outside_a_checkout_or_a_failed_head_read_raises_nothing_and_logs_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 60))
    calls = github(monkeypatch, [OLD])
    supervised(og, registry, monkeypatch, 2, console=True)
    assert calls == []  # no checkout: no repo to watch
    in_checkout(og)

    def down(*args: str, timeout: float = 0) -> str:
        raise omnigent_agent.Fail("gh api repos/o/r/commits/HEAD: HTTP 502")

    monkeypatch.setattr(omnigent_agent, "gh", down)
    capsys.readouterr()
    supervised(og, registry, monkeypatch, 3, console=True)
    err = capsys.readouterr().err
    assert err.count("o/r: default branch not read: gh api repos/o/r/commits/HEAD: HTTP 502") == 1
    assert landed(registry) == []


def test_a_worktree_s_repo_is_read_from_its_common_dir_on_any_host() -> None:
    og = Fleet()
    og.files[".git"] = "gitdir: /src/r/.git/worktrees/lead-1\n"
    og.files["/src/r/.git/worktrees/lead-1/commondir"] = "../..\n"
    og.files["/src/r/.git/config"] = GIT_CONFIG
    assert omnigent_agent.workspace_repo(og, "a") == "o/r"  # type: ignore[arg-type]
    assert omnigent_agent.workspace_repo(Fleet(), "a") == ""  # type: ignore[arg-type]
    for url in (
        "https://github.com/o/r.git",
        "https://github.com/o/r",
        "ssh://git@github.com/o/r.git",
        "http://proxy@127.0.0.1:9/git/o/r",
    ):
        assert omnigent_agent.origin_repo(f'[remote "origin"]\n\turl = {url}\n') == "o/r"
    upstream_only = '[remote "upstream"]\n\turl = https://github.com/x/y\n'
    assert omnigent_agent.origin_repo(upstream_only) == ""


def running_session(age: float) -> FakeOmnigent:
    """A fake whose only session ``k`` is running, its newest item ``age`` seconds old."""
    og = FakeOmnigent()
    og.create("claude", "kid")
    og.sessions["k"] = [
        {"id": "i1", "type": "message", "role": "user", "created_at": time.time() - age}
    ]
    og.info = lambda s: {  # type: ignore[method-assign]
        "status": "running",
        "title": "kid",
        "workspace": "/w/kid",
    }
    return og


def test_status_and_observe_call_a_running_session_stuck_only_past_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(omnigent_agent, "STUCK_SECONDS", 1800)
    key, line = omnigent_agent.observe(running_session(2 * 3600), "k", {}, baseline=True)
    assert key == ("stuck", "i1")
    assert line.startswith("k kid: stuck (no activity for 120 min)")
    assert omnigent_agent.observe(running_session(1700), "k", {})[0] == ("running",)
    assert omnigent_agent.observe(running_session(1900), "k", {})[0] == ("stuck", "i1")


def test_an_idle_session_with_an_old_item_is_not_stuck() -> None:
    og = running_session(10**5)
    og.info = lambda s: {"status": "idle", "title": "kid"}  # type: ignore[method-assign]
    assert omnigent_agent.observe(og, "k", {}, baseline=True)[0][0] == "ended"


def test_stuck_minutes_is_none_for_a_session_that_is_not_running() -> None:
    old = [{"id": "i1", "created_at": time.time() - 10**5}]
    assert omnigent_agent.stuck_minutes({"status": "idle"}, old) is None
    assert omnigent_agent.stuck_minutes({"status": "failed"}, old) is None
    assert omnigent_agent.stuck_minutes({"status": "running"}, old) is not None


def brief_and_heartbeats(brief_age: float, beat_age: float) -> FakeOmnigent:
    """A running session whose history is its brief and recent server items, nothing the agent wrote."""
    og = running_session(brief_age)
    now = time.time()
    og.sessions["k"] = [
        {"id": "b", "type": "message", "role": "user", "created_at": now - brief_age},
        {"id": "h1", "type": "resource_event", "created_at": now - beat_age},
        {"id": "h2", "type": "heartbeat", "created_at": now - beat_age},
        {"id": "h3", "type": "status", "role": "system", "created_at": now - beat_age},
    ]
    return og


def test_a_brief_with_recent_heartbeats_is_stuck_and_reap_force_archives_it() -> None:
    og = brief_and_heartbeats(2 * 3600, 30)
    assert omnigent_agent.observe(og, "k", {}, baseline=True)[0] == ("stuck", "b")
    lines, failed = omnigent_agent.reap(og, ["k"], force=True)
    assert not failed and og.interrupted == ["k"] and og.archived == ["k"]
    assert lines == ["k kid: archived (stuck); check git status in /w/kid"]


def test_a_brief_with_heartbeats_is_not_stuck_before_the_limit_from_its_start() -> None:
    og = brief_and_heartbeats(600, 30)
    assert omnigent_agent.observe(og, "k", {})[0] == ("running",)
    assert omnigent_agent.reap(og, ["k"], force=True)[0][0].startswith("k kid: kept (running)")


@pytest.mark.parametrize(
    "item",
    [
        {"type": "message", "role": "assistant"},
        {"type": "function_call"},
        {"type": "function_call_output"},
        {"type": "reasoning"},
    ],
)
def test_a_recent_agent_item_keeps_a_session_from_being_stuck(item: dict) -> None:
    og = brief_and_heartbeats(2 * 3600, 30)
    og.sessions["k"].append({"id": "a", **item, "created_at": time.time() - 60})
    assert omnigent_agent.observe(og, "k", {})[0] == ("running",)
    assert omnigent_agent.reap(og, ["k"], force=True)[0][0].startswith("k kid: kept (running)")


def test_agent_progress_at_counts_only_what_the_agent_wrote() -> None:
    items = [
        {"type": "message", "role": "user", "created_at": 500.0},
        {"type": "heartbeat", "created_at": 400.0},
        {"type": "resource_event", "created_at": 450.0},
        {"type": "message", "role": "assistant", "created_at": 100.0},
        {"type": "function_call", "created_at": 200.0},
        {"type": "message", "role": "assistant"},  # undated
    ]
    assert omnigent_agent.agent_progress_at(items) == 500.0  # the newest brief, once it acted
    assert omnigent_agent.agent_progress_at(items[1:5]) == 200.0
    assert omnigent_agent.agent_progress_at(items[:3]) is None
    assert omnigent_agent.agent_progress_at([{"type": "reasoning", "created_at": 300.0}]) == 300.0


def test_a_freshly_resumed_session_is_not_stuck_and_reap_force_keeps_it() -> None:
    og = running_session(60)
    now = time.time()
    og.sessions["k"] = [
        {"id": "b0", "type": "message", "role": "user", "created_at": now - 4 * 3600},
        {"id": "a0", "type": "message", "role": "assistant", "created_at": now - 3 * 3600},
        {"id": "b1", "type": "message", "role": "user", "created_at": now - 20},
        {"id": "h", "type": "heartbeat", "created_at": now - 5},
    ]
    assert omnigent_agent.stuck_minutes({"status": "running"}, og.sessions["k"]) is None
    assert omnigent_agent.observe(og, "k", {})[0] == ("running",)
    lines, failed = omnigent_agent.reap(og, ["k"], force=True)
    assert not failed and og.archived == [] and lines[0].startswith("k kid: kept (running)")


def test_a_session_with_an_old_agent_item_and_a_recent_user_item_is_kept_by_reap_force() -> None:
    og = running_session(60)
    now = time.time()
    og.sessions["k"] = [
        {"id": "a", "type": "message", "role": "assistant", "created_at": now - 10**5},
        {"id": "u", "type": "message", "role": "user", "created_at": now - 60},
    ]
    assert omnigent_agent.observe(og, "k", {})[0] == ("running",)
    lines, failed = omnigent_agent.reap(og, ["k"], force=True)
    assert not failed and og.interrupted == [] and og.archived == []
    assert lines[0].startswith("k kid: kept (running)")


def test_revived_session_is_kept_until_its_first_item() -> None:
    og = running_session(60)
    now = time.time()
    og.sessions["k"] = [
        {"id": "b", "type": "message", "role": "user", "created_at": now - 3 * 3600},
        {"id": "a", "type": "message", "role": "assistant", "created_at": now - 2 * 3600},
        {
            "id": "r",
            "type": "message",
            "role": "user",
            "created_at": now - 5,
            "content": [
                {"type": "input_text", "text": omnigent_agent.revive_line(Path("/r.md"), [])}
            ],
        },
    ]
    assert omnigent_agent.stuck_minutes({"status": "running"}, og.sessions["k"]) is None
    lines, failed = omnigent_agent.reap(og, ["k"], force=True)
    assert not failed and og.archived == [] and lines[0].startswith("k kid: kept (running)")


def test_supervisor_and_stuck_agree_on_a_never_acted_lead_given_a_new_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 4000))
    og.snap = {"status": "running"}
    kw = {"notify": "cos", "console": True, "fyi": "post"}
    supervised(og, registry, monkeypatch, 1, **kw)  # first sight
    state = state_of(registry)
    first = state["leads"]["a"]["progress_at"] = time.time() - 2000  # first seen a while ago
    registry.with_suffix(".state.json").write_text(json.dumps(state))
    og.histories["a"].append(told("Owner's answer: carry on.", 5))
    supervised(og, registry, monkeypatch, 1, **kw)
    assert omnigent_agent.agent_progress_at(og.histories["a"]) is None
    assert state_of(registry)["leads"]["a"]["progress_at"] == first  # no second definition


def test_a_session_whose_items_carry_no_date_is_not_stuck() -> None:
    og = running_session(0)
    og.sessions["k"] = [{"id": "i1", "type": "message", "role": "assistant"}]
    assert omnigent_agent.observe(og, "k", {})[0] == ("running",)


def test_watch_reports_a_session_that_goes_stuck_once() -> None:
    og = running_session(60)
    polls = []

    def items(sid: str) -> list[dict]:
        polls.append(sid)  # the first poll is the baseline; the item ages by the second
        age = 60 if len(polls) < 2 else 10**5
        return [{"id": "i1", "type": "message", "role": "user", "created_at": time.time() - age}]

    og.items = items  # type: ignore[method-assign]
    lines = omnigent_agent.watch(og, ["k"], timeout=5)
    assert len(lines) == 1 and "stuck (no activity for" in lines[0]


def test_follow_leaves_a_quiet_running_lead_to_its_progress_deadline() -> None:
    og = running_session(10**5)
    assert omnigent_agent.follow(og, "k", {}, {}, {}) == ("", ("running",))


def test_reap_force_stops_and_archives_a_stuck_session_and_names_its_workspace() -> None:
    og = running_session(10**5)
    lines, failed = omnigent_agent.reap(og, ["k"], force=True)
    assert not failed
    assert og.interrupted == ["k"] and og.archived == ["k"]
    assert lines == ["k kid: archived (stuck); check git status in /w/kid"]


def test_reap_force_keeps_a_running_session_with_recent_activity() -> None:
    og = running_session(60)
    lines, failed = omnigent_agent.reap(og, ["k"], force=True)
    assert not failed and og.interrupted == [] and og.archived == []
    assert lines == ["k kid: kept (running) (running with recent activity, not stuck)"]


def test_reap_without_force_keeps_a_stuck_session() -> None:
    og = running_session(10**5)
    lines, _ = omnigent_agent.reap(og, ["k"])
    assert og.archived == [] and lines == ["k kid: kept (running)"]


def test_reap_force_reports_a_failed_stop_and_goes_on() -> None:
    og = running_session(10**5)

    def refuse(sid: str) -> None:
        raise omnigent_agent.Fail("POST events: HTTP 503")

    og.interrupt = refuse  # type: ignore[method-assign]
    lines, failed = omnigent_agent.reap(og, ["k"], force=True)
    assert failed and og.archived == []
    assert lines == ["k kid: finish failed: POST events: HTTP 503"]


def test_the_real_interrupt_posts_the_interrupt_event(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []
    og = omnigent_agent.Omnigent.__new__(omnigent_agent.Omnigent)
    og.call = lambda m, p, b=None: calls.append((m, p, b)) or {}  # type: ignore[method-assign]
    og.interrupt("s1")
    assert calls == [("POST", "/v1/sessions/s1/events", {"type": "interrupt"})]


def test_reap_force_is_a_flag_of_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    og = running_session(10**5)
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    assert omnigent_agent.main(["reap", "k", "--force"]) == 0
    assert og.archived == ["k"]


@pytest.mark.parametrize("ages", [(10**5, 60), (60, 10**5)])
def test_a_session_with_an_old_and_a_recent_item_is_running_and_kept_by_reap_force(
    ages: tuple[float, float],
) -> None:
    og = running_session(60)
    og.sessions["k"] = [
        {"id": f"i{n}", "type": "message", "role": "assistant", "created_at": time.time() - age}
        for n, age in enumerate(ages)
    ]
    assert omnigent_agent.observe(og, "k", {})[0] == ("running",)
    lines, failed = omnigent_agent.reap(og, ["k"], force=True)
    assert not failed and og.interrupted == [] and og.archived == []
    assert lines[0].startswith("k kid: kept (running)")


def test_check_tells_a_stuck_child_owing_a_report_once_naming_reap_force(
    tmp_path: Path,
) -> None:
    og = running_session(3600)
    og.active = lambda sid: 0.0  # type: ignore[attr-defined]
    og.send = lambda sid, text: None  # type: ignore[method-assign]
    said: list[str] = []
    resumed: dict[str, float] = {}

    def look() -> str:
        return omnigent_agent.check(
            og, "k", tmp_path / "absent", 10**6, resumed, set(), said.append
        )[0]

    assert look() == "" and look() == ""
    assert len(said) == 1
    assert "stuck (no activity for 60 min)" in said[0] and "reap --force k" in said[0]
    og.sessions["k"] = [{"id": "i2", "type": "message", "role": "user", "created_at": time.time()}]
    assert look() == "" and len(said) == 1 and "stuck:k" not in resumed
    og.sessions["k"] = [
        {"id": "i3", "type": "message", "role": "user", "created_at": time.time() - 7200}
    ]
    look()
    assert len(said) == 2  # a new stretch is news again


def test_check_does_not_call_a_child_stuck_while_its_own_workers_are_active(
    tmp_path: Path,
) -> None:
    og = running_session(3600)
    og.active = lambda sid: time.time() - 5  # type: ignore[attr-defined]
    said: list[str] = []
    omnigent_agent.check(og, "k", tmp_path / "absent", 10**6, {}, set(), said.append)
    assert said == []


class FarVerifierHost(FarFiles, FakeOmnigent):
    """Hosts ``here`` (this one, ``h``) and ``mac``, where every session runs; each brief that lands
    records which files were already there."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.there_at_brief: list[list[str]] = []

    def host(self, name: str) -> str:
        return {"here": "h", "h": "h", "mac": "mac"}[name]

    def hosts(self) -> dict[str, str]:
        return {"h": "here", "mac": "mac"}

    def land(self, sid: str, text: str) -> None:
        self.there_at_brief.append(sorted(self.files))
        super().land(sid, text)


PR7_HEAD = "0123456789abcdef0123456789abcdef01234567"


def far_verifier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hosts: str) -> FarVerifierHost:
    og = FarVerifierHost()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "docs" / "agents").mkdir(parents=True)
    (tmp_path / "docs/agents/loop.md").write_text(f"# Loop\n\n{hosts}")
    (tmp_path / "b.md").write_text(
        f"Review PR #7 as the verifier; first `git worktree add --detach tree {PR7_HEAD}`"
    )
    gh = tmp_path.parent / f"{tmp_path.name}-ghbin"
    gh.mkdir()
    (gh / "gh").write_text(f"#!/bin/sh\necho {PR7_HEAD}\n")
    (gh / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{gh}{os.pathsep}{os.environ['PATH']}")
    (tmp_path / "gate.log").write_text("abc\nok\n")
    return og


VERIFY_FAR = ["start", "claude", "v", "b.md", "/there/repo/tmp/loop/pr7-v1.md", "--top-level"]
VERIFY_FAR += ["--host", "mac", "--pr", "7", "--role", str(VERIFIER_MD)]
VERIFY_FAR += ["--workspace", "/there/repo", "--confirm-seconds", "0"]


def test_a_cloud_pr_start_on_another_host_says_its_launch_dir_head_is_not_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = far_verifier(tmp_path, monkeypatch, "")
    og.procs.setdefault("mac", {})["/there/repo/.claude/settings.local.json"] = PINNED
    og.others = [
        {"id": "x1", "host_id": "mac", "status": "idle", "updated_at": 1}
    ]  # a file reads through it
    og.files["/there/repo/.claude/settings.local.json"] = PINNED  # what the session reads
    og.harnesses = {"claude-cloud": "acp:claude-cloud"}
    argv = ["start", "claude-cloud", "w", "do X", str(tmp_path / "tmp" / "r.md"), "--top-level"]
    argv += [
        "--host",
        "mac",
        "--pr",
        "a/b#7",
        "--workspace",
        "/there/repo",
        "--confirm-seconds",
        "0",
    ]
    assert omnigent_agent.main(argv) == 0
    err = capsys.readouterr().err
    assert "launch dir is on another host" in err and "HEAD is not moved" in err


def test_a_verifier_starts_on_a_host_loop_md_lists_with_its_files_there_and_reports_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Its --pr names the PR it reviews, not a cloud report channel: the brief goes inline to a
    top-level session on that host, the proof log is there before the brief, and wait reads its
    report through the file API and writes it to tmp/loop/ here, the only file made here."""
    og = far_verifier(tmp_path, monkeypatch, "Verifier hosts: box, mac\n")
    assert omnigent_agent.main([*VERIFY_FAR, "--file", "gate.log"]) == 0
    assert (
        og.top_level == [True] and og.workspaces == ["/there/repo"] and og.hosts_of["s0"] == "mac"
    )
    brief = omnigent_agent.text_of(og.sessions["s0"][1])
    assert brief.startswith("Work on v. Your brief:") and "git worktree add --detach" in brief
    assert "issues/7/comments" not in brief  # no PR preamble: its report is a file there
    assert og.there_at_brief == [["/there/repo/tmp/loop/gate.log"]]
    assert og.files["/there/repo/tmp/loop/gate.log"] == "abc\nok\n"
    report = "VERDICT: 0 blocker\nSATISFIED: yes\n"
    og.files["/there/repo/tmp/loop/pr7-v1.md"] = report
    og.times["/there/repo/tmp/loop/pr7-v1.md"] = time.time()  # written after the brief
    capsys.readouterr()
    argv = ["wait", "s0", "/there/repo/tmp/loop/pr7-v1.md", "--verdict", "--timeout", "5"]
    assert omnigent_agent.main(argv) == 0
    out, err = capsys.readouterr()
    home = tmp_path / "tmp" / "loop" / "pr7-v1.md"
    assert out.strip() == str(home) and home.read_text() == report
    assert "tracked changes are not checked here" in err
    made = {p for p in tmp_path.rglob("*") if ".git" not in p.parts}
    assert made - {tmp_path / p for p in ("docs", "docs/agents", "docs/agents/loop.md")} == {
        tmp_path / "b.md",
        tmp_path / "gate.log",
        tmp_path / "tmp",
        tmp_path / "tmp/loop",
        home,
    }


@pytest.mark.parametrize("host", ["mac", "h"])
def test_a_verifier_on_a_host_loop_md_does_not_list_is_refused_with_the_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], host: str
) -> None:
    og = far_verifier(tmp_path, monkeypatch, "Verifier hosts: box, other\n")
    argv = [*VERIFY_FAR]
    argv[argv.index("mac")] = host
    assert omnigent_agent.main(argv) == 1
    assert f"--host {host} is no verifier host loop.md lists: box, other" in capsys.readouterr().err
    assert og.sessions == {}


def test_without_a_verifier_hosts_line_a_verifier_starts_on_any_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = far_verifier(tmp_path, monkeypatch, "Owner: someone\n")
    assert omnigent_agent.main(VERIFY_FAR) == 0
    assert og.hosts_of["s0"] == "mac"


def test_a_lead_names_a_host_by_its_id_and_a_file_needs_another_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The line lists names; a host given by its id is checked by its name. --file is refused for
    a session here, which reads its own worktree."""
    monkeypatch.setattr(omnigent_agent, "check_out_head", lambda *a: None)  # no remote here
    og = far_verifier(tmp_path, monkeypatch, "Verifier hosts: here\n")
    argv = [*VERIFY_FAR]
    argv[argv.index("mac")] = "h"
    argv[argv.index("/there/repo")] = str(tmp_path)
    argv[4] = str(tmp_path / "tmp/loop/pr7-v1.md")
    assert omnigent_agent.main([*argv, "--file", "gate.log"]) == 1
    assert "--file is for a session on another host" in capsys.readouterr().err
    assert omnigent_agent.main(argv) == 0
    assert og.hosts_of["s0"] == "h"


def test_a_far_file_that_is_missing_here_or_fails_there_starts_no_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing file is refused before any session; a write the far host refuses abandons the
    session it made, before its brief names a file that is not there."""
    og = far_verifier(tmp_path, monkeypatch, "")
    assert omnigent_agent.main([*VERIFY_FAR, "--file", "absent.log"]) == 1
    assert "--file: no file at absent.log" in capsys.readouterr().err
    assert og.sessions == {}

    def refuse(sid: str, path: str, method: str = "GET", body: object = None) -> dict:
        raise omnigent_agent.Fail("PUT: HTTP 403")

    og.filesystem = refuse  # type: ignore[method-assign]
    assert omnigent_agent.main([*VERIFY_FAR, "--file", "gate.log"]) == 1
    assert "a --file did not reach its host: PUT: HTTP 403" in capsys.readouterr().err
    assert og.archived == ["s0"] and og.there_at_brief == []


def test_a_role_that_only_names_verifier_keeps_the_worker_pr_meaning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only the verifier's role file (verifier.md) takes --pr as the PR it reviews: another role
    whose path holds the word is a worker, refused --pr on a native agent."""
    og = far_verifier(tmp_path, monkeypatch, "")
    argv = [*VERIFY_FAR]
    helper = tmp_path / "roles" / "not-a-verifier-helper.md"
    helper.parent.mkdir()
    helper.write_text("role")
    argv[argv.index(str(VERIFIER_MD))] = str(helper)
    assert omnigent_agent.main(argv) == 1
    assert "is not a cloud agent" in capsys.readouterr().err
    assert og.sessions == {}


def test_a_far_verifier_starts_on_a_clone_other_sessions_hold_and_others_are_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A far verifier works in a worktree of its own under the clone, so a lead holding the clone
    does not stop it; a worker there, or a verifier on this host, is still refused."""
    monkeypatch.setattr(omnigent_agent, "check_out_head", lambda *a: None)  # no remote here
    og = far_verifier(tmp_path, monkeypatch, "")
    og.others = [
        {
            "id": "L",
            "title": "lead",
            "host_id": "mac",
            "workspace": "/there/repo",
            "status": "idle",
        },
        {"id": "M", "title": "lead", "host_id": "h", "workspace": str(tmp_path), "status": "idle"},
    ]
    assert omnigent_agent.main(VERIFY_FAR) == 0
    assert "the verifier works in its own worktree" in capsys.readouterr().err
    worker = [*VERIFY_FAR]
    worker[worker.index(str(VERIFIER_MD))] = str(VERIFIER_MD.with_name("worker.md"))
    worker[worker.index("--pr") : worker.index("--pr") + 2] = []
    worker[1] = "w"
    assert omnigent_agent.main(worker) == 1
    err = capsys.readouterr().err
    assert "is in use on host mac by live session" in err and "L lead" in err
    here = [*VERIFY_FAR]
    here[here.index("mac")] = "h"
    here[here.index("/there/repo")] = str(tmp_path)
    here[1], here[4] = "u", str(tmp_path / "tmp/loop/pr7-v1.md")
    assert omnigent_agent.main(here) == 1
    err = capsys.readouterr().err
    assert "is in use on host h by live session" in err and "M lead" in err


def test_supervise_install_writes_a_restarting_service_per_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unit and plist run the supervise command less --install and --log, restart it and
    log to the file, in directive forms systemd accepts; each registry gets its own name; only
    the variables the script reads are carried over. Neither systemctl nor launchctl runs."""
    one, two = tmp_path / "a.registry", tmp_path / "b.registry"
    log, cwd = tmp_path / "supervise.log", tmp_path / "work dir"
    argv = omnigent_agent.service_argv(
        [
            "supervise",
            "--registry",
            str(one),
            "--notify",
            "S",
            "--install",
            "--log",
            str(log),
            "X=r.md",
        ],
        ["X=r.md"],
    )
    assert argv[2:] == ["supervise", "--registry", str(one), "--notify", "S"]
    name = omnigent_agent.service_name(one)
    session_env = {
        "PATH": "/tmp/terminal-shim/bin:/home/u/.local/bin:/usr/bin",
        "TMPDIR": "/scratch/t",
        "RUNNER_SERVER_URL": "http://server",
        "OMNIGENT_USER_ID": "u1",
        "OMNIGENT_SUPERVISE_REGISTRY": str(one),
        "OMNIGENT_MODEL_CLAUDE": "m",
        "OMNIGENT_STUCK_SECONDS": "900",
        "OMNIGENT_RUNNER_ID": "runner-token",
        "OMNIGENT_RUNNER_PARENT_PID": "41",
        "OMNIGENT_ZYGOTE_SOCKET": "/tmp/z.sock",
        "OMNIGENT_TERMINAL_LAUNCH_SESSION_ID": "t1",
        "OMNIGENT_SESSION_ID": "s1",
        "OMNIGENT_PROCESS_LOG_FILE": "/tmp/p.log",
        "OMNIGENT_CONNECT_MARKER": "m",
        "HOME": "/home/u",
    }
    session_env["PATH"] += ":/scratch/t/x/bin"
    env = omnigent_agent.service_env(session_env)
    assert env == {
        "PATH": "/home/u/.local/bin:/usr/bin",
        "OMNIGENT_MODEL_CLAUDE": "m",
        "OMNIGENT_STUCK_SECONDS": "900",
        "OMNIGENT_SUPERVISE_REGISTRY": str(one),
        "OMNIGENT_USER_ID": "u1",
        "RUNNER_SERVER_URL": "http://server",
    }
    unit = omnigent_agent.systemd_unit(argv, log, name, env, cwd).splitlines()
    exec_start = next(line for line in unit if line.startswith("ExecStart="))
    assert f'"{one}" "--notify" "S"' in exec_start and "--install" not in exec_start
    assert "--log" not in exec_start and "X=r.md" not in exec_start
    # systemd takes these unquoted: a quoted WorkingDirectory is "not absolute"
    assert f"WorkingDirectory={cwd}" in unit
    assert f"StandardOutput=append:{log}" in unit and f"StandardError=append:{log}" in unit
    assert "Restart=always" in unit and 'Environment="PATH=/home/u/.local/bin:/usr/bin"' in unit
    assert not any(x in "\n".join(unit) for x in ("RUNNER_ID", "terminal-shim", "z.sock"))
    plist = omnigent_agent.launchd_plist(argv, log, omnigent_agent.service_label(one), env, cwd)
    assert plist["Label"] == "omnigent.supervise." + name.rsplit("-", 1)[1]
    assert plist["ProgramArguments"] == argv and plist["KeepAlive"] and plist["RunAtLoad"]
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == str(log)
    assert plist["EnvironmentVariables"] == {**env, "OMNIGENT_SERVICE": f"launchd:{plist['Label']}"}
    assert name.startswith("omnigent-supervise-") and name != omnigent_agent.service_name(two)
    assert omnigent_agent.service_name(tmp_path / "x" / ".." / "a.registry") == name

    # A re-install must restart a running unit so it takes the new command line: `restart`, not
    # `start` or `enable --now`. The one runner function is recorded, so nothing runs.
    calls: list[list[str]] = []
    monkeypatch.setattr(
        omnigent_agent, "_service_call", lambda cmd, check=True: calls.append(cmd) or ""
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(omnigent_agent.sys, "platform", "linux")
    omnigent_agent.install_service(argv, log, one)
    systemctl = [c[2:] for c in calls if c[:2] == ["systemctl", "--user"]]
    assert systemctl == [["daemon-reload"], ["enable", name], ["restart", name]]
    assert (tmp_path / "home/.config/systemd/user" / f"{name}.service").is_file()
    calls.clear()
    monkeypatch.setattr(omnigent_agent.sys, "platform", "darwin")
    omnigent_agent.install_service(argv, log, one)
    label = omnigent_agent.service_label(one)
    assert [c[:2] for c in calls] == [["launchctl", "bootout"], ["launchctl", "bootstrap"]]
    assert calls[0][2].endswith(f"/{label}") and calls[1][3].endswith(f"/{label}.plist")


def test_supervise_service_logs_a_stop_and_restart_hint_first_and_a_hand_run_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2350: a kill of the service-run supervisor restarts it. The unit and plist mark the service
    in the environment; a supervisor seeing the mark logs the stop and restart commands first."""
    reg = tmp_path / "a.registry"
    name, label = omnigent_agent.service_name(reg), omnigent_agent.service_label(reg)
    unit = omnigent_agent.systemd_unit(["x"], tmp_path / "l", name, {}, tmp_path)
    assert f'Environment="OMNIGENT_SERVICE=systemd:{name}"' in unit
    plist = omnigent_agent.launchd_plist(["x"], tmp_path / "l", label, {}, tmp_path)
    assert plist["EnvironmentVariables"]["OMNIGENT_SERVICE"] == f"launchd:{label}"

    linux = omnigent_agent.service_hint({"OMNIGENT_SERVICE": f"systemd:{name}"})
    assert linux and f"systemctl --user stop {name}" in linux
    assert f"systemctl --user restart {name}" in linux and "kill" in linux
    mac = omnigent_agent.service_hint({"OMNIGENT_SERVICE": f"launchd:{label}"}, uid=501)
    assert mac and f"launchctl bootout gui/501/{label}" in mac
    assert f"launchctl kickstart -k gui/501/{label}" in mac
    assert omnigent_agent.service_hint({}) is None

    class Stop(Exception): ...

    def first_line(env: dict[str, str]) -> str:
        monkeypatch.delenv("OMNIGENT_SERVICE", raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setattr(omnigent_agent, "Uncarded", lambda og: (_ for _ in ()).throw(Stop()))
        with pytest.raises(Stop):
            omnigent_agent.supervise_registry(object(), reg)  # type: ignore[arg-type]
        return capsys.readouterr().out

    assert f"systemctl --user stop {name}" in first_line({"OMNIGENT_SERVICE": f"systemd:{name}"})
    assert first_line({}) == ""


def test_supervise_install_arms_its_pairs_and_uninstall_removes_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through main: --install arms its <session>=<report> pairs in the registry, not in the
    unit's ExecStart, and --uninstall stops and removes the unit or plist. The one runner
    function is recorded, so neither systemctl nor launchctl runs."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        omnigent_agent, "_service_call", lambda cmd, check=True: calls.append(cmd) or ""
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    registry, log = tmp_path / "r.registry", tmp_path / "s.log"
    name, label = omnigent_agent.service_name(registry), omnigent_agent.service_label(registry)
    unit = tmp_path / "home/.config/systemd/user" / f"{name}.service"
    plist = tmp_path / "home/Library/LaunchAgents" / f"{label}.plist"
    supervise = ["supervise", "--registry", str(registry)]

    monkeypatch.setattr(omnigent_agent.sys, "platform", "linux")
    assert omnigent_agent.main([*supervise, "--install", "--log", str(log), "S1=rep.md"]) == 0
    assert "S1" in omnigent_agent.registered(registry)[0]
    assert f"S1={tmp_path / 'rep.md'}" in registry.read_text()
    exec_start = next(x for x in unit.read_text().splitlines() if x.startswith("ExecStart="))
    assert "S1" not in exec_start and "rep.md" not in exec_start
    calls.clear()
    assert omnigent_agent.main([*supervise, "--uninstall"]) == 0
    assert ["systemctl", "--user", "disable", "--now", name] in calls
    assert ["systemctl", "--user", "daemon-reload"] in calls and not unit.exists()

    monkeypatch.setattr(omnigent_agent.sys, "platform", "darwin")
    assert omnigent_agent.main([*supervise, "--install", "--log", str(log)]) == 0
    assert plist.is_file()
    calls.clear()
    assert omnigent_agent.main([*supervise, "--uninstall"]) == 0
    assert [c[:2] for c in calls] == [["launchctl", "bootout"]] and not plist.exists()


# --- a wait on a PR or issue reference is a named wait (#919) ---


def test_a_pr_or_issue_reference_after_waiting_on_is_a_named_wait() -> None:
    """`#N`, `owner/repo#N`, or either as a markdown link's text: the wait is that reference."""
    wait_of = omnigent_agent.wait_of
    assert wait_of("STILL WORKING: waiting on #916 and #908") == "#916"
    assert wait_of("STILL WORKING: waiting on [#916](https://github.com/o/r/pull/916)") == "#916"
    assert wait_of("STILL WORKING: waiting on [owner/repo#12](url)") == "owner/repo#12"
    assert wait_of("STILL WORKING: this waits for owner/repo#12") == "owner/repo#12"
    assert (
        wait_of("STILL WORKING: waiting on the/repo#12") == "the/repo#12"
    )  # an owner named like a pronoun
    assert wait_of("STILL WORKING: on it") == "unknown"


@pytest.mark.parametrize(
    ("text", "hosts"),
    [
        ("Placement: hosts: box, mac\n", ["box", "mac"]),
        ("Placement: same machine\n", []),
        ("Placement: CI\n", []),
        ("Verifier hosts: box, mac\n", ["box", "mac"]),
        ("Cloud verifier: worker session\n", []),
        ("Verifier hosts: box\nPlacement: hosts: mac\n", ["mac"]),
        ("Verifier hosts: box\nPlacement: same machine\n", []),
        ("Owner: someone\n", []),
    ],
)
def test_the_placement_line_names_hosts_and_the_two_old_lines_keep_their_meaning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, hosts: list[str]
) -> None:
    far_verifier(tmp_path, monkeypatch, text)
    assert omnigent_agent.placement_hosts(str(tmp_path)) == hosts


START_CLOUD = ["start", "claude", "w", "do X", "/there/repo/tmp/r.md", "--top-level"]
START_CLOUD += ["--workspace", "/there/repo", "--confirm-seconds", "0"]


def test_a_cloud_start_without_host_follows_the_placement_line_never_the_bridge_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    og = far_verifier(tmp_path, monkeypatch, "Placement: same machine\n")
    assert omnigent_agent.main(START_CLOUD) == 1
    assert "--host" in capsys.readouterr().err and og.sessions == {}
    (tmp_path / "docs/agents/loop.md").write_text("Placement: hosts: box, mac\n")
    real = og.call
    hosts = [
        {"host_id": "h", "name": "here", "status": "online"},
        {"host_id": "b", "name": "box", "status": "offline"},
        {"host_id": "mac", "name": "mac", "status": "online"},
    ]
    og.call = lambda m, path, *a, **k: (  # type: ignore[method-assign]
        {"hosts": hosts} if (m, path) == ("GET", "/v1/hosts") else real(m, path, *a, **k)
    )
    assert omnigent_agent.main(START_CLOUD) == 0
    assert og.hosts_of["s0"] == "mac"
    # an explicit --host works as before, even when the setting names another and in a cloud session
    (tmp_path / "docs/agents/loop.md").write_text("Placement: hosts: here, mac\n")
    assert omnigent_agent.main([*START_CLOUD, "--host", "mac"]) == 0
    assert og.hosts_of["s1"] == "mac"
    # outside a cloud session nothing changes: this host
    monkeypatch.delenv("CLAUDE_CODE_REMOTE")
    (tmp_path / "docs/agents/loop.md").write_text("Placement: same machine\n")
    argv = ["start", "claude", "w2", "do Y", str(tmp_path / "tmp/r2.md"), "--top-level"]
    assert omnigent_agent.main([*argv, "--workspace", str(tmp_path), "--confirm-seconds", "0"]) == 0
    assert og.hosts_of["s2"] == "h"


@pytest.mark.parametrize(
    ("line", "host", "code"),
    [
        ("Placement: hosts: box, mac\n", "mac", 0),
        ("Placement: hosts: box, other\n", "mac", 1),
        ("Verifier hosts: mac\nPlacement: hosts: box\n", "mac", 1),
        ("Verifier hosts: box\nPlacement: hosts: mac\n", "mac", 0),
        ("Verifier hosts: box\nPlacement: same machine\n", "mac", 0),
    ],
)
def test_a_verifier_start_on_a_host_follows_the_placement_line_over_the_old_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str, host: str, code: int
) -> None:
    """A Placement hosts: list limits a verifier's host as Verifier hosts: did, and the new line
    wins; a Placement that names no hosts (same machine) limits nothing, as no line did."""
    og = far_verifier(tmp_path, monkeypatch, line)
    argv = [*VERIFY_FAR]
    argv[argv.index("mac")] = host
    assert omnigent_agent.main(argv) == code
    assert bool(og.sessions) == (code == 0)


# --- a question card a session posts to its user is not an act for the parent (#920) ---


def test_a_question_card_to_the_user_is_one_record_and_a_harness_question_still_acts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A card (``policy_name`` ``sys_ask_user_question``) returns at once and its user answers it in
    their Inbox: the session is not blocked, and the card is one record line per question id."""
    registry = following(tmp_path)
    monkeypatch.setattr(omnigent_agent, "PROMPT_CONFIRM_SECONDS", 0.0)
    og = Lead(told("brief", 60), ending("STILL WORKING: on it", 50))
    card = {
        "elicitation_id": "q1",
        "params": {
            "policy_name": "sys_ask_user_question",
            "ask_user_question": {"questions": [{"question": "Which base?"}]},
            "message": "Which base?",
        },
    }
    harness = {
        "elicitation_id": "e2",
        "params": {
            "policy_name": "claude_native_permission",
            "ask_user_question": {"questions": [{"question": "Go?"}]},
            "message": "Go?",
        },
    }
    og.snap = {"status": "running", "pending_elicitations": [card]}
    states: list[str] = []

    def later(poll: int) -> None:
        states.append(state_of(registry)["leads"]["a"]["state"])
        if poll == 3:
            og.snap = {"status": "running", "pending_elicitations": [card, harness]}

    supervised(og, registry, monkeypatch, 5, later, notify="cos", console=True)
    assert states[:3] == ["running"] * 3  # the card alone blocks nothing
    routed = [(e["kind"], e["action"], e["line"]) for e in events_of(registry)]
    assert routed[0] == ("asked", "record", "a: asked its user: q1: Which base?")
    assert (events_of(registry)[0]["settled"], "Inbox" in events_of(registry)[0]["why"]) == (
        "rule",
        True,
    )  # the card's own rule records it, not a guess about the lead's turn
    assert [r[:2] for r in routed[1:]] == [("blocked", "act")]  # the card said once, never again
    assert "Which base?" not in routed[1][2]  # the parent's act names only the harness question
    assert not any("Which base?" in p for p in posted(og))


def owner_posts_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, previews: list[str]
) -> list[str]:
    """The posts to ``cos`` when a lead blocks on one approval prompt per preview, each answered
    before the next, with the post window passed between them."""
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    monkeypatch.setattr(omnigent_agent, "PROMPT_CONFIRM_SECONDS", 0.0)
    og = Lead(told("brief", 60), ending("STILL WORKING: its worker runs", 5))

    def prompt(n: int) -> dict:
        item = {"elicitation_id": f"e{n}", "params": {"content_preview": previews[n]}}
        return {"status": "running", "pending_elicitations": [item]}

    og.snap = prompt(0)

    def later(poll: int) -> None:
        offset[0] = poll * 400.0
        og.snap = prompt(poll) if poll < len(previews) else {"status": "running"}

    supervised(og, registry, monkeypatch, len(previews) + 1, later, notify="cos", console=True)
    return posted(og)


def test_the_same_owner_item_at_a_later_turn_end_is_posted_as_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = owner_posts_for(tmp_path, monkeypatch, ["Bash(ls)", "Bash(ls)"])
    assert "[owner] a t-a: blocked on a prompt: e0: Bash(ls)" in first
    assert second.startswith("Supervisor [info] b-")
    assert "[info] a t-a: blocked on a prompt: e1: Bash(ls)" in second


def test_an_owner_item_ignores_the_elicitation_id_a_re_raised_prompt_gets() -> None:
    line = "a t-a: blocked on a prompt: {}: Bash(ls)"
    assert omnigent_agent.owner_item(line.format("e0")) == omnigent_agent.owner_item(
        line.format("e1")
    )
    joined = "a t-a: blocked on a prompt: {}: Bash(ls); {}: Bash(rm x)"
    assert omnigent_agent.owner_item(joined.format("e0", "e1")) == omnigent_agent.owner_item(
        joined.format("e2", "e3")
    )


def test_a_changed_owner_item_is_posted_as_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = owner_posts_for(tmp_path, monkeypatch, ["Bash(ls)", "Bash(rm x)"])
    assert second.startswith("Supervisor [owner] b-")
    assert "[owner] a t-a: blocked on a prompt: e1: Bash(rm x)" in second
    assert "[owner] a t-a: blocked on a prompt: e0: Bash(ls)" in first


def test_a_new_session_s_first_owner_item_is_posted_as_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    [post] = owner_posts_for(tmp_path, monkeypatch, ["Bash(ls)"])
    assert post.startswith("Supervisor [owner] b-")


def owner_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, looks: list[tuple[dict, str]]
) -> list[str]:
    """The route of each post to ``cos`` when the lead shows one look per poll, the post window
    passed between them: a look is its ``snap`` and, unless empty, the turn end it then writes."""
    registry = following(tmp_path)
    offset = skewed(monkeypatch)
    monkeypatch.setattr(omnigent_agent, "PROMPT_CONFIRM_SECONDS", 0.0)
    og = Lead(told("brief", 60), ending("STILL WORKING: its worker runs", 5))

    def show(n: int) -> None:
        snap, text = looks[n]
        og.snap = snap
        if text:
            og.histories["a"].append({**ending(text, 0), "id": f"end{n}"})

    show(0)

    def later(poll: int) -> None:
        offset[0] = poll * 400.0
        if poll < len(looks):
            show(poll)
        else:
            og.snap = {"status": "running"}

    supervised(og, registry, monkeypatch, len(looks) + 1, later, notify="cos", console=True)
    return [re.match(r"Supervisor \[(\w+)\]", p).group(1) for p in posted(og)]  # type: ignore[union-attr]


def prompt_look(n: int, title: str, queued: int) -> tuple[dict, str]:
    item = {"elicitation_id": f"e{n}", "params": {"content_preview": "Bash(ls)"}}
    inputs = [{"id": f"i{k}"} for k in range(queued)]
    return {
        "status": "running",
        "pending_elicitations": [item],
        "title": title,
        "pending_inputs": inputs,
    }, ""


@pytest.mark.parametrize(
    ("looks", "routes"),
    [
        pytest.param(
            [({}, "USER NEEDED: approve A"), ({}, "USER NEEDED: approve B")],
            ["act", "act"],  # no card open: the manager's to act on (#2170)
            id="two-different-requests",
        ),
        pytest.param(
            [
                ({}, "USER NEEDED: approve A"),
                ({}, "DONE: step one"),
                ({}, "USER NEEDED: approve A"),
            ],
            [
                "act",
                "act",
                "act",
            ],  # only an owner item is kept once; a cardless ask is the manager's (#2170)
            id="one-request-twice",
        ),
        pytest.param(
            [prompt_look(0, "t-a", 0), prompt_look(1, "t-a renamed", 2)],
            ["owner", "info"],
            id="one-prompt-under-a-new-title-and-queue",
        ),
    ],
)
def test_an_owner_item_is_keyed_on_what_the_owner_acts_on_not_its_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, looks: list, routes: list[str]
) -> None:
    assert owner_routes(tmp_path, monkeypatch, looks) == routes


# --- a child never falls back to the runner's host, and a host verifier runs at the PR's head (#917) ---


def test_a_cloud_verifier_start_without_host_sends_no_create_and_names_the_in_vm_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    og = far_verifier(tmp_path, monkeypatch, "")
    verifier = [
        "start",
        "claude",
        "v",
        "b.md",
        str(tmp_path / "tmp/r.md"),
        "--confirm-seconds",
        "0",
    ]
    verifier += ["--pr", "7", "--role", str(VERIFIER_MD)]
    assert omnigent_agent.main(verifier) == 1
    err = capsys.readouterr().err
    assert "cloud_verifier.py verify" in err and "--host" in err and og.sessions == {}
    # any other child is refused too, naming both routes; an explicit --host start still works
    worker = ["start", "claude", "w", "b.md", str(tmp_path / "tmp/r2.md"), "--confirm-seconds", "0"]
    assert omnigent_agent.main(worker) == 1
    err = capsys.readouterr().err
    assert "--host" in err and "claude-cloud" in err and og.sessions == {}
    assert omnigent_agent.main([*VERIFY_FAR]) == 0 and len(og.sessions) == 1


def test_a_verifier_on_a_host_for_a_pr_starts_at_the_prs_head_never_the_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = far_verifier(tmp_path, monkeypatch, "")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]

    def run(*cmd: str) -> str:
        done = subprocess.run(
            [*git, *cmd], cwd=tmp_path, capture_output=True, text=True, check=True
        )
        return done.stdout.strip()

    run("add", "-A")
    run("commit", "-q", "-m", "base")
    base = run("rev-parse", "HEAD")
    (tmp_path / "change.txt").write_text("pr\n")
    run("add", "-A")
    run("commit", "-q", "-m", "pr")
    head = run("rev-parse", "HEAD")
    run("checkout", "-q", "--detach", base)
    # origin is this repo, whose pull/7/head names the PR's commit
    run("update-ref", "refs/pull/7/head", head)
    run("remote", "add", "origin", str(tmp_path))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(f"#!/bin/sh\necho {head}\n")
    (bin_dir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    argv = ["start", "claude", "v", "b.md", str(tmp_path / "tmp/r.md"), "--confirm-seconds", "0"]
    argv += ["--pr", "7", "--role", str(VERIFIER_MD), "--workspace", str(tmp_path)]
    assert omnigent_agent.main(argv) == 0 and len(og.sessions) == 1
    assert run("rev-parse", "HEAD") == head
    assert og.prs[-1].endswith("#7")  # labelled omni.pr with the PR it reviews (#945)
    # a checkout with tracked changes is refused, never moved under them
    run("checkout", "-q", "--detach", base)
    (tmp_path / "b.md").write_text("edited\n")
    again = [*argv]
    again[5 - 1] = str(tmp_path / "tmp/r3.md")
    assert omnigent_agent.main(again) == 1
    assert run("rev-parse", "HEAD") == base


def test_a_verifier_on_another_host_for_a_pr_needs_a_brief_that_pins_the_prs_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing here runs git on that host, so a brief that names no checkout of the head is refused
    before any session exists; one that names it starts."""
    og = far_verifier(tmp_path, monkeypatch, "")
    (tmp_path / "b.md").write_text("Review PR #7 as the verifier")
    assert omnigent_agent.main([*VERIFY_FAR]) == 1
    assert PR7_HEAD[:12] in capsys.readouterr().err and og.sessions == {}
    (tmp_path / "b.md").write_text(f"Review PR #7: `git worktree add --detach tree {PR7_HEAD}`")
    assert omnigent_agent.main([*VERIFY_FAR]) == 0 and len(og.sessions) == 1


@pytest.mark.parametrize("quiet", [False, True])
def test_a_quiet_waiter_is_not_stalled_while_a_session_its_line_names_progresses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quiet: bool
) -> None:
    gone = {"status": "idle", "updated_at": time.time() - 1300}
    snaps = {OTHER: gone, LEAD: gone} if quiet else {LEAD: gone}  # OTHER, running, progressed
    wait = f"STILL WORKING: waiting on [one](http://h/s/{OTHER}) and {LEAD}. Report: m.md"
    registry, og = manager(tmp_path, wait, **snaps)
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    if quiet:
        assert stalls(og) == ["Supervisor: m t-m: no progress 60m (unknown)"]
        assert f"quiet: {OTHER}, {LEAD}" in og.inbox[-1][1]
    else:
        assert stalls(og) == [] and deadlines(registry) == []


def named_wait_deadlines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replies: list[tuple[float, str]],
    first: str = "",
) -> Leads:
    """A manager quiet on a wait that names one of its leads and then, at each clock offset in
    ``replies``, replying with the wait given there; the ``no progress`` lines it raised."""
    gone = {"status": "idle", "updated_at": time.time() - 1300}
    first = first or f"STILL WORKING: waiting on {LEAD}. Report: m.md"
    snaps = {LEAD: gone, "x": gone, "y": gone}
    registry, og = manager(tmp_path, first, m={"status": "idle"}, **snaps)
    offset = skewed(monkeypatch)
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")  # the first
    for at, wait in replies:
        offset[0] = at
        og.histories["m"].append(
            {**ending(wait, 0), "id": f"reply{at}"}
        )  # a reply at ``at``, new progress
        offset[0] = at + omnigent_agent.DEADLINE_SECONDS + 100  # quiet again
        supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    return og


def test_a_reply_that_restates_its_wait_starts_no_new_deadline_until_four_times_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = omnigent_agent.DEADLINE_SECONDS
    same = f"STILL WORKING: waiting on {LEAD}. Report: m.md"
    og = named_wait_deadlines(tmp_path, monkeypatch, [(100.0, same), (2 * d, same)])
    assert len(stalls(og)) == 1  # once, though the reply was progress
    second = tmp_path / "late"
    second.mkdir()
    og = named_wait_deadlines(second, monkeypatch, [(100.0, same), (5 * d, same)])
    assert len(stalls(og)) == 2  # still stuck after 4x: one more


def test_a_wait_on_a_group_is_keyed_on_its_text_and_fires_once_more_when_stuck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = omnigent_agent.DEADLINE_SECONDS
    group = "STILL WORKING: waiting on my leads. Report: m.md"
    og = named_wait_deadlines(tmp_path, monkeypatch, [(100.0, group), (2 * d, group)], group)
    assert len(stalls(og)) == 1
    second = tmp_path / "late"
    second.mkdir()
    og = named_wait_deadlines(second, monkeypatch, [(100.0, group), (5 * d, group)], group)
    assert len(stalls(og)) == 2


def test_a_changed_wait_starts_a_new_deadline_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = omnigent_agent.DEADLINE_SECONDS
    first = f"STILL WORKING: waiting on {LEAD}. Report: m.md"
    other = "STILL WORKING: waiting on x. Report: m.md"
    og = named_wait_deadlines(
        tmp_path, monkeypatch, [(100.0, first), (2 * d, first), (4 * d, other)]
    )
    assert len(stalls(og)) == 2  # the first wait once, the changed one a new chain


def test_a_wait_on_a_parked_session_raises_no_deadline_but_its_news(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wait = f"STILL WORKING: waiting on {LEAD}. Report: m.md"
    gone = {"status": "idle", "updated_at": time.time() - 1300}
    registry, og = manager(tmp_path, wait, m={"status": "idle"}, **{LEAD: gone})
    og.histories[LEAD] = [ending("PARKED: the host is full. Report: o.md", 1300)]
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    assert stalls(og) == []
    assert any(f"{LEAD} ended PARKED" in t and "your wait still names it" in t for _, t in og.inbox)


@pytest.mark.parametrize(
    "wait, child, tail",
    [
        (f"STILL WORKING: waiting on {OTHER}. Report: m.md", OTHER, "your wait still names it"),
        ("STILL WORKING: waiting on the check. Report: m.md", WORKER, "a session it started"),
    ],
)
def test_a_childs_end_is_news_to_its_lead_once_per_end_not_once_per_lead_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wait: str, child: str, tail: str
) -> None:
    """The news wakes the lead and its reply is a new turn: keyed on that turn, the same end was
    news again at every poll. Keyed on the child's own end, only a new end of it is news."""
    gone = {"status": "idle", "updated_at": time.time() - 1300}
    registry, og = manager(tmp_path, wait, m={"status": "idle"}, **{child: gone})
    og.histories[child] = [ending("DONE: all merged. Report: o.md", 1300)]
    news = lambda: [t for s, t in og.inbox if s == "m" and f"{child} ended DONE" in t and tail in t]
    run = lambda: supervised(og, registry, monkeypatch, 2, notify=COS, console=True, fyi="post")
    run()
    assert len(news()) == 1  # once, not once per poll
    og.histories["m"].append(ending(wait, 0))  # the lead's reply: a new turn, the child unchanged
    skewed(monkeypatch)[0] = 4000.0
    run()
    assert len(news()) == 1
    og.histories[child].append(ending("DONE: merged again. Report: o.md", 0))  # it ended again
    skewed(monkeypatch)[0] = 8000.0
    run()
    assert len(news()) == 2


@pytest.mark.parametrize(
    "wait", ["waiting on the PM", "waiting on [the pm](http://h/s/{P}) now", "waiting on {P}"]
)
def test_a_wait_on_the_session_the_supervisor_notifies_is_acted_on_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wait: str
) -> None:
    pm = "9" * 32
    registry = following(tmp_path)
    og = Lead(told("brief", 600), ending(f"STILL WORKING: {wait.format(P=pm)}. Report: a.md", 400))
    og.snap = {"status": "idle"}
    offset = skewed(monkeypatch)
    supervised(
        og,
        registry,
        monkeypatch,
        3,
        lambda poll: offset.__setitem__(0, 400.0 * poll),
        notify=pm,
        console=True,
    )
    acts = [t for s, t in og.inbox if s == pm and "[act]" in t]
    assert acts and f"waits on its supervisor, session {pm}" in acts[0]


@pytest.mark.parametrize(
    ("b_wait", "b_parent", "who"),
    [
        ("waiting on the PM", "A", "{A} acts"),  # B, started by A, names no session
        ("waiting on {A}", None, "{A} and {B} acts"),  # B names A by id; neither started the other
        ("waiting on {A}", "B", "{B} acts"),
        ("waiting on {A}", "B's registry", "{B} acts"),  # B's leads= registry lists A
    ],
)
def test_two_quiet_sessions_waiting_on_each_other_raise_a_mutual_wait_naming_who_acts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, b_wait: str, b_parent: str | None, who: str
) -> None:
    a, b = "a" * 32, "b" * 32
    registry = follows(tmp_path, a)
    og = Leads(
        **{a: [told("brief", 4000), ending(f"STILL WORKING: waiting on {b}. Report: a.md", 3600)]}
    )
    og.histories[b] = [ending(f"STILL WORKING: {b_wait.format(A=a)}. Report: b.md", 3600)]
    quiet = {"status": "idle", "updated_at": time.time() - 3600}
    og.snaps[a] = {"status": "running", **quiet}
    og.snaps[b] = {**quiet, **({"parent_session_id": a} if b_parent == "A" else {})}
    if b_parent == "B":
        og.snaps[a]["parent_session_id"] = b
    if b_parent == "B's registry":  # the reverse of A's registry listing B: B owns A
        own = tmp_path / "b.registry"
        own.write_text(f"{a}={tmp_path / 'a.md'}\tfollow\n")
        with registry.open("a") as f:
            f.write(f"{b}={tmp_path / 'b.md'}\tleads={own}\n")
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    lines = [t.split("\n")[0] for _, t in og.inbox if t.startswith(f"Supervisor: {a}")]
    assert lines == [f"Supervisor: {a} t-{a}: mutual wait: {a} ↔ {b}; " + who.format(A=a, B=b)]


def test_a_named_session_that_shows_life_is_progress_also_when_it_is_the_notified_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, og = manager(tmp_path, f"STILL WORKING: waiting on {COS}. Report: m.md")
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    assert stalls(og) == [] and deadlines(registry) == []


def test_a_lead_whose_last_message_is_a_report_path_counts_the_sessions_its_report_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, og = manager(tmp_path, f"{tmp_path / 'm.md'}")  # OTHER runs: it progressed
    og.files[str(tmp_path / "m.md")] = f"STILL WORKING: waiting on {OTHER}.\n"
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    assert stalls(og) == [] and deadlines(registry) == []


def test_a_lead_started_by_the_manager_s_registry_that_waits_on_the_pm_is_a_mutual_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet = {"status": "idle", "updated_at": time.time() - 3600}  # top level: no parent id
    registry, og = manager(
        tmp_path, f"STILL WORKING: waiting on {LEAD}. Report: m.md", **{LEAD: quiet}
    )
    og.histories[LEAD] = [ending("STILL WORKING: waiting on the PM. Report: e.md", 3600)]
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    lines = [t.split("\n")[0] for _, t in og.inbox if t.startswith("Supervisor: m t-m:")]
    assert lines == [f"Supervisor: m t-m: mutual wait: m ↔ {LEAD}; m acts"]


def test_an_ended_lead_whose_report_waits_on_the_pm_s_gate_is_acted_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm, report = "9" * 32, str(tmp_path / "a.md")
    registry = following(tmp_path)
    og = Lead(told("brief", 600), ending(report, 400))
    og.files[report] = "STILL WORKING: waiting on the PM's gate and verifier for #1\n"
    og.snap = {"status": "idle"}
    offset = skewed(monkeypatch)
    supervised(
        og,
        registry,
        monkeypatch,
        3,
        lambda poll: offset.__setitem__(0, 400.0 * poll),
        notify=pm,
        console=True,
    )
    acts = [t for s, t in og.inbox if s == pm and "[act]" in t]
    assert acts and f"waits on its supervisor, session {pm}" in acts[0]


def test_call_allows_each_request_the_timeout_it_is_given_else_thirty_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[float] = []

    def urlopen(req: Request, timeout: float) -> io.BytesIO:
        seen.append(timeout)
        return io.BytesIO(b"{}")

    monkeypatch.setattr(omnigent_agent, "urlopen", urlopen)
    og = object.__new__(omnigent_agent.Omnigent)
    og.base, og._token = "http://omnigent.test", "synthetic"
    og.call("GET", "/v1/x")
    og.call("GET", "/v1/x", timeout=180.0)
    assert seen == [30.0, 180.0]


def test_a_claude_child_inherits_its_claude_starter_s_permission_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    modes: list[str | None] = []

    class Starter(FakeOmnigent):
        launch: ClassVar[list[str]] = []
        harness_of_starter = "claude-native"

        def info(self, sid: str) -> dict:
            if sid == "starter":
                return {"harness": self.harness_of_starter, "terminal_launch_args": self.launch}
            return super().info(sid)

        def create(self, agent: str, title: str, model: str | None = None, *rest: object) -> str:
            modes.append(rest[2] if len(rest) > 2 else None)  # permission_mode
            return super().create(agent, title, model, *rest)

    monkeypatch.setenv("OMNIGENT_SESSION_ID", "starter")
    monkeypatch.delenv("OMNIGENT_PERMISSION_MODE", raising=False)
    og = Starter()
    og.harnesses = {"agy": "antigravity-native", "claude-cloud": omnigent_agent.CLOUD_HARNESS}
    bypass = ["--permission-mode", "bypassPermissions"]
    n = 0

    def child(agent: str, starter: list[str], **kw: object) -> tuple[str | None, str]:
        nonlocal n
        n += 1
        Starter.launch = starter
        omnigent_agent.start(og, agent, f"t{n}", "do X", tmp_path / "r.md", None, 0, **kw)
        err = capsys.readouterr().err  # less the mode read back, which every start prints
        return modes[-1], re.sub(r"session \S+: permission mode .*\n", "", err)

    mode, err = child("claude", bypass)
    assert mode == "bypassPermissions" and "bypassPermissions inherited" in err
    assert child("claude", []) == (None, "")  # a starter with no mode: the harness default
    Starter.harness_of_starter = "codex-native"
    assert child("claude", bypass) == (None, "")  # only a Claude starter lends its mode
    Starter.harness_of_starter = "claude-native"
    mode, err = child("claude", bypass, permission_mode="plan")  # the flag wins
    assert mode == "plan" and "inherited" not in err
    monkeypatch.setenv("OMNIGENT_PERMISSION_MODE", "acceptEdits")  # so does the variable
    mode, err = child("claude", bypass)
    assert mode is None and "inherited" not in err  # create reads the variable itself
    monkeypatch.delenv("OMNIGENT_PERMISSION_MODE")
    # Another harness's child never inherits: Codex takes no mode, claude-cloud and agy keep
    # their own opt-in.
    assert child("codex", bypass) == (None, "")
    mode, err = child("claude-cloud", bypass)  # sends none, not the starter's
    assert mode is None and "inherited" not in err and "not applied" not in err
    with pytest.raises(omnigent_agent.Fail, match="a --role child blocks unattended"):
        Starter.launch = bypass
        omnigent_agent.start(og, "agy", "t-agy", "x", tmp_path / "r.md", "worker.md", 0)


def named_pr_argv(host: str, tmp_path: Path) -> list[str]:
    """VERIFY_FAR with ``--pr a/b#7``, for the far host or (``h``) this one."""
    argv = [*VERIFY_FAR]
    argv[argv.index("--pr") + 1] = "a/b#7"
    if host == "h":
        argv[argv.index("mac")] = "h"
        argv[argv.index("/there/repo")] = str(tmp_path)
        argv[4] = str(tmp_path / "tmp/loop/pr7-v1.md")
    return argv


def test_a_named_repository_reaches_the_far_verifiers_head_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    far_verifier(tmp_path, monkeypatch, "")
    seen: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        omnigent_agent,
        "pr_head",
        lambda *a: (seen.append(a), PR7_HEAD)[1],  # type: ignore[func-returns-value]
    )
    assert omnigent_agent.main(named_pr_argv("mac", tmp_path)) == 0
    assert seen == [(7, Path.cwd(), "a/b")]


def test_a_named_repository_reaches_the_verifiers_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    far_verifier(tmp_path, monkeypatch, "")
    seen: list[tuple[object, ...]] = []
    monkeypatch.setattr(omnigent_agent, "check_out_head", lambda *a: seen.append(a))
    assert omnigent_agent.main(named_pr_argv("h", tmp_path)) == 0
    assert seen == [(tmp_path, 7, "a/b")]


def test_a_named_repository_reaches_the_launch_dir_head_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = far_verifier(tmp_path, monkeypatch, "")
    og.harnesses = {"claude-cloud": "acp:claude-cloud"}
    for name in ("pin_environment", "refuse_untrusted_launch_dir"):
        monkeypatch.setattr(omnigent_agent, name, lambda *a: None)
    seen: list[tuple[object, ...]] = []
    monkeypatch.setattr(omnigent_agent, "move_head", lambda *a: seen.append(a))
    argv = ["start", "claude-cloud", "w", "do X", str(tmp_path / "tmp" / "r.md"), "--pr", "a/b#7"]
    assert omnigent_agent.main([*argv, "--workspace", str(tmp_path), "--confirm-seconds", "0"]) == 0
    assert seen == [(tmp_path, 7, "a/b")]


@pytest.mark.parametrize("which", ["pr_head", "pr_branch"])
def test_a_pr_lookup_names_the_repository_it_was_given(
    which: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[list[str]] = []

    def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "x\n", "")

    monkeypatch.setattr(omnigent_agent.subprocess, "run", run)
    getattr(omnigent_agent, which)(7, tmp_path, "a/b")
    getattr(omnigent_agent, which)(7, tmp_path)
    assert ran[0][ran[0].index("--repo") + 1] == "a/b" and "--repo" not in ran[1]


def test_a_far_workspace_never_reads_a_local_checkout_at_the_same_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ws" / ".git").mkdir(parents=True)
    ran: list[Path | None] = []
    monkeypatch.setattr(omnigent_agent, "repo_slug", lambda cwd=None: ran.append(cwd) or "c/d")
    assert omnigent_agent.pr_repo(7, tmp_path / "ws") == "c/d"
    with pytest.raises(omnigent_agent.Fail, match=r"--pr owner/repo#7"):
        omnigent_agent.pr_repo(7, tmp_path / "ws", local=False)
    assert ran == [
        tmp_path / "ws"
    ]  # the far call never asked git, here or in the current directory


def test_a_far_workspace_bare_pr_is_labelled_with_the_origin_read_on_its_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[Path | None] = []
    monkeypatch.setattr(
        omnigent_agent, "repo_slug", lambda cwd=None: ran.append(cwd) or "here/repo"
    )
    monkeypatch.setattr(omnigent_agent, "far_checkout_root", lambda *a: Path("/w"))
    monkeypatch.setattr(
        omnigent_agent,
        "far_origin_slug",
        lambda og, host, root, what: "far/repo" if (host, root) == ("h", Path("/w")) else "bad",
    )
    og = FakeOmnigent()
    assert omnigent_agent.pr_repo(7, "/w", False, (og, "h")) == "far/repo"
    assert ran == []  # the current directory's repository is never read


def test_a_far_workspace_with_no_readable_origin_refuses_a_bare_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(omnigent_agent, "repo_slug", lambda cwd=None: "here/repo")
    monkeypatch.setattr(omnigent_agent, "far_checkout_root", lambda *a: None)
    with pytest.raises(omnigent_agent.Fail, match=r"--pr owner/repo#7"):
        omnigent_agent.pr_repo(7, "/w", False, (FakeOmnigent(), "h"))


def test_a_named_repository_reaches_the_head_reads_inside_the_checkout_and_the_head_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, tuple[object, ...]]] = []

    def stop(name: str):
        def read(*a: object) -> str:
            seen.append((name, a))
            raise omnigent_agent.Fail("stop here")

        return read

    monkeypatch.setattr(omnigent_agent, "pr_head", stop("head"))
    monkeypatch.setattr(omnigent_agent, "pr_branch", stop("branch"))
    with pytest.raises(omnigent_agent.Fail):
        omnigent_agent.check_out_head(tmp_path, 7, "a/b")
    subprocess.run(["git", "init", "-q", str(tmp_path / "launch")], check=True)
    with pytest.raises(omnigent_agent.Fail):
        omnigent_agent.move_head(tmp_path / "launch", 7, "a/b")
    assert seen == [("head", (7, tmp_path, "a/b")), ("branch", (7, tmp_path / "launch", "a/b"))]


def test_start_for_another_host_never_reads_a_local_checkout_at_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ws" / ".git").mkdir(parents=True)
    ran: list[Path | None] = []
    monkeypatch.setattr(omnigent_agent, "repo_slug", lambda cwd=None: ran.append(cwd) or "c/d")
    og = FakeOmnigent()
    with pytest.raises(omnigent_agent.Fail, match=r"--pr owner/repo#7"):
        omnigent_agent.start(
            og, "claude", "t", "do X", tmp_path / "r", None, 0, workspace=str(tmp_path / "ws"),
            host_id="far", pr=7,
        )  # fmt: skip
    assert ran == []  # neither the workspace's path here nor the current directory is read


def test_an_unposted_report_of_a_named_repo_worker_posts_to_that_repo(tmp_path, monkeypatch):
    """Outside a checkout, a worker started with ``--pr a/b#7`` that replies UNPOSTED REPORT gets its
    report posted on a/b's PR #7, never on the cwd's repository."""
    og = FakeOmnigent(reply="UNPOSTED REPORT\nHead: abc\n")
    og.harnesses = {"claude-cloud": "acp:claude-cloud"}
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    calls = []

    def gh(*args, **kw):
        calls.append(args)
        if args[:2] == ("repo", "view"):
            raise omnigent_agent.Fail("gh repo view: fatal: not a git repository")
        if args[0] == "api" and any(str(a).startswith("body=") for a in args):
            return "https://github.com/a/b/pull/7#issuecomment-9\n"
        return "{}"

    monkeypatch.setattr(omnigent_agent, "gh", gh)
    pr = omnigent_agent.pr_ref("a/b#7")
    sid, _ = omnigent_agent.start(og, "claude-cloud", "t", "do X", tmp_path / "r", None, 0, pr=pr)
    omnigent_agent.wait(og, sid, tmp_path / "r.md", pr=pr)
    posts = [c for c in calls if any(str(a).startswith("body=") for a in c)]
    assert posts and posts[0][1] == "repos/a/b/issues/7/comments", calls


def test_reap_merged_takes_an_owner_repo_pr(monkeypatch):
    """``reap --merged --pr a/b#7`` gets past argument parsing (exit 2 is argparse's refusal)."""
    monkeypatch.setattr(
        omnigent_agent, "Omnigent", lambda: (_ for _ in ()).throw(omnigent_agent.Fail("parsed"))
    )
    try:
        code = omnigent_agent.main(["reap", "--merged", "--pr", "a/b#7"])
    except SystemExit as e:
        code = e.code
    assert code != 2


@pytest.mark.parametrize("cmd", ["run", "start"])
def test_a_role_path_that_is_not_a_file_is_refused_before_a_session(
    cmd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("brief")
    missing = str(tmp_path / "references" / "worker.md")
    assert omnigent_agent.main([cmd, "claude", "t", "b.md", "r.md", "--role", missing]) == 1
    assert missing in capsys.readouterr().err
    assert not og.sessions


def test_a_session_the_lead_started_that_ended_is_news_though_its_wait_names_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = {"status": "idle", "updated_at": time.time() - 1300}
    registry, og = manager(
        tmp_path,
        "STILL WORKING: waiting on the check. Report: m.md",
        m={"status": "idle"},
        **{WORKER: gone},
    )
    og.histories[WORKER] = [ending("DONE: checked. Report: w.md", 1300)]
    supervised(og, registry, monkeypatch, 2, notify=COS, console=True, fyi="post")
    told_m = [
        t for s, t in og.inbox if s == "m" and "ended DONE" in t and "a session it started" in t
    ]
    assert len(told_m) == 1 and WORKER in told_m[0]  # once, not once per poll


def test_a_pr_a_lead_waits_on_that_merged_is_news_to_it_from_github_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, og = manager(tmp_path, "STILL WORKING: spec filed\nWaits: #976", m={"status": "idle"})
    reads = []
    monkeypatch.setattr(
        omnigent_agent,
        "pr_settled",
        lambda pr, repo="": reads.append(pr) or (0, f"PR #{pr} merged"),
    )
    supervised(og, registry, monkeypatch, 3, notify=COS, console=True, fyi="post")
    told_m = [t for s, t in og.inbox if s == "m" and "PR #976 merged; your wait names it" in t]
    assert len(told_m) == 1 and reads  # once, however often it is read


@pytest.mark.parametrize("end", [(1, "PR #976 checks red: gate"), (0, "PR #976 checks green (3)")])
def test_a_waited_pr_that_ends_the_same_way_on_a_new_head_is_news_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, end: tuple[int, str]
) -> None:
    """A push is a new head, so the PR's next end is news though its text repeats and no poll saw
    its checks pending; the same head after the lead's reply is not."""
    wait = "STILL WORKING: fix pushed\nWaits: #976"
    registry, og = manager(tmp_path, wait, m={"status": "idle"})
    head = ["a1"]
    monkeypatch.setattr(omnigent_agent, "pr_settled", lambda pr, repo="": (*end, head[0]))
    told = lambda: [t for s, t in og.inbox if s == "m" and end[1] in t]
    run = lambda: supervised(og, registry, monkeypatch, 2, notify=COS, console=True, fyi="post")
    run()
    og.histories["m"].append(ending(wait, 0))  # a new lead turn, the PR unchanged
    skewed(monkeypatch)[0] = 4000.0
    run()
    assert len(told()) == 1
    head[0] = "b2"  # the fix was pushed while the lead waited on something else
    skewed(monkeypatch)[0] = 8000.0
    run()
    assert len(told()) == 2


def test_pr_settled_carries_the_head_gh_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    """The head keys a waited PR's end, so it is asked of GitHub and returned."""
    asked = []

    check = {"name": "gate", "status": "COMPLETED", "conclusion": "FAILURE"}
    inner = rest_like(lambda: {"state": "OPEN", "statusCheckRollup": [check]})

    def gh(*args: str, timeout: float = 0) -> str:
        asked.append(args)
        return inner(*args[:-1], args[-1].replace("pulls/976", "pulls/7"))

    monkeypatch.setattr(omnigent_agent, "gh", gh)
    monkeypatch.setattr(omnigent_agent, "ETAGS", {})
    assert omnigent_agent.pr_settled(976) == (1, "PR #976 checks red: gate", "s")
    assert asked[0][-1].endswith("/pulls/976")  # the pull's head is asked of GitHub and returned


def test_a_started_child_that_fails_again_after_a_revive_is_news_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wait = "STILL WORKING: waiting on the check. Report: m.md"
    registry, og = manager(
        tmp_path, wait, m={"status": "idle"}, **{WORKER: {"status": "failed", "updated_at": 0}}
    )
    og.histories[WORKER] = [ending("STILL WORKING: running tests", 1300)]
    told = lambda: [t for s, t in og.inbox if s == "m" and f"{WORKER} failed" in t]
    run = lambda: supervised(og, registry, monkeypatch, 2, notify=COS, console=True, fyi="post")
    run()
    assert len(told()) == 1
    og.histories["m"].append(ending(wait, 0))  # the lead revives it: a user message to the child
    og.histories[WORKER].append({**said("user", "revive", "u-revive"), "created_at": time.time()})
    og.snaps[WORKER]["status"] = "running"
    skewed(monkeypatch)[0] = 4000.0
    run()
    og.snaps[WORKER]["status"] = "failed"  # it fails again before it writes a message
    skewed(monkeypatch)[0] = 8000.0
    run()
    assert len(told()) == 2


def test_a_session_in_a_wait_set_is_news_once_per_end_not_once_per_lead_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = {"status": "idle", "updated_at": time.time() - 1300}
    wait = f"STILL WORKING: waiting\nWaits: {OTHER}, #976"
    registry, og = manager(tmp_path, wait, m={"status": "idle"}, **{OTHER: gone})
    og.histories[OTHER] = [ending("DONE: merged. Report: o.md", 1300)]
    monkeypatch.setattr(omnigent_agent, "pr_settled", lambda pr, repo="": (0, f"PR #{pr} merged"))
    told = lambda: [t for s, t in og.inbox if s == "m" and t.endswith("; your wait names it")]
    run = lambda: supervised(og, registry, monkeypatch, 2, notify=COS, console=True, fyi="post")
    run()
    assert len(told()) == 1  # every member ended: the set is news once
    og.histories["m"].append(ending(wait, 0))
    skewed(monkeypatch)[0] = 4000.0
    run()
    assert len(told()) == 1
    og.histories[OTHER].append(ending("DONE: merged again. Report: o.md", 0))
    skewed(monkeypatch)[0] = 8000.0
    run()
    assert len(told()) == 2


def test_a_waited_pr_that_merged_is_not_news_again_after_the_leads_next_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, og = manager(tmp_path, "STILL WORKING: spec filed\nWaits: #976", m={"status": "idle"})
    monkeypatch.setattr(omnigent_agent, "pr_settled", lambda pr, repo="": (0, f"PR #{pr} merged"))
    told_m = lambda: [t for s, t in og.inbox if s == "m" and "PR #976 merged; your wait names" in t]
    run = lambda: supervised(og, registry, monkeypatch, 2, notify=COS, console=True, fyi="post")
    run()
    og.histories["m"].append(ending("STILL WORKING: spec filed\nWaits: #976", 0))  # a new turn
    skewed(monkeypatch)[0] = 4000.0
    run()
    assert len(told_m()) == 1


def test_a_set_that_names_ci_is_not_over_when_only_its_pr_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, og = manager(tmp_path, "STILL WORKING: x\nWaits: #976, CI", m={"status": "idle"})
    monkeypatch.setattr(omnigent_agent, "pr_settled", lambda pr, repo="": (0, f"PR #{pr} merged"))
    supervised(og, registry, monkeypatch, 3, notify=COS, console=True, fyi="post")
    assert [t for s, t in og.inbox if s == "m" and "your wait names it" in t] == []


def test_an_open_pr_a_lead_waits_on_costs_one_github_read_a_minute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, og = manager(tmp_path, "STILL WORKING: x\nWaits: #976", m={"status": "idle"})
    calls: list[tuple] = []
    monkeypatch.setattr(omnigent_agent, "ETAGS", {})
    monkeypatch.setattr(omnigent_agent, "gh", pr_rest(calls))
    supervised(og, registry, monkeypatch, 3, notify=COS, console=True, fyi="post")
    assert not [c for c in calls if c[:2] == ("pr", "view")]  # no GraphQL read (#2068)
    waits = [c for c in calls if c[-1].endswith("/pulls/976")]
    assert len(waits) == 1


@pytest.mark.parametrize("labelled", [False, True])
def test_a_bare_pr_wait_is_read_from_the_leads_repo_not_the_supervisors_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, labelled: bool
) -> None:
    """The supervisor can run where no git remote exists, so ``gh pr view N`` there fails: the repo
    comes from the lead's omni.pr label, else its workspace's origin."""
    registry, og = manager(tmp_path, "STILL WORKING: x\nWaits: #976", m={"status": "idle"})
    if labelled:
        og.snaps["m"]["labels"] = {omnigent_agent.PR_LABEL: "o/r#12"}
    else:
        in_checkout(og)
    calls: list[tuple] = []
    inner = pr_rest()

    def gh(*args: str, **_: object) -> str:
        calls.append(args)
        if "{owner}" in args[-1]:
            raise omnigent_agent.Fail("gh api: no git remotes found")
        return inner(*args)

    monkeypatch.setattr(omnigent_agent, "ETAGS", {})
    monkeypatch.setattr(omnigent_agent, "gh", gh)
    supervised(og, registry, monkeypatch, 2, notify=COS, console=True, fyi="post")
    pulls = [c[-1] for c in calls if "/pulls/976" in c[-1]]
    assert pulls and all(p == "repos/o/r/pulls/976" for p in pulls), calls


def bare_pr_wait_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wait: str, og_setup
) -> list[str]:
    """The ``-R`` repo of each ``gh pr view`` the supervisor makes for a lead's wait, two polls."""
    registry, og = manager(tmp_path, f"STILL WORKING: x\nWaits: {wait}", m={"status": "idle"})
    og_setup(og)
    calls: list[str] = []
    monkeypatch.setattr(omnigent_agent, "ETAGS", {})
    monkeypatch.setattr(omnigent_agent, "gh", pr_rest(repo_seen=calls))
    supervised(og, registry, monkeypatch, 2, notify=COS, console=True, fyi="post")
    return calls


def test_a_ref_that_names_its_repo_keeps_it_over_the_leads_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    label = lambda og: og.snaps["m"].update(labels={omnigent_agent.PR_LABEL: "o/r#12"})
    assert set(bare_pr_wait_reads(tmp_path, monkeypatch, "x/y#976", label)) == {"x/y"}


def test_a_failed_workspace_read_is_tried_again_not_taken_for_no_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def flaky(og) -> None:
        in_checkout(og)
        read, failed = og.read_file, []

        def read_file(sid: str, path: str) -> str:
            if not failed:  # the server is restarting: the first read fails
                failed.append(path)
                raise omnigent_agent.Fail("HTTP 502")
            return read(sid, path)

        og.read_file = read_file

    assert bare_pr_wait_reads(tmp_path, monkeypatch, "#976", flaky)[-1] == "o/r"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("STILL WORKING: waiting\nWaits: session " + "a" * 32 + "\nCoS: https://x/1", False),
        (
            "STILL WORKING: waiting\nWaits: session "
            + "a" * 32
            + " (a worker) and a run\nCoS: https://x/1",
            False,
        ),
        ("STILL WORKING: waiting on CoS\nWaits: whatever the worker says", False),
        ("STILL WORKING: spec filed\nCoS: https://x/1", True),
        ("STILL WORKING: waiting\nWaits: supervisor (spec approval)\nCoS: https://x/1", True),
        ("STILL WORKING: waiting\nWaits: supervisor and a run", True),
        ("STILL WORKING: waiting\nWaits: CoS approval of the spec", True),
        ("STILL WORKING: waiting\nWaits: supervisor", True),
    ],
)
def test_a_waits_line_that_does_not_parse_is_no_wait_on_the_supervisor_and_a_cos_footer_is_none_with_it(
    text: str, expected: bool
) -> None:
    assert omnigent_agent.waits_on(text, {}, COS) is expected


def test_an_unparsed_waits_line_naming_the_supervisors_id_is_a_wait_on_it_by_the_named_check() -> (
    None
):
    text = f"STILL WORKING: waiting\nWaits: session {COS} (spec)"
    assert omnigent_agent.waits_on(text, {"named": [COS]}, COS) is True


RUN = "https://github.com/o/r/actions/runs/77"


def test_a_waits_line_that_does_not_parse_is_told_once_with_the_accepted_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, og = manager(
        tmp_path, "STILL WORKING: x\nWaits: my PR and the build", m={"status": "idle"}
    )
    supervised(og, registry, monkeypatch, 3, notify=COS, console=True, fyi="post")
    told = [t for s, t in og.inbox if s == "m" and "did not parse" in t]
    assert len(told) == 1 and "Accepted: supervisor, own supervisor, session <32-hex id>" in told[0]
    assert "GitHub Actions run URL" in told[0] and "my PR and the build" in told[0]


def test_a_waits_line_that_does_not_parse_is_told_again_only_on_the_leads_next_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    wait = "STILL WORKING: x\nWaits: my PR"
    registry, og = manager(tmp_path, wait, m={"status": "idle"})
    told = lambda: capsys.readouterr().out.count("did not parse")
    run = lambda: supervised(og, registry, monkeypatch, 2, notify=COS, console=True, fyi="post")
    run()
    first = told()
    skewed(monkeypatch)[0] = 4000.0
    run()
    assert (first, told()) == (1, 0)  # the same turn, later polls: said once
    og.histories["m"].append(ending(wait, 0))  # the lead tries again, and still gets it wrong
    skewed(monkeypatch)[0] = 8000.0
    run()
    assert told() == 1


def test_a_quiet_waiter_on_a_run_in_progress_is_not_raised_a_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, og = manager(tmp_path, f"STILL WORKING: x. Report: m.md\nWaits: {RUN}")
    monkeypatch.setattr(omnigent_agent, "gh", lambda *a, **k: '{"status":"in_progress"}')
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    assert stalls(og) == []


def test_a_waits_line_that_parses_gets_no_parse_news(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, og = manager(tmp_path, "STILL WORKING: x\nWaits: CI", m={"status": "idle"})
    supervised(og, registry, monkeypatch, 2, notify=COS, console=True, fyi="post")
    assert [t for s, t in og.inbox if s == "m" and "did not parse" in t] == []


def test_a_run_url_is_a_form_wait_on_prints_and_wait_of_names() -> None:
    assert omnigent_agent.wait_line(["run", RUN]) == f"Waits: {RUN}"
    assert omnigent_agent.wait_of(f"STILL WORKING: x\nWaits: {RUN}") == RUN
    with pytest.raises(omnigent_agent.Fail):
        omnigent_agent.wait_line(["run"])


def test_wait_on_refuses_a_bare_number_this_repo_has_no_such_thing_as(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def gh(*args: str, **_: object) -> str:
        if "issues/1357" in args[1]:
            raise omnigent_agent.Fail("gh api: Not Found (HTTP 404)")
        return "{}"

    monkeypatch.setattr(omnigent_agent, "gh", gh)
    with pytest.raises(omnigent_agent.Fail, match=r"owner/repo#1357"):
        omnigent_agent.check_bare_ref("Waits: #1357")
    omnigent_agent.check_bare_ref("Waits: #12")  # exists
    omnigent_agent.check_bare_ref("Waits: o/r#1357")  # names its repo: not checked
    omnigent_agent.check_bare_ref("Waits: CI")


def test_wait_on_lets_a_bare_number_pass_when_github_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def gh(*args: str, **_: object) -> str:
        raise omnigent_agent.Fail("gh api: timed out")

    monkeypatch.setattr(omnigent_agent, "gh", gh)
    omnigent_agent.check_bare_ref("Waits: #1357")


def badref_told(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wait: str,
    pr_error: str,
    issue_error: str | None,
    setup=lambda og: in_checkout(og),
    polls: int = 3,
) -> list[str]:
    """What the lead is told when ``gh pr view`` fails with ``pr_error`` and the issues endpoint
    fails with ``issue_error`` (found when None), over ``polls`` polls a minute apart."""
    registry, og = manager(tmp_path, f"STILL WORKING: x\nWaits: {wait}", m={"status": "idle"})
    setup(og)

    def gh(*args: str, **_: object) -> str:
        if "/pulls/" not in args[-1]:  # the issues endpoint
            if issue_error:
                raise omnigent_agent.Fail(issue_error)
            return "{}"
        raise omnigent_agent.Fail(pr_error)

    monkeypatch.setattr(omnigent_agent, "gh", gh)
    offset = skewed(monkeypatch)
    supervised(
        og,
        registry,
        monkeypatch,
        polls,
        between=lambda poll: offset.__setitem__(0, offset[0] + omnigent_agent.PR_POLL_SECONDS + 1),
        notify=COS,
        console=True,
        fyi="post",
    )
    return [x for x in capsys.readouterr().out.splitlines() if "never ends" in x]  # as said


NO_PR = "gh pr view: Could not resolve to a PullRequest with the number"
NOT_FOUND = "gh api: Not Found (HTTP 404)"


def test_a_rest_404_on_a_bare_ref_is_told_once_like_graphql_could_not_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The supervisor reads a PR through REST (#2068), which says 404 where GraphQL said "Could not resolve"."""
    told = badref_told(capsys, tmp_path, monkeypatch, "#1357", NOT_FOUND, NOT_FOUND)
    assert len(told) == 1 and "#1357 in o/r" in told[0]


def test_a_bare_ref_with_no_such_pr_or_issue_in_the_leads_repo_is_told_once_over_many_polls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    told = badref_told(capsys, tmp_path, monkeypatch, "#1357", NO_PR, NOT_FOUND)
    assert len(told) == 1 and "#1357 in o/r" in told[0] and "owner/repo#1357" in told[0]


@pytest.mark.parametrize(
    ("wait", "pr_error", "issue_error", "setup"),
    [
        ("#12", "gh pr view: HTTP 502", NOT_FOUND, in_checkout),  # unread, not missing
        ("o/r#12", NO_PR, NOT_FOUND, in_checkout),  # it names its repo
        ("#12", NO_PR, None, in_checkout),  # an issue of the lead's repo
        ("#12", NO_PR, "gh api: HTTP 502", in_checkout),  # the issue read failed: not known missing
        ("#12", NO_PR, NOT_FOUND, lambda og: None),  # the lead's repo is not known
    ],
)
def test_a_bare_ref_gets_no_missing_news_unless_the_leads_repo_is_known_to_lack_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    wait: str,
    pr_error: str,
    issue_error,
    setup,
) -> None:
    assert badref_told(capsys, tmp_path, monkeypatch, wait, pr_error, issue_error, setup) == []


def run_waited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wait: str, runs: list[dict], polls: int = 3
) -> list[str]:
    """The news a lead waiting on ``wait`` gets when each GitHub Actions read answers the next of
    ``runs`` (the last one repeats), the polls a minute apart."""
    registry, og = manager(tmp_path, f"STILL WORKING: x\nWaits: {wait}", m={"status": "idle"})
    reads = iter(runs)
    last: list[dict] = [runs[0]]

    def gh(*args: str, **_: object) -> str:
        if args[0] == "run":
            last[0] = next(reads, last[0])
            return json.dumps(last[0])
        return '{"state":"OPEN","statusCheckRollup":[],"headRefOid":"h"}'

    monkeypatch.setattr(omnigent_agent, "gh", gh)
    offset = skewed(monkeypatch)
    supervised(
        og,
        registry,
        monkeypatch,
        polls,
        between=lambda poll: offset.__setitem__(0, offset[0] + omnigent_agent.PR_POLL_SECONDS + 1),
        notify=COS,
        console=True,
        fyi="post",
    )
    return [t for s, t in og.inbox if s == "m" and "your wait names it" in t]


@pytest.mark.parametrize(
    ("conclusion", "good"),
    [
        ("success", True),
        ("skipped", True),
        ("neutral", True),
        ("failure", False),
        ("cancelled", False),
        ("timed_out", False),
    ],
)
def test_an_actions_runs_conclusion_is_good_or_failed(conclusion: str, good: bool) -> None:
    with pytest.MonkeyPatch.context() as mp:
        data = {"status": "completed", "conclusion": conclusion, "attempt": 1}
        mp.setattr(omnigent_agent, "gh", lambda *a, **k: json.dumps(data))
        assert omnigent_agent.run_settled(77, "o/r")[0] == (0 if good else 1)


def test_a_failed_run_in_a_set_wakes_the_lead_at_once_and_a_good_one_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    done = {"status": "completed", "attempt": 1}
    failed = run_waited(tmp_path, monkeypatch, f"{RUN}, CI", [{**done, "conclusion": "failure"}])
    assert len(failed) == 1 and "run o/r#77 failure" in failed[0]  # CI still pending: news anyway
    (tmp_path / "ok").mkdir()
    ok = run_waited(tmp_path / "ok", monkeypatch, f"{RUN}, CI", [{**done, "conclusion": "success"}])
    assert ok == []  # a good end waits for the rest of the set


def test_a_run_that_ends_is_news_once_over_many_polls_and_again_when_it_is_rerun_and_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    f1 = {"status": "completed", "conclusion": "failure", "attempt": 1}
    ends = lambda: set(re.findall(r"wait:m:fail:\w+", capsys.readouterr().out))
    run_waited(tmp_path, monkeypatch, RUN, [f1], polls=4)
    assert len(ends()) == 1  # many polls, one end
    (tmp_path / "again").mkdir()
    run_waited(tmp_path / "again", monkeypatch, RUN, [f1, {**f1, "attempt": 2}], polls=4)
    assert len(ends()) == 2  # the same run id, a new attempt: a new end


def test_wait_on_through_main_refuses_a_bare_number_this_repo_does_not_have(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def gh(*args: str, **_: object) -> str:
        raise omnigent_agent.Fail(NOT_FOUND)

    monkeypatch.setattr(omnigent_agent, "gh", gh)
    assert omnigent_agent.main(["wait-on", "pr", "1357"]) != 0
    assert "owner/repo#1357" in capsys.readouterr().err
    monkeypatch.setattr(omnigent_agent, "gh", lambda *a, **k: "{}")
    assert omnigent_agent.main(["wait-on", "pr", "12"]) == 0
    assert capsys.readouterr().out.strip() == "Waits: #12"


def told_idle(og) -> list[str]:
    return [t for s, t in og.inbox if s == "m" and "went idle" in t]


def test_a_named_session_idle_with_no_ending_word_is_news_to_its_waiter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a verifier of another harness, or a cloud run, whose answer is its report: no DONE line
    gone = {"status": "idle", "updated_at": time.time() - 1300}
    registry, og = manager(
        tmp_path,
        f"STILL WORKING: waiting on session {WORKER}",
        m={"status": "idle"},
        **{WORKER: gone},
    )
    og.histories[WORKER] = [ending("Verdict written. SATISFIED: yes", 100)]
    supervised(og, registry, monkeypatch, 2, notify=COS, console=True, fyi="post")
    assert len(told_idle(og)) == 1 and WORKER in told_idle(og)[0]


def test_a_session_the_lead_started_that_went_idle_after_its_turn_is_news_but_an_old_one_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = {"status": "idle", "updated_at": time.time() - 1300}
    for age, expect in ((100, 1), (2000, 0)):  # the lead's turn was 600 s ago
        (tmp_path / str(age)).mkdir()
        registry, og = manager(
            tmp_path / str(age),
            "STILL WORKING: waiting on the check. Report: m.md",
            m={"status": "idle"},
            **{WORKER: gone},
        )
        og.histories["m"] = [ending("STILL WORKING: waiting on the check. Report: m.md", 600)]
        og.histories[WORKER] = [ending("Pass 4 written. SATISFIED: yes", age)]
        supervised(og, registry, monkeypatch, 2, notify=COS, console=True, fyi="post")
        assert len(told_idle(og)) == expect


def test_a_session_that_ended_on_a_still_working_turn_or_was_just_prompted_is_not_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = {"status": "idle", "updated_at": time.time() - 1300}
    for n, history in enumerate(
        (
            [ending("STILL WORKING: compiling", 100)],  # idle between turns, not finished
            [ending("Pass 4 written.", 100), said("user", "now pass 5", "u1")],  # prompted since
            # prompted, then tool calls only: the interrupted turn has no answer yet
            [
                ending("Pass 4 written.", 100),
                said("user", "now pass 5", "u1"),
                {"id": "t1", "type": "function_call"},
            ],
        )
    ):
        (tmp_path / str(n)).mkdir()
        registry, og = manager(
            tmp_path / str(n),
            f"STILL WORKING: waiting on session {WORKER}",
            m={"status": "idle"},
            **{WORKER: gone},
        )
        og.histories[WORKER] = history
        supervised(og, registry, monkeypatch, 2, notify=COS, console=True, fyi="post")
        assert told_idle(og) == []


def test_a_failed_github_read_is_asked_again_only_after_a_minute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, og = manager(tmp_path, "STILL WORKING: x\nWaits: #976", m={"status": "idle"})
    calls: list[tuple] = []

    def down(*a, **k):
        calls.append(a)
        raise omnigent_agent.Fail("gh: HTTP 502")

    monkeypatch.setattr(omnigent_agent, "gh", down)
    supervised(og, registry, monkeypatch, 6, notify=COS, console=True, fyi="post")
    assert len(calls) <= 2  # one for the news read, at most one for the moving check


def test_a_wait_on_a_pr_whose_github_read_failed_is_not_moving_for_that_minute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = {"status": "idle", "updated_at": time.time() - 1300}
    registry, og = manager(tmp_path, "STILL WORKING: x\nWaits: #976", m=gone)

    def down(*a, **k):
        raise omnigent_agent.Fail("gh: HTTP 502")

    monkeypatch.setattr(omnigent_agent, "gh", down)
    supervised(og, registry, monkeypatch, 3, notify=COS, console=True, fyi="post")
    assert stalls(og)  # unread is not "open and pending": the quiet lead is still flagged


def test_a_childs_news_carries_the_first_line_of_its_last_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a worker that put its report in its final message, not in the report file
    gone = {"status": "idle", "updated_at": time.time() - 1300}
    registry, og = manager(
        tmp_path,
        f"STILL WORKING: waiting on session {WORKER}",
        m={"status": "idle"},
        **{WORKER: gone},
    )
    og.histories[WORKER] = [ending("Report: all 12 checks pass, nothing to fix.", 100)]
    supervised(og, registry, monkeypatch, 2, notify=COS, console=True, fyi="post")
    assert "all 12 checks pass" in told_idle(og)[0]


def test_start_refuses_a_model_a_newer_generation_supersedes(tmp_path: Path) -> None:
    """6.2 lists only sol and astra, so a mid-tier 6.1 terra is older than any of them; a pin may stay."""
    og = FakeOmnigent()
    og.listed = ["gpt-6.2-sol", "gpt-6.2-astra", "gpt-6.1-terra", "gpt-6-luna", "sonnet"]
    run = lambda m, **k: omnigent_agent.start(
        og, "codex", "t", "do X", tmp_path / "r.md", None, 0, model=m, **k
    )
    with pytest.raises(
        omnigent_agent.Fail,
        match="gpt-6.1-terra is an older generation.*gpt-6.2-sol, gpt-6.2-astra",
    ):
        run("gpt-6.1-terra")
    assert not og.sessions
    run("gpt-6.2-sol")
    run("gpt-6.1-terra", allow_older_model=True)
    assert omnigent_agent.newer_generation("sonnet", og.listed) == []
    assert omnigent_agent.newer_generation("gpt-6.2-sol", og.listed) == []
    assert omnigent_agent.newer_generation("gpt-6-luna", og.listed) == [
        "gpt-6.2-sol",
        "gpt-6.2-astra",
    ]  # only the newest generation: a hint from 6.1 would be refused again


@pytest.mark.parametrize(("flags", "code"), [([], 1), (["--allow-older-model"], 0)])
def test_start_cli_passes_allow_older_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    code: int,
) -> None:
    """The flag reaches start(): refused without it, a session with it."""
    og = FakeOmnigent()
    og.listed = ["gpt-6.2-sol", "gpt-6.2-astra", "gpt-6.1-terra"]
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "brief.md").write_text("do X")
    argv = ["start", "codex", "t", "brief.md", "r.md", "--confirm-seconds", "0"]
    assert omnigent_agent.main([*argv, "--model", "gpt-6.1-terra", *flags]) == code
    if code:
        assert "gpt-6.2-sol, gpt-6.2-astra" in capsys.readouterr().err
        assert not og.sessions
    else:
        assert og.sessions


@pytest.mark.parametrize(
    ("end", "deadline"),
    [
        ((0, "PR #976 checks green (3)", "a1"), False),  # open, checks settled: still live
        ((1, "PR #976 checks red: gate", "a1"), False),
        ((0, "PR #976 merged", "a1"), True),  # ended: news, so no longer a live wait
        (None, False),
    ],
)
def test_an_open_pr_a_lead_waits_on_is_live_after_its_checks_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, end: tuple | None, deadline: bool
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 4000), ending("STILL WORKING: queued\nWaits: #976", 3600))
    og.snap = {"status": "running"}
    monkeypatch.setattr(omnigent_agent, "pr_settled", lambda pr, repo="": end)
    supervised(og, registry, monkeypatch, 1, notify="cos", console=True, fyi="post")
    assert any("no progress" in p for p in fyis(og)) is deadline


def test_a_cloud_child_gets_its_role_file_s_text_and_a_host_child_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The VM holds none of this host's files, so a path to the role file reads nothing there."""
    monkeypatch.delenv("LOOP_DENY_LIST", raising=False)
    role = tmp_path / "worker.md"
    role.write_text("# Worker\nRULE-X\n")
    cloud = CloudFake()
    sid, _ = omnigent_agent.start(
        cloud, "claude-cloud", "t", "do X", tmp_path / "r.md", str(role), 0
    )
    text = omnigent_agent.text_of(cloud.sessions[sid][1])
    assert "<role-file>\n# Worker\nRULE-X\n</role-file>" in text and "Your role file is" not in text
    assert "other files it names may not be in this checkout" in text
    host = FakeOmnigent()
    omnigent_agent.start(host, "claude", "h", "do X", tmp_path / "h.md", str(role), 0)
    brief = omnigent_agent.brief_of(tmp_path / "h.md").read_text()
    assert f"Your role file is {role}; read it first." in brief and "RULE-X" not in brief


@pytest.mark.parametrize("pr", [None, 7])
@pytest.mark.parametrize("top_level", [False, True])
@pytest.mark.parametrize("cloud", [False, True])
@pytest.mark.parametrize("as_file", [False, True])
def test_brief_prints_the_body_start_sends_a_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cloud: bool,
    top_level: bool,
    pr: int | None,
    as_file: bool,
) -> None:
    """A cloud lead reaches Omnigent only through its MCP tools, so it composes a child's message with
    `brief` and passes it to sys_session_create: it prints, byte for byte and reading no server, the
    body `start` sends under its signature, lock and title lines (#1509)."""
    monkeypatch.delenv("LOOP_DENY_LIST", raising=False)
    monkeypatch.chdir(tmp_path)
    role = tmp_path / "worker.md"
    role.write_text("# Worker\nrule\n")
    text = "do X\nthen Y\n"
    brief = tmp_path / "brief.md"
    brief.write_text(text)
    report = tmp_path / "r.md"
    og = CloudFake() if cloud else FakeOmnigent()
    agent = "claude-cloud" if cloud else "claude"
    sid, _ = omnigent_agent.start(
        og, agent, "t", text, report, str(role), 0, top_level=top_level, pr=pr
    )
    sent = omnigent_agent.text_of(og.sessions[sid][1])
    inline = sent.partition("Your brief:\n\n")[2]
    full = inline or omnigent_agent.brief_of(report).read_text()
    body = re.sub(r"\A(?:AGENT_SIGNATURE|LOOP_HEAVY_LOCK)=.*\n", "", full)
    body = re.sub(r"\A(?:AGENT_SIGNATURE|LOOP_HEAVY_LOCK)=.*\n", "", body)
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda *a: pytest.fail("brief read a server"))
    argv = ["brief", str(brief) if as_file else text, "r.md", "--role", str(role)]
    argv += ["--cloud"] * cloud + ["--top-level"] * top_level + ["--pr", str(pr)] * bool(pr)
    assert omnigent_agent.main(argv) == 0
    printed = capsys.readouterr().out
    assert printed == body and printed.endswith(text)
    assert ("<role-file>\n# Worker\nrule\n</role-file>" in printed) == cloud


@pytest.mark.parametrize(
    ("role", "agent"),
    [("verifier.md", "claude"), ("verifier.md", "claude-cloud"), ("worker.md", "claude-cloud")],
)
def test_brief_prints_the_body_the_start_command_sends_with_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    role_file: Callable[[str], str],
    role: str,
    agent: str,
) -> None:
    """Through the CLI, as a lead runs both: a host verifier's --pr names the PR it reviews, so
    neither command tells it to report on that PR; a cloud child's --pr is its report channel (#1509)."""
    monkeypatch.delenv("LOOP_DENY_LIST", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.md").write_text("review\n")
    cloud = agent == "claude-cloud"
    og = CloudFake() if cloud else FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda *a: og)
    for name in ("pin_environment", "refuse_untrusted_launch_dir"):  # this host's Claude setup
        monkeypatch.setattr(omnigent_agent, name, lambda *a: None)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(tmp_path)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "pr 7"], check=True)
    head = subprocess.run(
        [*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout
    monkeypatch.setattr(omnigent_agent, "pr_head", lambda *a: head.strip())  # the checkout is at it
    argv = ["b.md", "r.md", "--role", role_file(role), "--pr", "7"]
    assert omnigent_agent.main(["start", agent, "t", *argv, "--same-family"]) == 0
    sid = next(iter(og.sessions))
    sent = omnigent_agent.text_of(og.sessions[sid][1])
    full = (
        sent.partition("Your brief:\n\n")[2]
        or omnigent_agent.brief_of(tmp_path / "r.md").read_text()
    )
    body = re.sub(r"\A(?:(?:AGENT_SIGNATURE|LOOP_HEAVY_LOCK)=.*\n)+", "", full)
    capsys.readouterr()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda *a: pytest.fail("brief read a server"))
    assert omnigent_agent.main(["brief", *argv, *["--cloud"] * cloud]) == 0
    assert capsys.readouterr().out == body
    assert ("PR #7" in body) == cloud


def test_brief_keeps_another_hosts_report_path_as_given(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A path on the target host means nothing here, so --remote keeps it unresolved, as start does (#1509)."""
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda *a: pytest.fail("brief read a server"))
    far = "/there/repo/../repo/tmp/loop/r.md"
    for remote, shown in ((True, far), (False, str(Path(far).resolve()))):
        argv = ["brief", "do X", far, "--top-level"] + ["--remote"] * remote
        assert omnigent_agent.main(argv) == 0
        assert shown in capsys.readouterr().out


def test_brief_refuses_an_unreadable_role_and_takes_a_long_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`brief` checks --role as start does, with a one-line error, and takes an inline brief too
    long for a file name as text, even where Path.is_file raises on it as on Python 3.12 (#1509)."""
    assert omnigent_agent.main(["brief", "do X", "r.md", "--cloud", "--role", "gone.md"]) == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "--role gone.md: not a readable file" in err
    long = "x" * 5000
    real = Path.is_file

    def raises_on_long(self: Path) -> bool:
        if len(self.name) > 255:
            raise OSError(errno.ENAMETOOLONG, "File name too long", str(self))
        return real(self)

    monkeypatch.setattr(Path, "is_file", raises_on_long)
    assert omnigent_agent.main(["brief", long, "r.md", "--cloud"]) == 0
    assert capsys.readouterr().out.endswith(long)


def test_prune_demo_branch_deletes_the_pr_s_media_branch_and_ignores_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_gh(*args: str, **kw: object) -> str:
        calls.append(args)
        if len(calls) == 2:
            raise omnigent_agent.Fail("HTTP 404")
        return ""

    monkeypatch.setattr(omnigent_agent, "gh", fake_gh)
    omnigent_agent.prune_demo_branch("o/r", 7)
    omnigent_agent.prune_demo_branch("o/r", 8)  # already gone: no error
    assert calls[0] == ("api", "-X", "DELETE", "repos/o/r/git/refs/heads/claude/ready-demo-7")


def test_reap_prunes_the_demo_branch_of_a_worker_whose_pr_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    og = FakeOmnigent()
    sid = og.create("claude-cloud", "w")
    og.labels[sid] = {omnigent_agent.PR_LABEL: "o/r#7"}
    calls: list[tuple[str, ...]] = []

    def fake_gh(*args: str, **kw: object) -> str:
        calls.append(args)
        return '{"state": "CLOSED"}'

    monkeypatch.setattr(omnigent_agent, "gh", fake_gh)
    monkeypatch.setattr(omnigent_agent, "EVENTS", None)
    omnigent_agent.reap(og, [sid])
    assert ("api", "-X", "DELETE", "repos/o/r/git/refs/heads/claude/ready-demo-7") in calls


@pytest.mark.parametrize(("top_level", "pr"), [(True, None), (False, None), (True, 7), (False, 7)])
def test_every_cloud_preamble_says_a_helper_is_a_native_subagent(
    top_level: bool, pr: int | None
) -> None:
    """A created child of a cloud session lands on the bridge host, which runs no loop work (#1659)."""
    rule = (
        "Inside a claude-cloud session, a helper for its own work (a check, a gate, a test run) is a native"
        " subagent in the VM, never a session made with `sys_session_create`: a created child lands on the"
        " bridge host, which runs no loop work. A worker or verifier the brief asks for is a separate agent,"
        " not a helper, and starts as `brief` describes."
    )
    assert rule in omnigent_agent.first_message(True, top_level, pr, "r.md", None, "do X")
    assert "sys_session_create`: a created child" not in omnigent_agent.first_message(
        False, top_level, None, "r.md", None, "do X"
    )


def test_a_report_on_another_host_is_its_path_there_with_or_without_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One rule for a start's report on another host, ``--pr`` or not: a relative one is refused,
    and an absolute one is that host's path, never created or resolved here."""
    og = far_host(monkeypatch, tmp_path, "cc-experiments", meminfo=mem(9000), loadavg="0 0 0")
    og.procs.setdefault("far", {})["/there/repo/.claude/settings.local.json"] = PINNED
    there = tmp_path / "there-home" / "r.md"  # a folder this host must not make
    for pr in ([], ["--pr", "o/r#7"]):
        argv = [*far_start("claude-cloud", "cc-experiments"), "--workspace", "/there/repo", *pr]
        argv[4] = "tmp/loop/r.md"
        assert omnigent_agent.main(argv) == 1, pr
        assert "is another host" in capsys.readouterr().err
        argv[4] = str(there)
        assert omnigent_agent.main(argv) == 0, pr
        assert not there.parent.exists(), pr
        og.archived = list(og.sessions)


@pytest.mark.parametrize("role", ["lead", "worker", "verifier"])
def test_role_name_resolves_to_the_pack_role_file_with_no_file_in_the_launch_dir(
    role: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--role verifier` names the pack's role file, so a cloud child's launch dir needs none (#1868)."""
    monkeypatch.chdir(tmp_path)
    found = Path(omnigent_agent.resolve_role(role))
    assert (
        found.is_file() and found.name == f"{role}.md" and found.parent.name == "engineering-loop"
    )
    assert omnigent_agent.main(["brief", "do X", "r.md", "--cloud", "--role", role]) == 0
    assert f"<role-file>\n{found.read_text().rstrip()}" in capsys.readouterr().out
    # a path, or a name that is no role, is left as given
    assert omnigent_agent.resolve_role("x/verifier.md") == "x/verifier.md"
    assert omnigent_agent.resolve_role("other") == "other"


def acts_of(og: Fleet, to: str) -> list[str]:
    return [t for s, t in og.inbox if s == to and "[act]" in t]


def test_a_turn_end_that_only_shows_progress_or_waiting_is_never_act_2090(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = following(tmp_path)
    og = Lead(
        told("brief", 900), ending("STILL WORKING: waiting on the verifier. Report: a.md", 400)
    )
    og.snap = {"status": "idle"}
    supervised(og, registry, monkeypatch, 2, notify=COS, console=True)
    assert acts_of(og, COS) == []


def lead_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sent: str | None, archived: bool = False
) -> Lead:
    """A followed lead that ends ``DONE``, after sending ``sent`` (if any) to the notified session."""
    tmp_path = tmp_path / ("sent" if sent else "archived" if archived else "none")
    tmp_path.mkdir(exist_ok=True)
    registry = following(tmp_path)
    call = {
        "type": "function_call",
        "name": "sys_session_send",
        "arguments": {"session_id": COS, "text": sent},
    }
    items = [told("brief", 900), ending("working", 600), *([call] if sent else [])]
    og = Lead(*items, ending("DONE: merged #5. Report: a.md", 400))
    og.snap = {"status": "idle", "archived": archived}
    supervised(og, registry, monkeypatch, 2, notify=COS, console=True)
    return og


def test_a_result_the_lead_already_sent_to_the_notified_session_gets_no_act_line_2090(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert acts_of(lead_done(tmp_path, monkeypatch, None), COS)  # the control: it is told
    assert acts_of(lead_done(tmp_path, monkeypatch, "DONE: merged #5. Report: a.md"), COS) == []


def test_a_done_of_a_session_the_notified_session_already_archived_gets_no_act_line_2090(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert acts_of(lead_done(tmp_path, monkeypatch, None, archived=True), COS) == []


def test_a_followed_sessions_leads_registry_makes_its_bare_done_no_act_line_2309(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``leads=`` field of a registry line reaches ``route`` as ``coordinator``: a manager's bare ``DONE`` raises
    nothing, the same turn from a followed lead with no registry is told."""
    text = "Nothing to do on #667. It isn't my PR.\n\nDONE"

    def run(sub: str, extra: str) -> list[str]:
        base = tmp_path / sub
        base.mkdir()
        (base / "leads.registry").write_text("")
        registry = base / "registry"
        registry.write_text(f"a={base / 'a.md'}\t{base / 'a.md.brief'}\tfollow{extra}\n")
        og = Lead(told("brief", 900), ending("working", 600), ending(text, 400))
        og.snap = {"status": "idle"}
        supervised(og, registry, monkeypatch, 2, notify=COS, console=True)
        return acts_of(og, COS)

    assert run("lead", "")  # the control: a lead's bare DONE is told
    assert run("manager", f"\tleads={tmp_path / 'manager' / 'leads.registry'}") == []


def test_a_managers_prose_result_over_a_bare_done_still_tells_the_lead_waiting_on_it_2309(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Option (a) of #2309: a manager's ``PR #5 merged as abc1234.`` above a bare ``DONE`` wakes no notified session, and
    the lead registered ``after=`` it is told; the manager's ``leads=`` field is read from the real registry line."""
    leads = tmp_path / "leads.registry"
    leads.write_text("")
    registry = tmp_path / "registry"
    registry.write_text(
        f"m={tmp_path / 'm.md'}\tfollow\tleads={leads}\nb={tmp_path / 'b.md'}\tfollow\tafter=m\n"
    )
    og = Leads(
        m=[told("brief", 60)],
        b=[told("brief", 60), ending("STILL WORKING: waiting on lead m", 5)],
    )

    def done(poll: int) -> None:
        if poll == 1:
            og.histories["m"].append(ending("PR #5 merged as abc1234.\n\nDONE", 0))

    supervised(og, registry, monkeypatch, 3, done, notify="cos", console=True)
    assert acts_of(og, "cos") == [] and posted(og) == []
    assert [(s, t) for s, t in og.inbox if s == "b" and "the lead you wait on, ended" in t]
    assert "m" in state_of(registry)["leads"]  # still followed


def test_a_send_that_is_not_the_result_leaves_the_done_an_act_line_2090(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert acts_of(lead_done(tmp_path, monkeypatch, "STILL WORKING: gate started"), COS)


def test_the_quota_line_is_said_once_until_the_quota_resets_2090(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "registry"
    registry.write_text("".join(f"{s}={tmp_path / s}.md\tfollow\n" for s in ("a", "b")))
    quota = lambda ago: ending("STILL WORKING: waiting on quota. Report: a.md", ago)
    og = Leads(a=[told("brief", 5000), quota(1000)], b=[told("brief", 5000), quota(800)])
    og.snaps = {"a": {"status": "idle"}, "b": {"status": "idle"}}
    monkeypatch.setattr(
        omnigent_agent,
        "default_head",
        lambda repo: (_ for _ in ()).throw(omnigent_agent.RateLimited("limit", 3000.0)),
    )
    monkeypatch.setattr(omnigent_agent, "workspace_repo", lambda og, sid: "o/r")
    offset = skewed(
        monkeypatch
    )  # the limit is read in the first poll; a's deadline passes in a later one, b's after
    supervised(
        og,
        registry,
        monkeypatch,
        5,
        lambda poll: offset.__setitem__(0, 250.0 * poll),
        notify=COS,
        console=True,
        fyi="post",
    )
    quiet = [
        t
        for t in [t for s, t in og.inbox if s == COS]
        if t.startswith("Supervisor: ") and "no progress" in t and "(quota" in t
    ]
    assert len(quiet) == 1


def test_quota_line_due_counts_one_line_per_reset_2090() -> None:
    state: dict = {}
    due = omnigent_agent.quota_line_due
    assert due(state, 1000.0, 600.0)
    assert not due(state, 1100.0, 500.0)
    assert due(state, 1601.0, 0.0)
    assert due(state, 1700.0, 900.0)
    assert not due(state, 1800.0, 800.0)


def test_several_waits_on_one_pr_read_it_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart finds every lead's wait at once; M waits naming one PR make one read of it, not M (#2068)."""
    sids = ("a", "b", "c")
    registry = follows(tmp_path, *sids)
    og = Leads(
        **{s: [told("brief", 4000), ending("STILL WORKING: x\nWaits: #976", 3600)] for s in sids}
    )
    for sid in sids:
        og.snaps[sid] = {"status": "idle", "updated_at": time.time() - 60}
    calls: list[tuple] = []
    monkeypatch.setattr(omnigent_agent, "ETAGS", {})
    monkeypatch.setattr(omnigent_agent, "gh", pr_rest(calls))
    supervised(og, registry, monkeypatch, 1, notify=COS, console=True, fyi="post")
    assert len([c for c in calls if c[-1].endswith("/pulls/976")]) == 1


def test_send_unarchives_a_registry_listed_session_and_refuses_an_unlisted_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent(reply="https://github.com/o/r/pull/7#issuecomment-42")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    fake_gh(monkeypatch, {"42": on_pr(7, "done")})
    monkeypatch.chdir(tmp_path)
    (tmp_path / "fix.md").write_text("fix it")
    listed, unlisted, forced = (og.create("claude", t) for t in ("a", "b", "c"))
    og.archived += [listed, unlisted, forced]
    registry = tmp_path / "leads.registry"
    registry.write_text(f"{listed}=old.md\n")
    send = [
        "send",
        "fix.md",
        "tmp/loop/r.md",
        "--pr",
        "7",
        "--registry",
        str(registry),
        "--confirm-seconds",
        "0",
    ]
    assert omnigent_agent.main(["send", listed, *send[1:]]) == 0
    assert listed not in og.archived
    assert any("fix it" in omnigent_agent.text_of(i) for i in og.sessions[listed])
    assert omnigent_agent.main(["send", unlisted, *send[1:]]) == omnigent_agent.GONE
    assert unlisted in og.archived and f"session {unlisted} is archived" in capsys.readouterr().err
    assert (
        omnigent_agent.main(["send", forced, "fix.md", "tmp/loop/c.md", *send[3:], "--unarchive"])
        == 0
    )
    assert forced not in og.archived
    # the registry named by the environment lists a session as well as --registry
    via_env = og.create("claude", "d")
    og.archived.append(via_env)
    registry.write_text(f"{via_env}=old.md\n")
    monkeypatch.setenv("OMNIGENT_SUPERVISE_REGISTRY", str(registry))
    assert (
        omnigent_agent.main(
            ["send", via_env, "fix.md", "tmp/loop/d.md", "--pr", "7", "--confirm-seconds", "0"]
        )
        == 0
    )
    assert via_env not in og.archived
