"""approvals.py: a PR past the 100 files GraphQL gives reads its full list from REST, so the risk rule's `paths`
judge it instead of holding (#1051)."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from approvals_harness import approvals, loop_md, pull
from test_approvals import BOTS, LOOP, add, authored

RULE = loop_md("risk:\n  paths:\n    - billing/**\n")


def crowded(total: int, *, risky: bool = False) -> dict[str, Any]:
    """A PR of `total` files whose GraphQL page holds the first 100; `risky` puts the last one under billing/."""
    big = pull(files=("docs/f0.md",))
    big["files"] = {
        "totalCount": total,
        "nodes": [{"path": f"docs/f{i}.md", "changeType": "MODIFIED"} for i in range(100)],
    }
    big["listed"] = [f"docs/f{i}.md" for i in range(total - risky)] + ["billing/x.py"] * risky
    return big


@pytest.fixture
def rest(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The REST list (pr_changes) of the PR in `state["pull"]`; `fail` makes GitHub refuse it, `calls` counts reads."""
    state: dict[str, Any] = {"pull": None, "fail": False, "calls": 0, "cut": None}

    def changes(_number: int) -> list[dict[str, Any]]:
        state["calls"] += 1
        if state["fail"]:
            raise subprocess.CalledProcessError(1, ["gh", "api"])
        listed = state["pull"]["listed"][: state["cut"]]
        return [{"filename": name, "status": "modified"} for name in listed]

    monkeypatch.setattr(approvals, "pr_changes", changes)
    return state


def judged(rest: dict[str, Any], big: dict[str, Any]) -> str | None:
    rest["pull"] = big
    return approvals.ruled(set(), approvals.touched(big), RULE)


def test_a_pr_of_101_files_off_the_risk_paths_passes(rest: dict[str, Any]) -> None:
    assert judged(rest, crowded(101)) is None


def test_a_101st_file_on_a_risk_path_still_matches(rest: dict[str, Any]) -> None:
    assert judged(rest, crowded(101, risky=True)) == "it matches a risk rule (path `billing/**`)"


def test_a_failed_rest_read_holds_the_rule(rest: dict[str, Any]) -> None:
    rest["fail"] = True
    assert approvals.paths(crowded(101)) is None
    assert "can't read every file" in (judged(rest, crowded(101)) or "")


def test_a_list_github_capped_holds_the_rule(rest: dict[str, Any]) -> None:
    rest["cut"] = 100
    assert "can't read every file" in (judged(rest, crowded(150)) or "")


def test_a_list_at_the_3000_file_cap_holds_the_rule(rest: dict[str, Any]) -> None:
    assert "can't read every file" in (judged(rest, crowded(3000)) or "")


def test_one_rest_read_serves_paths_and_the_old_names(
    rest: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    big = crowded(101)
    big["listed"][5] = "docs/new.md"
    rest["pull"] = big
    changes = [{"filename": name, "status": "modified"} for name in big["listed"]]
    changes[5] = {
        "filename": "docs/new.md",
        "status": "renamed",
        "previous_filename": "billing/old.py",
    }

    def counted(_n: int) -> list[dict[str, Any]]:
        rest["calls"] += 1
        return changes

    monkeypatch.setattr(approvals, "pr_changes", counted)
    found = approvals.touched(big)
    assert found is not None and "billing/old.py" in found and "docs/new.md" in found
    assert rest["calls"] == 1


RENOVATE = add(LOOP, "bot", 'renovate:\n  branch: renovate/\n  paths:\n    - "**/package.json"\n')


def lockfiles(
    status: str | None, *, branch: bool = False
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A bot PR of 150 package.json files, GraphQL's page the first 100; `status` is the REST status of file 120."""
    names = [f"p{i}/package.json" for i in range(150)]
    if branch:
        bot = {"__typename": "User", "login": "writer"}
        pr = {**pull(files=("uv.lock",)), "author": bot, "headRefName": "renovate/foo"}
        pr["authored"] = {"nodes": [authored("writer")]}
    else:
        bot = {"__typename": "Bot", "login": "dependabot"}
        pr = {**pull(files=("uv.lock",)), "author": bot}
        pr["authored"] = {"nodes": [authored("dependabot[bot]")]}
    pr["files"] = {
        "totalCount": 150,
        "nodes": [{"path": n, "changeType": "MODIFIED"} for n in names[:100]],
    }
    changes = [{"filename": n, "status": "modified"} for n in names]
    if status:
        changes[120] = {"filename": names[120], "status": status, "previous_filename": "app.py"}
    return pr, changes


@pytest.mark.parametrize(
    ("status", "branch", "exempt"),
    [
        ("renamed", False, False),
        ("copied", False, False),
        ("renamed", True, False),
        (None, False, True),
        (None, True, True),
    ],
)
def test_a_move_past_node_100_refuses_the_bot_exemption(
    monkeypatch: pytest.MonkeyPatch, status: str | None, branch: bool, exempt: bool
) -> None:
    pr, changes = lockfiles(status, branch=branch)
    monkeypatch.setattr(approvals, "pr_changes", lambda _n: changes)
    assert approvals.exempt(pr, RENOVATE if branch else BOTS) is exempt
