"""omnigent_agent.py: ``start --top-level --direct`` marks an owner-driven lead at launch (#1128)."""

from pathlib import Path

import pytest
import test_omnigent_agent as base  # the faked server
from test_omnigent_agent import no_model, no_sleep  # noqa: F401  (the fixtures apply here by name)

omnigent_agent = base.omnigent_agent


def test_a_direct_top_level_start_writes_follow_and_direct_and_a_re_arm_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = base.FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    registry, brief = tmp_path / "registry", tmp_path / "b.md"
    brief.write_text("discuss X")
    args = ["start", "claude", "t", str(brief), str(tmp_path / "a.md"), "--top-level"]
    assert omnigent_agent.main([*args, "--registry", str(registry), "--direct"]) == 0
    line = registry.read_text().strip()
    assert line.endswith("\tfollow\tdirect")
    sid, report, briefs, verdict, followed, after, direct, leads = omnigent_agent.entry(line)
    assert (followed, direct, verdict, after, leads) == (True, True, False, "", "")
    omnigent_agent.arm(registry, sid, report, briefs, followed=True)  # a re-arm keeps the mark
    assert omnigent_agent.entry(registry.read_text().strip())[6] is True
    capsys.readouterr()
    assert omnigent_agent.main([*args[:-1], "--registry", str(registry), "--direct"]) == 1
    assert "--direct needs --top-level and a registry" in capsys.readouterr().err


def test_a_top_level_start_without_direct_writes_no_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og = base.FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    registry, brief = tmp_path / "registry", tmp_path / "b.md"
    brief.write_text("do X")
    args = ["start", "claude", "t", str(brief), str(tmp_path / "a.md"), "--top-level"]
    assert omnigent_agent.main([*args, "--registry", str(registry)]) == 0
    assert omnigent_agent.entry(registry.read_text().strip())[6] is False
