"""Browser e2e for "Show in sidebar": reveal the current session's row.

A session filed in a project hides in the sidebar while the Projects section is
collapsed, and a long Sessions list scrolls it out of view. The action must
expand the section, scroll the row into view, and flash it, from both the
header menu and the command palette.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import httpx
from playwright.sync_api import Locator, Page, expect

from tests._helpers.session import post_session_bundle
from tests.e2e_ui.conftest import _build_hello_world_bundle

_FLASH = re.compile(r"(?:^|\s)animate-sidebar-reveal(?:\s|$)")


def _create_session(base_url: str, title: str, project: str | None = None) -> str:
    """Create a real top-level session with a stable title, optionally filed."""
    create = post_session_bundle(
        httpx.post, f"{base_url}/v1/sessions", _build_hello_world_bundle(), timeout=30.0
    )
    create.raise_for_status()
    session_id = create.json()["session_id"]
    patch: dict[str, object] = {"title": title}
    if project:
        patch["labels"] = {"omni_project": project}
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}", json=patch, timeout=10.0
    ).raise_for_status()
    return session_id


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id*."""
    return page.locator(f'li[data-sidebar-session-id="{session_id}"]')


def _scroll_sidebar_to_bottom(page: Page, filler_id: str) -> None:
    """Scroll the sidebar so the last filler row is in view and the top is not."""
    _row(page, filler_id).scroll_into_view_if_needed()


def test_show_in_sidebar_expands_scrolls_and_flashes_row(
    page: Page,
    live_server: str,
    output_path: str,
) -> None:
    """The header menu and command palette both reveal a hidden session row."""
    artifacts = Path(output_path)
    artifacts.mkdir(parents=True, exist_ok=True)
    project = f"reveal-{uuid.uuid4().hex[:6]}"
    target = _create_session(live_server, "Reveal target session", project)
    fillers = [_create_session(live_server, f"Filler session {i:02d}") for i in range(30)]

    # The user collapsed Projects earlier, so the filed session is hidden.
    page.set_viewport_size({"width": 1400, "height": 800})
    collapsed = json.dumps(json.dumps(["Projects"]))
    page.add_init_script(
        f"localStorage.setItem('omnigent:collapsed-sidebar-sections', {collapsed})"
    )
    page.goto(f"{live_server}/c/{target}")
    expect(page.get_by_test_id("header-conversation-actions")).to_be_visible(timeout=30_000)
    expect(_row(page, fillers[0])).to_be_attached(timeout=30_000)
    _scroll_sidebar_to_bottom(page, fillers[0])
    expect(_row(page, target)).to_have_count(0)
    page.screenshot(path=str(artifacts / "1-before-hidden.png"))

    # Entry point 1: the session header menu.
    page.get_by_test_id("header-conversation-actions").click()
    page.get_by_role("menuitem", name="Show in sidebar").click()
    row = _row(page, target)
    expect(row).to_be_in_viewport()
    expect(row).to_have_class(_FLASH)
    page.wait_for_timeout(300)  # the ring's first peak
    page.screenshot(path=str(artifacts / "2-after-header-menu.png"))
    expect(page.get_by_role("button", name=project, exact=True)).to_have_attribute(
        "aria-expanded", "true"
    )
    stored = page.evaluate("localStorage.getItem('omnigent:collapsed-sidebar-sections')")
    assert "Projects" not in json.loads(stored or "[]")
    expect(row).not_to_have_class(_FLASH, timeout=5_000)

    # Entry point 2: the command palette, after scrolling away again.
    _scroll_sidebar_to_bottom(page, fillers[0])
    expect(row).not_to_be_in_viewport()
    page.screenshot(path=str(artifacts / "3-before-palette.png"))
    page.keyboard.press("ControlOrMeta+k")
    page.get_by_text("Show current session in sidebar").click()
    expect(row).to_be_in_viewport()
    expect(row).to_have_class(_FLASH)
    page.wait_for_timeout(300)
    page.screenshot(path=str(artifacts / "4-after-command-palette.png"))
