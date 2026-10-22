"""Browser e2e: streamed field changes patch project folders without refetching them.

Each expanded project folder fetches its own ``GET /v1/sessions?project=<name>``
list. The session-updates WebSocket patches a filed session's fields (title,
``updated_at``, status) into its folder row in place; only a membership change
(archived, moved) refetches a folder. This drives that over the real stream:
expand several folders, change one filed session's title a few times, and
assert the folder row shows each title while the page sends no folder request.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid

import httpx
from playwright.sync_api import Page, Request, expect

from tests._helpers.session import post_session_bundle
from tests.e2e_ui.conftest import _build_hello_world_bundle

_FOLDER_COUNT = 3
_TITLE_CHANGES = 3
# The server compares watched sessions every 4 s; leave room for one rescan.
_STREAM_WAIT_S = 15.0


def _seed_filed_session(base_url: str, *, title: str, project_id: str) -> str:
    """Create a session with *title* filed under *project_id*; return its id."""
    create_resp = post_session_bundle(
        httpx.post, f"{base_url}/v1/sessions", _build_hello_world_bundle(), timeout=30.0
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title, "project_id": project_id},
        timeout=10.0,
    ).raise_for_status()
    return session_id


def test_title_changes_patch_expanded_folders_without_refetching_them(
    page: Page,
    live_server: str,
) -> None:
    """Three expanded folders send no list request while a filed title changes."""
    uniq = uuid.uuid4().hex[:6]
    names = [f"E2E Folder {uniq} {index}" for index in range(_FOLDER_COUNT)]
    project_ids: list[str] = []
    session_ids: list[str] = []
    try:
        for index, name in enumerate(names):
            project_resp = httpx.post(
                f"{live_server}/v1/projects", json={"name": name}, timeout=10.0
            )
            project_resp.raise_for_status()
            project_ids.append(project_resp.json()["id"])
            session_ids.append(
                _seed_filed_session(
                    live_server, title=f"e2e-folder-{uniq}-{index}", project_id=project_ids[-1]
                )
            )

        page.add_init_script(
            "localStorage.setItem('omnigent:expanded-project-sections', "
            f"{json.dumps(json.dumps(names))})"
        )
        folder_requests: list[str] = []

        def record_folder_request(request: Request) -> None:
            if "/v1/sessions?" in request.url and "project=" in request.url:
                folder_requests.append(request.url)

        page.on("request", record_folder_request)
        page.goto(live_server)
        for index in range(_FOLDER_COUNT):
            expect(page.get_by_text(f"e2e-folder-{uniq}-{index}", exact=True)).to_be_visible()
        assert len(folder_requests) >= _FOLDER_COUNT, "every expanded folder loads its list"

        folder_requests.clear()
        for change in range(_TITLE_CHANGES):
            title = f"e2e-folder-{uniq}-0-renamed-{change}"
            httpx.patch(
                f"{live_server}/v1/sessions/{session_ids[0]}",
                json={"title": title},
                timeout=10.0,
            ).raise_for_status()
            # The stream patched the folder row in place (now marked unread).
            expect(page.get_by_text(title)).to_be_visible(timeout=_STREAM_WAIT_S * 1000)
        # Let any debounced invalidation from the last frame fire.
        time.sleep(1.0)

        assert folder_requests == []
    finally:
        for session_id in session_ids:
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        for project_id in project_ids:
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{live_server}/v1/projects/{project_id}", timeout=10.0)
