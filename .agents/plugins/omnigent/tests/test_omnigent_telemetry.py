"""scripts/omnigent_telemetry.py's analysis on synthetic rows, no Omnigent and no database."""

import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "omnigent_telemetry",
    Path(__file__).resolve().parents[1]
    / "skills"
    / "omnigent-telemetry"
    / "scripts"
    / "omnigent_telemetry.py",
)
assert spec and spec.loader
telemetry = importlib.util.module_from_spec(spec)
sys.modules["omnigent_telemetry"] = telemetry
spec.loader.exec_module(telemetry)

NOW = 1_800_000_000.0
SINCE = telemetry.window_start(7, NOW)


def session(sid: str, root: str, title: str = "", **kw: object) -> dict:
    row = {
        "id": sid,
        "parent_id": None if sid == root else root,
        "root_id": root,
        "title": title or f"session {sid}",
        "created_at": SINCE + 60,
        "updated_at": SINCE + 120,
        "archived": False,
        "items": 10,
        "agent": "codex-native-ui",
        "host": "host-a",
        "workspace": "/work",
        "live_status": 1,
        "sub_agent_name": None,
        "usage": {"total_cost_usd": 1.0},
        "compactions": 0,
        "inherited": 0,
    }
    return {**row, **kw}


def tree(root: str, children: int, codex: int = 0, titles: tuple[str, ...] = ()) -> list[dict]:
    rows = [session(root, root)]
    rows += [
        session(f"{root}-c{i}", root, sub_agent_name="Codex" if i < codex else None)
        for i in range(children)
    ]
    rows += [session(f"{root}-v{i}", root, title=t) for i, t in enumerate(titles)]
    return rows


def db(**kw: object) -> dict:
    return {
        "daily": [],
        "sessions": [],
        "subscriptions": [],
        "tasks": [],
        "sidebar": {"unarchived": 0, "from_tasks": 0},
        **kw,
    }


def flags(rows: dict) -> list[str]:
    return telemetry.build(None, rows, None, 7, NOW)["flags"]


def kinds(rows: dict) -> list[str]:
    return [f.split()[0] for f in flags(rows)]


def edge(subscriber: str, target: str) -> dict:
    return {
        "subscriber_id": subscriber,
        "subscriber_title": f"lead {subscriber}",
        "target_id": target,
        "target_title": f"worker {target}",
    }


def task(failed: int) -> dict:
    return {
        "id": "task0001",
        "name": "nightly",
        "state": 1,
        "workspace": None,
        "runs": 10,
        "ok": 10 - failed,
        "failed": failed,
        "skipped": 0,
        "last_error": "host_offline" if failed else None,
    }


def test_a_tree_explodes_at_its_sub_agent_or_nested_codex_threshold() -> None:
    assert kinds(db(sessions=tree("r1", telemetry.SUBAGENTS))) == ["SUBAGENT_EXPLOSION"]
    assert kinds(db(sessions=tree("r1", telemetry.SUBAGENTS - 1))) == []
    nested = telemetry.NESTED_CODEX
    assert kinds(db(sessions=tree("r1", nested, codex=nested))) == ["SUBAGENT_EXPLOSION"]
    assert kinds(db(sessions=tree("r1", nested, codex=nested - 1))) == []


def test_verifier_churn_fires_on_a_pass_past_the_cap_in_either_title_form() -> None:
    for title in ("s12-verifier-p4", "verify #12 pass 5"):
        assert kinds(db(sessions=tree("r1", 0, titles=(title,)))) == ["VERIFIER_CHURN"]
    quiet = ("s12-verifier-p3", "verify #12 pass 3", "omp-plan")
    assert kinds(db(sessions=tree("r1", 0, titles=quiet))) == []


def test_fork_bloat_counts_inherited_items_of_a_session_started_in_the_window() -> None:
    at = telemetry.INHERITED_ITEMS
    assert kinds(db(sessions=[session("f1", "f1", inherited=at, items=at + 5)])) == ["FORK_BLOAT"]
    assert kinds(db(sessions=[session("f1", "f1", inherited=at - 1, items=at)])) == []
    old = session("f1", "f1", inherited=at, items=at, created_at=SINCE - 1)
    assert kinds(db(sessions=[old])) == []


def test_subscription_amplification_fires_when_one_target_wakes_several_subscribers() -> None:
    many = [edge(f"s{i}", "t1") for i in range(telemetry.SUBSCRIBERS)]
    assert kinds(db(subscriptions=many)) == ["SUBSCRIPTION_AMPLIFICATION"]
    one_each = [edge(f"s{i}", f"t{i}") for i in range(telemetry.SUBSCRIBERS)]
    assert kinds(db(subscriptions=one_each)) == []


def test_cron_fail_loop_fires_at_its_failed_run_count() -> None:
    assert kinds(db(tasks=[task(telemetry.CRON_FAILURES)])) == ["CRON_FAIL_LOOP"]
    assert kinds(db(tasks=[task(telemetry.CRON_FAILURES - 1)])) == []


def test_sidebar_noise_counts_unarchived_sessions_scheduled_runs_created() -> None:
    at = telemetry.SIDEBAR_SESSIONS
    assert kinds(db(sidebar={"unarchived": 50, "from_tasks": at})) == ["SIDEBAR_NOISE"]
    assert kinds(db(sidebar={"unarchived": 50, "from_tasks": at - 1})) == []


def test_without_the_database_the_api_sections_render_and_the_rest_are_named() -> None:
    usage = {
        "cost_today": 1.5,
        "cost_last_7d": 12.25,
        "cost_last_30d": 40.0,
        "sessions": [
            {
                "id": "r1",
                "title": "lead",
                "updated_at": SINCE + 1,
                "cost_usd": 3.0,
                "models": {"model-a": 3.0},
            },
            {
                "id": "r0",
                "title": "older",
                "updated_at": SINCE - 1,
                "cost_usd": 9.0,
                "models": {"model-a": 9.0},
            },
        ],
    }
    left = dict.fromkeys(telemetry.DB_SECTIONS, "unreachable")
    r = telemetry.build(usage, None, None, 7, NOW, left)
    assert [t["id"] for t in r["trees"]] == ["r1"]
    assert r["models"] == [{"model": "model-a", "cost_usd": 3.0}]
    out = telemetry.text(r)
    assert "7 days $12.25" in out and "r1" in out
    assert all(f"{s}: unreachable" in out for s in telemetry.DB_SECTIONS)
    assert "$12.25" in telemetry.page(r) and "<script" not in telemetry.page(r)


def test_usage_decodes_in_each_frame_omnigent_writes() -> None:
    doc = b'{"total_cost_usd": 0.12}'
    assert telemetry.decode_usage((b"\x00\x00" + doc).hex()) == {"total_cost_usd": 0.12}
    assert telemetry.decode_usage(doc.hex()) == {"total_cost_usd": 0.12}
    assert telemetry.decode_usage(None) == {}


def test_server_names_the_omnigent_the_usage_is_read_from(monkeypatch, capsys) -> None:
    servers: list[str] = []

    class Named:
        def __init__(self, named: str = "") -> None:
            servers.append(named)

        def call(self, method: str, path: str, **_: object) -> dict:
            return {"sessions": []}

    def no_db(*_: object) -> None:
        raise telemetry.Fail("no database")

    monkeypatch.setattr(telemetry, "Omnigent", Named)
    monkeypatch.setattr(telemetry, "fetch_db", no_db)
    monkeypatch.setattr(telemetry, "codex_rate_limits", lambda: None)
    assert telemetry.main(["--server", "https://work.example", "--json"]) == 0
    assert servers == ["https://work.example"]


def test_the_usage_request_carries_the_window_and_a_long_timeout(monkeypatch, capsys) -> None:
    calls: list[tuple[str, str, object]] = []

    class Recorded:
        def __init__(self, named: str = "") -> None:
            pass

        def call(self, method: str, path: str, **kw: object) -> dict:
            calls.append((method, path, kw.get("timeout")))
            return {"sessions": []}

    def no_db(*_: object) -> None:
        raise telemetry.Fail("no database")

    monkeypatch.setattr(telemetry, "Omnigent", Recorded)
    monkeypatch.setattr(telemetry, "fetch_db", no_db)
    monkeypatch.setattr(telemetry, "codex_rate_limits", lambda: None)
    assert telemetry.main(["--days", "3", "--json"]) == 0
    assert calls == [("GET", "/v1/usage?days=3", 180.0)]
