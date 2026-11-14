#!/usr/bin/env python3
"""ACP bridge: one Omnigent session drives one Claude Code cloud session.

The agent runs on Anthropic's infrastructure (claude.ai/code), not on this
machine. The bridge is a stdio Agent Client Protocol server, so Omnigent runs
it as a plain ``acp:<slug>`` agent:

* the first ``session/prompt`` creates the cloud session with
  ``claude --cloud "<prompt>"`` on a pseudo-terminal from the session's cwd
  (the cloud VM clones that checkout's GitHub remote and branch);
* later prompts are posted to ``POST /v1/code/sessions/{id}/events`` and
  ``session/cancel`` posts an ``interrupt`` control request there, the protocol
  the CLI's own interactive client speaks, so any process can re-attach;
* nothing else stops the cloud session: a bridge that is terminated, or loses
  the network, leaves the turn running, and the next bridge mirrors what it
  missed before it sends anything;
* the transcript is read back from ``GET /v1/code/sessions/{id}/events``,
  and mirrored as ACP ``session/update`` notifications until the turn's
  ``result`` event. All calls use the Claude CLI's own claude.ai login;
* the first answer starts with a ``cloud session: <url>`` line. A bridge that
  starts on a transcript Omnigent replays (after a restart) finds that line and
  re-attaches to the same cloud session, unarchiving it when needed.

Standard library only. Run ``python3 claude_cloud_acp.py --self-check`` to
exercise the event mapping offline.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import http.client
import io
import itertools
import json
import os
import pty
import pwd
import queue
import re
import runpy
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path

API_BASE = os.environ.get("CLAUDE_CLOUD_API_BASE", "https://api.anthropic.com")
CLAUDE_CLI = os.environ.get("CLAUDE_CLOUD_CLI", "claude")
# The stream sends a keepalive every ~12 s; reconnect when it goes quiet for longer.
STREAM_IDLE_S = 60
# A dropped stream reconnects after 1 s, doubling up to this while the failures last.
RECONNECT_MAX_S = 30
# A turn waits as long as its cloud session shows life; this long without any, while
# the session can be reached, fails it.
STALL_TIMEOUT_S = float(os.environ.get("CLAUDE_CLOUD_STALL_TIMEOUT_S", "1800"))
# One poll of a waiting turn counts this much at most: a longer step is this host
# asleep, not the cloud session quiet.
STALL_STEP_S = 2.0
# Quiet seconds after the cloud started a new CLI mid-turn before the turn counts as cut off.
CUT_OFF_GRACE_S = 30
# What the bridge posts to a session whose turn its sandbox cut off.
CONTINUE_PROMPT = (
    "The sandbox restarted and cut your last turn off before it finished. Tool calls that "
    "were running may not have completed. Check what is done, then continue the task."
)
# Omnigent ends a turn after HARNESS_ACP_PROMPT_TIMEOUT_S (300 s) with no message,
# and one cloud tool call can run longer, so a waiting turn sends an empty update.
KEEPALIVE_S = 60
# Omnigent's agent env allowlist drops USER, but the CLI's keychain lookup keys on it.
os.environ.setdefault("USER", pwd.getpwuid(os.getuid()).pw_name)
CLI_TIMEOUT_S = 300
# The CLI refreshes its token from 5 minutes before expiry.
REFRESH_WINDOW_S = 300
REFRESH_EVERY_S = 60
# ponytail: unverified that this subcommand refreshes; without it the next call fails clearly.
REFRESH_CMD = [CLAUDE_CLI, "agents", "--json"]
_last_refresh = -float(REFRESH_EVERY_S)
PROTOCOL_VERSION = 1
SESSION_ID_RE = re.compile(r"\b((?:session|cse)_[A-Za-z0-9]{10,})")
MARKER = "cloud session: https://claude.ai/code/"
MARKER_RE = re.compile(re.escape(MARKER) + r"((?:session|cse)_[A-Za-z0-9]{10,})")
# ACP session -> cloud session, for an Omnigent that re-prompts a respawned
# bridge with the old ACP session id instead of opening a new one; first
# prompt -> cloud session, for one that opens a new ACP session after a failed turn;
# and cloud session -> the last ``result`` mirrored to Omnigent (``seen:<id>``).
STATE_FILE = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / (
    "claude-cloud-acp/sessions.json"
)
# The head and tail of the transcript Omnigent's ACP executor replays into the
# first prompt of a fresh session (``AcpExecutor._history_prefix``).
REPLAY_HEAD = "Conversation so far:\nuser: "
REPLAY_TAIL = (
    "Respond to the latest user message, using the conversation above as context.\n\nuser: "
)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")

_write_lock = threading.Lock()
_last_sent = 0.0
# Replies to the requests this bridge sends Omnigent, by request id.
_replies: dict[str, queue.Queue[dict]] = {}
# A permission card waits for a person; past this the tool is denied.
APPROVAL_TIMEOUT_S = 1800


def send(msg: dict) -> None:
    global _last_sent
    with _write_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()
        _last_sent = time.monotonic()


def keepalive(acp_id: str, turn_done: threading.Event) -> None:
    """Until *turn_done*, tell Omnigent the turn is alive after KEEPALIVE_S with nothing sent.

    Its own thread, so a slow CLI call or token refresh before the first event is covered too.
    ponytail: time-based, not proof the cloud is progressing; STALL_TIMEOUT_S bounds a stuck turn.
    """
    while not turn_done.is_set():
        idle = time.monotonic() - _last_sent
        if idle >= KEEPALIVE_S:
            thought(acp_id, "")
        else:
            turn_done.wait(KEEPALIVE_S - idle)


def log(text: str) -> None:
    line = f"[claude-cloud-acp {os.getpid()}] {time.strftime('%H:%M:%S')} {text}"
    print(line, file=sys.stderr, flush=True)
    # Omnigent keeps only a tail of agent stderr; a file survives the bridge.
    with contextlib.suppress(OSError):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with (STATE_FILE.parent / "bridge.log").open("a") as out:
            out.write(line + "\n")


# ── claude.ai credentials and the events API ───────────────


def access_token() -> str:
    """Return the Claude CLI's current claude.ai access token.

    Read from where the CLI itself keeps it: the login keychain item
    ``Claude Code-credentials`` for ``$USER`` on macOS, else
    ``~/.claude/.credentials.json``, re-read on every call. Inside the CLI's
    own refresh window, run the CLI to refresh it: the CLI rotates the refresh
    token under its own lock, so the bridge never writes the credential.
    ponytail: only the default config dir (no CLAUDE_CONFIG_DIR suffix).
    """
    if token := os.environ.get("CLAUDE_CLOUD_TOKEN"):
        return token
    oauth = _read_login()
    if oauth.get("expiresAt", 0) / 1000 - time.time() < REFRESH_WINDOW_S:
        global _last_refresh
        if time.monotonic() - _last_refresh > REFRESH_EVERY_S:
            _last_refresh = time.monotonic()
            log("access token near expiry; asking the CLI to refresh it")
            subprocess.run(REFRESH_CMD, capture_output=True, timeout=CLI_TIMEOUT_S, check=False)
            oauth = _read_login()
        if oauth.get("expiresAt", 0) / 1000 < time.time():
            raise RuntimeError(
                "the Claude Code login has expired; run `claude` once to refresh it"
            )
    return oauth["accessToken"]


def _read_login() -> dict:
    raw = ""
    if sys.platform == "darwin":
        raw = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                os.environ["USER"],
                "-s",
                "Claude Code-credentials",
                "-w",
            ],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    if not raw:
        path = Path.home() / ".claude" / ".credentials.json"
        raw = path.read_text() if path.exists() else ""
    if not raw:
        raise RuntimeError("no Claude Code login found; run `claude auth login`")
    return json.loads(raw)["claudeAiOauth"]


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token()}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "ccr-byoc-2025-07-29",
        "Content-Type": "application/json",
    }


def call_api(path: str, body: dict | None = None, method: str | None = None) -> dict:
    """GET *path*, or POST (or *method*) *body* to it, on the sessions API; return the reply."""
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers=_headers(),
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return json.loads(raw) if raw.strip() else {}


def post_event(session_id: str, payload: dict) -> int:
    """Post one client event (a user message or a control request); return its sequence number."""
    payload = {"uuid": str(uuid.uuid4()), **payload}
    reply = call_api(
        f"/v1/code/sessions/{session_id}/events",
        {"session_id": session_id, "events": [{"payload": payload}]},
    )
    return int(reply["results"][0]["sequence_num"])


def post_active(session_id: str, payload: dict) -> tuple[int, bool]:
    """Post *payload*, unarchiving first if needed; return its sequence number and
    whether the session was unarchived."""
    try:
        return post_event(session_id, payload), False
    except urllib.error.HTTPError as exc:
        if exc.code != 409:  # 409 ``session_not_active``: archived
            raise
    call_api(f"/v1/code/sessions/{session_id}/unarchive", {})
    return post_event(session_id, payload), True


def send_message(session_id: str, text: str) -> tuple[int, bool]:
    """Post a user message; return its sequence number and whether the session was unarchived."""
    return post_active(
        session_id,
        {
            "session_id": session_id,
            "type": "user",
            "parent_tool_use_id": None,
            "message": {"role": "user", "content": text},
        },
    )


def interrupt(session_id: str) -> None:
    post_event(
        session_id,
        {
            "type": "control_request",
            "request_id": str(uuid.uuid4()),
            "request": {"subtype": "interrupt"},
        },
    )


def archive(session_id: str) -> None:
    """Archive the cloud session; a later message unarchives it (:func:`post_active`)."""
    try:
        call_api(f"/v1/code/sessions/{session_id}/archive", {})
    except urllib.error.HTTPError as exc:
        if exc.code != 409:  # already archived, as the CLI's own archive call reads it
            raise


def set_title(session_id: str, title: str) -> None:
    """Name the cloud session in the claude.ai list, the call the CLI's rename makes."""
    call_api(f"/v1/code/sessions/{session_id}", {"title": title}, method="PUT")


def fetch_events(session_id: str, cursor: str | None) -> tuple[list[dict], str | None]:
    """Fetch every event after *cursor*; return them and the new tail cursor."""
    events: list[dict] = []
    while True:
        query = "sort_order=asc" + (f"&cursor={cursor}" if cursor else "")
        page = call_api(f"/v1/code/sessions/{session_id}/events?{query}")
        events.extend(page.get("data") or [])
        if page.get("next_cursor"):
            cursor = page["next_cursor"]
            continue
        return events, page.get("resume_cursor") or cursor


def unreachable(error: Exception) -> bool:
    """Whether *error* means the network never reached the API, rather than the API refusing."""
    lost = isinstance(error, (OSError, http.client.HTTPException))
    return lost and not isinstance(error, urllib.error.HTTPError)


def stream_events(session_id: str, after: int) -> Iterator[dict | Exception | None]:
    """Yield the session's events after sequence number *after*, live, without end.

    Between events it yields None for a keepalive, and the error for a connection
    that failed or dropped, so the caller can check its deadline and tell a network
    that is away (:func:`unreachable`) from a quiet session. Reconnects from the
    last event seen, more slowly while the failures last.
    """
    wait, down = 1, False
    while True:
        error: Exception | None = None
        try:
            # Built here: reading the login can fail too, and is retried like the rest.
            req = urllib.request.Request(
                f"{API_BASE}/v1/code/sessions/{session_id}/events/stream?from_sequence_num={after}",
                headers={**_headers(), "Accept": "text/event-stream"},
            )
            with urllib.request.urlopen(req, timeout=STREAM_IDLE_S) as resp:
                for raw in resp:
                    if down:
                        log(f"stream back after {after}")
                    wait, down = 1, False
                    line = raw.decode()
                    # ponytail: one-line ``data:`` fields only, which is all the server sends.
                    event = json.loads(line[5:]) if line.startswith("data:") else {}
                    if "sequence_num" in event:
                        after = int(event["sequence_num"])
                        yield event
                    else:
                        yield None
        except (OSError, http.client.HTTPException, ValueError, RuntimeError) as exc:
            error = exc
            if not down:  # once per outage, not once per attempt
                log(f"stream dropped after {after}, reconnecting: {exc}")
            down = True
        yield error
        time.sleep(wait)
        wait = min(wait * 2, RECONNECT_MAX_S)


# Aliases the CLI resolves to the latest model; any full model id passes through too.
MODEL_ALIASES = ("haiku", "sonnet", "opus")
# Effort for new cloud sessions; unset leaves the account default.
EFFORT = os.environ.get("CLAUDE_CLOUD_EFFORT", "").strip()
# The levels ``claude --effort`` takes (from ``claude --help``).
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def create_cloud_session(
    text: str, cwd: str, model: str | None = None, effort: str | None = None
) -> str:
    """Start ``claude --cloud "<text>" [--model m] [--effort e]``; return the new session id.

    Creating a cloud session is interactive-only in the CLI (``--cloud`` refuses
    ``--print``), so run it on a pseudo-terminal, read the id it prints, and
    close the local view; the cloud session keeps running without it.
    """
    # The CLI uploads the repo as a git bundle; outside one it fails with a garbled TUI error.
    if subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--git-dir"], capture_output=True
    ).returncode:
        raise RuntimeError(
            f"{cwd} is not a git repository, and Claude cloud sessions upload the repo. "
            "Start the session from the repository folder."
        )
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
    proc = subprocess.Popen(
        [CLAUDE_CLI, "--cloud", text]
        + (["--model", model] if model else [])
        + (["--effort", effort or EFFORT] if effort or EFFORT else []),
        cwd=cwd,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        env={**os.environ, "TERM": "xterm-256color"},
    )
    os.close(slave)
    screen = ""
    try:
        deadline = time.monotonic() + CLI_TIMEOUT_S
        while time.monotonic() < deadline and proc.poll() is None:
            if select.select([master], [], [], 1)[0]:
                try:
                    screen += os.read(master, 65536).decode(errors="ignore")
                except OSError:
                    break
                clean = ANSI_RE.sub("", screen)
                if match := SESSION_ID_RE.search(clean):
                    return match.group(1)
                # The trust dialog is the user's call; don't answer it for them.
                if "trustthisfolder" in re.sub(r"\s", "", clean):
                    raise RuntimeError(
                        f"Claude Code has not trusted {cwd} yet. Run `claude` there once, "
                        "accept the folder-trust prompt, then retry."
                    )
        tail = ANSI_RE.sub("", screen).strip()[-800:]
        raise RuntimeError(f"`claude --cloud` printed no session id: {tail}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.close(master)


# Omnigent's host tools act on this machine, not the VM, which has its own shell and files.
HIDDEN_TOOL_PREFIX = "sys_os_"


class McpProxy:
    """A local stdio MCP server, served to the cloud session as an SDK MCP server.

    The cloud CLI sends each MCP message as an ``mcp_message`` control request
    over the events API; the reply goes back the same way. So the server runs on
    this host, and the VM never needs a route to it.
    """

    def __init__(self, spec: dict) -> None:
        self.name = spec["name"]
        self.spec = spec
        self.proc: subprocess.Popen | None = None
        self.init_result: dict | None = None
        # ponytail: one request at a time; concurrent tool calls queue here.
        self.lock = threading.Lock()

    def handle(self, message: dict) -> dict:
        """Answer one MCP JSON-RPC message from the cloud CLI."""
        method, mid = message.get("method"), message.get("id")
        with self.lock:
            self._start()
            if method == "initialize":
                # The proxy did the handshake once; a re-registered server reuses it.
                return {"jsonrpc": "2.0", "id": mid, "result": self.init_result}
            if mid is None:
                if method != "notifications/initialized":
                    self._write(message)
                return {"jsonrpc": "2.0", "result": {}}
            name = (message.get("params") or {}).get("name", "")
            if method == "tools/call" and name.startswith(HIDDEN_TOOL_PREFIX):
                error = {"code": -32602, "message": f"{name} is not available in a cloud session"}
                return {"jsonrpc": "2.0", "id": mid, "error": error}
            reply = self._call(message)
            if method == "tools/list" and "result" in reply:
                tools = reply["result"].get("tools") or []
                reply["result"]["tools"] = [
                    tool for tool in tools if not tool["name"].startswith(HIDDEN_TOOL_PREFIX)
                ]
            return reply

    def _start(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return
        env = {**os.environ, **{e["name"]: e["value"] for e in self.spec.get("env") or []}}
        self.proc = subprocess.Popen(
            [self.spec["command"], *self.spec.get("args", [])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=env,
            text=True,
        )
        reply = self._call(
            {
                "jsonrpc": "2.0",
                "id": "bridge-init",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "claude-cloud-acp", "version": "0"},
                },
            }
        )
        self.init_result = reply.get("result")
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        log(f"MCP server {self.name} started")

    def _write(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def _call(self, message: dict) -> dict:
        self._write(message)
        for line in self.proc.stdout:
            reply = json.loads(line)
            if reply.get("id") == message["id"] and "method" not in reply:
                return reply
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "error": {"code": -32000, "message": f"MCP server {self.name} exited"},
        }


def _answer_file(request_id: str) -> Path:
    name = hashlib.sha256(request_id.encode()).hexdigest()
    return STATE_FILE.parent / "answers" / f"{name}.json"


def once(request_id: str, message: dict, handle) -> dict:
    """Answer an MCP message, running a tool call at most once across bridges.

    A call's result is kept until the cloud session has it, and a day longer
    (:func:`forget_sent_answers`). A bridge that reads the same request again
    sends the kept result: the answer never got through, or that bridge listed
    the open requests before another bridge answered this one.
    When the earlier bridge stopped while the call ran, nobody knows
    whether it took effect, and the call fails saying so. Running it again could
    send a message or start a session twice.
    ponytail: the record is this machine's; a bridge on another host runs the call again.
    """
    if message.get("method") != "tools/call":
        return handle(message)  # handshakes and listings repeat safely
    record = _answer_file(request_id)
    record.parent.mkdir(parents=True, exist_ok=True)
    try:
        record.open("x").close()  # atomic: two bridges cannot both claim the call
    except FileExistsError:
        with contextlib.suppress(ValueError):  # empty or cut short: the call's fate is unknown
            return json.loads(record.read_text())
        error = {
            "code": -32000,
            "message": "the bridge restarted while this call ran: it may have taken effect, "
            "and its result is lost. Check before calling again.",
        }
        return {"jsonrpc": "2.0", "id": message.get("id"), "error": error}
    reply = handle(message)
    with contextlib.suppress(OSError):  # unsaved, the claim stays and reads as unknown
        whole = record.with_suffix(".tmp")
        whole.write_text(json.dumps(reply))
        whole.replace(record)
    return reply


SENT_KEPT_S = 86400


def forget_sent_answers() -> None:
    """Drop the tool results the cloud session has had for a day.

    Only a bridge that listed the open requests before the answer arrived reads
    such a request again, and it does so within seconds.
    """
    for sent in (STATE_FILE.parent / "answers").glob("*.sent"):
        with contextlib.suppress(OSError):  # another bridge dropped it first
            if time.time() - sent.stat().st_mtime > SENT_KEPT_S:
                sent.with_suffix(".json").unlink(missing_ok=True)
                sent.unlink()


def deliver(session_id: str, request_id: str, response: dict) -> None:
    """Post a control response, through a network outage: the CLI waits for it."""
    delay = 1
    while True:
        try:
            return respond(session_id, request_id, response)
        except (OSError, http.client.HTTPException) as exc:
            if not unreachable(exc):
                raise
        time.sleep(delay)
        delay = min(delay * 2, RECONNECT_MAX_S)


def respond(session_id: str, request_id: str, response: dict) -> None:
    post_event(
        session_id,
        {
            "type": "control_response",
            "response": {"subtype": "success", "request_id": request_id, "response": response},
        },
    )


def register_mcp(session_id: str, servers: dict[str, McpProxy]) -> tuple[int, bool]:
    """(Re)register *servers* in the cloud session, like :func:`post_active`.

    Removing them first makes the CLI connect afresh, so it handshakes with this
    bridge rather than one that has gone.
    """

    def request(wanted: dict) -> dict:
        return {
            "type": "control_request",
            "request_id": str(uuid.uuid4()),
            "request": {"subtype": "mcp_set_servers", "servers": wanted},
        }

    sent = post_active(session_id, request({}))
    post_event(session_id, request({name: {"type": "sdk", "name": name} for name in servers}))
    return sent


def ask_client(method: str, params: dict, timeout: float) -> dict:
    """Send Omnigent a JSON-RPC request and wait for its result; ``{}`` on error or timeout."""
    request_id = f"bridge-{uuid.uuid4().hex}"
    _replies[request_id] = queue.Queue(maxsize=1)
    send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    try:
        return _replies[request_id].get(timeout=timeout).get("result") or {}
    except queue.Empty:
        return {}
    finally:
        del _replies[request_id]


def approve(s: Session, tool: str, tool_input: dict, call_id: str) -> bool:
    """Ask Omnigent whether a built-in tool may run: TOOL_CALL policy, then an approval card."""
    params = {
        "sessionId": s.acp_id,
        "toolCall": {"toolCallId": call_id, "title": tool, "rawInput": tool_input},
        "options": [
            {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
            {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
        ],
    }
    # ponytail: once-scoped only; "always allow" would need the CLI's updatedPermissions.
    outcome = ask_client("session/request_permission", params, APPROVAL_TIMEOUT_S).get("outcome")
    return (outcome or {}).get("optionId") == "allow"


def answer_control(s: Session, payload: dict) -> None:
    """Answer a control request the cloud CLI sent to this client."""
    request, request_id = payload.get("request") or {}, payload.get("request_id")
    subtype = request.get("subtype")
    if subtype == "mcp_message" and (proxy := s.mcp.get(request.get("server_name", ""))):
        reply = once(request_id, request.get("message") or {}, proxy.handle)
        deliver(s.cloud_id, request_id, {"mcp_response": reply})
        if (record := _answer_file(request_id)).exists():
            record.with_suffix(".sent").touch()  # delivered: kept one more day
    elif subtype == "can_use_tool":
        tool, tool_input = request.get("tool_name", ""), request.get("input") or {}
        # Omnigent applies its own policy when its relay runs an MCP tool. The
        # VM's built-ins ask here only under the repo's `permissions.ask` rules.
        allowed = any(tool.startswith(f"mcp__{name}__") for name in s.mcp) or approve(
            s, tool, tool_input, request.get("tool_use_id") or request_id
        )
        if allowed:
            decision = {"behavior": "allow", "updatedInput": tool_input}
        else:
            decision = {"behavior": "deny", "message": f"{tool} is not approved by Omnigent"}
        deliver(s.cloud_id, request_id, decision)


# ── event mapping ──────────────────────────────────────────


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            _text_of(b.get("text", b.get("content", ""))) for b in content if isinstance(b, dict)
        )
    return ""


def note_ran_model(s: Session, payload: dict) -> None:
    """Remember the model the cloud's init event reports, so the turn's result can carry it."""
    model = payload.get("model")
    if payload.get("type") == "system" and payload.get("subtype") == "init" and model:
        s.ran_model = str(model)


def to_updates(payload: dict) -> tuple[list[dict], bool]:
    """Map one cloud-session event payload to ACP updates; True ends the turn."""
    kind = payload.get("type")
    # Sub-agent steps carry the id of the Agent tool call that started them.
    # ponytail: mirrored inline with a label, not as nested cards; ACP has no nesting.
    sub = "[sub-agent] " if payload.get("parent_tool_use_id") else ""
    if kind == "result" and not sub:
        return [], True
    if kind == "system" and payload.get("subtype") == "init" and payload.get("model"):
        return (
            [
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": f"[cloud] model: {payload['model']}\n"},
                }
            ],
            False,
        )
    if kind == "env_manager_log":
        text = (payload.get("data") or {}).get("content")
        return (
            [
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": f"[cloud] {text}\n"},
                }
            ]
            if text
            else []
        ), False
    message = payload.get("message") or {}
    blocks = message.get("content")
    if not isinstance(blocks, list):
        return [], False
    updates: list[dict] = []
    for block in blocks:
        btype = block.get("type")
        if kind == "assistant" and btype == "text" and block.get("text"):
            updates.append(
                {
                    "sessionUpdate": "agent_thought_chunk" if sub else "agent_message_chunk",
                    "content": {"type": "text", "text": sub + block["text"]},
                }
            )
        elif kind == "assistant" and btype == "thinking" and block.get("thinking"):
            updates.append(
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": block["thinking"]},
                }
            )
        elif kind == "assistant" and btype == "tool_use":
            updates.append(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": block.get("id"),
                    "title": sub + str(block.get("name")),
                    "kind": "other",
                    "status": "in_progress",
                    "rawInput": block.get("input") or {},
                }
            )
        elif kind == "user" and btype == "tool_result":
            updates.append(
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": block.get("tool_use_id"),
                    "status": "failed" if block.get("is_error") else "completed",
                    "content": [
                        {
                            "type": "content",
                            "content": {"type": "text", "text": _text_of(block.get("content"))},
                        }
                    ],
                }
            )
    return updates, False


# ── ACP server ─────────────────────────────────────────────


class Session:
    def __init__(self, cwd: str, acp_id: str = "") -> None:
        self.cwd = cwd
        self.acp_id = acp_id
        self.cloud_id: str | None = None
        # Picked through ``session/set_model``; it applies when the cloud session is created.
        self.model: str | None = None
        # Picked through ``session/set_config_option``; applies at creation like the model.
        self.effort: str | None = None
        # The model the cloud session was created with ("" = account default); None if adopted.
        self.started_model: str | None = None
        # A pick already answered with the adopted-session note.
        self.noted_model: str | None = None
        # The model the cloud's init event said it runs; sent back as the turn's usage model.
        self.ran_model: str | None = None
        # The effort the cloud session was created with ("" = default); None if adopted.
        self.started_effort: str | None = None
        self.mcp: dict[str, McpProxy] = {}
        self.events: queue.Queue[dict] = queue.Queue()
        self.reader: threading.Thread | None = None
        # Events the turns since the last completed one took off the queue. Omnigent
        # drops what a failed turn showed, so its retry in this bridge shows them again.
        self.taken: list[dict] = []
        # Control requests being answered: the cloud sends nothing while it waits on one.
        self.answering: set[str] = set()
        # Why the stream's last connection failed; None while it delivers.
        self.stream_error: Exception | None = None
        self.cancelled = threading.Event()
        self.running = False
        self.interrupted = False
        # A turn of this bridge that carried the marker has completed.
        self.marked = False
        # The last ``result`` mirrored; recorded as seen once Omnigent has the turn.
        self.mirrored = 0
        # This bridge's last turn was mirrored to its end, so the cloud session is idle.
        self.caught_up = False
        self.lock = threading.RLock()  # re-entered by the SIGTERM handler

    def follow(self, after: int, settled: frozenset[str] = frozenset()) -> None:
        """Read the cloud session's stream from *after* for the rest of the bridge's life.

        Control requests are answered as they arrive, between turns too: the CLI
        handshakes with MCP servers whenever it (re)connects, and gives up quickly.
        Those in *settled* were dealt with before this bridge came, so a stream read
        from the past does not run their tool calls again.
        Every event also goes to :attr:`events` for the running turn.
        """
        if self.reader is not None:
            return

        def read() -> None:
            for event in stream_events(self.cloud_id, after):
                if isinstance(event, Exception):
                    self.stream_error = event
                    continue
                self.stream_error = None
                payload = (event or {}).get("payload") or {}
                if (
                    payload.get("type") == "control_request"
                    and payload.get("request_id") not in settled
                ):
                    # Off the reader: a tool call can outlast the CLI's wait for other events.
                    threading.Thread(target=self.answer, args=(payload,), daemon=True).start()
                if event is not None:
                    self.events.put(event)

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def answer(self, payload: dict) -> None:
        request_id = payload.get("request_id") or str(uuid.uuid4())
        self.answering.add(request_id)
        try:
            answer_control(self, payload)
        finally:
            self.answering.discard(request_id)

    @property
    def offline(self) -> bool:
        """The network is away: the cloud session cannot be reached, and keeps running."""
        return self.stream_error is not None and unreachable(self.stream_error)

    def events_while_alive(self) -> Iterator[dict | None]:
        """Yield the session's events, and None each quiet second, while it shows life.

        Life is an event, or a control request still being answered. After
        STALL_TIMEOUT_S that the session could be reached and showed neither, raise:
        it has stopped progressing. Time this host slept or had no network is not
        counted, because the session works on without it and the stream catches up.
        """
        quiet, last = 0.0, time.monotonic()
        while True:
            try:
                event = self.events.get(timeout=1)
            except queue.Empty:
                now = time.monotonic()
                quiet, last = quiet + min(now - last, STALL_STEP_S), now
                if self.answering or self.offline:
                    quiet = 0.0
                elif quiet > STALL_TIMEOUT_S:
                    why = f" (the stream fails: {self.stream_error})" if self.stream_error else ""
                    raise RuntimeError(
                        f"cloud session https://claude.ai/code/{self.cloud_id} made no progress "
                        f"for {STALL_TIMEOUT_S:.0f}s{why}; it is left as it is, and the next "
                        "message continues it"
                    ) from None
                yield None
                continue
            quiet, last = 0.0, time.monotonic()
            yield event

    def cancel(self) -> None:
        """Interrupt the running cloud turn, once. Only ``session/cancel`` leads here.

        Sent right away, not on the next poll: Omnigent terminates the bridge
        straight after ``session/cancel``.
        """
        first = not self.cancelled.is_set()
        self.cancelled.set()
        with self.lock:
            if self.running and self.cloud_id and not self.interrupted:
                try:
                    interrupt(self.cloud_id)
                except (OSError, http.client.HTTPException, RuntimeError) as exc:
                    # The turn's loop calls again each second until it gets through.
                    if first:
                        log(f"interrupt of {self.cloud_id} not delivered, retrying: {exc}")
                    return
                log(f"interrupted {self.cloud_id}")
                # Only once delivered: a SIGTERM during the POST aborts it, and
                # the handler re-entering here sends it again.
                self.interrupted = True


SESSIONS: dict[str, Session] = {}


def remember(key: str, cloud_id: str | None) -> None:
    """Record *key*'s cloud session; None forgets it."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # The bridges of other Omnigent sessions write the same file.
    with STATE_FILE.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        known = recall()
        if cloud_id is None:
            known.pop(key, None)
        else:
            known[key] = cloud_id
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(known))
        tmp.replace(STATE_FILE)


def recall() -> dict[str, str]:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def session_for(acp_id: str) -> Session:
    """The live session, or one rebuilt from the state file after a respawn."""
    if acp_id not in SESSIONS:
        cloud_id = recall().get(acp_id)
        if cloud_id is None:
            raise RuntimeError(f"unknown ACP session {acp_id}")
        log(f"re-attaching {acp_id} to {cloud_id} after a respawn")
        SESSIONS[acp_id] = Session(os.getcwd(), acp_id)
        SESSIONS[acp_id].cloud_id = cloud_id
    return SESSIONS[acp_id]


def prompt_text(blocks: list[dict]) -> str:
    return "\n\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


def first_prompt_key(cwd: str, text: str) -> str:
    return "first:" + hashlib.sha256(f"{cwd}\0{text}".encode()).hexdigest()


def replayed_first_prompt(replay: str) -> str | None:
    """The fresh prompt that started *replay*'s transcript, as :func:`prompt_text` gave it.

    That is any injected system prompt and the first user message. Each bridge
    marks its turns until one completes, so a replay without a marker holds no
    bridge turn and the first message ends at the next ``user: `` line. Lines of
    other roles come from another harness, and then the replay matches nothing.
    ponytail: a first message with a ``user: `` line of its own is ambiguous; one
    that is cut there matches only a session whose whole first message was the cut part.
    """
    head, found, history = replay.partition(REPLAY_HEAD)
    history = history.rsplit(REPLAY_TAIL, 1)[0]
    if not found or re.search(r"\n(?:assistant|system|developer|tool): ", history):
        return None
    return (head + history.split("\nuser: ", 1)[0]).strip()


def recall_first_prompt(cwd: str, replay: str) -> str | None:
    """The cloud session this bridge created for the Omnigent session *replay* comes from."""
    first = replayed_first_prompt(replay)
    return None if first is None else recall().get(first_prompt_key(cwd, first))


def split_replay(text: str, cwd: str) -> tuple[str | None, str]:
    """Return the cloud session a replayed transcript belongs to, and the message to send.

    That is the session its marker names or, when the turn that would have
    left the marker failed, the one created for its first message. Without
    either the text is a fresh request and goes out whole. With one, the cloud
    session already holds the conversation, so only the latest user message is sent.
    """
    ids = MARKER_RE.findall(text)
    cloud_id = ids[-1] if ids else recall_first_prompt(cwd, text)
    if cloud_id is None:
        return None, text
    latest = text.rsplit(REPLAY_TAIL, 1)[-1] if REPLAY_TAIL in text else text
    latest = "\n".join(line for line in latest.splitlines() if MARKER not in line).strip()
    return cloud_id, latest


def same_message(posted: str, text: str, prompt: str) -> bool:
    """Whether the cloud session got *text* as *posted*: sent again, it would run twice.

    *prompt* is what Omnigent sent this bridge. The first message of an Omnigent
    session was posted with what Omnigent put before it, and
    :func:`replayed_first_prompt` restores that from a replayed *prompt*.
    ponytail: a first message that was itself a replayed transcript (a session
    moved here from another harness) is not recognized, and is sent again.
    """
    posted, text = posted.strip(), text.strip()
    if not text or posted == text:
        return bool(text)
    history = prompt.partition(REPLAY_HEAD)[2].rsplit(REPLAY_TAIL, 1)[0]
    is_first = history.split("\nuser: ", 1)[0].strip() == text
    return is_first and posted == replayed_first_prompt(prompt)


def backlog(s: Session, text: str, prompt: str) -> tuple[int, bool, frozenset[str], int] | None:
    """What a bridge new to its cloud session mirrors before it sends *text*.

    The session's last message is behind when Omnigent never got its turn: the
    turn still runs, or it ended after the bridge that sent it was gone. Returns
    that message's sequence number; whether it is *text* itself (a retry, which
    sent again would run twice); the control requests already dealt with; and
    where to read the stream from, which is earlier than the message when a
    request from before it still waits for an answer. None when nothing is behind.
    ponytail: without a ``seen:`` record (another machine's bridge), a finished
    turn counts as behind only when its message is *text*.
    """
    events, _ = fetch_events(s.cloud_id, None)
    message = result = 0
    posted = ""
    asked: dict[str, int] = {}
    settled: set[str] = set()
    for event in sorted(events, key=lambda e: int(e["sequence_num"])):
        seq, payload = int(event["sequence_num"]), event.get("payload") or {}
        kind = payload.get("type")
        if kind == "control_request":
            asked[payload.get("request_id")] = seq
        elif kind == "control_response":
            settled.add((payload.get("response") or {}).get("request_id"))
        elif kind == "control_cancel_request":  # a new CLI drops what the old one asked
            settled.add(payload.get("request_id"))
        elif payload.get("parent_tool_use_id"):
            continue  # a sub-agent's step
        elif kind == "result":
            result = seq
            settled |= asked.keys()  # a turn's requests end with it
        elif kind == "user" and event.get("source", "client") == "client":
            content = (payload.get("message") or {}).get("content")
            # A tool result is a user event too, from the VM.
            if isinstance(content, str) or not any(
                b.get("type") == "tool_result" for b in content or []
            ):
                message = seq
                if _text_of(content) != CONTINUE_PROMPT:  # it continues the message before
                    posted = _text_of(content)
    same = same_message(posted, text, prompt)
    seen = recall().get(f"seen:{s.cloud_id}")
    behind = message > int(seen) if seen else message > 0 and (result < message or same)
    if not behind:
        return None
    waiting = [seq - 1 for request_id, seq in asked.items() if request_id not in settled]
    return message, same, frozenset(settled), min([message, *waiting])


def thought(acp_id: str, text: str) -> None:
    update(
        acp_id, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": text}}
    )


def update(acp_id: str, upd: dict) -> None:
    send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": acp_id, "update": upd},
        }
    )


def run_turn(acp_id: str, text: str) -> str:
    s = session_for(acp_id)
    s.cancelled.clear()
    s.interrupted = False
    s.running = True
    turn_done = threading.Event()
    beat = threading.Thread(
        target=keepalive, args=(acp_id, turn_done), name="keepalive", daemon=True
    )
    beat.start()
    try:
        stop = _mirror_turn(acp_id, s, text)
        s.marked = True
        return stop
    except Exception as exc:
        s.caught_up = False  # the cloud turn may still run; the next turn looks first
        # Omnigent drops a failed turn's output, marker included, so the error names it.
        if s.cloud_id and s.cloud_id not in str(exc):
            raise RuntimeError(
                f"{exc} (cloud session https://claude.ai/code/{s.cloud_id})"
            ) from exc
        raise
    finally:
        # Joined before the prompt's response goes out, so no update follows it.
        turn_done.set()
        beat.join()
        s.running = False


def announce(acp_id: str, s: Session) -> None:
    """Record the cloud session as soon as it exists, so a turn that fails still names it."""
    remember(acp_id, s.cloud_id)
    thought(acp_id, f"[cloud] https://claude.ai/code/{s.cloud_id}\n")
    # Lands in the transcript, so a restarted bridge finds the session.
    update(
        acp_id,
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": f"{MARKER}{s.cloud_id}\n\n"},
        },
    )


def _create(acp_id: str, s: Session, cli_prompt: str, first: str) -> None:
    """Start the cloud session, recorded under the Omnigent session's *first* prompt."""
    s.cloud_id = create_cloud_session(cli_prompt, s.cwd, s.model, s.effort)
    s.started_model = s.model or ""
    s.started_effort = s.effort or ""
    # Recorded first: nothing after it may lose the link to a session that exists.
    remember(first_prompt_key(s.cwd, first), s.cloud_id)
    log(f"created {s.cloud_id}")
    announce(acp_id, s)


def _mirror_turn(acp_id: str, s: Session, text: str) -> str:
    prompt, again = text, True
    if s.caught_up:
        s.taken.clear()  # the last turn reached Omnigent
    if s.cloud_id is None:
        s.cloud_id, text = split_replay(text, s.cwd)
        if s.cloud_id is None:
            if s.mcp:
                after = _bootstrap(acp_id, s, text)
            else:
                _create(acp_id, s, text, text)
                after = 0
            s.follow(after)
            return _continued(acp_id, s, _mirror(acp_id, s, after))
        log(f"re-attaching to {s.cloud_id}")
    if not s.marked:
        # A new bridge, or one whose announcing turn failed and Omnigent dropped it.
        announce(acp_id, s)
    if not s.caught_up and (behind := backlog(s, text, prompt)):
        # Attach before sending: the cloud session did not stop when its bridge did,
        # nor when this bridge's last turn failed.
        after, same, settled, start = behind
        log(f"{s.cloud_id}: mirroring from {after} before sending (same message: {same})")
        thought(
            acp_id,
            "[cloud] the session kept working without the bridge; showing what it did"
            + ("\n" if same else ", then sending this message\n"),
        )
        s.follow(start, settled)
        stop = _mirror(acp_id, s, after)
        if stop == "cut off":
            if same:  # this message's one continuation
                text, again = CONTINUE_PROMPT, False
        elif same or stop == "cancelled":
            return stop
    check_model(acp_id, s)
    check_effort(s)
    after, unarchived = _send(s, text)
    if unarchived:
        thought(acp_id, "[cloud] unarchived the session; files from before the archive are gone\n")
    s.follow(after)
    return _continued(acp_id, s, _mirror(acp_id, s, after), again)


def _continued(acp_id: str, s: Session, stop: str, again: bool = True) -> str:
    """Ask a session whose turn was cut off to continue, once, and mirror that turn.

    *again* is false when the turn just mirrored was that one continuation.
    """
    if stop != "cut off":
        return stop
    if again:
        after, _ = _send(s, CONTINUE_PROMPT)
        stop = _mirror(acp_id, s, after)
    if stop == "cut off":
        # A sandbox that keeps restarting needs a person.
        raise RuntimeError("the cloud session's sandbox restarted mid-turn twice")
    return stop


# What a new CLI and the bridge exchange while no turn runs.
_NOT_TURN_LIFE = {
    "env_manager_log",
    "control_request",
    "control_response",
    "control_cancel_request",
}


def _mirror(acp_id: str, s: Session, after: int) -> str:
    """Mirror the cloud session's events after *after* until its turn ends.

    A turn ends with a ``result``. One the cloud cut off has none: its sandbox was
    replaced mid-turn (it idled on a request to a host that was away), the new CLI
    announces itself and then waits for a message. That returns "cut off", once
    the session then stayed quiet for :data:`CUT_OFF_GRACE_S` seconds in a row
    with no answer of this host under way. Anything the turn itself sends after
    the new CLI's start (output, a tool's progress) shows it is alive.

    Events an earlier, failed turn of this bridge took come first.
    """
    offline = False
    began, quiet = False, None  # the turn produced output; quiet seconds since a new CLI
    again = len(s.taken)
    for n, event in enumerate(itertools.chain(list(s.taken), s.events_while_alive())):
        if s.cancelled.is_set():
            # A cancel that came before the session existed, or that the network
            # dropped. Keep mirroring after it: the interrupted turn still ends
            # with a ``result`` event.
            s.cancel()
        if s.offline != offline:
            offline = s.offline
            thought(
                acp_id,
                "[cloud] no network: the cloud session keeps running, reconnecting\n"
                if offline
                else "[cloud] reconnected\n",
            )
        if event is None:
            if quiet is not None:
                quiet = 0 if offline or s.answering else quiet + 1
                if quiet >= CUT_OFF_GRACE_S:
                    log(f"{s.cloud_id}: the sandbox restarted mid-turn; the turn was cut off")
                    thought(acp_id, "[cloud] the sandbox restarted and cut the turn off\n")
                    return "cut off"
            continue
        if n >= again:
            s.taken.append(event)
        if int(event["sequence_num"]) <= after:
            continue  # from before this turn's message
        payload = event.get("payload") or {}
        if payload.get("type") == "assistant":
            began, quiet = True, None
        elif began and payload.get("subtype") == "turn_handoff_available":
            quiet = 0  # a CLI that starts before the turn does takes the message instead
        elif quiet is not None:
            quiet = 0 if payload.get("type") in _NOT_TURN_LIFE else None
        note_ran_model(s, payload)
        updates, done = to_updates(payload)
        for upd in updates:
            update(acp_id, upd)
        if done:
            s.mirrored, s.caught_up = int(event["sequence_num"]), True
            return "cancelled" if s.interrupted else "end_turn"
    raise AssertionError("events_while_alive raises rather than ends")


# The CLI starts a new session on its first prompt, before servers can be registered.
# ponytail: a throwaway first turn; it shows in the cloud transcript.
BOOTSTRAP_PROMPT = "Reply with the single word: ready"


def _bootstrap(acp_id: str, s: Session, text: str) -> int:
    """Create the cloud session, register MCP servers, then post *text*; return its seq."""
    _create(acp_id, s, BOOTSTRAP_PROMPT, text)
    s.follow(0)
    for event in s.events_while_alive():
        payload = (event or {}).get("payload") or {}
        if payload.get("type") in ("env_manager_log", "system"):
            note_ran_model(s, payload)
            for upd in to_updates(payload)[0]:
                update(acp_id, upd)
        elif to_updates(payload)[1]:
            if payload.get("is_error"):  # a failed setup script ends the session here
                errors = payload.get("errors") or [str(payload.get("result") or "unknown error")]
                # The cloud says to start a new session, so the next message makes one.
                for key in (acp_id, first_prompt_key(s.cwd, text)):
                    remember(key, None)
                SESSIONS[acp_id] = Session(s.cwd, acp_id)
                SESSIONS[acp_id].mcp = s.mcp
                raise RuntimeError(f"cloud session setup failed: {'; '.join(errors)}")
            break
    after, _ = _send(s, text)
    return after


def _send(s: Session, text: str) -> tuple[int, bool]:
    """Post the prompt, registering MCP servers first.

    Every time: a cloud session resumed in a new sandbox runs a new CLI without them.
    """
    if not s.mcp:
        return send_message(s.cloud_id, text)
    sent = register_mcp(s.cloud_id, s.mcp)
    send_message(s.cloud_id, text)
    return sent


def new_session_result(acp_id: str) -> dict:
    """The ``session/new`` answer; the catalog makes Omnigent send ``session/set_model``."""
    models = [{"modelId": m, "name": m} for m in MODEL_ALIASES]
    effort = {
        "id": "effort",
        "name": "Effort",
        "type": "select",
        "currentValue": EFFORT or "",
        "options": [{"value": level, "name": level} for level in EFFORT_LEVELS],
    }
    return {
        "sessionId": acp_id,
        "models": {"availableModels": models},
        "configOptions": [effort],
    }


def set_model(params: dict) -> None:
    """Record Omnigent's pick. Omnigent's executor ignores a refusal here, so the turn refuses."""
    session_for(params["sessionId"]).model = params.get("modelId")


def set_effort(params: dict) -> None:
    """Record Omnigent's effort pick; refuse a level the cloud CLI does not take."""
    level = params.get("value")
    if level not in EFFORT_LEVELS:
        raise ValueError(
            f"effort {level!r} is not supported by Claude cloud sessions; "
            f"allowed levels: {', '.join(EFFORT_LEVELS)}"
        )
    session_for(params["sessionId"]).effort = level


def check_effort(s: Session) -> None:
    """Refuse a level the running cloud session can't take: the CLI fixes it at creation."""
    if not s.effort or s.started_effort is None or s.effort == (s.started_effort or EFFORT):
        return
    current = s.started_effort or EFFORT or "the account default"
    raise RuntimeError(
        f"this cloud session runs at effort {current}; "
        f"it cannot switch to {s.effort}. Start a new session for that effort."
    )


def check_model(acp_id: str, s: Session) -> None:
    """Refuse a pick the existing cloud session can't take: the CLI fixes the model at creation."""
    if not s.model or s.model in (s.started_model, s.noted_model):
        return
    if s.started_model is not None:
        raise RuntimeError(
            f"this cloud session runs on {s.started_model or 'the account default'}; "
            f"it cannot switch to {s.model}. Start a new session for that model."
        )
    # Adopted after a restart: its original model isn't recorded.
    thought(acp_id, "[cloud] resumed session keeps the model it started on\n")
    s.noted_model = s.model


def handle_prompt(msg: dict) -> None:
    params = msg.get("params") or {}
    try:
        stop = run_turn(params["sessionId"], prompt_text(params.get("prompt") or []))
        result: dict = {"stopReason": stop}
        ran_model = SESSIONS[params["sessionId"]].ran_model
        if ran_model:
            result["usage"] = {"model": ran_model}
        send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
    except Exception as exc:  # noqa: BLE001 — every failure goes back to the client
        log(f"turn failed: {exc}")
        send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32000, "message": str(exc)}})
        return
    # Only after the response: a bridge that dies before it leaves the turn
    # unseen, so a retry of its message mirrors it again and is not sent twice.
    session = SESSIONS[params["sessionId"]]
    with contextlib.suppress(OSError):
        if session.mirrored:
            remember(f"seen:{session.cloud_id}", str(session.mirrored))


def stop(*_: object) -> None:
    """On SIGTERM or EOF, leave: a running cloud turn goes on, and the next bridge mirrors it.

    Only ``session/cancel`` stops the cloud session. Omnigent terminates the bridge
    straight after sending one, so a cancel still on its way is delivered first.
    """
    for session in SESSIONS.values():
        if session.cancelled.is_set():
            session.cancel()
        elif session.running:
            log(f"leaving {session.cloud_id} running")
    log("stopping")
    sys.exit(0)


def serve() -> None:
    signal.signal(signal.SIGTERM, stop)
    forget_sent_answers()
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        method, params, mid = msg.get("method"), msg.get("params") or {}, msg.get("id")
        if method is None and (waiting := _replies.get(str(mid))):
            waiting.put(msg)
        elif method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "agentCapabilities": {
                            "loadSession": False,
                            "promptCapabilities": {"image": False},
                        },
                        "authMethods": [],
                    },
                }
            )
        elif method == "session/new":
            acp_id = f"ccloud-{uuid.uuid4().hex}"
            SESSIONS[acp_id] = Session(params.get("cwd") or os.getcwd(), acp_id)
            # ponytail: stdio servers only, which is what Omnigent's relay is.
            SESSIONS[acp_id].mcp = {
                spec["name"]: McpProxy(spec)
                for spec in params.get("mcpServers") or []
                if spec.get("command")
            }
            log(f"session {acp_id} cwd={SESSIONS[acp_id].cwd}")
            send({"jsonrpc": "2.0", "id": mid, "result": new_session_result(acp_id)})
        elif method == "session/set_config_option" and params.get("configId") == "effort":
            try:
                set_effort(params)
                send({"jsonrpc": "2.0", "id": mid, "result": {}})
            except ValueError as exc:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": str(exc)}})
        elif method == "session/set_model":
            set_model(params)
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "session/prompt":
            threading.Thread(target=handle_prompt, args=(msg,), daemon=True).start()
        elif method == "session/cancel":
            log(f"cancel {params.get('sessionId')}")
            if session := SESSIONS.get(params.get("sessionId", "")):
                session.cancel()
        elif mid is not None:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {"code": -32601, "message": f"method not supported: {method}"},
                }
            )
    stop()  # stdin closed: Omnigent is tearing the bridge down


# A stdio MCP server with one ``echo`` tool, for the self-check and ``e2e.py mcp``.
ECHO_MCP = """
import json, socket, sys
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    r = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
         "serverInfo": {"name": "echo", "version": "0"}}
    if m["method"] == "tools/list":
        schema = {"type": "object", "properties": {"text": {"type": "string"}}}
        r = {"tools": [{"name": n, "description": "Echo text, tagged with the serving host.",
             "inputSchema": schema} for n in ("echo", "sys_os_shell")]}
    elif m["method"] == "tools/call":
        t = m["params"]["arguments"].get("text", "")
        t = f"echo: {t} (served by {socket.gethostname()})"
        r = {"content": [{"type": "text", "text": t}]}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
"""


def self_check() -> None:
    globals()["STATE_FILE"] = Path(tempfile.mkdtemp()) / "sessions.json"
    real_stream_events = stream_events
    os.environ.pop("CLAUDE_CLOUD_TOKEN", None)  # the token cases read the stubbed login
    # Outside a git repo the session fails plainly, before any `claude` runs.
    not_repo = Path(tempfile.mkdtemp())
    stub = not_repo / "claude"
    stub.write_text(f"#!/bin/sh\ntouch {not_repo}/ran\n")
    stub.chmod(0o755)
    real_cli, globals()["CLAUDE_CLI"] = CLAUDE_CLI, str(stub)
    real_fns = {n: globals()[n] for n in ("create_cloud_session", "remember", "announce", "_send")}
    try:
        create_cloud_session("hi", str(not_repo))
        raise AssertionError("a session started outside a git repo")
    except RuntimeError as exc:
        assert "is not a git repository" in str(exc), exc
    assert not (not_repo / "ran").exists()
    # The picked model and effort reach the CLI as flags; none picked adds none.
    subprocess.run(["git", "init", "-q", str(not_repo)], check=True)
    stub.write_text(
        f'#!/bin/sh\necho "$@" > {not_repo}/args\necho session_01ArgsArgsArgs; sleep 2\n'
    )
    for model, effort, flags in ((None, "", ""), ("haiku", "low", " --model haiku --effort low")):
        globals()["EFFORT"] = effort
        assert create_cloud_session("hi", str(not_repo), model) == "session_01ArgsArgsArgs"
        assert (not_repo / "args").read_text().strip() == "--cloud hi" + flags
    globals()["EFFORT"] = ""
    # A per-session pick wins over the bridge-wide default.
    assert create_cloud_session("hi", str(not_repo), None, "max")
    assert (not_repo / "args").read_text().strip() == "--cloud hi --effort max"
    ran = Session("/tmp")
    note_ran_model(ran, {"type": "system", "subtype": "init", "model": "test-model-1"})
    assert ran.ran_model == "test-model-1"
    note_ran_model(ran, {"type": "assistant", "model": "ignored"})
    assert ran.ran_model == "test-model-1"
    init_upd, _ = to_updates({"type": "system", "subtype": "init", "model": "test-model-1"})
    assert "model: test-model-1" in init_upd[0]["content"]["text"], init_upd
    globals()["CLAUDE_CLI"] = real_cli
    # Negotiation: the catalog, then a pick, reach creation; a later different pick refuses.
    picked: list[str | None] = []
    globals()["create_cloud_session"] = lambda _p, _c, model=None, _effort=None: (
        picked.append(model) or "session_01PickedPicked"
    )
    globals()["remember"] = lambda *_: None
    globals()["announce"] = lambda *_: None
    globals()["_send"] = lambda *_: (1, False)
    first = SESSIONS["acp-pick"] = Session("/", "acp-pick")
    assert {
        m["modelId"] for m in new_session_result("acp-pick")["models"]["availableModels"]
    } == set(MODEL_ALIASES)
    set_model({"sessionId": "acp-pick", "modelId": "haiku"})
    first.reader = threading.current_thread()
    first.events.put({"sequence_num": "1", "payload": {"type": "result"}})
    with contextlib.redirect_stdout(io.StringIO()):
        _mirror_turn("acp-pick", first, "go")
    assert picked == ["haiku"], picked
    # An effort pick reaches the CLI; a level the CLI lacks is refused naming the allowed ones.
    set_effort({"sessionId": "acp-pick", "value": "low"})
    assert first.effort == "low"
    try:
        set_effort({"sessionId": "acp-pick", "value": "ultra"})
        raise AssertionError("an unsupported effort was accepted")
    except ValueError as exc:
        assert "low, medium, high, xhigh, max" in str(exc), exc
    first.started_effort = "low"
    first.effort = "max"
    try:
        check_effort(first)
        raise AssertionError("a running session switched effort")
    except RuntimeError as exc:
        assert "cannot switch to max" in str(exc), exc
    first.effort = "low"
    check_effort(first)
    set_model({"sessionId": "acp-pick", "modelId": "opus"})
    try:
        _mirror_turn("acp-pick", first, "again")
        raise AssertionError("a running session switched model")
    except RuntimeError as exc:
        assert "cannot switch to opus" in str(exc), exc
    # A session adopted from a transcript can't be checked: it says so and goes on.
    adopted = Session("/", "acp-adopt")
    adopted.cloud_id, adopted.model = "session_01Adopted", "opus"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        check_model("acp-adopt", adopted)
    assert "keeps the model it started on" in out.getvalue() and adopted.started_model is None
    # The init event's model shows up even when MCP servers force the bootstrap turn.
    boot = SESSIONS["acp-boot"] = Session("/", "acp-boot")
    boot.reader = threading.current_thread()
    boot.events.put(
        {
            "sequence_num": "1",
            "payload": {"type": "system", "subtype": "init", "model": "test-model-1"},
        }
    )
    boot.events.put({"sequence_num": "2", "payload": {"type": "result"}})
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        _bootstrap("acp-boot", boot, "go")
    assert "model: test-model-1" in out.getvalue(), out.getvalue()
    # The model the cloud ran reaches Omnigent in the prompt result, whether the turn
    # went through the bootstrap (MCP servers) or the plain mirror path.
    for acp_id, with_mcp in (("acp-ran-boot", True), ("acp-ran-mirror", False)):
        ran = SESSIONS[acp_id] = Session("/", acp_id)
        ran.reader = threading.current_thread()
        if with_mcp:
            ran.mcp = {
                "echo": McpProxy(
                    {"name": "echo", "command": sys.executable, "args": ["-c", ECHO_MCP]}
                )
            }
        for seq, payload in enumerate(
            [{"type": "system", "subtype": "init", "model": "ran-model-1"}, {"type": "result"}]
            + ([{"type": "result"}] if with_mcp else []),
            1,
        ):
            ran.events.put({"sequence_num": str(seq), "payload": payload})
        params = {"sessionId": acp_id, "prompt": [{"type": "text", "text": "go"}]}
        with contextlib.redirect_stdout(sent := io.StringIO()):
            handle_prompt({"id": 7, "params": params})
        replies = [json.loads(line) for line in sent.getvalue().splitlines() if '"id": 7' in line]
        assert replies and replies[-1]["result"]["usage"] == {"model": "ran-model-1"}, (
            acp_id,
            sent.getvalue(),
        )
    # Over the real wire: catalog in session/new, set_model acknowledged, prompt dispatched.
    server = subprocess.Popen(
        [sys.executable, __file__], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )

    def call(mid: int, method: str, params: dict) -> dict:
        server.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params}) + "\n"
        )
        server.stdin.flush()
        while (reply := json.loads(server.stdout.readline())).get("id") != mid:
            pass  # skip keepalive updates
        return reply

    try:
        call(1, "initialize", {})
        new = call(2, "session/new", {"cwd": str(not_repo / "nowhere")})["result"]
        assert new["models"]["availableModels"], new
        sid = new["sessionId"]
        assert [o["id"] for o in new["configOptions"]] == ["effort"], new
        bad = call(
            9,
            "session/set_config_option",
            {"sessionId": sid, "configId": "effort", "value": "ultra"},
        )
        assert "allowed levels" in bad["error"]["message"], bad
        ok = call(
            10,
            "session/set_config_option",
            {"sessionId": sid, "configId": "effort", "value": "low"},
        )
        assert ok["result"] == {}, ok
        assert call(3, "session/set_model", {"sessionId": sid, "modelId": "haiku"})["result"] == {}
        reply = call(
            4, "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]}
        )
        assert "is not a git repository" in reply["error"]["message"], reply
    finally:
        server.kill()
    for name in ("create_cloud_session", "remember", "announce", "_send"):
        globals()[name] = real_fns[name]
    upd, done = to_updates(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
                ]
            },
        }
    )
    assert [u["sessionUpdate"] for u in upd] == ["agent_message_chunk", "tool_call"] and not done
    assert upd[1]["toolCallId"] == "t1" and upd[1]["rawInput"] == {"command": "ls"}
    upd, _ = to_updates(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "is_error": True,
                        "content": [{"type": "text", "text": "boom"}],
                    }
                ]
            },
        }
    )
    assert upd[0]["status"] == "failed" and upd[0]["content"][0]["content"]["text"] == "boom"
    assert to_updates({"type": "result"}) == ([], True)
    upd, _ = to_updates(
        {
            "type": "assistant",
            "parent_tool_use_id": "x",
            "message": {
                "content": [
                    {"type": "text", "text": "looking"},
                    {"type": "tool_use", "id": "t2", "name": "Read", "input": {}},
                ]
            },
        }
    )
    assert [u["sessionUpdate"] for u in upd] == ["agent_thought_chunk", "tool_call"]
    assert (
        upd[0]["content"]["text"] == "[sub-agent] looking"
        and upd[1]["title"] == "[sub-agent] Read"
    )
    assert to_updates({"type": "result", "parent_tool_use_id": "x"}) == ([], False)
    assert split_replay("fix the bug", "/") == (None, "fix the bug")
    replay = (
        "Conversation so far:\nuser: hi\nassistant: done\n\n"
        f"{MARKER}session_01AbCdEfGhIjKl\n\n{REPLAY_TAIL}and now this?"
    )
    assert split_replay(replay, "/") == ("session_01AbCdEfGhIjKl", "and now this?")
    adopt = f"{MARKER}session_01AbCdEfGhIjKl\ncontinue the migration"
    assert split_replay(adopt, "/") == ("session_01AbCdEfGhIjKl", "continue the migration")
    assert to_updates({"type": "user", "message": {"content": "plain prompt echo"}}) == ([], False)
    screen = "\x1b[32m✓\x1b[0m Created cloud session \x1b]8;;x\x07https://claude.ai/code/session_01DiUkqY2kzbUb"
    assert SESSION_ID_RE.search(ANSI_RE.sub("", screen)).group(1) == "session_01DiUkqY2kzbUb"
    proxy = McpProxy({"name": "echo", "command": sys.executable, "args": ["-c", ECHO_MCP]})
    init = proxy.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    assert init["id"] == 0 and init["result"]["serverInfo"]["name"] == "echo", init
    assert proxy.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})["result"] == {}
    call = {"name": "echo", "arguments": {"text": "hi"}}
    reply = proxy.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": call})
    assert reply["id"] == 5 and reply["result"]["content"][0]["text"].startswith("echo: hi"), reply
    listed = proxy.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/list"})
    assert [t["name"] for t in listed["result"]["tools"]] == ["echo"], listed
    hidden = {"name": "sys_os_shell", "arguments": {}}
    refused = proxy.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": hidden})
    assert refused["id"] == 7 and "error" in refused, refused
    proxy.proc.kill()
    answers: list[tuple] = []
    globals()["respond"] = lambda *args: answers.append(args[1:])
    verdicts = {"Bash": "allow", "Write": "reject"}
    asked: list[dict] = []
    globals()["ask_client"] = lambda _method, params, _timeout: (
        asked.append(params)
        or {"outcome": {"outcome": "selected", "optionId": verdicts[params["toolCall"]["title"]]}}
    )
    gated = Session("/", "acp-1")
    gated.mcp = {"echo": proxy}
    for tool in ("mcp__echo__echo", "Bash", "Write"):
        ask = {"subtype": "can_use_tool", "tool_name": tool, "input": {"x": 1}}
        answer_control(gated, {"request_id": tool, "request": ask})
    assert [(rid, r["behavior"]) for rid, r in answers] == [
        ("mcp__echo__echo", "allow"),
        ("Bash", "allow"),
        ("Write", "deny"),
    ], answers
    assert [(a["sessionId"], a["toolCall"]["rawInput"]) for a in asked] == [
        ("acp-1", {"x": 1})
    ] * 2
    logins = [{"accessToken": "old", "expiresAt": (time.time() + 60) * 1000}]
    logins.append({"accessToken": "new", "expiresAt": (time.time() + 3600) * 1000})
    runs: list[list[str]] = []
    globals()["_read_login"] = lambda: logins[0] if not runs else logins[1]
    real_run, subprocess.run = subprocess.run, lambda cmd, **_: runs.append(cmd)
    try:
        assert access_token() == "new" and runs == [REFRESH_CMD], runs
        assert access_token() == "new" and len(runs) == 1  # fresh token: no second refresh
        # Right after boot monotonic() is near zero; a fresh module must still refresh at once.
        fresh = runpy.run_path(__file__, run_name="fresh_bridge")
        boot_runs: list[list[str]] = []
        fresh_globals = fresh["access_token"].__globals__
        fresh_globals["_read_login"] = lambda: (
            logins[1]
            if boot_runs
            else {"accessToken": "old", "expiresAt": (time.time() + 60) * 1000}
        )
        subprocess.run = lambda cmd, **_: boot_runs.append(cmd)
        real_monotonic, time.monotonic = time.monotonic, lambda: 5.0
        try:
            assert fresh["access_token"]() == "new" and boot_runs == [fresh["REFRESH_CMD"]], (
                boot_runs
            )
        finally:
            time.monotonic = real_monotonic
    finally:
        subprocess.run = real_run
    # Each prompt re-registers the MCP servers before it, for a CLI a new sandbox started.
    posts: list[str] = []

    def record(_id: str, payload: dict) -> tuple[int, bool]:
        posts.append(payload.get("request", {}).get("subtype") or payload["type"])
        return len(posts), False

    real_posts = post_active, post_event
    globals()["post_active"] = globals()["post_event"] = record
    tools = Session("/", "acp-tools")
    tools.cloud_id, tools.mcp = "session_01Tools", {"echo": proxy}
    assert _send(tools, "one") == (1, False) and _send(tools, "two") == (4, False)
    assert posts == ["mcp_set_servers", "mcp_set_servers", "user"] * 2, posts
    globals()["post_active"], globals()["post_event"] = real_posts
    # A turn quiet for longer than KEEPALIVE_S still sends updates, so Omnigent
    # never idles it out; a turn that fails has already named its cloud session.
    out = io.StringIO()
    before_events: list[int] = []

    def slow_send(*_: object) -> tuple[int, bool]:
        time.sleep(2.5)  # a token refresh or POST before the first event
        before_events.append(out.getvalue().count('"session/update"'))
        return 1, False

    globals()["_send"] = slow_send
    globals()["create_cloud_session"] = lambda *_: "session_01NewNewNewNew"
    real_remember, globals()["remember"] = remember, lambda *_: None
    # The sessions below fake a reader; an empty cloud log has nothing to catch up on.
    globals()["fetch_events"] = lambda *_: ([], None)
    globals()["KEEPALIVE_S"] = 0.5
    quiet = SESSIONS["acp-quiet"] = Session("/", "acp-quiet")
    quiet.cloud_id, quiet.reader = "session_q", threading.current_thread()
    result = {"sequence_num": "2", "payload": {"type": "result"}}
    threading.Timer(5, quiet.events.put, [result]).start()
    with contextlib.redirect_stdout(out):
        assert run_turn("acp-quiet", "sleep 420") == "end_turn"
    assert before_events[0] >= 1 and out.getvalue().count('"session/update"') >= 3, out.getvalue()
    fresh = SESSIONS["acp-fresh"] = Session("/", "acp-fresh")
    fresh.reader = threading.current_thread()
    globals()["STALL_TIMEOUT_S"] = 0.1
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.suppress(RuntimeError):
        run_turn("acp-fresh", "start")
    assert f"{MARKER}session_01NewNewNewNew" in out.getvalue(), out.getvalue()
    # A turn outlives the stall timeout while its cloud session sends events or
    # waits on a control request the bridge is answering.
    globals()["STALL_TIMEOUT_S"] = 0.5
    globals()["_send"] = lambda *_: (1, False)
    working = SESSIONS["acp-working"] = Session("/", "acp-working")
    working.cloud_id, working.reader = "session_w", threading.current_thread()
    for seq in range(2, 10):
        progress = {"sequence_num": str(seq), "payload": {"type": "tool_progress"}}
        threading.Timer(0.3 * seq, working.events.put, [progress]).start()
    threading.Timer(3.2, working.events.put, [result | {"sequence_num": "10"}]).start()
    started = time.monotonic()
    with contextlib.redirect_stdout(io.StringIO()):
        assert run_turn("acp-working", "build it") == "end_turn"
    assert time.monotonic() - started > 3, "ended before the result"
    real_answer_control = answer_control
    globals()["answer_control"] = lambda *_: time.sleep(2.5)  # a long MCP call
    threading.Thread(target=working.answer, args=({"request_id": "mcp-call"},)).start()
    threading.Timer(2.7, working.events.put, [result | {"sequence_num": "11"}]).start()
    with contextlib.redirect_stdout(io.StringIO()):
        assert run_turn("acp-working", "wait on a child session") == "end_turn"
    # A stalled one fails naming its cloud session; Omnigent then respawns the
    # bridge and replays a transcript that lost the marker with the failed turn,
    # and the new bridge continues that cloud session instead of creating one.
    globals()["remember"] = real_remember
    created: list[str] = []
    globals()["create_cloud_session"] = lambda prompt, _cwd, _model=None, _effort=None: (
        created.append(prompt) or "session_01StalledStalled"
    )
    stalled = SESSIONS["acp-stalled"] = Session("/repo", "acp-stalled")
    stalled.reader = threading.current_thread()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            run_turn("acp-stalled", prompt_text([{"type": "text", "text": "the brief\nstep 2 "}]))
        raise AssertionError("a stalled turn ended")
    except RuntimeError as exc:
        assert "session_01StalledStalled made no progress" in str(exc), exc
    posted: list[tuple[str, str]] = []
    globals()["_send"] = lambda s, text: posted.append((s.cloud_id, text)) or (5, False)
    retry = SESSIONS["acp-retry"] = Session("/repo", "acp-retry")
    retry.reader = threading.current_thread()
    retry.events.put(result | {"sequence_num": "6"})
    replay = f"{REPLAY_HEAD}the brief\nstep 2 \nuser: and this\n\n{REPLAY_TAIL}and now this"
    with contextlib.redirect_stdout(io.StringIO()):
        assert run_turn("acp-retry", replay) == "end_turn"
    assert created == ["the brief\nstep 2"] and posted == [
        ("session_01StalledStalled", "and now this")
    ]
    assert split_replay(replay, "/other") == (None, replay)  # another workspace's session
    for other in ("the brief", "the brief\nstep 2\nstep 3"):  # another first message
        assert split_replay(f"{REPLAY_HEAD}{other}\n\n{REPLAY_TAIL}hi", "/repo")[0] is None
    injected = f"rules\n\n{REPLAY_HEAD}the brief\nuser: ok\n\n{REPLAY_TAIL}hi"
    assert replayed_first_prompt(injected) == "rules\n\nthe brief"
    real_remember(first_prompt_key("/repo", "the brief"), "session_01ShortBrief")
    # Another harness's reply before the failure: no match, whatever was recorded.
    real_remember(first_prompt_key("/repo", "the brief\nassistant: B"), "session_01OtherBrief")
    other_harness = f"{REPLAY_HEAD}the brief\nassistant: B\n\n{REPLAY_TAIL}hi"
    assert split_replay(other_harness, "/repo")[0] is None
    # Until one of its turns completes, a bridge marks every turn.
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.suppress(RuntimeError):
        run_turn("acp-stalled", "again")  # stalls once more
    retry.events.put(result | {"sequence_num": "7"})
    with contextlib.redirect_stdout(out):
        assert run_turn("acp-retry", "next") == "end_turn"
    marked_out = out.getvalue()
    assert marked_out.count(MARKER) == 1, marked_out  # acp-retry has completed a turn: no marker
    stalled.events.put(result | {"sequence_num": "8"})
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert run_turn("acp-stalled", "and now") == "end_turn"
    assert MARKER in out.getvalue(), out.getvalue()
    for quoted in ("scope: B", "assistant: quoted"):  # a line of the first message, not a turn
        assert (
            split_replay(f"{REPLAY_HEAD}the brief\n{quoted}\n\n{REPLAY_TAIL}hi", "/repo")[0]
            is None
        )
    # A session that exists stays recorded even when the next step fails.
    real_log, globals()["log"] = log, lambda _: (_ for _ in ()).throw(BrokenPipeError())
    with contextlib.suppress(BrokenPipeError):
        _create("acp-x", Session("/logs"), "go", "go")
    globals()["log"] = real_log
    assert recall().get(first_prompt_key("/logs", "go")) == "session_01StalledStalled"
    # Bridges of concurrent sessions keep each other's records.
    writers = [
        threading.Thread(target=real_remember, args=(f"k{n}", f"session_{n}")) for n in range(20)
    ]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()
    assert all(f"k{n}" in recall() for n in range(20)), recall()
    # Each turn's keepalive has ended by the time the turn returns, however fast turns follow.
    real_mirror_turn = _mirror_turn
    globals()["_mirror_turn"] = lambda *_: time.sleep(0.1) or "end_turn"
    threads_before = threading.active_count()
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(3):
            run_turn("acp-quiet", "again")
            assert threading.active_count() == threads_before, threading.enumerate()
    # A bootstrap turn that ends in an error fails the turn and sends no prompt.
    prompts: list[str] = []
    globals()["_send"] = lambda _s, text: prompts.append(text) or (1, False)
    broken = Session("/repo", "acp-broken")
    broken.reader = threading.current_thread()
    broken.mcp = {"echo": proxy}
    setup = {
        "type": "result",
        "is_error": True,
        "errors": ["Setup script failed with exit code 1."],
    }
    broken.events.put({"sequence_num": "1", "payload": setup})
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            _bootstrap("acp-broken", broken, "go")
        raise AssertionError("a failed setup sent the prompt")
    except RuntimeError as exc:
        assert str(exc) == "cloud session setup failed: Setup script failed with exit code 1.", exc
    assert prompts == [], prompts
    assert not {"acp-broken", first_prompt_key("/repo", "go")} & recall().keys(), recall()
    replaced = SESSIONS["acp-broken"]
    assert replaced is not broken and replaced.cloud_id is None and replaced.mcp is broken.mcp
    sent: list[str] = []
    globals()["interrupt"] = sent.append
    idle = Session("/")
    idle.cloud_id = "session_x"
    idle.cancel()
    busy = Session("/")
    busy.cloud_id, busy.running = "session_y", True
    busy.cancel()
    busy.cancel()
    assert sent == ["session_y"], sent
    # A bridge that is terminated leaves a running cloud turn alone. Only a cancel
    # that was asked for, and not delivered yet, goes out on the way.
    sent.clear()
    left = SESSIONS["acp-left"] = Session("/", "acp-left")
    left.cloud_id, left.running = "session_left", True
    stopping = SESSIONS["acp-stopping"] = Session("/", "acp-stopping")
    stopping.cloud_id, stopping.running = "session_stopping", True
    stopping.cancelled.set()  # ``session/cancel`` came, and SIGTERM cut its POST short
    with contextlib.suppress(SystemExit):
        stop()
    assert sent == ["session_stopping"], sent
    left.running = stopping.running = False
    # An interrupt the network drops is sent again: not lost, and not fatal to the bridge.
    attempts: list[str] = []

    def flaky_interrupt(cloud_id: str) -> None:
        attempts.append(cloud_id)
        if len(attempts) == 1:
            raise urllib.error.URLError("no route to host")

    globals()["interrupt"] = flaky_interrupt
    busy.interrupted = False
    busy.cancel()
    assert not busy.interrupted
    busy.cancel()
    assert busy.interrupted and attempts == ["session_y"] * 2, attempts
    # A new bridge reads its cloud session's log before it sends, to see what is behind.
    assert same_message("the brief", " the brief\n", "the brief")
    retried_first = f"rules\n\n{REPLAY_HEAD}the brief\n\n{REPLAY_TAIL}the brief"
    assert same_message("rules\n\nthe brief", "the brief", retried_first)  # with its system prompt
    later = f"rules\n\n{REPLAY_HEAD}the brief\nuser: go on\n\n{REPLAY_TAIL}go on"
    assert not same_message("rules\n\nthe brief", "go on", later)
    # Another message that an earlier one happens to end with is not a retry.
    assert not same_message("Print this literal text:\ncontinue", "continue", "continue")
    assert not same_message("x", "", "")

    def cloud_log(*rows: tuple[str, dict]) -> list[dict]:
        return [
            {"sequence_num": str(seq), "source": source, "payload": payload}
            for seq, (source, payload) in enumerate(rows, 1)
        ]

    def said(text: str) -> tuple[str, dict]:
        return "client", {"type": "user", "message": {"content": text}}

    def answered_with(text: str) -> tuple[str, dict]:
        block = {"type": "text", "text": text}
        return "worker", {"type": "assistant", "message": {"content": [block]}}

    def asks(request_id: str) -> tuple[str, dict]:
        request = {"subtype": "mcp_message", "server_name": "echo"}
        return "worker", {"type": "control_request", "request_id": request_id, "request": request}

    tool_output = {"type": "tool_result", "tool_use_id": "t1", "content": "x"}
    ended = ("worker", {"type": "result"})
    cloud = cloud_log(
        said("ready"),
        asks("old"),
        ended,
        said("the brief"),
        asks("done"),
        ("client", {"type": "control_response", "response": {"request_id": "done"}}),
        ("worker", {"type": "user", "message": {"content": [tool_output]}}),
        asks("open"),
    )
    globals()["fetch_events"] = lambda *_: (list(cloud), None)
    late = SESSIONS["acp-late"] = Session("/repo", "acp-late")
    late.cloud_id = "session_late"
    # Its turn still runs: behind from the message on, whatever is sent now. The
    # request an earlier turn asked and the one already answered are settled.
    assert backlog(late, "the brief", "") == (4, True, frozenset({"old", "done"}), 4)
    assert backlog(late, "continue", "") == (4, False, frozenset({"old", "done"}), 4)
    finished = cloud + cloud_log(*[ended] * 9)[8:]
    globals()["fetch_events"] = lambda *_: (finished, None)
    # A turn that ended with nobody mirroring it is behind for a retry of its message...
    assert backlog(late, "the brief", "") == (4, True, frozenset({"old", "done", "open"}), 4)
    assert backlog(late, "continue", "") is None  # ...but not for another, without a record
    real_remember("seen:session_late", "3")  # Omnigent has the turns up to the first result
    assert backlog(late, "continue", "")[:2] == (4, False)
    real_remember("seen:session_late", "9")
    assert backlog(late, "the brief", "") is None  # seen: the same text is a new message
    real_remember("seen:session_late", None)
    # A request from before the message that still waits (the MCP handshake a
    # stopped bridge never answered) is read again, from just before it.
    handshake = cloud_log(said("ready"), ended, asks("handshake"), said("go"))
    globals()["fetch_events"] = lambda *_: (handshake, None)
    assert backlog(late, "go", "") == (4, True, frozenset(), 2)
    # The whole turn: a bridge that comes while the cloud turn runs mirrors it to the
    # end, answers only the request still open, and sends nothing for a retry.
    nap = time.sleep

    def cloud_stream(_cloud_id: str, after: int) -> Iterator[dict]:
        while True:
            for event in list(cloud):
                if int(event["sequence_num"]) > after:
                    after = int(event["sequence_num"])
                    yield event
            nap(0.05)

    def cloud_send(_s: Session, text: str) -> tuple[int, bool]:
        prompts.append(text)
        rows = [*[ended] * len(cloud), said(text), answered_with(f"did: {text}"), ended]
        cloud.extend(cloud_log(*rows)[-3:])
        return len(cloud) - 2, False

    reanswered: list[str] = []
    globals()["answer_control"] = lambda _s, payload: reanswered.append(payload["request_id"])
    globals()["fetch_events"] = lambda *_: (list(cloud), None)
    globals()["stream_events"], globals()["_send"] = cloud_stream, cloud_send
    globals()["_mirror_turn"] = real_mirror_turn
    prompts.clear()
    rest = cloud_log(*[ended] * len(cloud), answered_with("first task done"), ended)[-2:]
    threading.Timer(0.5, cloud.extend, [rest]).start()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert run_turn("acp-late", "the brief") == "end_turn"
    assert "first task done" in out.getvalue() and prompts == [], (out.getvalue(), prompts)
    assert reanswered == ["open"], reanswered
    # The turn is seen only once its response has left. A bridge that died just
    # before leaves it unseen, and a retry mirrors it again without sending.
    assert "seen:session_late" not in recall(), recall()

    def prompted(acp_id: str, text: str) -> str:
        params = {"sessionId": acp_id, "prompt": [{"type": "text", "text": text}]}
        with contextlib.redirect_stdout(shown := io.StringIO()):
            handle_prompt({"id": 1, "params": params})
        return shown.getvalue()

    again = SESSIONS["acp-again"] = Session("/repo", "acp-again")
    again.cloud_id = "session_late"
    shown = prompted("acp-again", "the brief")
    assert "first task done" in shown and '"stopReason": "end_turn"' in shown, shown
    assert prompts == [] and recall()["seen:session_late"] == "10", (prompts, recall())
    # A turn that ran and ended with no bridge is shown before a new message is sent.
    cloud.extend(
        cloud_log(*[ended] * len(cloud), said("second task"), answered_with("second done"), ended)[
            -3:
        ]
    )
    after_outage = SESSIONS["acp-after"] = Session("/repo", "acp-after")
    after_outage.cloud_id = "session_late"
    shown = prompted("acp-after", "continue")
    assert prompts == ["continue"] and recall()["seen:session_late"] == "16", (prompts, recall())
    assert -1 < shown.find("second done") < shown.find("did: continue"), shown
    # The same bridge, after a turn of its own failed with the message already
    # posted: the retry mirrors that turn and does not post the message again.
    after_outage.caught_up = False
    cloud.extend(
        cloud_log(*[ended] * len(cloud), said("third task"), answered_with("third done"), ended)[
            -3:
        ]
    )
    shown = prompted("acp-after", "third task")
    assert "third done" in shown and prompts == ["continue"], (shown, prompts)
    assert recall()["seen:session_late"] == "19", recall()
    # A catch-up that worked, then a send that failed: the turn failed, so Omnigent
    # dropped what it showed. The retry in the same bridge shows again what the
    # failed turn took off the queue, and then sends.
    after_outage.caught_up = False
    cloud.extend(
        cloud_log(*[ended] * len(cloud), said("fourth task"), answered_with("fourth done"), ended)[
            -3:
        ]
    )

    def no_route(*_: object) -> tuple[int, bool]:
        globals()["_send"] = cloud_send
        raise OSError("no route to host")

    globals()["_send"] = no_route
    shown = prompted("acp-after", "fifth task")
    assert "fourth done" in shown and "no route to host" in shown, shown
    assert recall()["seen:session_late"] == "19", recall()
    shown = prompted("acp-after", "fifth task")
    assert -1 < shown.find("fourth done") < shown.find("did: fifth task"), shown
    assert prompts == ["continue", "fifth task"], prompts
    # A turn the cloud cut off (a new CLI started mid-turn, then silence) has no
    # result. It ends as cut off, and the bridge asks the session to continue:
    # once in a turn of its own, and in place of a retried message.
    globals()["CUT_OFF_GRACE_S"] = 1
    short_stall, globals()["STALL_TIMEOUT_S"] = STALL_TIMEOUT_S, 60
    new_cli = ("worker", {"type": "system", "subtype": "turn_handoff_available"})

    def cut_off_once(_s: Session, text: str) -> tuple[int, bool]:
        globals()["_send"] = cloud_send
        prompts.append(text)
        rows = [*[ended] * len(cloud), said(text), answered_with("half done"), new_cli]
        cloud.extend(cloud_log(*rows)[-3:])
        return len(cloud) - 2, False

    globals()["_send"] = cut_off_once
    prompts.clear()
    shown = prompted("acp-after", "sixth task")
    assert prompts == ["sixth task", CONTINUE_PROMPT], (prompts, shown)
    assert -1 < shown.find("half done") < shown.find("cut the turn off") < shown.find("did: "), (
        shown
    )
    rows = [*[ended] * len(cloud), said("seventh task"), answered_with("half again"), new_cli]
    cloud.extend(cloud_log(*rows)[-3:])
    retried = SESSIONS["acp-cut"] = Session("/repo", "acp-cut")
    retried.cloud_id = "session_late"
    prompts.clear()
    shown = prompted("acp-cut", "seventh task")
    assert prompts == [CONTINUE_PROMPT] and "half again" in shown, (prompts, shown)
    assert backlog(retried, "seventh task", "") is None  # seen to the continued turn's end
    # A retried message's continuation that is cut off too fails: one continuation each.
    rows = [*[ended] * len(cloud), said("eighth task"), answered_with("half 8"), new_cli]
    cloud.extend(cloud_log(*rows)[-3:])
    globals()["_send"] = cut_off_once  # the continuation is cut off, a second would end
    again_cut = SESSIONS["acp-cut2"] = Session("/repo", "acp-cut2")
    again_cut.cloud_id = "session_late"
    prompts.clear()
    shown = prompted("acp-cut2", "eighth task")
    assert prompts == [CONTINUE_PROMPT] and "restarted mid-turn twice" in shown, (prompts, shown)

    # A new CLI mid-turn is no cut-off while the turn shows life after it (a tool's
    # progress), or while this host still answers a request of it.
    def turn_survives(sign_of_life: list[tuple[str, dict]], answering: bool) -> None:
        def send(_s: Session, text: str) -> tuple[int, bool]:
            prompts.append(text)
            rows = [said(text), answered_with("part"), new_cli, *sign_of_life]
            cloud.extend(cloud_log(*[ended] * len(cloud), *rows)[-len(rows) :])
            sent = len(cloud) - len(rows) + 1
            if answering:
                retried.answering.add("held")
            late = cloud_log(*[ended] * len(cloud), answered_with("whole"), ended)[-2:]
            threading.Timer(
                2.5, lambda: (retried.answering.discard("held"), cloud.extend(late))
            ).start()
            return sent, False

        globals()["_send"] = send
        prompts.clear()
        shown = prompted("acp-cut", "a long task")
        assert prompts == ["a long task"] and "whole" in shown, (prompts, shown)
        assert "cut the turn off" not in shown, shown

    turn_survives([("worker", {"type": "tool_progress"})], answering=False)
    turn_survives([], answering=True)
    # A first turn that is cut off is continued as any other.
    cloud.clear()

    def create_cut_off(_acp_id: str, fresh: Session, first: str, _text: str) -> None:
        fresh.cloud_id = "session_late"
        cloud.extend(cloud_log(said(first), answered_with("half first"), new_cli))

    real_create, globals()["_create"] = _create, create_cut_off
    globals()["_send"] = cloud_send
    prompts.clear()
    SESSIONS["acp-first"] = Session("/repo", "acp-first")
    shown = prompted("acp-first", "a first task")
    assert prompts == [CONTINUE_PROMPT] and '"stopReason": "end_turn"' in shown, (prompts, shown)
    globals()["_create"] = real_create
    globals()["STALL_TIMEOUT_S"] = short_stall
    # A tool call runs once across bridges: its result is kept until delivered,
    # and a call cut short by a restart fails instead of running again.
    ran: list[dict] = []

    def tool(message: dict) -> dict:
        ran.append(message)
        return {"jsonrpc": "2.0", "id": message["id"], "result": len(ran)}

    call = {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "send"}}
    assert once("req-1", call, tool)["result"] == 1
    assert once("req-1", call, tool)["result"] == 1 and len(ran) == 1  # answer lost, read again
    _answer_file("req-1").write_text("")  # the bridge stopped while it ran
    assert "may have taken effect" in once("req-1", call, tool)["error"]["message"]
    _answer_file("req-1").write_text('{"jsonrpc": "2.0", "resu')  # a reply cut short
    assert "may have taken effect" in once("req-1", call, tool)["error"]["message"]
    assert len(ran) == 1
    # A delivered result stays a day: a bridge that listed the open requests before
    # the answer arrived reads the request again, and must not run the call again.
    sent_to: list[dict] = []
    real_deliver, globals()["deliver"] = deliver, lambda *args: sent_to.append(args[2])
    late_reader = Session("/repo", "acp-two")
    late_reader.cloud_id, late_reader.mcp = (
        "session_two",
        {"omnigent": type("P", (), {"handle": staticmethod(tool)})},
    )
    asked = {
        "request_id": "req-3",
        "request": {"subtype": "mcp_message", "server_name": "omnigent", "message": call},
    }
    real_answer_control(late_reader, asked)
    real_answer_control(late_reader, asked)  # the other bridge, from its older view of the log
    assert len(ran) == 2 and sent_to[0] == sent_to[1], (ran, sent_to)
    globals()["deliver"] = real_deliver
    forget_sent_answers()
    assert _answer_file("req-3").exists()  # sent just now
    old = time.time() - SENT_KEPT_S - 60
    os.utime(_answer_file("req-3").with_suffix(".sent"), (old, old))
    forget_sent_answers()
    assert not _answer_file("req-3").exists() and _answer_file("req-1").exists()  # unsent stays
    tries: list[str] = []

    def flaky_respond(_session: str, request_id: str, _response: dict) -> None:
        tries.append(request_id)
        if len(tries) < 3:
            raise urllib.error.URLError("no route")

    real_respond, globals()["respond"] = respond, flaky_respond
    real_nap, time.sleep = time.sleep, lambda _s: None
    deliver("session_x", "req-9", {})
    time.sleep, globals()["respond"] = real_nap, real_respond
    assert tries == ["req-9"] * 3, tries
    listing = {"jsonrpc": "2.0", "id": 10, "method": "tools/list"}
    assert [once("req-2", listing, tool)["result"] for _ in range(2)] == [3, 4]
    # No network is not a stall: the turn waits, and fails only when the session
    # can be reached and stays silent. The reason the stream gives is in the error.
    assert unreachable(urllib.error.URLError("no route")) and unreachable(TimeoutError())
    assert unreachable(http.client.IncompleteRead(b""))
    refused = urllib.error.HTTPError("https://x", 401, "Unauthorized", {}, None)
    assert not unreachable(refused) and not unreachable(RuntimeError("login expired"))
    globals()["STALL_TIMEOUT_S"] = 0.5
    away = Session("/", "acp-away")
    away.cloud_id, away.stream_error = "session_away", urllib.error.URLError("no route")
    waiting = away.events_while_alive()
    assert [next(waiting) for _ in range(3)] == [None] * 3 and away.offline
    away.stream_error = refused
    try:
        next(waiting)
        raise AssertionError("a session that refuses its stream never stalled")
    except RuntimeError as exc:
        assert "the stream fails: HTTP Error 401" in str(exc), exc
    # Time the host slept is not the cloud's silence: a clock that jumps hours
    # between two polls counts as one poll.
    real_time, jumps = time, itertools.count(0, 10_000)
    globals()["STALL_TIMEOUT_S"] = STALL_STEP_S * 2.5
    globals()["time"] = type("T", (), {"monotonic": staticmethod(lambda: next(jumps))})
    slept = Session("/", "acp-slept")
    slept.cloud_id = "session_slept"
    waiting = slept.events_while_alive()
    try:
        assert [next(waiting), next(waiting)] == [None, None]
        try:
            next(waiting)
            raise AssertionError("a silent session never stalled")
        except RuntimeError as exc:
            assert "made no progress" in str(exc), exc
    finally:
        globals()["time"] = real_time
    globals()["stream_events"] = real_stream_events
    streams = [
        [b":keepalive\n", b'data: {"sequence_num": "7", "payload": {"type": "user"}}\n'],
        urllib.error.URLError("network is unreachable"),
        [b'data: {"sequence_num": "8", "payload": {"type": "result"}}\n'],
    ]
    urls: list[str] = []
    naps: list[float] = []

    class FakeResponse(list):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_urlopen(req, **_):
        urls.append(req.full_url)
        if isinstance(stream := streams.pop(0), Exception):
            raise stream
        return FakeResponse(stream)

    globals()["access_token"] = lambda: "token"
    urllib.request.urlopen, real_urlopen = fake_urlopen, urllib.request.urlopen
    stub_time = {"sleep": staticmethod(naps.append), "strftime": staticmethod(time.strftime)}
    globals()["time"] = type("T", (), stub_time)
    try:
        seen = list(itertools.islice(stream_events("s", 6), 6))
    finally:
        urllib.request.urlopen = real_urlopen
    # A keepalive, an event, the stream's end, a drop (the error itself), then on.
    assert [e["sequence_num"] if isinstance(e, dict) else e for e in seen] == [
        None,
        "7",
        None,
        seen[3],
        "8",
        None,
    ], seen
    assert unreachable(seen[3]), seen
    # Reconnects after the last seen, waiting longer each time until a line arrives.
    assert [u.rsplit("=", 1)[1] for u in urls] == ["6", "7", "7"], urls
    assert naps == [1, 2], naps
    globals()["time"] = real_time
    check_archive_and_title()
    print("self-check ok")


def check_archive_and_title() -> None:
    """``--archive`` and ``--title`` make the CLI's own calls, on the session a target names."""
    calls: list[tuple[str, dict | None, str | None]] = []
    already: set[str] = set()

    def fake_api(path: str, body: dict | None = None, method: str | None = None) -> dict:
        calls.append((path, body, method))
        if path.endswith("/archive") and path in already:
            raise urllib.error.HTTPError(path, 409, "Conflict", {}, None)  # type: ignore[arg-type]
        already.add(path)
        return {}

    real_api, globals()["call_api"] = call_api, fake_api
    real_out = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        transcript = io.StringIO(
            f"user: go\nassistant: {MARKER}session_0123456789ab\n\n"
            f"user: again\nassistant: {MARKER}session_ABCDEFGHIJKL\n\nwork\n"
        )
        url = "https://claude.ai/code/cse_0123456789ab"
        assert command(["--archive", "-", url], transcript) == 0
        assert command(["--archive", "session_ABCDEFGHIJKL"], io.StringIO()) == 0  # archived
        assert command(["--title", "175-worker", url], io.StringIO()) == 0
        assert calls == [
            ("/v1/code/sessions/session_ABCDEFGHIJKL/archive", {}, None),  # the last marker
            ("/v1/code/sessions/cse_0123456789ab/archive", {}, None),
            ("/v1/code/sessions/session_ABCDEFGHIJKL/archive", {}, None),
            ("/v1/code/sessions/cse_0123456789ab", {"title": "175-worker"}, "PUT"),
        ], calls
        assert sys.stdout.getvalue().count("archived ") == 3, sys.stdout.getvalue()
        calls.clear()
        # A target that names no session archives nothing, not even the ones before it.
        assert command(["--archive", url, "-"], io.StringIO("user: hi\n")) == 1
        assert command(["--archive", "not-a-session"], io.StringIO()) == 1
        assert calls == [] and "names no cloud session" in sys.stderr.getvalue()

        def refused(path: str, *_: object, **__: object) -> dict:
            raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)  # type: ignore[arg-type]

        globals()["call_api"] = refused
        assert command(["--archive", url], io.StringIO()) == 1
        assert command(["--title", "t", url], io.StringIO()) == 1
        # Run as a script, either flag reaches the command, before any login is read.
        for flag in ("--archive", "--title"):
            args = [flag, "x", "-"] if flag == "--title" else [flag, "not-a-session"]
            ran = subprocess.run(
                [sys.executable, __file__, *args], input="", capture_output=True, text=True
            )
            assert ran.returncode == 1 and "claude_cloud_acp.py:" in ran.stderr, ran
    finally:
        globals()["call_api"] = real_api
        sys.stdout, sys.stderr = real_out
    # The API answers an archive or a rename with an empty body or with JSON.
    sent: list[urllib.request.Request] = []
    replies = [b"", b'{"title": "t"}']

    def fake_urlopen(
        req: urllib.request.Request, **_: object
    ) -> contextlib.AbstractContextManager:
        sent.append(req)
        return contextlib.nullcontext(io.BytesIO(replies.pop(0)))

    real_urlopen, urllib.request.urlopen = urllib.request.urlopen, fake_urlopen
    try:
        assert call_api("/v1/code/sessions/s/archive", {}) == {}
        assert call_api("/v1/code/sessions/s", {"title": "t"}, method="PUT") == {"title": "t"}
    finally:
        urllib.request.urlopen = real_urlopen
    assert [r.get_method() for r in sent] == ["POST", "PUT"], sent


def cloud_session_of(target: str, stdin: io.TextIOBase) -> str:
    """The cloud session *target* names: its id, its claude.ai URL, or ``-`` for text on
    *stdin* with the bridge's ``cloud session:`` line (an Omnigent session's transcript),
    whose last such line counts, as on a resume."""
    if target == "-":
        ids = MARKER_RE.findall(stdin.read())
        if not ids:
            raise ValueError(f"no '{MARKER}<id>' line on stdin")
        return ids[-1]
    if match := SESSION_ID_RE.search(target):
        return match.group(1)
    raise ValueError(f"{target!r} names no cloud session")


def command(argv: list[str], stdin: io.TextIOBase = sys.stdin) -> int:
    """``--archive TARGET...`` or ``--title TITLE TARGET``: what ends or names an Omnigent session
    calls on its cloud session, since the bridge cannot tell a deleted session from a cancel."""
    import argparse

    parser = argparse.ArgumentParser(prog="claude_cloud_acp.py")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--archive", nargs="+", metavar="TARGET")
    action.add_argument("--title", nargs=2, metavar=("TITLE", "TARGET"))
    args = parser.parse_args(argv)
    try:
        if args.title:
            title, target = args.title
            cloud_id = cloud_session_of(target, stdin)
            set_title(cloud_id, title)
            print(f"titled {cloud_id}")
            return 0
        cloud_ids = [cloud_session_of(target, stdin) for target in args.archive]
        for cloud_id in cloud_ids:
            archive(cloud_id)
            print(f"archived {cloud_id}")
    except (ValueError, OSError, http.client.HTTPException, RuntimeError) as exc:
        print(f"claude_cloud_acp.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        self_check()
    elif {"--archive", "--title"} & set(sys.argv):
        sys.exit(command(sys.argv[1:]))
    else:
        serve()
