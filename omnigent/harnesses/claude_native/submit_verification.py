"""Read fresh native outcomes without attributing them to a web input."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, cast

from omnigent.util.json_types import JsonObject

_MAX_FRESH_BYTES = 16 * 1024 * 1024
_BLOCKED_HEADER = "UserPromptSubmit operation blocked by hook:\n"
_PROMPT_SEPARATOR = "\n\nOriginal prompt: "


@dataclass(frozen=True)
class SubmitBoundary:
    """A pinned native session and its transcript immediately before submit."""

    path: Path
    native_session_id: str
    byte_offset: int
    prefix_sha256: str
    file_id: tuple[int, int] | None
    # The record Claude was still writing at the submit starts at the offset; it predates it.
    skips_first_record: bool = False
    # Wall-clock time of the submit, for a boundary at the transcript's start: older
    # records (a resumed session's history) predate it.
    recorded_since: float | None = None


def capture_submit_boundary(path: Path, native_session_id: str) -> SubmitBoundary | None:
    """Capture the append boundary at the start of the last line; unreadable files stay unknown."""
    try:
        path = path.resolve()
        try:
            with path.open("rb") as handle:
                identity = os.fstat(handle.fileno())
                file_id = (identity.st_dev, identity.st_ino)
                handle.seek(0, 2)
                end = handle.tell()
                offset = _line_start(handle, end)
                handle.seek(max(0, offset - 256))
                prefix = handle.read(min(offset, 256))
        except FileNotFoundError:
            end, offset, prefix, file_id = 0, 0, b"", None
        return SubmitBoundary(
            path=path,
            native_session_id=native_session_id,
            byte_offset=offset,
            prefix_sha256=hashlib.sha256(prefix).hexdigest(),
            file_id=file_id,
            skips_first_record=offset < end,
        )
    except (OSError, ValueError, RuntimeError):
        return None


def _line_start(handle: BinaryIO, end: int) -> int:
    """Return the offset just past the last newline before *end*, or 0 when there is none."""
    position = end
    while position > 0:
        chunk_start = max(0, position - 256)
        handle.seek(chunk_start)
        newline = handle.read(position - chunk_start).rfind(b"\n")
        if newline >= 0:
            return chunk_start + newline + 1
        position = chunk_start
    return 0


def capture_transcript_start(
    path: Path, native_session_id: str, *, recorded_since: float
) -> SubmitBoundary:
    """A boundary at the transcript's start, for when none could be captured before submit.

    :param recorded_since: Wall-clock time of the submit; records stamped earlier don't count.
    """
    return SubmitBoundary(
        path=path,
        native_session_id=native_session_id,
        byte_offset=0,
        prefix_sha256=hashlib.sha256(b"").hexdigest(),
        file_id=None,
        recorded_since=recorded_since,
    )


def transcript_timestamp(value: object) -> float | None:
    """
    Parse a Claude transcript timestamp.

    :param value: Timestamp string, e.g.
        ``"2026-05-27T22:53:13.245Z"``.
    :returns: Unix timestamp, e.g. ``1779922393.245``, or ``None``
        when parsing fails.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_fresh_submit_records(boundary: SubmitBoundary) -> list[JsonObject]:
    """Return complete appended records while the captured file still matches."""
    try:
        with boundary.path.open("rb") as handle:
            identity = os.fstat(handle.fileno())
            if boundary.file_id is not None and boundary.file_id != (
                identity.st_dev,
                identity.st_ino,
            ):
                return []
            handle.seek(0, 2)
            if handle.tell() < boundary.byte_offset:
                return []
            handle.seek(max(0, boundary.byte_offset - 256))
            prefix = handle.read(min(boundary.byte_offset, 256))
            if hashlib.sha256(prefix).hexdigest() != boundary.prefix_sha256:
                return []
            data = handle.read(_MAX_FRESH_BYTES + 1)
            if len(data) > _MAX_FRESH_BYTES:
                return []
        records: list[JsonObject] = []
        lines = data.splitlines(keepends=True)
        if boundary.skips_first_record:
            lines = lines[1:]
        for line in lines:
            if not line.endswith(b"\n"):
                break
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(entry, dict) and _recorded_since_boundary(entry, boundary):
                records.append(cast(JsonObject, entry))
        return records
    except (OSError, ValueError, RuntimeError):
        return []


def _recorded_since_boundary(entry: dict[str, object], boundary: SubmitBoundary) -> bool:
    """Whether *entry* is stamped no earlier than the boundary's submit time, when it has one."""
    if boundary.recorded_since is None:
        return True
    recorded_at = transcript_timestamp(entry.get("timestamp"))
    # Claude stamps records to the millisecond.
    return recorded_at is not None and recorded_at >= boundary.recorded_since - 0.001


def _prompt_key(text: str) -> str:
    """Reduce prompt text to what survives the bracketed paste: LF line ends, no control bytes.

    A slash command reduces to ``/name args``, the form Claude's echo is read back as.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    key = "".join(char for char in text if char in "\n\t" or ord(char) >= 0x20).strip()
    return " ".join(key.split(maxsplit=1)) if key.startswith("/") else key


def _record_texts(entry: JsonObject) -> list[str]:
    """Return the prompt texts an enqueue or user record carries (string or text blocks)."""
    if entry.get("type") == "queue-operation":
        content = entry.get("content")
    else:
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
    return []


def has_recorded_prompt(
    boundary: SubmitBoundary,
    submitted_text: str,
    *,
    normalize_prompt: Callable[[str], str],
) -> bool:
    """Whether Claude recorded *submitted_text* since *boundary*, as a new turn or a queued one.

    Claude writes either record only once its UserPromptSubmit hooks have run. The
    text must match, so a late record of an earlier prompt or one the person typed
    does not count; *normalize_prompt* reads a slash-command echo back as ``/name args``.
    """
    submitted_key = _prompt_key(submitted_text)
    for entry in read_fresh_submit_records(boundary):
        if entry.get("sessionId") != boundary.native_session_id or entry.get("isSidechain"):
            continue
        is_enqueue = entry.get("type") == "queue-operation" and entry.get("operation") == "enqueue"
        is_prompt = entry.get("type") == "user" and isinstance(entry.get("promptSource"), str)
        if not (is_enqueue or is_prompt):
            continue
        for text in _record_texts(entry):
            if _prompt_key(normalize_prompt(text)) == submitted_key:
                return True
    return False


def has_matching_hook_refusal(
    boundary: SubmitBoundary,
    submitted_text: str,
    *,
    normalize_prompt: Callable[[str], str],
) -> bool:
    """Match a fresh explicit refusal only to stop retries, without assigning ownership."""
    for entry in read_fresh_submit_records(boundary):
        if (
            entry.get("type") != "system"
            or entry.get("isSidechain") is True
            or entry.get("sessionId") != boundary.native_session_id
            or not isinstance(entry.get("uuid"), str)
            or not entry["uuid"]
        ):
            continue
        content = entry.get("content")
        if not isinstance(content, str) or not content.startswith(_BLOCKED_HEADER):
            continue
        body = content.removeprefix(_BLOCKED_HEADER)
        ending = entry.get("promptSubmitEnding")
        if isinstance(ending, dict):
            outcome = ending.get("ending")
            if not isinstance(outcome, dict) or outcome.get("kind") != "blocked_by_hook":
                continue
            reason = outcome.get("reason")
            if not isinstance(reason, str) or not body.startswith(reason + _PROMPT_SEPARATOR):
                continue
            prompt = body[len(reason + _PROMPT_SEPARATOR) :]
        elif ending is None and body.count(_PROMPT_SEPARATOR) == 1:
            _reason, _separator, prompt = body.partition(_PROMPT_SEPARATOR)
        else:
            continue
        if normalize_prompt(prompt) == submitted_text:
            return True
    return False
