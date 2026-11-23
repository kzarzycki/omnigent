"""A start that never produced output must not be adopted by another start of the same prompt."""

import importlib.util
import sys
import threading
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "claude_cloud_acp", Path(__file__).with_name("claude_cloud_acp.py")
)
bridge = importlib.util.module_from_spec(_spec)
sys.modules["claude_cloud_acp"] = bridge
_spec.loader.exec_module(bridge)

BRIEF = "verify the pull request"
REPLAY = f"{bridge.REPLAY_HEAD}{BRIEF}\n\n{bridge.REPLAY_TAIL}continue"


@pytest.fixture
def cloud(monkeypatch, tmp_path):
    """Fake cloud: each created session is numbered; nothing reaches the network."""
    created: list[str] = []

    def create(*_args):
        created.append(f"session_0000000000{len(created)}")
        return created[-1]

    monkeypatch.setattr(bridge, "STATE_FILE", tmp_path / "state" / "sessions.json")
    monkeypatch.setattr(bridge, "create_cloud_session", create)
    monkeypatch.setattr(bridge, "send", lambda _message: None)
    monkeypatch.setattr(bridge, "fetch_events", lambda *_: ([], None))
    monkeypatch.setattr(bridge, "_send", lambda *_: (1, False))
    monkeypatch.setattr(bridge, "check_model", lambda *_: None)
    monkeypatch.setattr(bridge, "check_effort", lambda *_: None)
    monkeypatch.setattr(bridge, "STALL_TIMEOUT_S", 0.3)
    monkeypatch.setattr(bridge, "FIRST_TURN_TIMEOUT_S", 0.1, raising=False)
    bridge.SESSIONS.clear()
    return created


def start(acp_id: str, *events: dict) -> bridge.Session:
    """An ACP session whose cloud stream yields *events* and then goes quiet."""
    session = bridge.SESSIONS[acp_id] = bridge.Session("/repo", acp_id)
    session.reader = threading.current_thread()  # no network stream
    for number, payload in enumerate(events, 1):
        session.events.put({"sequence_num": str(number), "payload": payload})
    return session


def test_stalled_first_start_is_not_adopted_by_next_start_of_same_prompt(cloud):
    start("acp-a")
    with pytest.raises(RuntimeError, match=r"no output"):
        bridge.run_turn("acp-a", BRIEF)
    # Another Omnigent session with the same first message and workspace.
    result = {"type": "result"}
    start("acp-b", {"type": "assistant", "message": {}}, result)
    assert bridge.run_turn("acp-b", REPLAY) == "end_turn"
    assert cloud == ["session_00000000000", "session_00000000001"], cloud
    assert bridge.SESSIONS["acp-b"].cloud_id == "session_00000000001"


def test_same_session_retry_after_a_stalled_start_creates_a_new_cloud_session(cloud):
    start("acp-a")
    with pytest.raises(RuntimeError, match=r"no output"):
        bridge.run_turn("acp-a", BRIEF)
    retry = bridge.SESSIONS["acp-a"]
    retry.reader = threading.current_thread()
    retry.events.put({"sequence_num": "1", "payload": {"type": "result"}})
    assert bridge.run_turn("acp-a", BRIEF) == "end_turn"
    assert retry.cloud_id == "session_00000000001", cloud


@pytest.mark.usefixtures("cloud")
def test_first_turn_error_names_session_and_setup_output():
    start("acp-a", {"type": "env_manager_log", "data": {"content": "npm ci running"}})
    with pytest.raises(RuntimeError) as caught:
        bridge.run_turn("acp-a", BRIEF)
    assert "session_00000000000" in str(caught.value)
    assert "npm ci running" in str(caught.value)
    assert "after its first prompt" in str(caught.value)


@pytest.mark.usefixtures("cloud")
def test_stall_after_output_keeps_the_session_for_the_next_message():
    start("acp-a", {"type": "assistant", "message": {}})
    with pytest.raises(RuntimeError, match="made no progress"):
        bridge.run_turn("acp-a", BRIEF)
    assert bridge.recall()[bridge.first_prompt_key("/repo", BRIEF)] == "session_00000000000"
