"""approvals.py: `needs-owner` on the PR itself holds it from `land`, and the owner's go lifts it (#968)."""

from __future__ import annotations

from typing import Any

import pytest
from approvals_harness import (
    HEAD,
    approvals,
    lands,  # noqa: F401
    pull,
)

HELD = "#7 waits for the owner (needs-owner)"
HOLD = "#7 is held (loop:hold): see the hold comment on the PR"


def test_a_ready_pr_carrying_needs_owner_is_no_candidate_for_land_and_check_merge_says_why(
    lands: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    lands["open"](7, {"7.txt": "7\n"}, labels=("needs-owner",))
    lands["open"](8, {"8.txt": "8\n"})
    # the batch: another PR's land takes #8 and leaves the held #7
    code, lines = lands["land"]("8")
    assert code == 0, lines
    assert HELD in lines
    assert lands["merged"]() == ["8"]
    # `--only` on the held PR itself lands nothing
    code, lines = lands["land"]("7", "--only")
    assert code == 1
    assert HELD in lines
    assert lands["merged"]() == ["8"]
    # check merge names why
    assert approvals.main(["check", "merge", "7"]) == 1
    assert HELD in capsys.readouterr().out.splitlines()


def test_the_owners_go_removes_needs_owner_from_the_pr_and_land_takes_it_again(
    lands: dict[str, Any],
    added: list[tuple[int, str, bool]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = lands["open"](7, {"7.txt": "7\n"}, labels=("needs-owner",))
    pr = lands["pulls"][7]

    def label(number: int, name: str, add: bool) -> None:
        added.append((number, name, add))
        held = lands["pulls"][number]["labels"]["nodes"]
        held[:] = [node for node in held if node["name"] != name] + (
            [{"name": name}] if add else []
        )

    monkeypatch.setattr(approvals, "label", label)
    assert approvals.main(["approve", "merge", "7", "--head", head, "--by", "owner"]) == 0
    assert (7, "needs-owner", False) in added
    assert "needs-owner" not in approvals.names(pr)
    code, lines = lands["land"]("7", "--only")
    assert code == 0, lines
    assert HELD not in lines
    assert lands["merged"]() == ["7"]


def test_the_delegates_go_leaves_needs_owner_on_the_pr(
    added: list[tuple[int, str, bool]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Into a feature branch, where the delegate's go is a go: the hold is still the owner's to lift."""
    pr = pull(labels=("needs-owner",))
    pr["baseRefName"] = "feature-base"
    monkeypatch.setattr(approvals, "pull_request", lambda _n: pr)
    monkeypatch.setattr(approvals, "comment", lambda _n, _body: None)
    assert approvals.main(["approve", "merge", "7", "--head", HEAD, "--by", "delegate"]) == 0
    assert (7, "needs-owner", False) not in added


def test_needs_owner_on_the_pr_holds_its_merge_never_its_build(lands: dict[str, Any]) -> None:
    lands["open"](7, {"7.txt": "7\n"}, labels=("needs-owner",))
    pr = lands["pulls"][7]
    tracker, loop = approvals.project()
    assert HELD in approvals.gated("merge", pr, tracker, loop)[0]
    assert HELD not in approvals.gated("build", pr, tracker, loop)[0]


def test_a_ready_pr_carrying_loop_hold_is_no_candidate_for_land_and_removing_it_lets_it_land(
    lands: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    lands["open"](7, {"7.txt": "7\n"}, labels=("loop:hold",))
    lands["open"](8, {"8.txt": "8\n"})
    # a bare land: the batch takes #8, skips the held #7 and writes nothing to it
    code, lines = lands["land"]()
    assert code == 0, lines
    assert HOLD in lines
    assert lands["merged"]() == ["8"]
    assert [node["name"] for node in lands["pulls"][7]["labels"]["nodes"]] == ["loop:hold"]
    assert not lands["pulls"][7]["comments"]["nodes"]
    assert all("7" not in args for args in lands["calls"])
    # `--only` on the held PR itself lands nothing and names the label
    code, lines = lands["land"]("7", "--only")
    assert code == 1
    assert HOLD in lines
    assert lands["merged"]() == ["8"]
    assert approvals.main(["check", "merge", "7"]) == 1
    assert HOLD in capsys.readouterr().out.splitlines()
    # removing the label lets it land
    nodes = lands["pulls"][7]["labels"]["nodes"]
    nodes[:] = [node for node in nodes if node["name"] != "loop:hold"]
    code, lines = lands["land"]("7", "--only")
    assert code == 0, lines
    assert HOLD not in lines
    assert lands["merged"]() == ["8", "7"]


def test_loop_hold_on_the_pr_holds_its_merge_never_its_build(lands: dict[str, Any]) -> None:
    lands["open"](7, {"7.txt": "7\n"}, labels=("loop:hold",))
    pr = lands["pulls"][7]
    tracker, loop = approvals.project()
    assert HOLD in approvals.gated("merge", pr, tracker, loop)[0]
    assert HOLD not in approvals.gated("build", pr, tracker, loop)[0]
