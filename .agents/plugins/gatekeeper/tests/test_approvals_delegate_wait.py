"""approvals.py: a risk:high merge's wait names the delegated go where the base's policy lets it count (#1546)."""

from __future__ import annotations

from pathlib import Path

from approvals_harness import (  # noqa: F401
    HEAD,
    approvals,
    gating,
    judged_pr,
    judging,
    lands,
    loop_md,
)

OWNER = f"`approve merge 7 --head {HEAD} --by owner`"
DELEGATE = f"`approve merge 7 --head {HEAD} --by delegate`"


def wait(gating, capsys, monkeypatch, settings: str, **kwargs) -> str:
    state, _ = gating
    Path("docs/agents/loop.md").write_text(loop_md(settings))
    state["pull"] = judged_pr(judging("low — fine"), **kwargs)
    assert approvals.main(["check", "merge", "7"]) == approvals.WAITING
    return capsys.readouterr().out


def test_a_delegating_base_names_both_go_commands(gating, capsys, monkeypatch) -> None:
    out = wait(gating, capsys, monkeypatch, "loop:\n  delegate: all\n", labels=("risk:high",))
    assert f"({OWNER}, or the delegate's: {DELEGATE};" in out


def test_a_base_that_does_not_delegate_names_the_owner_and_the_setting(
    gating, capsys, monkeypatch
) -> None:
    out = wait(gating, capsys, monkeypatch, "loop:\n  delegate: feature\n", labels=("risk:high",))
    assert OWNER in out and "--by delegate" not in out
    assert "`loop.delegate: all` in the policy file lets a delegate's go count" in out


def test_an_owner_only_change_keeps_the_owners_wait(gating, capsys, monkeypatch) -> None:
    out = wait(
        gating,
        capsys,
        monkeypatch,
        "loop:\n  delegate: all\nlevels:\n  high:\n    merge: owner\n",
        labels=("risk:high",),
    )
    assert OWNER in out and "--by delegate" not in out and "loop.delegate" not in out
    assert "the delegate's go and `approved:merge` are no go" in out
