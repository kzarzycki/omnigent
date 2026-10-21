#!/usr/bin/env python3
"""Live e2e scenarios for the claude-cloud ACP bridge, driven as an ACP client.

Each scenario starts real cloud sessions on the signed-in claude.ai account.

    python3 e2e.py turns    <git checkout>   # create, tool call, follow-up
    python3 e2e.py restart  <git checkout>   # a new bridge process loses the cloud session
    python3 e2e.py cancel   <git checkout>   # cancel stops the mirror, not the cloud turn
    python3 e2e.py subagent <git checkout>   # sub-agent steps are not mirrored
    python3 e2e.py untrusted                 # a folder Claude Code has not trusted fails clearly
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import claude_cloud_acp as bridge  # noqa: E402


class Client:
    """One bridge process, spoken to over ACP stdio like Omnigent does."""

    def __init__(self, cwd: str) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(HERE / "claude_cloud_acp.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.next_id = 0
        self.cloud_url = None
        self.call("initialize", {"protocolVersion": 1})
        self.session = self.call("session/new", {"cwd": cwd, "mcpServers": []})["result"][
            "sessionId"
        ]

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
        print(f"\033[2m  thought: {text}\033[0m")
    elif kind == "tool_call":
        print(
            f"\033[36m  tool: {update.get('title')} "
            f"{json.dumps(update.get('rawInput', {}))[:120]}\033[0m"
        )
    elif kind == "tool_call_update":
        texts = [c.get("content", {}).get("text", "") for c in update.get("content") or []]
        print("  result: " + " | ".join(t.strip().replace("\n", " | ") for t in texts)[:200])
    elif kind == "agent_message_chunk":
        print(f"  assistant: {update['content']['text'].strip()}")


def scenario_turns(cwd: str) -> None:
    client = Client(cwd)
    client.prompt("Run `pwd; git log --oneline -1; uname -sm` and report the output.")
    client.prompt("Follow-up: what file did you see at the repo root? Answer in one line.")
    client.close()


def scenario_restart(cwd: str) -> None:
    urls = []
    for _ in range(2):
        client = Client(cwd)
        client.prompt("Reply with the single word: hello")
        urls.append(client.cloud_url)
        client.close()
    print(f"\nfirst bridge: {urls[0]}\nsecond bridge: {urls[1]}")
    print(
        "same cloud session"
        if urls[0] == urls[1]
        else "\033[33mnew cloud session: the first one was not resumed\033[0m"
    )


def scenario_cancel(cwd: str) -> None:
    client = Client(cwd)

    def cancel_on_tool(update: dict) -> None:
        if update.get("sessionUpdate") == "tool_call":
            print("\033[33m  -> sending session/cancel\033[0m")
            client.request("session/cancel", {"sessionId": client.session})

    client.prompt(
        "Run `sleep 20; echo finished-after-cancel` and report its output.", cancel_on_tool
    )
    cloud_id = client.cloud_url.rsplit("/", 1)[1]
    client.close()
    print("\nReading the cloud session directly after the cancel:")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        events, _ = bridge.fetch_events(cloud_id, None)
        results = [
            e["payload"].get("result")
            for e in events
            if (e.get("payload") or {}).get("type") == "result"
        ]
        if results:
            print(
                "\033[33m  the cloud turn finished anyway: "
                f"{str(results[-1]).strip()[:200]}\033[0m"
            )
            return
        time.sleep(3)
    print("  no result within 120s")


def scenario_subagent(cwd: str) -> None:
    client = Client(cwd)
    client.prompt(
        "Use the Task tool to start one sub-agent that runs `echo from-subagent` with Bash. "
        "Then tell me what it returned."
    )
    client.close()


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
        "cancel": scenario_cancel,
        "subagent": scenario_subagent,
        "untrusted": scenario_untrusted,
    }[name](*args)
