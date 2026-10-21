#!/usr/bin/env python3
"""ACP bridge: one Omnigent session drives one Claude Code cloud session.

The agent runs on Anthropic's infrastructure (claude.ai/code), not on this
machine. The bridge is a stdio Agent Client Protocol server, so Omnigent runs
it as a plain ``acp:<slug>`` agent:

* the first ``session/prompt`` creates the cloud session with
  ``claude --cloud "<prompt>"`` on a pseudo-terminal from the session's cwd
  (the cloud VM clones that checkout's GitHub remote and branch);
* later prompts are queued with ``claude -p "<prompt>" --cloud <id>``;
* the transcript is read back from ``GET /v1/code/sessions/{id}/events`` with
  the Claude CLI's own claude.ai login, and mirrored as ACP ``session/update``
  notifications until the turn's ``result`` event.

Standard library only. Run ``python3 claude_cloud_acp.py --self-check`` to
exercise the event mapping offline.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import pwd
import re
import select
import struct
import subprocess
import sys
import termios
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = os.environ.get("CLAUDE_CLOUD_API_BASE", "https://api.anthropic.com")
CLAUDE_CLI = os.environ.get("CLAUDE_CLOUD_CLI", "claude")
POLL_S = float(os.environ.get("CLAUDE_CLOUD_POLL_S", "2"))
TURN_TIMEOUT_S = float(os.environ.get("CLAUDE_CLOUD_TURN_TIMEOUT_S", "3600"))
# Omnigent's agent env allowlist drops USER, but the CLI's keychain lookup keys on it.
os.environ.setdefault("USER", pwd.getpwuid(os.getuid()).pw_name)
CLI_TIMEOUT_S = 300
PROTOCOL_VERSION = 1
SESSION_ID_RE = re.compile(r"\b((?:session|cse)_[A-Za-z0-9]{10,})")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")

_write_lock = threading.Lock()


def send(msg: dict) -> None:
    with _write_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def log(text: str) -> None:
    print(f"[claude-cloud-acp] {text}", file=sys.stderr, flush=True)


# ── claude.ai credentials and the events API ───────────────


def access_token() -> str:
    """Return the Claude CLI's current claude.ai access token.

    Read from where the CLI itself keeps it: the login keychain item
    ``Claude Code-credentials`` for ``$USER`` on macOS, else
    ``~/.claude/.credentials.json``. The CLI refreshes the token whenever it
    runs, and every turn runs it to create or message the session.
    ponytail: only the default config dir (no CLAUDE_CONFIG_DIR suffix), and
    no refresh of our own; a turn that outlives the token fails its poll.
    """
    if token := os.environ.get("CLAUDE_CLOUD_TOKEN"):
        return token
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
    return json.loads(raw)["claudeAiOauth"]["accessToken"]


def fetch_events(session_id: str, cursor: str | None) -> tuple[list[dict], str | None]:
    """Fetch every event after *cursor*; return them and the new tail cursor."""
    events: list[dict] = []
    while True:
        query = "sort_order=asc" + (f"&cursor={cursor}" if cursor else "")
        req = urllib.request.Request(
            f"{API_BASE}/v1/code/sessions/{session_id}/events?{query}",
            headers={
                "Authorization": f"Bearer {access_token()}",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "ccr-byoc-2025-07-29",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            page = json.load(resp)
        events.extend(page.get("data") or [])
        if page.get("next_cursor"):
            cursor = page["next_cursor"]
            continue
        return events, page.get("resume_cursor") or cursor


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


def run_cli(args: list[str], cwd: str) -> str:
    proc = subprocess.run(
        [CLAUDE_CLI, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_S,
        check=False,
    )
    out = proc.stdout + proc.stderr
    if proc.returncode != 0:
        raise RuntimeError(
            f"`claude {args[0]} ...` exited {proc.returncode}: {out.strip()[-800:]}"
        )
    return out


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
    # ponytail: sub-agent traffic (parent_tool_use_id set) is dropped, not
    # mirrored as child sessions.
    if payload.get("parent_tool_use_id"):
        return [], False
    if kind == "result":
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
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": block["text"]},
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
                    "title": block.get("name"),
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
    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.cloud_id: str | None = None
        self.cursor: str | None = None
        self.cancelled = threading.Event()


SESSIONS: dict[str, Session] = {}


def prompt_text(blocks: list[dict]) -> str:
    return "\n\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


def run_turn(acp_id: str, text: str) -> str:
    s = SESSIONS[acp_id]
    s.cancelled.clear()
    if s.cloud_id is None:
        s.cloud_id = create_cloud_session(text, s.cwd)
        log(f"created {s.cloud_id}: https://claude.ai/code/{s.cloud_id}")
        send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": acp_id,
                    "update": {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": {
                            "type": "text",
                            "text": f"[cloud] https://claude.ai/code/{s.cloud_id}\n",
                        },
                    },
                },
            }
        )
    else:
        _, s.cursor = fetch_events(s.cloud_id, s.cursor)  # skip anything since the last turn
        run_cli(["-p", text, "--cloud", s.cloud_id], s.cwd)
    deadline = time.monotonic() + TURN_TIMEOUT_S
    while time.monotonic() < deadline:
        if s.cancelled.is_set():
            # ponytail: the cloud turn keeps running; the CLI has no interrupt.
            return "cancelled"
        try:
            events, s.cursor = fetch_events(s.cloud_id, s.cursor)
        except urllib.error.URLError as exc:
            log(f"poll failed, retrying: {exc}")
            events = []
        for event in events:
            updates, done = to_updates(event.get("payload") or {})
            for update in updates:
                send(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {"sessionId": acp_id, "update": update},
                    }
                )
            if done:
                return "end_turn"
        time.sleep(POLL_S)
    raise RuntimeError(f"no result from cloud session {s.cloud_id} within {TURN_TIMEOUT_S:.0f}s")


def handle_prompt(msg: dict) -> None:
    params = msg.get("params") or {}
    try:
        stop = run_turn(params["sessionId"], prompt_text(params.get("prompt") or []))
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": stop}})
    except Exception as exc:  # noqa: BLE001 — every failure goes back to the client
        log(f"turn failed: {exc}")
        send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32000, "message": str(exc)}})


def serve() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        method, params, mid = msg.get("method"), msg.get("params") or {}, msg.get("id")
        if method == "initialize":
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
            acp_id = f"ccloud-{len(SESSIONS) + 1}-{int(time.time())}"
            SESSIONS[acp_id] = Session(params.get("cwd") or os.getcwd())
            log(f"session {acp_id} cwd={SESSIONS[acp_id].cwd}")
            send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": acp_id}})
        elif method == "session/prompt":
            threading.Thread(target=handle_prompt, args=(msg,), daemon=True).start()
        elif method == "session/cancel":
            if s := SESSIONS.get(params.get("sessionId", "")):
                s.cancelled.set()
        elif mid is not None:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {"code": -32601, "message": f"method not supported: {method}"},
                }
            )


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
    assert to_updates(
        {
            "type": "assistant",
            "parent_tool_use_id": "x",
            "message": {"content": [{"type": "text", "text": "sub"}]},
        }
    ) == ([], False)
    assert to_updates({"type": "user", "message": {"content": "plain prompt echo"}}) == ([], False)
    screen = "\x1b[32m✓\x1b[0m Created cloud session \x1b]8;;x\x07https://claude.ai/code/session_01DiUkqY2kzbUb"
    assert SESSION_ID_RE.search(ANSI_RE.sub("", screen)).group(1) == "session_01DiUkqY2kzbUb"
    print("self-check ok")


if __name__ == "__main__":
    self_check() if "--self-check" in sys.argv else serve()
