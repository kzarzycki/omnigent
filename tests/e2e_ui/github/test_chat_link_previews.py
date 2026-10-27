"""E2E: chat link previews ask only this server, and only for links it can read.

A deterministic assistant message (``external_assistant_message``, no LLM run)
carries a GitHub PR link, links to other hosts, and links to an open and a
refused session on this server. GitHub's preview route is intercepted, as the
other GitHub UI tests intercept provider responses; session previews go through
the real server's permission-checked session read.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import httpx
from playwright.sync_api import Page, Request, Route, expect

_AGENT_NAME = "hello_world"

_PR_PREVIEW = {
    "connected": True,
    "preview": {
        "kind": "pull",
        "title": "Fix rendering on narrow screens",
        "state": "merged",
        "labels": [{"name": "bug", "color": "d73a4a", "description": None}],
        "checks": {"passed": 5, "failed": 1, "pending": 0},
    },
}


def test_chat_link_previews_fetch_only_from_this_server(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """GitHub and session links preview; links to other hosts make no request."""
    base_url, session_id = seeded_session
    message = "\n\n".join(
        [
            "Landed https://github.com/acme/app/pull/42 today.",
            "Leaked https://attacker.example/collect?d=secret-token and"
            " https://github.com.attacker.example/acme/app/pull/42.",
            f"[this chat]({base_url}/c/{session_id}) and"
            f" [a hidden chat]({base_url}/c/conv_not_visible_here)",
        ]
    )
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": _AGENT_NAME, "text": message},
        },
        timeout=10.0,
    ).raise_for_status()
    title = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json().get("title")

    app_host = urlsplit(base_url).netloc
    off_origin: list[str] = []
    session_reads: list[str] = []

    def record(request: Request) -> None:
        url = urlsplit(request.url)
        if url.scheme in ("http", "https") and url.netloc != app_host:
            off_origin.append(request.url)
        if re.fullmatch(r"/v1/sessions/conv_not_visible_here", url.path):
            session_reads.append(url.path)

    previews: list[str] = []

    def github_preview(route: Route) -> None:
        previews.append(urlsplit(route.request.url).path)
        route.fulfill(json=_PR_PREVIEW)

    page.on("request", record)
    page.route(re.compile(r"/v1/connections/github/repos/[^/]+/[^/]+/previews/"), github_preview)
    page.goto(f"{base_url}/c/{session_id}")

    expect(page.get_by_label("Pull request status: Merged")).to_be_visible(timeout=30_000)
    assert previews == ["/v1/connections/github/repos/acme/app/previews/pull/42"]
    expect(
        page.get_by_role("link", name="attacker.example/collect?d=secret-token")
    ).to_be_visible()

    page.get_by_role("link", name="app#42").hover()
    card = page.get_by_test_id("github-link-preview")
    expect(card).to_contain_text("Fix rendering on narrow screens")
    expect(card).to_contain_text("bug")
    expect(card).to_contain_text("Checks: 1 failed, 5 passed")
    page.mouse.move(0, 0)
    expect(card).to_have_count(0)

    page.get_by_role("link", name="this chat").hover()
    session_card = page.get_by_test_id("session-link-preview")
    expect(session_card).to_contain_text(title or "Untitled session")
    page.mouse.move(0, 0)
    expect(session_card).to_have_count(0)

    page.get_by_role("link", name="a hidden chat").hover()
    page.wait_for_timeout(500)
    assert session_reads, "the hidden session link should still ask the server"
    expect(session_card).to_have_count(0)

    assert off_origin == []
