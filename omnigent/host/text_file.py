"""Single-file text read served over the host tunnel (``GET /v1/hosts/{id}/files/...``).

Stdlib-only so both the server (pre-check of the credential deny-list) and the
host (the read itself) can import it.
"""

from __future__ import annotations

import fnmatch
import os
import stat
from typing import Any, BinaryIO

MAX_TEXT_FILE_BYTES = 64 * 1024
# Deny-list for the common credential files; the route stays owner-only either way.
_CREDENTIAL_BASENAMES = (".credentials.json", "auth.json", ".env", ".env.*")
_SSH_DIRECTORY = ".ssh"


class HostTextFileError(Exception):
    """A text-file read failed with an HTTP-mappable outcome.

    :param status: HTTP status, e.g. ``404``.
    :param code: Machine-readable code, e.g. ``"not_found"``.
    :param message: Human-readable detail.
    """

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def is_credential_path(path: str) -> bool:
    """Whether ``path`` names a credential file the read route refuses.

    :param path: Absolute or ``~``-prefixed path, ``/`` or ``\\`` separated.
    :returns: ``True`` for a credential basename or any path under a ``.ssh`` directory.
    """
    # Casefolded so a case-insensitive filesystem can't reach `.ENV` or `.SSH`.
    parts = [part.casefold() for part in path.replace("\\", "/").split("/") if part]
    if not parts:
        return False
    if _SSH_DIRECTORY in parts[:-1]:
        return True
    return any(fnmatch.fnmatchcase(parts[-1], pattern) for pattern in _CREDENTIAL_BASENAMES)


def validate_text_file_range(offset: object, limit: object) -> tuple[int, int]:
    """Check a byte range for :func:`read_text_file` and apply its defaults.

    :param offset: First byte to read, ``None`` for ``0``; any value is type-checked here
        because it arrives from a frame.
    :param limit: Most bytes to return, ``None`` for :data:`MAX_TEXT_FILE_BYTES`;
        larger values are cut down to it.
    :returns: ``(offset, limit)`` ready to read with.
    :raises HostTextFileError: 400 ``invalid_range`` for a negative ``offset``, a
        ``limit`` below 1 or a value that is not an integer.
    """
    resolved_offset = 0 if offset is None else offset
    resolved_limit = MAX_TEXT_FILE_BYTES if limit is None else limit
    if (
        not isinstance(resolved_offset, int)
        or not isinstance(resolved_limit, int)
        or isinstance(resolved_offset, bool)
        or isinstance(resolved_limit, bool)
    ):
        raise HostTextFileError(400, "invalid_range", "offset and limit must be integers")
    if resolved_offset < 0:
        raise HostTextFileError(400, "invalid_range", "offset must be 0 or more")
    if resolved_limit < 1:
        raise HostTextFileError(400, "invalid_range", "limit must be 1 or more")
    return resolved_offset, min(resolved_limit, MAX_TEXT_FILE_BYTES)


def _utf8_width(lead: int) -> int:
    """Byte length of the UTF-8 character that starts with ``lead`` (1 if not a lead byte)."""
    if lead >= 0xF0:
        return 4
    if lead >= 0xE0:
        return 3
    if lead >= 0xC0:
        return 2
    return 1


def _complete_characters_length(raw: bytes) -> int:
    """Length of ``raw`` without a trailing character cut off by the slice end."""
    # A character is at most 4 bytes, so its lead byte is within the last 4.
    for back in range(1, min(4, len(raw)) + 1):
        byte = raw[-back]
        if byte & 0xC0 != 0x80:
            return len(raw) - back if _utf8_width(byte) > back else len(raw)
    return len(raw)


def read_text_file(path: str, offset: object = None, limit: object = None) -> dict[str, Any]:
    """Read a UTF-8 text file on this host, whole or as one byte range.

    With neither ``offset`` nor ``limit`` the whole file is returned and a file over
    :data:`MAX_TEXT_FILE_BYTES` is a 413. With either, at most ``limit`` bytes from
    ``offset`` are returned, ended at the last whole character.

    :param path: Absolute or ``~``-prefixed path; ``~`` expands against this host's home.
    :param offset: First byte of the range, e.g. ``65535`` (a previous ``next_offset``).
    :param limit: Most bytes in the range, cut down to :data:`MAX_TEXT_FILE_BYTES`.
    :returns: ``{"object": "file", "path": <absolute path>, "size": <bytes>, "content": <text>}``;
        a range adds ``offset``, ``length``, ``next_offset`` (``None`` at end of file)
        and ``mtime_ns``, and its ``size`` is the whole file's.
    :raises HostTextFileError: 403 for a credential path (checked on the given and the
        symlink-resolved path), 404 when missing, 400 for a directory, non-UTF-8 bytes,
        an offset inside a character or an invalid range, 413 over
        :data:`MAX_TEXT_FILE_BYTES` without a range.
    """
    byte_range = (
        validate_text_file_range(offset, limit)
        if offset is not None or limit is not None
        else None
    )
    expanded = os.path.expanduser(path)
    absolute = os.path.abspath(expanded)
    resolved = os.path.realpath(absolute)
    if is_credential_path(absolute) or is_credential_path(resolved):
        raise HostTextFileError(403, "forbidden_path", "credential files cannot be read")
    try:
        mode = os.stat(resolved).st_mode
    except FileNotFoundError as exc:
        raise HostTextFileError(404, "not_found", "file does not exist") from exc
    if stat.S_ISDIR(mode):
        raise HostTextFileError(
            400, "is_a_directory", "path is a directory; use /filesystem to list it"
        )
    if not stat.S_ISREG(mode):
        # A FIFO or device would block the host's worker thread on open.
        raise HostTextFileError(400, "not_a_regular_file", "path is not a regular file")
    try:
        # Open the resolved path the deny-list checked, not the path as given.
        with open(resolved, "rb") as handle:
            if byte_range is not None:
                return _read_range(handle, absolute, *byte_range)
            # One extra byte detects an oversized file without reading all of it.
            raw = handle.read(MAX_TEXT_FILE_BYTES + 1)
    except FileNotFoundError as exc:
        raise HostTextFileError(404, "not_found", "file does not exist") from exc
    except PermissionError as exc:
        raise HostTextFileError(403, "permission_denied", "permission denied") from exc
    except OSError as exc:
        raise HostTextFileError(400, "unreadable", f"not a readable file: {exc.strerror}") from exc
    if len(raw) > MAX_TEXT_FILE_BYTES:
        raise HostTextFileError(
            413,
            "too_large",
            f"file is larger than {MAX_TEXT_FILE_BYTES} bytes; "
            "use offset/limit to read it in parts",
        )
    return {"object": "file", "path": absolute, "size": len(raw), "content": _decode(raw)}


def _decode(raw: bytes) -> str:
    """Decode ``raw`` as UTF-8, mapping a failure to a 400 ``not_utf8``."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HostTextFileError(400, "not_utf8", "file is not valid UTF-8") from exc


def _read_range(handle: BinaryIO, absolute: str, offset: int, limit: int) -> dict[str, Any]:
    """Read one range of an open file; reads at most ``limit`` plus 3 bytes.

    :param handle: The file, opened in binary mode.
    :param absolute: The path to report.
    :param offset: First byte, already validated.
    :param limit: Most bytes to return, already cut down to the maximum.
    :returns: The range payload described in :func:`read_text_file`.
    """
    stat_result = os.fstat(handle.fileno())
    size = stat_result.st_size
    raw = b""
    if offset < size:
        handle.seek(offset)
        raw = handle.read(limit)
    if raw and raw[0] & 0xC0 == 0x80:
        raise HostTextFileError(400, "not_utf8", "offset is inside a UTF-8 character")
    length = len(raw)
    if raw and offset + length < size:
        length = _complete_characters_length(raw)
        if length == 0:
            # ``limit`` is narrower than the character at ``offset``: return that one character.
            raw += handle.read(_utf8_width(raw[0]) - len(raw))
            length = len(raw)
    raw = raw[:length]
    end = offset + length
    return {
        "object": "file",
        "path": absolute,
        "size": size,
        "offset": offset,
        "length": length,
        "next_offset": end if end < size else None,
        "mtime_ns": stat_result.st_mtime_ns,
        "content": _decode(raw),
    }


__all__ = [
    "MAX_TEXT_FILE_BYTES",
    "HostTextFileError",
    "is_credential_path",
    "read_text_file",
    "validate_text_file_range",
]
