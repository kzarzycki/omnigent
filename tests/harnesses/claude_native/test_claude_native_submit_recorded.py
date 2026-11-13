"""A delivered message holds the pane until Claude records it, so the next can't overtake it."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.harnesses.claude_native import bridge, delivery_diagnostics
from omnigent.util.json_types import JsonObject
from tests.harnesses.claude_native.test_claude_native_bridge import _composer_pane
from tests.harnesses.claude_native.test_claude_native_bridge import (
    _trust_tmp_bridge_parent as _trust_tmp_bridge_parent,
)

_NATIVE_SESSION_ID = "11111111-2222-4333-8444-555555555555"
# How long Claude's UserPromptSubmit hooks hold the prompt before recording it.
_HOOK_S = 6.0


def _record(scenario: str, content: str) -> JsonObject:
    if scenario == "enqueued":
        # A prompt submitted mid-turn is queued; screening can withhold its text.
        return {"type": "queue-operation", "operation": "enqueue", "sessionId": _NATIVE_SESSION_ID}
    record: JsonObject = {
        "type": "user",
        "sessionId": _NATIVE_SESSION_ID,
        "isSidechain": False,
        "promptSource": "typed",
        "message": {"role": "user", "content": content},
    }
    if scenario == "wrong-session":
        record["sessionId"] = "other-native-session"
    elif scenario == "sidechain":
        record["isSidechain"] = True
    elif scenario == "tool-result":
        del record["promptSource"]
        record["message"] = {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        }
    return record


@pytest.mark.parametrize(
    "scenario,waits_for_record",
    [
        ("typed", True),
        ("enqueued", True),
        ("wrong-session", False),
        ("sidechain", False),
        ("tool-result", False),
    ],
)
def test_a_delivered_message_returns_only_once_claude_records_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    waits_for_record: bool,
) -> None:
    content = "[System: session worker (abc) ended its turn (completed).]"
    transcript = tmp_path / "native.jsonl"
    transcript.write_text('{"type":"system","content":"prefix"}\n')
    bridge.record_hook_event(
        tmp_path,
        {
            "hook_event_name": "SessionStart",
            "session_id": _NATIVE_SESSION_ID,
            "transcript_path": str(transcript),
        },
    )
    elapsed = 0.0
    submitted_at: float | None = None
    recorded = False
    pane = _composer_pane()

    def sleep(seconds: float) -> None:
        nonlocal elapsed, recorded
        elapsed += seconds
        if submitted_at is not None and not recorded and elapsed - submitted_at >= _HOOK_S:
            recorded = True
            with transcript.open("a") as handle:
                handle.write(json.dumps(_record(scenario, content)) + "\n")

    def run_tmux(_socket: str, *args: str) -> None:
        nonlocal pane, submitted_at
        if args[0] == "paste-buffer":
            pane = _composer_pane(content)
        elif args[-1] == "Enter" and submitted_at is None:
            # The draft leaves the box at once; the hooks hold the prompt.
            pane = _composer_pane()
            submitted_at = elapsed

    monkeypatch.setattr(
        bridge,
        "time",
        SimpleNamespace(monotonic=lambda: elapsed, sleep=sleep, time=lambda: elapsed),
    )
    monkeypatch.setattr(delivery_diagnostics, "time", bridge.time)
    monkeypatch.setattr(bridge, "_run_tmux", run_tmux)
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_args: pane)
    monkeypatch.setattr(bridge, "_wait_for_claude_prompt_ready", lambda *_args, **_kw: None)
    monkeypatch.setattr(bridge, "_restore_occupied_input", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        bridge,
        "_wait_for_tmux_info",
        lambda *_args, **_kw: {"socket_path": "/unused/socket", "tmux_target": "main"},
    )
    monkeypatch.setattr(bridge, "_PASTE_SETTLE_S", 0)

    bridge.inject_user_message(tmp_path, content=content)

    assert submitted_at is not None
    held = elapsed - submitted_at
    if waits_for_record:
        assert _HOOK_S <= held < _HOOK_S + 1
    else:
        # Nothing that proves the prompt was taken: give up at the cap, without failing.
        assert held >= bridge._SUBMIT_RECORD_TIMEOUT_S
