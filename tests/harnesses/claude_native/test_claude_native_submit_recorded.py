"""A delivered message holds back the next one until Claude records it, so it can't overtake it."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.harnesses.claude_native import bridge, delivery_diagnostics
from omnigent.harnesses.claude_native.submit_verification import (
    capture_submit_boundary,
    has_recorded_prompt,
)
from omnigent.util.json_types import JsonObject
from tests.harnesses.claude_native.test_claude_native_bridge import _composer_pane
from tests.harnesses.claude_native.test_claude_native_bridge import (
    _trust_tmp_bridge_parent as _trust_tmp_bridge_parent,
)

_NATIVE_SESSION_ID = "11111111-2222-4333-8444-555555555555"
# How long Claude's UserPromptSubmit hooks hold the prompt before recording it.
_HOOK_S = 6.0
_CONTENT = "[System: session worker (abc) ended its turn (completed).]"


def _typed(content: object, **fields: object) -> JsonObject:
    record: JsonObject = {
        "type": "user",
        "sessionId": _NATIVE_SESSION_ID,
        "isSidechain": False,
        "promptSource": "typed",
        "message": {"role": "user", "content": content},
    }
    record.update(fields)
    return record


def _enqueued(content: object) -> JsonObject:
    return {
        "type": "queue-operation",
        "operation": "enqueue",
        "sessionId": _NATIVE_SESSION_ID,
        "content": content,
    }


_TOOL_RESULT = {
    "type": "user",
    "sessionId": _NATIVE_SESSION_ID,
    "message": {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
    },
}
_MULTI_LINE = "first line\r\nsecond line\n\n  indented third\t(tab)"


@pytest.mark.parametrize(
    "submitted,record,matches",
    [
        (_CONTENT, _typed(_CONTENT), True),
        (_CONTENT, _enqueued(_CONTENT), True),
        (_CONTENT, _typed([{"type": "text", "text": _CONTENT}]), True),
        # Claude wraps bracketed pastes and keeps line breaks as LF.
        (
            _MULTI_LINE,
            _typed(
                '<pasted_content id="1">\nfirst line\nsecond line\n\n'
                '  indented third\t(tab)\n</pasted_content id="1">'
            ),
            True,
        ),
        (_MULTI_LINE, _enqueued("first line\rsecond line\n\n  indented third\t(tab)\n"), True),
        ("/my-skill do it", _typed("<command-name>/my-skill</command-name>"), True),
        (
            "/my-skill do it",
            _typed(
                "<command-message>my-skill</command-message>\n"
                "<command-name>/my-skill</command-name>\n<command-args>do it</command-args>"
            ),
            True,
        ),
        # An earlier prompt's late record, or one the person typed, is not this submit.
        (_CONTENT, _typed("an earlier prompt"), False),
        (_CONTENT, _enqueued("an earlier prompt"), False),
        ("/my-skill", _typed("<command-name>/other</command-name>"), False),
        (_CONTENT, _enqueued(None), False),
        (_CONTENT, _typed(_CONTENT, sessionId="other-native-session"), False),
        (_CONTENT, _typed(_CONTENT, isSidechain=True), False),
        (_CONTENT, _TOOL_RESULT, False),
        (_CONTENT, {**_enqueued(_CONTENT), "operation": "dequeue"}, False),
    ],
)
def test_only_a_record_of_the_submitted_text_counts(
    tmp_path: Path, submitted: str, record: JsonObject, matches: bool
) -> None:
    transcript = tmp_path / "native.jsonl"
    transcript.write_text('{"type":"system","content":"prefix"}\n')
    boundary = capture_submit_boundary(transcript, _NATIVE_SESSION_ID)
    assert boundary is not None
    with transcript.open("a") as handle:
        handle.write(json.dumps(record) + "\n")

    recorded = has_recorded_prompt(
        boundary, submitted, normalize_prompt=bridge._unwrap_pasted_content_markers
    )

    assert recorded is matches


class _Delivery:
    """A fake pane and clock; the transcript gains *records* once the hooks have run."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        records: list[tuple[float, JsonObject]],
        draft_visible: bool = True,
        transcript_known: bool = True,
        on_sleep: Callable[[_Delivery], None] | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.elapsed = 0.0
        self.submitted_at: float | None = None
        self.transcript = tmp_path / "native.jsonl"
        self.transcript.write_text('{"type":"system","content":"prefix"}\n')
        if transcript_known:
            bridge.record_hook_event(
                tmp_path,
                {
                    "hook_event_name": "SessionStart",
                    "session_id": _NATIVE_SESSION_ID,
                    "transcript_path": str(self.transcript),
                },
            )
        pending = sorted(records, key=lambda item: item[0])
        pane = _composer_pane()

        def sleep(seconds: float) -> None:
            self.elapsed += seconds
            while pending and self.submitted_at is not None:
                delay, record = pending[0]
                if self.elapsed - self.submitted_at < delay:
                    break
                pending.pop(0)
                with self.transcript.open("a") as handle:
                    handle.write(json.dumps(record) + "\n")
            if on_sleep is not None and self.submitted_at is not None:
                on_sleep(self)

        def run_tmux(_socket: str, *args: str) -> None:
            nonlocal pane
            if args[0] == "paste-buffer" and draft_visible:
                pane = _composer_pane(_CONTENT)
            elif args[-1] == "Enter" and self.submitted_at is None:
                # The draft leaves the box at once; the hooks hold the prompt.
                pane = _composer_pane()
                self.submitted_at = self.elapsed

        clock = SimpleNamespace(
            monotonic=lambda: self.elapsed, sleep=sleep, time=lambda: self.elapsed
        )
        monkeypatch.setattr(bridge, "time", clock)
        monkeypatch.setattr(delivery_diagnostics, "time", clock)
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

    def deliver(self) -> float:
        """Deliver the message and return how long it was held after the submit Enter."""
        bridge.inject_user_message(self.tmp_path, content=_CONTENT)
        assert self.submitted_at is not None
        return self.elapsed - self.submitted_at


@pytest.mark.parametrize("draft_visible", [True, False], ids=["verified", "blind-submit"])
@pytest.mark.parametrize("record", [_typed(_CONTENT), _enqueued(_CONTENT)], ids=["typed", "queued"])
def test_a_delivered_message_returns_only_once_claude_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record: JsonObject, draft_visible: bool
) -> None:
    delivery = _Delivery(
        tmp_path, monkeypatch, records=[(_HOOK_S, record)], draft_visible=draft_visible
    )

    held = delivery.deliver()

    assert _HOOK_S <= held < _HOOK_S + 1


def test_an_earlier_prompts_late_record_does_not_release_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delivery = _Delivery(
        tmp_path,
        monkeypatch,
        records=[(0.0, _typed("an earlier prompt")), (_HOOK_S, _typed(_CONTENT))],
    )

    held = delivery.deliver()

    assert _HOOK_S <= held < _HOOK_S + 1


def test_an_unrecorded_message_gives_up_at_the_cap_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    delivery = _Delivery(tmp_path, monkeypatch, records=[])

    with caplog.at_level(logging.WARNING, logger=bridge.__name__):
        held = delivery.deliver()

    assert bridge._SUBMIT_RECORD_TIMEOUT_S <= held < bridge._SUBMIT_RECORD_TIMEOUT_S + 1
    assert "did not record the submitted prompt" in caplog.text


def test_an_unknown_transcript_skips_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delivery = _Delivery(
        tmp_path, monkeypatch, records=[(_HOOK_S, _typed(_CONTENT))], transcript_known=False
    )

    held = delivery.deliver()

    assert held < 1


def test_a_cancelled_delivery_stops_waiting_without_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancel_event = threading.Event()
    cancel_after_s = 2.0

    def cancel_later(delivery: _Delivery) -> None:
        assert delivery.submitted_at is not None
        if delivery.elapsed - delivery.submitted_at >= cancel_after_s:
            cancel_event.set()

    delivery = _Delivery(
        tmp_path, monkeypatch, records=[(_HOOK_S, _typed(_CONTENT))], on_sleep=cancel_later
    )

    with bridge.cancellable_injection(cancel_event):
        held = delivery.deliver()

    assert cancel_after_s <= held < cancel_after_s + 1


def test_controls_can_take_the_pane_while_a_message_waits_for_its_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[tuple[bool, bool, bool]] = []

    def probe_locks(delivery: _Delivery) -> None:
        if observed:
            return
        injection_file_lock = bridge._bridge_injection_file_lock(delivery.tmp_path)
        try:
            injection_file_lock.acquire(timeout=0)
        except bridge.FileLockTimeout:
            pane_file_free = False
        else:
            injection_file_lock.release()
            pane_file_free = True
        observed.append(
            (
                not bridge._bridge_injection_lock(delivery.tmp_path).locked(),
                pane_file_free,
                bridge._bridge_message_lock(delivery.tmp_path).locked(),
            )
        )

    delivery = _Delivery(
        tmp_path, monkeypatch, records=[(_HOOK_S, _typed(_CONTENT))], on_sleep=probe_locks
    )
    # Probe only once the delivery is waiting for the record, past submit verification.
    real_wait = bridge._wait_for_prompt_recorded

    def wait_then_probe(submitted: object) -> None:
        observed.clear()
        real_wait(submitted)  # type: ignore[arg-type]

    monkeypatch.setattr(bridge, "_wait_for_prompt_recorded", wait_then_probe)

    delivery.deliver()

    # The pane's locks (thread and process) are free; the next message stays held back.
    assert observed == [(True, True, True)]
