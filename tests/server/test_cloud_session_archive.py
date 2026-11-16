"""Archiving a claude-cloud session archives its cloud session; other harnesses make no call."""

from __future__ import annotations

import subprocess
import threading
from types import SimpleNamespace

import pytest

from omnigent.server import cloud_session_archive, session_live_state
from omnigent.server.cloud_session_archive import (
    CLOUD_SESSION_MARKER,
    archive_claude_cloud_session,
)
from omnigent.server.routes._sessions import orchestration

pytestmark = pytest.mark.filterwarnings("ignore")

CLOUD_ID = "session_ABCDEFGHIJKL"


def _item(role: str, text: str):
    return SimpleNamespace(
        data=SimpleNamespace(role=role, content=[{"type": "text", "text": text}])
    )


class _Store:
    def __init__(self, *items) -> None:
        self.items = list(items)

    def list_items(self, session_id, limit, type, after=None):
        start = 0 if after is None else int(after) + 1
        page = self.items[start : start + limit]
        return SimpleNamespace(
            data=page,
            last_id=str(start + len(page) - 1) if page else None,
            has_more=start + limit < len(self.items),
        )


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


def test_a_quoted_marker_is_not_the_target(calls):
    quoted = "worker said:\n" + CLOUD_SESSION_MARKER + "session_ZZZZZZZZZZZZ"
    store = _Store(
        _item("assistant", f"{CLOUD_SESSION_MARKER}{CLOUD_ID}\nworking"),
        _item("assistant", quoted),
        _item("assistant", f"> {CLOUD_SESSION_MARKER}session_YYYYYYYYYYYY"),
    )
    assert archive_claude_cloud_session(CONV, store) is True
    assert calls[0]["input"] == CLOUD_SESSION_MARKER + CLOUD_ID


def test_a_marker_past_the_first_page_is_found(calls, monkeypatch):
    monkeypatch.setattr(cloud_session_archive, "_PAGE_SIZE", 2)
    filler = [_item("assistant", "working")] * 5
    store = _Store(*filler, _item("assistant", f"{CLOUD_SESSION_MARKER}{CLOUD_ID}"))
    assert archive_claude_cloud_session(CONV, store) is True
    assert calls[0]["input"] == CLOUD_SESSION_MARKER + CLOUD_ID


def test_a_missing_login_is_warned_about_once(calls, caplog):
    cloud_session_archive._login_warning_logged = False
    calls.state["result"] = subprocess.CompletedProcess([], 1, "", "no Claude Code login found")
    archive_claude_cloud_session(CONV, CLOUD_STORE)
    archive_claude_cloud_session(CONV, CLOUD_STORE)
    assert caplog.text.count("holds no claude.ai login") == 1


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
    assert archived_ids == ["s" * 32]
    for thread in threading.enumerate():
        if thread.name == "cloud-session-archive":
            thread.join(5)
    assert len(calls) == 1


def test_a_slow_cloud_archive_does_not_hold_up_the_live_state_executor(monkeypatch):
    release = threading.Event()
    started = threading.Event()
    monkeypatch.setattr(
        session_live_state,
        "archive_claude_cloud_session",
        lambda conv, store: (started.set(), release.wait(10)),
    )
    conv = SimpleNamespace(id="s" * 32, archived=False, labels={})
    store = SimpleNamespace(
        get_conversation=lambda session_id: conv, update_conversation=lambda *a, **k: None
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
    caller = threading.Thread(
        target=session_live_state.persist_scheduled_run_completion, args=("s" * 32, "succeeded")
    )
    try:
        caller.start()
        assert started.wait(5)
        caller.join(2)
        assert not caller.is_alive()  # the transition ended while the archive still runs
    finally:
        release.set()
