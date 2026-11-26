"""A message to an idle session resumes its cloud session, or fails naming the cause."""

import importlib.util
import io
import json
import sys
import threading
import urllib.error
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "claude_cloud_acp", Path(__file__).with_name("claude_cloud_acp.py")
)
bridge = importlib.util.module_from_spec(_spec)
sys.modules["claude_cloud_acp"] = bridge
_spec.loader.exec_module(bridge)

CLOUD_ID = "session_01ResumeTest"
REPLAY = (
    f"{bridge.REPLAY_HEAD}first\n\nassistant: {bridge.MARKER}{CLOUD_ID}\n\n"
    f"{bridge.REPLAY_TAIL}continue"
)


@pytest.fixture(autouse=True)
def cloud(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "STATE_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(bridge, "send", lambda _message: None)
    monkeypatch.setattr(bridge, "fetch_events", lambda *_: ([], None))
    monkeypatch.setattr(bridge, "check_model", lambda *_: None)
    monkeypatch.setattr(bridge, "check_effort", lambda *_: None)
    bridge.SESSIONS.clear()


def idle_session() -> bridge.Session:
    """A fresh bridge for an idle Omnigent session; its cloud stream holds one result."""
    session = bridge.SESSIONS["acp-a"] = bridge.Session("/repo", "acp-a")
    session.reader = threading.current_thread()
    session.events.put({"sequence_num": "2", "payload": {"type": "result"}})
    return session


def http_error(code: int, body: dict | None = None) -> urllib.error.HTTPError:
    raw = io.BytesIO(json.dumps(body or {}).encode())
    return urllib.error.HTTPError("https://x", code, "err", {}, raw)  # type: ignore[arg-type]


def test_idle_session_resumes_the_same_cloud_session(monkeypatch):
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(bridge, "_send", lambda s, text: (sent.append((s.cloud_id, text)), (1, False))[1])
    session = idle_session()
    assert bridge.run_turn("acp-a", REPLAY) == "end_turn"
    assert session.cloud_id == CLOUD_ID and sent == [(CLOUD_ID, "continue")]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (http_error(404), "no longer exists"),
        (http_error(401), "login expired"),
        (http_error(500, {"error": {"message": "upstream boom"}}), "upstream boom"),
        (urllib.error.URLError("no route to host"), "unreachable"),
    ],
)
def test_failed_resume_names_the_cause(monkeypatch, error, expected):
    def refuse(*_):
        raise error

    monkeypatch.setattr(bridge, "_send", refuse)
    idle_session()
    with pytest.raises(RuntimeError) as caught:
        bridge.run_turn("acp-a", REPLAY)
    assert expected in str(caught.value) and CLOUD_ID in str(caught.value)


def test_failed_lookup_of_a_resumed_session_names_the_cause(monkeypatch):
    def refuse(*_):
        raise http_error(404)

    monkeypatch.setattr(bridge, "fetch_events", refuse)
    idle_session()
    with pytest.raises(RuntimeError, match="could not reach the cloud session"):
        bridge.run_turn("acp-a", REPLAY)


def test_empty_exception_still_answers_with_a_message(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(bridge, "send", sent.append)

    def blank(*_):
        raise RuntimeError

    monkeypatch.setattr(bridge, "run_turn", blank)
    bridge.handle_prompt({"id": 1, "params": {"sessionId": "acp-a", "prompt": []}})
    assert sent[-1]["error"]["message"]
