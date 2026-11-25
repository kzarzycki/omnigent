"""Server CPU-seconds per ordinary request, in accounts-auth mode.

Seeds a throwaway SQLite corpus, boots ``omnigent server`` with accounts auth,
signs in as an invited non-admin member who owns the sessions, then drives
``GET /v1/sessions``, ``/v1/sessions/{id}`` and ``/v1/sessions/{id}/items`` at
a fixed rate and reports the server process's CPU time divided by requests.

    uv run --no-sync dev/benchmarks/omnigent/request_cpu.py --requests 300 --rate 20
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from dev.benchmarks.omnigent import seed as seed_corpus

_MEMBER = "member"
_PASSWORD = "bench-password-123"
_ADMIN_PASSWORD = "bench-admin-password-123"


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_ready(client: httpx.Client, deadline_s: float = 90.0) -> None:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            if client.get("/health").status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise RuntimeError("server did not become ready")


def _process_cpu_seconds(root: psutil.Process) -> float:
    procs = [root, *root.children(recursive=True)]
    total = 0.0
    for proc in procs:
        try:
            times = proc.cpu_times()
        except psutil.Error:
            continue
        total += times.user + times.system
    return total


def _save_profile(admin: httpx.Client, out: Path, seconds: int) -> None:
    """Save the server's sampled stacks (``/v1/debug/profile``) for one route."""
    response = admin.get("/v1/debug/profile", params={"seconds": max(1, seconds - 1)})
    out.write_text(response.text)


def _drive(client: httpx.Client, path: str, requests: int, rate: float) -> None:
    interval = 1.0 / rate
    next_at = time.monotonic()
    for _ in range(requests):
        response = client.get(path)
        response.raise_for_status()
        next_at += interval
        delay = next_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)


def run(
    sessions: int, items: int, requests: int, rate: float, profile_dir: Path | None = None
) -> dict[str, float]:
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        db_path = tmp / "bench.db"
        db_uri = f"sqlite:///{db_path}"
        seed_corpus.seed(db_uri, sessions=sessions, items_per_session=items, projects=0)
        port = _free_port()
        base = f"http://127.0.0.1:{port}"
        env = {
            **os.environ,
            "OMNIGENT_AUTH_PROVIDER": "accounts",
            "OMNIGENT_ACCOUNTS_ENABLED": "1",
            "OMNIGENT_ACCOUNTS_COOKIE_SECRET": "ab" * 32,
            "OMNIGENT_ACCOUNTS_BASE_URL": base,
            "OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD": _ADMIN_PASSWORD,
        }
        omni = str(Path(sys.executable).with_name("omni"))
        log = (tmp / "server.log").open("wb")
        server = subprocess.Popen(
            [
                omni,
                "server",
                "--port",
                str(port),
                "--database-uri",
                db_uri,
                "--artifact-location",
                str(tmp / "artifacts"),
            ],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(base_url=base, timeout=30) as admin:
                _wait_ready(admin)
                # The bootstrap admin is named after the OS user.
                with sqlite3.connect(db_path) as conn:
                    admin_name = conn.execute(
                        "SELECT id FROM users WHERE password_hash IS NOT NULL LIMIT 1"
                    ).fetchone()[0]
                admin.post(
                    "/auth/login", json={"username": admin_name, "password": _ADMIN_PASSWORD}
                ).raise_for_status()
                invite = admin.post("/auth/invite", json={"is_admin": False}).json()["token"]
                with httpx.Client(base_url=base, timeout=30) as member:
                    member.post(
                        "/auth/register",
                        json={"invite": invite, "username": _MEMBER, "password": _PASSWORD},
                    ).raise_for_status()
                    # Hand the seeded sessions (owned by "local") to the member.
                    conn = sqlite3.connect(db_path)
                    conn.execute(
                        "UPDATE session_permissions SET user_id = ? WHERE user_id = 'local'",
                        (_MEMBER,),
                    )
                    conn.commit()
                    conn.close()
                    listing = member.get(
                        "/v1/sessions", params={"limit": 1, "visibility": "all"}
                    ).json()
                    rows = listing.get("data") or listing.get("sessions") or []
                    session_id = rows[0]["id"]
                    routes = {
                        "list_sessions": "/v1/sessions?visibility=all",
                        "get_session": f"/v1/sessions/{session_id}",
                        "get_items": f"/v1/sessions/{session_id}/items",
                    }
                    root = psutil.Process(server.pid)
                    results: dict[str, float] = {}
                    for name, path in routes.items():
                        _drive(member, path, 20, rate)  # warm-up
                        profiler = None
                        if profile_dir is not None:
                            profiler = threading.Thread(
                                target=_save_profile,
                                args=(admin, profile_dir / f"{name}.txt", int(requests / rate)),
                            )
                            profiler.start()
                        before = _process_cpu_seconds(root)
                        _drive(member, path, requests, rate)
                        if profiler is not None:
                            profiler.join()
                        results[name] = (_process_cpu_seconds(root) - before) / requests
                    return results
        finally:
            server.terminate()
            try:
                server.wait(timeout=20)
            except subprocess.TimeoutExpired:
                server.kill()
            log.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=200)
    parser.add_argument("--items-per-session", type=int, default=50)
    parser.add_argument("--requests", type=int, default=300)
    parser.add_argument("--rate", type=float, default=20.0, help="requests per second")
    parser.add_argument("--profile-dir", type=Path, help="write per-route sampled stacks here")
    args = parser.parse_args()
    if args.profile_dir is not None:
        args.profile_dir.mkdir(parents=True, exist_ok=True)
    results = run(
        args.sessions, args.items_per_session, args.requests, args.rate, args.profile_dir
    )
    print(json.dumps({k: round(v, 5) for k, v in results.items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
