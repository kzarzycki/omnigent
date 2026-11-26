"""relay and send warn, and still deliver, when the message names an issue outside the lead's request or epic."""

import json
import re
from pathlib import Path

import pytest
from test_omnigent_agent import FakeOmnigent, omnigent_agent, relay

ISSUES = {  # the repository as GitHub answers: #1830 is the lead's story of epic #1800
    1830: {"__typename": "Issue", "parent": {"number": 1800}, "subIssues": {"nodes": []}},
    1800: {"__typename": "Issue", "parent": None, "subIssues": {"nodes": [{"number": 1830}]}},
    1831: {"__typename": "Issue", "parent": {"number": 1800}, "subIssues": {"nodes": []}},
    1777: {"__typename": "Issue", "parent": None, "subIssues": {"nodes": []}},
    1840: {"__typename": "PullRequest"},
}


@pytest.mark.parametrize(
    ("message", "warned"),
    [
        ("also look at #1777", "#1777"),
        ("fold in https://github.com/o/r/issues/1777 too", "#1777"),
        ("#1831 is in your epic", ""),
        ("o/r#1800 moved", ""),
        ("fix the finding on PR #1840", ""),
        ("other/repo#1777 is theirs", ""),
    ],
)
def test_relay_warns_on_an_issue_outside_the_leads_epic(
    message: str,
    warned: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "lead")
    og.land(sid, "# Lead: one lead per request (#1830)")
    og.labels[sid] = {"omni.role": "lead"}
    monkeypatch.setattr(omnigent_agent, "workspace_repo", lambda *_: "o/r")
    monkeypatch.setattr(omnigent_agent, "pack_behind", lambda *_: None)

    def gh(*args: str, **_: object) -> str:
        nums = map(int, re.findall(r"number: (\d+)\)", args[3]))
        return json.dumps({"data": {"repository": {f"n{n}": ISSUES.get(n) for n in nums}}})

    monkeypatch.setattr(omnigent_agent, "gh", gh)
    assert relay(og, sid, tmp_path, monkeypatch, message) == 0
    out = capsys.readouterr()
    assert out.out.strip() == f"session {sid}: delivered"
    assert (warned in out.err and "fresh lead" in out.err) if warned else out.err == ""


def test_relay_stays_quiet_when_the_epic_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = FakeOmnigent()
    sid = og.create("claude", "lead")
    og.land(sid, "# Lead (#1830)")
    og.labels[sid] = {"omni.role": "lead"}
    monkeypatch.setattr(omnigent_agent, "workspace_repo", lambda *_: "o/r")
    monkeypatch.setattr(omnigent_agent, "pack_behind", lambda *_: None)

    def gh(*_: str, **__: object) -> str:
        raise omnigent_agent.Fail("gh api graphql: HTTP 502")

    monkeypatch.setattr(omnigent_agent, "gh", gh)
    assert relay(og, sid, tmp_path, monkeypatch, "also #1777") == 0
    assert capsys.readouterr().err == ""
