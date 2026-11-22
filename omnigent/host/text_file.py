"""Single-file text read served over the host tunnel (``GET /v1/hosts/{id}/files/...``).

Stdlib-only so both the server (pre-check of the credential deny-list) and the
host (the read itself) can import it.
"""

from __future__ import annotations

import fnmatch
import os
from typing import Any

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
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    if not parts:
        return False
    if _SSH_DIRECTORY in parts[:-1]:
        return True
    return any(fnmatch.fnmatchcase(parts[-1], pattern) for pattern in _CREDENTIAL_BASENAMES)


def read_text_file(path: str) -> dict[str, Any]:
    """Read a small UTF-8 text file on this host.

    :param path: Absolute or ``~``-prefixed path; ``~`` expands against this host's home.
    :returns: ``{"object": "file", "path": <absolute path>, "size": <bytes>, "content": <text>}``.
    :raises HostTextFileError: 403 for a credential path (checked on the given and the
        symlink-resolved path), 404 when missing, 400 for a directory or non-UTF-8 bytes,
        413 over :data:`MAX_TEXT_FILE_BYTES`.
    """
    expanded = os.path.expanduser(path)
    absolute = os.path.abspath(expanded)
    resolved = os.path.realpath(absolute)
    if is_credential_path(absolute) or is_credential_path(resolved):
        raise HostTextFileError(403, "forbidden_path", "credential files cannot be read")
    if not os.path.exists(absolute):
        raise HostTextFileError(404, "not_found", "file does not exist")
    if os.path.isdir(absolute):
        raise HostTextFileError(
            400, "is_a_directory", "path is a directory; use /filesystem to list it"
        )
    try:
        with open(absolute, "rb") as handle:
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
            413, "too_large", f"file is larger than {MAX_TEXT_FILE_BYTES} bytes"
        )
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HostTextFileError(400, "not_utf8", "file is not valid UTF-8") from exc
    return {"object": "file", "path": absolute, "size": len(raw), "content": content}


__all__ = ["MAX_TEXT_FILE_BYTES", "HostTextFileError", "is_credential_path", "read_text_file"]
