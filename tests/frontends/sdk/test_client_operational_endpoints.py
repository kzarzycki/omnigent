"""Operational endpoint wrappers on :class:`OmnigentClient`.

Server info, the usage report and the hosts list are plain GETs; each
wrapper must hit its route and hand back the server's data without the
caller reaching into the private HTTP client.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from omnigent_client._client import OmnigentClient
from omnigent_client._errors import OmnigentError

from omnigent.server.schemas import UsageReport


def _mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[OmnigentClient, httpx.AsyncBaseTransport, httpx.MockTransport]:
    """Build an :class:`OmnigentClient` whose transport is *handler*.

    The loopback base URL makes the client ignore ambient proxy settings,
    which would otherwise route requests around the swapped transport.

    :returns: The client, its original transport (restore before closing)
        and the mock transport (close in teardown).
    """
    client = OmnigentClient("http://127.0.0.1")
    original = client._http._transport
    mock_transport = httpx.MockTransport(handler)
    client._http._transport = mock_transport
    return client, original, mock_transport


async def _close(
    client: OmnigentClient,
    original: httpx.AsyncBaseTransport,
    mock_transport: httpx.MockTransport,
) -> None:
    client._http._transport = original
    await client.close()
    await mock_transport.aclose()


@pytest.mark.asyncio
async def test_info_returns_server_info() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(
            200,
            json={"server_version": "0.19.0.dev0", "single_user": True, "accounts_enabled": False},
        )

    client, original, mock_transport = _mock_client(handler)
    try:
        info = await client.info()
    finally:
        await _close(client, original, mock_transport)

    assert calls == ["GET /v1/info"]
    assert info == {
        "server_version": "0.19.0.dev0",
        "single_user": True,
        "accounts_enabled": False,
    }


@pytest.mark.asyncio
async def test_usage_returns_usage_report() -> None:
    calls: list[str] = []
    report: dict[str, Any] = {
        "object": "usage_report",
        "cost_today": 0.5,
        "cost_last_7d": 1.25,
        "cost_last_30d": 3.0,
        "total_cost_usd": 3.0,
        "daily_costs": [{"day": "2026-11-08", "cost_usd": 0.5}],
        "sessions": [
            {
                "id": "conv_abc",
                "created_at": 1_700_000_000,
                "updated_at": 1_700_000_100,
                "cost_usd": 1.25,
                "models": {"claude-opus-4-8": 1.0, "claude-haiku-5-5": 0.25},
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json=report)

    client, original, mock_transport = _mock_client(handler)
    try:
        usage = await client.usage()
    finally:
        await _close(client, original, mock_transport)

    assert calls == ["GET /v1/usage"]
    assert isinstance(usage, UsageReport)
    assert usage.total_cost_usd == 3.0
    assert usage.cost_today == 0.5
    assert usage.cost_last_7d == 1.25
    assert [(day.day, day.cost_usd) for day in usage.daily_costs] == [("2026-11-08", 0.5)]
    assert [session.id for session in usage.sessions] == ["conv_abc"]
    assert usage.sessions[0].cost_usd == 1.25
    assert usage.sessions[0].models == {"claude-opus-4-8": 1.0, "claude-haiku-5-5": 0.25}


@pytest.mark.asyncio
async def test_usage_401_raises_omnigent_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            401, json={"error": {"code": "unauthorized", "message": "Authentication required"}}
        )

    client, original, mock_transport = _mock_client(handler)
    try:
        with pytest.raises(OmnigentError) as exc_info:
            await client.usage()
    finally:
        await _close(client, original, mock_transport)

    assert "Authentication required" in str(exc_info.value)


@pytest.mark.asyncio
async def test_usage_malformed_body_raises_omnigent_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"object": "usage_report", "cost_today": "lots"})

    client, original, mock_transport = _mock_client(handler)
    try:
        with pytest.raises(OmnigentError, match="GET /v1/usage returned an unexpected body"):
            await client.usage()
    finally:
        await _close(client, original, mock_transport)


@pytest.mark.asyncio
async def test_list_hosts_returns_hosts() -> None:
    calls: list[str] = []
    host = {
        "host_id": "host_a1",
        "name": "laptop",
        "owner": "local",
        "status": "online",
        "sandbox_provider": None,
        "configured_harnesses": {"claude": True},
        "gateway_inference": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={"hosts": [host]})

    client, original, mock_transport = _mock_client(handler)
    try:
        hosts = await client.list_hosts()
    finally:
        await _close(client, original, mock_transport)

    assert calls == ["GET /v1/hosts"]
    assert hosts == [host]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="missing-hosts"),
        pytest.param({"hosts": "none"}, id="hosts-not-a-list"),
        pytest.param({"hosts": ["host_a1"]}, id="host-not-an-object"),
    ],
)
async def test_list_hosts_malformed_body_raises_omnigent_error(body: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=body)

    client, original, mock_transport = _mock_client(handler)
    try:
        with pytest.raises(
            OmnigentError, match="GET /v1/hosts returned a malformed 'hosts' array"
        ):
            await client.list_hosts()
    finally:
        await _close(client, original, mock_transport)


def _host_file_pages(text: str, page_bytes: int) -> list[dict[str, Any]]:
    """Split ASCII ``text`` into range bodies the host file route would return."""
    size = len(text)
    starts = list(range(0, size, page_bytes))
    return [
        {
            "object": "file",
            "path": "/home/u/big.md",
            "size": size,
            "offset": start,
            "length": len(text[start : start + page_bytes]),
            "next_offset": start + page_bytes if start + page_bytes < size else None,
            "mtime_ns": 1,
            "content": text[start : start + page_bytes],
        }
        for start in starts
    ]


@pytest.mark.asyncio
async def test_read_host_file_follows_next_offset_to_the_end() -> None:
    text = "x" * 25
    pages = {page["offset"]: page for page in _host_file_pages(text, 10)}
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.url.raw_path.decode()}")
        return httpx.Response(200, json=pages[int(request.url.params["offset"])])

    client, original, mock_transport = _mock_client(handler)
    try:
        content = await client.read_host_file("host_a1", "~/big.md", max_bytes=100)
    finally:
        await _close(client, original, mock_transport)

    assert content == text
    assert calls == [f"/v1/hosts/host_a1/files/~/big.md?offset={offset}" for offset in (0, 10, 20)]


@pytest.mark.asyncio
async def test_read_host_file_over_max_bytes_raises() -> None:
    pages = {page["offset"]: page for page in _host_file_pages("x" * 25, 10)}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[int(request.url.params["offset"])])

    client, original, mock_transport = _mock_client(handler)
    try:
        with pytest.raises(OmnigentError, match="larger than 15 bytes"):
            await client.read_host_file("host_a1", "/home/u/big.md", max_bytes=15)
    finally:
        await _close(client, original, mock_transport)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"content": "x", "length": 1, "next_offset": 0}, id="offset-goes-back"),
        pytest.param({"content": "x", "next_offset": None}, id="missing-length"),
        pytest.param({"length": 1, "next_offset": None}, id="missing-content"),
    ],
)
async def test_read_host_file_malformed_range_raises(body: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=body)

    client, original, mock_transport = _mock_client(handler)
    try:
        with pytest.raises(OmnigentError, match="malformed range"):
            await client.read_host_file("host_a1", "/home/u/big.md", max_bytes=100)
    finally:
        await _close(client, original, mock_transport)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "new_text,new_mtime",
    [
        pytest.param("n" * 25, 2, id="mtime-changes"),
        pytest.param("n" * 26, 1, id="size-changes-within-one-mtime-tick"),
    ],
)
async def test_read_host_file_restarts_when_the_file_changes_between_pages(
    new_text: str, new_mtime: int
) -> None:
    old_pages = {page["offset"]: page for page in _host_file_pages("o" * 25, 10)}
    new_pages = {
        page["offset"]: {**page, "mtime_ns": new_mtime} for page in _host_file_pages(new_text, 10)
    }
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        calls.append(offset)
        # The file is rewritten after the first page was served.
        pages = old_pages if len(calls) == 1 else new_pages
        return httpx.Response(200, json=pages[offset])

    client, original, mock_transport = _mock_client(handler)
    try:
        content = await client.read_host_file("host_a1", "~/big.md", max_bytes=100)
    finally:
        await _close(client, original, mock_transport)

    assert content == new_text
    assert calls == [0, 10, 0, 10, 20]


@pytest.mark.asyncio
async def test_read_host_file_gives_up_on_a_file_that_keeps_changing() -> None:
    pages = {page["offset"]: page for page in _host_file_pages("x" * 25, 10)}
    served = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal served
        served += 1
        return httpx.Response(
            200, json={**pages[int(request.url.params["offset"])], "mtime_ns": served}
        )

    client, original, mock_transport = _mock_client(handler)
    try:
        with pytest.raises(OmnigentError, match="kept changing"):
            await client.read_host_file("host_a1", "~/big.md", max_bytes=100)
    finally:
        await _close(client, original, mock_transport)
