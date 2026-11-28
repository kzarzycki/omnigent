"""Shared helpers for runner tests."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from omnigent.inner.executor import NativeTurnAcceptance
from omnigent.inner.os_env import OSEnvironment
from omnigent.inner.terminal import TerminalInstance
from omnigent.runner import subagent_work


def accept_subagent_turn(
    child_id: str, *, thread_id: str, response_id: str, now: float | None = None
) -> None:
    """Finish one delivery with caller-supplied authoritative native identity."""
    request = subagent_work.begin_subagent_child_acceptance(child_id)
    try:
        subagent_work.arm_subagent_child_start_timeout(
            child_id,
            acceptance=NativeTurnAcceptance(child_id, thread_id, response_id),
            request=request,
            now=now,
        )
    finally:
        subagent_work.finish_subagent_child_acceptance(request)


_TITLE_FIELD = re.compile(
    r"(p\d+): repository\(owner: \$(o\d+), name: \$(n\d+)\) \{ pullRequest\(number: (\d+)\)"
)


def title_query_prs(argv: Sequence[str]) -> dict[str, tuple[str, int]] | None:
    """The PRs a runner's batched title query asks for: ``{alias: (owner/name, number)}``.

    :param argv: The ``gh`` arguments, without ``gh`` itself.
    :returns: ``None`` when ``argv`` is not that query.
    """
    if list(argv[:2]) != ["api", "graphql"]:
        return None
    values = dict(argv[index + 1].split("=", 1) for index, arg in enumerate(argv) if arg == "-f")
    fields = _TITLE_FIELD.findall(values.get("query", ""))
    if not fields:
        return None
    return {
        alias: (f"{values[owner]}/{values[name]}", int(number))
        for alias, owner, name, number in fields
    }


def title_query_answer(pulls: Mapping[str, Mapping[str, Any] | None]) -> str:
    """GitHub's answer to that query: each alias's ``{title, state}``, ``None`` for not found."""
    data: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    for alias, pull in pulls.items():
        data[alias] = {"pullRequest": dict(pull) if pull is not None else None}
        if pull is None:
            errors.append(
                {"type": "NOT_FOUND", "path": [alias, "pullRequest"], "message": "not found"}
            )
    return json.dumps({"data": data, **({"errors": errors} if errors else {})})


class NullServerClient:
    """Minimal fake Omnigent server client for tests that do not exercise Omnigent interactions.

    Returns empty/204 responses to all requests so the runner's Omnigent calls
    (session fetch, label patch, history load, etc.) succeed silently.
    Used wherever ``create_runner_app`` is called in tests that only
    exercise runner-local behavior with no real Omnigent server.
    """

    class _Response:
        """Stub HTTP response that looks like a 200 with an empty body."""

        status_code = 200
        is_success = True

        def json(self) -> dict[str, Any]:
            """Return an empty JSON object."""
            return {}

        def raise_for_status(self) -> None:
            """No-op: stub always succeeds."""

    async def get(self, url: str, **kwargs: Any) -> _Response:
        """Return an empty 200 for any GET request.

        :param url: Request URL (ignored).
        :param kwargs: Extra keyword arguments (ignored).
        :returns: Stub 200 response with empty JSON body.
        """
        del url, kwargs
        return self._Response()

    async def post(self, url: str, **kwargs: Any) -> _Response:
        """Return an empty 200 for any POST request.

        :param url: Request URL (ignored).
        :param kwargs: Extra keyword arguments (ignored).
        :returns: Stub 200 response with empty JSON body.
        """
        del url, kwargs
        return self._Response()

    async def patch(self, url: str, **kwargs: Any) -> _Response:
        """Return an empty 200 for any PATCH request.

        :param url: Request URL (ignored).
        :param kwargs: Extra keyword arguments (ignored).
        :returns: Stub 200 response with empty JSON body.
        """
        del url, kwargs
        return self._Response()


class RunningFlagTerminalInstance(TerminalInstance):
    """Terminal instance stub whose liveness follows its running flag."""

    async def set_conversation_link(self, conversation_link: str | None) -> None:
        """
        Update the in-memory status link without shelling out to tmux.

        :param conversation_link: Conversation URL to show, e.g.
            ``"/c/conv_abc123"``, or ``None`` to clear it.
        :returns: None.
        """
        self.conversation_link = conversation_link

    async def is_alive(self) -> bool:
        """
        Report liveness without shelling out to tmux.

        :returns: The current ``running`` flag.
        """
        return self.running


def make_test_terminal_instance(
    name: str,
    session_key: str,
    tmp_path: Path,
    *,
    running: bool = True,
    os_env: OSEnvironment | None = None,
) -> TerminalInstance:
    """
    Build a terminal instance stub for runner/resource tests.

    :param name: Terminal name, e.g. ``"bash"``.
    :param session_key: Per-launch session key, e.g. ``"s1"``.
    :param tmp_path: Temporary directory for placeholder paths.
    :param running: Initial in-memory running flag.
    :param os_env: Optional terminal-specific OS environment.
    :returns: A test terminal instance.
    """
    return RunningFlagTerminalInstance(
        name=name,
        session_key=session_key,
        socket_path=tmp_path / f"{name}-{session_key}.sock",
        private_dir=tmp_path / f"{name}-{session_key}",
        os_env=os_env,
        running=running,
    )
