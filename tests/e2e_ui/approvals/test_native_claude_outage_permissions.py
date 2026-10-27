"""Real Claude tool calls during a server outage follow Claude's own rules.

Only model replies are scripted. Claude's tools, command hooks, terminal and
the browser run live; the server is stopped and started for real.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import cast

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.harnesses.claude_native import bridge
from tests._helpers.polling import poll_until
from tests.e2e._native_resume_helpers import wait_for_terminal_ready
from tests.e2e.conftest import get_mock_requests
from tests.e2e_ui.conftest import (
    _server_state,
    configure_mock_llm,
    native_claude_mock_session_context,
    reset_mock_llm,
)
from tests.e2e_ui.messages.test_native_claude_render_parity import _ensure_chat_view

_APPROVAL_CARD = '[data-testid="approval-card"]'
# Longer than the policy hook's 30s retry budget plus one 30s permission backoff.
_OUTAGE_SETTLE_S = 45.0


def _bash(call_id: str, command: str) -> dict:
    return {
        "tool_calls": [
            {"call_id": call_id, "name": "Bash", "arguments": json.dumps({"command": command})}
        ]
    }


def _tool_results(mock_url: str, call_id: str) -> list[dict]:
    return [
        block
        for request in get_mock_requests(mock_url)
        for message in request.get("messages", [])
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "tool_result" and block.get("tool_use_id") == call_id
    ]


def _pane(session_id: str) -> tuple[str, str]:
    info = json.loads(
        (bridge.bridge_dir_for_conversation_id(session_id) / "tmux.json").read_text()
    )
    return info["socket_path"], info["tmux_target"]


def _dialog_for(session_id: str, needle: str) -> bool:
    pane = bridge._capture_pane(*_pane(session_id))
    # The dialog wraps a long path inside its box at any character.
    return bridge._user_prompt_visible(pane) and needle in re.sub(r"[\s│]+", "", pane)


def _pending_commands(base_url: str, session_id: str) -> list[str]:
    snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10).json()
    return [json.dumps(item) for item in snapshot.get("pending_elicitations") or []]


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_outage_runs_allowed_calls_and_carries_other_prompts_to_the_web(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    claude_managed_settings: None,
) -> None:
    """
    With the server stopped: an allowed ``grep`` runs unprompted, a prompt
    answered in the terminal raises no card later, and an unanswered prompt's
    card reaches the web once the server is back.
    """
    stop = _server_state.get("stop_server")
    restart = _server_state.get("restart_server")
    if not callable(stop) or not callable(restart):
        pytest.skip("requires the locally spawned restartable server")
    stop_server = cast("Callable[[], None]", stop)
    restart_server = cast("Callable[[], None]", restart)
    marker = uuid.uuid4().hex[:12]
    workdir = tmp_path_factory.mktemp(f"outage-{marker}")
    answered = workdir / "answered-in-terminal"
    carried = workdir / "carried-to-web"
    grep_id, answered_id, carried_id = (f"toolu_{name}_{marker}" for name in ("g", "a", "c"))
    done = f"Outage journey done ({marker})."

    with native_claude_mock_session_context(
        live_server,
        mock_llm_server_url,
        tmp_path_factory,
        terminal_launch_args=["--permission-mode", "default", "--allowedTools", "Bash(grep:*)"],
    ) as (base_url, session_id):
        reset_mock_llm(mock_llm_server_url)
        configure_mock_llm(
            mock_llm_server_url,
            [
                # The delay holds the turn while the test stops the server.
                # A plain read inside the workspace, which Bash(grep:*) covers.
                {**_bash(grep_id, "grep -c spec_version pyproject.toml"), "delay": 20.0},
                _bash(answered_id, f"touch {answered}"),
                _bash(carried_id, f"touch {carried}"),
                {"text": done},
            ],
            match=marker,
            required_tools=["Bash"],
        )
        with httpx.Client(base_url=base_url, timeout=10) as client:
            wait_for_terminal_ready(
                client, conversation_id=session_id, harness="claude", timeout=90
            )
        page.goto(f"{base_url}/c/{session_id}")
        _ensure_chat_view(page)
        page.get_by_role("textbox", name="Message the agent").fill(
            f"Run the three commands ({marker})."
        )
        page.get_by_role("button", name="Send", exact=True).click()
        poll_until(lambda: get_mock_requests(mock_llm_server_url), timeout_s=60)
        stop_server()
        try:
            # Allowed by Claude's own rule: runs with no answer from anyone.
            poll_until(lambda: _tool_results(mock_llm_server_url, grep_id), timeout_s=120)
            assert not any(r.get("is_error") for r in _tool_results(mock_llm_server_url, grep_id))

            # Not allowed: Claude prompts; answer it in the terminal.
            poll_until(lambda: _dialog_for(session_id, str(answered)), timeout_s=120)
            socket_path, target = _pane(session_id)
            subprocess.run(
                ["tmux", "-S", socket_path, "send-keys", "-t", target, "Enter"], check=True
            )
            poll_until(answered.exists, timeout_s=60)

            # Not allowed and left unanswered until the server returns.
            poll_until(lambda: _dialog_for(session_id, str(carried)), timeout_s=120)
            time.sleep(_OUTAGE_SETTLE_S)
        except BaseException:
            print(bridge._capture_pane(*_pane(session_id)))
            raise
        finally:
            # Cleanup and the card below need the server back.
            restart_server()

        poll_until(
            lambda: any(str(carried) in item for item in _pending_commands(base_url, session_id)),
            timeout_s=90,
        )
        deadline = time.monotonic() + _OUTAGE_SETTLE_S
        while time.monotonic() < deadline:
            pending = _pending_commands(base_url, session_id)
            assert not any(str(answered) in item for item in pending), pending
            time.sleep(2)

        page.reload()
        _ensure_chat_view(page)
        card = page.locator(_APPROVAL_CARD).filter(has_text=str(carried))
        expect(card).to_be_visible(timeout=60_000)
        card.get_by_role("button", name="Approve", exact=True).click()
        poll_until(carried.exists, timeout_s=60)
        expect(page.get_by_text(done, exact=True)).to_be_visible(timeout=60_000)
        assert not _pending_commands(base_url, session_id)
        assert Path(answered).exists()
