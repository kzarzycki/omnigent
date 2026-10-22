"""Session ids in API paths canonicalize to the bare hex the stores return."""

from __future__ import annotations

import pytest

from omnigent.server.app import canonicalize_session_path

_HEX = "0ce214d6ac80416ba305b08ecd16763a"
_DASHED = "0ce214d6-ac80-416b-a305-b08ecd16763a"
_OTHER = "11111111222233334444555555555555"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (f"/v1/sessions/conv_{_HEX}", f"/v1/sessions/{_HEX}"),
        (f"/v1/sessions/{_HEX.upper()}/items", f"/v1/sessions/{_HEX}/items"),
        (f"/v1/sessions/{_DASHED}/resources", f"/v1/sessions/{_HEX}/resources"),
        (f"/v1/sessions/conv_{_DASHED}", f"/v1/sessions/{_HEX}"),
        (
            f"/v1/sessions/conv_{_HEX}/subscriptions/conv_{_OTHER}",
            f"/v1/sessions/{_HEX}/subscriptions/{_OTHER}",
        ),
        (f"/v1/sessions/{_HEX}", f"/v1/sessions/{_HEX}"),
        # Not a uuid: left for the route to 404.
        ("/v1/sessions/not-a-session/items", "/v1/sessions/not-a-session/items"),
        # Only the session-id positions; free-form segments keep their spelling.
        (
            f"/v1/sessions/{_HEX}/resources/github/diff/fixtures/msg_{_OTHER}",
            f"/v1/sessions/{_HEX}/resources/github/diff/fixtures/msg_{_OTHER}",
        ),
        (
            f"/v1/hosts/h1/filesystem/home/u/.omnigent/sessions/conv_{_HEX}",
            f"/v1/hosts/h1/filesystem/home/u/.omnigent/sessions/conv_{_HEX}",
        ),
    ],
)
def test_canonicalize_session_path(path: str, expected: str) -> None:
    assert canonicalize_session_path(path) == expected
