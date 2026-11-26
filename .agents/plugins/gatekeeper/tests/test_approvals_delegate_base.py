"""approvals.py: `approve merge --by delegate` reads `loop.delegate` from the default branch's policy at
`origin/<base>`, the one the owner approved, not from the checkout it runs in (#842)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from approvals_harness import (
    HEAD,
    add,
    approvals,
    gatekeeper_md,
    git_in,
    loop_md,
    pull,
)

REAL = approvals.base_policy  # the shared fixtures replace it per test
RULE = loop_md("risk:\n  paths:\n    - .github/**\n")
ALL = add(RULE, "loop", "delegate: all\n")
LOOP = Path("docs/agents/loop.md")


def committed(where: Path, loop: str) -> None:
    """`where`, a checkout whose last commit holds `loop` as its loop.md."""
    (where / "docs" / "agents").mkdir(parents=True, exist_ok=True)
    (where / LOOP).write_text(loop)
    git_in(where, "add", "-A")
    git_in(where, "commit", "-q", "-m", "policy")


def identify(where: Path) -> None:
    git_in(where, "config", "user.email", "loop@example.com")
    git_in(where, "config", "user.name", "loop")


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A clone of an origin whose main holds `RULE`, on a branch `feature`, run in; a PR #7 into main stubbed on
    GitHub, each comment posted collected in `posted`. A test sets main's policy with `main(<loop.md>)` and the
    branch's by writing its loop.md."""
    origin, work = tmp_path / "origin", tmp_path / "work"
    git_in(tmp_path, "init", "-q", "-b", "main", str(origin))
    identify(origin)
    committed(origin, RULE)
    git_in(tmp_path, "clone", "-q", str(origin), str(work))
    git_in(work, "checkout", "-q", "-b", "feature")
    identify(work)

    def main(loop: str) -> None:
        git_in(origin, "checkout", "-q", "main")
        committed(origin, loop)

    pr = pull(files=(".github/ci.yml",))
    posted: list[str] = []
    monkeypatch.setattr(approvals, "pull_request", lambda _n: pr)
    monkeypatch.setattr(approvals, "comment", lambda _n, body: posted.append(body))
    monkeypatch.setattr(approvals, "base_policy", REAL)
    monkeypatch.chdir(work)
    return {"main": main, "posted": posted, "work": work, "origin": origin}


def delegate(capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = approvals.main(["approve", "merge", "7", "--head", HEAD, "--by", "delegate"])
    return code, capsys.readouterr().out


def test_a_branch_that_sets_delegate_all_gets_no_delegates_go_into_main_while_mains_policy_lacks_it(
    checkout: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    LOOP.write_text(ALL)  # the branch's own edit, which main never approved
    code, out = delegate(capsys)
    assert code == 1 and "`loop.delegate: all`" in out and "main" in out
    assert (checkout["posted"], hub) == ([], {})
    committed(checkout["work"], ALL)  # committed on the branch, all the same
    assert delegate(capsys)[0] == 1
    assert (checkout["posted"], hub) == ([], {})


def test_mains_delegate_all_covers_a_branch_whose_policy_lacks_it(
    checkout: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    checkout["main"](ALL)  # main moved on after the branch was cut: the fetch reads it
    assert LOOP.read_text() == RULE
    assert delegate(capsys) == (
        0,
        f"PR #7: the delegate's go is on {HEAD}, under the owner's delegation\n",
    )
    assert [s["context"] for s in hub[HEAD]] == ["owner"]
    assert len(checkout["posted"]) == 1


def test_a_default_branch_with_no_policy_gives_no_delegates_go_whatever_the_checkout_says(
    checkout: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    origin = checkout["origin"]
    git_in(origin, "rm", "-q", str(LOOP))
    git_in(
        origin, "commit", "-q", "-m", "no policy"
    )  # main holds none: the policy is main's (#1047), so the checkout's `delegate: all` is no rule
    LOOP.write_text(ALL)
    assert delegate(capsys)[0] == 1
    assert hub == {}


def test_mains_gatekeeper_md_policy_is_read_as_the_checkouts_would_be(
    checkout: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Main moved its policy to gatekeeper.md's ## Policy with `delegate: all`; the branch still has loop.md's."""
    origin = checkout["origin"]
    git_in(origin, "rm", "-q", str(LOOP))
    (origin / "docs" / "agents").mkdir(parents=True)
    (origin / "docs" / "agents" / "gatekeeper.md").write_text(
        gatekeeper_md("risk:\n  paths:\n    - .github/**\nloop:\n  delegate: all\n")
    )
    git_in(origin, "add", "-A")
    git_in(origin, "commit", "-q", "-m", "gatekeeper")
    assert delegate(capsys)[0] == 0
    hub.clear()
    (origin / "docs" / "agents" / "gatekeeper.md").write_text(
        gatekeeper_md("loop:\n  delegate: feature\n")
    )
    git_in(origin, "commit", "-q", "-am", "feature")
    code, out = delegate(capsys)
    assert code == 1 and "main's gatekeeper.md" in out


def test_a_delegates_go_counts_only_while_mains_policy_still_permits_it(
    checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    status = {
        "context": "owner",
        "state": "success",
        "description": f"go on {HEAD[:12]}{approvals.DELEGATED}",
        "creator": {"login": "bot"},
    }
    monkeypatch.setattr(approvals, "statuses", lambda _head: [status])
    monkeypatch.setattr(approvals, "viewer", lambda: "bot")
    monkeypatch.setattr(approvals, "current", lambda *_a: True)
    pr = pull(files=(".github/ci.yml",))
    checkout["main"](ALL)
    assert approvals.go_on(pr, HEAD, approvals.project()[1]) == "delegate"
    approvals.main(["check", "merge", "7"])
    counted = capsys.readouterr().out
    assert "waits for the owner's go" not in counted
    checkout["main"](RULE)  # main drops `delegate: all`
    committed(checkout["work"], ALL)  # the branch keeps it
    assert approvals.go_on(pr, HEAD, approvals.project()[1]) == ""
    # the same through the user's path: `check merge` no longer finds the delegate's go
    code = approvals.main(["check", "merge", "7"])
    out = capsys.readouterr().out
    assert code == 1 and "waits for the owner's go" in out
