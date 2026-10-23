"""Browser e2e for copying a session id and reading its details from the sidebar row menu.

The row menu is one component behind the desktop kebab, right-click and phone
long-press, so the id is reachable without opening the session. Nothing here
navigates into the session: the chat URL must stay on the landing page.
"""

from __future__ import annotations

import uuid

import httpx
from playwright.sync_api import Page, expect

from tests._helpers.session import post_session_bundle
from tests.e2e_ui.conftest import _build_hello_world_bundle


def _seed_session(base_url: str, *, title: str) -> str:
    resp = post_session_bundle(
        httpx.post, f"{base_url}/v1/sessions", _build_hello_world_bundle(), timeout=30.0
    )
    resp.raise_for_status()
    session_id = resp.json()["session_id"]
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}", json={"title": title}, timeout=10.0
    ).raise_for_status()
    return session_id


def test_row_menu_copies_id_and_shows_details_without_opening(
    page: Page, live_server: str
) -> None:
    """Copy session ID and Details… work from the row menu and leave the page where it is."""
    title = f"e2e-details-{uuid.uuid4().hex[:6]}"
    session_id = _seed_session(live_server, title=title)
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=live_server)
    page.goto(live_server)
    row = page.get_by_text(title, exact=True).first
    expect(row).to_be_visible()

    row.click(button="right")
    page.get_by_role("menuitem", name="Copy session ID").click()
    expect(page.get_by_text("Session ID copied")).to_be_visible()
    assert page.evaluate("navigator.clipboard.readText()") == session_id

    row.click(button="right")
    page.get_by_role("menuitem", name="Details…").click()
    dialog = page.get_by_test_id("session-details-dialog")
    expect(dialog.get_by_test_id("session-detail-Session ID")).to_have_text(session_id)
    expect(dialog.get_by_test_id("session-detail-Title")).to_have_text(title)
    dialog.get_by_test_id("copy-path-button").nth(1).click()
    assert page.evaluate("navigator.clipboard.readText()") == title
    assert "/c/" not in page.url


def test_row_menu_long_press_on_a_phone(page: Page, live_server: str) -> None:
    """At phone width a touch long-press opens the same menu and Details… works."""
    title = f"e2e-details-phone-{uuid.uuid4().hex[:6]}"
    session_id = _seed_session(live_server, title=title)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(live_server)
    page.get_by_role("button", name="Open sidebar").click()
    row = page.get_by_text(title, exact=True).first
    expect(row).to_be_visible()

    page.wait_for_timeout(500)  # the drawer finishes sliding in
    box = row.bounding_box()
    assert box is not None
    point = {"clientX": box["x"] + 5, "clientY": box["y"] + 5}
    touch = {"pointerType": "touch", "pointerId": 7, "button": 0, "isPrimary": True, **point}
    row.dispatch_event("pointerdown", touch)
    page.wait_for_timeout(900)
    page.get_by_role("menuitem", name="Details…").click()
    expect(page.get_by_test_id("session-detail-Session ID")).to_have_text(session_id)
    page.screenshot(path="tmp/loop/session-details/phone-details.png")
