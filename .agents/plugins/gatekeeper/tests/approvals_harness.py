"""The shared harness of the approvals.py tests, for a test module beside test_approvals.py.

A new module, `test_approvals_<topic>.py`, imports what it uses from here by name. It imports none
of the fixtures in AUTOUSE: conftest.py turns each one on for every module whose name starts with
`test_approvals`, as for test_approvals.py, so a fixture added here reaches every module, the ones
main gained since, without an edit there (#1015). They are plain fixtures, not autouse ones, since
conftest.py imports them and an autouse fixture there would stub approvals.py for every module.
The other names live in test_approvals.py, which this re-exports. Nothing here starts with
`test_`, so pytest collects no test twice.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from approvals_repos import build_template, committed, git_in
from test_approvals import (
    AS_WRITTEN,
    AUTO,
    BARE,
    BASE,
    GITHUB_RULE,
    HEAD,
    HELD,
    LABELED,
    LOOP,
    POLICY,
    PROJECT,
    PUSHED,
    SCRIPTS,
    TEXTED,
    TRACKER,
    TREE,
    VERDICT,
    VERIFIED,
    WRITTEN,
    Cloud,
    add,
    approvals,
    approved,
    authored,
    board,
    by,
    checkout,
    cloud,
    gatekeeper_md,
    gating,
    github,
    heavy_by_run,
    held,
    issue,
    judged_pr,
    judging,
    labelled,
    lands,
    loop_md,
    merge,
    node,
    note,
    pull,
    record,
    rest_comment,
    specced,
    status,
    verdict_on,
    writers,
)

# The fixtures every test of approvals.py runs under, defined below; conftest.py turns each on.
AUTOUSE = (
    "repo_template",
    "attached",
    "pushed",
    "others_open",
    "hub",
    "refs",
    "required",
    "added",
    "assigned",
    "board_risks",
    "graphql_answers",
    "merging",
)


@pytest.fixture(scope="session")
def repo_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The one committed repo `committed` copies, built once per session; under xdist each worker's tmp_path_factory
    is its own, so no two workers share a copy, and a test changes only its own."""
    return build_template(tmp_path_factory.mktemp("repo-template") / "repo")


@pytest.fixture
def attached(monkeypatch: pytest.MonkeyPatch) -> None:
    """local-ci runs in the test's process, as in a session of its own; the detached tests run the script."""
    monkeypatch.setattr(approvals, "leads_its_session", lambda: True)


@pytest.fixture
def pushed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The head was pushed at PUSHED, before the PR's labels were added (LABELED), and the directory a test runs in
    is the checkout's root; a test says otherwise."""
    monkeypatch.setattr(approvals, "head_pushed", lambda _pull: PUSHED)
    monkeypatch.setattr(approvals, "toplevel", Path.cwd)
    monkeypatch.setattr(approvals, "viewer", lambda: "loop")
    monkeypatch.setattr(approvals, "merged_high", lambda _branch: [])
    # the checkout under test is not the one judged against origin/<base>: test_approvals_base_judges.py covers it
    monkeypatch.setattr(approvals, "judge_with_base", lambda _base, fetch=False, rules=True: None)
    monkeypatch.setattr(approvals, "base_policy", lambda _base, checkout: checkout)


@pytest.fixture
def others_open(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The repo's open PRs `check build` compares a PR with: none, unless a test adds one."""
    found: list[dict[str, Any]] = []
    monkeypatch.setattr(approvals, "open_pulls", lambda: found, raising=False)
    return found


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    """GitHub's commit statuses by commit, newest first: what statuses() reads and post_status() writes as `loop`."""
    found: dict[str, list[dict[str, Any]]] = {}

    def post(head: str, context: str, description: str, state: str = "success") -> None:
        found.setdefault(head, []).insert(0, status(context, description, state=state))

    monkeypatch.setattr(approvals, "statuses", lambda head: found.get(head, []))
    monkeypatch.setattr(approvals, "post_status", post)
    return found


@pytest.fixture
def refs(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    """origin's record branches as publish() pushes a pass to them, by name, and record_ref() reads them: a record
    counts for the tree, command and env it was pushed with. The ref tests put the real functions back on real
    repos."""
    pushed: dict[str, dict[str, Any]] = {}

    def push(record: dict[str, Any]) -> str:
        name = approvals.record_name(record["tree"], record["command"])
        pushed[name] = {**record, "env": record.get("env", approvals.gate()[2])}
        return approvals.RECORD_BRANCH.format(name)

    def read(
        held: str, command: str, env: dict[str, str], _why: list[str] | None = None
    ) -> dict[str, Any] | None:
        found = pushed.get(approvals.record_name(held, command))
        if found is None or (found["tree"], found["command"], found["env"]) != (held, command, env):
            return None
        ref = approvals.RECORD_BRANCH.format(approvals.record_name(held, command))
        return {**found, "exit": 0, "published": True, "ref": ref}

    monkeypatch.setattr(approvals, "record_ref", read)
    monkeypatch.setattr(approvals, "push_record", push)
    monkeypatch.setattr(approvals, "drop_records", lambda _heads: None)
    return pushed


@pytest.fixture
def required(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The base branch requires no checks, instead of reading its rules; the list holds each base asked about."""
    asked: list[str] = []
    monkeypatch.setattr(approvals, "required_checks", lambda base: asked.append(base) or set())
    return asked


@pytest.fixture
def added(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str, bool]]:
    """Every label approvals.py adds or removes, instead of making the call; `github` records them with the rest."""
    calls: list[tuple[int, str, bool]] = []
    monkeypatch.setattr(
        approvals, "label", lambda number, name, add: calls.append((number, name, add))
    )
    return calls


@pytest.fixture
def merging(monkeypatch: pytest.MonkeyPatch) -> None:
    """The adapter's merge finds every required check green on the head, instead of reading them: its gh stub has no route
    for them. test_approvals_no_bypass.py puts the real read back."""
    monkeypatch.setattr(approvals.github, "red_checks", lambda _number, _head: [])


@pytest.fixture
def board_risks(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The repo's board as board.py reads and sets it over gh, instead of GitHub: a `Risk` field whose option for
    each PR on the board is in `values`, every option set in `set` (which leaves `values`, so a test that checks
    a PR twice reads the board it set up), no linked board once `linked` is False, and `refused` the stderr each gh
    call fails with."""
    state: dict[str, Any] = {"values": {}, "set": [], "linked": True, "refused": None}
    items: dict[str, int] = {}

    def graphql(query: str, **values: Any) -> Any:
        if state["refused"]:
            raise subprocess.CalledProcessError(1, "gh", stderr=state["refused"])
        if query == board.LOOKUP:
            number, said = values["issue"], state["values"].get(values["issue"])
            on = (
                []
                if said is None
                else [{"project": {"id": "P"}, "fieldValueByName": {"name": said}}]
            )
            field = {
                "id": "F",
                "options": [{"id": f"o-{o}", "name": o} for o in ("Low", "Medium", "High")],
            }
            boards = [{"id": "P", "field": field}] if state["linked"] else []
            return {
                "repository": {
                    "issueOrPullRequest": {"id": f"N{number}", "projectItems": {"nodes": on}},
                    "projectsV2": {"nodes": boards},
                }
            }
        if query == board.ADD:
            items[f"I-{values['issue']}"] = int(values["issue"][1:])
            return {"addProjectV2ItemById": {"item": {"id": f"I-{values['issue']}"}}}
        assert query == board.SET, query
        state["set"].append((items[values["item"]], values["option"].removeprefix("o-")))
        return {}

    monkeypatch.setattr(board, "_graphql", graphql)
    monkeypatch.setattr(approvals, "board", lambda: board)
    return state


@pytest.fixture
def assigned(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Every assignee approvals.py adds or removes, and the record claim() comments, instead of making the call."""
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        approvals, "assign", lambda number, login, add: calls.append((number, login, add))
    )
    real = approvals.comment

    def comment(number: int, body: str) -> None:
        if body.startswith(approvals.ASSIGNED):
            calls.append((number, approvals.machine(body)))
        else:
            real(number, body)

    monkeypatch.setattr(approvals, "comment", comment)
    return calls


@pytest.fixture
def graphql_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts as a run that has not seen GraphQL refused."""
    monkeypatch.setattr(approvals.github, "_rest_only", False)


__all__ = [
    "AS_WRITTEN",
    "AUTO",
    "AUTOUSE",
    "BARE",
    "BASE",
    "GITHUB_RULE",
    "HEAD",
    "HELD",
    "LABELED",
    "LOOP",
    "POLICY",
    "PROJECT",
    "PUSHED",
    "SCRIPTS",
    "TEXTED",
    "TRACKER",
    "TREE",
    "VERDICT",
    "VERIFIED",
    "WRITTEN",
    "Cloud",
    "add",
    "added",
    "approvals",
    "approved",
    "assigned",
    "attached",
    "authored",
    "board",
    "board_risks",
    "by",
    "checkout",
    "cloud",
    "committed",
    "gatekeeper_md",
    "gating",
    "git_in",
    "github",
    "graphql_answers",
    "heavy_by_run",
    "held",
    "hub",
    "issue",
    "judged_pr",
    "judging",
    "labelled",
    "lands",
    "loop_md",
    "merge",
    "merging",
    "node",
    "note",
    "others_open",
    "pull",
    "pushed",
    "record",
    "refs",
    "repo_template",
    "required",
    "rest_comment",
    "specced",
    "status",
    "verdict_on",
    "writers",
]
