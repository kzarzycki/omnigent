"""omnigent_agent.py: a claude-cloud start is judged against its own memory floor (#1671)."""

import importlib.util
from pathlib import Path

import pytest
import test_omnigent_agent as base  # the faked server
from test_omnigent_agent import no_model, no_sleep, trusted_folders  # noqa: F401  (by name)

omnigent_agent = base.omnigent_agent


def test_at_1300_mb_a_cloud_start_is_admitted_and_a_host_start_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.undo()  # the real capacity check, on a faked host
    monkeypatch.setattr(omnigent_agent, "cpu_idle", lambda: 90.0)
    monkeypatch.setattr(omnigent_agent, "available_mb", lambda: 1300.0)
    assert omnigent_agent.capacity(cpu=False) == (
        True,
        "cloud start, cpu not judged, 1300 MB available (min 1200)",
    )
    assert omnigent_agent.capacity() == (
        False,
        "cpu 90% idle (min 10), 1300 MB available (min 1536)",
    )


def test_a_host_s_row_and_its_override_set_the_host_floor_not_the_cloud_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert omnigent_agent.min_available_mb("cc-experiments") == 2500
    assert omnigent_agent.min_available_mb("cc-experiments", cloud=True) == 1200
    assert omnigent_agent.host_limits("cc-experiments", cloud=True) == (1200, 40)
    monkeypatch.setenv("OMNIGENT_MIN_AVAILABLE_MB_CC_EXPERIMENTS", "3000")
    assert omnigent_agent.min_available_mb("cc-experiments") == 3000
    assert omnigent_agent.min_available_mb("cc-experiments", cloud=True) == 1200
    assert omnigent_agent.min_available_mb("other") == omnigent_agent.MIN_AVAILABLE_MB


def test_the_variable_overrides_the_cloud_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_CLOUD_MIN_AVAILABLE_MB", "1400")
    spec = importlib.util.spec_from_file_location("fresh_omnigent_agent", base.spec.origin)
    assert spec and spec.loader
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)  # the floors are read at import, as the other knobs are
    assert fresh.min_available_mb("cc-experiments", cloud=True) == 1400
    monkeypatch.setattr(fresh, "available_mb", lambda: 1300.0)
    assert fresh.capacity(cpu=False)[0] is False
    assert fresh.min_available_mb("other") == 1536  # the host floor is untouched


def test_a_cloud_start_on_a_host_whose_row_says_2500_starts_at_1300_mb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = base.far_host(
        monkeypatch, tmp_path, "cc-experiments", meminfo=base.mem(1300), loadavg="3.00 2 1"
    )
    og.procs.setdefault("far", {})["/there/repo/.claude/settings.local.json"] = base.PINNED
    cloud = [*base.far_start("claude-cloud", "cc-experiments"), "--workspace", "/there/repo"]
    assert omnigent_agent.main(cloud) == 0 and og.sessions
    host = [*base.far_start("claude", "cc-experiments"), "--workspace", "/there/repo"]
    assert omnigent_agent.main(host) == 1  # a host start there: 1300 under its row's 2500


def test_capacity_cloud_judges_by_the_cloud_floor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.undo()  # the real capacity check, on a faked host
    monkeypatch.setattr(omnigent_agent, "cpu_idle", lambda: 90.0)
    monkeypatch.setattr(omnigent_agent, "available_mb", lambda: 1300.0)
    assert omnigent_agent.main(["capacity", "--cloud"]) == 0
    assert capsys.readouterr().out == "cloud start, cpu not judged, 1300 MB available (min 1200)\n"
    assert omnigent_agent.main(["capacity"]) == 1
