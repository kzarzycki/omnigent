"""End-to-end happy path for a scheduled run on a real native harness.

A task filed into a project with ``archive_when_done`` is fired twice
through ``POST /v1/scheduled-tasks/{id}/run`` on a live server and host
daemon:

1. A quiet run replies and ends ``succeeded``; its session is in the
   task's project and gets archived.
2. A kept run calls ``sys_session_keep``. Nobody is there to answer an
   approval prompt, so none may appear; the label must land on the
   calling session before the run completes, which keeps it unarchived.

Claude-native runs in the permission modes scheduled tasks use
(``bypassPermissions`` and ``auto``); Codex-native runs with its default.
Each needs a real logged-in CLI, so each is opt-in like the other host
e2e tests::

    OMNIGENT_E2E_CLAUDE_NATIVE=1 OMNIGENT_E2E_CODEX_NATIVE=1 \\
    .venv/bin/python -m pytest tests/e2e/test_scheduled_run_native_e2e.py \\
        --llm-api-key "mock-key" -v
"""

from __future__ import annotations

import contextlib
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.tools.builtins.session_keep import KEEP_LABEL_KEY
from tests.e2e.helpers import POLL_INTERVAL_S
from tests.e2e.test_host_claude_native_e2e import (
    _claude_native_agent_id,
    _online_host_id,
    _poll_for_assistant_marker,
    _workspace_trusted_in_claude_config,
)
from tests.e2e.test_host_claude_native_fork_e2e import _host_daemon
from tests.e2e.test_host_codex_native_e2e import _codex_native_agent_id

_CLAUDE = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CLAUDE_NATIVE") != "1" or shutil.which("claude") is None,
    reason=(
        "claude-native e2e needs an interactive Claude login; set "
        "OMNIGENT_E2E_CLAUDE_NATIVE=1 (and have `claude` installed + logged in) to run"
    ),
)
_CODEX = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CODEX_NATIVE") != "1" or shutil.which("codex") is None,
    reason="codex-native e2e needs `codex` on PATH and OMNIGENT_E2E_CODEX_NATIVE=1 to run",
)

_RUN_TIMEOUT_S = 300.0


def _wait_for_finished_run(
    client: httpx.Client, *, task_id: str, after_run_ids: set[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Poll a task's runs until a new one leaves ``running``.

    Fails as soon as the run's session shows a pending elicitation: an
    unattended run that prompts would otherwise just hang to the timeout.

    :param client: HTTP client pointed at the test server.
    :param task_id: The scheduled task id.
    :param after_run_ids: Ids of runs that existed before this fire.
    :returns: The finished run and its session snapshot.
    :raises AssertionError: On a pending elicitation or a timeout.
    """
    deadline = time.monotonic() + _RUN_TIMEOUT_S
    run: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/scheduled-tasks/{task_id}/runs", timeout=30.0)
        resp.raise_for_status()
        run = next((r for r in resp.json()["runs"] if r["id"] not in after_run_ids), None)
        if run is not None and run["conversation_id"]:
            session = client.get(f"/v1/sessions/{run['conversation_id']}", timeout=30.0)
            session.raise_for_status()
            snapshot = session.json()
            pending = snapshot["pending_elicitations"]
            assert not pending, f"scheduled run {run['id']} asked for approval: {pending!r}"
            if run["status"] != "running":
                return run, snapshot
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"no run of task {task_id} finished within {_RUN_TIMEOUT_S}s: {run!r}")


def _fire(
    client: httpx.Client, *, task_id: str, marker: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Fire a task with the run-now route and wait for the run to finish.

    :param client: HTTP client pointed at the test server.
    :param task_id: The scheduled task id.
    :param marker: Text the prompt asks the agent to reply with.
    :returns: The finished run and its session snapshot, re-read after the
        reply so the completion hook's archive decision has been written.
    """
    before = client.get(f"/v1/scheduled-tasks/{task_id}/runs", timeout=30.0)
    before.raise_for_status()
    seen = {r["id"] for r in before.json()["runs"]}
    fired = client.post(f"/v1/scheduled-tasks/{task_id}/run", timeout=30.0)
    assert fired.status_code == 202, fired.text
    run, _ = _wait_for_finished_run(client, task_id=task_id, after_run_ids=seen)
    session_id = run["conversation_id"]
    _poll_for_assistant_marker(client, session_id=session_id, marker=marker, timeout=60.0)
    # The archive write follows the run's terminal write on the same worker.
    time.sleep(2 * POLL_INTERVAL_S)
    session = client.get(f"/v1/sessions/{session_id}", timeout=30.0)
    session.raise_for_status()
    return run, session.json()


@pytest.mark.parametrize(
    ("harness", "permission_mode"),
    [
        pytest.param("claude", "bypassPermissions", marks=_CLAUDE, id="claude-bypassPermissions"),
        pytest.param("claude", "auto", marks=_CLAUDE, id="claude-auto"),
        pytest.param("codex", None, marks=_CODEX, id="codex-default"),
    ],
)
def test_scheduled_run_is_filed_archived_and_kept_without_a_prompt(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    harness: str,
    permission_mode: str | None,
) -> None:
    """A quiet run is filed and archived; a kept run labels itself, unprompted."""
    workspace = tmp_path / "scheduled_ws"
    workspace.mkdir()
    trusted = (
        _workspace_trusted_in_claude_config(workspace)
        if harness == "claude"
        else contextlib.nullcontext()
    )
    quiet = f"QUIET_{uuid.uuid4().hex[:6].upper()}"
    kept = f"KEPT_{uuid.uuid4().hex[:6].upper()}"
    reason = f"found a reply {kept}"

    with trusted, _host_daemon(tmp_path, live_server):
        host_id = _online_host_id(http_client, timeout=30.0)
        agent_id = (
            _claude_native_agent_id(http_client)
            if harness == "claude"
            else _codex_native_agent_id(http_client)
        )
        project = http_client.post(
            "/v1/projects", json={"name": f"scheduled-e2e-{uuid.uuid4().hex[:8]}"}
        )
        project.raise_for_status()
        project_id = project.json()["id"]
        created = http_client.post(
            "/v1/scheduled-tasks",
            json={
                "name": f"scheduled e2e {harness}",
                "prompt": f"Reply with exactly {quiet} and nothing else. Do not use any tools.",
                # Never due during the test; it only fires through run-now.
                "rrule": "FREQ=YEARLY",
                "agent_id": agent_id,
                "permission_mode": permission_mode,
                "project_id": project_id,
                "archive_when_done": True,
                "workspace": str(workspace),
                "host_id": host_id,
            },
            timeout=30.0,
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]

        run, session = _fire(http_client, task_id=task_id, marker=quiet)
        assert run["status"] == "succeeded", run
        assert session["project_id"] == project_id
        assert session["archived"] is True, "a quiet succeeded run must be archived"
        assert KEEP_LABEL_KEY not in session["labels"]

        patched = http_client.patch(
            f"/v1/scheduled-tasks/{task_id}",
            json={
                "prompt": (
                    f'Call the omnigent tool sys_session_keep with reason "{reason}". '
                    f"After it returns, reply with exactly {kept} and nothing else."
                )
            },
            timeout=30.0,
        )
        assert patched.status_code == 200, patched.text

        run, session = _fire(http_client, task_id=task_id, marker=kept)
        assert run["status"] == "succeeded", run
        assert session["id"] == run["conversation_id"]
        assert session["labels"].get(KEEP_LABEL_KEY) == reason, session["labels"]
        assert session["project_id"] == project_id
        assert session["archived"] is False, "a kept run must stay visible"
