"""A delivered message holds back the next one until Claude records it, so it can't overtake it."""

from __future__ import annotations

import functools
import json
import logging
import multiprocessing
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.harnesses.claude_native import bridge, delivery_diagnostics
from omnigent.harnesses.claude_native.submit_verification import (
    capture_submit_boundary,
    has_recorded_prompt,
    read_fresh_submit_records,
)
from omnigent.util.json_types import JsonObject
from tests.harnesses.claude_native.conftest import SUBMIT_RECORD_TIMEOUT_S
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


@pytest.fixture(autouse=True)
def _wait_for_the_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "_SUBMIT_RECORD_TIMEOUT_S", SUBMIT_RECORD_TIMEOUT_S)


def _stamped(record: JsonObject, at: float) -> JsonObject:
    """Return *record* with Claude's millisecond ISO timestamp for *at*."""
    timestamp = datetime.fromtimestamp(at, UTC).isoformat(timespec="milliseconds")
    return {**record, "timestamp": timestamp.replace("+00:00", "Z")}


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
        ("/my-skill", _typed("<command-name>/my-skill</command-name>"), True),
        (
            "/my-skill  do it",
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
        # The same skill run earlier with other arguments, or an echo without them.
        (
            "/my-skill second",
            _typed(
                "<command-message>my-skill</command-message>\n"
                "<command-name>/my-skill</command-name>\n<command-args>first</command-args>"
            ),
            False,
        ),
        ("/my-skill do it", _typed("<command-name>/my-skill</command-name>"), False),
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
        boundary, submitted, normalize_prompt=bridge._recorded_prompt_text
    )

    assert recorded is matches


@pytest.mark.parametrize(
    "in_flight",
    ['{"type":"system"', '{"type":"system","content":"' + "x" * 600, "{"],
    ids=["short", "past-256-bytes", "one-byte"],
)
@pytest.mark.parametrize(
    "history", ['{"type":"system","content":"prefix"}\n', ""], ids=["history", "first-line"]
)
def test_a_boundary_starts_at_the_record_in_flight_and_skips_it(
    tmp_path: Path, history: str, in_flight: str
) -> None:
    transcript = tmp_path / "native.jsonl"
    transcript.write_text(history + in_flight)

    boundary = capture_submit_boundary(transcript, _NATIVE_SESSION_ID)

    assert boundary is not None
    assert boundary.byte_offset == len(history)
    with transcript.open("a") as handle:
        handle.write(',"content":"tail"}\n' + json.dumps(_typed(_CONTENT)) + "\n")
    assert read_fresh_submit_records(boundary) == [_typed(_CONTENT)]


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
                    handle.write(json.dumps(_stamped(record, self.elapsed)) + "\n")
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
@pytest.mark.parametrize(
    "record", [_typed(_CONTENT), _enqueued(_CONTENT)], ids=["typed", "queued"]
)
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


@pytest.mark.parametrize(
    "transcript_at_submit,hook_s", [("unreported", 1.0), ("mid-write", _HOOK_S)]
)
def test_a_transcript_unknown_at_submit_still_holds_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transcript_at_submit: str, hook_s: float
) -> None:
    # A fresh session's first message: hooks report the transcript (here only after
    # the record), or finish a record, late.
    def settle_transcript(delivery: _Delivery) -> None:
        if transcript_at_submit == "unreported":
            if _CONTENT not in delivery.transcript.read_text():
                return
            bridge.record_hook_event(
                delivery.tmp_path,
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": _NATIVE_SESSION_ID,
                    "transcript_path": str(delivery.transcript),
                },
            )
        elif not delivery.transcript.read_text().endswith("\n"):
            with delivery.transcript.open("a") as handle:
                handle.write(',"content":"tail"}\n')

    delivery = _Delivery(
        tmp_path,
        monkeypatch,
        records=[(hook_s, _typed(_CONTENT))],
        transcript_known=transcript_at_submit != "unreported",
        on_sleep=settle_transcript,
    )
    if transcript_at_submit == "mid-write":
        with delivery.transcript.open("a") as handle:
            handle.write('{"type":"system"')

    held = delivery.deliver()

    assert hook_s <= held < hook_s + 1


def test_an_earlier_identical_prompt_does_not_release_the_wait_mid_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The person sends the same text again while Claude is still writing a record.
    def finish_record(delivery: _Delivery) -> None:
        if not delivery.transcript.read_text().endswith("\n"):
            with delivery.transcript.open("a") as handle:
                handle.write(',"content":"tail"}\n')

    delivery = _Delivery(
        tmp_path, monkeypatch, records=[(_HOOK_S, _typed(_CONTENT))], on_sleep=finish_record
    )
    with delivery.transcript.open("a") as handle:
        handle.write(json.dumps(_typed(_CONTENT)) + '\n{"type":"system"')

    held = delivery.deliver()

    assert _HOOK_S <= held < _HOOK_S + 1


@pytest.mark.parametrize(
    "report_after_s,history",
    [
        (3.0, None),
        (1.0, _stamped(_typed(_CONTENT), 50.0)),
        (1.0, _typed(_CONTENT)),
    ],
    ids=["slow-hook-start", "resumed-history", "unstamped-history"],
)
def test_a_transcript_reported_late_holds_the_wait_until_the_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report_after_s: float,
    history: JsonObject | None,
) -> None:
    # The hooks report the transcript some seconds into the hook window; a resumed
    # session's transcript may already hold the same prompt from before the submit.
    def report_transcript(delivery: _Delivery) -> None:
        assert delivery.submitted_at is not None
        if delivery.elapsed - delivery.submitted_at >= report_after_s:
            bridge.record_hook_event(
                delivery.tmp_path,
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": _NATIVE_SESSION_ID,
                    "transcript_path": str(delivery.transcript),
                },
            )

    delivery = _Delivery(
        tmp_path,
        monkeypatch,
        records=[(_HOOK_S, _typed(_CONTENT))],
        transcript_known=False,
        on_sleep=report_transcript,
    )
    delivery.elapsed = 100.0
    if history is not None:
        with delivery.transcript.open("a") as handle:
            handle.write(json.dumps(history) + "\n")

    held = delivery.deliver()

    assert _HOOK_S <= held < _HOOK_S + 1


def test_a_transcript_never_reported_waits_to_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delivery = _Delivery(
        tmp_path, monkeypatch, records=[(_HOOK_S, _typed(_CONTENT))], transcript_known=False
    )

    held = delivery.deliver()

    assert bridge._SUBMIT_RECORD_TIMEOUT_S <= held < bridge._SUBMIT_RECORD_TIMEOUT_S + 1


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


def _fake_pane_with_slow_hooks(
    bridge_dir: Path, events: Path, *, hook_s: float, patch: Callable[[str, object], None]
) -> None:
    """Fake this process's pane on the real clock; each submit is recorded *hook_s* later."""
    transcript = bridge_dir / "native.jsonl"
    pane = _composer_pane()
    pasted = ""

    def log(event: str) -> None:
        with events.open("a") as handle:
            handle.write(event + "\n")

    def record(text: str) -> None:
        with transcript.open("a") as handle:
            handle.write(json.dumps(_typed(text)) + "\n")
        log(f"recorded {text}")

    def run_tmux(_socket: str, *args: str) -> None:
        nonlocal pane, pasted
        if args[0] == "load-buffer":
            pasted = Path(args[-1]).read_text().strip()
        elif args[0] == "paste-buffer":
            pane = _composer_pane(pasted)
        elif args[-1] == "Enter" and pane != _composer_pane():
            pane = _composer_pane()
            log(f"submitted {pasted}")
            threading.Timer(hook_s, record, args=(pasted,)).start()

    patch("_run_tmux", run_tmux)
    patch("_capture_pane", lambda *_args: pane)
    patch("_wait_for_claude_prompt_ready", lambda *_args, **_kw: None)
    patch("_restore_occupied_input", lambda *_args, **_kw: None)
    patch(
        "_wait_for_tmux_info",
        lambda *_args, **_kw: {"socket_path": "/unused/socket", "tmux_target": "main"},
    )
    patch("_PASTE_SETTLE_S", 0)


def _deliver_from_another_process(bridge_dir: str, events: str, content: str) -> None:
    """Child-process body; module level so multiprocessing can pickle it."""
    patch = functools.partial(setattr, bridge)
    patch("_TRUSTED_PARENT", Path(bridge_dir))
    patch("_BRIDGE_ROOT", Path(bridge_dir))
    _fake_pane_with_slow_hooks(Path(bridge_dir), Path(events), hook_s=1.0, patch=patch)
    bridge.inject_user_message(Path(bridge_dir), content=content)


def test_a_message_from_another_process_waits_for_this_ones_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner and harness processes share the pane, so the message lock is a file lock."""
    events = tmp_path / "events.log"
    events.touch()
    (tmp_path / "native.jsonl").write_text('{"type":"system","content":"prefix"}\n')
    bridge.record_hook_event(
        tmp_path,
        {
            "hook_event_name": "SessionStart",
            "session_id": _NATIVE_SESSION_ID,
            "transcript_path": str(tmp_path / "native.jsonl"),
        },
    )
    child = multiprocessing.get_context("spawn").Process(
        target=_deliver_from_another_process,
        args=(str(tmp_path), str(events), "first message"),
    )
    child.start()
    deadline = time.monotonic() + 30
    while "submitted first message" not in events.read_text():
        assert child.is_alive() and time.monotonic() < deadline, events.read_text()
        time.sleep(0.05)
    _fake_pane_with_slow_hooks(
        tmp_path, events, hook_s=0.0, patch=functools.partial(monkeypatch.setattr, bridge)
    )

    bridge.inject_user_message(tmp_path, content="second message")

    child.join(timeout=30)
    assert child.exitcode == 0
    order = events.read_text().splitlines()
    assert order.index("recorded first message") < order.index("submitted second message"), order
