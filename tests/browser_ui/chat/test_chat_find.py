"""The chat's find bar stays usable while floating warning banners show."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.chat._geometry_helpers import DESKTOP, PHONE, assert_no_overlap, box
from tests.browser_ui.chat.session_contract import ChatSessionContract

# Every stream slot held by another holder: the chat opens over budget and
# raises the too-many-tabs banner, as when other tabs hold all the slots.
_HOLD_STREAM_SLOTS = """
for (let i = 0; i < 30; i += 1) {
  navigator.locks.request(`omnigent:stream-slot:${i}`, () => new Promise(() => {}));
}
"""
_COVERED_CONTROLS = """(bar) => [...bar.querySelectorAll('input, button')].filter((el) => {
  const r = el.getBoundingClientRect();
  return !el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
}).map((el) => el.getAttribute('aria-label'))"""


@pytest.mark.parametrize("viewport", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_find_bar_stacks_above_the_too_many_tabs_banner(
    page: Page,
    chat_session_contract: ChatSessionContract,
    viewport: dict[str, int],
) -> None:
    """A banner stacks under the find bar instead of covering its controls."""
    chat = chat_session_contract
    chat.seed_transcript(4)
    chat.contract.json(
        f"/v1/sessions/{chat.session_id}/search",
        {"data": [], "has_more": False, "next_before": None},
    )
    page.set_viewport_size(viewport)
    page.add_init_script(_HOLD_STREAM_SLOTS)
    page.goto(f"{chat.url}?find=absent")

    bar = page.get_by_role("search")
    banner = page.get_by_role("status").filter(has_text="You have a lot of conversations open")
    expect(bar.get_by_text("No results")).to_be_visible(timeout=20_000)
    expect(banner).to_be_visible()

    assert bar.evaluate(_COVERED_CONTROLS) == []
    assert_no_overlap(box(bar), box(banner))
    assert box(bar)["y"] < box(banner)["y"]
