"""omnigent_agent.py: a claude-cloud start takes `auto` from the command line only (#1632)."""

from pathlib import Path

import pytest
import test_omnigent_agent as base  # the faked server
from test_omnigent_agent import no_model, no_sleep  # noqa: F401  (the fixtures apply here by name)

omnigent_agent = base.omnigent_agent


def cloud() -> base.FakeOmnigent:
    og = base.FakeOmnigent()
    og.harnesses = {"claude-cloud": omnigent_agent.CLOUD_HARNESS}
    return og


def test_auto_from_the_environment_alone_starts_a_cloud_worker_with_no_mode_and_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OMNIGENT_PERMISSION_MODE", "auto")
    og = cloud()
    omnigent_agent.start(
        og, "claude-cloud", "t", "do X", tmp_path / "r.md", None, 0, top_level=True
    )
    assert og.modes["s0"] is None
    err = capsys.readouterr().err
    assert "OMNIGENT_PERMISSION_MODE=auto is not applied" in err
    assert "bypassPermissions" not in err.split("not applied")[1].split("pass")[0]


def test_only_the_flag_starts_a_cloud_worker_in_auto(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OMNIGENT_PERMISSION_MODE", "auto")
    og = cloud()
    omnigent_agent.start(
        og, "claude-cloud", "t", "do X", tmp_path / "r.md", None, 0, permission_mode="auto"
    )
    assert og.modes["s0"] == "auto"
    assert "not applied" not in capsys.readouterr().err


def test_bypass_from_the_environment_is_not_applied_to_a_cloud_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OMNIGENT_PERMISSION_MODE", "bypassPermissions")
    og = cloud()
    omnigent_agent.start(og, "claude-cloud", "t", "do X", tmp_path / "r.md", None, 0)
    assert og.modes["s0"] is None
    assert "OMNIGENT_PERMISSION_MODE=bypassPermissions is not applied" in capsys.readouterr().err


def test_start_prints_the_mode_the_session_record_names_not_the_one_it_asked_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OMNIGENT_PERMISSION_MODE", raising=False)
    og = cloud()
    info = og.info
    og.info = lambda s: {**info(s), "permission_mode": "auto"}  # type: ignore[method-assign]
    omnigent_agent.start(
        og,
        "claude-cloud",
        "t",
        "do X",
        tmp_path / "r.md",
        None,
        0,
        permission_mode="bypassPermissions",
    )
    assert og.modes["s0"] == "bypassPermissions"  # asked for
    assert "session s0: permission mode auto" in capsys.readouterr().err  # got


@pytest.mark.parametrize(
    ("record", "mode"),
    [
        ({"permission_mode": "bypassPermissions"}, "bypassPermissions"),
        ({"labels": {"omnigent.acp.permission_mode": "auto"}}, "auto"),
        ({"metadata": {"omnigent.acp.permission_mode": "auto"}}, "auto"),
        ({"terminal_launch_args": ["--permission-mode", "plan", "--x"]}, "plan"),
        (
            {"terminal_launch_args": ["--dangerously-bypass-approvals-and-sandbox"]},
            "bypassPermissions",
        ),
        ({"terminal_launch_args": ["--dangerously-skip-permissions"]}, "bypassPermissions"),
        ({}, "not named in the session record (the harness default)"),
    ],
)
def test_granted_mode_reads_each_place_a_session_record_names_its_mode(
    record: dict, mode: str
) -> None:
    assert omnigent_agent.granted_mode(record) == mode


@pytest.mark.parametrize("top_level", [False, True])
def test_the_real_create_sends_a_cloud_mode_only_from_the_flag(
    monkeypatch: pytest.MonkeyPatch, top_level: bool
) -> None:
    monkeypatch.setattr(omnigent_agent, "own_id", lambda: "")
    monkeypatch.setattr(omnigent_agent, "parent_id", lambda: "p" if not top_level else None)
    calls = base._agent_server(
        monkeypatch,
        {
            "/v1/projects": {"data": []},
            "/v1/sessions": {"id": "s1"},
            "/v1/sessions/p": {"id": "p"},
        },
    )
    monkeypatch.setattr(
        omnigent_agent.Omnigent,
        "agent",
        lambda self, name: {"id": name, "harness": omnigent_agent.CLOUD_HARNESS},
    )
    omnigent_agent.Omnigent().create(
        "claude-cloud", "t", top_level=top_level, permission_mode="bypassPermissions"
    )
    body = calls[-1][2]
    assert body["permission_mode"] == "bypassPermissions"
    assert ("parent_session_id" in body) is not top_level
    monkeypatch.setenv("OMNIGENT_PERMISSION_MODE", "bypassPermissions")
    omnigent_agent.Omnigent().create("claude-cloud", "t", top_level=top_level)
    assert "permission_mode" not in calls[-1][2]


def test_an_unsupported_mode_from_the_environment_is_not_applied_to_a_cloud_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OMNIGENT_PERMISSION_MODE", "plan")
    omnigent_agent.start(cloud(), "claude-cloud", "t", "do X", tmp_path / "r.md", None, 0)
    err = capsys.readouterr().err
    assert "OMNIGENT_PERMISSION_MODE=plan is not applied" in err
    assert "--permission-mode plan" not in err
    assert "takes no permission mode" not in err


def test_an_unread_record_reports_its_mode_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OMNIGENT_PERMISSION_MODE", raising=False)
    og = cloud()

    def unreadable(sid: str) -> dict:
        raise omnigent_agent.Fail("GET: HTTP 503")

    og.info = unreadable  # type: ignore[method-assign]
    omnigent_agent.start(og, "claude-cloud", "t", "do X", tmp_path / "r.md", None, 0)
    assert "permission mode unknown (its record could not be read)" in capsys.readouterr().err
