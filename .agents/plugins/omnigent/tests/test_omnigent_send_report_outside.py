"""omnigent_agent.py send: an absolute report outside a remote session's workspace is refused up front (#1586)."""

from pathlib import Path

import pytest
from test_omnigent_agent import FakeOmnigent, FarFiles, omnigent_agent


def test_send_to_a_remote_session_refuses_an_absolute_report_outside_its_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Far(FarFiles, FakeOmnigent):
        pass

    og = Far(reply="done")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    sid = og.create("claude", "t", workspace="/elsewhere/repo")  # another host's path
    (tmp_path / "a.md").write_text("use main")
    base = ["send", sid, "a.md", "--confirm-seconds", "0", "--timeout", "5"]
    bad = str(tmp_path / "tmp/loop/r.md")  # a local absolute path: nothing there is the session's
    assert omnigent_agent.main([*base[:3], bad, *base[3:]]) != 0
    assert "is outside the child's workspace /elsewhere/repo" in capsys.readouterr().err
    assert not og.files  # nothing was written to the host
    # inside its workspace, or relative to it, the check lets the send through to wait
    for ok in ("/elsewhere/repo/tmp/loop/r.md", "tmp/loop/r.md"):
        omnigent_agent.main([*base[:3], ok, *base[3:]])
        assert "outside the child's workspace" not in capsys.readouterr().err, ok


def test_a_remote_workspace_is_compared_as_the_session_gave_it_not_resolved_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A path that is a symlink on this host is another host's plain path: resolving it here
    refused an absolute report inside the session's workspace."""

    class Far(FarFiles, FakeOmnigent):
        pass

    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    og = Far(reply="done")
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    sid = og.create("claude", "t", workspace=str(tmp_path / "link"))
    (tmp_path / "a.md").write_text("use main")
    report = str(tmp_path / "link/tmp/loop/r.md")
    omnigent_agent.main(["send", sid, "a.md", report, "--confirm-seconds", "0", "--timeout", "5"])
    assert "outside the child's workspace" not in capsys.readouterr().err
