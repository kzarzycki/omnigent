"""``GET /v1/debug/profile``: admin-only sampling profiler."""

from __future__ import annotations

import asyncio
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
from fastapi import FastAPI

from omnigent.server.auth import delegated_path_allowed
from omnigent.server.routes import debug_profile
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.util.stack_sampler import format_report, sample_stacks
from tests._helpers.polling import poll_until_async

_ADMIN = "admin@profile.test"
_USER = "user@profile.test"


@pytest.fixture(autouse=True)
def _multi_user_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep an unauthenticated caller from resolving to the single local user."""
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "")


def _spin_until_stopped(stop: list[bool]) -> None:
    while not stop:
        sum(range(200))


@contextmanager
def _busy_thread() -> Iterator[None]:
    stop: list[bool] = []
    thread = threading.Thread(target=_spin_until_stopped, args=(stop,), name="busy-worker")
    thread.start()
    try:
        yield
    finally:
        stop.append(True)
        thread.join()


def _busy_stack_count(report: str) -> tuple[int, int]:
    """Return the busy loop's sample count and the highest count of any stack.

    Idle threads are sampled every round too, so the busy loop ties with them at the
    top rather than outranking them.
    """
    counts = {}
    for line in report.split("\n\n", 1)[0].splitlines():
        stack, _, count = line.rpartition(" ")
        counts[stack] = int(count)
    busy = [count for stack, count in counts.items() if stack.endswith(":_spin_until_stopped")]
    assert len(busy) == 1, report
    return busy[0], max(counts.values())


def _sampler_is_running() -> bool:
    return any(thread.name == "omnigent-stack-sampler" for thread in threading.enumerate())


def _admin_headers(db_uri: str) -> dict[str, str]:
    SqlAlchemyPermissionStore(db_uri).ensure_user(_ADMIN, is_admin=True)
    return {"X-Forwarded-Email": _ADMIN}


def test_busy_loop_is_the_top_stack_and_the_sampler_leaves_itself_out() -> None:
    with _busy_thread():
        report = sample_stacks(1.0)

    stacks, _, rest = report.partition("\n\n# samples per thread\n")
    busy_count, top_count = _busy_stack_count(report)
    assert busy_count > 20
    assert busy_count >= 0.9 * top_count
    assert any(
        line.startswith("threading.py:_bootstrap;") and ":_spin_until_stopped " in line
        for line in stacks.splitlines()
    )
    assert "busy-worker" in rest
    assert "omnigent-stack-sampler" not in rest
    assert "# interval requested 10.0 ms, achieved" in rest


@pytest.mark.asyncio
async def test_admin_gets_collapsed_stacks_over_http(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    with _busy_thread():
        response = await auth_client.get(
            "/v1/debug/profile?seconds=1", headers=_admin_headers(db_uri)
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    busy_count, top_count = _busy_stack_count(response.text)
    assert busy_count >= 0.9 * top_count


@pytest.mark.asyncio
async def test_profile_refuses_unauthenticated_and_non_admin_callers(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    SqlAlchemyPermissionStore(db_uri).ensure_user(_USER, is_admin=False)

    unauthenticated = await auth_client.get("/v1/debug/profile?seconds=1")
    non_admin = await auth_client.get(
        "/v1/debug/profile?seconds=1", headers={"X-Forwarded-Email": _USER}
    )

    assert unauthenticated.status_code == 401
    assert non_admin.status_code == 403


def test_delegated_tokens_are_confined_away_from_the_profile_path() -> None:
    assert not delegated_path_allowed("/v1/debug/profile")


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", ["0", "61", "-1"])
async def test_profile_seconds_outside_range_is_a_400(
    auth_client: httpx.AsyncClient, db_uri: str, seconds: str
) -> None:
    response = await auth_client.get(
        f"/v1/debug/profile?seconds={seconds}", headers=_admin_headers(db_uri)
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_second_profile_during_a_running_one_is_a_409(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    headers = _admin_headers(db_uri)
    first = asyncio.create_task(auth_client.get("/v1/debug/profile?seconds=2", headers=headers))
    await poll_until_async(_sampler_is_running, timeout_s=5)

    second = await auth_client.get("/v1/debug/profile?seconds=1", headers=headers)

    assert second.status_code == 409
    assert (await first).status_code == 200
    third = await auth_client.get("/v1/debug/profile?seconds=1", headers=headers)
    assert third.status_code == 200


def _live_samplers() -> int:
    return sum(thread.name == "omnigent-stack-sampler" for thread in threading.enumerate())


@pytest.mark.asyncio
async def test_cancelled_request_ends_its_sampler_before_the_next_profile_starts(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    headers = _admin_headers(db_uri)
    first = asyncio.create_task(auth_client.get("/v1/debug/profile?seconds=30", headers=headers))
    await poll_until_async(lambda: _live_samplers() == 1)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    await poll_until_async(lambda: _live_samplers() == 0, timeout_s=2)
    second = await auth_client.get("/v1/debug/profile?seconds=1", headers=headers)
    assert second.status_code == 200


@pytest.mark.asyncio
async def test_client_disconnect_ends_the_sampler_and_frees_the_lock(
    auth_app: FastAPI, auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    headers = _admin_headers(db_uri)
    disconnected = asyncio.Event()

    request_sent = False

    async def receive() -> dict[str, object]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        pass

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/v1/debug/profile",
        "raw_path": b"/v1/debug/profile",
        "query_string": b"seconds=30",
        "root_path": "",
        "headers": [(b"x-forwarded-email", headers["X-Forwarded-Email"].encode())],
        "server": ("test", 80),
        "client": ("127.0.0.1", 1234),
    }
    request = asyncio.create_task(auth_app(scope, receive, send))
    await poll_until_async(lambda: _live_samplers() == 1)

    disconnected.set()
    await asyncio.wait_for(request, timeout=5)

    assert _live_samplers() == 0
    again = await auth_client.get("/v1/debug/profile?seconds=1", headers=headers)
    assert again.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(("query", "expected_seconds"), [("", 30), ("?seconds=60", 60)])
async def test_default_and_maximum_window_reach_the_sampler(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    expected_seconds: int,
) -> None:
    windows: list[float] = []

    def fake_sample(duration_s: float, **_: object) -> str:
        windows.append(duration_s)
        return "stub\n"

    monkeypatch.setattr(debug_profile, "sample_stacks", fake_sample)

    response = await auth_client.get(f"/v1/debug/profile{query}", headers=_admin_headers(db_uri))

    assert response.status_code == 200
    assert windows == [expected_seconds]


def test_report_orders_stacks_by_count_totals_threads_and_reports_achieved_interval() -> None:
    report = format_report(
        Counter({"a.py:f": 9, "b.py:g": 10, "c.py:h": 100}),
        Counter({"worker": 7, "main": 12}),
        rounds=4,
        elapsed_s=0.2,
        interval_s=0.01,
    )

    assert report == (
        "c.py:h 100\n"
        "b.py:g 10\n"
        "a.py:f 9\n"
        "\n"
        "# samples per thread\n"
        "main 12\n"
        "worker 7\n"
        "\n"
        "# interval requested 10.0 ms, achieved 50.0 ms over 4 rounds in 0.2 s\n"
    )


def _park_until_released(release: threading.Event) -> None:
    release.wait()


def test_a_parked_thread_is_counted_once_per_round_in_stacks_and_thread_totals() -> None:
    release = threading.Event()
    thread = threading.Thread(target=_park_until_released, args=(release,), name="parked-worker")
    thread.start()
    try:
        report = sample_stacks(0.3)
    finally:
        release.set()
        thread.join()

    stacks, _, rest = report.partition("\n\n# samples per thread\n")
    totals, _, interval_line = rest.partition("\n\n")
    parked_stack_samples = sum(
        int(line.rpartition(" ")[2])
        for line in stacks.splitlines()
        if "_park_until_released" in line
    )
    parked_total = next(
        int(line.rpartition(" ")[2])
        for line in totals.splitlines()
        if line.startswith("parked-worker ")
    )
    rounds = int(interval_line.split(" over ")[1].split(" rounds")[0])
    assert parked_stack_samples == parked_total == rounds
