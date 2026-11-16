"""Archiving a claude-cloud session archives its claude.ai cloud session; other harnesses make no call."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from omnigent.server import cloud_session_archive, session_live_state
from omnigent.server.routes._sessions import orchestration
from omnigent.server.cloud_session_archive import CLOUD_SESSION_MARKER, archive_claude_cloud_session

pytestmark = pytest.mark.filterwarnings("ignore")

CLOUD_ID = "session_ABCDEFGHIJKL"


def _item(role: str, text: str):
    return SimpleNamespace(data=SimpleNamespace(role=role, content=[{"type": "text", "text": text}]))


class _Store:
    def __init__(self, *items) -> None:
        self.items = list(items)

    def list_items(self, session_id, limit, type):
        return SimpleNamespace(data=self.items)


CLOUD_STORE = _Store(
    _item("user", "do the work"), _item("assistant", f"{CLOUD_SESSION_MARKER}{CLOUD_ID}\nworking")
)
CONV = SimpleNamespace(id="s" * 32)


@pytest.fixture
def calls(monkeypatch):
    """Records each bridge run; ``result`` sets what it returns."""
    class Calls(list):
        state: dict

    recorded = Calls()
    state = {"result": subprocess.CompletedProcess([], 0, "", "")}

    def run(argv, **kwargs):
        recorded.append({"argv": argv, **kwargs})
        if isinstance(state["result"], Exception):
            raise state["result"]
        return state["result"]

    monkeypatch.setattr(cloud_session_archive.subprocess, "run", run)
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._resolve_harness", lambda *a, **k: "acp:claude-cloud"
    )
    recorded.state = state
    return recorded


def test_archives_the_cloud_session_the_transcript_names(calls):
    assert archive_claude_cloud_session(CONV, CLOUD_STORE) is True
    (call,) = calls
    assert call["argv"][-2:] == ["--archive", "-"]
    assert CLOUD_SESSION_MARKER + CLOUD_ID in call["input"]


def test_a_failed_cloud_archive_is_logged_and_reported_not_raised(calls, caplog):
    calls.state["result"] = subprocess.CompletedProcess([], 1, "", "HTTP Error 503")
    assert archive_claude_cloud_session(CONV, CLOUD_STORE) is False
    assert "HTTP Error 503" in caplog.text
    calls.state["result"] = subprocess.TimeoutExpired("bridge", 60)
    assert archive_claude_cloud_session(CONV, CLOUD_STORE) is False


def test_another_harness_makes_no_call(calls, monkeypatch):
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._resolve_harness", lambda *a, **k: "claude-sdk"
    )
    assert archive_claude_cloud_session(CONV, CLOUD_STORE) is False
    assert calls == []


def test_an_acp_session_without_a_cloud_session_line_makes_no_call(calls):
    assert archive_claude_cloud_session(CONV, _Store(_item("assistant", "hi"))) is False
    assert calls == []


@pytest.mark.asyncio
async def test_the_archive_teardown_archives_the_cloud_session(calls):
    store = SimpleNamespace(
        get_conversation=lambda session_id: SimpleNamespace(
            id=session_id, archived=True, host_id=None, runner_id=None, git_branch=None
        ),
        delete_session_subscriptions_for=lambda session_id: None,
        list_items=CLOUD_STORE.list_items,
    )
    await orchestration._archive_stop("s" * 32, store, None, None)
    assert len(calls) == 1


def test_a_scheduled_run_archived_when_done_archives_the_cloud_session(calls, monkeypatch):
    archived_ids: list[str] = []
    conv = SimpleNamespace(id="s" * 32, archived=False, labels={})
    store = SimpleNamespace(
        get_conversation=lambda session_id: conv,
        update_conversation=lambda session_id, archived: archived_ids.append(session_id),
        list_items=CLOUD_STORE.list_items,
    )
    run = SimpleNamespace(id="run", scheduled_task_id="task")
    tasks = SimpleNamespace(
        get_running_run_by_conversation=lambda session_id: run,
        update_run=lambda *a, **k: run,
        get=lambda task_id: SimpleNamespace(archive_when_done=True),
    )
    monkeypatch.setattr(session_live_state, "_store", store)
    monkeypatch.setattr(session_live_state, "_scheduled_task_store", tasks)
    monkeypatch.setattr(session_live_state, "submit", lambda description, fn: fn())
    session_live_state.persist_scheduled_run_completion("s" * 32, "succeeded")
    assert archived_ids == ["s" * 32] and len(calls) == 1
