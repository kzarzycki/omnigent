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
import io
import itertools
import json
import os
import pty
import pwd
import queue
import re
import select
import signal
import struct
import subprocess
import sys
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
TURN_TIMEOUT_S = float(os.environ.get("CLAUDE_CLOUD_TURN_TIMEOUT_S", "3600"))
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
_last_refresh = 0.0
PROTOCOL_VERSION = 1
SESSION_ID_RE = re.compile(r"\b((?:session|cse)_[A-Za-z0-9]{10,})")
MARKER = "cloud session: https://claude.ai/code/"
MARKER_RE = re.compile(re.escape(MARKER) + r"((?:session|cse)_[A-Za-z0-9]{10,})")
# The tail of the transcript Omnigent's ACP executor replays into the first
# prompt of a fresh session (``AcpExecutor._history_prefix``).
# ACP session -> cloud session, for an Omnigent that re-prompts a respawned
# bridge with the old ACP session id instead of opening a new one.
STATE_FILE = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / (
    "claude-cloud-acp/sessions.json"
)
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
    ponytail: time-based, not proof the cloud is progressing; TURN_TIMEOUT_S bounds a stuck turn.
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


def call_api(path: str, body: dict | None = None) -> dict:
    """GET *path*, or POST *body* to it, on the sessions API; return the JSON reply."""
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers=_headers(),
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


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


def stream_events(session_id: str, after: int) -> Iterator[dict | None]:
    """Yield the session's events after sequence number *after*, live, without end.

    Yields None for keepalives and dropped connections, so the caller can check
    its deadline. Reconnects from the last event seen.
    """
    while True:
        req = urllib.request.Request(
            f"{API_BASE}/v1/code/sessions/{session_id}/events/stream?from_sequence_num={after}",
            headers={**_headers(), "Accept": "text/event-stream"},
        )
        try:
            with urllib.request.urlopen(req, timeout=STREAM_IDLE_S) as resp:
                for raw in resp:
                    line = raw.decode()
                    # ponytail: one-line ``data:`` fields only, which is all the server sends.
                    event = json.loads(line[5:]) if line.startswith("data:") else {}
                    if "sequence_num" in event:
                        after = int(event["sequence_num"])
                        yield event
                    else:
                        yield None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(f"stream dropped after {after}, reconnecting: {exc}")
        yield None
        time.sleep(1)


def create_cloud_session(text: str, cwd: str) -> str:
    """Start ``claude --cloud "<text>"`` and return the new session id.

    Creating a cloud session is interactive-only in the CLI (``--cloud`` refuses
    ``--print``), so run it on a pseudo-terminal, read the id it prints, and
    close the local view; the cloud session keeps running without it.
    """
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
    proc = subprocess.Popen(
        [CLAUDE_CLI, "--cloud", text],
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
        reply = proxy.handle(request.get("message") or {})
        respond(s.cloud_id, request_id, {"mcp_response": reply})
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
        respond(s.cloud_id, request_id, decision)


# ── event mapping ──────────────────────────────────────────


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            _text_of(b.get("text", b.get("content", ""))) for b in content if isinstance(b, dict)
        )
    return ""


def to_updates(payload: dict) -> tuple[list[dict], bool]:
    """Map one cloud-session event payload to ACP updates; True ends the turn."""
    kind = payload.get("type")
    # Sub-agent steps carry the id of the Agent tool call that started them.
    # ponytail: mirrored inline with a label, not as nested cards; ACP has no nesting.
    sub = "[sub-agent] " if payload.get("parent_tool_use_id") else ""
    if kind == "result" and not sub:
        return [], True
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
        self.mcp: dict[str, McpProxy] = {}
        self.mcp_registered = False
        self.events: queue.Queue[dict] = queue.Queue()
        self.reader: threading.Thread | None = None
        self.cancelled = threading.Event()
        self.running = False
        self.interrupted = False
        self.lock = threading.RLock()  # re-entered by the SIGTERM handler

    def follow(self, after: int) -> None:
        """Read the cloud session's stream from *after* for the rest of the bridge's life.

        Control requests are answered as they arrive, between turns too: the CLI
        handshakes with MCP servers whenever it (re)connects, and gives up quickly.
        The rest goes to :attr:`events` for the running turn.
        """
        if self.reader is not None:
            return

        def read() -> None:
            for event in stream_events(self.cloud_id, after):
                payload = (event or {}).get("payload") or {}
                if payload.get("type") == "control_request":
                    # Off the reader: a tool call can outlast the CLI's wait for other events.
                    threading.Thread(
                        target=answer_control,
                        args=(self, payload),
                        daemon=True,
                    ).start()
                elif event is not None:
                    self.events.put(event)

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def cancel(self) -> None:
        """Interrupt the running cloud turn, once.

        Sent right away, not on the next poll: Omnigent terminates the bridge
        straight after ``session/cancel``.
        """
        self.cancelled.set()
        with self.lock:
            if self.running and self.cloud_id and not self.interrupted:
                log(f"interrupting {self.cloud_id}")
                interrupt(self.cloud_id)
                # Only once delivered: a SIGTERM during the POST aborts it, and
                # the handler re-entering here sends it again.
                self.interrupted = True


SESSIONS: dict[str, Session] = {}


def remember(acp_id: str, cloud_id: str) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    known = recall()
    known[acp_id] = cloud_id
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


def split_replay(text: str) -> tuple[str | None, str]:
    """Return the cloud session a replayed transcript names, and the message to send.

    Without a marker the text is a fresh request and goes out whole. With one,
    the cloud session already holds the conversation, so only the latest user
    message is sent.
    """
    ids = MARKER_RE.findall(text)
    if not ids:
        return None, text
    latest = text.rsplit(REPLAY_TAIL, 1)[-1] if REPLAY_TAIL in text else text
    latest = "\n".join(line for line in latest.splitlines() if MARKER not in line).strip()
    return ids[-1], latest


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
        return _mirror_turn(acp_id, s, text)
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


def _mirror_turn(acp_id: str, s: Session, text: str) -> str:
    if s.cloud_id is None:
        s.cloud_id, text = split_replay(text)
        if s.cloud_id is None:
            if s.mcp:
                after = _bootstrap(acp_id, s, text)
            else:
                s.cloud_id = create_cloud_session(text, s.cwd)
                log(f"created {s.cloud_id}")
                announce(acp_id, s)
                after = 0
        else:
            log(f"re-attaching to {s.cloud_id}")
            announce(acp_id, s)
            after, unarchived = _send(s, text)
            if unarchived:
                thought(
                    acp_id,
                    "[cloud] unarchived the session; files from before the archive are gone\n",
                )
    else:
        after, unarchived = _send(s, text)
        if unarchived:
            thought(
                acp_id, "[cloud] unarchived the session; files from before the archive are gone\n"
            )
    s.follow(after)
    deadline = time.monotonic() + TURN_TIMEOUT_S
    while time.monotonic() < deadline:
        if s.cancelled.is_set():
            # A cancel that came before the session existed. Keep mirroring after
            # it: the interrupted turn still ends with a ``result`` event.
            s.cancel()
        try:
            event = s.events.get(timeout=1)
        except queue.Empty:
            continue
        if int(event["sequence_num"]) <= after:
            continue  # from before this turn's message
        updates, done = to_updates(event.get("payload") or {})
        for upd in updates:
            update(acp_id, upd)
        if done:
            return "cancelled" if s.interrupted else "end_turn"
    raise RuntimeError(f"no result from cloud session {s.cloud_id} within {TURN_TIMEOUT_S:.0f}s")


# The CLI starts a new session on its first prompt, before servers can be registered.
# ponytail: a throwaway first turn; it shows in the cloud transcript.
BOOTSTRAP_PROMPT = "Reply with the single word: ready"


def _bootstrap(acp_id: str, s: Session, text: str) -> int:
    """Create the cloud session, register MCP servers, then post *text*; return its seq."""
    s.cloud_id = create_cloud_session(BOOTSTRAP_PROMPT, s.cwd)
    log(f"created {s.cloud_id}")
    announce(acp_id, s)
    s.follow(0)
    deadline = time.monotonic() + TURN_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            payload = s.events.get(timeout=1).get("payload") or {}
        except queue.Empty:
            continue
        if payload.get("type") == "env_manager_log":
            for upd in to_updates(payload)[0]:
                update(acp_id, upd)
        elif to_updates(payload)[1]:
            break
    after, _ = _send(s, text)
    return after


def _send(s: Session, text: str) -> tuple[int, bool]:
    """Post the prompt, registering MCP servers first when this bridge hasn't yet."""
    if not s.mcp or s.mcp_registered:
        return send_message(s.cloud_id, text)
    sent = register_mcp(s.cloud_id, s.mcp)
    s.mcp_registered = True
    send_message(s.cloud_id, text)
    return sent


def handle_prompt(msg: dict) -> None:
    params = msg.get("params") or {}
    try:
        stop = run_turn(params["sessionId"], prompt_text(params.get("prompt") or []))
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": stop}})
    except Exception as exc:  # noqa: BLE001 — every failure goes back to the client
        log(f"turn failed: {exc}")
        send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32000, "message": str(exc)}})


def stop(*_: object) -> None:
    """On SIGTERM or EOF, interrupt any running cloud turn; nobody is left to mirror it."""
    log("stopping")
    for session in SESSIONS.values():
        session.cancel()
    sys.exit(0)


def serve() -> None:
    signal.signal(signal.SIGTERM, stop)
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
            send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": acp_id}})
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
    assert split_replay("fix the bug") == (None, "fix the bug")
    replay = (
        "Conversation so far:\nuser: hi\nassistant: done\n\n"
        f"{MARKER}session_01AbCdEfGhIjKl\n\n{REPLAY_TAIL}and now this?"
    )
    assert split_replay(replay) == ("session_01AbCdEfGhIjKl", "and now this?")
    adopt = f"{MARKER}session_01AbCdEfGhIjKl\ncontinue the migration"
    assert split_replay(adopt) == ("session_01AbCdEfGhIjKl", "continue the migration")
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
    finally:
        subprocess.run = real_run
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
    globals()["remember"] = lambda *_: None
    globals()["KEEPALIVE_S"] = 0.5
    quiet = SESSIONS["acp-quiet"] = Session("/", "acp-quiet")
    quiet.cloud_id, quiet.reader = "session_q", threading.current_thread()
    result = {"sequence_num": "2", "payload": {"type": "result"}}
    threading.Timer(5, quiet.events.put, [result]).start()
    with contextlib.redirect_stdout(out):
        assert run_turn("acp-quiet", "sleep 420") == "end_turn"
    assert before_events[0] >= 1 and out.getvalue().count('"session/update"') >= 3, out.getvalue()
    assert not any(t.name == "keepalive" for t in threading.enumerate())  # ended with its turn
    fresh = SESSIONS["acp-fresh"] = Session("/", "acp-fresh")
    fresh.reader = threading.current_thread()
    globals()["TURN_TIMEOUT_S"] = 0.1
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.suppress(RuntimeError):
        run_turn("acp-fresh", "start")
    assert f"{MARKER}session_01NewNewNewNew" in out.getvalue(), out.getvalue()
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
    streams = [
        [b":keepalive\n", b'data: {"sequence_num": "7", "payload": {"type": "user"}}\n'],
        [b'data: {"sequence_num": "8", "payload": {"type": "result"}}\n'],
    ]
    urls: list[str] = []

    class FakeResponse(list):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_urlopen(req, **_):
        urls.append(req.full_url)
        return FakeResponse(streams.pop(0))

    globals()["access_token"] = lambda: "token"
    urllib.request.urlopen, real_urlopen = fake_urlopen, urllib.request.urlopen
    globals()["time"] = type("T", (), {"sleep": staticmethod(lambda _: None)})
    try:
        seen = [e and e["sequence_num"] for e in itertools.islice(stream_events("s", 6), 5)]
    finally:
        urllib.request.urlopen = real_urlopen
    assert seen == [None, "7", None, "8", None], seen
    assert [u.rsplit("=", 1)[1] for u in urls] == ["6", "7"], (
        urls
    )  # reconnects after the last seen
    print("self-check ok")


if __name__ == "__main__":
    self_check() if "--self-check" in sys.argv else serve()
