#!/usr/bin/env python3
"""Live e2e scenarios for the claude-cloud ACP bridge, driven as an ACP client.

Each scenario starts real cloud sessions on the signed-in claude.ai account.

    python3 e2e.py turns    <git checkout>   # create, tool call, follow-up
    python3 e2e.py restart  <git checkout>   # a new bridge re-attaches from the transcript
    python3 e2e.py respawn  <git checkout>   # a respawned bridge keeps its ACP session id
    python3 e2e.py archived <git checkout>   # a turn on an archived session unarchives it
    python3 e2e.py cancel   <git checkout>   # cancel interrupts the running cloud turn
    python3 e2e.py teardown <git checkout>   # cancel then kill, as Omnigent's Esc does
    python3 e2e.py subagent <git checkout>   # sub-agent steps are mirrored with a label
    python3 e2e.py mcp      <git checkout>   # the cloud agent calls an MCP server on this host
    python3 e2e.py untrusted                 # a folder Claude Code has not trusted fails clearly

A scenario that checks an outcome exits non-zero when it doesn't hold.
"""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import claude_cloud_acp as bridge  # noqa: E402

# What Omnigent's ACP executor sends a fresh session after a restart.
REPLAY = "Conversation so far:\n{history}\n\n" + bridge.REPLAY_TAIL + "{latest}"


LOOP = (
    "Run this with Bash in the foreground and report the last line it printed:\n"
    'python3 -c "import time\nfor i in range(60): print(i, flush=True); time.sleep(1)\n'
    "print('loop-finished')\""
)


def check(ok: bool, what: str) -> None:
    print(f"\n\033[{'32mPASS' if ok else '31mFAIL'}\033[0m {what}")
    if not ok:
        raise SystemExit(1)


class Client:
    """One bridge process, spoken to over ACP stdio like Omnigent does."""

    def __init__(self, cwd: str, session: str | None = None, mcp: list | None = None) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(HERE / "claude_cloud_acp.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.next_id = 0
        self.cloud_url = None
        self.answer = ""
        self.thoughts: list[str] = []
        self.tools: list[str] = []
        self.call("initialize", {"protocolVersion": 1})
        self.session = (
            session
            or self.call("session/new", {"cwd": cwd, "mcpServers": mcp or []})["result"][
                "sessionId"
            ]
        )

    def request(self, method: str, params: dict) -> int:
        self.next_id += 1
        self.proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
            + "\n"
        )
        self.proc.stdin.flush()
        return self.next_id

    def wait(self, req_id: int, on_update=None) -> dict:
        for line in self.proc.stdout:
            msg = json.loads(line)
            if msg.get("method") == "session/update":
                show(msg["params"]["update"], self)
                if on_update:
                    on_update(msg["params"]["update"])
            elif msg.get("id") == req_id:
                return msg
        raise RuntimeError("bridge exited")

    def call(self, method: str, params: dict) -> dict:
        return self.wait(self.request(method, params))

    def prompt(self, text: str, on_update=None) -> dict:
        print(f"\n\033[1m> {text}\033[0m")
        self.answer = ""
        params = {"sessionId": self.session, "prompt": [{"type": "text", "text": text}]}
        reply = self.wait(self.request("session/prompt", params), on_update)
        if "error" in reply:
            print(f"\033[31m  error: {reply['error']['message']}\033[0m")
        else:
            print(f"\033[32m  stopReason: {reply['result']['stopReason']}\033[0m")
        return reply

    def close(self) -> None:
        self.proc.terminate()


def show(update: dict, client: Client) -> None:
    kind = update.get("sessionUpdate")
    if kind == "agent_thought_chunk":
        text = update["content"]["text"].strip()
        if text.startswith("[cloud] https://"):
            client.cloud_url = text.removeprefix("[cloud] ")
        client.thoughts.append(text)
        print(f"\033[2m  thought: {text}\033[0m")
    elif kind == "tool_call":
        client.tools.append(str(update.get("title")))
        print(
            f"\033[36m  tool: {update.get('title')} "
            f"{json.dumps(update.get('rawInput', {}))[:120]}\033[0m"
        )
    elif kind == "tool_call_update":
        texts = [c.get("content", {}).get("text", "") for c in update.get("content") or []]
        print("  result: " + " | ".join(t.strip().replace("\n", " | ") for t in texts)[:200])
    elif kind == "agent_message_chunk":
        client.answer += update["content"]["text"]
        print(f"  assistant: {update['content']['text'].strip()}")


def scenario_turns(cwd: str) -> None:
    client = Client(cwd)
    client.prompt("Run `pwd; git log --oneline -1; uname -sm` and report the output.")
    client.prompt("Follow-up: what file did you see at the repo root? Answer in one line.")
    client.close()


def scenario_restart(cwd: str) -> None:
    first = Client(cwd)
    ask = "Remember the codeword PELICAN. Reply only: noted"
    first.prompt(ask)
    first.close()
    print("\n-- bridge gone; a new one gets the transcript Omnigent replays --")
    second = Client(cwd)
    history = f"user: {ask}\nassistant: {first.answer.strip()}"
    second.prompt(REPLAY.format(history=history, latest="What was the codeword? One word."))
    second.close()
    check(second.cloud_url == first.cloud_url, f"same cloud session ({first.cloud_url})")
    check("PELICAN" in second.answer.upper(), "the cloud session kept its own history")


def scenario_respawn(cwd: str) -> None:
    first = Client(cwd)
    first.prompt("Remember the codeword HERON. Reply only: noted")
    first.close()
    print("\n-- bridge gone; the new one is prompted with the old ACP session id --")
    second = Client(cwd, session=first.session)
    second.prompt("What was the codeword? One word.")
    second.close()
    check("HERON" in second.answer.upper(), "re-attached to the same cloud session")


def scenario_archived(cwd: str) -> None:
    client = Client(cwd)
    client.prompt("Remember the codeword OSPREY. Reply only: noted")
    cloud_id = client.cloud_url.rsplit("/", 1)[1]
    bridge.call_api(f"/v1/code/sessions/{cloud_id}/archive", {})
    print(f"\n-- archived {cloud_id}, as the claude.ai sidebar does --")
    client.prompt("What was the codeword? One word.")
    client.close()
    check(any("unarchived" in t for t in client.thoughts), "the bridge unarchived the session")
    check("OSPREY" in client.answer.upper(), "the conversation survived the archive")


def scenario_cancel(cwd: str) -> None:
    client = Client(cwd)
    client.prompt("Reply with the single word: ready")

    def cancel_on_tool(update: dict) -> None:
        if update.get("sessionUpdate") == "tool_call":
            print("\033[33m  -> sending session/cancel\033[0m")
            client.request("session/cancel", {"sessionId": client.session})

    started = time.monotonic()
    client.prompt(LOOP, cancel_on_tool)
    print(f"\n  turn ended after {time.monotonic() - started:.0f}s (the loop alone takes 60s)")
    client.close()


def scenario_teardown(cwd: str) -> None:
    client = Client(cwd)
    client.prompt("Reply with the single word: ready")
    cloud_id = client.cloud_url.rsplit("/", 1)[1]
    _, cursor = bridge.fetch_events(cloud_id, None)

    def cancel_and_kill(update: dict) -> None:
        if update.get("sessionUpdate") == "tool_call":
            print("\033[33m  -> session/cancel, close stdin, SIGTERM\033[0m")
            client.request("session/cancel", {"sessionId": client.session})
            client.proc.stdin.close()
            client.proc.terminate()

    started = time.monotonic()
    with contextlib.suppress(RuntimeError, ValueError):  # the bridge is gone mid-turn
        client.prompt(LOOP, cancel_and_kill)
    result = None
    while result is None and time.monotonic() - started < 120:
        events, cursor = bridge.fetch_events(cloud_id, cursor)
        result = next(
            (e for e in events if (e.get("payload") or {}).get("type") == "result"), None
        )
        time.sleep(2)
    took = time.monotonic() - started
    print(f"\n  cloud turn ended after {took:.0f}s (the loop alone takes 60s)")
    check(
        result is not None and took < 45, "the cloud turn was interrupted before the bridge died"
    )


def scenario_subagent(cwd: str) -> None:
    client = Client(cwd)
    client.prompt(
        "Use the Task tool to start one sub-agent that runs `echo from-subagent` with Bash. "
        "Then tell me what it returned."
    )
    client.close()
    check(any(t.startswith("[sub-agent] ") for t in client.tools), "sub-agent tool calls shown")


def scenario_mcp(cwd: str) -> None:
    echo = [
        {"name": "echo", "command": sys.executable, "args": ["-c", bridge.ECHO_MCP], "env": []}
    ]
    ask = "Call the echo tool of the echo MCP server with text '{}' and quote its result exactly."
    first = Client(cwd, mcp=echo)
    first.prompt(ask.format("hello from cloud"))  # the first turn already has the tool
    first.close()
    print("\n-- bridge gone; a new one re-registers the server from this host --")
    second = Client(cwd, mcp=echo)
    history = f"user: {ask.format('hello from cloud')}\nassistant: {first.answer.strip()}"
    second.prompt(REPLAY.format(history=history, latest=ask.format("second bridge")))
    second.close()
    host = socket.gethostname()
    check(f"served by {host}" in first.answer, f"the cloud agent called a tool served by {host}")
    check(second.cloud_url == first.cloud_url, "the new bridge re-attached to the same session")
    check("second bridge" in second.answer, "the new bridge serves the tool to that session")


def scenario_untrusted() -> None:
    repo = tempfile.mkdtemp(prefix="ccr-untrusted-")
    subprocess.run(["git", "init", "-q", repo], check=True)
    Path(repo, "README.md").write_text("untrusted\n")
    subprocess.run(["git", "-C", repo, "add", "."], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "init"], check=True)
    print(f"fresh checkout: {repo}")
    client = Client(repo)
    client.prompt("Say hi.")
    client.close()


if __name__ == "__main__":
    name, args = sys.argv[1], sys.argv[2:]
    {
        "turns": scenario_turns,
        "restart": scenario_restart,
        "respawn": scenario_respawn,
        "archived": scenario_archived,
        "cancel": scenario_cancel,
        "teardown": scenario_teardown,
        "subagent": scenario_subagent,
        "mcp": scenario_mcp,
        "untrusted": scenario_untrusted,
    }[name](*args)
