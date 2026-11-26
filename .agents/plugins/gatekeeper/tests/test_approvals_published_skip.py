"""approvals.py: publish() pushes no pass that origin already holds as its record branch (#1057)."""

from __future__ import annotations

from typing import Any

import pytest
from approvals_harness import approvals

HEAD_SHA = "a" * 40
TREE_SHA = "b" * 40
REF = "refs/heads/loop/records/one"


def pass_of(command: str, **more: Any) -> dict[str, Any]:
    return {
        "head": HEAD_SHA,
        "tree": TREE_SHA,
        "command": command,
        "exit": 0,
        "time": "2026-01-01T00:00:00Z",
        **more,
    }


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """publish()'s pushes (push_record) and `gate` statuses (shown), recorded instead of made."""
    seen: dict[str, list[Any]] = {"pushed": [], "shown": []}

    def push(record: dict[str, Any]) -> str:
        seen["pushed"].append(record["command"])
        return f"refs/heads/loop/records/{record['command']}"

    def show(head: str, record: dict[str, Any]) -> None:
        seen["shown"].append(head)

    monkeypatch.setattr(approvals, "push_record", push)
    monkeypatch.setattr(approvals, "shown", show)
    return seen


def test_a_single_pass_origin_holds_is_not_pushed_again_and_says_it_is_in_origin(
    calls: dict[str, list[Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    approvals.publish(HEAD_SHA, pass_of("check", published=True, ref=REF))
    out = capsys.readouterr().out
    assert calls["pushed"] == []
    assert f"already in origin as the record branch {REF}" in out
    assert "not pushed to origin" not in out
    assert calls["shown"] == [HEAD_SHA]


def test_a_pass_not_yet_in_origin_is_pushed(
    calls: dict[str, list[Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    approvals.publish(HEAD_SHA, pass_of("check"))
    out = capsys.readouterr().out
    assert calls["pushed"] == ["check"]
    assert "published in origin as the record branch refs/heads/loop/records/check" in out
    assert "already in origin" not in out
    assert calls["shown"] == [HEAD_SHA]


def test_a_steps_pass_pushes_only_its_steps_not_yet_in_origin(
    calls: dict[str, list[Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    steps = [pass_of("lint", published=True, ref=REF), pass_of("test")]
    approvals.publish(HEAD_SHA, pass_of("check", steps=steps))
    out = capsys.readouterr().out
    assert calls["pushed"] == ["test"]
    assert f"already in origin as the record branch {REF}" in out
    assert "not pushed to origin" not in out
    assert calls["shown"] == [HEAD_SHA]
