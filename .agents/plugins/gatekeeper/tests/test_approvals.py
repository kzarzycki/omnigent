# New tests go in a module of their own, test_approvals_<topic>.py, importing approvals_harness.
"""scripts/approvals.py: each loop step leaves proof on GitHub; the gate checks it, and `approve`/`verdict` write it."""

from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import importlib.util
import itertools
import json
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from approvals_repos import committed, git_in

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "gatekeeper" / "scripts"
GITHUB_SCRIPTS = REPOSITORY / "github" / "skills" / "github-tracker" / "scripts"
LOOP_SCRIPTS = REPOSITORY / "loop" / "skills" / "engineering-loop" / "scripts"
SPEC = importlib.util.spec_from_file_location("gatekeeper", SCRIPTS / "gatekeeper.py")
assert SPEC and SPEC.loader
approvals = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(approvals)

# `brief` and `release` are the loop's own script's (loop.py), which reads the PR through the gatekeeper; these tests,
# written when they were the gatekeeper's verbs, run them through that script on this module, so every fake they
# set on `approvals` is what loop.py reads. A copy of loop.py's own tests is loop/tests/test_loop.py.
_LOOP_PATH = Path(__file__).resolve().parents[2] / "loop/skills/engineering-loop/scripts/loop.py"
_LOOP_SPEC = importlib.util.spec_from_file_location("loop_under_test", _LOOP_PATH)
assert _LOOP_SPEC and _LOOP_SPEC.loader
loop_script = importlib.util.module_from_spec(_LOOP_SPEC)
_LOOP_SPEC.loader.exec_module(loop_script)
loop_script.gatekeeper = approvals
_gatekeeper_main = approvals.main


def _main(argv: list[str]) -> int:
    """The gatekeeper's main, or loop.py's for the two verbs it owns."""
    return (
        loop_script.main(argv) if argv[:1] in (["brief"], ["release"]) else _gatekeeper_main(argv)
    )


approvals.main = _main
approvals.brief, approvals.release = loop_script.brief, loop_script.release


def heavy_by_run(words: Any, env: Any = None, **popen: Any) -> tuple[int, Any]:
    """approvals.run_heavy through `approvals.subprocess.run`, so a test that fakes that run fakes a gate step too."""
    done = approvals.subprocess.run(list(words), check=False, env=env, **popen)
    return done.returncode, getattr(done, "stdout", None)


# board.py reads names from approvals.py beside it, as when run
sys.path.insert(0, str(SCRIPTS))
BOARD_SPEC = importlib.util.spec_from_file_location("board", GITHUB_SCRIPTS / "board.py")
assert BOARD_SPEC and BOARD_SPEC.loader
board = importlib.util.module_from_spec(BOARD_SPEC)
BOARD_SPEC.loader.exec_module(board)

PROJECT = Path(__file__).resolve().parent / "fixtures" / "loop-project"
TRACKER = (PROJECT / "docs" / "agents" / "issue-tracker.md").read_text()
LOOP = (PROJECT / "docs" / "agents" / "loop.md").read_text()


def in_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run in the fixture project, whose policy files are its own: the checkout's `origin/<base>` holds the fixture
    only where this PR is merged, so the policy is read from the working tree, as WORKING_TREE_POLICY says."""
    monkeypatch.chdir(PROJECT)
    monkeypatch.setenv("GATEKEEPER_POLICY_FROM_WORKING_TREE", "1")


def loop_md(settings: str = "") -> str:
    """A loop.md whose settings block holds `settings`, YAML."""
    return f"# Loop facts\n\n## Settings\n\n```yaml\n{settings}```\n\nThe prose for people.\n"


CAP = approvals.cap(None)  # SKILL.md § The cap: the default these tests pin
BARE = loop_md()  # a loop.md with no settings, so no rules


def add(loop: str, section: str, settings: str) -> str:
    """`loop` with `settings`, keys written flush left, added to its `section`, made at the block's end where it has
    none."""
    header, settings = f"\n{section}:\n", textwrap.indent(settings, "  ")
    if header in loop:
        return loop.replace(header, header + settings, 1)
    end = loop.index("```", loop.index("```yaml\n") + len("```yaml\n"))
    return loop[:end] + header[1:] + settings + loop[end:]


HEAD, BASE = "a" * 40, "b" * 40
TREE = "e" * 40  # the head's tree in a faked checkout
WRITTEN = "2026-10-01T09:00:00Z"
AS_WRITTEN = "as written 2026-10-01 09:00:00 UTC"
# what `approve` records for the spec `issue()` holds: its text's digest
TEXTED = "sha256 " + hashlib.sha256(b"the spec").hexdigest()
IDS = itertools.count(1)
PUSHED, LABELED = "2026-10-01T10:00:00Z", "2026-10-01T11:00:00Z"
# land's wait for a check whose only run is the draft's skip
WAITED = (
    "PR #7: no `check` on the head commit yet since the PR went ready"
    " (its workflow may lack the `ready_for_review` type)"
)
TOPLEVEL = approvals.toplevel
VIEWER = approvals.viewer
REQUIRED = approvals.required_checks
MERGED_HIGH = approvals.merged_high
COMMENT = approvals.comment
OPEN_PULLS = getattr(approvals, "open_pulls", list)


RECORD_REF = approvals.record_ref
PUSH_RECORD = approvals.push_record
DROP_RECORDS = approvals.drop_records


def pushed_pass(
    refs: dict[str, dict[str, Any]],
    head: str,
    tree: str,
    command: str,
    env: dict[str, str] | None = None,
) -> None:
    """A pass of `command` on `tree`, run on `head`, as another clone pushed it to origin's record branch."""
    record = {"head": head, "tree": tree, "command": command, "time": "2026-01-01T00:00:00Z"}
    refs[approvals.record_name(tree, command)] = record | {"env": env or {}}


def status(
    context: str, description: str, by: str = "loop", state: str = "success"
) -> dict[str, Any]:
    return {
        "context": context,
        "state": state,
        "description": description,
        "creator": {"login": by},
        "created_at": "2026-01-01T00:00:00Z",
    }


def problems(issues: list[dict[str, Any]]) -> list[str]:
    return approvals.problems(issues, approvals.label_names(TRACKER))


def note(body: str, **fields: Any) -> dict[str, Any]:
    """A comment as GitHub returns it, written by the account running approvals.py unless a test says otherwise."""
    return {
        "id": f"IC_{next(IDS)}",
        "author": {"login": "loop"},
        "body": body,
        "createdAt": WRITTEN,
        "lastEditedAt": None,
        "isMinimized": False,
        "viewerCanMinimize": True,
        **fields,
    }


def node(
    labels: tuple[str, ...] = (), comments: tuple[str | dict[str, Any], ...] = (), **fields: Any
) -> dict[str, Any]:
    return {
        "labels": {"nodes": [{"name": name} for name in labels]},
        "comments": {
            "nodes": [body if isinstance(body, dict) else note(body) for body in comments]
        },
        **fields,
    }


def issue(
    number: int, *labels: str, body: str = "the spec", comments: tuple[str, ...] = ()
) -> dict[str, Any]:
    return node(
        labels,
        comments,
        number=number,
        body=body,
        createdAt=WRITTEN,
        lastEditedAt=None,
        repository={"nameWithOwner": "kzarzycki/scratch-gate-revert"},
    )


def specced(number: int, *labels: str) -> dict[str, Any]:
    return issue(number, "area:web", "size:S", *labels)


def record(point: str, by: str, **fields: str) -> str:
    return "\n".join(
        [
            f"Approved: {point}",
            f"By: {by}",
            *(f"{key.capitalize()}: {value}" for key, value in fields.items()),
        ]
    )


def approved(number: int, *labels: str, body: str = "the spec") -> dict[str, Any]:
    """A specced issue whose spec the owner approved."""
    comments = (record("spec", "owner", spec=AS_WRITTEN),)
    return issue(number, "size:S", "approved:spec", *labels, body=body, comments=comments)


def pull(
    *,
    body: str = "## Evidence\n\nlog line\n",
    files: tuple[str, ...] = (),
    comments: tuple[str | dict[str, Any], ...] = (),
    labels: tuple[str, ...] = (),
    checks: tuple[tuple[str, str], ...] = (("check", "SUCCESS"),),
    labeled: str = LABELED,
    reviews: tuple[dict[str, Any], ...] = (),
    latest: tuple[str, ...] = (),
    threads: tuple[bool, ...] = (),
) -> dict[str, Any]:
    contexts = [
        {"__typename": "CheckRun", "name": name, "status": "COMPLETED", "conclusion": state}
        for name, state in checks
    ]
    return node(
        labels,
        comments,
        number=7,
        title="Feature",
        isCrossRepository=False,
        author={"login": "worker"},
        state="OPEN",
        isDraft=False,
        mergeStateStatus="BLOCKED",
        autoMergeRequest=None,
        isInMergeQueue=False,
        body=body,
        baseRefName="main",
        baseRefOid=BASE,
        headRefName="feature",
        headRefOid=HEAD,
        baseRepository={
            "nameWithOwner": "kzarzycki/scratch-gate-revert",
            "defaultBranchRef": {"name": "main"},
        },
        files={"nodes": [{"path": path} for path in files]},
        latestReviews={
            "nodes": [{"state": state, "author": {"login": "reviewer"}} for state in latest]
        },
        reviews={"nodes": list(reviews)},
        reviewThreads={"nodes": [{"isResolved": resolved} for resolved in threads]},
        commits={"nodes": [{"commit": {"statusCheckRollup": {"contexts": {"nodes": contexts}}}}]},
        closingIssuesReferences={"nodes": []},
        timelineItems={
            "nodes": [{"createdAt": labeled, "label": {"name": name}} for name in labels]
        },
    )


def verdict_on(
    commit: str,
    counts: str = "0 blocker, 0 major, 1 minor",
    satisfied: str = "yes",
    author: str | None = "loop",
    heading: str = "Verifier (claude), pass 1",
) -> dict[str, Any]:
    """A verifier pass's PR review as GitHub's GraphQL returns it, by the account running approvals.py."""
    return {
        "author": {"login": author} if author else None,
        "state": "COMMENTED",
        "commit": {"oid": commit},
        "body": f"{heading} on {commit[:7]}.\n\nVERDICT: {counts}\nSATISFIED: {satisfied}\n",
    }


VERDICT = verdict_on(HEAD, "0 blocker, 0 major, 2 minor")
OWNED = ("approved:merge",)
HELD = f"PR #7 waits for the owner's go on its head (`approve merge 7 --head {HEAD} --by owner`; into main `approved:merge` is no go; `loop.delegate: all` in the policy file lets a delegate's go count), since it is high risk: "


def given_go(pr: dict[str, Any], hub: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """`pr` with the owner's go on its head as `approve merge` leaves it: the record on the PR, the status in `hub`."""
    head = pr["headRefOid"]
    pr["comments"]["nodes"].append(note(record("merge", "owner", head=head)))
    hub.setdefault(head, []).insert(0, status("owner", f"The owner's go on {head[:12]}"))
    return pr


def into_feature(pr: dict[str, Any]) -> dict[str, Any]:
    """`pr` into a feature branch, where `approved:merge` added after the head's push is a go too."""
    pr["baseRefName"] = "stream"
    return pr


BILLING = HELD + "it matches a risk rule (path `billing/**`)"
GITHUB = HELD + "it matches a risk rule (path `.github/**`)"


def merge(pr: dict[str, Any], issues: list[dict[str, Any]], loop: str = LOOP) -> list[str]:
    """check merge's lines: what fails, then what it waits for."""
    return approvals.proofs("merge", pr, issues, loop) + approvals.waits(pr, issues, loop)


# --- labels and state ---


def test_the_label_rule_is_the_loops_fixed_set_plus_the_tracker_files_sections() -> None:
    assert approvals.label_names(TRACKER) == {
        "category": {
            "kind:bug",
            "kind:enhancement",
            "kind:chore",
            "kind:documentation",
            "kind:ops",
        },
        "component": {"area:agents", "area:api", "area:web"},
        "size": {"size:XS", "size:S", "size:M", "size:L", "size:XL"},
    }
    assert approvals.label_names(
        loop_md('components:\n  area:web: ""\ncategories:\n  kind:ops: ""\n')
    ) == {
        "category": approvals.CATEGORIES | {"kind:ops"},
        "component": {"area:web"},
        "size": approvals.SIZES,
    }


@pytest.mark.parametrize(
    ("settings", "refused"),
    [
        ('sizes:\n  XXL: ""\n', "issue-tracker.md has an unknown settings section `sizes`"),
        ("components: api\n", "issue-tracker.md has a setting `components` outside a section"),
        ("[components]\napi\n", "issue-tracker.md's settings block is not YAML"),
        (
            "components:\n  api: 1\n",
            "issue-tracker.md's `components.api` must be a string, not 1",
        ),
    ],
)
def test_a_tracker_block_off_its_schema_is_refused_naming_the_key(
    settings: str, refused: str
) -> None:
    with pytest.raises(approvals.Refused) as raised:
        approvals.label_names(loop_md(settings))
    assert str(raised.value).startswith(refused)
    with pytest.raises(approvals.Refused, match="issue-tracker.md has no ```yaml settings block"):
        approvals.label_names("## Components\n\n- `billing`: invoices.\n")


def test_a_pr_that_closes_no_issue_skipped_the_spec() -> None:
    assert problems([]) == [
        "the PR names no issue: it needs a `Closes #<issue>` line for an approved spec"
    ]


def test_a_parked_issue_is_named() -> None:
    found = problems(
        [
            specced(1, "kind:bug"),
            issue(3, "needs-owner", "kind:bug", "area:api", "size:M"),
            specced(4, "kind:bug", "needs-owner"),
        ]
    )
    assert found == [
        "#3 waits for the owner (needs-owner)",
        "#4 waits for the owner (needs-owner)",
    ]


def test_an_approved_spec_with_a_size_is_ready_without_ready_for_agent() -> None:
    ready = approved(1, "kind:bug", "area:web")
    assert problems([ready]) + approvals.proofs("build", pull(), [ready], LOOP) == []


def test_an_issue_without_approved_spec_is_not_ready() -> None:
    intent = issue(2, "kind:chore", "area:agents", "size:XS", "ready-for-agent")
    assert problems([intent]) + approvals.proofs("build", pull(), [intent], LOOP) == [
        "#2 has no `Approved: spec` record by the owner",
        "#2 lacks the `approved:spec` label",
    ]


def test_check_build_before_the_pr_judges_the_issue(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no PR yet, `check build --issue <n>` (or the issue the branch is named for) applies the PR's build proofs
    to the issue alone, so an unapproved spec is not built."""
    spec: dict[int, dict[str, Any]] = {
        2: issue(2, "kind:chore", "area:agents", "size:XS", "risk:high"),
        3: approved(3, "kind:chore", "area:agents"),
    }
    monkeypatch.setattr(approvals, "project", lambda: (TRACKER, LOOP))
    monkeypatch.setattr(approvals, "one_repo", lambda: None)
    monkeypatch.setattr(approvals, "issue_node", lambda n: spec[n])
    monkeypatch.setattr(approvals, "current_pr", lambda: None)
    branch, changed = ["loop/3-fix"], [""]

    def git(*args: str) -> str:
        if args[0] == "diff":
            assert args == ("diff", "--name-only", "--no-renames", "-z", "origin/main...HEAD")
            if changed[0] is None:
                raise subprocess.CalledProcessError(128, ["git", *args])
            return "".join(f"{path}\0" for path in changed[0].split())
        return ("origin/main" if args[-1] == "origin/HEAD" else branch[0]) + "\n"

    monkeypatch.setattr(approvals, "git", git)

    def build(*args: str) -> tuple[int, list[str]]:
        code = approvals.main(["check", "build", *args])
        return code, capsys.readouterr().out.splitlines()

    unapproved = [
        "#2 has no `Approved: spec` record by the owner",
        "#2 lacks the `approved:spec` label",
    ]
    assert build("--issue", "2") == (1, unapproved)
    assert build("--issue", "3") == (0, ["#3: every build proof holds; no PR yet"])
    assert build() == (0, ["#3: every build proof holds; no PR yet"])
    branch[0] = "loop/2-fix"
    assert build() == (1, unapproved)
    branch[0] = "spike"
    assert build()[0] == 1
    assert build("7", "--issue", "2") == (1, ["--issue goes with check build and no PR number"])

    # a positional number no PR has is the issue, printing what --issue prints; a PR's number is unchanged
    def pull_request(n: int) -> dict[str, Any]:
        if n != 5:
            raise subprocess.CalledProcessError(
                1, ["gh"], stderr=f"Could not resolve to a PullRequest with the number of {n}"
            )
        return {"number": 5, "labels": {"nodes": []}, "baseRefName": "main"}

    monkeypatch.setattr(approvals, "pull_request", pull_request)
    monkeypatch.setattr(approvals, "gated", lambda *a: (["gated"], []))
    assert build("2") == build("--issue", "2") == (1, unapproved)
    assert build("3") == build("--issue", "3") == (0, ["#3: every build proof holds; no PR yet"])
    assert build("5") == (1, ["gated"])

    # where GraphQL answers 403, the REST fallback says a number no PR has is "Not Found": still the issue's
    def gh(*args: str, data: str | None = None) -> str:
        if args[:2] == ("api", "graphql"):
            raise subprocess.CalledProcessError(1, ["gh", *args], stderr="gh: HTTP 403: Forbidden")
        assert args in {
            ("api", "repos/{owner}/{repo}/pulls/2"),
            ("api", "repos/{owner}/{repo}/pulls/3"),
        }
        raise subprocess.CalledProcessError(1, ["gh", *args], stderr="gh: Not Found (HTTP 404)")

    monkeypatch.setattr(approvals, "pull_request", pull_request_of_record)
    monkeypatch.setattr(approvals.github, "gh", gh)
    monkeypatch.setattr(approvals.github, "_rest_only", False)
    assert build("2") == build("--issue", "2") == (1, unapproved)
    assert build("3") == (0, ["#3: every build proof holds; no PR yet"])
    assert approvals.github._rest_only


pull_request_of_record = approvals.pull_request


@pytest.mark.parametrize(
    "missing", ["Could not resolve to a PullRequest with the number of 3", "Not Found"]
)
def test_check_merge_of_a_number_no_pr_has_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing: str,
) -> None:
    """Only `check build` reads a number no PR has as the issue: `check merge` fails on the lookup, since there is no
    PR to merge, though #3's spec is approved."""
    monkeypatch.setattr(approvals, "project", lambda: (TRACKER, LOOP))
    monkeypatch.setattr(approvals, "issue_node", lambda n: approved(n, "kind:chore", "area:agents"))
    monkeypatch.setattr(approvals, "unbuilt", lambda *a: pytest.fail("judged the issue"))

    def pull_request(n: int) -> dict[str, Any]:
        raise subprocess.CalledProcessError(1, ["gh"], stderr=f"gh: {missing}")

    monkeypatch.setattr(approvals, "pull_request", pull_request)
    assert approvals.main(["check", "merge", "3"]) == 1
    assert capsys.readouterr().out.splitlines() == [f"gh: {missing}"]


def test_every_closing_issue_specced_lands() -> None:
    assert (
        problems([specced(1, "kind:ops", "triaged-by-bot"), specced(2, "kind:bug", "area:api")])
        == []
    )


def test_a_specced_issue_without_exactly_one_category_is_named() -> None:
    found = problems([specced(1), specced(2, "kind:bug", "kind:chore")])
    categories = "kind:bug, kind:chore, kind:documentation, kind:enhancement, kind:ops"
    assert found == [
        f"#1 needs exactly one category label ({categories}), has 0",
        f"#2 needs exactly one category label ({categories}), has 2",
    ]


def test_a_wayfinder_ticket_needs_only_its_state() -> None:
    assert problems([issue(58, "wayfinder:task", "approved:spec")]) == []
    assert problems([issue(58, "wayfinder:task", "ready-for-agent")]) == [
        "#58 lacks `approved:spec`: spec it first"
    ]
    merged = pull(reviews=(VERDICT,), labels=OWNED)
    assert merge(merged, [issue(58, "wayfinder:task")]) == []


def test_a_specced_issue_without_a_component_or_exactly_one_size_is_named() -> None:
    found = problems([issue(1, "kind:bug"), specced(2, "kind:bug", "size:M")])
    sizes = "size:L, size:M, size:S, size:XL, size:XS"
    assert found == [
        f"#1 needs exactly one size label ({sizes}), has 0",
        "#1 needs a component label (area:agents, area:api, area:web)",
        f"#2 needs exactly one size label ({sizes}), has 2",
    ]


def test_a_tracker_file_without_components_says_so() -> None:
    assert (
        approvals.problems([specced(1, "kind:bug")], approvals.label_names(BARE))[-1]
        == "#1 needs a component label (docs/agents/issue-tracker.md lists none)"
    )


@pytest.mark.parametrize(
    ("body", "numbers"),
    [
        ("Closes #549", [549]),
        ("Closes #556.\n\n## Summary", [556]),
        ("fixes #1, Resolved: #2 and CLOSE #3; closes #1\tclosed #4", [1, 2, 3, 4]),
        ("An example: `Closes #5`, <!-- Closes #6 -->\n```\nFixes #7\n```\nCloses #8", [8]),
        ("## Fixes\n\n#12 and closes\n#13; closes #14x", []),
        (
            "- step:\n  ```\n  Fixes #7\n  ```\n> ```\n> Fixes #9\n> ```\n```\nx ``` y\nFixes #10\n```\nCloses #8",
            [8],
        ),
        ("Use ``` inline.\nCloses #15\n```\ncode\n```\nRun `a` then\nFixes:\t#16\n`b`", [15, 16]),
        ("Fix #7\nfixed #8\nresolve #9\nresolves #10", [7, 8, 9, 10]),
        ("Part of #11\npart of: #12; departs of #13, part #14", [11, 12]),
        ("Implements #20\nimplements: #21; reimplements #22, implement #23", [20, 21]),
        ("", []),
        ("See #12; follows #13. Discloses #14, prefix #15, closes owner/repo#16, closes#17", []),
    ],
)
def test_the_body_names_its_closing_issues(body: str, numbers: list[int]) -> None:
    assert approvals.named(body) == numbers


def test_each_link_keeps_its_kind_and_a_closing_keyword_wins() -> None:
    body = (
        "Implements #1\nPart of #2\nCloses #3\nPart of #4, implements #4\nImplements #5\nfixes #5"
    )
    assert approvals.links(body) == {
        1: approvals.IMPLEMENTS,
        2: approvals.PART,
        3: approvals.CLOSES,
        4: approvals.IMPLEMENTS,
        5: approvals.CLOSES,
    }


# --- approval rules ---


def test_loop_md_settings_are_read_from_its_yaml_block() -> None:
    assert approvals.settings(LOOP) == {
        "spec": {"auto": ["bug", "follow-up", "sub-issue"]},
        "practice": {"spec": "to-spec"},
        "risk": {
            "labels": ["size:L", "size:XL", "area:api"],
            "paths": ["billing/**", ".github/**"],
        },
    }
    assert approvals.settings(None) == approvals.settings(BARE) == {}
    commented = loop_md(
        "# a comment\nloop:\n  cap: 3  # passes\nrisk:\n  paths:\n    # a note\n    - a/**\n"
    )
    assert approvals.settings(commented) == {"cap": 3, "risk": {"paths": ["a/**"]}}
    bots = loop_md(
        "bot:\n  dependabot[bot]:\n    paths:\n      - uv.lock\n  renovate[bot]:\n    paths:\n      - a\n      - b\n"
    )
    assert approvals.settings(bots)["bot"] == [
        {"login": "dependabot[bot]", "paths": ["uv.lock"]},
        {"login": "renovate[bot]", "paths": ["a", "b"]},
    ]
    # only the first yaml block is the settings: a later one is an example in the prose
    later = loop_md("loop:\n  cap: 3\n") + "```yaml\nloop:\n  cap: 4\n```\n"
    assert approvals.settings(later) == {"cap": 3}


@pytest.mark.parametrize(
    ("settings", "refused"),
    [
        ("loop:\n  caps: 3\n", "loop.md has an unknown setting `loop.caps`"),
        ("risk:\n  path:\n    - a/**\n", "loop.md has an unknown setting `risk.path`"),
        ("plan:\n  paths:\n    - a/**\n", "loop.md has an unknown setting `plan.paths`"),
        (
            "practice:\n  reviews: code-review\n",
            "loop.md has an unknown setting `practice.reviews`",
        ),
        ("approvals:\n  spec: auto\n", "loop.md has an unknown settings section `approvals`"),
        ("spec: auto\n", "loop.md has a setting `spec` outside a section"),
        (
            "bot:\n  paths:\n    - uv.lock\n",
            "loop.md's `bot.paths` must be a section holding `paths`",
        ),
        (
            "loop:\n  cap: three\n",
            "loop.md's `loop.cap` must be a whole number, not 'three'",
        ),
        ("loop:\n  cap: true\n", "loop.md's `loop.cap` must be a whole number, not True"),
        ('loop:\n  ci: "false"\n', "loop.md's `loop.ci` must be true or false, not 'false'"),
        (
            "spec:\n  auto: auto unless risk\n",
            "loop.md's `spec.auto` must be true, false or a list of bug, follow-up, sub-issue, not 'auto unless risk'",
        ),
        (
            "spec:\n  auto:\n    - bugs\n",
            "loop.md's `spec.auto` must be true, false or a list of bug, follow-up, sub-issue, not ['bugs']",
        ),
        ("loop:\n  spec: auto\n", "loop.md has an unknown setting `loop.spec`"),
        (
            "risk:\n  labels: size:L, size:XL\n",
            "loop.md's `risk.labels` must be a list of strings, not 'size:L, size:XL'",
        ),
        (
            "risk:\n  labels:\n    - 1\n",
            "loop.md's `risk.labels` must be a list of strings, not [1]",
        ),
        ("bot:\n  dependabot[bot]:\n", "loop.md's `bot.dependabot[bot]` lacks `paths`"),
        ("land:\n  signed_squash_to: main\n", "loop.md's `land` lacks `allowed_signers`"),
        (
            "land:\n  signed_squash_to: main\n  allowed_signers: s\n  where: everywhere\n",
            "loop.md's `land.where` must be one of host, cloud, both, not 'everywhere'",
        ),
        (
            'land:\n  signed_squash_to: ""\n  allowed_signers: s\n',
            "loop.md's `land.signed_squash_to` is empty",
        ),
        (
            "loop:\n  head_proof: gate-ui\n",
            "loop.md's `loop.head_proof` must be a section holding `status`, `env`",
        ),
        ("loop:\n  head_proof:\n    status: gate-ui\n", "loop.md's `loop.head_proof` lacks `env`"),
        (
            "loop:\n  head_proof:\n    status: gate-ui\n    env: GATE_UI_TESTS\n",
            "loop.md's `loop.head_proof.env` must be a list of strings, not 'GATE_UI_TESTS'",
        ),
    ],
)
def test_settings_that_do_not_match_the_schema_are_refused_naming_the_key(
    settings: str, refused: str
) -> None:
    with pytest.raises(approvals.Refused) as raised:
        approvals.settings(loop_md(settings))
    assert str(raised.value) == refused


def test_a_loop_md_without_its_settings_block_or_with_one_that_is_not_yaml_is_refused() -> None:
    """A project still on the line form would otherwise lose its risk rules unnoticed."""
    with pytest.raises(approvals.Refused, match="loop.md has no ```yaml settings block"):
        approvals.settings("## Approvals\n\n- risk: path `.github/**`\n")
    for broken in ("loop:\n  cap: 3\n ci: true\n", "risk:\n  paths: [a/**\n", "loop:\n\tcap: 3\n"):
        with pytest.raises(approvals.Refused, match="loop.md's settings block is not YAML:"):
            approvals.settings(loop_md(broken))
    with pytest.raises(approvals.Refused, match="loop.md's settings block is not YAML sections"):
        approvals.settings(loop_md("- loop\n"))
    for twice, key in (
        ("loop:\n  cap: 3\n  cap: 4\n", "loop.cap"),
        ("risk: {}\nrisk: {}\n", "risk"),
    ):
        with pytest.raises(approvals.Refused, match=f"loop.md's `{key}` is set twice"):
            approvals.settings(loop_md(twice))


@pytest.mark.parametrize(
    ("settings", "refused"),
    [
        ("practice:\n  plan: no\n", "loop.md's `practice.plan` must be a string, not False"),
        ("practice:\n  plan: 1.10\n", "loop.md's `practice.plan` must be a string, not 1.1"),
        (
            "risk:\n  labels:\n    - on\n",
            "loop.md's `risk.labels` must be a list of strings, not [True]",
        ),
        (
            "loop:\n  local_ci_env:\n    - 1.10\n",
            "loop.md's `loop.local_ci_env` must be a list of strings",
        ),
        ("loop:\n  ci: off-ish\n", "loop.md's `loop.ci` must be true or false, not 'off-ish'"),
        ("bot:\n  yes:\n    paths: []\n", "loop.md's `bot.True` is no name: quote it"),
    ],
)
def test_what_yaml_1_1_reads_as_another_type_is_refused_naming_the_key(
    settings: str, refused: str
) -> None:
    """PyYAML follows YAML 1.1: `no`, `on` and `yes` are booleans and `1.10` the number 1.1, never the name written."""
    with pytest.raises(approvals.Refused) as raised:
        approvals.settings(loop_md(settings))
    assert str(raised.value).startswith(refused)
    quoted = settings.replace("no\n", '"no"\n').replace("1.10", '"1.10"').replace("on\n", '"on"\n')
    if quoted != settings:
        approvals.settings(loop_md(quoted))


def test_a_tracker_name_yaml_reads_as_a_boolean_is_refused_naming_it() -> None:
    with pytest.raises(
        approvals.Refused, match="issue-tracker.md's `components.True` is no name: quote it"
    ):
        approvals.label_names(loop_md("components:\n  on: the on-call rota.\n"))
    assert (
        "area:on"
        in approvals.label_names(loop_md('components:\n  "on": the on-call rota.\n'))["component"]
    )


def test_skill_mds_settings_examples_pass_their_schema() -> None:
    """policy.md shows the policy's block, then issue-tracker.md's: each is what the scripts read."""
    skill = (SCRIPTS.parent / "policy.md").read_text()
    loop, tracker = re.findall(r"^```yaml\n(.*?)^```$", skill, re.DOTALL | re.MULTILINE)[:2]
    assert approvals.settings(loop_md(loop))["bot"] == [
        {"login": "dependabot[bot]", "paths": ["uv.lock", "**/package.json"]},
        {"login": "renovate", "branch": "renovate/", "paths": ["uv.lock"]},
        {
            "login": "pack-bump",
            "branch": "claude/ready-pack-bump-",
            "paths": ["apm.yml", "apm.lock.yaml", ".agents/**", ".claude/**"],
        },
    ]
    assert approvals.tracker_settings(loop_md(tracker))["renames"] == {"frontend": "area:web"}
    excepted = approvals.settings(loop_md(loop))["carry_over_main"]["except"]
    # a glob matches the full path: a nested copy needs a glob of its own
    for path in ("uv.lock", "engineering/uv.lock"):
        assert any(fnmatch.fnmatch(path, glob) for glob in excepted), path


def test_a_script_run_without_pyyaml_says_in_one_line_how_to_run_it() -> None:
    """`-S` leaves out site-packages, where PyYAML is installed, as a bare `python3` without it would."""
    run = subprocess.run(
        [sys.executable, "-S", str(SCRIPTS / "approvals.py"), "check", "build"],
        capture_output=True,
        check=False,
        text=True,
    )
    assert run.returncode == 1
    assert run.stderr.splitlines() == [
        f"approvals.py needs PyYAML: run it with `uv run {SCRIPTS / 'approvals.py'}`, which installs it"
    ]


EXAMPLE = "## Example\n\n```yaml\nloop:\n  ci: false\n```\n\n"


def test_a_yaml_example_outside_settings_is_never_read_as_the_settings() -> None:
    """Only the block under `## Settings` is read: an example in the prose neither replaces nor stands in for it."""
    document = EXAMPLE + loop_md("loop:\n  ci: true\nrisk:\n  always: true\n")
    assert approvals.settings(document)["ci"] is True
    assert approvals.ruled(set(), [], document) == "it matches a risk rule (always)"
    with pytest.raises(approvals.Refused, match="loop.md has no ```yaml settings block"):
        approvals.settings("## Approvals\n\n- risk: always\n\n" + EXAMPLE)
    tracker = EXAMPLE + loop_md("components:\n  billing: invoices\n")
    assert approvals.label_names(tracker)["component"] == {"area:billing"}
    with pytest.raises(approvals.Refused, match="issue-tracker.md has no ```yaml settings block"):
        approvals.label_names("## Components\n\n- `billing`: invoices.\n\n" + EXAMPLE)


def gatekeeper_md(settings: str = "") -> str:
    """A gatekeeper.md whose policy block holds `settings`, YAML."""
    return f"# Gatekeeper\n\n## Policy\n\n```yaml\n{settings}```\n"


@pytest.fixture
def agents(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A checkout's docs/agents/ under tmp_path, empty, and no deprecation line of either kind printed yet this run,
    so a test's stderr doesn't depend on which tests ran before it."""
    monkeypatch.setattr(approvals, "toplevel", lambda: tmp_path)
    monkeypatch.setattr(approvals, "DEPRECATED", set())
    approvals.moved.cache_clear()
    (tmp_path / "docs" / "agents").mkdir(parents=True)
    return tmp_path / "docs" / "agents"


def test_the_policy_is_read_from_gatekeeper_md_under_its_policy_heading(
    agents: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """loop.md keeps its prose: a gatekeeper.md beside it is the one source of the bar, and its messages name it."""
    (agents / "loop.md").write_text("# Loop facts\n\nWorker model: mid\n")
    (agents / "gatekeeper.md").write_text(gatekeeper_md("loop:\n  gate: mise run check\n"))
    _, policy = approvals.project()
    assert approvals.settings(policy)["gate"] == "mise run check"
    assert capsys.readouterr().err == ""
    (agents / "gatekeeper.md").write_text(gatekeeper_md("loop:\n  ci: no-ish\n"))
    with pytest.raises(approvals.Refused, match="gatekeeper.md's `loop.ci` must be true or false"):
        approvals.project()
    (agents / "gatekeeper.md").write_text(loop_md("loop:\n  ci: false\n"))
    with pytest.raises(
        approvals.Refused, match="gatekeeper.md has no ```yaml settings block under ## Policy"
    ):
        approvals.project()


def test_loop_md_settings_without_gatekeeper_md_are_read_with_one_deprecation_line(
    agents: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (agents / "loop.md").write_text(loop_md("loop:\n  gate: mise run check\n"))
    for _ in range(2):
        assert approvals.settings(approvals.project()[1])["gate"] == "mise run check"
    assert capsys.readouterr().err == (
        "docs/agents/loop.md's ## Settings is deprecated: move the block to docs/agents/gatekeeper.md under"
        " ## Policy (the gatekeeper skill's policy.md)\n"
    )


def test_a_policy_in_both_gatekeeper_md_and_loop_md_is_refused_naming_both(agents: Path) -> None:
    """Two sources of the bar would drift."""
    (agents / "loop.md").write_text(loop_md("loop:\n  ci: false\n"))
    (agents / "gatekeeper.md").write_text(gatekeeper_md("loop:\n  ci: false\n"))
    with pytest.raises(approvals.Refused) as raised:
        approvals.project()
    assert str(raised.value) == (
        "both docs/agents/gatekeeper.md and docs/agents/loop.md's ## Settings hold a policy:"
        " keep it in docs/agents/gatekeeper.md alone"
    )


@pytest.mark.parametrize(
    ("terms", "labels", "paths", "hit"),
    [
        ({"always": True}, set(), [], "always"),
        ({"always": False}, set(), [], False),
        ({"labels": ["size:L", "size:XL"]}, {"size:XL"}, [], "label `size:XL`"),
        ({"labels": ["size:L"]}, {"size:XL"}, [], False),
        ({"labels": ["area:api"]}, {"api"}, [], False),
        ({"labels": ["kind:bug"]}, {"kind:bug"}, [], "label `kind:bug`"),
        ({"paths": ["billing/**"]}, set(), ["billing/a/invoice.py"], "path `billing/**`"),
        ({"paths": ["billing/**"]}, set(), ["web/app.ts"], False),
        ({"paths": ["billing/**"]}, set(), None, None),
        ({"labels": ["size:XL"], "paths": ["billing/**"]}, {"size:XL"}, None, "label `size:XL`"),
        ({"paths": ["docs/*.md"]}, set(), ["docs/a.md"], "path `docs/*.md`"),
        ({"paths": ["a dir/**"]}, set(), ["a dir/b, c.py"], "path `a dir/**`"),
        ({"verifier": True}, set(), [], False),
        ({}, {"size:XL"}, None, False),
    ],
)
def test_a_rule_holds_on_its_first_matching_term_or_is_left_to_the_loop(
    terms: dict[str, Any], labels: set[str], paths: list[str] | None, hit: str | bool | None
) -> None:
    assert approvals.matches(terms, labels, paths) == hit


def test_a_path_naming_the_verifier_is_no_verifier_term() -> None:
    """The line form searched the rule's text for `verifier`, so a path holding the word held every PR the verifier
    judged high; the gate now reads the `verifier` field alone."""
    loop = loop_md("risk:\n  paths:\n    - docs/verifier.md\n")
    assert (
        approvals.ruled(set(), ["app.py"], loop, "the verifier's pass 1 says `Risk: high`") is None
    )
    assert (
        approvals.ruled(set(), ["docs/verifier.md"], loop)
        == "it matches a risk rule (path `docs/verifier.md`)"
    )


# --- proof at build ---


def test_an_approved_spec_passes_build() -> None:
    assert approvals.proofs("build", pull(), [approved(1, "area:web")], LOOP) == []


def test_a_spec_nobody_approved_is_named() -> None:
    assert approvals.proofs("build", pull(), [specced(1, "kind:bug")], BARE) == [
        "#1 has no `Approved: spec` record by the owner",
        "#1 lacks the `approved:spec` label",
    ]


def test_a_spec_is_named_by_its_last_edit_as_github_shows_it() -> None:
    assert approvals.version({"createdAt": WRITTEN, "lastEditedAt": None}) == AS_WRITTEN
    edited = {"createdAt": WRITTEN, "lastEditedAt": "2026-10-02T14:35:27Z"}
    assert approvals.version(edited) == "as edited 2026-10-02 14:35:27 UTC"


def test_a_spec_edited_after_its_approval_needs_approving_again() -> None:
    edited = approved(1, "area:web")
    edited["lastEditedAt"] = "2026-10-02T14:35:27Z"
    assert approvals.proofs("build", pull(), [edited], LOOP) == [
        "#1 has no `Approved: spec` record by the owner for its current spec: the owner approves it again"
    ]


def test_only_the_owners_record_approves_a_spec_and_no_coordinators_is_asked_for() -> None:
    """`check` and `land` read the owner's record alone: an agent's record approves nothing."""
    agents = issue(
        1, "size:S", "approved:spec", comments=(record("spec", "coordinator", spec=AS_WRITTEN),)
    )
    assert approvals.proofs("build", pull(), [agents], BARE) == [
        "#1 has no `Approved: spec` record by the owner"
    ]
    assert approvals.proofs("build", pull(), [approved(1, "area:api")], BARE) == []


def test_a_spec_in_a_comment_is_the_one_approved() -> None:
    spec = "## Spec\n\nthe real spec"
    labels = ("ready-for-agent", "size:S", "area:web", "approved:spec")
    edited = "2026-10-02T11:00:00Z"
    comments = (
        note(spec, lastEditedAt=edited),
        record("spec", "owner", spec="as edited 2026-10-02 11:00:00 UTC"),
    )
    tool_owned = issue(1, *labels, body="a tool's body", comments=comments)
    assert approvals.proofs("build", pull(), [tool_owned], LOOP) == []


def test_a_leading_ai_marker_line_is_skipped_when_reading_a_heading() -> None:
    notes = [
        "> 🤖 AI\n\n## Spec\n\nthe marked spec",
        "> 🤖 AI\n\n## Specification notes\n\nnot a spec",
    ]
    found = approvals.headed(issue(1, comments=notes), "## Spec")
    assert found is not None and found["body"].endswith("the marked spec")
    assert approvals.headed(issue(1, comments=notes[1:]), "## Spec") is None


def test_a_heading_is_the_comments_whole_first_line() -> None:
    notes = (
        "## Spec\n\nthe old spec",
        "## Specification notes\n\nnot a spec",
        "  ## Spec  \nthe spec",
    )
    found = approvals.headed(issue(1, comments=notes), "## Spec")
    assert found is not None and found["body"] == "  ## Spec  \nthe spec"
    assert approvals.headed(issue(1, comments=notes[1:2]), "## Spec") is None
    assert approvals.headed(issue(1, comments=("## Plan\n1. a",)), "## Plan") is not None


def test_a_due_plan_needs_its_comment_and_approval() -> None:
    plans = loop_md("practice:\n  plan: writing-plans\n")
    assert approvals.proofs("build", pull(), [approved(1, "area:web")], plans) == [
        "#1 has no `## Plan` comment, and one is due"
    ]
    assert (
        approvals.proofs(
            "build", pull(), [approved(1, "area:web")], loop_md("plan:\n  always: true\n")
        )
        == []
    )
    planned = approved(1, "area:web")
    planned["comments"]["nodes"].append(note("## Plan\n\n1. slice"))
    assert (
        approvals.proofs("build", pull(), [planned], BARE) == []
    )  # no agent approves a plan either
    assert approvals.proofs("build", pull(), [planned], loop_md("plan:\n  always: true\n")) == [
        "#1 has no `Approved: plan` record by the owner",
        "#1 lacks the `approved:plan` label",
    ]


# --- proof at merge ---


def test_a_pr_with_every_proof_passes_merge() -> None:
    checks = (("check", "SUCCESS"), ("pr-board / sync", "FAILURE"))
    done = pull(reviews=(VERDICT,), labels=OWNED, checks=checks)
    assert merge(done, [approved(1, "area:web")]) == []


def test_a_demo_linked_instead_of_embedded_fails_check_merge_naming_demo_sh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(approvals, "writer", lambda login: login == "loop")
    embedded = (
        "## Demo\n\nthe preview, on aaaaaaa.\nhttps://github.com/user-attachments/assets/0b1c-2d\n"
    )
    asset = "https://github.com/kzarzycki/omnigent/releases/download/demo-190/check-1.mp4"
    linked = f"## Demo\n\n- [check 1]({asset})\n"
    issues = [approved(1, "area:web")]
    done = {"reviews": (VERDICT,), "labels": OWNED}
    assert merge(pull(comments=(embedded,), **done), issues) == []
    shot = "## Demo\n\nthe card, on aaaaaaa.\nhttps://github.com/user-attachments/assets/9f8e-7d\n"
    assert merge(pull(comments=(shot,), **done), issues) == []
    failed = merge(pull(comments=(embedded, linked), **done), issues)
    assert len(failed) == 1 and asset in failed[0] and "`scripts/demo.sh`" in failed[0]
    assert (
        "embeds no video or screenshot"
        in merge(pull(comments=("## Demo\n\nsee the PR",), **done), issues)[0]
    )
    # at build too, once a demo is posted; a stranger's demo comment holds nothing
    assert approvals.proofs("build", pull(comments=(linked,)), issues, LOOP) == failed
    stranger = note(linked, author={"login": "drive-by"})
    assert merge(pull(comments=(embedded, stranger), **done), issues) == []
    # nor does a stranger's later demo clear a linked one; a demo minimized as outdated is skipped
    cleared = note(embedded, author={"login": "drive-by"})
    assert merge(pull(comments=(linked, cleared), **done), issues) == failed
    outdated = note(linked, isMinimized=True)
    assert merge(pull(comments=(embedded, outdated), **done), issues) == []


def test_a_merge_waits_for_check_and_the_owners_go_and_fails_a_missing_proof(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    hub: dict[str, list[dict[str, Any]]],
) -> None:
    def gated(pr: dict[str, Any]) -> tuple[int, list[str]]:
        pr["closingIssuesReferences"]["nodes"] = [approved(1, "area:web", "kind:bug")]
        monkeypatch.setattr(approvals, "pull_request", lambda _n: pr)
        code = approvals.main(["check", "merge", "7"])
        return code, capsys.readouterr().out.splitlines()

    comments: list[tuple[int, str]] = []
    monkeypatch.setattr(approvals, "comment", lambda number, body: comments.append((number, body)))
    in_project(monkeypatch)
    monkeypatch.setattr(approvals, "required_checks", lambda _base: {"check"})
    running = pull(reviews=(VERDICT,), checks=(("check", "IN_PROGRESS"),), files=("billing/a.py",))
    assert gated(running) == (
        approvals.WAITING,
        ["PR #7: `check` is IN_PROGRESS on the head commit", BILLING],
    )
    labelled = pull(reviews=(VERDICT,), labels=OWNED, files=("billing/a.py",))
    assert gated(labelled) == (approvals.WAITING, [BILLING])  # into main the label is no go
    assert gated(given_go(labelled, hub)) == (0, [f"PR #7: the go on {HEAD} is the owner's"])
    assert gated(pull(labels=OWNED, checks=(("check", "IN_PROGRESS"),))) == (
        1,
        [
            "PR #7: no verifier review posted (approvals.py verdict)",
            "PR #7: `check` is IN_PROGRESS on the head commit",
        ],
    )


@pytest.mark.parametrize("loop", [LOOP, add(LOOP, "loop", "ci: false\n")], ids=["ci", "ci-false"])
def test_check_merge_passes_on_a_base_requiring_no_check_with_no_ci_and_a_satisfied_verdict(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    required: list[str],
    loop: str,
) -> None:
    """#349, #1872: a repo with no CI, whose base requires no check, has no status on the head at all: `check merge`
    waits for no aggregate `check` and no local-ci pass, so the verifier's satisfied record lets it pass (exit 0), as
    `land` merges it."""
    in_project(monkeypatch)
    monkeypatch.setattr(approvals, "project", lambda: (TRACKER, loop))
    monkeypatch.setattr(
        approvals, "reusable", lambda *_a, **_k: pytest.fail("read a local-ci pass")
    )
    pr = pull(reviews=(VERDICT,), checks=(), files=("app.py",))
    pr["closingIssuesReferences"]["nodes"] = [approved(1, "area:web", "kind:bug")]
    monkeypatch.setattr(approvals, "pull_request", lambda _n: pr)
    assert approvals.main(["check", "merge", "7"]) == 0
    assert not [line for line in capsys.readouterr().out.splitlines() if "check" in line]
    assert required == ["main"]


def test_a_label_added_before_the_heads_push_is_no_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    billing = ("billing/a.py",)
    stale = pull(reviews=(VERDICT,), labels=OWNED, labeled="2026-10-01T09:59:59Z", files=billing)
    assert merge(into_feature(stale), [approved(1, "area:web")]) == [
        "PR #7: `approved:merge` was added before the head was pushed: the owner adds it again"
    ]
    same_second = pull(reviews=(VERDICT,), labels=OWNED, labeled=PUSHED, files=billing)
    assert merge(into_feature(same_second), [approved(1, "area:web")]) == [
        "PR #7: `approved:merge` was added before the head was pushed: the owner adds it again"
    ]
    monkeypatch.setattr(approvals, "head_pushed", lambda _pull: None)
    assert merge(
        into_feature(pull(reviews=(VERDICT,), labels=OWNED, files=billing)),
        [approved(1, "area:web")],
    ) == [
        f"PR #7: GitHub lists no push of the head {HEAD}, so `approved:merge` can't be dated after it"
    ]


def test_a_label_added_again_after_the_push_is_the_approval() -> None:
    again = pull(reviews=(VERDICT,), labels=OWNED)
    again["timelineItems"]["nodes"] = [
        {"createdAt": "2026-10-01T09:00:00Z", "label": {"name": "approved:merge"}},
        {"createdAt": LABELED, "label": {"name": "approved:merge"}},
    ]
    assert merge(again, [approved(1, "area:web")]) == []


DELEGATED = note(record("merge", "delegate"), createdAt="2026-10-01T11:00:01Z")


def test_a_label_approves_a_high_risk_merge_into_a_feature_branch_only(
    hub: dict[str, list[dict[str, Any]]],
) -> None:
    """A delegate acting through the owner's account labels no differently from the owner, so into the default branch
    no label is the go, with a delegate record or without; only the owner's `approve merge` is."""
    high = ("risk:high", "approved:merge")
    held = [HELD + "`risk:high` is on PR #7"]
    delegated = pull(reviews=(VERDICT,), labels=high, comments=(DELEGATED,))
    assert merge(delegated, [approved(1, "area:web")]) == held
    by_hand = pull(reviews=(VERDICT,), labels=high)  # no delegate record
    assert merge(by_hand, [approved(1, "area:web")]) == held
    assert hub == {}  # and it never becomes a go on the head
    assert merge(into_feature(delegated), [approved(1, "area:web")]) == []
    assert merge(given_go(by_hand, hub), [approved(1, "area:web")]) == []
    feature = into_feature(pull(reviews=(VERDICT,), labels=("risk:high",)))
    assert merge(feature, [approved(1, "area:web")]) == [
        HELD.replace(
            "; into main `approved:merge` is no go; `loop.delegate: all` in the policy file lets a delegate's go count",
            ", or `approved:merge`",
        )
        + "`risk:high` is on PR #7"
    ]


def test_a_branchs_merge_to_the_default_branch_carries_its_high_risk_prs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(approvals, "merged_high", lambda branch: asked.append(branch) or [12, 15])
    assert merge(pull(reviews=(VERDICT,)), [approved(1, "area:web")]) == [
        HELD + "`risk:high` is on #12, merged into feature, #15, merged into feature"
    ]
    into_feature = pull(reviews=(VERDICT,))
    into_feature["baseRefName"] = "stream"
    assert merge(into_feature, [approved(1, "area:web")]) == []
    assert asked == ["feature"]
    monkeypatch.undo()
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(approvals.github, "gh", lambda *args: calls.append(args) or "12\n15\n")
    assert approvals.merged_high("feature") == [12, 15]
    assert calls == [
        (
            "pr",
            "list",
            "--base",
            "feature",
            "--state",
            "merged",
            "--label",
            "risk:high",
            "--limit",
            "100",
            "--json",
            "number",
            "--jq",
            ".[].number",
        )
    ]


def test_the_head_was_pushed_when_the_branchs_activity_last_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.undo()  # the autouse fixture's stub of head_pushed
    monkeypatch.setattr(approvals.github, "gh", lambda *args: calls.append(args) or f"{PUSHED}\n")
    assert approvals.head_pushed(pull()) == PUSHED
    assert calls == [
        (
            "api",
            "repos/{owner}/{repo}/activity?ref=refs/heads/feature&per_page=100",
            "--jq",
            f'[.[] | select(.after == "{HEAD}") | .timestamp] | first // ""',
        )
    ]
    monkeypatch.setattr(approvals.github, "gh", lambda *args: "\n")
    assert approvals.head_pushed(pull()) is None


def run(name: str, conclusion: str, started: str, workflow: str = "CI") -> dict[str, Any]:
    """A check run as the rollup returns it, with its start and its workflow."""
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": "COMPLETED",
        "conclusion": conclusion,
        "startedAt": started,
        "checkSuite": {"workflowRun": {"workflow": {"name": workflow}}},
    }


def with_runs(*runs: dict[str, Any]) -> dict[str, Any]:
    pr = pull()
    pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"] = list(runs)
    return pr


def test_only_the_aggregate_check_is_read() -> None:
    pr = with_runs(
        run("lint", "FAILURE", "2026-10-03T13:00:00Z"),
        {"__typename": "StatusContext", "context": "loop:approvals", "state": "PENDING"},
    )
    assert approvals.ci("PR #7", pr, {"check"}) == ["PR #7: no `check` on the head commit yet"]
    pr = with_runs(run("lint", "FAILURE", "2026-10-03T13:00:00Z"), run("check", "SUCCESS", ""))
    assert approvals.ci("PR #7", pr, {"check"}) == []


@pytest.mark.parametrize(
    ("base", "checks", "lines"),
    [
        ({"qualify"}, (("qualify", "SUCCESS"),), []),
        (
            {"qualify"},
            (("check", "SUCCESS"), ("qualify", "FAILURE")),
            ["PR #7: `qualify` is FAILURE on the head commit"],
        ),
        ({"qualify"}, (("check", "SUCCESS"),), ["PR #7: no `qualify` on the head commit yet"]),
        # a base that requires nothing waits for no check, as land reads it (#349, #1872): no aggregate `check`
        (set(), (("check", "SUCCESS"), ("lint", "FAILURE")), []),
        (set(), (("lint", "SUCCESS"),), []),
        (set(), (), []),
        # the shipped ruleset: `loop:approvals` is check merge's own output, never something it waits for
        ({"check", approvals.APPROVALS}, (("check", "SUCCESS"),), []),
        ({approvals.APPROVALS}, (("check", "SUCCESS"),), []),
        ({approvals.APPROVALS}, (("lint", "SUCCESS"),), []),
    ],
)
def test_check_merge_waits_for_the_checks_the_base_branch_requires(
    monkeypatch: pytest.MonkeyPatch,
    required: list[str],
    base: set[str],
    checks: tuple[tuple[str, str], ...],
    lines: list[str],
) -> None:
    """check merge reads the base's required checks as land does, once."""
    monkeypatch.setattr(approvals, "required_checks", lambda name: required.append(name) or base)
    pr = pull(reviews=(VERDICT,), checks=checks)
    assert approvals.waits(pr, [approved(1, "area:web")], LOOP) == lines
    assert required == ["main"]


def test_a_run_cancelled_by_a_newer_green_run_of_the_same_check_is_not_a_problem() -> None:
    pr = with_runs(
        run("check", "CANCELLED", "2026-10-03T13:44:42Z"),
        run("check", "SUCCESS", "2026-10-03T13:44:49Z"),
    )
    assert approvals.ci("PR #7", pr, {"check"}) == []


def test_the_newest_run_of_a_check_counts_when_it_failed() -> None:
    pr = with_runs(
        run("check", "SUCCESS", "2026-10-03T13:00:00Z"),
        run("check", "FAILURE", "2026-10-03T14:00:00Z"),
    )
    assert approvals.ci("PR #7", pr, {"check"}) == ["PR #7: `check` is FAILURE on the head commit"]


def test_one_job_name_in_two_workflows_is_two_checks() -> None:
    pr = with_runs(
        run("check", "SUCCESS", "2026-10-03T14:00:00Z", "CI"),
        run("check", "FAILURE", "2026-10-03T13:00:00Z", "Nightly"),
    )
    assert approvals.ci("PR #7", pr, {"check"}) == ["PR #7: `check` is FAILURE on the head commit"]


def test_a_high_risk_change_needs_a_default_tier_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """A satisfied lower-tier verdict saying `Risk: high` blocks the merge; a default-tier verdict on the same head
    clears it, and a heading naming no tier reads as the default."""
    monkeypatch.setattr(approvals, "writer", lambda _login: False)
    judged = add(loop_md("spec:\n  auto: true\n"), "risk", "verifier: true\n")

    def said(family: str, number: int, risk: str) -> dict[str, Any]:
        heading = f"Verifier ({family}), pass {number}"
        return verdict_on(HEAD, "0 blocker, 0 major, 0 minor", heading=heading) | {
            "body": f"{heading} on {HEAD[:7]}.\n\nVERDICT: 0 blocker, 0 major, 0 minor\nSATISFIED: yes\nRisk: {risk}\n"
        }

    def gate(*reviews: dict[str, Any]) -> list[str]:
        return approvals.proofs(
            "merge",
            pull(reviews=reviews, labels=OWNED),
            [approved(1, "kind:bug", "area:web")],
            judged,
        )

    assert gate(said("codex/terra-6.1/med mid", 1, "high — CI permissions")) == [
        (
            "PR #7 is high risk (it matches a risk rule's `verifier` term: the verifier's pass 1 on aaaaaaa says"
            " `Risk: high — CI permissions`), and its newest verdict came from a lower tier (mid): a default-tier"
            " verifier pass on the head first"
        )
    ]
    assert (
        gate(said("codex/terra-6.1/med mid", 1, "high"), said("codex/sol-6.1/med", 2, "high")) == []
    )
    assert gate(said("codex/terra-6.1/med mid", 1, "low — a fixture")) == []
    assert gate(said("codex", 1, "high")) == []
    # a tier word inside the model id is no tier: only a word of its own after the signature is
    assert gate(said("codex/sol-mid-preview/med", 1, "high")) == []
    assert gate(said("claude/small-model/?, same-family", 1, "high")) == []
    assert gate(said("claude, same-family, small", 1, "high"))


def test_without_ci_check_merge_passes_on_the_verifiers_record_with_no_local_ci_pass(
    monkeypatch: pytest.MonkeyPatch, no_origin: list[list[str]]
) -> None:
    """#1872, #349: under `ci: false`, on a base that requires no check, a satisfied verifier record is the merge's
    proof: no local-ci pass is read, and no aggregate `check` is waited for, whatever comments say."""
    no_ci = add(LOOP, "loop", "ci: false\n")
    monkeypatch.setattr(
        approvals, "reusable", lambda *_a, **_k: pytest.fail("read a local-ci pass")
    )
    monkeypatch.setattr(approvals, "writer", lambda login: False)

    def proofs(*comments: dict[str, Any]) -> list[str]:
        merged = pull(reviews=(VERDICT,), comments=comments, labels=OWNED, checks=())
        merged["isCrossRepository"] = True
        return merge(merged, [approved(1, "area:web")], no_ci)

    forged = note(f"{approvals.CHECKED}\nHead: {HEAD}\n", author={"login": "worker"})
    obsolete = note(
        f"{approvals.CHECKED}\nHead: {HEAD}\nCommand: obsolete-gate\nEnv: GATE_SCOPE=smoke\n",
        author={"login": "loop"},
    )
    assert proofs() == proofs(forged) == proofs(obsolete) == []
    # the same PR with no verdict still fails: the record is what the merge needs
    unverified = pull(labels=OWNED, checks=())
    assert merge(unverified, [approved(1, "area:web")], no_ci) == [
        "PR #7: no verifier review posted (approvals.py verdict)"
    ]


def test_more_checks_than_one_page_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(approvals, "required_checks", lambda _base: {"check"})
    big = pull(reviews=(VERDICT,), labels=OWNED, checks=(("lint", "SUCCESS"),))
    big["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["totalCount"] = 101
    assert merge(big, [approved(1, "area:web")]) == [
        "PR #7 has more than 1 CI checks: the gate reads one page",
    ]


def test_more_comments_than_one_page_is_refused() -> None:
    crowded = pull(comments=("a note", "another"))
    crowded["comments"]["totalCount"] = 101
    with pytest.raises(
        approvals.Refused, match="#7 has more than 2 comments: the gate reads one page"
    ):
        approvals.notes(crowded)


def test_a_pr_without_its_proof_names_each_missing_one() -> None:
    bare = pull(
        body="## Evidence\n\n## Summary\n", checks=(("check", "FAILURE"),), files=("billing/a.py",)
    )
    assert merge(bare, [approved(1, "area:web")]) == [
        "PR #7: its body has no `## Evidence` section with content",
        "PR #7: no verifier review posted (approvals.py verdict)",
        BILLING,
    ]
    # a base that requires no check waits for none (#349)
    assert not [line for line in merge(pull(checks=()), [], BARE) if "`check`" in line]


def test_a_risk_path_rule_holds_only_a_pr_touching_its_path(
    hub: dict[str, list[dict[str, Any]]],
) -> None:
    rule = loop_md("risk:\n  paths:\n    - billing/**\n")
    billing = pull(files=("billing/invoice.py",), reviews=(VERDICT,))
    assert merge(billing, [approved(1, "area:web")], rule) == [BILLING]
    assert (
        merge(pull(files=("web/app.ts",), reviews=(VERDICT,)), [approved(1, "area:web")], rule)
        == []
    )
    assert merge(given_go(billing, hub), [approved(1, "area:web")], rule) == []


# --- sign-off: the owner checks a landed change in use ---

SIGNOFF = add(LOOP, "signoff", "labels:\n  - area:web\npaths:\n  - app/**\n")
LANDED = pull(reviews=(VERDICT,), labels=OWNED)


def linking(keyword: str, number: int = 1, files: tuple[str, ...] = ()) -> dict[str, Any]:
    return pull(
        body=f"{keyword} #{number}\n\n## Evidence\n\nlog line\n",
        reviews=(VERDICT,),
        labels=OWNED,
        files=files,
    )


def test_the_signoff_rule_is_read_from_the_settings_and_is_no_risk_rule() -> None:
    assert approvals.settings(SIGNOFF)["signoff"] == {"labels": ["area:web"], "paths": ["app/**"]}
    assert approvals.settings(SIGNOFF)["risk"] == approvals.settings(LOOP)["risk"]


def test_without_a_signoff_line_any_link_lands_as_today() -> None:
    for keyword in ("Closes", "Implements", "Part of"):
        assert merge(linking(keyword), [approved(1, "area:web")]) == []
    owner = approved(1, "area:web", body="the spec\n\nSign-off: owner\n")
    assert merge(linking("Closes"), [owner]) == []


def test_an_issue_a_signoff_rule_matches_is_implemented_not_closed() -> None:
    web = approved(1, "area:web")
    assert merge(linking("Implements"), [web], SIGNOFF) == []
    assert merge(linking("Part of"), [web], SIGNOFF) == []
    assert merge(linking("Closes"), [web], SIGNOFF) == [
        (
            "#1: the PR says `Closes #1`, but #1 waits for the owner's sign-off after landing, since it"
            " matches a sign-off rule (label `area:web`): use `Implements #1`, which leaves it open"
        )
    ]
    # GitHub's own link, with no line in the body, closes the issue on merge
    assert merge(LANDED, [web], SIGNOFF)[0].startswith(
        "#1: GitHub links #1 to the PR as closing, with no line in its body, but #1 waits for the owner's sign-off"
    )
    # the PR's files and labels count, as for a risk rule
    api = approved(1, "area:api")
    assert approvals.linked(linking("Implements", files=("app/main.swift",)), [api], SIGNOFF) == []
    assert approvals.linked(linking("Implements", files=("api/a.py",)), [api], SIGNOFF) != []


def test_an_issue_no_signoff_rule_matches_is_closed() -> None:
    api = approved(1, "area:api")
    assert approvals.linked(linking("Closes"), [api], SIGNOFF) == []
    assert approvals.linked(linking("Part of"), [api], SIGNOFF) == []
    assert approvals.linked(linking("Implements"), [api], SIGNOFF) == [
        (
            "#1: the PR says `Implements #1`, but #1 needs no sign-off (no sign-off rule matches): use"
            " `Closes #1`, since `Implements` would leave it open for a sign-off nobody asked for"
        )
    ]


def test_the_spec_asks_for_a_signoff_and_never_waives_a_rule() -> None:
    owner = approved(1, "area:api", body="the spec\n\n- **Sign-off:** owner\n")
    assert approvals.linked(linking("Implements"), [owner], SIGNOFF) == []
    assert approvals.linked(linking("Closes"), [owner], SIGNOFF) == [
        (
            "#1: the PR says `Closes #1`, but #1 waits for the owner's sign-off after landing, since its"
            " spec says `Sign-off: owner`: use `Implements #1`, which leaves it open"
        )
    ]
    waived = approved(1, "area:web", body="the spec\n\nSign-off: none\n")
    assert approvals.linked(linking("Implements"), [waived], SIGNOFF) == []
    none = approved(1, "area:api", body="the spec\n\nSign-off: none\n")
    assert approvals.linked(linking("Implements"), [none], SIGNOFF) == [
        (
            "#1: the PR says `Implements #1`, but #1 needs no sign-off (no sign-off rule matches and its"
            " spec says `Sign-off: none`): use `Closes #1`, since `Implements` would leave it open for a"
            " sign-off nobody asked for"
        )
    ]
    quoted = approved(1, "area:api", body="the spec\n\n`Sign-off: owner` is an example\n")
    assert approvals.linked(linking("Closes"), [quoted], SIGNOFF) == []


def test_signoff_paths_past_the_files_the_gate_reads_ask_for_the_signoff() -> None:
    def crowded(keyword: str) -> dict[str, Any]:
        pr = linking(keyword, files=("README.md",))
        pr["files"]["totalCount"] = 101
        return pr

    assert approvals.linked(crowded("Implements"), [approved(1, "area:api")], SIGNOFF) == []
    assert approvals.linked(crowded("Closes"), [approved(1, "area:api")], SIGNOFF)[0].startswith(
        "#1: the PR says `Closes #1`, but #1 waits for the owner's sign-off after landing, since the gate"
        " can't read every file the PR changes, so the sign-off rule's `paths` hold"
    )


def test_signoff_always_asks_for_implements_and_a_rule_matching_nothing_asks_for_none() -> None:
    web = approved(1, "area:web")
    assert approvals.linked(linking("Closes"), [web], add(LOOP, "signoff", "always: true\n"))[
        0
    ].startswith("#1: the PR says `Closes #1`, but #1 waits for the owner's sign-off after landing")
    nothing = add(LOOP, "signoff", "labels:\n  - area:nothing\n")
    assert approvals.linked(linking("Closes"), [web], nothing) == []


def test_a_wayfinder_ticket_and_a_repo_without_loop_md_are_not_checked() -> None:
    ticket = issue(58, "wayfinder:task", "approved:spec", "area:web")
    assert approvals.linked(linking("Closes", 58), [ticket], SIGNOFF) == []
    assert approvals.proofs("merge", linking("Closes"), [approved(1, "area:web")], None) == []


# --- an exact revert ---

# GitHub's REST answers from a scratch round trip, trimmed to what the gate reads where noted: #1 merged (pull-1.json
# trimmed), #2 the PR GitHub's Revert button made for it after main moved, #3 that revert plus one line, still open;
# #2's and #3's merge base (trimmed) and the trees (trimmed to path, mode, SHA and type) of #1's base and merge
# commit and of the reverts' merge base and heads.
REVERTED = Path(__file__).resolve().parent / "fixtures" / "reverts"
REVERT_BASE, REVERT_HEAD = (
    "340b9aa38acac5a0d84e8c6d276a6ed2f3afdbe4",
    "47e28a794ff0884f8631342b8ed1aae4733e1ba6",
)
HEADS = {2: REVERT_HEAD, 3: "89cfd913d7696aafeb9cd6b4ac444aa131717e5b"}
REVERT_BODY = "Reverts kzarzycki/scratch-gate-revert#1\n\n## Evidence\n\n#1 set the wrong rate.\n"


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """gh answering REST calls from the recordings, a 404 for any other number; the paths asked for."""
    asked: list[str] = []

    def gh(*args: str, data: str | None = None) -> str:
        path = next(arg for arg in args if arg.startswith("repos/"))
        asked.append(path)
        if path.endswith("/comments") and "body=@-" in args:  # flag()'s reason
            return ""
        name = re.sub(r"^repos/\{owner\}/\{repo\}/pulls/(\d+)(/files)?.*", r"pull-\1\2", path)
        name = re.sub(r"^repos/\{owner\}/\{repo\}/git/trees/(\w+)\?recursive=1$", r"tree-\1", name)
        name = re.sub(r"^repos/\{owner\}/\{repo\}/(compare)/", r"\1-", name)
        found = REVERTED / f"{name.replace('/', '-')}.json"
        if not found.exists():
            raise subprocess.CalledProcessError(1, "gh", stderr="gh: Not Found (HTTP 404)\n")
        if "/files" in path:  # as `--jq '.[]'` prints them
            assert args[-2:] == ("--jq", ".[]")
            return "".join(json.dumps(entry) + "\n" for entry in json.loads(found.read_text()))
        if "/compare/" in path:  # as `--jq .merge_base_commit.sha` prints it
            assert args[-2:] == ("--jq", ".merge_base_commit.sha")
            return json.loads(found.read_text())["merge_base_commit"]["sha"] + "\n"
        return found.read_text()

    monkeypatch.setattr(approvals.github, "gh", gh)
    in_project(monkeypatch)
    return asked


def revert_pr(number: int = 2, body: str = REVERT_BODY, **fields: Any) -> dict[str, Any]:
    """The revert PR as GitHub's GraphQL returns it: the paths it touches are #1's."""
    return {
        **pull(body=body, files=("app.py", "billing/rate.py", "notes.py"), **fields),
        "number": number,
        "baseRefOid": REVERT_BASE,
        "headRefOid": HEADS.get(number, REVERT_HEAD),
    }


def gated(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    point: str,
    pr: dict[str, Any],
) -> tuple[int, list[str]]:
    monkeypatch.setattr(approvals, "pull_request", lambda _n: pr)
    code = approvals.main(["check", point, str(pr["number"])])
    return code, capsys.readouterr().out.splitlines()


def test_an_exact_revert_lands_without_a_spec_or_a_verdict(
    recorded: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    hub: dict[str, list[dict[str, Any]]],
) -> None:
    done = given_go(revert_pr(), hub)
    assert gated(monkeypatch, capsys, "build", done) == (0, [])
    assert gated(monkeypatch, capsys, "merge", done) == (
        0,
        [f"PR #2: the go on {REVERT_HEAD} is the owner's"],
    )
    # the inverse comes from GitHub's own diff of #1, not from anything approvals.py writes
    assert "repos/{owner}/{repo}/pulls/1/files?per_page=100" in recorded
    # the modes come from the trees of #1's base and merge commit and of the revert's merge base and head
    assert {
        "repos/{owner}/{repo}/git/trees/2a90fbed44fbe633bd17dff0af93d5ea1b5d3fe6?recursive=1",
        "repos/{owner}/{repo}/git/trees/6417e28b25ef79e8a0e6406bad2e2ae03a8330c3?recursive=1",
        f"repos/{{owner}}/{{repo}}/compare/{REVERT_BASE}...{REVERT_HEAD}",
        f"repos/{{owner}}/{{repo}}/git/trees/{REVERT_BASE}?recursive=1",
        f"repos/{{owner}}/{{repo}}/git/trees/{REVERT_HEAD}?recursive=1",
    } <= set(recorded)


def test_an_exact_revert_still_needs_check_and_the_merge_approval(
    recorded: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    hub: dict[str, list[dict[str, Any]]],
) -> None:
    monkeypatch.setattr(approvals, "required_checks", lambda _base: {"check"})
    red = given_go(revert_pr(checks=(("check", "FAILURE"),)), hub)
    assert gated(monkeypatch, capsys, "merge", red) == (
        approvals.WAITING,
        ["PR #2: `check` is FAILURE on the head commit"],
    )
    hub.clear()
    unapproved = revert_pr(body="Reverts #1\n\n## Evidence\n\n#1 set the wrong rate.\n")
    assert gated(monkeypatch, capsys, "merge", unapproved) == (
        approvals.WAITING,
        [
            f"PR #2 waits for the owner's go on its head (`approve merge 2 --head {REVERT_HEAD} --by owner`; into main `approved:merge` is no go; `loop.delegate: all` in the policy file lets a delegate's go count), since it is high risk: it matches a risk rule (path `billing/**`)"
        ],
    )
    bare = given_go(revert_pr(body="Reverts #1\n"), hub)
    assert gated(monkeypatch, capsys, "merge", bare) == (
        1,
        ["PR #2: its body has no `## Evidence` section with content"],
    )


def test_a_revert_with_one_extra_line_gets_every_proof_of_any_pr(
    recorded: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    hub: dict[str, list[dict[str, Any]]],
) -> None:
    plus = given_go(revert_pr(3), hub)
    assert gated(monkeypatch, capsys, "merge", plus) == (
        1,
        [
            (
                "PR #3 is not an exact revert of #1: `billing/rate.py` has 1 line(s) beyond the"
                " inverse of #1, 0 missing. It needs every proof of any PR"
            ),
            ("the PR names no issue: it needs a `Closes #<issue>` line for an approved spec"),
            "PR #3: no verifier review posted (approvals.py verdict)",
        ],
    )


def held_revert(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    revert: dict[str, Any],
    original: dict[str, Any],
    loop: str = BARE,
) -> tuple[int, list[str]]:
    """check merge on an exact revert of `original` (#1), by default under a loop.md with no risk rule, so only the
    risk of what it reverts can hold it."""
    monkeypatch.setattr(approvals, "project", lambda: (TRACKER, loop))
    monkeypatch.setattr(approvals, "pull_request", lambda n: original if n == 1 else revert)
    code = approvals.main(["check", "merge", str(revert["number"])])
    return code, capsys.readouterr().out.splitlines()


def test_an_exact_revert_of_a_high_risk_change_waits_for_the_owners_go(
    recorded: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    hub: dict[str, list[dict[str, Any]]],
) -> None:
    """The risk of the change a revert undoes (or re-lands) carries over: `risk:high` on an issue the revert closes,
    on the PR it reverts, or that PR's last verdict saying `Risk: high`."""
    low = {**pull(), "number": 1}
    held = f"PR #2 waits for the owner's go on its head (`approve merge 2 --head {REVERT_HEAD} --by owner`; into main `approved:merge` is no go; `loop.delegate: all` in the policy file lets a delegate's go count), since it is high risk: "
    closing = revert_pr(body=REVERT_BODY + "\nCloses #71\n")
    closing["closingIssuesReferences"] = {"nodes": [specced(71, "kind:bug", "risk:high")]}
    assert held_revert(monkeypatch, capsys, closing, low) == (
        approvals.WAITING,
        [held + "`risk:high` is on #71"],
    )
    high = {**pull(labels=("risk:high",)), "number": 1}
    assert held_revert(monkeypatch, capsys, revert_pr(), high) == (
        approvals.WAITING,
        [held + "`risk:high` is on #1, which it reverts"],
    )
    said = "Verifier (codex), pass 2\n\nVERDICT: 0 blocker, 0 major, 0 minor\nSATISFIED: yes\nRisk: high — CI permissions\n"
    judged = {**pull(reviews=(verdict_on(HEAD) | {"body": said},)), "number": 1}
    assert held_revert(monkeypatch, capsys, revert_pr(), judged) == (
        approvals.WAITING,
        [
            held
            + "it reverts #1, and the verifier's pass 2 on aaaaaaa says `Risk: high — CI permissions`"
        ],
    )
    # a risk rule reads the labels of the issues a revert names, as for any PR
    ruled = revert_pr(body=REVERT_BODY + "\nCloses #71\n")
    ruled["closingIssuesReferences"] = {"nodes": [specced(71, "kind:bug")]}
    web = add(BARE, "risk", "labels: [area:web]\n")
    assert held_revert(monkeypatch, capsys, ruled, low, web) == (
        approvals.WAITING,
        [held + "it matches a risk rule (label `area:web`)"],
    )
    # the owner's go clears each, and a revert of a low-risk PR lands without it
    assert held_revert(monkeypatch, capsys, given_go(revert_pr(), hub), high) == (
        0,
        [f"PR #2: the go on {REVERT_HEAD} is the owner's"],
    )
    assert held_revert(monkeypatch, capsys, revert_pr(), low) == (0, [])


@pytest.mark.parametrize(
    ("line", "why"),
    [
        ("Reverts #3", "#3 is not a merged pull request"),
        ("Reverts #9", "#9 is not a merged pull request"),
        (
            "Reverts other/fork#1",
            "it names a PR of other/fork, and this is kzarzycki/scratch-gate-revert",
        ),
    ],
)
def test_reverts_naming_no_merged_pr_here_is_refused(
    recorded: list[str], line: str, why: str
) -> None:
    number = re.findall(r"\d+", line)[-1]
    assert approvals.reverts(revert_pr(body=f"{line}\n")) == (
        None,
        [f"PR #2 is not an exact revert of #{number}: {why}. It needs every proof of any PR"],
    )


def test_a_quoted_or_mid_line_reverts_is_no_revert(recorded: list[str]) -> None:
    for body in ("`Reverts #1`\n", "This Reverts #1\n", "```\nReverts #1\n```\n"):
        assert approvals.reverts(revert_pr(body=body)) == (None, [])
    assert recorded == []


def entry(filename: str, patch: str | None, **fields: Any) -> dict[str, Any]:
    return {
        "filename": filename,
        "status": "modified",
        "changes": 1,
        **({"patch": patch} if patch else {}),
        **fields,
    }


def test_the_inverse_ignores_line_numbers_and_context_and_follows_renames() -> None:
    original = [
        entry("a.py", "@@ -1,3 +1,3 @@\n one\n-two\n+2\n three"),
        entry("new.py", "@@ -1,2 +1,2 @@\n-x\n+y", previous_filename="old.py", status="renamed"),
    ]
    revert = [
        entry("a.py", "@@ -10,2 +10,2 @@\n moved context\n-2\n+two"),
        entry("old.py", "@@ -1 +1 @@\n-y\n+x", previous_filename="new.py", status="renamed"),
    ]
    assert approvals.inexact(original, revert, 1, KEPT, MOVED) == []
    moved = revert[:1] + [entry("new.py", "@@ -1 +1 @@\n-y\n+x")]
    assert approvals.inexact(original, moved, 1, KEPT, MOVED) == [
        "it changes `new.py`, which #1 did not",
        "it leaves `old.py -> new.py` as #1 changed it",
    ]


def test_a_file_github_shows_no_diff_of_is_no_exact_revert() -> None:
    assert approvals.inexact(
        [entry("logo.png", None)], [entry("logo.png", None)], 1, KEPT, MOVED
    ) == [
        "GitHub shows no diff of `logo.png` to compare",
        "GitHub shows no diff of `logo.png` to compare",
    ]
    unchanged = {"filename": "run.sh", "status": "modified", "changes": 0, "patch": ""}
    assert approvals.inexact([unchanged], [unchanged], 1, KEPT, MOVED) == [
        "GitHub shows no diff of `run.sh` to compare",
        "GitHub shows no diff of `run.sh` to compare",
    ]


def test_a_missing_final_newline_counts() -> None:
    assert approvals.changed_lines("@@ -1 +1 @@\n-x\n\\ No newline at end of file\n+x") == [
        (["x"], ["x\n\\ no newline"])
    ]


CALLS = "@@ -1,2 +1,2 @@\n-authorize()\n-transfer()\n+authorize_v2()\n+transfer_v2()"
UNDO_CALLS = "@@ -3,2 +3,2 @@\n-authorize_v2()\n-transfer_v2()\n+authorize()\n+transfer()"
PATHS = ("a.py", "new.py", "old.py", "logo.png", "run.sh", "app.py")


def at(*blobs: str) -> tuple[dict[str, tuple[str, str]], ...]:
    """Trees holding every path with mode 100644, the nth tree's blobs named blobs[n]."""
    return tuple({path: ("100644", blob) for path in PATHS} for blob in blobs)


# #1's base and merge commit trees, and the revert's merge base and head after main changed the files since, so the
# trees can't show the inverse and the diffs decide; every mode kept
KEPT, MOVED = at("before", "after"), at("main", "head")


def test_the_inverse_keeps_the_order_of_changed_lines() -> None:
    original = [entry("app.py", CALLS)]
    assert approvals.inexact(original, [entry("app.py", UNDO_CALLS)], 1, KEPT, MOVED) == []
    swapped = UNDO_CALLS.replace("+authorize()\n+transfer()", "+transfer()\n+authorize()")
    assert approvals.inexact(original, [entry("app.py", swapped)], 1, KEPT, MOVED) == [
        "`app.py` changes the lines #1 changed in another order"
    ]
    # the same lines in the same order, one of them moved into another run
    two = [entry("app.py", "@@ -1,3 +1,3 @@\n-a\n+b\n x\n-c\n+d")]
    moved = [entry("app.py", "@@ -1,3 +1,3 @@\n-b\n+a\n+c\n x\n-d")]
    assert approvals.inexact(two, moved, 1, KEPT, MOVED) == [
        "`app.py` changes the lines #1 changed in another order"
    ]


def test_a_revert_that_restores_the_trees_is_exact_whatever_order_its_diff_shows() -> None:
    """A `git revert` of #1 with no later change to its files: GitHub's diff pairs the restored lines another way
    (#351), but the head holds #1's base blobs, so the change is undone."""
    original = [entry("app.py", CALLS)]
    swapped = [
        entry(
            "app.py", UNDO_CALLS.replace("+authorize()\n+transfer()", "+transfer()\n+authorize()")
        )
    ]
    restored = (KEPT[1], KEPT[0])
    assert approvals.inexact(original, swapped, 1, KEPT, restored) == []
    # a head that keeps a line of #1 holds another blob, so the diffs decide
    kept = (KEPT[1], {**KEPT[0], "app.py": ("100644", "kept")})
    assert approvals.inexact(original, swapped, 1, KEPT, kept) == [
        "`app.py` changes the lines #1 changed in another order"
    ]
    # a file #1 did not change is no part of its revert, whatever the trees hold
    extra = swapped + [entry("run.sh", "@@ -1 +1 @@\n-a\n+b")]
    assert approvals.inexact(original, extra, 1, KEPT, restored) == [
        "`app.py` changes the lines #1 changed in another order",
        "it changes `run.sh`, which #1 did not",
    ]


def test_a_revert_that_changes_a_mode_is_no_exact_revert() -> None:
    exact = ([entry("app.py", CALLS)], [entry("app.py", UNDO_CALLS)], 1)
    assert approvals.inexact(
        *exact, KEPT, (MOVED[0], {**MOVED[1], "app.py": ("100755", "head")})
    ) == ["`app.py` has mode 100755 at the head, and 100644 before #1"]
    for truncated in ((None, MOVED[1]), (MOVED[0], None)):
        assert approvals.inexact(*exact, truncated, MOVED) == [
            "GitHub truncates a tree it would compare file modes in"
        ]
        assert approvals.inexact(*exact, KEPT, truncated) == [
            "GitHub truncates a tree it would compare file modes in"
        ]


def test_a_revert_that_also_undoes_a_later_mode_change_is_no_exact_revert() -> None:
    """#1 kept app.py's mode, main made it executable after, and the revert branches from there and resets it: its
    head has app.py's mode from before #1, but it undoes main's change too."""
    exact = ([entry("app.py", CALLS)], [entry("app.py", UNDO_CALLS)], 1)
    assert approvals.inexact(
        *exact, KEPT, ({**MOVED[0], "app.py": ("100755", "main")}, MOVED[1])
    ) == ["`app.py` has mode 100755 at the merge base, and 100644 after #1"]


def test_an_empty_file_left_in_place_is_no_exact_revert() -> None:
    added = {"filename": "run.sh", "status": "added", "changes": 0}
    moded = {"filename": "run.sh", "status": "modified", "changes": 0}
    assert approvals.inexact(
        [added],
        [moded],
        1,
        ({}, {"run.sh": ("100644", "empty")}),
        ({"run.sh": ("100644", "empty")}, {"run.sh": ("100755", "empty")}),
    ) == [
        "GitHub shows no diff of `run.sh` to compare",
        "GitHub shows no diff of `run.sh` to compare",
        "`run.sh` is modified, and undoing #1 needs removed",
        "`run.sh` has mode 100755 at the head, and absent before #1",
    ]


# --- writing proof ---


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Every write approvals.py makes, instead of making it."""
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        approvals,
        "comment",
        lambda number, body: calls.append(("comment", number, approvals.machine(body))),
    )
    monkeypatch.setattr(
        approvals, "label", lambda number, name, add: calls.append(("label", number, name, add))
    )
    monkeypatch.setattr(
        approvals, "minimize", lambda comment_id: calls.append(("minimize", comment_id))
    )
    in_project(monkeypatch)
    return calls


def test_the_owners_approval_is_recorded_labelled_and_clears_needs_owner(
    github: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals, "issue_node", lambda _n: specced(1, "kind:bug", "needs-owner"))
    assert approvals.main(["approve", "spec", "1", "--by", "owner"]) == 0
    assert github == [
        ("comment", 1, f"Approved: spec\nBy: owner\nSpec: {TEXTED}\n"),
        ("label", 1, "approved:spec", True),
        ("label", 1, "needs-owner", False),
    ]


def test_a_post_naming_a_term_of_the_local_deny_list_is_refused_before_any_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The list names private terms; a body that names one, in any case and as a whole word, is refused naming the
    term and the line, and nothing reaches gh. Without the variable, nothing changes."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        approvals.subprocess,
        "run",
        lambda command, **_kw: calls.append(command) or subprocess.CompletedProcess(command, 0, ""),
    )
    deny = tmp_path / "deny.txt"
    deny.write_text(
        "# private names\nAcme-Widgets\nacme/secret-repo  # its repo\nbuild-host.local\n"
    )
    monkeypatch.setenv("LOOP_DENY_LIST", str(deny))
    for body, term, line in [
        ("Fixed.\nSeen in ACME-WIDGETS's run.\n", "Acme-Widgets", 2),
        ("See acme/secret-repo#12.\n", "acme/secret-repo", 1),
        ("Ran on build-host.local\n", "build-host.local", 1),
    ]:
        with pytest.raises(approvals.Refused) as refused:
            approvals.comment(7, body)
        assert str(refused.value) == (
            f"refused to post: line {line} names `{term}` from the deny list (LOOP_DENY_LIST); nothing posted"
        )
    with pytest.raises(approvals.Refused, match="names `Acme-Widgets`"):
        approvals.post_review(7, {"body": "pass 1 on acme-widgets"})
    # a review is JSON: the text GitHub publishes is checked, not its escaped form (`\nAcme`, `Caf\u00e9`)
    deny.write_text("Acme-Widgets\nCafé\n")
    for review, term, line in [
        ({"body": "Safe heading\nAcme-Widgets is listed"}, "Acme-Widgets", 2),
        (
            {
                "body": "Safe",
                "comments": [{"path": "x.py", "line": 1, "body": "Safe\n\nSeen in café"}],
            },
            "Café",
            3,
        ),
    ]:
        with pytest.raises(approvals.Refused) as refused:
            approvals.post_review(7, review)
        assert f"line {line} names `{term}`" in str(refused.value)
    assert calls == []
    # a term inside a longer word is no match
    approvals.comment(7, "Acme-Widgetsmith and xacme/secret-repo are fine.\n")
    assert len(calls) == 1
    monkeypatch.delenv("LOOP_DENY_LIST")
    approvals.comment(7, "Seen in Acme-Widgets's run.\n")
    assert len(calls) == 2
    monkeypatch.setenv("LOOP_DENY_LIST", str(tmp_path / "missing.txt"))
    with pytest.raises(approvals.Refused, match="can't be read"):
        approvals.comment(7, "anything")
    assert len(calls) == 2


def test_verdict_refuses_a_report_naming_a_listed_term_before_reading_github(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deny, report = tmp_path / "deny.txt", tmp_path / "report.md"
    deny.write_text("Acme-Widgets\n")
    monkeypatch.setenv("LOOP_DENY_LIST", str(deny))
    report.write_text(REPORT.replace("The doc.", "Seen in Acme-Widgets."))
    read: list[str] = []
    monkeypatch.setattr(approvals, "pull_request", lambda _n: read.append("pull") or pull())
    monkeypatch.setattr(approvals, "diff_lines", lambda *_a: read.append("diff") or {})
    monkeypatch.setattr(approvals, "one_repo", lambda: None)
    assert approvals.main(["verdict", "7", str(report)]) == 1
    assert "names `Acme-Widgets` from the deny list" in capsys.readouterr().out
    assert read == []


@pytest.mark.parametrize(
    ("actor", "at", "said"),
    [
        (
            "octocat",
            LABELED,
            "#1: recorded the owner's `approved:spec`, added after the spec's last edit, as their approval",
        ),
        (
            "octocat",
            "2026-10-01T08:00:00Z",
            (
                f"#1: the owner's `approved:spec` was added before the spec's last edit ({AS_WRITTEN}): the owner"
                " adds it again"
            ),
        ),
        ("someone", LABELED, None),
    ],
    ids=["the owner after the edit", "the owner before it", "another account"],
)
def test_the_owners_own_label_after_the_last_edit_is_their_approval(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    actor: str,
    at: str,
    said: str | None,
) -> None:
    """With loop.md's `owner`, the owner's `approved:spec` added after the spec's last edit is recorded as their word
    and `needs-owner` comes off, so the gate proceeds; an older label or anyone else's leaves the refusal."""
    loop = add(LOOP, "loop", "owner: octocat\n")
    spec = issue(1, "kind:enhancement", "area:web", "size:S", "needs-owner", "approved:spec")
    spec["timelineItems"] = {
        "nodes": [
            {
                "__typename": "LabeledEvent",
                "createdAt": at,
                "actor": {"__typename": "User", "login": actor},
                "label": {"name": "approved:spec"},
            }
        ]
    }

    def label(number: int, name: str, add: bool) -> None:
        held = [node for node in spec["labels"]["nodes"] if node["name"] != name]
        spec["labels"]["nodes"] = held + [{"name": name}] * add

    monkeypatch.setattr(approvals, "label", label)
    monkeypatch.setattr(
        approvals, "comment", lambda _n, body: spec["comments"]["nodes"].append(note(body))
    )
    monkeypatch.setattr(approvals, "issue_node", lambda _n: spec)
    pr = pull()
    pr["closingIssuesReferences"] = {"nodes": [spec]}
    found, _issues = approvals.gated("merge", pr, TRACKER, loop)
    if actor == "octocat" and at == LABELED:
        assert found == ["PR #7: no verifier review posted (approvals.py verdict)"]
        assert "needs-owner" not in approvals.names(spec)
    else:
        assert found[:2] == [
            "#1 waits for the owner (needs-owner)",
            "#1 has no `Approved: spec` record by the owner",
        ]
    assert capsys.readouterr().out.splitlines() == ([said] if said else [])


def test_check_build_before_the_pr_records_the_owners_own_label(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`check build --issue <n>`, before a PR exists, adopts the owner's own label as the PR path does."""
    loop = add(LOOP, "loop", "owner: octocat\n")
    spec = issue(1, "kind:enhancement", "area:web", "size:S", "needs-owner", "approved:spec")
    spec["timelineItems"] = {
        "nodes": [
            {
                "__typename": "LabeledEvent",
                "createdAt": LABELED,
                "actor": {"__typename": "User", "login": "octocat"},
                "label": {"name": "approved:spec"},
            }
        ]
    }

    def label(number: int, name: str, add: bool) -> None:
        held = [node for node in spec["labels"]["nodes"] if node["name"] != name]
        spec["labels"]["nodes"] = held + [{"name": name}] * add

    monkeypatch.setattr(approvals, "label", label)
    monkeypatch.setattr(
        approvals, "comment", lambda _n, body: spec["comments"]["nodes"].append(note(body))
    )
    monkeypatch.setattr(approvals, "issue_node", lambda _n: spec)
    monkeypatch.setattr(approvals, "project", lambda: (TRACKER, loop))
    monkeypatch.setattr(approvals, "one_repo", lambda: None)
    monkeypatch.setattr(
        approvals, "git", lambda *args: "" if args[0] == "diff" else "origin/main\n"
    )
    assert approvals.main(["check", "build", "--issue", "1"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "#1: recorded the owner's `approved:spec`, added after the spec's last edit, as their approval",
        "#1: every build proof holds; no PR yet",
    ]


def test_an_edit_after_the_owners_label_is_never_recorded_under_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spec is read again to record it: an edit between the check and the record is refused, not approved, and
    loop.md's owner matches the label's actor whatever its casing, as GitHub's logins do."""
    loop = add(LOOP, "loop", "owner: Octocat\n")
    spec = issue(1, "kind:enhancement", "area:web", "size:S", "needs-owner", "approved:spec")
    spec["timelineItems"] = {
        "nodes": [
            {
                "__typename": "LabeledEvent",
                "createdAt": LABELED,
                "actor": {"__typename": "User", "login": "octocat"},
                "label": {"name": "approved:spec"},
            }
        ]
    }
    later = {**spec, "body": "the spec, edited", "lastEditedAt": "2026-10-01T12:00:00Z"}
    sent: list[str] = []
    monkeypatch.setattr(approvals, "label", lambda *args, **kwargs: sent.append("label"))
    monkeypatch.setattr(approvals, "comment", lambda _n, body: sent.append(approvals.machine(body)))
    monkeypatch.setattr(approvals, "issue_node", lambda _n: later)
    with pytest.raises(
        approvals.Refused, match=r"#1: the spec was edited after the owner's `approved:spec`"
    ):
        approvals.adopted([spec], loop)
    assert sent == []
    monkeypatch.setattr(approvals, "issue_node", lambda _n: spec)
    approvals.adopted([spec], loop)
    assert sent[0] == f"Approved: spec\nBy: owner\nSpec: {TEXTED}\n"


def test_approve_refuses_the_coordinator(
    github: list[tuple[Any, ...]], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        approvals.main(["approve", "spec", "1", "--by", "coordinator"])
    assert exited.value.code == 2
    assert "invalid choice: 'coordinator'" in capsys.readouterr().err
    assert github == []


def test_delegate_labels_then_records_a_feature_branch_pr_and_refuses_the_default_branch(
    github: list[tuple[Any, ...]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pr = pull()
    monkeypatch.setattr(approvals, "pull_request", lambda _n: pr)
    assert approvals.main(["delegate", "7"]) == 1
    assert capsys.readouterr().out == (
        "PR #7 targets main, the default branch, where a label is no go: the head-pinned `approve merge 7 --head <sha>"
        " --by delegate` gives the delegate's go there with `loop.delegate: all`, and the owner's go always covers it\n"
    )
    assert github == []
    pr["baseRefName"] = "stream"
    assert approvals.main(["delegate", "7"]) == 0
    assert github == [
        ("label", 7, "approved:merge", True),
        ("comment", 7, "Approved: merge\nBy: delegate\n"),
    ]


def test_delegate_takes_its_label_off_again_when_the_record_fails(
    github: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A label without its record would read as the owner's once the PR is retargeted to the default branch."""
    pr = pull()
    pr["baseRefName"] = "stream"
    monkeypatch.setattr(approvals, "pull_request", lambda _n: pr)

    def failing(number: int, body: str) -> None:
        raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 503: Service Unavailable")

    monkeypatch.setattr(approvals, "comment", failing)
    assert approvals.main(["delegate", "7"]) == 1
    assert github == [
        ("label", 7, "approved:merge", True),
        ("label", 7, "approved:merge", False),
    ]


def test_a_re_approval_says_why_and_minimizes_every_record_it_supersedes(
    github: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    agents = note(record("spec", "coordinator", spec=AS_WRITTEN))
    owners = note(record("spec", "owner", spec=AS_WRITTEN))
    edited = specced(1, "kind:bug", "size:XL")
    edited["comments"]["nodes"] = [agents, owners]
    edited["lastEditedAt"] = "2026-10-02T14:35:27Z"
    monkeypatch.setattr(approvals, "issue_node", lambda _n: edited)
    assert approvals.main(["approve", "spec", "1", "--by", "owner"]) == 0
    assert github[:3] == [
        (
            "comment",
            1,
            (
                f"Approved: spec\nBy: owner\nSpec: {TEXTED}\n"
                "The spec changed after the last approval, so it was checked again.\n"
            ),
        ),
        ("minimize", agents["id"]),
        ("minimize", owners["id"]),
    ]


def test_a_comment_gate_py_cannot_or_need_not_minimize_is_left_alone(
    github: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    theirs = note(
        record("spec", "owner", spec="as written 2026-09-01 09:00:00 UTC"),
        viewerCanMinimize=False,
    )
    hidden = note(
        record("spec", "owner", spec="as written 2026-09-02 09:00:00 UTC"), isMinimized=True
    )
    old = specced(1, "kind:bug")
    old["comments"]["nodes"] = [theirs, hidden]
    monkeypatch.setattr(approvals, "issue_node", lambda _n: old)
    assert approvals.main(["approve", "spec", "1", "--by", "owner"]) == 0
    assert [call for call in github if call[0] == "minimize"] == []


def test_approve_merge_posts_the_owners_go_on_the_head_and_a_record_it_writes_whole(
    github: list[tuple[Any, ...]],
    hub: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(approvals, "pull_request", lambda _n: pull())
    assert approvals.main(["approve", "merge", "7", "--head", HEAD, "--by", "owner"]) == 0
    assert [(s["context"], s["description"]) for s in hub[HEAD]] == [
        ("owner", f"The owner's go on {HEAD[:12]}")
    ]
    assert github == [
        ("comment", 7, f"Approved by the owner.\nApproved: merge\nBy: owner\nHead: {HEAD}\n")
    ]


def test_approve_merge_refuses_a_closed_pr(
    github: list[tuple[Any, ...]],
    hub: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(approvals, "pull_request", lambda _n: {**pull(), "state": "MERGED"})
    assert approvals.main(["approve", "merge", "7", "--head", HEAD, "--by", "owner"]) == 1
    assert (github, hub) == ([], {})


def test_the_owners_go_covers_its_head_only(
    hub: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    no_origin: list[list[str]],
) -> None:
    """A status belongs to one commit: a push moves the head off it, with no label to remove. The status needs the
    owner's record naming the head beside it, so a status alone is no go."""
    high = pull(labels=("risk:high",))
    assert approvals.owner_waits(high, [], LOOP) == [HELD + "`risk:high` is on PR #7"]
    hub[HEAD] = [status("owner", f"The owner's go on {HEAD[:12]}")]
    assert approvals.owner_waits(high, [], LOOP) == [HELD + "`risk:high` is on PR #7"]
    high["comments"]["nodes"].append(note(record("merge", "owner", head=HEAD)))
    assert approvals.owner_waits(high, [], LOOP) == []
    assert approvals.owner_waits({**high, "headRefOid": "c" * 40}, [], LOOP) == [
        HELD.replace(HEAD, "c" * 40) + "`risk:high` is on PR #7",
        f"PR #7: the go on {HEAD} carries nothing to {'c' * 40}: no verifier review is on {'c' * 40}",
    ]
    monkeypatch.setattr(approvals, "writer", lambda login: False)
    hub[HEAD] = [status("owner", "The owner's go", by="contributor")]  # anyone can't give it
    assert approvals.owner_waits(high, [], LOOP) == [HELD + "`risk:high` is on PR #7"]


def test_approve_merge_refuses_a_head_other_than_the_one_the_owner_was_shown(
    github: list[tuple[Any, ...]],
    hub: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(approvals, "pull_request", lambda _n: pull())
    assert approvals.main(["approve", "merge", "7", "--head", "c" * 40, "--by", "owner"]) == 1
    assert approvals.main(["approve", "merge", "7", "--by", "owner"]) == 1
    assert approvals.main(["approve", "spec", "7", "--head", HEAD, "--by", "owner"]) == 1
    assert (github, hub) == ([], {})
    out = capsys.readouterr().out
    assert f"PR #7's head is {HEAD}, not {'c' * 40}" in out and "needs --head <sha>" in out
    assert "belong to `approve merge`" in out


def test_withdrawing_a_go_removes_the_label_so_it_stays_withdrawn_into_a_feature_branch(
    github: list[tuple[Any, ...]],
    hub: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    labelled = into_feature(pull(labels=("risk:high", "approved:merge")))
    monkeypatch.setattr(approvals, "pull_request", lambda _n: labelled)
    assert approvals.owner_waits(labelled, [], LOOP) == []
    assert (
        approvals.main(["approve", "merge", "7", "--head", HEAD, "--by", "owner", "--withdraw"])
        == 0
    )
    assert ("label", 7, "approved:merge", False) in github
    unlabelled = into_feature(pull(labels=("risk:high",)))  # what GitHub reads back
    assert approvals.owner_waits(unlabelled, [], LOOP)


def test_a_newer_failing_owner_status_withdraws_the_go(
    github: list[tuple[Any, ...]],
    hub: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    high = pull(labels=("risk:high",))
    monkeypatch.setattr(approvals, "pull_request", lambda _n: high)
    assert approvals.main(["approve", "merge", "7", "--head", HEAD, "--by", "owner"]) == 0
    # what GitHub reads back
    high["comments"]["nodes"].append(note(record("merge", "owner", head=HEAD)))
    assert approvals.owner_waits(high, [], LOOP) == []
    assert (
        approvals.main(["approve", "merge", "7", "--head", HEAD, "--by", "owner", "--withdraw"])
        == 0
    )
    assert [s["state"] for s in hub[HEAD]] == ["failure", "success"]
    assert github[-1] == ("comment", 7, f"Withdrawn: merge\nBy: owner\nHead: {HEAD}\n")
    assert approvals.owner_waits(high, [], LOOP) == [HELD + "`risk:high` is on PR #7"]


def test_a_go_once_turned_from_the_label_is_no_go(hub: dict[str, list[dict[str, Any]]]) -> None:
    """An owner status the gate wrote from the label stays on old heads; the label can be a delegate's, so it is no go."""
    high = pull(labels=("risk:high",))
    high["comments"]["nodes"].append(note(record("merge", "owner", head=HEAD)))
    hub[HEAD] = [status("owner", f"The owner's go on {HEAD[:12]} {approvals.LABELLED}")]
    assert approvals.owner_waits(high, [], LOOP) == [HELD + "`risk:high` is on PR #7"]


def test_the_label_into_the_default_branch_is_no_go_and_writes_nothing(
    hub: dict[str, list[dict[str, Any]]],
) -> None:
    labelled = pull(labels=("risk:high", "approved:merge"))
    assert approvals.owner_waits(labelled, [], LOOP) == [HELD + "`risk:high` is on PR #7"]
    assert approvals.owner_waits(into_feature(labelled), [], LOOP) == []
    assert hub == {}  # the gate writes no status from a label


def test_approved_merge_on_a_change_that_is_not_high_risk_changes_nothing(
    hub: dict[str, list[dict[str, Any]]],
) -> None:
    low = pull(reviews=(VERDICT,))
    assert merge(pull(reviews=(VERDICT,), labels=OWNED), [approved(1, "area:web")]) == merge(
        low, [approved(1, "area:web")]
    )
    assert approvals.owner_waits(pull(labels=OWNED), [], LOOP) == []
    assert hub == {}


REPORT = f"""# Verifier report: PR #7, pass 2

Verifier: claude, same-family, pass 2

## Criteria

- A draft never merges on its skipped checks: Before merge line 1, its check.

## Correctness

### C1 (blocker) app.py:12: a draft merges on its skipped checks

- Failing input: a draft.

### C2 (minor) app.py:40: a line outside the diff

Evidence: the run.

### S1 (minor) docs/old.md:3: a file outside the diff

The doc.

Head: {HEAD}
VERDICT: 1 blocker, 0 major, 2 minor
SATISFIED: no
Risk: low — a test fixture
"""


@pytest.fixture
def reviewing(
    github: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> list[tuple[Any, ...]]:
    """`verdict` against a stubbed GitHub: the PR's base, the diff's commentable lines, and each review it posts."""
    asked: list[tuple[str, str]] = []

    def diff_lines(base: str, head: str) -> dict[str, list[int]]:
        asked.append((base, head))
        return {"app.py": [10, 11, 12, 13, 30, 31]}

    monkeypatch.setattr(approvals, "pull_request", lambda _n: {**pull(), "headRefOid": "2" * 40})
    monkeypatch.setattr(approvals, "diff_lines", diff_lines)
    monkeypatch.setattr(
        approvals, "post_review", lambda pr, review: github.append(("review", pr, review))
    )
    github.append(("asked", asked))
    return github


@pytest.mark.parametrize(
    "heading",
    [
        "Verifier (codex), pass 5",
        "Verifier (codex/terra-6.1/med), pass 5",
        "Verifier (cc/opus-5.5/?, same-family), pass 5",
    ],
)
@pytest.mark.parametrize("marker", ["", "> 🤖 AI\n\n"])
def test_a_review_or_comment_is_found_with_or_without_a_signature_and_the_ai_marker(
    heading: str, marker: str
) -> None:
    review = verdict_on(HEAD, heading=heading)
    assert approvals.PASS.match(review["body"])[2] == "5"
    note = {"body": f"{marker}{heading}\n\nbody", "author": {"login": "loop"}}
    assert approvals.headed({"comments": {"nodes": [note]}}, heading) is not None


def test_a_report_whose_verifier_line_names_a_signature_becomes_the_headings_family(
    tmp_path: Path,
) -> None:
    report = tmp_path / "r.md"
    report.write_text(REPORT.replace("claude, same-family, pass 2", "codex/terra-6.1/med, pass 5"))
    assert approvals.parse_report(report)[0] == "Verifier (codex/terra-6.1/med), pass 5"


def test_a_qualifier_after_the_pass_number_is_posted_in_the_reviews_first_line(
    reviewing: list[tuple[Any, ...]], tmp_path: Path
) -> None:
    """Text after `pass <n>` on the `Verifier:` line is accepted and posted, so a qualifier the project asks for needs
    no line or comment of its own; the heading the gate reads stays `Verifier (<family>), pass <n>`."""
    report = tmp_path / "report.md"
    report.write_text(
        REPORT.replace(
            "Verifier: claude, same-family, pass 2",
            "Verifier: claude, same-family, pass 2 (merge of main; the other family is out of quota)",
        )
    )
    assert approvals.main(["verdict", "7", str(report)]) == 0
    (_, _asked), (_, _pr, review) = reviewing
    first = review["body"].splitlines()[0]
    assert first == (
        f"Verifier (claude, same-family), pass 2 on {HEAD[:7]}: (merge of main; the other family is out of quota)."
    )
    assert approvals.PASS.match(review["body"]).groups() == ("claude, same-family", "2")
    assert review["comments"][0]["body"].startswith(
        "**Verifier (claude, same-family), pass 2 · C1**"
    )


@pytest.mark.parametrize("qualifier", [": note, pass 12", "(redoes Verifier (other), pass 12)"])
def test_a_qualifier_naming_another_pass_never_changes_the_pass(
    tmp_path: Path, qualifier: str
) -> None:
    report = tmp_path / "report.md"
    line = "Verifier: claude, same-family, pass 2"
    report.write_text(REPORT.replace(line, f"{line} {qualifier}"))
    heading, *_rest, said = approvals.parse_report(report)
    assert approvals.PASS.match(heading).groups() == ("claude, same-family", "2")
    assert said == qualifier.lstrip(": ")


def test_brief_embeds_the_prs_text_labels_and_a_body_fence_stays_inside_its_own(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A verifier with no GitHub access reads the title, body and labels from the brief; a body holding a fence is
    wrapped in a longer one, so it cannot close the brief's fence early."""
    monkeypatch.chdir(tmp_path)
    body = "Closes #12.\n\n```sh\nrm -rf x\n```\n\n````\nnested\n````\n"
    pr = pull(body=body)
    pr["title"] = "fix: a `quoted` title"
    pr["labels"] = {"nodes": [{"name": "kind:bug"}, {"name": "risk:low"}]}
    monkeypatch.setattr(approvals, "pull_request", lambda _n: pr)
    monkeypatch.setattr(approvals, "merge_base", lambda base, head: BASE)
    monkeypatch.setattr(approvals, "project", lambda: (None, LOOP))
    assert approvals.main(["brief", "7"]) == 0
    out = capsys.readouterr().out
    assert "- Spec: #12\n" in out
    assert "- Labels: kind:bug, risk:low\n" in out
    assert "PR title:\n```\nfix: a `quoted` title\n```\n" in out
    assert f"PR body:\n`````\n{body.rstrip()}\n`````" in out


def test_brief_fills_the_facts_from_the_pr_and_refuses_a_stale_proof(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The base is the merge-base with the default branch, the head the PR's, the pass one after the newest verdict
    review's, whose commit is the previous report's head; loop.md's `## Verifier brief` is appended."""
    # no clone here, so the unforced-merge read's fetch of the made-up head fails at once
    monkeypatch.chdir(tmp_path)
    earlier = "c" * 40
    reviews = (
        verdict_on(earlier, heading="Verifier (codex), pass 1"),
        verdict_on(earlier, heading="Verifier (codex), pass 2"),
    )
    pr = pull(body="Closes #12.\n\n## Evidence\n\nlog\n", reviews=reviews)
    asked: list[tuple[str, str]] = []
    monkeypatch.setattr(approvals, "pull_request", lambda _n: pr)
    monkeypatch.setattr(
        approvals, "merge_base", lambda base, head: asked.append((base, head)) or BASE
    )
    monkeypatch.setattr(
        approvals,
        "project",
        lambda: (
            None,
            LOOP + "\n## Verifier brief\n\n- Run the full gate bare.\n    indented snippet  \n\n",
        ),
    )
    priorities, proof, stale = tmp_path / "p.md", tmp_path / "gate.log", tmp_path / "old.log"
    priorities.write_text("- the revert path\n")
    proof.write_text(f"{HEAD}\nok\n")
    stale.write_text(f"{earlier}\nok\n")
    assert (
        approvals.main(["brief", "7", "--priorities", str(priorities), "--proof", str(proof)]) == 0
    )
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("Review PR #7 as the verifier, pass 3, following ")
    assert out[0].endswith("verifier.md.")
    assert out[1:] == [
        f"- Base: {BASE}",
        f"- Reviewed head: {HEAD}",
        "- Spec: #12",
        f"- Acceptance tests: uv run {os.path.relpath(SCRIPTS / 'acceptance.py')} 12",
        "- Report: tmp/loop/pr7-v3.md",
        f"- Previous report: tmp/loop/pr7-v2.md, on {earlier}: review the diff since that head and rerun its repros",
        f"- Proof log: {proof} (its first line is the head)",
        "",
        "PR title:",
        "```",
        pr["title"],
        "```",
        "",
        "PR body:",
        "```",
        "Closes #12.",
        "",
        "## Evidence",
        "",
        "log",
        "```",
        "",
        "Priorities:",
        "- the revert path",
        "",
        "- Run the full gate bare.",
        "    indented snippet  ",
    ]
    assert asked == [("main", HEAD)]
    assert approvals.main(["brief", "7", "--proof", str(proof), "--proof", str(stale)]) == 1
    assert (
        capsys.readouterr().out
        == f"PR #7: stale proof, its first line isn't the head {HEAD}: {stale}; rerun it\n"
    )
    # the first line exactly: a trailing space or a CR is not the head; a missing log is named, not a traceback
    for line in (f"{HEAD} ", f"{HEAD}\r"):
        stale.write_bytes(f"{line}\nok\n".encode())
        assert approvals.main(["brief", "7", "--proof", str(stale)]) == 1
        assert "stale proof" in capsys.readouterr().out
    assert approvals.main(["brief", "7", "--proof", str(tmp_path / "absent.log")]) == 1
    assert capsys.readouterr().out == f"PR #7: no proof log at {tmp_path / 'absent.log'}\n"
    # the previous pass's findings travel in the brief: a cloud verifier can't read the host's report file
    pr["reviewThreads"]["nodes"] = [
        {
            "isResolved": False,
            "comments": {
                "nodes": [
                    {"body": "**Verifier (codex), pass 2 · F1** (major): a gap\n\nit fails"},
                    {"body": "**Verifier (codex), pass 1 · F9** (minor): older"},
                ]
            },
        }
    ]
    assert approvals.main(["brief", "7"]) == 0
    out = capsys.readouterr().out
    assert (
        "- Previous findings, pass 2:\n  1. **Verifier (codex), pass 2 · F1** (major): a gap" in out
    )
    assert "F9" not in out
    # a finding on a file outside the diff sits in the review's body
    pr["reviews"]["nodes"][-1]["body"] += (
        "- **Verifier (codex), pass 2 · F3** (minor): x, at `a.md:1`\n- **Verifier (codex), pass 1 · F8** (minor): y\n"
    )
    assert approvals.main(["brief", "7"]) == 0
    out = capsys.readouterr().out
    assert "F3** (minor): x" in out and "F8" not in out
    pr["reviews"] = {"nodes": []}
    assert approvals.main(["brief", "7"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "- Report: tmp/loop/pr7-v1.md" in out
    assert not [line for line in out if line.startswith("- Previous report")]
    # a policy in gatekeeper.md leaves the Verifier brief in loop.md, the loop's prose
    agents = tmp_path / "docs" / "agents"
    agents.mkdir(parents=True)
    (agents / "loop.md").write_text("## Verifier brief\n\n- Read the loop file's brief.\n")
    monkeypatch.setattr(approvals, "toplevel", lambda: tmp_path)
    monkeypatch.setattr(approvals, "project", lambda: (None, approvals.Policy(gatekeeper_md())))
    assert approvals.main(["brief", "7"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "- Read the loop file's brief."


def test_a_far_brief_names_the_verifier_host_s_paths_and_the_worktree_it_makes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A verifier on another host makes its own detached worktree at the head there: the brief gives the fetch and the
    worktree line, the role in that worktree, and the report, previous report and proof logs in its clone's tmp/loop/,
    where start --file writes them. A relative --far is no path there."""
    # no clone here, so the unforced-merge read's fetch of the made-up head fails at once
    monkeypatch.chdir(tmp_path)
    earlier = "c" * 40
    pr = pull(
        body="Closes #12.\n", reviews=(verdict_on(earlier, heading="Verifier (codex), pass 1"),)
    )
    monkeypatch.setattr(approvals, "pull_request", lambda _n: pr)
    monkeypatch.setattr(approvals, "merge_base", lambda base, head: BASE)
    monkeypatch.setattr(approvals, "project", lambda: (None, LOOP))
    root = Path(approvals.__file__).resolve().parents[4]
    monkeypatch.setattr(approvals, "toplevel", lambda: root)
    proof = tmp_path / "gate.log"
    proof.write_text(f"{HEAD}\nok\n")
    assert approvals.main(["brief", "7", "--far", "/there/repo", "--proof", str(proof)]) == 0
    tree = "/there/repo/tmp/loop/pr7-v2-tree"
    assert capsys.readouterr().out.splitlines() == [
        f"Review PR #7 as the verifier, pass 2, following {tree}/loop/skills/engineering-loop/verifier.md.",
        f"- Base: {BASE}",
        f"- Reviewed head: {HEAD}",
        "- Spec: #12",
        f"- Acceptance tests: uv run {tree}/gatekeeper/skills/gatekeeper/scripts/acceptance.py 12",
        (
            f"- Worktree: first, in /there/repo, run `git fetch origin pull/7/head` and"
            f" `git worktree add --detach {tree} {HEAD}`, then run every command of this brief in {tree}"
        ),
        "- Report: /there/repo/tmp/loop/pr7-v2.md",
        (
            f"- Previous report: /there/repo/tmp/loop/pr7-v1.md, on {earlier}: review the diff since that head and"
            " rerun its repros"
        ),
        "- Proof log: /there/repo/tmp/loop/gate.log (its first line is the head)",
        "",
        "PR title:",
        "```",
        "Feature",
        "```",
        "",
        "PR body:",
        "```",
        "Closes #12.",
        "```",
    ]
    # the previous pass saved its repros beside its report in the clone's tmp/loop/: named there by path
    (tmp_path / "tmp" / "loop").mkdir(parents=True)
    (tmp_path / "tmp/loop/pr7-v1.md").write_text(
        "Repro: `uv run pytest tmp/loop/pr7-v1-repro.py`, again tmp/loop/pr7-v1-repro.py,"
        " tmp/loop/pr7-v1.md and tmp/loop/pr7-v1-tree/x.py\n"
    )
    assert approvals.main(["brief", "7", "--far", "/there/repo"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert [line for line in out if line.startswith("- Repro")] == [
        "- Repro: /there/repo/tmp/loop/pr7-v1-repro.py"
    ]
    # a verifier here works where the lead copied its repros: the brief lists none
    assert approvals.main(["brief", "7"]) == 0
    assert not [line for line in capsys.readouterr().out.splitlines() if line.startswith("- Repro")]
    assert approvals.main(["brief", "7", "--far", "there/repo"]) == 1
    assert capsys.readouterr().out == "--far there/repo: an absolute path on the verifier's host\n"


def test_the_verdict_posts_the_report_as_a_review_on_the_commit_it_reviewed(
    reviewing: list[tuple[Any, ...]], tmp_path: Path
) -> None:
    """The commit is the report's `Head:`, not the PR's head when it is posted: a push in between would otherwise get
    a review nobody gave. A finding's line outside the diff moves to the nearest diff line and says so; a file
    outside the diff goes in the review's body."""
    report = tmp_path / "report.md"
    report.write_text(REPORT)
    assert approvals.main(["verdict", "7", str(report)]) == 0
    (_, asked), (_, pr, review) = reviewing
    heading = "Verifier (claude, same-family), pass 2"
    assert (asked, pr) == ([(BASE, HEAD)], 7)
    assert review == {
        "commit_id": HEAD,
        "event": "COMMENT",
        "body": approvals.fold(
            f"{heading} on {HEAD[:7]}.",
            "VERDICT: 1 blocker, 0 major, 2 minor\nSATISFIED: no\nRisk: low — a test fixture",
            "## Criteria\n\n- A draft never merges on its skipped checks: Before merge line 1, its check.\n\n"
            f"- **{heading} · S1** (minor): a file outside the diff, at `docs/old.md:3`, a file outside the diff\n\n"
            "The doc.",
        ),
        "comments": [
            {
                "path": "app.py",
                "line": 12,
                "side": "RIGHT",
                "body": f"**{heading} · C1** (blocker): a draft merges on its skipped checks\n\n- Failing input: a draft.",
            },
            {
                "path": "app.py",
                "line": 31,
                "side": "RIGHT",
                "body": (
                    f"**{heading} · C2** (minor): a line outside the diff\n\n"
                    "At `app.py:40`, outside the diff: placed at the nearest diff line.\n\nEvidence: the run."
                ),
            },
        ],
    }


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (
            ("SATISFIED: no\n", ""),
            "has no `VERDICT:` or `SATISFIED:` line: the report is unfinished",
        ),
        ((f"Head: {HEAD}\n", ""), "has no `Head: <sha>` line naming the full commit it reviewed"),
        (
            (f"Head: {HEAD}\n", f"Head: {HEAD[:7]}\n"),
            "has no `Head: <sha>` line naming the full commit it reviewed",
        ),
        (
            ("Verifier: claude, same-family, pass 2\n", ""),
            "has no `Verifier: <family>, pass <n>` line",
        ),
    ],
)
def test_an_unfinished_report_posts_nothing(
    reviewing: list[tuple[Any, ...]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    change: tuple[str, str],
    why: str,
) -> None:
    report = tmp_path / "report.md"
    report.write_text(REPORT.replace(*change))
    assert approvals.main(["verdict", "7", str(report)]) == 1
    assert capsys.readouterr().out == f"{report} {why}\n"
    assert [call for call in reviewing if call[0] == "review"] == []


RANGED = f"""Verifier: claude, pass 1

## Criteria

- The review posts each finding.

## Correctness

### C1 (Major) app.py:9-11: a line range and a capitalised severity

```
$ grep x
# the line that matters
### quoted, not a finding
```

### C2 (minor) `app.py:28-29`: a range outside the diff

The run.

Head: {HEAD}
VERDICT: 0 blocker, 1 major, 1 minor
SATISFIED: no
Risk: low — a test fixture
"""


def test_a_finding_takes_a_line_range_any_case_severity_and_a_fenced_heading_in_its_evidence(
    reviewing: list[tuple[Any, ...]], tmp_path: Path
) -> None:
    """A range anchors at its last line, or the nearest diff line; a `#` line inside a code fence is evidence."""
    report = tmp_path / "report.md"
    report.write_text(RANGED)
    assert approvals.main(["verdict", "7", str(report)]) == 0
    heading = "Verifier (claude), pass 1"
    assert reviewing[-1][2]["comments"] == [
        {
            "path": "app.py",
            "line": 11,
            "side": "RIGHT",
            "body": (
                f"**{heading} · C1** (major): a line range and a capitalised severity\n\n"
                "```\n$ grep x\n# the line that matters\n### quoted, not a finding\n```"
            ),
        },
        {
            "path": "app.py",
            "line": 30,
            "side": "RIGHT",
            "body": (
                f"**{heading} · C2** (minor): a range outside the diff\n\n"
                "At `app.py:28-29`, outside the diff: placed at the nearest diff line.\n\nThe run."
            ),
        },
    ]


def test_a_finding_heading_verdict_cant_read_posts_nothing(
    reviewing: list[tuple[Any, ...]], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A finding the review would leave out is no finding on the trail: refuse rather than drop it."""
    report = tmp_path / "report.md"
    report.write_text(REPORT.replace("### C2 (minor) app.py:40:", "### C2 (minor) app.py:"))
    assert approvals.main(["verdict", "7", str(report)]) == 1
    assert capsys.readouterr().out == (
        f"{report}: can't read the finding heading `### C2 (minor) app.py: a line outside the diff`: write it "
        "`### <id> (<blocker|major|minor>) <path>:<line>: <title>`\n"
    )
    assert [call for call in reviewing if call[0] == "review"] == []


# homelab PR #66's verifier pass 1, which `verdict` refused for its `### P1`..`### P4` sub-headings
SUBHEADED = f"""# PR #66 verifier report

Verifier: claude, same-family, pass 1

## Criteria

- The review posts each finding.

## Standards

None.

## Spec

Met.

## Correctness

### P1: root cause on live data and in window_check

Holds.

### P2: side effects of the 1d lookback

None.

### P3: window_check fails on main, passes on the head

Ran both.

### P4: the deploy step

Fine.

### M1 (minor) app.py:11: README says Agent loop spend uses the counters datasource "for every counter"

The README.

### P5: after a finding

Ends M1's body.

### Cron at 02:00 UTC skips the run

### P6: the 1:1 mapping holds

### Repro: `curl localhost:8080/health`

### P7 02:00 run skipped

### Instance on localhost:8000

### Repro on node:20 at 2026-10-21T02:00:00Z

Head: {HEAD}
VERDICT: 0 blocker, 0 major, 1 minor
SATISFIED: yes
Risk: low — a test fixture
"""


def test_a_sub_heading_that_is_no_finding_is_left_out_of_the_review(
    reviewing: list[tuple[Any, ...]], tmp_path: Path
) -> None:
    """Verifiers split Correctness into `###` sub-headings; one that doesn't read as a finding, with no
    `(<severity>)` in it, posts nothing and ends the finding above it, whatever times or ports it names."""
    report = tmp_path / "report.md"
    report.write_text(SUBHEADED)
    assert approvals.main(["verdict", "7", str(report)]) == 0
    heading = "Verifier (claude, same-family), pass 1"
    assert reviewing[-1][2]["comments"] == [
        {
            "path": "app.py",
            "line": 11,
            "side": "RIGHT",
            "body": (
                f"**{heading} · M1** (minor): README says Agent loop spend uses the counters datasource "
                '"for every counter"\n\nThe README.'
            ),
        }
    ]


@pytest.mark.parametrize(
    "broken",
    [
        "### C2 app.py:40: a line outside the diff",
        "### C2 minor app.py:40: a line outside the diff",
        "### C2 [minor] `app.py:40`: a line outside the diff",
        "### C2 [minor] **app.py:40**: a line outside the diff",
        "### C2 (nit) Makefile:40: a line outside the diff",
        "### app.py:40: a line outside the diff",
        "**C2 (minor) app.py:40: a line outside the diff**",
    ],
)
def test_a_finding_verdict_cant_read_leaves_the_counts_short_and_posts_nothing(
    reviewing: list[tuple[Any, ...]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    broken: str,
) -> None:
    """Whatever shape a finding's heading takes, `VERDICT:` still counts it: a report whose finding headings fall
    short of its counts is refused, never posted with that finding dropped."""
    report = tmp_path / "report.md"
    report.write_text(REPORT.replace("### C2 (minor) app.py:40: a line outside the diff", broken))
    assert approvals.main(["verdict", "7", str(report)]) == 1
    assert capsys.readouterr().out == (
        f"{report}: `VERDICT: 1 blocker, 0 major, 2 minor` counts 3 findings, but 2 headings read as one "
        "(1 blocker, 0 major, 1 minor): write each `### <id> (<blocker|major|minor>) <path>:<line>: <title>`\n"
    )
    assert [call for call in reviewing if call[0] == "review"] == []


def test_a_verdict_line_without_all_three_counts_posts_nothing(
    reviewing: list[tuple[Any, ...]], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "report.md"
    report.write_text(
        REPORT.replace("VERDICT: 1 blocker, 0 major, 2 minor", "VERDICT: 1 blocker, 2 minor")
    )
    assert approvals.main(["verdict", "7", str(report)]) == 1
    assert capsys.readouterr().out == (
        f"{report}: write the verdict `VERDICT: <n> blocker, <n> major, <n> minor`\n"
    )
    assert [call for call in reviewing if call[0] == "review"] == []


@pytest.mark.parametrize(
    ("extra", "refused"),
    [
        ("", True),
        ("Not run: gate\n", True),
        ("Not run: login, owner action: sign in to the app\n", False),
    ],
)
def test_an_unsatisfied_report_with_no_finding_is_refused_unless_it_names_what_was_not_run(
    reviewing: list[tuple[Any, ...]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    extra: str,
    refused: bool,
) -> None:
    report = tmp_path / "report.md"
    empty = (
        REPORT.split("### C1")[0] + f"{extra}\nHead: {HEAD}\nVERDICT: 0 blocker, 0 major, 0 minor\n"
    )
    report.write_text(empty + "SATISFIED: no\nRisk: low — a test fixture\n")
    assert approvals.main(["verdict", "7", str(report)]) == int(refused)
    assert ("<report>.question" in capsys.readouterr().out) == refused
    assert bool([call for call in reviewing if call[0] == "review"]) != refused


@pytest.mark.parametrize(
    "extra", ["Not run: acceptance 1, owner action: sign in to the app\n", "Not run: gate\n"]
)
def test_a_satisfied_report_listing_a_run_not_made_is_refused(
    reviewing: list[tuple[Any, ...]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    extra: str,
) -> None:
    report = tmp_path / "report.md"
    empty = (
        REPORT.split("### C1")[0] + f"{extra}\nHead: {HEAD}\nVERDICT: 0 blocker, 0 major, 0 minor\n"
    )
    report.write_text(empty + "SATISFIED: yes\nRisk: low — a test fixture\n")
    assert approvals.main(["verdict", "7", str(report)]) == 1
    assert "<report>.question" in capsys.readouterr().out
    assert not [call for call in reviewing if call[0] == "review"]


def test_the_diffs_commentable_lines_are_the_new_side_of_each_hunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch = "@@ -1,3 +1,4 @@\n one\n-two\n+2\n+2b\n three\n@@ -20 +21,2 @@\n+x\n y"
    files = [{"filename": "a.py", "patch": patch}, {"filename": "logo.png", "patch": None}]
    monkeypatch.setattr(
        approvals.github,
        "gh",
        lambda *args, data=None: "".join(json.dumps(entry) + "\n" for entry in files),
    )
    assert approvals.diff_lines(BASE, HEAD) == {"a.py": [1, 2, 3, 4, 21, 22], "logo.png": []}


# --- the verifier's reviews that let a PR land ---

OLD = "c" * 40


def verified(
    *reviews: dict[str, Any], files: tuple[str, ...] = ("app.py",), **fields: Any
) -> list[str]:
    pr = pull(reviews=reviews, files=files, **fields)
    return approvals.proofs("merge", pr, [approved(1, "area:web")], LOOP)


@pytest.mark.parametrize(
    ("review", "line"),
    [
        (
            verdict_on(HEAD, satisfied="no"),
            f"PR #7: a verifier review on {HEAD} is not satisfied: fix and verify again",
        ),
        (
            verdict_on(HEAD, "1 blocker, 0 major, 0 minor"),
            f"PR #7: the verifier reviews on {HEAD} have 1 blocker and 0 major open: fix and verify again",
        ),
        (
            verdict_on(HEAD, "0 blockers, 2 majors, 0 minor"),
            f"PR #7: the verifier reviews on {HEAD} have 0 blocker and 2 major open: fix and verify again",
        ),
        (
            {**verdict_on(HEAD), "body": "Verifier (claude), pass 1\n\nSATISFIED: yes\n"},
            "PR #7: a verifier review lacks its commit, or a `VERDICT:` or `SATISFIED:` line (approvals.py verdict)",
        ),
        (
            {**verdict_on(HEAD), "commit": None},
            "PR #7: a verifier review lacks its commit, or a `VERDICT:` or `SATISFIED:` line (approvals.py verdict)",
        ),
    ],
)
def test_a_review_unsatisfied_or_with_a_blocker_or_major_open_lets_nothing_land(
    review: dict[str, Any], line: str
) -> None:
    assert verified(review) == [line]
    assert verified(VERDICT, review) == [line]


def test_no_verifier_review_lets_nothing_land() -> None:
    other = {**VERDICT, "body": "Looks fine.\n\nSATISFIED: yes\n"}
    assert verified(other) == ["PR #7: no verifier review posted (approvals.py verdict)"]
    assert verified(verdict_on(HEAD, heading="Verifier (claude, same-family), pass 3")) == []


def test_every_verifier_review_on_the_newest_passs_commit_must_be_satisfied() -> None:
    """A mixed PR's two verifiers review one commit: the second's satisfied review leaves the first's blocker open."""
    blocked = verdict_on(
        HEAD, "1 blocker, 0 major, 0 minor", satisfied="no", heading="Verifier (codex), pass 1"
    )
    assert verified(blocked, VERDICT) == [
        f"PR #7: a verifier review on {HEAD} is not satisfied: fix and verify again",
        f"PR #7: the verifier reviews on {HEAD} have 1 blocker and 0 major open: fix and verify again",
    ]
    # a pass on a later commit supersedes those on an earlier one, whichever family wrote either
    assert verified(verdict_on(OLD, "1 blocker, 0 major, 0 minor", satisfied="no"), VERDICT) == []
    codex = verdict_on(
        OLD, "1 blocker, 0 major, 0 minor", satisfied="no", heading="Verifier (codex), pass 1"
    )
    claude = verdict_on(HEAD, heading="Verifier (claude, same-family), pass 2")
    assert verified(codex, claude) == []


def test_a_later_pass_on_the_same_commit_supersedes_an_earlier_one() -> None:
    """Pass 1's major, triaged as theoretical with no commit after it, is accepted by pass 2 on the same head; a mixed
    PR's two verifiers share a pass number, so both of pass 2's reviews still count."""
    rejected = verdict_on(
        HEAD,
        "0 blocker, 1 major, 0 minor",
        satisfied="no",
        heading="Verifier (claude, same-family), pass 1",
    )
    accepted = verdict_on(HEAD, heading="Verifier (claude, same-family), pass 2")
    assert verified(rejected, accepted) == []
    codex = verdict_on(
        HEAD, "1 blocker, 0 major, 0 minor", satisfied="no", heading="Verifier (codex), pass 2"
    )
    assert verified(rejected, codex, accepted) == [
        f"PR #7: a verifier review on {HEAD} is not satisfied: fix and verify again",
        f"PR #7: the verifier reviews on {HEAD} have 1 blocker and 0 major open: fix and verify again",
    ]


def test_a_review_on_an_older_commit_never_covers_the_head(no_origin: list[list[str]]) -> None:
    """Every commit after a verdict needs one of its own, a merge of main that touches no PR file included."""
    line = (
        f"PR #7: the newest verifier review is on {OLD}, not on the head {HEAD}:"
        " every commit after a verdict needs a verdict of its own"
    )
    assert verified(verdict_on(OLD)) == [line]
    assert verified(verdict_on(OLD), files=("README.md",)) == [line]
    # a comment after the verdict is no verdict
    assert verified(verdict_on(OLD), comments=("Triaged: fixed in the head, lands.",)) == [line]


def test_the_cap_is_skills_default_unless_the_projects_loop_md_sets_its_own() -> None:
    assert approvals.cap(None) == approvals.cap(BARE) == 3  # SKILL.md § The cap
    assert approvals.cap(loop_md("loop:\n  cap: 4\n")) == 4
    with pytest.raises(approvals.Refused, match="`loop.cap` must be a whole number"):
        approvals.cap(loop_md("loop:\n  cap: 3 passes\n"))


def test_only_a_review_by_the_prs_author_or_the_viewer_counts(
    monkeypatch: pytest.MonkeyPatch, writers: list[str]
) -> None:
    """Anyone can review a public repo; a stranger's satisfied review is no verifier review."""
    stranger = verdict_on(HEAD, author="stranger")
    blocked = verdict_on(HEAD, "1 blocker, 0 major, 0 minor", satisfied="no")
    assert verified(stranger) == ["PR #7: no verifier review posted (approvals.py verdict)"]
    assert verified(blocked, stranger) == verified(blocked)
    assert verified(verdict_on(HEAD, author="worker")) == []  # the PR's author
    monkeypatch.setattr(approvals, "viewer", lambda: "coordinator")
    assert verified(verdict_on(HEAD, author="coordinator")) == []
    assert verified(verdict_on(HEAD, author=None)) == [  # a deleted account
        "PR #7: no verifier review posted (approvals.py verdict)"
    ]


def test_a_writers_review_counts_with_an_installation_token(
    monkeypatch: pytest.MonkeyPatch, writers: list[str]
) -> None:
    """A colleague who can write to the repo verifies someone else's PR and posts the verdict as themself: CI, with no
    viewer, counts it; a stranger's still doesn't."""
    monkeypatch.setattr(approvals, "viewer", lambda: None)

    def proofs(review: dict[str, Any]) -> list[str]:
        pr = {
            **pull(reviews=(review,), files=("app.py",)),
            "author": {"__typename": "User", "login": "author"},
        }
        return approvals.proofs("merge", pr, [approved(1, "area:web")], LOOP)

    assert proofs(verdict_on(HEAD, author="worker")) == []
    assert proofs(verdict_on(HEAD, author="stranger")) == [
        "PR #7: no verifier review posted (approvals.py verdict)"
    ]
    assert writers == ["worker", "stranger"]


def test_without_a_user_token_the_prs_author_and_writers_reviews_count(
    monkeypatch: pytest.MonkeyPatch, writers: list[str]
) -> None:
    """An installation token (the approvals workflow's `github.token`) can't read `GET /user`: the gate trusts the
    PR's author and the repo's writers instead of failing every PR with a verifier review."""

    def gh(*args: str, data: str | None = None) -> str:
        assert args[:2] == ("api", "user"), args
        raise subprocess.CalledProcessError(
            1, "gh", stderr="gh: Resource not accessible by integration (HTTP 403)\n"
        )

    monkeypatch.setattr(approvals.github, "gh", gh)
    monkeypatch.setattr(approvals, "viewer", VIEWER)
    assert approvals.viewer() is None
    assert verified(verdict_on(HEAD, author="worker")) == []
    assert verified(verdict_on(HEAD, author="loop")) == [
        "PR #7: no verifier review posted (approvals.py verdict)"
    ]


def test_a_review_requesting_changes_lets_nothing_land() -> None:
    assert verified(VERDICT, latest=("CHANGES_REQUESTED", "APPROVED")) == [
        "PR #7: reviewer requested changes: answer the review"
    ]
    assert verified(VERDICT, latest=("APPROVED", "COMMENTED")) == []


def test_an_unresolved_review_thread_lets_nothing_land() -> None:
    assert verified(VERDICT, threads=(True, False, False)) == [
        "PR #7: 2 review thread(s) unresolved: fix and reply, or triage, then resolve (github-tracker's github.md, Review trail)"
    ]
    assert verified(VERDICT, threads=(True, True)) == []


BOTS = add(LOOP, "bot", 'dependabot[bot]:\n  paths:\n    - uv.lock\n    - "**/package.json"\n')
UNREVIEWED = ["PR #7: no verifier review posted (approvals.py verdict)"]


def authored(login: str | None, committer: str | None = None) -> dict[str, Any]:
    """A commit on the PR as GraphQL's `authored` returns it, by `login` (None: no GitHub account), committed by
    `committer` (None: no account, as GitHub's web-flow committer on a bot's own commits)."""
    return {
        "commit": {
            "author": {"user": {"login": login} if login else None},
            "committer": {"user": {"login": committer} if committer else None},
        }
    }


@pytest.mark.parametrize(
    "login", ["dependabot", "dependabot[bot]"]
)  # GraphQL's spelling, and REST's
def test_an_allow_listed_bots_lock_or_manifest_only_pr_needs_no_verifier_review(login: str) -> None:
    def made(
        files: tuple[str, ...] = ("uv.lock",),
        who: str = login,
        kind: str = "Bot",
        commits: tuple[str | None, ...] = ("dependabot[bot]",),
    ) -> dict[str, Any]:
        pr = {**pull(files=files), "author": {"__typename": kind, "login": who}}
        pr["authored"] = {"nodes": [authored(by) for by in commits]}
        return pr

    def bot(*files: str, loop: str = BOTS, **fields: Any) -> list[str]:
        return approvals.proofs("merge", made(files or ("uv.lock",), **fields), [], loop)

    def gate(pr: dict[str, Any]) -> list[str]:
        return approvals.proofs("merge", pr, [], BOTS)

    assert bot("uv.lock", "web/package.json") == []
    assert bot("uv.lock", "app.py") == UNREVIEWED  # code needs a review as usual
    assert bot(who="renovate") == UNREVIEWED  # a bot no entry names
    assert bot(kind="User") == UNREVIEWED  # a person is never the bot
    assert bot(loop=LOOP) == UNREVIEWED  # no `bot` entry: no exemption
    assert approvals.proofs("merge", made(files=()), [], BOTS) == UNREVIEWED
    assert bot(commits=("dependabot", "dependabot[bot]")) == []
    assert bot(commits=("dependabot[bot]", "worker")) == UNREVIEWED  # a person's push
    assert bot(commits=(None,)) == UNREVIEWED  # an author GitHub ties to no account
    assert bot(commits=()) == UNREVIEWED
    for change in (
        "RENAMED",
        "COPIED",
    ):  # app.py moved or copied onto uv.lock: GraphQL shows only uv.lock
        moved = made()
        moved["files"]["nodes"][0]["changeType"] = change
        assert gate(moved) == UNREVIEWED
    crowded = made(files=("uv.lock",) * 100)
    crowded["files"]["totalCount"] = 101  # past the one page of files the gate reads
    assert gate(crowded) == UNREVIEWED
    long = made(commits=("dependabot[bot]",) * 100)
    long["authored"]["totalCount"] = 101  # past the one page of commits the gate reads
    assert gate(long) == UNREVIEWED
    rebased = made()
    rebased["authored"]["nodes"] = [authored("dependabot[bot]", committer="worker")]
    assert gate(rebased) == UNREVIEWED  # a person's rebase keeps the bot as author
    signed = made()
    signed["authored"]["nodes"] = [authored("dependabot[bot]", committer="dependabot[bot]")]
    assert gate(signed) == []
    assert (
        approvals.settings(BOTS)["risk"] == approvals.settings(LOOP)["risk"]
    )  # a bot entry is no risk rule


def test_a_co_author_trailer_does_not_refuse_the_bot_exemption_but_another_commit_account_does() -> (
    None
):
    """GraphQL's `authors` lists a trailer's account beside the commit's author; only the commit's own `author` and
    committer count, since a trailer is text the committer wrote, not another account's push."""
    pr = {**pull(files=("uv.lock",)), "author": {"__typename": "Bot", "login": "dependabot[bot]"}}
    pr["authored"] = {"nodes": [authored("dependabot[bot]")]}
    pr["authored"]["nodes"][0]["commit"]["authors"] = {
        "totalCount": 2,
        "nodes": [{"user": {"login": "dependabot[bot]"}}, {"user": {"login": "claude"}}],
    }
    assert approvals.exempt(pr, BOTS)
    pr["authored"]["nodes"][0]["commit"]["author"] = {"user": {"login": "claude"}}
    assert not approvals.exempt(pr, BOTS)
    pr["authored"]["nodes"] = [authored("dependabot[bot]", committer="claude")]
    assert not approvals.exempt(pr, BOTS)
    for trailer in (
        False,
        True,
    ):  # an author GitHub ties to no account, with and without a trailer's account
        pr["authored"]["nodes"] = [authored(None)]
        if trailer:
            pr["authored"]["nodes"][0]["commit"]["authors"] = {
                "totalCount": 2,
                "nodes": [{"user": None}, {"user": {"login": "dependabot[bot]"}}],
            }
        assert pr["authored"]["nodes"][0]["commit"]["author"] == {"user": None}
        assert not approvals.exempt(pr, BOTS)


RENOVATE = add(LOOP, "bot", "renovate:\n  branch: renovate/\n  paths:\n    - uv.lock\n")


def test_a_branch_section_exempts_an_updaters_pr_opened_under_a_persons_token() -> None:
    def made(
        branch: str = "renovate/foo",
        commits: tuple[str | None, ...] = ("writer",),
        labels: tuple[str, ...] = (),
        fork: bool = False,
        files: tuple[str, ...] = ("uv.lock",),
        author: str | None = "writer",
    ) -> dict[str, Any]:
        pr = {
            **pull(files=files, labels=labels),
            "author": author and {"__typename": "User", "login": author},
            "headRefName": branch,
            "isCrossRepository": fork,
        }
        pr["authored"] = {"nodes": [authored(by) for by in commits]}
        return pr

    def gate(loop: str = RENOVATE, **fields: Any) -> list[str]:
        return approvals.proofs("merge", made(**fields), [], loop)

    assert gate() == []  # the writer's own renovate/ branch, lock file only
    assert gate(commits=("writer", "other")) == UNREVIEWED  # a second person's commit
    assert gate(labels=("verify",)) == UNREVIEWED  # a major update asks for its review
    assert gate(branch="feature/foo") == UNREVIEWED  # another branch
    assert gate(fork=True) == UNREVIEWED  # a fork may name any branch
    assert gate(files=("uv.lock", "app.py")) == UNREVIEWED  # code needs a review as usual
    assert gate(loop=BOTS) == UNREVIEWED  # a login section never matches a person
    # a deleted author and commits mapped to no account would compare "" with "": refused
    assert gate(author=None, commits=(None,)) == UNREVIEWED


def test_the_verify_label_refuses_a_login_sections_exemption_too() -> None:
    pr = {
        **pull(files=("uv.lock",), labels=("verify",)),
        "author": {"__typename": "Bot", "login": "dependabot"},
    }
    pr["authored"] = {"nodes": [authored("dependabot[bot]")]}
    assert approvals.proofs("merge", pr, [], BOTS) == UNREVIEWED


def test_more_reviews_or_threads_than_one_page_is_refused() -> None:
    crowded = pull(reviews=(VERDICT,), threads=(True,))
    crowded["reviews"]["totalCount"] = 101
    crowded["reviewThreads"]["totalCount"] = 101
    assert approvals.proofs("merge", crowded, [approved(1, "area:web")], LOOP) == [
        "PR #7 has more than 1 reviews: the gate reads one page",
        "PR #7 has more than 1 review threads: the gate reads one page",
    ]


# --- risk rules at the merge ---

GITHUB_RULE = loop_md("risk:\n  paths:\n    - .github/**\n")


def test_a_risk_path_rule_asks_for_the_label_only_on_a_pr_touching_its_path() -> None:
    def waits(loop: str, *files: str, total: int | None = None) -> list[str]:
        pr = pull(reviews=(VERDICT,), files=files)
        if total is not None:
            pr["files"]["totalCount"] = total
        return approvals.waits(pr, [approved(1, "area:web")], loop)

    assert waits(GITHUB_RULE, ".github/ci.yml") == [GITHUB]
    assert waits(GITHUB_RULE, "README.md") == []
    # files the gate can't read
    assert waits(GITHUB_RULE, "README.md", total=101) == [
        HELD
        + "the gate can't read every file the PR changes, so the risk rule's `paths` hold until a"
        " person judges them"
    ]
    assert waits(loop_md("risk:\n  labels:\n    - area:web\n"), "README.md") == [
        HELD + "it matches a risk rule (label `area:web`)"
    ]
    assert waits(loop_md("risk:\n  labels:\n    - kind:ops\n"), "README.md") == []


# --- a checkout against a stubbed GitHub ---


@pytest.fixture
def landing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A checkout with the fixture's tracker and a `risk: paths .github/**` rule, against a stubbed GitHub: the PR (one
    approved issue, a verdict on the head, `check` green), the checks the base's rulesets and branch protection
    require; `calls` holds each `gh pr` command."""
    agents = tmp_path / "docs" / "agents"
    agents.mkdir(parents=True)
    (agents / "issue-tracker.md").write_text(TRACKER)
    (agents / "loop.md").write_text(GITHUB_RULE)
    pr = pull(reviews=(VERDICT,), files=("app.py",))
    pr["closingIssuesReferences"]["nodes"] = [approved(1, "area:web", "kind:bug")]
    state: dict[str, Any] = {
        "pull": pr,
        "rulesets": ["check"],
        "protection": [],
        "calls": [],
    }

    def gh(*args: str, data: str | None = None) -> str:
        if args[0] == "pr":
            state["calls"].append(args)
            return ""
        if args[:2] == ("api", "--paginate") and args[2].endswith("/rules/branches/main"):
            if isinstance(state["rulesets"], Exception):
                raise state["rulesets"]
            return "".join(f"{name}\n" for name in state["rulesets"])
        if args[1] == "repos/{owner}/{repo}/branches/main":
            return "".join(f"{name}\n" for name in state["protection"])
        if args[1].endswith("/comments"):  # flag()'s reason
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(approvals.github, "gh", gh)
    monkeypatch.setattr(approvals, "required_checks", REQUIRED)
    monkeypatch.setattr(approvals, "pull_request", lambda _n: state["pull"])
    monkeypatch.chdir(tmp_path)
    return state


def rules_error(status: int, message: str) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(
        1, ("gh", "api"), stderr=f"gh: {message} (HTTP {status})\n"
    )


FREE_PLAN = "Upgrade to GitHub Pro or make this repository public to enable this feature."


def test_a_free_plan_private_repo_requires_no_checks_and_says_github_enforces_nothing(
    landing: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """GitHub's free plan has no rulesets on a private repo: its 403 reads as no required checks."""
    landing["rulesets"] = rules_error(403, FREE_PLAN)
    assert approvals.required_checks("main") == set()
    assert capsys.readouterr().out.splitlines() == [
        "GitHub enforces no rules on main here (its plan has none): land's proofs are the only gate"
    ]


@pytest.mark.parametrize(
    "error",
    [rules_error(403, "Resource not accessible by integration"), rules_error(404, "Not Found")],
)
def test_any_other_rules_error_fails(
    landing: dict[str, Any], error: subprocess.CalledProcessError
) -> None:
    landing["rulesets"] = error
    with pytest.raises(subprocess.CalledProcessError):
        approvals.required_checks("main")


def test_the_required_checks_are_the_rulesets_and_the_branch_protections(
    landing: dict[str, Any],
) -> None:
    landing.update(rulesets=["check", "loop:approvals"], protection=["qualify"])
    assert approvals.required_checks("main") == {"check", "loop:approvals", "qualify"}
    assert approvals.ci("PR #7", pull(checks=()), set()) == [
        "PR #7: no check on the head commit yet"
    ]
    assert approvals.ci("PR #7", pull(checks=(("lint", "SUCCESS"),)), set()) == []


# --- land: every landable PR in one gated batch, against a bare origin and a GitHub that squashes like GitHub ---

GATE_SCRIPT = """import pathlib, sys
runs = pathlib.Path(sys.argv[1])
runs.open("a").write(pathlib.Path.cwd().name + "\\n")
flaky = runs.with_suffix(".flaky")
if pathlib.Path("flaky.txt").exists() and len(flaky.read_text() if flaky.exists() else "") < 2:
    flaky.open("a").write("x")
    sys.exit(1)
if pathlib.Path("dirty.txt").exists():
    pathlib.Path("app.py").write_text("changed\\n")
faults = runs.with_suffix(".faults")
if pathlib.Path("machine.txt").exists() and len(faults.read_text() if faults.exists() else "") < int(
    pathlib.Path("machine.txt").read_text()
):
    faults.open("a").write("x")
    pathlib.Path("leftover.txt").write_text("")
    print("the screen is locked")
    sys.exit(78)
sys.exit(1 if pathlib.Path("red.txt").exists() else 0)
"""


def checks_if_changed(args: tuple[str, ...], pulls: dict[int, dict[str, Any]]) -> str:
    """GitHub's answer to rest_if_changed's `gh api -i [-H If-None-Match: <etag>] repos/.../commits/<sha>/check-runs`
    or `.../status` read (#1915): the check runs or statuses of the PR whose head is `<sha>`, as REST names them,
    from its statusCheckRollup, so a wait between rounds sees the same checks a round does. An ETag that still
    matches gets a 304, the way gh reports one where it exits 1 (test_land_wait_budget's Hub)."""
    found = re.fullmatch(
        r"repos/\{owner\}/\{repo\}/commits/([0-9a-f]+)/(check-runs|status)\?.*", args[-1]
    )
    assert found, args
    sha, kind = found.groups()
    contexts = [
        context
        for pr in pulls.values()
        if pr["headRefOid"] == sha
        for context in pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"]
    ]
    if kind == "check-runs":
        runs = [
            {
                "name": context["name"],
                "status": (context.get("status") or "COMPLETED").lower(),
                "conclusion": (context.get("conclusion") or "").lower() or None,
            }
            for context in contexts
            if context.get("__typename") == "CheckRun"
        ]
        body = json.dumps({"total_count": len(runs), "check_runs": runs})
    else:
        statuses = [
            {"context": context["context"], "state": context["state"].lower()}
            for context in contexts
            if context.get("__typename") == "StatusContext"
        ]
        body = json.dumps({"state": "pending", "statuses": statuses})
    etag = f'"{hashlib.sha1(body.encode()).hexdigest()}"'
    if f"If-None-Match: {etag}" in args:
        raise subprocess.CalledProcessError(
            1, ("gh", *args), f"HTTP/2.0 304 Not Modified\nEtag: {etag}\n\n", "gh: HTTP 304"
        )
    return f"HTTP/2.0 200 OK\nEtag: {etag}\n\n{body}"


@pytest.fixture
def lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> dict[str, Any]:
    """`land` in a clone of a bare `origin` holding `main`, with the fixture's tracker and a `.github/**` risk rule,
    against a fake GitHub. `open(number, files)` pushes a PR off main writing those files (its branch and
    `refs/pull/<n>/head`), with one approved issue and a satisfied verdict on its head; `gh pr merge` squashes the head
    onto origin's main as GitHub does. The gate fails on a tree holding `red.txt`, and twice on one holding
    `flaky.txt`, and changes `app.py` on one holding `dirty.txt`; on one holding `machine.txt`, it exits 78 the number of
    times the file says, leaving `leftover.txt`; `runs()` counts its runs. `land(*argv)` runs it: (exit, lines)."""
    # a landed PR's worker sessions are Omnigent's, which no land here may reach (test_approvals_keep_pr_worker.py)
    monkeypatch.setattr(approvals, "archive_workers", lambda _number: None)
    # its tests rewrite the policy in the work tree after opening PRs, so it is read there, as in a checkout without
    # origin (test_approvals_policy_base.py tests the read from origin/<base>)
    monkeypatch.setenv(approvals.WORKING_TREE_POLICY, "1")
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    git_in(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    (origin / "hooks" / "pre-receive").write_text(NO_REWRITE)
    (origin / "hooks" / "pre-receive").chmod(0o755)
    committed(work)
    git_in(work, "remote", "add", "origin", str(origin))
    git_in(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    git_in(work, "fetch", "-q", "origin")
    agents = work / "docs" / "agents"
    agents.mkdir(parents=True)
    (agents / "issue-tracker.md").write_text(TRACKER)
    (agents / "loop.md").write_text(GITHUB_RULE)
    (tmp_path / "gate.py").write_text(GATE_SCRIPT)
    runs = tmp_path / "runs.log"
    monkeypatch.setattr(
        approvals, "LOCAL_CI", [sys.executable, str(tmp_path / "gate.py"), str(runs)]
    )
    state: dict[str, Any] = {
        "origin": origin,
        "work": work,
        "pulls": {},
        "calls": [],
        "comments": [],
    }
    pulls = state["pulls"]

    def open_pr(
        number: int, files: dict[str, str], messages: tuple[str, ...] = (), **fields: Any
    ) -> str:
        git_in(work, "switch", "-q", "--detach", "origin/main")
        for name, text in files.items():
            (work / name).parent.mkdir(parents=True, exist_ok=True)
            (work / name).write_text(text)
        git_in(work, "add", *files)
        for message in messages or (f"PR {number}",):
            git_in(work, "commit", "-q", "--allow-empty", "-m", message)
        head = git_in(work, "rev-parse", "HEAD").strip()
        git_in(
            work,
            "push",
            "-q",
            "origin",
            f"{head}:refs/heads/pr{number}",
            f"{head}:refs/pull/{number}/head",
        )
        pr = pull(**{"reviews": (verdict_on(head),), **fields})
        pr.update(number=number, title=f"PR {number}", headRefOid=head, headRefName=f"pr{number}")
        pr["closingIssuesReferences"]["nodes"] = [approved(100 + number, "area:web", "kind:bug")]
        pulls[number] = pr
        return head

    def gh(*args: str, data: str | None = None) -> str:
        if args[:2] == ("repo", "view"):
            return "main\n"
        if args[:2] == ("pr", "ready"):
            state["calls"].append(args)
            pulls[int(args[-1])]["isDraft"] = "--undo" in args
            return ""
        if args[:2] == ("api", "-X"):
            # an edit of a PR's body or comment at its REST path, which land no longer makes
            state["calls"].append(args)
            state.setdefault("edits", {})[args[3]] = args[args.index("-f") + 1].removeprefix(
                "body="
            )
            return ""
        if args[:2] == ("pr", "merge"):
            state["calls"].append(args)
            number, head = int(args[2]), args[args.index("--match-head-commit") + 1]
            body = args[args.index("--body") + 1] if "--body" in args else "GitHub's default body"
            state.setdefault("bodies", {})[number] = body
            assert pulls[number]["headRefOid"] == head
            tip = git_in(origin, "rev-parse", "main").strip()
            tree = git_in(origin, "merge-tree", "--write-tree", tip, head).split()[0]
            squash = git_in(
                origin,
                "-c",
                "user.name=GitHub",
                "-c",
                "user.email=gh@example.com",
                "commit-tree",
                tree,
                "-p",
                tip,
                "-m",
                f"PR {number} (#{number})",
                "-m",
                body,
            ).strip()
            git_in(origin, "update-ref", "refs/heads/main", squash, tip)
            pulls[number]["state"] = "MERGED"
            state.setdefault("squashes", {})[number] = squash
            return ""
        if args[0] == "api" and re.fullmatch(r"repos/\{owner\}/\{repo\}/pulls/\d+", args[1]):
            # the PR as REST reads it, which land re-reads after a failed merge call (#923)
            number = int(args[1].rsplit("/", 1)[1])
            return json.dumps(
                {
                    "merged": pulls[number]["state"] == "MERGED",
                    "head": {"sha": pulls[number]["headRefOid"]},
                    "merge_commit_sha": state.get("squashes", {}).get(number),
                }
            )
        if args[:2] == ("api", "rate_limit"):  # land --wait's poll_seconds: plenty of quota
            return "5000 5000 0"
        if args[:2] == ("api", "-i"):
            # land --wait's ETag read of a head's checks between rounds (#1915)
            return checks_if_changed(args, pulls)
        raise AssertionError(args)

    def comment(number: int, body: str) -> None:
        state["comments"].append((number, approvals.machine(body)))
        state.setdefault("raw", []).append((number, body))
        pulls[number]["comments"]["nodes"].append(note(body))

    def listed() -> list[dict[str, Any]]:
        return [
            {"number": n, "baseRefName": pr["baseRefName"], "isDraft": pr["isDraft"]}
            for n, pr in pulls.items()
            if pr["state"] == "OPEN"
        ]

    monkeypatch.setattr(approvals.github, "gh", gh)
    monkeypatch.setattr(approvals, "comment", comment)
    monkeypatch.setattr(approvals, "open_pulls", listed)
    monkeypatch.setattr(approvals, "pull_request", lambda n: pulls[n])
    monkeypatch.setattr(approvals, "one_repo", lambda: None)
    monkeypatch.chdir(work)

    def land(*argv: str) -> tuple[int, list[str]]:
        code = approvals.main(["land", *argv])
        return code, capsys.readouterr().out.splitlines()

    state.update(
        open=open_pr,
        land=land,
        runs=lambda: len(runs.read_text().splitlines()) if runs.exists() else 0,
        merged=lambda: [args[2] for args in state["calls"] if args[:2] == ("pr", "merge")],
        main=lambda what="": git_in(origin, "rev-parse", f"main{what}").strip(),
        lock=lambda: held_tip(origin),
        tips=lambda: (
            git_in(origin, "log", "--format=%H %P%x00%s", REF).splitlines()
            if git_in(origin, "for-each-ref", REF)
            else []
        ),
    )
    return state


# the landing lock's branch on main, and what land's messages call it
REF, NAME = approvals.LOCK.format("main"), "loop-landing/main"
# origin's pre-receive hook: it refuses a push to a landing lock ref that deletes it or is no fast-forward, so a land
# that ever pushed either fails, as a cloud session's proxy refuses it
NO_REWRITE = """#!/bin/sh
zero=0000000000000000000000000000000000000000
while read old new ref; do
  case "$ref" in refs/heads/loop-landing/*|refs/loop/*) ;; *) continue ;; esac
  if [ "$new" = "$zero" ]; then echo "refused: delete of $ref" >&2; exit 1; fi
  if [ "$old" != "$zero" ] && ! git merge-base --is-ancestor "$old" "$new"; then
    echo "refused: non-fast-forward of $ref" >&2; exit 1
  fi
done
"""


def held_tip(origin: Path) -> str:
    """The lock branch's tip while it is held, else "" (missing, or its tip `free`)."""
    tip = git_in(origin, "for-each-ref", "--format=%(objectname) %(subject)", REF).strip()
    return tip.split()[0] if f" {approvals.HELD}" in tip else ""


def landed_note(lands: dict[str, Any], number: int) -> dict[str, str]:
    [body] = [body for n, body in lands["comments"] if n == number and body.startswith("Landed\n")]
    return dict(line.split(": ", 1) for line in body.splitlines()[1:])


def test_land_squashes_with_githubs_default_body_a_session_line_included(
    lands: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    """land passes no `--body`, so GitHub's squash default writes it, a cloud session's link line kept."""
    link = "Claude-Session: https://claude.ai/code/session_01AbCdEf"
    lands["open"](7, {"7.txt": "7\n"}, messages=(f"Add 7\n\nWhy 7.\n\n{link}",))
    code, lines = lands["land"]()
    assert code == 0, lines
    merges = [args for args in lands["calls"] if args[:2] == ("pr", "merge")]
    assert merges and all("--body" not in args for args in merges)
    assert lands["bodies"] == {7: "GitHub's default body"}


def test_land_merges_three_verified_prs_with_no_gate_run(
    lands: dict[str, Any], hub: dict[str, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1872: with no merge queue, land merges every PR whose verifier record is satisfied, in one batch, and runs
    no gate command: not the batch's, not the new tip's, and it publishes no pass on main."""
    monkeypatch.setattr(approvals, "run_steps", lambda *_a, **_k: pytest.fail("land ran the gate"))
    for number in (9, 7, 8):
        lands["open"](number, {f"{number}.txt": f"{number}\n"})
    code, lines = lands["land"]()
    assert code == 0, lines
    assert (lands["runs"](), lands["merged"]()) == (0, ["7", "8", "9"])
    # each PR's tree is the base with it and those before it merged
    for number, back in ((7, "~2"), (8, "~1"), (9, "")):
        assert landed_note(lands, number) == {
            "Base": lands["main"](),
            "Tree": lands["main"](f"{back}^{{tree}}"),
            "Gate": approvals.NO_GATE,
            "Batch": "#7, #8, #9",
        }
    assert [s for s in hub.get(lands["main"](), []) if s["context"] == approvals.PUBLISHED] == []
    assert lands["lock"]() == ""
    assert NAME not in git_in(lands["work"], "worktree", "list")


HEAD_PROOF = "head_proof:\n  status: gate-ui\n  env:\n    - GATE_UI_TESTS\n"


def test_with_head_proof_land_refuses_a_head_without_its_status_and_lands_it_once_there(
    lands: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    """The refusal names the command that records the proof; a status on an older head, or naming another tree,
    proves nothing about this one."""
    (lands["work"] / "docs" / "agents" / "loop.md").write_text(add(GITHUB_RULE, "loop", HEAD_PROOF))
    older = lands["open"](7, {"7.txt": "7\n"})
    hub[older] = [
        status("gate-ui", f"Tree {approvals.tree_of(older)} on {older[:12]}: GATE_UI_TESTS=smoke")
    ]
    work = lands["work"]
    (work / "7.txt").write_text("seven\n")
    git_in(work, "commit", "-qam", "PR 7, again")
    head = git_in(work, "rev-parse", "HEAD").strip()
    git_in(work, "push", "-q", "origin", f"{head}:refs/heads/pr7", f"{head}:refs/pull/7/head")
    lands["pulls"][7].update(headRefOid=head, reviews={"nodes": [verdict_on(head)]})
    hub[head] = [
        status("gate-ui", f"Tree {approvals.tree_of(older)} on {head[:12]}: GATE_UI_TESTS=smoke")
    ]
    code, lines = lands["land"]("7")
    assert (code, lands["runs"](), lands["merged"]()) == (1, 0, [])
    assert lines == [
        (
            f"PR #7: its head {head} has no green `gate-ui` status for its tree (loop.md `head_proof`): run"
            " `approvals.py local-ci --record --head-proof` on a clean checkout of it, with the tier set in"
            " GATE_UI_TESTS"
        )
    ]
    hub[head].insert(
        0, status("gate-ui", f"Tree {approvals.tree_of(head)} on {head[:12]}: GATE_UI_TESTS=smoke")
    )
    assert lands["land"]("7")[0] == 0
    assert lands["merged"]() == ["7"]


def lock_commit(lands: dict[str, Any], seconds_ago: int) -> str:
    """Another land's `held` lock commit, taken `seconds_ago`, pushed onto the lock branch's tip as a fast-forward."""
    when = f"@{int(datetime.now(UTC).timestamp()) - seconds_ago} +0000"
    tip = git_in(lands["origin"], "for-each-ref", "--format=%(objectname)", REF).strip()
    tree = git_in(lands["work"], "hash-object", "-t", "tree", "-w", os.devnull).strip()
    held = subprocess.run(
        [
            "git",
            "commit-tree",
            *(["-p", tip] if tip else []),
            "-m",
            f"{approvals.HELD} [skip ci]\n\npid 1, nonce 00",
            tree,
        ],
        cwd=lands["work"],
        env={**os.environ, "GIT_COMMITTER_DATE": when},
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    git_in(lands["work"], "push", "-q", "origin", f"{held}:{REF}")
    return held


def test_a_held_lock_waits_naming_its_age_and_a_stale_one_is_broken(lands: dict[str, Any]) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    held = lock_commit(lands, 10 * 60)
    code, lines = lands["land"]("7")
    assert code == approvals.WAITING
    assert lines[0].startswith(
        f"another land holds the landing lock {NAME} (pid 1), taken 10 minutes ago ("
    )
    assert (lands["lock"](), lands["merged"]()) == (held, [])
    lock_commit(lands, 3 * 60 * 60)
    code, lines = lands["land"]("7")
    assert (code, lines[0]) == (
        0,
        f"the landing lock {NAME} is 3 hours old, past 2 hours: broken as stale",
    )
    assert (lands["lock"](), lands["merged"]()) == ("", ["7"])


def test_a_low_risk_pr_carrying_approved_merge_is_a_candidate_like_any_other(
    lands: dict[str, Any],
) -> None:
    lands["open"](7, {"7.txt": "7\n"}, labels=OWNED)
    assert lands["land"]("7")[0] == 0
    assert lands["merged"]() == ["7"]


def test_a_pr_with_no_loop_trail_is_never_a_candidate_under_opt_out(lands: dict[str, Any]) -> None:
    (lands["work"] / "docs" / "agents" / "loop.md").write_text(
        add(GITHUB_RULE, "loop", "opt_out: true\n")
    )
    lands["open"](7, {"7.txt": "7\n"}, reviews=())
    lands["pulls"][7]["closingIssuesReferences"]["nodes"] = [specced(1, "kind:bug")]
    lands["open"](8, {"8.txt": "8\n"})
    code, lines = lands["land"]()
    assert code == 0 and OUTSIDE in lines
    assert lands["merged"]() == ["8"]
    assert lands["land"]("7") == (1, [OUTSIDE])


def test_a_conflicting_pr_is_skipped_with_one_comment_per_head(lands: dict[str, Any]) -> None:
    lands["open"](7, {"app.py": "value = 7\n"})
    head = lands["open"](8, {"app.py": "value = 8\n"})
    code, lines = lands["land"]()
    assert code == 1
    assert "PR #8 conflicts with main plus #7 merged before it: skipped" in lines
    assert lands["merged"]() == ["7"]
    assert lands["land"]()[0] == 1  # alone on the new main it still conflicts, and says it no more
    [(number, body)] = [
        (n, body) for n, body in lands["raw"] if body.startswith(approvals.CONFLICTS)
    ]
    assert (number, approvals.machine(body).splitlines()[1]) == (8, f"Head: {head}")
    assert "It conflicts with main plus #7 merged before it: merge origin/main in" in body


CHECK_SCRIPT = """import os, pathlib, subprocess, sys
pathlib.Path(sys.argv[1]).open("a").write(f"{os.environ['LAND_HEAD']} {os.environ['LAND_BASE']}\\n")
files = subprocess.run(["git", "ls-tree", "--name-only", os.environ["LAND_HEAD"]], capture_output=True, text=True)
pathlib.Path("check.cache").write_text("left behind\\n")
if os.environ["STALE"] in files.stdout.split():
    print("forked before the restructuring: old.txt is back")
    sys.exit(1)
"""


def pre_land(lands: dict[str, Any], configured: bool = True) -> Path:
    """main holds `check.py`, which refuses a head whose tree holds the file `STALE` names and leaves a file behind,
    and loop.md's `pre_land` runs it with `STALE=old.txt`, unless not `configured`; the log of its runs, one
    `<LAND_HEAD> <LAND_BASE>` line each."""
    work, runs = lands["work"], lands["work"].parent / "pre_land.log"
    git_in(work, "switch", "-q", "--detach", "origin/main")
    (work / "check.py").write_text(CHECK_SCRIPT)
    git_in(work, "add", "check.py")
    git_in(work, "commit", "-qm", "the pre-land check")
    git_in(work, "push", "-q", "origin", "HEAD:main")
    git_in(work, "fetch", "-q", "origin")
    if configured:
        (work / "docs" / "agents" / "loop.md").write_text(
            add(
                GITHUB_RULE,
                "loop",
                f"pre_land: {shlex.join(['STALE=old.txt', sys.executable, 'check.py', str(runs)])}\n",
            )
        )
    return runs


def test_a_pr_the_pre_land_check_refuses_is_left_out_with_one_comment_and_stays_ready(
    lands: dict[str, Any],
) -> None:
    runs = pre_land(lands)
    base = lands["main"]()
    fine = lands["open"](7, {"7.txt": "7\n"})
    old = lands["open"](8, {"old.txt": "old\n"})
    # the heads are on origin only, as a PR's are, so the check reads one only once land fetched it
    work = lands["work"]
    git_in(work, "switch", "-q", "--detach", "origin/main")
    for ref in ("pr7", "pr8"):  # the push's remote-tracking refs
        git_in(work, "update-ref", "-d", f"refs/remotes/origin/{ref}")
    git_in(work, "reflog", "expire", "--expire=now", "--all")
    git_in(work, "gc", "-q", "--prune=now")
    code, lines = lands["land"]()
    assert code == 1, lines
    # no gate runs, and what the check left in the worktree never reaches the batch
    assert (lands["runs"](), lands["merged"]()) == (0, ["7"])
    assert runs.read_text().splitlines() == [f"{fine} {base}", f"{old} {base}"]
    command = shlex.join(["STALE=old.txt", sys.executable, "check.py", str(runs)])
    assert (
        f"PR #8: `{command}` refused it against main at {base}, exiting 1: left out of this batch"
        in lines
    )
    assert landed_note(lands, 7)["Batch"] == "#7"
    # not red: neither evicted nor back to draft, and the same head refused again says it no more
    assert lands["land"]()[0] == 1
    assert not lands["pulls"][8]["isDraft"]
    assert not [call for call in lands["calls"] if call[:2] == ("pr", "ready")]
    [(number, body)] = [
        (n, body) for n, body in lands["comments"] if body.startswith(approvals.PRE_LAND)
    ]
    assert (number, body.splitlines()[1]) == (8, f"Head: {old}")
    assert "forked before the restructuring: old.txt is back\n" in body
    assert lands["land"]("8")[0] == 1
    # a new head refused is said again
    said = lands["pulls"][8]["comments"]
    for ref in ("refs/heads/pr8", "refs/pull/8/head"):  # a force-push
        git_in(lands["origin"], "update-ref", "-d", ref)
    newer = lands["open"](8, {"old.txt": "older\n"})
    lands["pulls"][8]["comments"] = said
    assert lands["land"]("8")[0] == 1
    assert [
        body.splitlines()[1]
        for n, body in lands["comments"]
        if n == 8 and body.startswith(approvals.PRE_LAND)
    ] == [f"Head: {old}", f"Head: {newer}"]


def test_a_pr_editing_the_pre_land_check_is_judged_by_the_bases_version(
    lands: dict[str, Any],
) -> None:
    runs = pre_land(lands)
    lands["open"](7, {"old.txt": "old\n", "check.py": "import sys\nsys.exit(0)\n"})
    assert lands["land"]()[0] == 1
    assert lands["merged"]() == []
    assert len(runs.read_text().splitlines()) == 1


def test_without_pre_land_no_check_runs(lands: dict[str, Any]) -> None:
    runs = pre_land(lands, configured=False)
    lands["open"](7, {"old.txt": "old\n"})
    assert lands["land"]()[0] == 0
    assert lands["merged"]() == ["7"]
    assert not runs.exists()


def test_check_merge_reports_what_the_pre_land_check_refuses(
    lands: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """#2063: check merge runs land's `pre_land` (pre_land_run) once nothing else holds the merge, so it never calls
    clear a PR land leaves out; a refusal is a wait, as land keeps the PR ready, and it posts no comment."""
    runs = pre_land(lands)
    base = lands["main"]()
    fine = lands["open"](7, {"7.txt": "7\n"})
    old = lands["open"](8, {"old.txt": "old\n"})
    command = shlex.join(["STALE=old.txt", sys.executable, "check.py", str(runs)])
    assert approvals.main(["check", "merge", "7"]) == 0
    capsys.readouterr()
    assert approvals.main(["check", "merge", "8"]) == approvals.WAITING
    lines = capsys.readouterr().out.splitlines()
    at = lines.index(
        f"PR #8: `{command}` refuses it against main at {base}, exiting 1: land leaves it out"
    )
    assert lines[at + 1] == "  forked before the restructuring: old.txt is back"
    assert runs.read_text().splitlines() == [f"{fine} {base}", f"{old} {base}"]
    assert not [body for _, body in lands["comments"] if body.startswith(approvals.PRE_LAND)]
    # land agrees: it leaves out the PR check merge held
    assert lands["land"]()[0] == 1
    assert lands["merged"]() == ["7"]


def test_check_merge_says_pre_land_has_not_run_while_something_else_holds(
    lands: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    runs = pre_land(lands)
    lands["open"](8, {"old.txt": "old\n"})
    lands["pulls"][8]["reviews"]["nodes"] = []  # no verdict: a proof is missing
    command = shlex.join(["STALE=old.txt", sys.executable, "check.py", str(runs)])
    assert approvals.main(["check", "merge", "8"]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert (
        f"PR #8: pre_land not run: `{command}` (land runs it once nothing else holds the merge)"
        in lines
    )
    assert not runs.exists()


def test_check_merge_without_a_working_tree_says_pre_land_is_not_run(
    lands: dict[str, Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = pre_land(lands)
    lands["open"](8, {"old.txt": "old\n"})
    monkeypatch.setattr(approvals, "policy_ref", lambda _root: "refs/remotes/origin/main")
    assert approvals.main(["check", "merge", "8"]) == approvals.WAITING
    lines = capsys.readouterr().out.splitlines()
    assert any(
        line.startswith("PR #8: pre_land not run:") and "no working tree" in line for line in lines
    ), lines
    assert not runs.exists()


def configure_pre_land(lands: dict[str, Any], command: list[str]) -> None:
    (lands["work"] / "docs" / "agents" / "loop.md").write_text(
        add(GITHUB_RULE, "loop", f"pre_land: {shlex.join(command)}\n")
    )


def test_a_pre_land_refusal_too_long_for_a_comment_posts_its_end(lands: dict[str, Any]) -> None:
    configure_pre_land(
        lands,
        [sys.executable, "-c", "print('x' * 70000); print('the reason'); raise SystemExit(1)"],
    )
    lands["open"](7, {"7.txt": "7\n"})
    assert lands["land"]()[0] == 1
    [body] = [body for _, body in lands["comments"] if body.startswith(approvals.PRE_LAND)]
    assert "x\nthe reason\n````\n" in body
    assert len(body) <= 65536


def test_a_pre_land_whose_command_is_not_there_is_refused_naming_it(
    lands: dict[str, Any],
) -> None:
    configure_pre_land(lands, ["misee", "run", "pre-land"])
    lands["open"](7, {"7.txt": "7\n"})
    code, lines = lands["land"]()
    assert code == 1
    assert any("can't run: [Errno 2] No such file or directory: 'misee'" in line for line in lines)


def test_a_head_missing_from_origin_is_not_judged_by_pre_land(lands: dict[str, Any]) -> None:
    runs = pre_land(lands)
    head = lands["open"](7, {"7.txt": "7\n"})
    work = lands["work"]
    git_in(work, "switch", "-q", "--detach", "origin/main")
    for ref in ("refs/heads/pr7", "refs/pull/7/head"):
        git_in(lands["origin"], "update-ref", "-d", ref)
    git_in(work, "update-ref", "-d", "refs/remotes/origin/pr7")
    for repo in (work, lands["origin"]):
        git_in(repo, "reflog", "expire", "--expire=now", "--all")
        git_in(repo, "gc", "-q", "--prune=now")
    code, lines = lands["land"]()
    assert code == 1
    assert f"PR #7: its head {head} is not on origin: push it, then land again" in lines
    assert not runs.exists()
    assert not [body for _, body in lands["comments"] if body.startswith(approvals.PRE_LAND)]


def test_a_pre_land_with_a_shell_operator_is_refused() -> None:
    with pytest.raises(
        approvals.Refused, match="loop.md's `pre_land` is 'a && b': variables to set"
    ):
        approvals.command_line("a && b", "pre_land")


def test_land_marks_a_named_draft_ready_and_waits_for_the_required_checks_it_starts(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals, "required_checks", lambda _base: {"check", approvals.APPROVALS})
    lands["open"](7, {"7.txt": "7\n"})
    lands["pulls"][7]["isDraft"] = True
    assert lands["land"]() == (0, [])  # no PR named: a draft is no candidate
    code, lines = lands["land"]("7")
    assert (code, lines) == (
        approvals.WAITING,
        ["PR #7 is ready now", "PR #7: CI starts on ready, so its required checks are pending"],
    )
    # ready, `check` green and `loop:approvals` not waited for: it lands, with no gate run
    assert lands["land"]("7")[0] == 0
    assert (lands["runs"](), lands["merged"]()) == (0, ["7"])


def test_the_lock_is_taken_broken_and_released_past_a_pre_push_hook_that_fails(
    lands: dict[str, Any],
) -> None:
    # the lock commit holds the base's own tree, so a project's pre-push checks have nothing of it to check
    lands["open"](7, {"7.txt": "7\n"})
    lock_commit(lands, 3 * 60 * 60)
    hook = lands["work"] / ".git/hooks/pre-push"
    hook.write_text("#!/bin/sh\necho the hook ran >&2\nexit 1\n")
    hook.chmod(0o755)
    code, lines = lands["land"]("7")
    assert code == 0, lines
    assert (lands["lock"](), lands["merged"]()) == ("", ["7"])


def test_the_lock_is_released_when_land_fails(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    lands["open"](7, {"7.txt": "7\n"})

    def broken(*_args: Any) -> Any:
        raise RuntimeError("GitHub went away")

    monkeypatch.setattr(approvals, "merges", broken)
    with pytest.raises(RuntimeError):
        lands["land"]("7")
    assert lands["lock"]() == ""
    assert git_in(lands["work"], "worktree", "list").count("\n") == 1


def test_a_pr_missing_a_proof_is_never_readied_nor_merged(lands: dict[str, Any]) -> None:
    lands["open"](7, {"7.txt": "7\n"}, reviews=())
    lands["pulls"][7]["isDraft"] = True
    assert lands["land"]("7")[0] == 1
    assert (lands["calls"], lands["runs"]()) == ([], 0)


def test_a_high_risk_pr_without_the_owners_go_waits_and_never_merges(
    lands: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    lands["open"](7, {".github/ci.yml": "on: push\n"})
    lands["pulls"][7]["files"] = {"nodes": [{"path": ".github/ci.yml"}]}
    lands["open"](8, {"8.txt": "8\n"})
    code, lines = lands["land"]("7")
    assert code == approvals.WAITING
    assert any(line.startswith("PR #7 waits for the owner's go on its head") for line in lines)
    assert lands["merged"]() == ["8"]


def test_a_pr_whose_required_check_is_red_is_no_candidate(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals, "required_checks", lambda _base: {"check"})
    lands["open"](7, {"7.txt": "7\n"}, checks=(("check", "FAILURE"),))
    lands["open"](8, {"8.txt": "8\n"})
    assert lands["land"]("7")[0] == 1
    assert lands["merged"]() == ["8"]


def test_two_lands_from_one_free_tip_in_the_same_second_one_wins_and_the_other_waits(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # one identity and one commit second: the lock commit's nonce alone tells the two lands' commits apart, else the
    # loser's push of the same commit would succeed as a no-op
    lands["open"](7, {"7.txt": "7\n"})
    earlier: list[str] = []
    assert approvals.locked("main", earlier)
    assert approvals.unlock("main", earlier[-1]).returncode == 0
    free = git_in(lands["origin"], "rev-parse", REF).strip()
    now = f"@{int(datetime.now(UTC).timestamp())} +0000"
    monkeypatch.setenv("GIT_COMMITTER_DATE", now)
    monkeypatch.setenv("GIT_AUTHOR_DATE", now)
    winner: list[str] = []
    real = approvals.git

    def git(*args: str) -> str:
        out = real(*args)
        if (
            args[:1] == ("log",) and not winner
        ):  # this land has read the free tip; another takes it first
            winner.append("")
            assert approvals.locked("main", winner)
        return out

    monkeypatch.setattr(approvals, "git", git)
    code, lines = lands["land"]("7")
    assert (code, lands["merged"]()) == (approvals.WAITING, [])
    assert lands["lock"]() == winner[-1]
    assert git_in(lands["origin"], "rev-parse", f"{winner[-1]}^").strip() == free
    assert lines[-1].startswith(
        f"another land holds the landing lock {NAME} (pid {os.getpid()}, beats), taken 0 minutes ago ("
    )


def test_a_land_that_lost_one_race_and_took_the_lock_on_its_retry_releases_that_take(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # another land takes and releases the lock between this one's read and its push: its retry from the new free tip
    # wins, and its release is of that commit, not of the first one origin never took
    lands["open"](7, {"7.txt": "7\n"})
    other: list[str] = []
    real = approvals.git

    def git(*args: str) -> str:
        out = real(*args)
        if args[:2] == ("rev-parse", f"refs/remotes/origin/{NAME}") and not other:
            other.append("")
            assert approvals.locked("main", other)
            assert approvals.unlock("main", other[-1]).returncode == 0
        return out

    tip = lock_commit(lands, 60)
    git_in(
        lands["work"], "push", "-q", "origin", f"{approvals.lock_commit(approvals.FREE, tip)}:{REF}"
    )
    monkeypatch.setattr(approvals, "git", git)
    assert lands["land"]("7")[0] == 0
    assert (lands["lock"](), lands["merged"]()) == ("", ["7"])
    assert (
        len(lands["tips"]()) == 7
    )  # theirs taken and released, then ours, moved to name its PR before merging


def test_the_batch_starts_from_the_base_as_origin_has_it_when_the_lock_is_taken(
    lands: dict[str, Any],
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    # main moves on origin, to a tree of its own, after this checkout last fetched it
    work, tip = lands["work"], lands["main"]()
    (work / "elsewhere.txt").write_text("elsewhere\n")
    git_in(work, "add", "elsewhere.txt")
    git_in(work, "commit", "-qm", "elsewhere")
    git_in(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    moved = git_in(work, "rev-parse", "HEAD").strip()
    git_in(work, "update-ref", "refs/remotes/origin/main", tip)
    assert lands["land"]("7")[0] == 0
    # the batch is made from the moved base, and no gate runs on it
    assert (lands["runs"](), lands["main"]("~1")) == (0, moved)


def test_a_release_moves_the_lock_to_a_free_child_of_ours_and_its_history_only_grows(
    lands: dict[str, Any],
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    assert lands["land"]("7")[0] == 0
    first = git_in(lands["origin"], "rev-parse", REF).strip()
    lands["open"](8, {"8.txt": "8\n"})
    assert lands["land"]("8")[0] == 0
    second = git_in(lands["origin"], "rev-parse", REF).strip()
    git_in(lands["origin"], "merge-base", "--is-ancestor", first, second)
    empty = git_in(lands["origin"], "hash-object", "-t", "tree", os.devnull).strip()
    assert git_in(lands["origin"], "log", "--format=%T", REF).split() == [empty] * 6
    tips = [line.split("\0") for line in lands["tips"]()]
    # each land takes the lock, moves it to name the last PR it merges (#901), then frees it
    assert [subject for _, subject in tips] == [
        f"{approvals.FREE} [skip ci]",
        f"{approvals.HELD} [skip ci]",
        f"{approvals.HELD} [skip ci]",
        f"{approvals.FREE} [skip ci]",
        f"{approvals.HELD} [skip ci]",
        f"{approvals.HELD} [skip ci]",
    ]
    # each commit's parent is the one below it, and the first lock commit has none
    commits = [ids.split() for ids, _ in tips]
    assert [rest for _, *rest in commits] == [[c[0]] for c in commits[1:]] + [[]]
    body = git_in(lands["origin"], "log", "-1", "--format=%b", f"{REF}~1").strip()
    assert re.fullmatch(rf"pid {os.getpid()}, PR #8, beats, nonce [0-9a-f]{{16}}", body)
    assert lands["lock"]() == ""


def test_a_stale_lock_is_broken_and_taken_in_one_push_and_its_holders_release_then_leaves_it(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    old: list[str] = []
    monkeypatch.setenv(
        "GIT_COMMITTER_DATE", f"@{int(datetime.now(UTC).timestamp()) - 3 * 60 * 60} +0000"
    )
    assert approvals.locked("main", old)
    monkeypatch.delenv("GIT_COMMITTER_DATE")
    pushes: list[str] = []
    run = subprocess.run

    def counted(args: Any, *rest: Any, **kw: Any) -> Any:
        if isinstance(args, list) and args[:2] == ["git", "push"]:
            pushes.append(args[-1])
        return run(args, *rest, **kw)

    monkeypatch.setattr(approvals.subprocess, "run", counted)
    new: list[str] = []
    assert approvals.locked("main", new)
    assert (
        capsys.readouterr().out
        == f"the landing lock {NAME} is 3 hours old, past 2 hours: broken as stale\n"
    )
    assert pushes == [f"{new[-1]}:{REF}"]
    assert git_in(lands["origin"], "rev-parse", f"{new[-1]}^").strip() == old[-1]
    released = approvals.unlock("main", old[-1])
    assert released.returncode != 0
    assert "stale info" in released.stderr
    assert lands["lock"]() == new[-1]


def test_origin_refuses_a_delete_or_rewrite_of_the_lock_so_no_land_ever_pushed_one(
    lands: dict[str, Any],
) -> None:
    # the fixture's pre-receive hook is what every land test pushes the lock past: it is no hook that passes everything
    lands["open"](7, {"7.txt": "7\n"})
    assert lands["land"]("7")[0] == 0
    tip = git_in(lands["origin"], "rev-parse", REF).strip()
    for refspec in (f":{REF}", f"+{tip}~1:{REF}"):
        pushed = subprocess.run(
            ["git", "-C", str(lands["work"]), "push", "-q", "origin", refspec],
            capture_output=True,
            text=True,
            check=False,
        )
        assert pushed.returncode != 0 and "refused: " in pushed.stderr, pushed.stderr
    assert git_in(lands["origin"], "rev-parse", REF).strip() == tip


def test_origin_refusing_the_locks_push_is_refused_naming_its_reason(
    lands: dict[str, Any],
) -> None:
    # a refusal of a push from the tip read again is origin's, not another land's race: no wait clears it
    lands["open"](7, {"7.txt": "7\n"})
    hook = lands["origin"] / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho no pushes to the lock >&2\nexit 1\n")
    with pytest.raises(
        approvals.Refused, match=f"origin refused the landing lock {NAME}: .*no pushes to the lock"
    ):
        approvals.locked("main", [])
    assert lands["tips"]() == []


def test_a_land_waits_on_the_previous_versions_lock_and_never_writes_it(
    lands: dict[str, Any],
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    theirs = lands["main"]()
    git_in(lands["origin"], "update-ref", approvals.LEGACY, theirs)
    code, lines = lands["land"]("7")
    assert (code, lands["merged"](), lands["tips"]()) == (approvals.WAITING, [], [])
    assert lines == [
        f"a land of the previous version holds the landing lock {approvals.LEGACY}: land --wait waits for it"
    ]
    assert git_in(lands["origin"], "rev-parse", approvals.LEGACY).strip() == theirs
    git_in(lands["origin"], "update-ref", "-d", approvals.LEGACY)
    assert lands["land"]("7")[0] == 0


def test_a_stale_lock_of_the_previous_version_is_passed_over_and_left_in_place(
    lands: dict[str, Any],
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    when = f"@{int(datetime.now(UTC).timestamp()) - approvals.CEILING - 60} +0000"
    tree = git_in(lands["work"], "hash-object", "-t", "tree", "-w", os.devnull).strip()
    theirs = subprocess.run(
        ["git", "commit-tree", "-m", "landing lock", tree],
        cwd=lands["work"],
        env={**os.environ, "GIT_COMMITTER_DATE": when},
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    git_in(lands["work"], "push", "-q", "origin", f"{theirs}:{approvals.LEGACY}")
    assert lands["land"]("7")[0] == 0
    assert git_in(lands["origin"], "rev-parse", approvals.LEGACY).strip() == theirs


def test_a_previous_version_lock_released_after_it_was_listed_lets_the_land_go_on(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    git_in(lands["origin"], "update-ref", approvals.LEGACY, lands["main"]())
    real = approvals.git

    def released_after_listing(*args: str) -> str:
        out = real(*args)
        if args[:2] == ("ls-remote", "origin") and approvals.LEGACY in out:
            git_in(lands["origin"], "update-ref", "-d", approvals.LEGACY)
        return out

    monkeypatch.setattr(approvals, "git", released_after_listing)
    assert lands["land"]("7")[0] == 0


def test_a_failed_fetch_of_a_previous_version_lock_still_there_is_refused_not_waited_on(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    git_in(lands["origin"], "update-ref", approvals.LEGACY, lands["main"]())
    real = approvals.git

    def failing_fetch(*args: str) -> str:
        if args[:3] == ("fetch", "-q", "origin") and args[3].startswith(f"+{approvals.LEGACY}:"):
            raise subprocess.CalledProcessError(128, ["git", *args], stderr="transport failed")
        return real(*args)

    monkeypatch.setattr(approvals, "git", failing_fetch)
    code, lines = lands["land"]("7")
    assert code == 1 and "transport failed" in "\n".join(lines)
    assert lands["merged"]() == []


def test_the_batch_starts_from_the_base_origin_has_once_the_lock_is_taken(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Origin's base moves after any earlier fetch but before the lock is taken: the batch is merged onto the moved
    base, and no gate runs on it."""
    lands["open"](7, {"7.txt": "7\n"})
    monkeypatch.setattr(approvals, "required_checks", lambda _base: set())
    work = lands["work"]
    fetched = git_in(work, "rev-parse", "refs/remotes/origin/main").strip()
    real = approvals.locked
    moved: list[str] = []

    def move_then_lock(base: str, held: list[str], pr: int | None = None) -> bool:
        (work / "elsewhere.txt").write_text("elsewhere\n")
        git_in(work, "add", "elsewhere.txt")
        git_in(work, "commit", "-qm", "origin moves before the lock is taken")
        git_in(work, "push", "-q", "origin", "HEAD:refs/heads/main")
        moved.append(git_in(work, "rev-parse", "HEAD").strip())
        git_in(work, "update-ref", "refs/remotes/origin/main", fetched)
        return real(base, held, pr)

    monkeypatch.setattr(approvals, "locked", move_then_lock)
    assert lands["land"]("7")[0] == 0
    assert (lands["runs"](), lands["main"]("~1")) == (0, moved[0])


def stop_this_thread(sig: int) -> None:
    """Stop this process with `sig` as a stop reaches the land, whose CLI runs one thread: to the whole process
    (os.kill) in a serial run, as in CI, and to the thread running the test in a pytest-xdist worker
    (PYTEST_XDIST_WORKER set). A signal sent to the whole process goes to any thread that doesn't block it, and an
    xdist worker runs a second one (execnet's, started with `_thread`, so `threading` doesn't count it), which would
    take a stop the land holds blocked while it releases its lock."""
    if not os.environ.get("PYTEST_XDIST_WORKER"):
        os.kill(os.getpid(), sig)
    else:
        signal.pthread_kill(threading.get_ident(), sig)


@pytest.mark.parametrize("worker, sent", [("", "process"), ("gw0", "thread")])
def test_a_test_stop_goes_to_the_process_serially_and_to_the_test_thread_in_an_xdist_worker(
    monkeypatch: pytest.MonkeyPatch, worker: str, sent: str
) -> None:
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setenv("PYTEST_XDIST_WORKER", worker)
    monkeypatch.setattr(os, "kill", lambda pid, sig: calls.append(("process", pid, sig)))
    monkeypatch.setattr(signal, "pthread_kill", lambda tid, sig: calls.append(("thread", tid, sig)))
    stop_this_thread(signal.SIGTERM)
    target = os.getpid() if sent == "process" else threading.get_ident()
    assert calls == [(sent, target, signal.SIGTERM)]


@pytest.fixture
def stoppable() -> Iterator[None]:
    """SIGTERM and SIGHUP at their defaults for the test, since a runner started under nohup passes SIGHUP ignored."""
    before = {sig: signal.signal(sig, signal.SIG_DFL) for sig in approvals.STOPS}
    yield
    for sig, handler in before.items():
        signal.signal(sig, handler)


@pytest.mark.usefixtures("stoppable")
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_a_land_stopped_by_sigterm_or_sighup_releases_its_lock_and_exits_128_plus_the_signal(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch, sig: signal.Signals
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    held: list[str] = []

    def stopped(*_args: Any) -> None:
        held.append(lands["lock"]())
        stop_this_thread(sig)

    monkeypatch.setattr(approvals, "merges", stopped)
    before = {s: signal.getsignal(s) for s in approvals.STOPS}
    with pytest.raises(SystemExit) as stop:
        lands["land"]("7")
    assert held[0], "the land held no lock when it was stopped"
    assert (stop.value.code, lands["lock"]()) == (128 + sig, "")
    assert {s: signal.getsignal(s) for s in approvals.STOPS} == before, (
        "the land left its handlers behind"
    )


@pytest.mark.usefixtures("stoppable")
@pytest.mark.parametrize("when", ["before taking", "taking", "releasing", "releasing first"])
def test_a_stop_while_the_lock_is_pushed_still_releases_it(
    lands: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    when: str,
) -> None:
    # before taking: the stop arrives before the lock's push runs, so origin has no lock to release. taking: as the
    # push reaches origin, before locked() returns. releasing: a second stop arrives as the release is pushed, as
    # `uv run` forwards the signal Python already got. releasing first: the land's only stop arrives there
    lands["open"](7, {"7.txt": "7\n"})
    run = subprocess.run

    def pushing(args: Any, *rest: Any, **kw: Any) -> Any:
        target = args[-1] if isinstance(args, list) and args[:2] == ["git", "push"] else ""
        state = (
            git_in(lands["work"], "log", "-1", "--format=%s", target.split(":")[0])
            if target.endswith(f":{REF}")
            else ""
        )
        taking, releasing = state.startswith(approvals.HELD), state.startswith(approvals.FREE)
        if (when == "before taking" and taking) or (when.startswith("releasing") and releasing):
            stop_this_thread(signal.SIGTERM)
        done = run(args, *rest, **kw)
        if when == "taking" and taking:
            stop_this_thread(signal.SIGTERM)
        return done

    monkeypatch.setattr(approvals.subprocess, "run", pushing)
    if when == "releasing":
        monkeypatch.setattr(approvals, "merges", lambda *_: stop_this_thread(signal.SIGTERM))
    with pytest.raises(SystemExit) as stop:
        lands["land"]("7")
    assert (stop.value.code, lands["lock"]()) == (143, "")
    assert "was not released" not in capsys.readouterr().out


def test_the_release_push_runs_outside_the_lands_process_group(
    lands: dict[str, Any], tmp_path: Path
) -> None:
    # ssh and gh as the credential helper unblock SIGTERM and SIGHUP, so a group stop would end them mid-release
    groups = tmp_path / "groups.log"
    receive = tmp_path / "receive-pack"
    receive.write_text(f'#!/bin/sh\nps -o pgid= -p $$ >> "{groups}"\nexec git-receive-pack "$@"\n')
    receive.chmod(0o755)
    git_in(lands["work"], "config", "remote.origin.receivepack", str(receive))
    lands["open"](7, {"7.txt": "7\n"})
    groups.unlink()
    assert (lands["land"]("7")[0], lands["lock"]()) == (0, "")
    pushes = [int(g) for g in groups.read_text().split()]
    assert pushes[0] == os.getpgrp(), (
        "the lock's take ran outside the land's group, so the test proves nothing"
    )
    assert pushes[-1] != os.getpgrp(), "the release push ran in the land's process group"


@pytest.mark.usefixtures("stoppable")
def test_a_second_stop_while_the_batch_worktree_is_removed_leaves_no_worktree(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # uv run forwards the first stop again while the unwinding removes the batch worktree; stopped() ignores it
    lands["open"](7, {"7.txt": "7\n"})
    removed: list[str] = []
    run = subprocess.run

    def removing(args: Any, *rest: Any, **kw: Any) -> Any:
        if isinstance(args, list) and args[:3] == ["git", "worktree", "remove"]:
            removed.append(args[-1])
            stop_this_thread(signal.SIGTERM)
        return run(args, *rest, **kw)

    monkeypatch.setattr(approvals.subprocess, "run", removing)
    monkeypatch.setattr(approvals, "merges", lambda *_: stop_this_thread(signal.SIGTERM))
    with pytest.raises(SystemExit) as stop:
        lands["land"]("7")
    assert (stop.value.code, lands["lock"]()) == (143, "")
    assert removed, "the land never removed its batch worktree"
    assert not Path(removed[0]).exists(), (
        f"the batch worktree {Path(removed[0]).name} was left behind"
    )


def test_a_land_that_inherits_sighup_ignored_outlives_a_hangup(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    merges = approvals.merges

    def hangup(*args: Any) -> Any:
        stop_this_thread(signal.SIGHUP)
        return merges(*args)

    monkeypatch.setattr(approvals, "merges", hangup)
    before = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        assert (lands["land"]("7")[0], lands["lock"]()) == (0, "")
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, before)
    assert lands["merged"]() == ["7"]


def test_a_lock_another_land_took_meanwhile_stays_theirs(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    theirs: list[str] = []

    def broken_and_taken(*_args: Any) -> None:
        theirs.append(lock_commit(lands, 0))

    monkeypatch.setattr(approvals, "merges", broken_and_taken)
    _, lines = lands["land"]("7")
    assert lands["lock"]() == theirs[0]
    assert lines[-1].startswith(f"the landing lock {NAME} was not released: ")


def test_breaking_a_stale_lock_leaves_one_another_land_took_first(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    lock_commit(lands, 3 * 60 * 60)
    fresh: list[str] = []
    real = approvals.git

    def git(*args: str) -> str:
        out = real(*args)
        if (
            args[:1] == ("log",) and not fresh
        ):  # this land has read the stale tip; another breaks and takes it first
            fresh.append(lock_commit(lands, 60))
        return out

    monkeypatch.setattr(approvals, "git", git)
    code, lines = lands["land"]("7")
    assert (code, lands["lock"](), lands["merged"]()) == (approvals.WAITING, fresh[0], [])
    assert lines[-1].startswith(
        f"another land holds the landing lock {NAME} (pid 1), taken 1 minutes ago ("
    )


def refusing(monkeypatch: pytest.MonkeyPatch, refuse: Callable[[tuple[str, ...]], bool]) -> None:
    """GitHub refuses `gh pr merge` where `refuse(args)` says so."""
    fake = approvals.github.gh

    def gh(*args: str, data: str | None = None) -> str:
        if args[:2] == ("pr", "merge") and refuse(args):
            raise subprocess.CalledProcessError(1, args, stderr="Required status check is expected")
        return fake(*args, data=data)

    monkeypatch.setattr(approvals.github, "gh", gh)


def test_a_merge_github_refuses_stops_the_rest_and_the_partial_base_is_not_gated(
    lands: dict[str, Any], hub: dict[str, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    for number in (7, 8, 9):
        lands["open"](number, {f"{number}.txt": f"{number}\n"})
    refusing(monkeypatch, lambda args: args[2] == "8")
    code, _ = lands["land"]()
    assert (code, lands["merged"]()) == (1, ["7"])
    tip, tree = lands["main"](), lands["main"]("^{tree}")
    assert lands["runs"]() == 0
    assert landed_note(lands, 7)["Tree"] == tree
    assert [s for s in hub.get(tip, []) if s["context"] == approvals.PUBLISHED] == []


def test_a_merge_github_leaves_unmerged_stops_the_rest(
    lands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`gh pr merge` exits 0 having only queued #8: it gets no `Landed`, and #9 waits for the next land."""
    for number in (7, 8, 9):
        lands["open"](number, {f"{number}.txt": f"{number}\n"})
    fake = approvals.github.gh

    def gh(*args: str, data: str | None = None) -> str:
        if args[:3] == ("pr", "merge", "8"):
            return ""
        return fake(*args, data=data)

    monkeypatch.setattr(approvals.github, "gh", gh)
    code, lines = lands["land"]()
    assert (code, lands["merged"]()) == (1, ["7"])
    assert any("PR #8: the merge was refused" in line and "not merged" in line for line in lines)
    assert [n for n, body in lands["comments"] if body.startswith("Landed\n")] == [7]


def test_a_base_that_moved_during_the_merges_is_not_gated(
    lands: dict[str, Any], hub: dict[str, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    work, moved = lands["work"], []

    def merged_by_hand(_args: tuple[str, ...]) -> bool:
        if not moved:
            git_in(work, "switch", "-q", "--detach", "origin/main")
            (work / "red.txt").write_text("red\n")
            git_in(work, "add", "red.txt")
            git_in(work, "commit", "-qm", "merged by hand")
            moved.append(git_in(work, "push", "-q", "origin", "HEAD:main"))
        return False

    refusing(monkeypatch, merged_by_hand)
    code, lines = lands["land"]("7")
    assert (code, lands["merged"](), lands["runs"]()) == (0, ["7"], 0), lines
    assert [s for s in hub.get(lands["main"](), []) if s["context"] == "gate"] == []


def test_a_refused_batch_lands_on_the_next_land_and_neither_runs_a_gate(
    lands: dict[str, Any], hub: dict[str, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](8, {"8.txt": "8\n"})
    refused: list[str] = []
    refusing(monkeypatch, lambda args: not refused and not refused.append(args[2]))
    assert lands["land"]()[0] == 1
    # the refused head keeps only the `land` statuses (the success before its merge call, the error after the
    # refusal): no gate result was published
    assert (
        lands["runs"](),
        lands["merged"](),
        [s["context"] for ss in hub.values() for s in ss],
    ) == (0, [], ["land", "land"])
    assert lands["land"]()[0] == 0
    assert (lands["runs"](), lands["merged"]()) == (0, ["7", "8"])


def test_land_on_a_merged_pr_says_so_and_on_a_closed_one_refuses(lands: dict[str, Any]) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    lands["pulls"][7]["state"] = "MERGED"
    assert lands["land"]("7") == (0, ["PR #7 is merged"])
    lands["pulls"][7]["state"] = "CLOSED"
    assert lands["land"]("7") == (1, ["PR #7 is closed: nothing to land"])
    assert (lands["lock"](), lands["runs"]()) == ("", 0)


def test_the_old_settings_say_they_are_deprecated_and_the_gate_keys_are_ignored(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1872: a policy still setting a key that drove land's gate parses, says once that it is ignored, and reads as
    one without it; `ci` and `carry_over_main` still work, deprecated."""
    loop = add(
        add(
            add(LOOP, "loop", "ci: false\nbatch_queue: true\n"),
            "carry_over_main",
            "except:\n  - mise.toml\n",
        ),
        "land",
        "gate_on: cloud\ncloud_gate_timeout_seconds: 2400\nhost_gate: false\nbatch_wait_seconds: 5\n",
    )
    approvals.DEPRECATED.clear()
    read = approvals.settings(loop)
    assert approvals.no_ci(loop) and "batch_queue" not in read
    assert read["land"] == {"batch_wait_seconds": 5}
    assert capsys.readouterr().err.splitlines() == [
        "ignored: loop.md's `loop.batch_queue` does nothing, since land runs no gate (#1872): remove it",
        "deprecated: ci goes in the next release; land batches every landable PR (#650)",
        "deprecated: carry_over_main goes in the next release; land batches every landable PR (#650)",
        *(
            f"ignored: loop.md's `land.{key}` does nothing, since land runs no gate (#1872): remove it"
            for key in ("gate_on", "cloud_gate_timeout_seconds", "host_gate")
        ),
    ]
    with pytest.raises(SystemExit):
        approvals.main(["check", "queue", "7", "--head", HEAD])
    with pytest.raises(SystemExit):
        approvals.main(
            ["batch", "7", "--merge", HEAD, "--batch", HEAD, "--exit", "0", "--log", "x"]
        )


# --- land: a local signed squash, where loop.md says so ---

REPO = "https://github.com/kzarzycki/scratch-gate-revert"
SIGNED_LAND = "signed_squash_to: main\nallowed_signers: signers\n"


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def signing(landing: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    """`landing` in a real checkout whose `origin` names the PR's repo and reaches a local bare repo: `main` and the
    PR's branch `feature` (also `refs/pull/7/head`) one commit ahead, an SSH signing key, and an allowed signers file
    `signers` holding it. The PR's head and verdict are the real commit; loop.md has the `land` table."""
    remote = tmp_path.parent / f"{tmp_path.name}-remote.git"
    sh("git", "init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    git = lambda *args: sh("git", "-c", "commit.gpgsign=false", *args, cwd=tmp_path)
    git("init", "-q", "-b", "main")
    git("config", "user.name", "Lander")
    git("config", "user.email", "lander@example.com")
    git("remote", "add", "origin", REPO)
    git("config", f"url.{remote.as_uri()}.insteadOf", REPO)
    loop = tmp_path / "docs" / "agents" / "loop.md"
    loop.write_text(add(loop.read_text(), "land", SIGNED_LAND))
    git("add", "docs")
    git("commit", "-qm", "base")
    git("push", "-q", "origin", "main")
    git("switch", "-qc", "feature")
    (tmp_path / "app.py").write_text("print('feature')\n")
    git("add", "app.py")
    git("commit", "-qm", "feature")
    git("push", "-q", "origin", "feature", "HEAD:refs/pull/7/head")
    git("switch", "-q", "main")
    key = tmp_path.parent / f"{tmp_path.name}-key"
    sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "lander", "-f", str(key), cwd=tmp_path)
    git("config", "gpg.format", "ssh")
    git("config", "user.signingkey", str(key))
    (tmp_path / "signers").write_text(f"lander@example.com {key.with_suffix('.pub').read_text()}")
    head = git("rev-parse", "feature")
    landing["pull"].update(
        headRefOid=head, reviews={"nodes": [verdict_on(head, "0 blocker, 0 major, 0 minor")]}
    )
    return {"git": git, "remote": remote, "base": git("rev-parse", "main"), "head": head}


def signed_land(
    landing: dict[str, Any], capsys: pytest.CaptureFixture[str], onto: tuple[str, str] | None = None
) -> tuple[int, list[str], list[tuple[str, ...]]]:
    """land's signed merge of the PR, as it runs it: refused by unsignable() before anything moves, else
    signed_squash(); (exit, lines, gh pr calls)."""
    try:
        signed = approvals.signed(approvals.project()[1])
        assert signed
        if why := approvals.unsignable(landing["pull"], *signed):
            raise approvals.Refused(why)
        commit = approvals.signed_squash(landing["pull"], signed[1], onto)
        print(f"PR #7 landed in main as {commit}, signed by a key in {signed[1]}")
        code = 0
    except approvals.Refused as exc:
        print(exc)
        code = 1
    return code, capsys.readouterr().out.splitlines(), landing["calls"]


def remote_ref(signing: dict[str, Any], ref: str) -> str:
    return sh("git", "rev-parse", ref, cwd=signing["remote"])


def test_a_hosts_global_git_config_cannot_take_over_a_tests_signing(
    signing: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host whose global config signs with a failing program still signs with the key `signing` made (#400)."""
    home = tmp_path.parent / f"{tmp_path.name}-home"
    home.mkdir()
    (home / ".gitconfig").write_text('[gpg "ssh"]\n\tprogram = false\n[commit]\n\tgpgsign = true\n')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    signing["git"]("commit", "-q", "-S", "--allow-empty", "-m", "signed")
    verified = subprocess.run(
        ["git", "-c", "gpg.ssh.allowedSignersFile=signers", "verify-commit", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert 'Good "git" signature for lander@example.com' in verified.stderr


def test_land_with_the_line_squashes_signs_and_pushes_the_pr_branch_then_main(
    landing: dict[str, Any], signing: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, calls = signed_land(landing, capsys)
    commit = remote_ref(signing, "main")
    assert (code, out, calls) == (
        0,
        [f"PR #7 landed in main as {commit}, signed by a key in signers"],
        [],  # never `gh pr merge`: GitHub's squash would carry GitHub's signature
    )
    git = signing["git"]
    assert (
        remote_ref(signing, "feature") == commit
    )  # GitHub marks the PR merged: its head is in main
    assert git("rev-parse", f"{commit}^") == signing["base"]  # one commit on main's old tip
    assert git("rev-parse", f"{commit}^{{tree}}") == git("rev-parse", f"{signing['head']}^{{tree}}")
    assert git("log", "-1", "--format=%s", commit) == "Feature (#7)"
    git("-c", "gpg.ssh.allowedSignersFile=signers", "verify-commit", commit)


def refused_untouched(
    landing: dict[str, Any], signing: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> list[str]:
    code, out, calls = signed_land(landing, capsys)
    assert (code, calls) == (1, [])
    assert remote_ref(signing, "feature") == signing["head"]
    return out


def test_land_with_the_line_refuses_a_checkout_that_signs_nothing(
    landing: dict[str, Any], signing: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    signing["git"]("config", "--unset", "user.signingkey")
    assert refused_untouched(landing, signing, capsys) == [
        "this checkout signs no commits with an SSH key: set `gpg.format ssh` and `user.signingkey`"
    ]
    assert remote_ref(signing, "main") == signing["base"]


def test_land_with_the_line_refuses_a_signature_the_allowed_signers_lack(
    landing: dict[str, Any],
    signing: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    other = tmp_path.parent / f"{tmp_path.name}-other"
    sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other), cwd=tmp_path)
    (tmp_path / "signers").write_text(f"lander@example.com {other.with_suffix('.pub').read_text()}")
    [line] = refused_untouched(landing, signing, capsys)
    assert line.startswith("PR #7: the squash is not signed by a key in signers: ")
    assert remote_ref(signing, "main") == signing["base"]


def test_land_with_the_line_refuses_a_pr_from_another_repository(
    landing: dict[str, Any], signing: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    landing["pull"]["isCrossRepository"] = True
    assert refused_untouched(landing, signing, capsys) == [
        (
            "PR #7 comes from another repository: its branch isn't ours to push, push it to"
            " kzarzycki/scratch-gate-revert first"
        )
    ]


def test_land_with_the_line_refuses_a_pr_missing_the_base_head(
    landing: dict[str, Any], signing: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    git = signing["git"]
    git("commit", "-q", "--allow-empty", "-m", "landed meanwhile")
    git("push", "-q", "origin", "main")
    tip = git("rev-parse", "main")
    assert refused_untouched(landing, signing, capsys) == [
        (
            f"PR #7 does not contain main's head {tip}: merge origin/main in, rerun the gates, get a verdict on the"
            " merge, then land again"
        )
    ]
    assert remote_ref(signing, "main") == tip


def test_land_with_the_line_moves_neither_branch_when_the_base_moves_during_the_push(
    landing: dict[str, Any],
    signing: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another PR lands between the squash and its push: the base keeps that commit, and the PR's branch its head."""
    git, real = signing["git"], approvals.git
    moved = []

    def landing_meanwhile(*args: str) -> str:
        if args[0] == "push" and not moved:
            tree = git("rev-parse", f"{signing['base']}^{{tree}}")
            moved.append(git("commit-tree", "-p", signing["base"], "-m", "another landing", tree))
            git("push", "-q", "origin", f"{moved[0]}:refs/heads/main")
        return real(*args)

    monkeypatch.setattr(approvals, "git", landing_meanwhile)
    [line] = refused_untouched(landing, signing, capsys)
    assert line.startswith(
        f"PR #7: main refused the squash, so feature is back at {signing['head']}: "
    )
    assert remote_ref(signing, "main") == moved[0]


def test_land_with_the_line_refuses_another_base_and_a_line_it_cannot_read(
    landing: dict[str, Any],
    signing: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    landing["pull"]["baseRefName"] = "release"
    assert refused_untouched(landing, signing, capsys) == [
        "PR #7 targets release, but the policy lands signed squashes only to main"
    ]
    loop = tmp_path / "docs" / "agents" / "loop.md"
    loop.write_text(loop.read_text().replace("  allowed_signers: signers\n", ""))
    signing["git"]("commit", "-qam", "no signers")  # the policy is main's (#1047)
    signing["git"]("push", "-q", "origin", "main")
    assert refused_untouched(landing, signing, capsys) == [
        "loop.md's `land` lacks `allowed_signers`"
    ]


def test_a_batch_squash_lands_the_gated_tree_on_the_commit_before_it(
    landing: dict[str, Any], signing: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """In land's batch the squash holds the tree gated with the PR, on the base's tip the batch merged onto; a base
    that moved since is refused, since the squash would drop what landed meanwhile."""
    git = signing["git"]
    tree = git("rev-parse", f"{signing['head']}^{{tree}}")
    assert signed_land(landing, capsys, ("c" * 40, tree))[:2] == (
        1,
        [
            f"PR #7: main moved to {signing['base']} since the batch was merged onto {'c' * 40}: land again"
        ],
    )
    code, _, _ = signed_land(landing, capsys, (signing["base"], tree))
    commit = remote_ref(signing, "main")
    assert (code, git("rev-parse", f"{commit}^"), git("rev-parse", f"{commit}^{{tree}}")) == (
        0,
        signing["base"],
        tree,
    )


# --- opt-out: a person keeps their PR out of the loop ---

OPTED = add(LOOP, "loop", "opt_out: true\n")
OUTSIDE = (
    "PR #7 is outside the loop (loop.md `opt_out: true`, no verifier review, spec approval,"
    " revert or bot exemption): its author merges it"
)


@pytest.mark.needs_tool("ssh-keygen")
def test_a_green_signed_batch_gives_each_pr_a_commit_of_its_own_change(
    lands: dict[str, Any], tmp_path: Path
) -> None:
    work = lands["work"]
    git_in(work, "remote", "set-url", "origin", REPO)
    git_in(work, "config", f"url.{lands['origin'].as_uri()}.insteadOf", REPO)
    key = tmp_path / "key"
    sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "lander", "-f", str(key), cwd=tmp_path)
    git_in(work, "config", "gpg.format", "ssh")
    git_in(work, "config", "user.signingkey", str(key))
    (work / "signers").write_text(f"loop@example.com {key.with_suffix('.pub').read_text()}")
    loop = work / "docs" / "agents" / "loop.md"
    loop.write_text(add(loop.read_text(), "land", SIGNED_LAND))
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](8, {"8.txt": "8\n"})
    assert lands["land"]()[0] == 0
    # no gate runs, and each commit holds its own PR's change, so reverting one reverts only it
    assert lands["runs"]() == 0
    assert git_in(work, "log", "-2", "--name-only", "--format=%s", lands["main"]()).split() == [
        "PR", "8", "(#8)", "8.txt", "PR", "7", "(#7)", "7.txt",
    ]  # fmt: skip


# --- land in a cloud session: the landing key from the environment, never the VM's ---

CLOUD_LAND = SIGNED_LAND + "where: cloud\nsigning_key_env: LAND_KEY\n"
# a cloud VM's signing program, which signs with the session's key: here it fails, so a land through it fails
VM_SIGNER = "#!/bin/sh\necho 'the session signer was used' >&2\nexit 1\n"


@pytest.fixture
def cloud_land(
    lands: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """`lands` as a cloud session runs it: the cloud marker set, PR 7 open, loop.md's `land` section with `where:
    cloud` and `signing_key_env: LAND_KEY`, the variable holding a landing key listed in `signers`, and the VM's own
    config (`commit.gpgsign`, a session key, a signing program that fails). The temporary directory is a fresh one,
    `tmp`, so a key folder left behind shows. `out(code, lines)` checks the key's text was printed nowhere."""
    work = lands["work"]
    git_in(work, "remote", "set-url", "origin", REPO)
    git_in(work, "config", f"url.{lands['origin'].as_uri()}.insteadOf", REPO)
    keys = tmp_path / "keys"
    keys.mkdir()
    for name in ("landing", "session", "stranger"):
        sh(
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            name,
            "-f",
            str(keys / name),
            cwd=keys,
        )
    (work / "signers").write_text(f"loop@example.com {(keys / 'landing.pub').read_text()}")
    loop = work / "docs" / "agents" / "loop.md"
    loop.write_text(add(loop.read_text(), "land", CLOUD_LAND))
    head = lands["open"](7, {"7.txt": "7\n"})
    signer = tmp_path / "code-sign"
    signer.write_text(VM_SIGNER)
    signer.chmod(0o755)
    for key, value in (
        ("gpg.format", "ssh"),
        ("commit.gpgsign", "true"),
        ("user.signingkey", str(keys / "session")),
        ("gpg.ssh.program", str(signer)),
    ):
        git_in(work, "config", key, value)
    landing_key = (keys / "landing").read_text()
    monkeypatch.setenv(approvals.CLOUD, "true")
    monkeypatch.setenv("LAND_KEY", landing_key)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    faked = approvals.github.gh

    def gh(*args: str, data: str | None = None) -> str:
        """`lands`' gh, also listing the open PRs as drop_records reads them over REST once a land merged."""
        if args[:2] == ("api", "--paginate") and "pulls?state=open" in args[2]:
            assert args[3:] == ("--jq", ".[]"), args
            return "".join(
                json.dumps({"number": pr["number"], "head": {"sha": pr["headRefOid"]}}) + "\n"
                for pr in lands["pulls"].values()
                if pr["state"] == "OPEN"
            )
        return faked(*args, data=data)

    monkeypatch.setattr(approvals.github, "gh", gh)

    def out(capsys: pytest.CaptureFixture[str], code_lines: tuple[int, list[str]]) -> list[str]:
        """land's lines, once the key's text is in neither them nor stderr, and no key folder is left."""
        err = capsys.readouterr().err
        for secret in (landing_key, *landing_key.splitlines()[1:-1]):
            assert secret not in "\n".join(code_lines[1]) and secret not in err
        assert list(tmp.iterdir()) == []
        return code_lines[1]

    lands.update(keys=keys, head=head, loop=loop, out=out, tmp=tmp, base=lands["main"]())
    return lands


def unmoved(cloud_land: dict[str, Any]) -> None:
    """No ref the land would move moved: main, the PR's branch, and no landing lock taken."""
    assert (cloud_land["main"](), git_in(cloud_land["origin"], "rev-parse", "pr7").strip()) == (
        cloud_land["base"],
        cloud_land["head"],
    )
    assert cloud_land["tips"]() == []


def test_a_cloud_land_signs_the_squash_with_the_environments_key_not_the_vms(
    cloud_land: dict[str, Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The VM's signing program fails, yet the PR lands, as a squash a host verifies against `signers` with the
    landing key; GitHub refusing the `gate` status, as a cloud session's proxy does, the pass goes to origin as its record
    branch, which the land drops once that tree landed."""

    def refused(*_args: Any, **_kw: Any) -> None:
        raise subprocess.CalledProcessError(1, "gh", stderr="gh: Forbidden (HTTP 403)")

    monkeypatch.setattr(approvals, "post_status", refused)
    for name, real in (
        ("record_ref", RECORD_REF),
        ("push_record", PUSH_RECORD),
        ("drop_records", DROP_RECORDS),
    ):
        monkeypatch.setattr(approvals, name, real)
    lines = cloud_land["out"](capsys, cloud_land["land"]("7"))
    tip = cloud_land["main"]()
    assert f"PR #7 landed in main as {tip}, signed by a key in signers" in lines
    assert cloud_land["merged"]() == []  # never GitHub's merge
    assert git_in(cloud_land["origin"], "rev-parse", "pr7").strip() == tip
    verified = subprocess.run(
        # as a host checks it, with ssh-keygen rather than the VM's program
        [
            "git",
            *("-c", "gpg.ssh.program=ssh-keygen"),
            *("-c", "gpg.ssh.allowedSignersFile=signers"),
            *("verify-commit", tip),
        ],
        cwd=cloud_land["work"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    assert 'Good "git" signature for loop@example.com with ED25519 key' in verified.stderr
    assert cloud_land["tips"]() and cloud_land["lock"]() == ""  # the lock was taken and released
    # no gate ran, so no pass is published on the new tip
    assert not any(line.startswith("published in origin") for line in lines)


@pytest.mark.parametrize(
    ("key", "why"),
    [
        (None, "the variable LAND_KEY (loop.md's `land.signing_key_env`) is not set"),
        ("", "the variable LAND_KEY (loop.md's `land.signing_key_env`) is empty"),
        (" \n", "the variable LAND_KEY (loop.md's `land.signing_key_env`) is empty"),
        (
            "stranger",
            (
                "the key in the variable LAND_KEY (loop.md's `land.signing_key_env`) is not in signers: add its"
                " public key there"
            ),
        ),
        (
            "garbage",
            "the variable LAND_KEY (loop.md's `land.signing_key_env`) holds no SSH private key without a passphrase",
        ),
    ],
)
def test_a_cloud_land_refuses_a_missing_empty_or_unlisted_key_and_prints_no_key(
    cloud_land: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    key: str | None,
    why: str,
) -> None:
    if key is None:
        monkeypatch.delenv("LAND_KEY")
    elif key == "stranger":
        monkeypatch.setenv("LAND_KEY", (cloud_land["keys"] / "stranger").read_text())
    elif key == "garbage":
        monkeypatch.setenv("LAND_KEY", (cloud_land["keys"] / "landing").read_text()[:-40] + "\n")
    else:
        monkeypatch.setenv("LAND_KEY", key)
    code, lines = cloud_land["land"]("7")
    assert (code, cloud_land["out"](capsys, (code, lines))) == (1, [why])
    unmoved(cloud_land)
    if key in ("stranger", "garbage"):
        text = os.environ["LAND_KEY"]
        assert all(part not in "\n".join(lines) for part in text.splitlines()[1:-1])


def test_a_cloud_land_refuses_a_section_naming_no_key_variable(
    cloud_land: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    cloud_land["loop"].write_text(
        cloud_land["loop"].read_text().replace("  signing_key_env: LAND_KEY\n", "")
    )
    assert cloud_land["out"](capsys, cloud_land["land"]("7")) == [
        (
            "land in a cloud session signs with the key in the variable loop.md's `land.signing_key_env` names,"
            " and it names none"
        )
    ]
    unmoved(cloud_land)


@pytest.mark.parametrize(
    ("where", "marker", "place"),
    [("host", True, "a cloud session"), ("cloud", False, "a host")],
)
def test_land_refuses_where_loop_md_excludes_the_place_it_runs(
    cloud_land: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    where: str,
    marker: bool,
    place: str,
) -> None:
    cloud_land["loop"].write_text(
        cloud_land["loop"].read_text().replace("where: cloud", f"where: {where}")
    )
    if not marker:
        monkeypatch.delenv(approvals.CLOUD)
    # never a PR changed (readied, labelled, merged): the refusal comes first, after only the read of its base, whose
    # policy it is (#1047)
    assert cloud_land["out"](capsys, cloud_land["land"]("7")) == [
        f"loop.md's `land.where` is `{where}`, so land does not run in {place}"
    ]
    assert cloud_land["calls"] == []
    unmoved(cloud_land)


def test_a_cloud_land_with_where_unset_lands_like_both(
    cloud_land: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """`where` unset is `both` (#1356), so a cloud session lands as it does with `where: both`."""
    text = cloud_land["loop"].read_text()
    cloud_land["loop"].write_text(text.replace("  where: cloud\n", ""))
    code, lines = cloud_land["land"]("7")
    assert (code, cloud_land["main"]()) == (
        0,
        git_in(cloud_land["origin"], "rev-parse", "pr7").strip(),
    )
    cloud_land["out"](capsys, (code, lines))


@pytest.fixture
def rest_land(cloud_land: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """`cloud_land` with a land section that names no signed squash and no key, and GitHub's GraphQL (`gh pr merge`)
    refused as a cloud session's proxy does; `puts` keeps each REST merge call, `PUT pulls/N/merge`, made as GitHub
    does: a `sha` that is not the PR's head is a 409, else it squashes the head onto main."""
    cloud_land["loop"].write_text(
        GITHUB_RULE.replace("```yaml\n", "```yaml\nland:\n  where: cloud\n")
    )
    monkeypatch.delenv("LAND_KEY")
    puts: list[tuple[str, ...]] = []
    faked = approvals.github.gh

    def gh(*args: str, data: str | None = None) -> str:
        if args[:2] == ("pr", "merge"):
            raise subprocess.CalledProcessError(
                1, "gh", stderr=cloud_land.get("refusal", REFUSED_GRAPHQL)
            )
        if args[:3] == ("api", "-X", "PUT") and args[3].endswith("/merge"):
            puts.append(args)
            fields = dict(arg.split("=", 1) for arg in args[4:] if arg != "-f")
            number = int(args[3].split("/")[-2])
            if fields["sha"] != cloud_land["pulls"][number]["headRefOid"]:
                raise subprocess.CalledProcessError(
                    1,
                    "gh",
                    stderr="Head branch was modified. Review and try the merge again. (HTTP 409)",
                )
            body = ("--body", fields["commit_message"]) if "commit_message" in fields else ()
            return faked(
                "pr", "merge", str(number), f"--{fields['merge_method']}", *body,
                "--match-head-commit", fields["sha"],
            )  # fmt: skip
        return faked(*args, data=data)

    monkeypatch.setattr(approvals.github, "gh", gh)
    monkeypatch.setattr(approvals.github, "_rest_only", False)
    cloud_land["puts"] = puts
    return cloud_land


def test_a_cloud_land_with_no_signed_squash_merges_over_rest_pinned_to_the_gated_head(
    rest_land: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """No queue, no `signed_squash_to`, no key: the PR lands through `PUT pulls/N/merge` as a squash whose `sha` is the
    head the gates checked, which GitHub signs."""
    code, lines = rest_land["land"]("7")
    assert code == 0, lines
    assert "PR #7 lands as a squash" in lines
    assert [args[3] for args in rest_land["puts"]] == ["repos/{owner}/{repo}/pulls/7/merge"]
    put = rest_land["puts"][0]
    assert "merge_method=squash" in put and f"sha={rest_land['head']}" in put
    assert rest_land["main"]() != rest_land["base"]
    assert [args[:2] for args in rest_land["calls"] if args[:2] == ("pr", "merge")] == [
        ("pr", "merge")
    ]


def test_a_cloud_rest_squash_sends_no_commit_message(
    rest_land: dict[str, Any],
) -> None:
    """The PUT carries no `commit_message`, so GitHub's squash default writes the body, a session link line kept."""
    git_in(rest_land["work"], "config", "commit.gpgsign", "false")  # the VM's signer fails
    link = "Claude-Session: https://claude.ai/code/session_01AbCdEf"
    head = rest_land["open"](8, {"8.txt": "8\n"}, (f"PR 8\n\nWhat it does.\n\n{link}",))
    code, lines = rest_land["land"]("8")
    assert code == 0, lines
    [put] = [args for args in rest_land["puts"] if args[3].endswith("/pulls/8/merge")]
    assert f"sha={head}" in put and "merge_method=squash" in put
    assert not any(arg.startswith("commit_message=") for arg in put)


def test_a_cloud_rest_land_of_a_merge_commit_head_sends_merge_with_no_body(
    rest_land: dict[str, Any],
) -> None:
    """A PR whose head matches `land.merge_commit_heads` lands as a merge commit: `merge_method=merge`, and no
    `commit_message`, which only a squash carries."""
    rest_land["loop"].write_text(
        GITHUB_RULE.replace(
            "```yaml\n", "```yaml\nland:\n  where: cloud\n  merge_commit_heads: [sync/*]\n"
        )
    )
    git_in(rest_land["work"], "config", "commit.gpgsign", "false")  # the VM's signer fails
    head = rest_land["open"](9, {"9.txt": "9\n"})
    rest_land["pulls"][9]["headRefName"] = "sync/up"
    code, lines = rest_land["land"]("9")
    assert code == 0, lines
    assert "PR #9 lands as a merge" in lines
    [put] = [args for args in rest_land["puts"] if args[3].endswith("/pulls/9/merge")]
    assert "merge_method=merge" in put and f"sha={head}" in put
    assert not any(arg.startswith("commit_message=") for arg in put)


def test_a_cloud_rest_land_refuses_a_head_that_moved_and_leaves_main_alone(
    rest_land: dict[str, Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub answers a `sha` that is no longer the PR's head with a 409: the PR is not merged and land says so."""
    sent = approvals.github.merge

    def moved(number: int, method: str, head: str) -> None:
        sent(number, method, "0" * 40)

    monkeypatch.setattr(approvals.github, "merge", moved)
    code, lines = rest_land["land"]("7")
    assert code == 1
    assert any("the merge was refused" in line and "HTTP 409" in line for line in lines), lines
    assert rest_land["main"]() == rest_land["base"]


def refused_merge(
    rest_land: dict[str, Any], monkeypatch: pytest.MonkeyPatch, body: str
) -> tuple[int, list[str]]:
    """Land PR #7 where the proxy answers the REST merge with a 403 whose body is `body`."""
    faked = approvals.github.gh

    def gh(*args: str, data: str | None = None) -> str:
        if args[:3] == ("api", "-X", "PUT") and args[3].endswith("/merge"):
            raise subprocess.CalledProcessError(1, "gh", stderr=f"{body} (HTTP 403)")
        return faked(*args, data=data)

    monkeypatch.setattr(approvals.github, "gh", gh)
    return rest_land["land"]("7")


def test_a_proxy_path_403_on_the_rest_merge_names_the_host_land_not_add_repo(
    rest_land: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    code, lines = refused_merge(
        rest_land,
        monkeypatch,
        "Write access to this GitHub API path is not permitted through this proxy",
    )
    text = " ".join(lines)
    assert code == 1 and "from a host checkout" in text and "not permitted through" in text
    assert "add_repo" not in text
    assert rest_land["main"]() == rest_land["base"]


def test_an_unattached_repo_403_on_the_rest_merge_names_add_repo(
    rest_land: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    code, lines = refused_merge(
        rest_land, monkeypatch, "GitHub access to this repository is not enabled for this session"
    )
    text = " ".join(lines)
    assert code == 1 and "add_repo" in text and '"push"' in text
    assert rest_land["main"]() == rest_land["base"]


def test_a_cloud_land_asking_a_signed_squash_without_ssh_keygen_refuses_naming_it(
    cloud_land: dict[str, Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    code, lines = cloud_land["land"]("7")
    assert (code, cloud_land["out"](capsys, (code, lines))) == (1, [approvals.NO_KEYGEN])
    assert "`ssh-keygen`" in approvals.NO_KEYGEN
    unmoved(cloud_land)


def test_a_signed_squash_without_ssh_keygen_is_refused_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(approvals.Refused, match="needs `ssh-keygen`"):
        approvals.signed_squash({"number": 7}, Path("signers"))


def test_the_cloud_key_folder_is_removed_when_the_land_fails_after_taking_it(
    cloud_land: dict[str, Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A land that raises once the key is on disk still removes its folder, and signs nothing after it."""
    seen: list[list[str]] = []

    def broken(*_args: Any) -> tuple[int, bool]:
        seen.append(list(approvals.SIGNING))
        raise approvals.Refused("broken")

    monkeypatch.setattr(approvals, "landing_here", broken)
    assert cloud_land["out"](capsys, cloud_land["land"]("7")) == ["broken"]
    assert seen[0][1::2] == [
        "gpg.format=ssh",
        f"user.signingkey={seen[0][3].split('=', 1)[1]}",
        "gpg.ssh.program=ssh-keygen",
    ]
    assert Path(seen[0][3].split("=", 1)[1]).parent.parent == cloud_land["tmp"]
    assert approvals.SIGNING == []


def test_the_cloud_key_file_is_the_owners_alone(
    cloud_land: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    modes: list[int] = []
    monkeypatch.setattr(
        approvals,
        "landing_here",
        lambda *_a: (
            modes.append(Path(approvals.SIGNING[3].split("=", 1)[1]).stat().st_mode & 0o777)
            or (0, False)
        ),
    )
    assert approvals.landing(7) == (0, False)
    assert modes == [0o600]


def test_a_cloud_land_reads_the_default_branch_over_rest(
    cloud_land: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals.github, "_rest_only", True)
    monkeypatch.setattr(approvals.github, "rest", lambda path: {"default_branch": "main"})
    named: list[str] = []
    monkeypatch.setattr(
        approvals, "locked", lambda base, _held, _pr=None: named.append(base) or False
    )
    assert approvals.landing(None) == (approvals.WAITING, True)
    assert named == ["main"]


def test_a_cloud_land_takes_a_pr_rest_cannot_say_is_queued_only_on_the_projects_word(
    cloud_land: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """REST says neither whether a PR is in a merge queue nor whether classic branch protection requires one: a cloud
    session refuses such a PR unless loop.md's `land` section says `merge_queue: false`. A host still refuses it."""
    pull = cloud_land["pulls"][7] | {"isInMergeQueue": None}
    with approvals.placed(approvals.project()[1]):
        assert "`merge_queue: false`" in approvals.unsignable(pull, "main", Path("signers"))
    cloud_land["loop"].write_text(
        cloud_land["loop"]
        .read_text()
        .replace(
            "  signing_key_env: LAND_KEY\n", "  signing_key_env: LAND_KEY\n  merge_queue: false\n"
        )
    )
    with approvals.placed(approvals.project()[1]):
        assert approvals.unsignable(pull, "main", Path("signers")) is None
    monkeypatch.delenv(approvals.CLOUD)
    git_in(cloud_land["work"], "config", "gpg.ssh.program", "ssh-keygen")
    assert approvals.unsignable(pull, "main", Path("signers")).startswith(
        "PR #7: GitHub's REST API doesn't say whether it is in a merge queue, which would merge it with GitHub's"
        " signature: land it where GraphQL answers"
    )


def test_a_key_is_listed_only_as_its_type_and_body_together_on_a_live_line(tmp_path: Path) -> None:
    signers = tmp_path / "signers"
    signers.write_text(
        "# a@example.com ssh-ed25519 BODY\nb@example.com ssh-rsa BODY\nc@example.com ssh-ed25519 OTHER\n"
    )
    assert not approvals.listed("ssh-ed25519 BODY landing", signers)
    signers.write_text(signers.read_text() + 'd@example.com namespaces="git" ssh-ed25519 BODY d\n')
    assert approvals.listed("ssh-ed25519 BODY landing", signers)


def test_green_required_checks_and_the_verifiers_record_land_with_no_gate(
    lands: dict[str, Any], hub: dict[str, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # green required CI on the head: land merges with no gate of its own (#1872)
    monkeypatch.setattr(approvals, "required_checks", lambda _base: {"check"})
    lands["open"](7, {"7.txt": "7\n"})
    assert lands["land"]("7")[0] == 0
    assert (lands["runs"](), lands["merged"]()) == (0, ["7"])
    assert landed_note(lands, 7)["Gate"] == approvals.NO_GATE


def gate(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, list[str]]:
    code = approvals.main(list(argv))
    return code, capsys.readouterr().out.splitlines()


def test_a_pr_with_no_loop_trail_is_outside_the_loop_where_opt_out_is_allowed(
    landing: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    Path("docs/agents/loop.md").write_text(OPTED)
    mine = landing["pull"]
    mine["reviews"]["nodes"] = []
    mine["closingIssuesReferences"]["nodes"] = [specced(1, "kind:bug")]
    assert gate(capsys, "check", "merge", "7") == (0, [OUTSIDE])
    # risk rules hold no PR outside the loop: the project accepted that with the setting
    mine["labels"]["nodes"].append({"name": "risk:high"})
    assert gate(capsys, "check", "merge", "7") == (0, [OUTSIDE])
    # without the setting, the same PR fails the gate as before
    Path("docs/agents/loop.md").write_text(LOOP)
    code, lines = gate(capsys, "check", "merge", "7")
    assert code == 1 and "PR #7: no verifier review posted (approvals.py verdict)" in lines


@pytest.mark.parametrize("trail", ["review", "spec-record", "approved-spec", "revert"])
def test_any_loop_trail_keeps_the_pr_in_the_loop(landing: dict[str, Any], trail: str) -> None:
    mine = landing["pull"]
    mine["reviews"]["nodes"] = [VERDICT] if trail == "review" else []
    linked = specced(1, "kind:bug")
    if trail == "spec-record":
        linked = issue(1, "kind:bug", comments=(record("spec", "loop", spec="as written x"),))
    if trail == "approved-spec":
        linked = specced(1, "kind:bug", "approved:spec")
    if trail == "revert":
        mine["body"] = "Reverts #3\n\n## Evidence\n\nwrong\n"
    mine["closingIssuesReferences"]["nodes"] = [linked]
    assert approvals.outside(mine, OPTED) is None
    assert approvals.outside({**mine, "reviews": {"nodes": []}, "body": ""}, LOOP) is None


def test_an_exempt_bot_pr_keeps_its_trail() -> None:
    pr = {**pull(files=("uv.lock",)), "author": {"__typename": "Bot", "login": "dependabot"}}
    pr["authored"] = {"nodes": [authored("dependabot[bot]")]}
    assert approvals.outside(pr, add(BOTS, "loop", "opt_out: true\n")) is None
    assert approvals.outside(pr, OPTED) is not None  # no `bot` entry: a person's PR like any other


def test_a_policy_covered_spec_is_no_trail(landing: dict[str, Any]) -> None:
    mine = landing["pull"]
    mine["reviews"]["nodes"] = []
    mine["closingIssuesReferences"]["nodes"] = [
        specced(1, "kind:bug")
    ]  # `spec.auto` naming `bug` covers a bug
    assert approvals.outside(mine, OPTED) == OUTSIDE


# --- a repo without the loop's files ---


def test_without_loop_md_an_issue_needs_only_approved_spec_and_no_needs_owner(
    landing: dict[str, Any], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    for name in ("issue-tracker.md", "loop.md"):
        (tmp_path / "docs" / "agents" / name).unlink()
    bare = issue(1, "approved:spec")  # no category, component, size or approval record
    landing["pull"]["closingIssuesReferences"]["nodes"] = [bare]
    landing["pull"]["files"]["nodes"] = [{"path": ".github/ci.yml"}]
    assert gate(capsys, "check", "merge", "7") == (0, [])
    landing["pull"]["closingIssuesReferences"]["nodes"] = [
        issue(1, "approved:spec", "needs-owner"),
        issue(2, "size:XL"),
    ]
    assert gate(capsys, "check", "merge", "7") == (
        1,
        ["#1 waits for the owner (needs-owner)", "#2 lacks the `approved:spec` label"],
    )


def test_without_the_tracker_file_no_label_shape_is_checked(
    landing: dict[str, Any], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    (tmp_path / "docs" / "agents" / "issue-tracker.md").unlink()
    landing["pull"]["closingIssuesReferences"]["nodes"] = [approved(1)]  # no category or component
    assert approvals.main(["check", "build", "7"]) == 0
    assert capsys.readouterr().out == ""


def test_a_run_from_a_subdirectory_reads_the_checkouts_loop_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    committed(repo)
    agents = repo / "docs" / "agents"
    agents.mkdir(parents=True)
    (agents / "issue-tracker.md").write_text(TRACKER)
    (agents / "loop.md").write_text(loop_md("practice:\n  plan: writing-plans\n"))
    (repo / "src").mkdir()
    pr = pull()
    pr["closingIssuesReferences"]["nodes"] = [approved(1, "area:web", "kind:bug")]
    monkeypatch.setattr(approvals, "pull_request", lambda _n: pr)
    monkeypatch.setattr(approvals, "toplevel", TOPLEVEL)
    monkeypatch.chdir(repo / "src")
    assert (approvals.main(["check", "build", "7"]), capsys.readouterr().out) == (
        1,
        "#1 has no `## Plan` comment, and one is due\n",
    )


# --- check end to end, GitHub stubbed ---


def test_check_without_a_pr_or_an_issue_has_nothing_to_gate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A merge has nothing to gate before its PR; a build on a branch naming no issue is refused, since it needs an
    approved spec."""
    monkeypatch.setattr(approvals, "current_pr", lambda: None)
    monkeypatch.setattr(approvals, "git", lambda *_args: "spike\n")
    in_project(monkeypatch)
    assert (approvals.main(["check", "merge"]), capsys.readouterr().out) == (
        0,
        "no PR for this branch yet: nothing to gate\n",
    )
    assert (approvals.main(["check", "build"]), capsys.readouterr().out) == (
        1,
        (
            "no PR for this branch yet, and its name holds no issue number: pass --issue <n>, since a build needs an"
            " approved spec\n"
        ),
    )


def test_a_closing_line_with_no_such_issue_fails_with_the_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refused(_pr: int) -> dict[str, Any]:
        raise subprocess.CalledProcessError(
            1,
            "gh",
            stderr="gh: Could not resolve to an issue or pull request with the number of 9.\n",
        )

    monkeypatch.setattr(approvals, "pull_request", refused)
    in_project(monkeypatch)
    assert (approvals.main(["check", "build", "1"]), capsys.readouterr().out) == (
        1,
        "gh: Could not resolve to an issue or pull request with the number of 9.\n",
    )


def test_run_outside_a_git_checkout_fails_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(approvals, "toplevel", TOPLEVEL)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    monkeypatch.chdir(tmp_path)
    assert (approvals.main(["check", "build", "1"]), capsys.readouterr().out) == (
        1,
        "not in a git checkout: run from the repo's checkout\n",
    )


def test_check_reads_the_projects_own_components_and_categories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    agents = tmp_path / "docs" / "agents"
    agents.mkdir(parents=True)
    (agents / "issue-tracker.md").write_text(
        loop_md('components:\n  billing: invoices.\ncategories:\n  research: ""\n')
    )
    (agents / "loop.md").write_text(BARE)
    pr = pull()
    pr["closingIssuesReferences"]["nodes"] = [approved(1, "area:billing", "kind:research")]
    monkeypatch.setattr(approvals, "pull_request", lambda _pr: pr)
    monkeypatch.chdir(tmp_path)
    assert (approvals.main(["check", "build", "7"]), capsys.readouterr().out) == (0, "")


# --- local-ci: the local check, run on the PR head ---


@pytest.fixture
def checkout(
    github: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> dict[str, Any]:
    """A checkout at the PR head and clean, its git common dir under tmp_path; a test changes it, or what the check
    does to it. `runs` counts the checks run."""
    state: dict[str, Any] = {"head": HEAD, "status": "", "exit": 0, "after": None, "runs": 0}
    state["trees"] = {}  # a commit's tree, TREE unless named
    state["common"] = tmp_path / "common.git"
    state["loop"] = LOOP

    def git(*args: str) -> str:
        if args == ("rev-parse", "--path-format=absolute", "--git-common-dir"):
            return f"{state['common']}\n"
        if args[0] == "rev-parse" and args[-1].endswith("^{tree}"):
            commit = args[-1].removesuffix("^{tree}").replace("HEAD", state["head"])
            return state["trees"].get(commit, TREE) + "\n"
        if args[0] == "rev-parse":
            return state["head"] + "\n"
        if args[0] == "ls-files":
            return "H app.py\n"
        assert args == (
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignore-submodules=none",
        )
        return state["status"]

    def run(
        command: list[str], check: bool, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        assert (command, check) == (["mise", "run", "check:all"], False)
        state["runs"] += 1
        state["env"] = env
        if state["after"]:
            state["status"] = state["after"]
        return subprocess.CompletedProcess(command, state["exit"])

    monkeypatch.setattr(approvals, "pull_request", lambda _n: pull())
    monkeypatch.setattr(approvals, "git", git)
    monkeypatch.setattr(approvals.subprocess, "run", run)
    monkeypatch.setattr(approvals, "run_heavy", heavy_by_run)
    monkeypatch.setattr(approvals, "one_repo", lambda: None)  # the faked run serves the check alone
    # the host's load and swap are read by `sysctl` on macOS, which the faked run cannot answer; no test here reads them
    monkeypatch.setattr(approvals, "host_load", lambda: {"load_1m": None, "swap_used_mb": None})
    monkeypatch.setattr(approvals, "project", lambda: (None, state["loop"]))
    return state


def test_a_passing_check_on_the_clean_head_is_recorded(
    github: list[tuple[Any, ...]], checkout: dict[str, Any]
) -> None:
    assert approvals.main(["local-ci", "7", "--run"]) == 0
    assert github == [
        (
            "comment",
            7,
            f"{approvals.CHECKED}\nHead: {HEAD}\nCommand: mise run check:all\nEnv: default\n",
        )
    ]
    record = json.loads(record_at().read_text())
    assert {k: record[k] for k in ("head", "tree", "command", "exit")} == {
        "head": HEAD,
        "tree": TREE,
        "command": "mise run check:all",
        "exit": 0,
    }
    assert record["time"] and record["host"]


@pytest.mark.parametrize(
    ("change", "said"),
    [
        ({"head": "b" * 40}, f"the checkout is at {'b' * 40}, PR #7's head is {HEAD}"),
        ({"status": " M app.py\n"}, "the tree has changes"),
        ({"exit": 2}, "mise run check:all exited 2: nothing recorded"),
        (
            {"exit": 78, "loop": add(BARE, "loop", "machine_exit: 78\n")},
            "mise run check:all hit a machine fault (exit 78, loop.md's `machine_exit`): nothing recorded",
        ),
        ({"exit": 78}, "mise run check:all exited 78: nothing recorded"),
        ({"after": " M uv.lock\n"}, "mise run check:all changed the checkout"),
    ],
)
def test_a_check_off_the_head_failed_or_changing_the_tree_records_nothing(
    github: list[tuple[Any, ...]],
    checkout: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    change: dict[str, Any],
    said: str,
) -> None:
    checkout.update(change)
    assert approvals.main(["local-ci", "7", "--run"]) == 1
    assert github == []
    assert said in capsys.readouterr().out
    assert not (checkout["common"] / "approvals").exists()


@pytest.mark.parametrize("change", [{"exit": 2}, {"after": " M uv.lock\n"}])
def test_record_writes_only_a_passing_check_on_a_clean_unchanged_tree(
    github: list[tuple[Any, ...]],
    checkout: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    change: dict[str, Any],
) -> None:
    checkout.update(change)
    assert approvals.main(["local-ci", "--record"]) == 1
    assert not (checkout["common"] / "approvals").exists()
    # a failed run writes its failure status (test_approvals_failed_gate.py), never a pass
    assert not [s for found in hub.values() for s in found if s["state"] == "success"]
    checkout.update({"status": "", "exit": 0, "after": None})
    assert approvals.main(["local-ci", "--record"]) == 0
    assert record_at().exists()
    assert github == []  # no PR yet: nothing is posted


@pytest.mark.parametrize("code", [0, 2])
def test_record_on_a_checkout_with_changes_runs_the_check_and_records_nothing(
    github: list[tuple[Any, ...]],
    checkout: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
    code: int,
) -> None:
    """The recording command is the gate while iterating too: a run on changes is no pass on HEAD, so it records
    nothing, and the check's exit is the command's. A clean green run, the last one, records itself."""
    assert approvals.main(["local-ci", "--record"]) == 0
    checkout.update({"status": " M app.py\n", "exit": code})
    assert approvals.main(["local-ci", "--record"]) == code
    assert checkout["runs"] == 2  # a pass on HEAD's tree says nothing about the changes
    assert "on a checkout with changes, so nothing is recorded" in capsys.readouterr().out
    assert len(hub[HEAD]) == 1 and github == []


def test_record_on_a_tree_already_green_reuses_it_and_publishes_it_again(
    checkout: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    refs: dict[str, dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rerun once the head is pushed, it publishes a pass origin and GitHub could not take before, without running the
    check: the record branch again, and the `gate` status that shows it."""
    assert approvals.main(["local-ci", "--record"]) == 0
    hub.clear()  # the commit was not on GitHub yet
    refs.clear()  # nor origin
    assert approvals.main(["local-ci", "--record"]) == 0
    assert checkout["runs"] == 1
    assert [s["description"] for s in hub[HEAD]] == [
        f"Tree {TREE} on {HEAD[:12]}: mise run check:all"
    ]
    assert list(refs) == [approvals.record_name(TREE, "mise run check:all")]
    out = capsys.readouterr().out
    assert f"Reused by tree from the pass recorded on {HEAD}" in out


def test_a_recorded_pass_on_the_head_is_posted_without_running_again(
    github: list[tuple[Any, ...]], checkout: dict[str, Any]
) -> None:
    assert approvals.main(["local-ci", "--record"]) == 0
    record = json.loads(record_at().read_text())
    checkout["head"] = "c" * 40  # the checkout moved on: the record still proves the PR's head
    assert approvals.main(["local-ci", "7", "--run"]) == 0
    assert checkout["runs"] == 1
    assert github == [
        (
            "comment",
            7,
            (
                f"{approvals.CHECKED}\nHead: {HEAD}\nCommand: mise run check:all\nEnv: default\n"
                f"Reused by tree from the pass recorded on {HEAD} at {record['time']}, not run again.\n"
            ),
        )
    ]
    posted = [body for kind, _pr, body in github if kind == "comment"]
    merged = pull(reviews=(VERDICT,), comments=tuple(posted), labels=OWNED, checks=())
    assert merge(merged, [approved(1, "area:web")], add(LOOP, "loop", "ci: false\n")) == []


def test_a_local_ci_comment_names_no_host(
    checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run and recorded on a host named for its owner, the pass is posted without it, and still proves the head."""
    sent = posts(monkeypatch)
    assert approvals.main(["local-ci", "--record"]) == 0
    record = json.loads(record_at().read_text())
    assert record["host"] == "owner-MacBook.local"  # the local record keeps it
    assert approvals.main(["local-ci", "7", "--run"]) == 0
    [body] = sent
    assert "owner-MacBook" not in body and "/Users/owner" not in body
    merged = pull(reviews=(VERDICT,), comments=(body,), labels=OWNED, checks=())
    assert merge(merged, [approved(1, "area:web")], add(LOOP, "loop", "ci: false\n")) == []


@pytest.mark.parametrize(
    ("file", "record"),
    [
        ("f" * 40, {"tree": "f" * 40, "command": "mise run check:all", "exit": 0}),
        (TREE, {"tree": TREE, "command": "mise run check", "exit": 0}),
        (TREE, {"tree": TREE, "command": "mise run check:all", "exit": 2}),
        (TREE, {"tree": "f" * 40, "command": "mise run check:all", "exit": 0}),
    ],
    ids=["another tree", "another command", "a failed run", "a record naming another tree"],
)
def test_a_record_for_another_tree_or_command_or_a_failed_run_runs_the_check(
    github: list[tuple[Any, ...]], checkout: dict[str, Any], file: str, record: dict[str, Any]
) -> None:
    where = approvals.recorded(file, "mise run check:all")
    where.parent.mkdir(parents=True)
    where.write_text(json.dumps({**record, "head": HEAD, "env": {}, "time": "t", "host": "h"}))
    assert approvals.main(["local-ci", "7", "--run"]) == 0
    assert checkout["runs"] == 1
    assert github == [
        (
            "comment",
            7,
            f"{approvals.CHECKED}\nHead: {HEAD}\nCommand: mise run check:all\nEnv: default\n",
        )
    ]


def test_local_ci_with_no_record_refuses_without_running_the_gate(
    github: list[tuple[Any, ...]], checkout: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert approvals.main(["local-ci", "7"]) == 1
    out = capsys.readouterr().out
    name = approvals.RECORD_BRANCH.format(approvals.record_name(TREE, "mise run check:all"))
    assert (checkout["runs"], github) == (0, [])
    assert HEAD in out and TREE in out and name in out and "--run" in out


def test_local_ci_with_a_record_missing_its_time_refuses_and_names_the_field(
    github: list[tuple[Any, ...]], checkout: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    where = approvals.recorded(TREE, "mise run check:all")
    where.parent.mkdir(parents=True)
    where.write_text(
        json.dumps(
            {
                "head": HEAD,
                "tree": TREE,
                "command": "mise run check:all",
                "exit": 0,
                "env": {},
                "host": "h",
            }
        )
    )
    assert approvals.main(["local-ci", "7"]) == 1
    assert (checkout["runs"], github) == (0, [])
    assert "lacks its `time` field" in capsys.readouterr().out


def test_local_ci_run_alone_is_refused_without_a_pr(
    github: list[tuple[Any, ...]], checkout: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert approvals.main(["local-ci", "--record", "--run"]) == 1
    assert checkout["runs"] == 0


ENV_LOOP = add(LOOP, "loop", "local_ci_env:\n  - GATE_UI_TESTS\n  - GATE_SCOPE\n")


def record_at(head: str = HEAD) -> Path:
    """Where this clone records a pass on `head`'s tree under the current env."""
    return approvals.recorded(approvals.tree_of(head), approvals.gate()[0])


def recorded_pass(checkout: dict[str, Any]) -> dict[str, Any]:
    return json.loads(record_at().read_text())


def test_the_gate_runs_with_the_names_local_ci_env_lists_cleared_so_an_ambient_tier_changes_no_pass(
    github: list[tuple[Any, ...]], checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host's ambient GATE_UI_TESTS is no part of the gate: it is cleared for the run, and the pass is reused
    whatever the next host has set."""
    checkout["loop"] = ENV_LOOP
    monkeypatch.setenv("GATE_UI_TESTS", "0")
    monkeypatch.setenv("UNLISTED", "kept")
    assert approvals.main(["local-ci", "--record"]) == 0
    assert "GATE_UI_TESTS" not in checkout["env"] and checkout["env"]["UNLISTED"] == "kept"
    assert recorded_pass(checkout)["env"] == {}
    monkeypatch.setenv("GATE_UI_TESTS", "full")
    assert approvals.main(["local-ci", "7", "--run"]) == 0
    assert checkout["runs"] == 1
    [(_, _, body)] = github
    assert "Env: default\nReused by tree" in body


def test_a_gate_that_sets_a_name_runs_with_it_and_names_it(
    github: list[tuple[Any, ...]], checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout["loop"] = add(ENV_LOOP, "loop", "gate: GATE_UI_TESTS=0 mise run check:all\n")
    monkeypatch.setenv("GATE_UI_TESTS", "full")
    monkeypatch.setenv("GATE_SCOPE", "changed")
    assert approvals.main(["local-ci", "7", "--run"]) == 0
    assert checkout["env"]["GATE_UI_TESTS"] == "0" and "GATE_SCOPE" not in checkout["env"]
    assert github == [
        (
            "comment",
            7,
            f"{approvals.CHECKED}\nHead: {HEAD}\nCommand: GATE_UI_TESTS=0 mise run check:all\nEnv: GATE_UI_TESTS=0\n",
        )
    ]


def test_record_head_proof_runs_the_gate_with_the_callers_tier_over_the_gate_lines_and_publishes_it(
    github: list[tuple[Any, ...]],
    checkout: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the head proof's names pass through, and its pass is no plain gate pass: the plain gate runs again."""
    gated = add(ENV_LOOP, "loop", "gate: GATE_UI_TESTS=0 mise run check:all\n")
    checkout["loop"] = add(gated, "loop", HEAD_PROOF)
    monkeypatch.setenv("GATE_UI_TESTS", "smoke")
    monkeypatch.setenv("GATE_SCOPE", "changed")
    assert approvals.main(["local-ci", "--record", "--head-proof"]) == 0
    assert checkout["env"]["GATE_UI_TESTS"] == "smoke" and "GATE_SCOPE" not in checkout["env"]
    assert [(s["context"], s["description"]) for s in hub[HEAD]] == [
        ("gate-ui", f"Tree {TREE} on {HEAD[:12]}: GATE_UI_TESTS=smoke")
    ]
    assert approvals.main(["local-ci", "--record", "--head-proof"]) == 0
    assert (checkout["runs"], len(hub[HEAD])) == (1, 1)  # reused, and published once
    assert approvals.main(["local-ci", "--record"]) == 0
    assert checkout["runs"] == 2 and checkout["env"]["GATE_UI_TESTS"] == "0"
    assert [s["context"] for s in hub[HEAD]] == ["gate", "gate-ui"]
    assert github == []


def test_record_head_proof_on_a_checkout_with_changes_runs_the_callers_tier_and_publishes_nothing(
    checkout: dict[str, Any], hub: dict[str, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    gated = add(ENV_LOOP, "loop", "gate: GATE_UI_TESTS=0 mise run check:all\n")
    checkout.update(loop=add(gated, "loop", HEAD_PROOF), status=" M app.py\n")
    monkeypatch.setenv("GATE_UI_TESTS", "smoke")
    assert approvals.main(["local-ci", "--record", "--head-proof"]) == 0
    assert checkout["env"]["GATE_UI_TESTS"] == "smoke"
    assert hub == {}


@pytest.mark.parametrize(
    ("loop", "argv"),
    [
        (LOOP, ["local-ci", "--record", "--head-proof"]),
        (add(LOOP, "loop", HEAD_PROOF), ["local-ci", "7", "--head-proof"]),
        (
            add(LOOP, "loop", "head_proof:\n  status: gate-ui\n  env:\n    - 1GATE\n"),
            ["local-ci", "--record", "--head-proof"],
        ),
        *(
            (
                add(LOOP, "loop", f"head_proof:\n  status: {taken}\n  env:\n    - GATE_UI_TESTS\n"),
                ["local-ci", "--record", "--head-proof"],
            )
            for taken in (approvals.PUBLISHED, approvals.OWNER_GO, approvals.APPROVALS)
        ),
    ],
    ids=[
        "no head_proof in loop.md",
        "with a PR",
        "an env naming no variable",
        "the gate's own status",
        "the owner's go",
        "the approvals status",
    ],
)
def test_head_proof_without_the_setting_or_with_a_pr_is_refused(
    github: list[tuple[Any, ...]],
    checkout: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    loop: str,
    argv: list[str],
) -> None:
    checkout["loop"] = loop
    assert approvals.main(argv) == 1
    assert (checkout["runs"], github, hub) == (0, [], {})


def test_head_proof_reuses_a_gate_pass_published_under_the_command_it_runs_naming_its_values(
    checkout: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    refs: dict[str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record from when the gate line itself set `smoke` is a pass of the command the head proof runs."""
    gated = add(ENV_LOOP, "loop", "gate: GATE_UI_TESTS=0 mise run check:all\n")
    checkout["loop"] = add(gated, "loop", HEAD_PROOF)
    monkeypatch.setenv("GATE_UI_TESTS", "smoke")
    command, _, env = approvals.gate(["GATE_UI_TESTS"])
    assert command == "GATE_UI_TESTS=smoke mise run check:all"
    pushed_pass(refs, HEAD, TREE, command, env)
    assert approvals.main(["local-ci", "--record", "--head-proof"]) == 0
    assert checkout["runs"] == 0
    assert hub[HEAD][0] == status("gate-ui", f"Tree {TREE} on {HEAD[:12]}: GATE_UI_TESTS=smoke")


def test_a_head_proof_description_says_default_for_no_value_and_fits_github() -> None:
    record = {"tree": TREE, "head": HEAD, "env": {"LONG": "x" * 200}}
    assert approvals.proof_line(record, ["UNSET"]) == f"Tree {TREE} on {HEAD[:12]}: default"
    assert len(approvals.proof_line(record, ["UNSET", "LONG"])) == approvals.DESCRIBED


def test_a_head_proof_description_names_no_home_directory() -> None:
    scope = f"{Path.home()}/work/app"
    record = {"tree": TREE, "head": HEAD, "env": {"GATE_SCOPE": scope}}
    line = approvals.proof_line(record, ["GATE_SCOPE"])
    assert str(Path.home()) not in line
    assert line.endswith("GATE_SCOPE=~/work/app")


def test_a_name_added_to_local_ci_env_keeps_every_pass(
    github: list[tuple[Any, ...]], checkout: dict[str, Any]
) -> None:
    assert approvals.main(["local-ci", "--record"]) == 0
    checkout["loop"] = ENV_LOOP
    assert approvals.reusable(HEAD) is not None
    assert approvals.main(["local-ci", "7", "--run"]) == 0
    assert checkout["runs"] == 1


@pytest.mark.parametrize(
    "gate",
    [
        "GATE_UI_TESTS=0",
        "1GATE=0 mise run check:all",
        "' '",
        "'mise run a && mise run b'",
        "'mise run a;mise run b'",
        "'mise run a | tee log'",
    ],
    ids=["nothing to run", "no variable name", "empty", "a chain", "a sequence", "a pipe"],
)
def test_a_gate_that_runs_nothing_or_sets_no_variable_is_refused(
    github: list[tuple[Any, ...]],
    checkout: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    gate: str,
) -> None:
    checkout["loop"] = add(LOOP, "loop", f"gate: {gate}\n")
    assert approvals.main(["local-ci", "7", "--run"]) == 1
    assert (checkout["runs"], github) == (0, [])
    assert "loop.md's `gate`" in capsys.readouterr().out


def test_a_gate_whose_command_is_not_there_is_refused_naming_it(
    github: list[tuple[Any, ...]],
    checkout: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def missing(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError(2, "No such file or directory", "misee")

    monkeypatch.setattr(approvals.subprocess, "run", missing)
    monkeypatch.setattr(approvals, "run_heavy", heavy_by_run)
    assert approvals.main(["local-ci", "--record"]) == 1
    assert "can't run: [Errno 2] No such file or directory: 'misee'" in capsys.readouterr().out
    assert not (checkout["common"] / "approvals").exists()


def test_a_hash_inside_a_word_is_part_of_it(checkout: dict[str, Any]) -> None:
    checkout["loop"] = add(LOOP, "loop", 'gate: "GATE_X=a#b mise run check:all#x"\n')
    assert approvals.gate()[1:] == (["mise", "run", "check:all#x"], {"GATE_X": "a#b"})


def test_a_variable_set_twice_runs_the_command_with_the_last_value(
    checkout: dict[str, Any],
) -> None:
    checkout["loop"] = add(LOOP, "loop", "gate: A=1 A=2 mise run check:all\n")
    assert approvals.gate() == (
        "A=1 A=2 mise run check:all",
        ["mise", "run", "check:all"],
        {"A": "2"},
    )


def test_a_gate_too_long_for_a_status_publishes_its_digest_and_is_reused_from_it(
    checkout: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    """GitHub refuses a description over 140 characters: the command's digest stands in, and still matches it."""
    long = "GATE_SCOPE=full GATE_UI_TESTS=0 GATE_ANDROID=skip GATE_LINUX=skip mise run check:all"
    checkout["loop"] = add(LOOP, "loop", f"gate: {long}\n")
    assert approvals.main(["local-ci", "--record"]) == 0
    [published] = hub[HEAD]
    assert len(published["description"]) <= approvals.DESCRIBED
    assert (
        published["description"] == f"Tree {TREE} on {HEAD[:12]}: sha256 {approvals.gate_key(long)}"
    )
    record_at().unlink()  # another clone: only the record branch
    assert approvals.reusable(HEAD)["published"] is True


def test_a_record_without_env_runs_the_check_again(
    github: list[tuple[Any, ...]], checkout: dict[str, Any]
) -> None:
    where = record_at()
    where.parent.mkdir(parents=True)
    old = {
        "head": HEAD,
        "tree": TREE,
        "command": "mise run check:all",
        "exit": 0,
        "time": "t",
        "host": "h",
    }
    where.write_text(json.dumps(old))  # as 0.23.1 wrote it, but for the tree
    assert approvals.main(["local-ci", "7", "--run"]) == 0
    assert checkout["runs"] == 1
    assert recorded_pass(checkout)["env"] == {}


@pytest.mark.parametrize(
    "drop",
    [{"time": None}, {"head": None}, {"time": 1}, {"env": []}],
    ids=["no time", "no commit", "a number for time", "env not a dict"],
)
def test_an_incomplete_record_runs_the_check_again(
    github: list[tuple[Any, ...]], checkout: dict[str, Any], drop: dict[str, Any]
) -> None:
    where = record_at()
    where.parent.mkdir(parents=True)
    full = {
        "head": HEAD,
        "tree": TREE,
        "command": "mise run check:all",
        "exit": 0,
        "env": {},
        "time": "t",
        "host": "h",
    }
    record = {k: v for k, v in {**full, **drop}.items() if v is not None}
    where.write_text(json.dumps(record))
    assert approvals.main(["local-ci", "7", "--run"]) == 0
    assert checkout["runs"] == 1
    assert github == [
        (
            "comment",
            7,
            f"{approvals.CHECKED}\nHead: {HEAD}\nCommand: mise run check:all\nEnv: default\n",
        )
    ]


@pytest.mark.parametrize("names", ["\n  - GATE-UI", "\n  - GATE_UI GATE_SCOPE"])
def test_a_local_ci_env_that_is_no_list_of_variable_names_is_refused(
    github: list[tuple[Any, ...]],
    checkout: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    names: str,
) -> None:
    checkout["loop"] = add(LOOP, "loop", f"local_ci_env:{names}\n")
    assert approvals.main(["local-ci", "7", "--run"]) == 1
    assert (checkout["runs"], github) == (0, [])
    assert "`local_ci_env`" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["local-ci"], ["local-ci", "7", "--record"]])
def test_local_ci_takes_a_pr_or_record(
    github: list[tuple[Any, ...]], checkout: dict[str, Any], argv: list[str]
) -> None:
    assert approvals.main(argv) == 1
    assert (checkout["runs"], github) == (0, [])


def test_a_local_ci_record_is_the_merge_proof_without_ci(
    github: list[tuple[Any, ...]], checkout: dict[str, Any]
) -> None:
    assert approvals.main(["local-ci", "7", "--run"]) == 0
    posted = [body for kind, _pr, body in github if kind == "comment"]
    merged = pull(reviews=(VERDICT,), comments=tuple(posted), labels=OWNED, checks=())
    assert merge(merged, [approved(1, "area:web")], add(LOOP, "loop", "ci: false\n")) == []


def hidden_untracked(repo: Path) -> None:
    git_in(repo, "config", "status.showUntrackedFiles", "no")
    (repo / "new.py").write_text("")


def hidden_submodule_edit(repo: Path) -> None:
    committed(repo.parent / "lib")
    git_in(repo, "submodule", "add", "-q", str(repo.parent / "lib"), "lib")
    git_in(repo, "commit", "-qm", "lib")
    git_in(repo, "config", "submodule.lib.ignore", "all")
    (repo / "lib" / "app.py").write_text("value = 2\n")


def hidden_by_ignore_stat(repo: Path) -> None:
    git_in(repo, "config", "core.ignoreStat", "true")
    (repo / "lib.py").write_text("value = 1\n")
    git_in(repo, "add", "lib.py")  # core.ignoreStat marks it assume-unchanged
    git_in(repo, "commit", "-qm", "lib")
    (repo / "lib.py").write_text("value = 2\n")


@pytest.mark.parametrize("hide", [hidden_untracked, hidden_submodule_edit, hidden_by_ignore_stat])
def test_a_change_the_status_config_hides_still_records_nothing(
    github: list[tuple[Any, ...]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    hide: Any,
) -> None:
    repo = tmp_path / "repo"
    committed(repo)
    hide(repo)
    head = git_in(repo, "rev-parse", "HEAD").strip()
    monkeypatch.setattr(approvals, "pull_request", lambda _n: {**pull(), "headRefOid": head})
    monkeypatch.chdir(repo)
    assert approvals.main(["local-ci", "7", "--run"]) == 1
    assert github == []
    assert "the tree has changes" in capsys.readouterr().out


def test_a_record_made_in_one_worktree_is_reused_from_another(
    github: list[tuple[Any, ...]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    head = committed(repo)
    git_in(repo, "worktree", "add", "-q", "--detach", str(tmp_path / "tree"), head)
    monkeypatch.setattr(approvals, "LOCAL_CI", ["true"])
    monkeypatch.setattr(approvals, "one_repo", lambda: None)
    monkeypatch.chdir(tmp_path / "tree")
    assert approvals.main(["local-ci", "--record"]) == 0
    assert git_in(repo, "status", "--porcelain", "--untracked-files=all") == ""  # nothing to commit
    monkeypatch.chdir(repo)
    assert approvals.reusable(head)["head"] == head
    assert approvals.reusable("c" * 40) is None


def published_on_a(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: tuple[str, ...] = ("true",)
) -> str:
    """Host A records and publishes a pass of `gate` (default `true`) on a pushed head; the test then runs in host B, a clone made
    before that head was pushed, with no record of it. Returns the head."""
    origin = tmp_path / "origin.git"
    git_in(tmp_path, "init", "-q", "--bare", str(origin))
    a, b = tmp_path / "a", tmp_path / "b"
    committed(a)
    git_in(a, "remote", "add", "origin", str(origin))
    git_in(a, "push", "-q", "origin", "HEAD:refs/heads/main")
    git_in(tmp_path, "clone", "-q", str(origin), str(b))
    (a / "app.py").write_text("value = 2\n")
    git_in(a, "commit", "-qam", "change")
    head = git_in(a, "rev-parse", "HEAD").strip()
    git_in(a, "push", "-q", "origin", "HEAD:refs/heads/feature")
    monkeypatch.setattr(approvals, "one_repo", lambda: None)
    monkeypatch.setattr(approvals, "pull_request", lambda _n: {**pull(), "headRefOid": head})
    monkeypatch.chdir(a)
    monkeypatch.setattr(approvals, "LOCAL_CI", list(gate))
    assert approvals.main(["local-ci", "--record"]) == 0
    monkeypatch.chdir(b)
    return head


def shallow_clone(tmp_path: Path, behind: int = 3, depth: int = 1) -> tuple[Path, str, str]:
    """A bare origin whose main has one commit and whose `feature` is `behind` commits past it, and a `--depth`
    clone of `feature` that holds none of main. Returns the clone, the fork point and the clone's head."""
    origin, work, clone = tmp_path / "origin.git", tmp_path / "work", tmp_path / "clone"
    git_in(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    fork = committed(work)
    git_in(work, "branch", "-M", "main")
    git_in(work, "remote", "add", "origin", str(origin))
    git_in(work, "push", "-q", "origin", "main")
    # One `git fast-import` writes the history: no per-commit `git commit` to race a background gc or maintenance
    # job for the index lock, sign with a host's key or need a host identity (#970).
    stream = f"reset refs/heads/feature\nfrom {fork}\n" + "".join(
        f"commit refs/heads/feature\ncommitter loop <loop@example.com> {1_700_000_000 + n} +0000\n"
        f"data {len(f'change {n}')}\nchange {n}\nM 100644 inline app.py\n"
        f"data {len(f'value = {n + 2}' + chr(10))}\nvalue = {n + 2}\n\n"
        for n in range(behind)
    )
    subprocess.run(
        ["git", "-C", str(work), "-c", "gc.auto=0", "-c", "maintenance.auto=false", "fast-import", "--quiet"],
        input=stream, check=True, capture_output=True, text=True,
    )  # fmt: skip
    git_in(work, "push", "-q", "origin", "refs/heads/feature:refs/heads/feature")
    git_in(
        tmp_path, "clone", "-q", "--depth", str(depth), "--branch", "feature",
        f"file://{origin}", str(clone),
    )  # fmt: skip
    return clone, fork, git_in(clone, "rev-parse", "HEAD").strip()


def test_a_shallow_clone_is_deepened_until_the_merge_base_with_the_base_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clone, fork, head = shallow_clone(tmp_path)
    monkeypatch.chdir(clone)
    monkeypatch.setattr(approvals, "pull_request", lambda _n: {**pull(), "baseRefName": "main"})
    assert git_in(clone, "rev-parse", "--is-shallow-repository").strip() == "true"
    approvals.deepened(None)
    assert git_in(clone, "merge-base", "origin/main", "HEAD").strip() == fork
    assert git_in(clone, "rev-parse", "HEAD").strip() == head
    err = capsys.readouterr().err.splitlines()
    assert err and all(line.startswith("local-ci: ") for line in err)
    assert any("--deepen" in line for line in err)


def test_a_pr_s_base_is_the_one_a_shallow_clone_is_deepened_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone, fork, _ = shallow_clone(tmp_path)
    origin = tmp_path / "origin.git"
    git_in(origin, "branch", "release", fork)
    monkeypatch.chdir(clone)
    monkeypatch.setattr(approvals, "pull_request", lambda _n: {**pull(), "baseRefName": "release"})
    approvals.deepened(7)
    assert git_in(clone, "merge-base", "origin/release", "HEAD").strip() == fork


def test_a_history_deeper_than_one_step_unshallows_when_deepening_falls_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clone, fork, _ = shallow_clone(tmp_path, behind=400)
    monkeypatch.chdir(clone)
    approvals.deepened(None)
    assert git_in(clone, "merge-base", "origin/main", "HEAD").strip() == fork
    assert "--unshallow" in capsys.readouterr().err


def test_a_full_clone_runs_no_deepening_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clone, _, _ = shallow_clone(tmp_path)
    git_in(clone, "fetch", "-q", "--unshallow", "origin")
    calls: list[list[str]] = []
    real = subprocess.run

    def traced(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(list(cmd))
        return real(cmd, *args, **kwargs)

    monkeypatch.chdir(clone)
    monkeypatch.setattr(subprocess, "run", traced)
    approvals.deepened(None)
    assert not [c for c in calls if "--deepen" in c or "--unshallow" in c or "fetch" in c]
    assert capsys.readouterr().err == ""


def test_local_ci_record_in_a_shallow_clone_gives_the_gate_a_merge_base_and_records_the_head_tree(
    github: list[tuple[Any, ...]],
    hub: dict[str, list[dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clone, fork, head = shallow_clone(tmp_path)
    seen = tmp_path / "seen"
    script = tmp_path / "gate.sh"
    script.write_text(f"#!/bin/sh\ngit merge-base origin/main HEAD > {seen}\n")
    script.chmod(0o755)
    monkeypatch.setattr(approvals, "one_repo", lambda: None)
    monkeypatch.chdir(clone)
    monkeypatch.setattr(approvals, "LOCAL_CI", [str(script)])
    assert approvals.main(["local-ci", "--record"]) == 0
    assert seen.read_text().strip() == fork
    [record] = (clone / ".git" / "approvals" / "local-ci").glob("*.json")
    assert (
        json.loads(record.read_text())["tree"] == git_in(clone, "rev-parse", "HEAD^{tree}").strip()
    )
    assert git_in(clone, "rev-parse", "HEAD").strip() == head


def test_a_shallow_clone_that_already_reaches_the_base_is_not_deepened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # the clone holds the fork point, but not origin/main yet
    clone, fork, _ = shallow_clone(tmp_path, depth=4)
    monkeypatch.chdir(clone)
    approvals.deepened(None)
    assert git_in(clone, "merge-base", "origin/main", "HEAD").strip() == fork
    assert git_in(clone, "rev-parse", "--is-shallow-repository").strip() == "true"
    err = capsys.readouterr().err
    assert "--deepen" not in err and "--unshallow" not in err


def test_local_ci_record_on_a_changed_shallow_checkout_gives_the_gate_a_merge_base_and_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clone, fork, _ = shallow_clone(tmp_path)
    (clone / "app.py").write_text("value = 99\n")
    seen = tmp_path / "seen"
    script = tmp_path / "gate.sh"
    script.write_text(f"#!/bin/sh\ngit merge-base origin/main HEAD > {seen}\n")
    script.chmod(0o755)
    monkeypatch.setattr(approvals, "one_repo", lambda: None)
    monkeypatch.chdir(clone)
    monkeypatch.setattr(approvals, "LOCAL_CI", [str(script)])
    assert approvals.main(["local-ci", "--record"]) == 0
    assert seen.read_text().strip() == fork
    assert "nothing is recorded" in capsys.readouterr().out
    assert not list((clone / ".git" / "approvals" / "local-ci").glob("*.json"))


def test_a_pass_recorded_on_one_host_is_reused_on_another_for_the_same_head_and_env(
    github: list[tuple[Any, ...]],
    hub: dict[str, list[dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host B finds A's pushed pass and posts it without running the check, which would fail there."""
    head = published_on_a(tmp_path, monkeypatch)
    line = f"Tree {approvals.tree_of(head)} on {head[:12]}: true"
    assert [s["description"] for s in hub[head]] == [line]

    def never(*_args: Any) -> Any:
        raise AssertionError("host B ran the check")

    monkeypatch.setattr(approvals, "run_local_ci", never)
    assert approvals.reusable(head)["head"] == head  # local-ci <pr> reads this
    assert approvals.main(["local-ci", "7", "--run"]) == 0
    [(_, _, body)] = github
    assert f"Reused by tree from the pass recorded on {head} at 2026-" in body
    assert {s["description"] for s in hub[head]} == {line}  # B shows the same pass


def test_a_pass_published_under_another_command_is_not_reused_on_another_host(
    github: list[tuple[Any, ...]],
    hub: dict[str, list[dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host B has no record to compare the command with, so the published status's key alone must tell: a gate whose
    command changed since A's pass runs again."""
    head = published_on_a(tmp_path, monkeypatch)
    no_ci = add(LOOP, "loop", "ci: false\n")
    posted = note(f"{approvals.CHECKED}\nHead: {head}\nCommand: true\nEnv: default\n")
    merged = {
        **pull(reviews=(verdict_on(head),), comments=(posted,), labels=OWNED, checks=()),
        "headRefOid": head,
    }
    assert merge(merged, [approved(1, "area:web")], no_ci) == []
    monkeypatch.setattr(approvals, "LOCAL_CI", ["true", "--another-tier"])
    ran: list[str | None] = []

    def run_local_ci(pr: int | None, at: str | None, _passed: Any = ()) -> dict[str, Any]:
        ran.append(at)
        raise approvals.Refused("the check would run")

    monkeypatch.setattr(approvals, "run_local_ci", run_local_ci)
    assert approvals.reusable(head) is None
    # check merge reads no local-ci pass (#1872): only `local-ci` runs the gate again
    assert merge(merged, [approved(1, "area:web")], no_ci) == []
    assert approvals.main(["local-ci", "7", "--run"]) == 1
    assert (ran, github) == ([head], [])


def test_a_green_gate_status_with_no_record_branch_is_no_pass(
    github: list[tuple[Any, ...]],
    checkout: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 2: a `gate` status naming the head's tree and today's command, by a writer, is only shown: with no
    record branch and no record in this clone, the check runs."""
    monkeypatch.setattr(approvals, "writer", lambda login: login == "maintainer")
    hub[HEAD] = [status("gate", f"Tree {TREE} on {HEAD[:12]}: mise run check:all", by="maintainer")]
    assert approvals.reusable(HEAD) is None
    assert approvals.main(["local-ci", "7", "--run"]) == 0
    assert checkout["runs"] == 1


def test_two_commits_with_one_tree_and_env_share_a_pass_and_another_tree_runs_it(
    github: list[tuple[Any, ...]], checkout: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    """A rebase or amended message keeps the tree: the second commit reuses the first's pass, naming it."""
    assert approvals.main(["local-ci", "--record"]) == 0
    amended = "c" * 40
    checkout["head"] = amended
    assert approvals.main(["local-ci", "--record"]) == 0
    assert checkout["runs"] == 1
    assert hub[amended][0]["description"] == f"Tree {TREE} on {HEAD[:12]}: mise run check:all"
    checkout["trees"][amended] = "f" * 40  # the same commit name, another tree
    hub.clear()
    assert approvals.main(["local-ci", "--record"]) == 0
    assert checkout["runs"] == 2


def test_without_github_or_origin_the_pass_is_recorded_here_and_says_what_was_not_published(
    github: list[tuple[Any, ...]],
    checkout: dict[str, Any],
    refs: dict[str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A refused status is said and is no error, since the record branch is the pass; a refused push leaves the pass
    in this clone, which still reuses it."""

    def offline(*_args: Any) -> Any:
        raise subprocess.CalledProcessError(1, "gh", stderr="error connecting to api.github.com")

    monkeypatch.setattr(approvals, "statuses", offline)
    monkeypatch.setattr(approvals, "post_status", offline)
    assert approvals.main(["local-ci", "--record"]) == 0
    out = capsys.readouterr().out
    assert "published in origin as the record branch refs/heads/loop-gate/" in out
    assert (
        "the `gate` status, which only shows the pass on the PR, was not written: error connecting"
        in out
    )
    refs.clear()

    def no_origin(*_args: Any) -> Any:
        raise subprocess.CalledProcessError(
            1, "git push", stderr="could not read from remote repository"
        )

    monkeypatch.setattr(approvals, "push_record", no_origin)
    assert approvals.main(["local-ci", "--record"]) == 0
    assert (
        "not pushed to origin, so only this clone reuses it: could not read from remote repository"
        in capsys.readouterr().out
    )
    assert record_at().exists()
    assert approvals.reusable(HEAD)["head"] == HEAD


def gate_faked(
    tmp_path: Path, exit_code: int = 0, seconds: float = 0, held: bool = False
) -> tuple[Path, str, dict[str, str]]:
    """A clean repo and an environment whose `mise` is a fake gate: it sleeps (`held`: until the test creates
    `tmp_path / "release"`, so it ends when the test says; it says `gate started` and appends to `starts` first, and
    gives up after 60 s, so a red test leaves no process looping), prints, counts its runs, exits."""
    repo = tmp_path / "repo"
    head = committed(repo)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "mise"
    fake.write_text(
        "#!/bin/sh\n"
        + (
            f"echo gate started\necho started >> {tmp_path / 'starts'}\n"
            f"end=$(( $(date +%s) + 60 ))\n"
            f'while [ ! -e {tmp_path / "release"} ] && [ "$(date +%s)" -lt "$end" ]; do sleep 0.02; done\n'
            if held
            else f"sleep {seconds}\n"
        )
        + f"echo gate done\necho run >> {tmp_path / 'runs'}\nexit {exit_code}\n"
    )
    fake.chmod(0o755)
    return repo, head, {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


# what approvals.py prints once reading the policy in a checkout without origin, as gate_faked's repo is (#1047)
UNBASED = "approvals.py: no origin/<base> ref, so the policy is read from the working tree\n"


def caller(repo: Path, env: dict[str, str], *args: str) -> subprocess.Popen[str]:
    """`local-ci` run as an agent's harness runs it: in a process group of its own, not leading a session."""
    return subprocess.Popen(
        [sys.executable, str(SCRIPTS / "approvals.py"), "local-ci", *args],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        process_group=0,
    )


def polled(until: Callable[[], bool], seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while not until():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def test_a_killed_caller_leaves_the_detached_check_to_finish_and_record(tmp_path: Path) -> None:
    repo, head, env = gate_faked(tmp_path, held=True)
    common = repo / ".git" / "approvals" / "local-ci"
    run = caller(repo, env, "--record")
    pid = common / f"{head}.pid"
    try:
        # detach() fills the pid file only after the child's setsid, so the kill can't reach it (#732)
        polled(lambda: pid.exists() and pid.read_text())
        os.killpg(run.pid, signal.SIGKILL)
        run.wait()
    finally:
        (
            tmp_path / "release"
        ).touch()  # the gate was held through the kill, and ends now (or the test failed)
    polled(lambda: (common / f"{head}.exit").exists())
    assert (common / f"{head}.exit").read_text() == "0\n"
    [record] = common.glob("*.json")  # keyed by tree and env
    assert json.loads(record.read_text())["exit"] == 0
    assert "gate done" in (common / f"{head}.log").read_text()
    status = subprocess.run(
        [sys.executable, str(SCRIPTS / "approvals.py"), "local-ci", "--status", head],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (status.returncode, status.stdout.split(":")[0]) == (0, f"passed on {head}")


def test_status_of_a_detached_head_proof_run_reads_the_command_it_ran(tmp_path: Path) -> None:
    """Through the real detached entry point: the run records its command beside its exit code, so --status finds a
    head proof under the caller's tier, and a plain --record run still reads under the plain gate."""
    repo, head, env = gate_faked(tmp_path)
    loop = add(
        add(ENV_LOOP, "loop", "gate: GATE_UI_TESTS=0 mise run check:all\n"), "loop", HEAD_PROOF
    )
    (repo / "docs" / "agents").mkdir(parents=True)
    (repo / "docs" / "agents" / "loop.md").write_text(loop)
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-qm", "loop")
    head = git_in(repo, "rev-parse", "HEAD").strip()
    common = repo / ".git" / "approvals" / "local-ci"

    def status() -> str:
        run = subprocess.run(
            [sys.executable, str(SCRIPTS / "approvals.py"), "local-ci", "--status", head],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert run.returncode == 0, run.stdout + run.stderr
        return run.stdout

    proof = {**env, "GATE_UI_TESTS": "smoke"}
    first = caller(repo, proof, "--record", "--head-proof")
    out, err = first.communicate(timeout=60)
    assert first.returncode == 0, out + err
    polled(lambda: (common / f"{head}.exit").exists())
    assert status().startswith(
        f"passed on {head} under `GATE_UI_TESTS=smoke mise run check:all`: log"
    )
    # a plain run, as before: the plain gate's command and record
    plain = {k: v for k, v in env.items() if k != "GATE_UI_TESTS"}
    assert caller(repo, plain, "--record").wait(timeout=60) == 0
    runs = tmp_path / "runs"
    polled(lambda: runs.read_text().count("run") == 2 and (common / f"{head}.exit").exists())
    assert status().startswith(f"passed on {head}: log")


def test_status_never_says_passed_for_a_run_that_recorded_nothing(tmp_path: Path) -> None:
    """A --record on a checkout with changes exits 0 without a pass on HEAD: --status must not call HEAD passed."""
    repo, head, env = gate_faked(tmp_path)
    (repo / "app.py").write_text("value = 3\n")
    assert caller(repo, env, "--record").wait(timeout=60) == 0
    status = subprocess.run(
        [sys.executable, str(SCRIPTS / "approvals.py"), "local-ci", "--status", head],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (status.returncode, status.stdout.split(":")[0]) == (
        1,
        f"exited 0 without recording a pass on {head} under the gate's command",
    )


@pytest.mark.parametrize(
    ("exit_code", "code", "last"),
    [
        (0, 0, "{checked} on {head}: recorded in {record}"),
        (2, 1, "mise run check:all exited 2: nothing recorded"),
    ],
)
def test_a_caller_left_alone_exits_and_prints_as_an_attached_run(
    tmp_path: Path, exit_code: int, code: int, last: str
) -> None:
    repo, head, env = gate_faked(tmp_path, exit_code)
    out, err = (
        text.replace(UNBASED, "") for text in caller(repo, env, "--record").communicate(timeout=60)
    )
    record = next((repo / ".git" / "approvals" / "local-ci").glob("*.json"), None)
    # a pass then says it isn't published: the repo has no remote
    assert (out.splitlines()[:2], err) == (
        ["gate done", last.format(checked=approvals.CHECKED, head=head, record=record)],
        "",
    )
    assert (
        subprocess.run(
            [sys.executable, str(SCRIPTS / "approvals.py"), "local-ci", "--record"],
            cwd=repo,
            env=env,
            capture_output=True,
            check=False,
            start_new_session=True,
        ).returncode
        == code
    )  # leading its session, it runs in place, as before


def test_a_second_local_ci_on_a_running_head_follows_it(tmp_path: Path) -> None:
    repo, head, env = gate_faked(tmp_path, held=True)
    first = caller(repo, env, "--record")
    pid = repo / ".git" / "approvals" / "local-ci" / f"{head}.pid"
    second = None
    try:
        polled(lambda: pid.exists() and pid.read_text())  # detach() writes it once the child runs
        claimed = pid.read_text()
        second = caller(repo, env, "--record")
        status = subprocess.run(
            [sys.executable, str(SCRIPTS / "approvals.py"), "local-ci", "--status", "HEAD"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert status.returncode == 3 and status.stdout.startswith(f"running on {head}")
        # the held gate is released only once the second caller shows the run's output: it has joined the run, and a
        # second that started its own would have written a second line to `starts` by then
        assert second.stdout is not None
        seen = ""
        deadline = time.monotonic() + 30
        while "gate started\n" not in seen:
            assert time.monotonic() < deadline, "the second caller never showed the run's output"
            if select.select([second.stdout], [], [], 0.1)[0]:
                seen += os.read(second.stdout.fileno(), 4096).decode()
        assert (tmp_path / "starts").read_text() == "started\n"
        # a follower never writes the claim; a second caller that started its own run replaces it
        assert pid.read_text() == claimed
    finally:
        (tmp_path / "release").touch()
    assert (
        first.communicate(timeout=60)[0]
        .replace(UNBASED, "")
        .startswith("gate started\ngate done\n")
    )
    second.communicate(timeout=60)
    assert (first.returncode, second.returncode) == (0, 0)
    assert (tmp_path / "runs").read_text() == "run\n"  # one gate ran


def test_two_callers_started_at_once_run_one_gate(tmp_path: Path) -> None:
    repo, _, env = gate_faked(tmp_path, seconds=1)
    callers = [caller(repo, env, "--record") for _ in range(4)]
    for run in callers:
        run.communicate(timeout=60)
    assert [run.returncode for run in callers] == [0] * 4
    assert (tmp_path / "runs").read_text() == "run\n"  # the pid file's lock let one start


def test_a_caller_starts_no_gate_while_another_holds_the_pid_file(tmp_path: Path) -> None:
    repo, head, env = gate_faked(tmp_path)
    common = repo / ".git" / "approvals" / "local-ci"
    common.mkdir(parents=True)
    with open(common / f"{head}.pid", "a+") as claim:
        fcntl.flock(claim, fcntl.LOCK_EX)
        run = caller(repo, env, "--record")
        time.sleep(2)  # long enough for an unlocked caller to start the gate and finish
        # an unlocked caller writes its claim into the pid file, and opens the log, well before the gate ends
        assert (run.poll(), (tmp_path / "runs").exists()) == (None, False)
        assert (common / f"{head}.pid").read_text() == "" and not (common / f"{head}.log").exists()
    run.communicate(timeout=30)
    assert (run.returncode, (tmp_path / "runs").read_text()) == (0, "run\n")


def dead_pid() -> int:
    ended = subprocess.Popen(["true"])
    ended.wait()
    return ended.pid


def test_a_run_s_start_time_reads_the_same_whatever_the_caller_s_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    german = approvals.since(os.getpid())
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    assert german is not None and approvals.since(os.getpid()) == german


@pytest.mark.parametrize(
    ("pid", "exit_file"),
    [
        (dead_pid, "1\n"),  # an older run left its exit code; this one passes
        (dead_pid, None),  # an older run was killed before it wrote one
        (os.getpid, None),  # its pid now belongs to another process, started at another time
    ],
)
def test_a_stale_run_on_the_head_is_replaced_by_a_new_one(
    tmp_path: Path, pid: Callable[[], int], exit_file: str | None
) -> None:
    repo, head, env = gate_faked(tmp_path)
    common = repo / ".git" / "approvals" / "local-ci"
    common.mkdir(parents=True)
    stale = {"pid": pid(), "since": "Thu Jan  1 00:00:00 1970", "argv": ["local-ci", "--record"]}
    (common / f"{head}.pid").write_text(json.dumps(stale))
    if exit_file:
        (common / f"{head}.exit").write_text(exit_file)
    status = subprocess.run(
        [sys.executable, str(SCRIPTS / "approvals.py"), "local-ci", "--status", head],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert status.returncode == 1  # failed, or ended without an exit code: not running (3)
    run = caller(repo, env, "--record")
    out, _ = run.communicate(timeout=30)
    assert (run.returncode, out.replace(UNBASED, "").splitlines()[0]) == (0, "gate done")
    assert (common / f"{head}.exit").read_text() == "0\n"
    assert (tmp_path / "runs").read_text() == "run\n"


# --- the owner's policy: specs approved and merges landed without an agent's approval ---

POLICY = loop_md(
    "spec:\n  auto:\n    - bug\n    - follow-up\n    - sub-issue\nrisk:\n  paths:\n    - billing/**\n"
)
EDITED = "2026-10-02T14:35:27Z"


def epic(*labels: str, by: str = "owner", **fields: Any) -> dict[str, Any]:
    """An epic as an issue's GraphQL `parent`, with `by`'s spec record of its first version."""
    found = issue(9, "kind:enhancement", *labels, comments=(record("spec", by, spec=AS_WRITTEN),))
    return {**found, **fields}


def story(
    parent: dict[str, Any], author: str | None = "worker", body: str = "the spec"
) -> dict[str, Any]:
    """A native sub-issue of `parent`, opened by `author`, with no approval of its own."""
    found = issue(1, "kind:enhancement", "area:web", "size:S", body=body)
    return {**found, "parent": parent, "author": {"login": author} if author else None}


@pytest.fixture
def writers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`worker` can write to the repo and nobody else can; the logins asked about."""
    asked: list[str] = []

    def writer(login: str) -> bool:
        asked.append(login)
        return login == "worker"

    monkeypatch.setattr(approvals, "writer", writer)
    return asked


@pytest.mark.parametrize(
    ("spec", "covered"),
    [
        (issue(1, "kind:bug"), True),
        (
            issue(
                1, "kind:enhancement", body="The rate.\n\nFound while #12 reviewing the invoice.\n"
            ),
            True,
        ),
        (issue(1, "kind:enhancement", body="- Found while #12, a follow-up\n"), True),
        (issue(1, "kind:enhancement", body="An example: `Found while #12`\n"), False),
        (issue(1, "kind:enhancement", body="The template:\n\n```\nFound while #12\n```\n"), False),
        (issue(1, "kind:enhancement", body="`Found while #12` is the line.\n"), False),
        (issue(1, "kind:bug", "epic"), False),
        (issue(1, "kind:enhancement", body="It was found while #12 ran.\n"), False),
        (story(epic("approved:spec")), True),
        (story(epic("approved:spec"), author="drive-by"), False),
        (story(epic("approved:spec"), author=None), False),
        (story(epic()), False),
        (story(epic("approved:spec", by="coordinator")), False),
        (story(epic("approved:spec", lastEditedAt=EDITED)), False),
        (issue(1, "kind:enhancement", "epic"), False),
    ],
    ids=[
        "bug",
        "follow-up",
        "follow-up-in-a-list",
        "quoted-follow-up",
        "fenced-follow-up",
        "line-start-quoted-follow-up",
        "bug-epic",
        "mid-line-follow-up",
        "writers-story",
        "non-writers-story",
        "ghost-authors-story",
        "story-of-an-epic-without-approved-spec",
        "story-of-an-epic-only-an-agent-approved",
        "story-of-an-epic-edited-since",
        "new-epic",
    ],
)
def test_spec_policy_covers_a_bug_a_follow_up_and_a_writers_story_of_an_approved_epic(
    writers: list[str], spec: dict[str, Any], covered: bool
) -> None:
    assert approvals.spec_policy(spec, set(approvals.SPEC_KINDS)) is covered
    assert not approvals.spec_policy(spec, set())
    assert "parent {" in approvals.github.ISSUE and "author { login }" in approvals.github.ISSUE


def test_spec_policy_covers_only_the_kinds_auto_names(writers: list[str]) -> None:
    bug, follow_up = issue(1, "kind:bug"), issue(2, "kind:enhancement", body="Found while #12\n")
    child = story(epic("approved:spec"))
    for kind, covered in (("bug", bug), ("follow-up", follow_up), ("sub-issue", child)):
        assert [approvals.spec_policy(spec, {kind}) for spec in (bug, follow_up, child)] == [
            spec is covered for spec in (bug, follow_up, child)
        ]
    assert approvals.spec_kinds(loop_md("spec:\n  auto:\n    - bug\n")) == {"bug"}
    assert approvals.spec_kinds(AUTO) == approvals.spec_kinds(BARE) == set()


def test_a_spec_policy_covers_needs_no_record_or_label_and_one_it_does_not_needs_the_owner(
    writers: list[str],
) -> None:
    bug, child, new_epic = (
        specced(1, "kind:bug"),
        story(epic("approved:spec")),
        specced(3, "kind:enhancement", "epic"),
    )
    assert approvals.proofs("build", pull(), [bug, child], POLICY) == []
    assert approvals.proofs("build", pull(), [new_epic], POLICY) == [
        "#3 has no `Approved: spec` record by the owner",
        "#3 lacks the `approved:spec` label",
    ]
    # without the policy, a bug is the owner's to approve like any spec
    assert approvals.proofs(
        "build", pull(), [bug], loop_md("risk:\n  paths:\n    - billing/**\n")
    ) == [
        "#1 has no `Approved: spec` record by the owner",
        "#1 lacks the `approved:spec` label",
    ]
    assert problems([specced(1, "kind:bug", "needs-owner")]) == [
        "#1 waits for the owner (needs-owner)"
    ]


AUTO = loop_md(
    "spec:\n  auto: true\nrisk:\n  labels:\n    - area:api\n  paths:\n    - billing/**\n"
)


def by(author: str, found: dict[str, Any]) -> dict[str, Any]:
    return {**found, "author": {"login": author}}


def test_check_build_before_the_pr_judges_the_files_the_branch_changed(
    writers: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no PR, a `paths` risk rule under `spec.auto` reads the files the branch changed since it left the default
    branch, as it reads a PR's: a protected file holds the spec for the owner, and files git can't list hold too."""
    spec = {
        1: by("worker", specced(1, "kind:enhancement")),
        2: by("worker", approved(2, "kind:enhancement", "area:web", "size:S")),
    }
    changed: list[str | None] = [""]

    def git(*args: str) -> str:
        if args[0] == "diff":
            assert args == ("diff", "--name-only", "--no-renames", "-z", "origin/main...HEAD")
            if changed[0] is None:
                raise subprocess.CalledProcessError(128, ["git", *args])
            return "".join(f"{path}\0" for path in changed[0].split())
        return "origin/main\n" if args[-1] == "origin/HEAD" else "loop/1-fix\n"

    monkeypatch.setattr(approvals, "project", lambda: (TRACKER, AUTO))
    monkeypatch.setattr(approvals, "one_repo", lambda: None)
    monkeypatch.setattr(approvals, "issue_node", lambda n: spec[n])
    monkeypatch.setattr(approvals, "current_pr", lambda: None)
    monkeypatch.setattr(approvals, "git", git)

    def build(number: int) -> tuple[int, list[str]]:
        code = approvals.main(["check", "build", "--issue", str(number)])
        return code, capsys.readouterr().out.splitlines()

    assert build(1) == (0, ["#1: every build proof holds; no PR yet"])
    changed[0] = "app.py\nbilling/a.py\n"
    assert build(1) == (
        1,
        [
            (
                "#1: its spec is the owner's to approve under `spec.auto: true`, since it matches a risk rule (path"
                " `billing/**`)"
            ),
            "#1 has no `Approved: spec` record by the owner",
            "#1 lacks the `approved:spec` label",
        ],
    )
    assert build(2)[0] == 0  # the owner approved it
    changed[0] = None
    code, out = build(1)
    assert code == 1 and "the gate can't read every file the PR changes" in out[0]


def test_the_branch_files_check_build_reads_are_its_real_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-ASCII name, which git quotes in its plain output, and one with spaces reach the risk rules as committed."""
    origin, clone = tmp_path / "origin.git", tmp_path / "clone"
    git_in(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    committed(clone)
    git_in(clone, "branch", "-M", "main")
    git_in(clone, "remote", "add", "origin", str(origin))
    git_in(clone, "push", "-q", "origin", "main")
    git_in(clone, "remote", "set-head", "origin", "main")
    git_in(clone, "checkout", "-q", "-b", "loop/1-fix")
    names = [".github/workflows/contrôle.yml", "docs/decision notes.md"]
    for name in names:
        (clone / name).parent.mkdir(parents=True, exist_ok=True)
        (clone / name).write_text("x\n")
    git_in(clone, "add", "-A")
    git_in(clone, "commit", "-qm", "files")
    monkeypatch.chdir(clone)
    assert sorted(approvals.paths(approvals.unopened())) == sorted(names)


def test_spec_auto_is_its_own_policy() -> None:
    assert approvals.spec_auto(AUTO) and not approvals.spec_auto(POLICY)
    assert not approvals.spec_auto(loop_md("spec:\n  auto: false\n")) and not approvals.spec_auto(
        BARE
    )


def test_spec_auto_approves_every_spec_a_writer_opened_but_one_a_risk_rule_holds(
    writers: list[str],
) -> None:
    feature, new_epic = (
        by("worker", specced(1, "kind:enhancement")),
        by("worker", specced(3, "kind:enhancement", "epic")),
    )
    # risk:high holds the merge, not the spec
    marked = by("worker", specced(5, "kind:bug", "risk:high"))
    assert approvals.proofs("build", pull(), [feature, new_epic, marked], AUTO) == []
    held, outsider = (
        by("worker", specced(4, "kind:enhancement", "area:api")),
        by("drive-by", specced(6, "kind:bug")),
    )
    assert approvals.holds(pull(), [feature, held, outsider], AUTO) == [
        "#4: its spec is the owner's to approve under `spec.auto: true`, since it matches a risk rule (label `area:api`)",
        "#6: its spec is the owner's to approve under `spec.auto: true`, since its author @drive-by can't write to the repo",
    ]
    assert approvals.proofs("build", pull(), [held, outsider], AUTO) == [
        "#4 has no `Approved: spec` record by the owner",
        "#4 lacks the `approved:spec` label",
        "#6 has no `Approved: spec` record by the owner",
        "#6 lacks the `approved:spec` label",
    ]
    # the owner's approval lets a held spec build
    owned = by("worker", approved(4, "kind:enhancement", "area:api"))
    assert approvals.proofs("build", pull(), [owned], AUTO) == []


def test_under_spec_auto_a_file_the_spec_did_not_foresee_holds_the_spec(
    writers: list[str],
) -> None:
    feature = by("worker", specced(1, "kind:enhancement"))
    pushed = pull(files=("app.py", "billing/a.py"))
    assert approvals.holds(pushed, [feature], AUTO) == [
        "#1: its spec is the owner's to approve under `spec.auto: true`, since it matches a risk rule (path `billing/**`)"
    ]
    assert approvals.proofs("build", pushed, [feature], AUTO) == [
        "#1 has no `Approved: spec` record by the owner",
        "#1 lacks the `approved:spec` label",
    ]
    # a PR label counts too, and paths the gate can't judge hold
    assert approvals.holds(pull(labels=("area:api",)), [feature], AUTO)
    crowded = pull()
    crowded["files"]["totalCount"] = 101
    assert approvals.holds(crowded, [feature], AUTO) == [
        (
            "#1: its spec is the owner's to approve under `spec.auto: true`, since the gate can't read every file"
            " the PR changes, so the risk rule's `paths` hold until a person judges them"
        )
    ]


def test_under_spec_auto_a_risk_rule_holds_no_merge_but_risk_high_and_the_cap_do(
    writers: list[str],
) -> None:
    owned = by("worker", approved(1, "kind:enhancement", "area:web"))
    done = pull(reviews=(VERDICT,), files=("billing/a.py",))
    assert merge(done, [owned], AUTO) == []
    assert approvals.risk(done, [owned], AUTO) is None
    marked = pull(reviews=(VERDICT,), files=("billing/a.py",), labels=("risk:high",))
    assert approvals.risk(marked, [owned], AUTO) == "`risk:high` is on PR #7"
    # the same PR beside `spec.auto` as a list waits for the owner's merge label
    assert approvals.risk(done, [owned], POLICY) == "it matches a risk rule (path `billing/**`)"


def test_a_policy_line_is_no_condition_so_a_low_risk_pr_waits_for_nothing() -> None:
    done = pull(reviews=(VERDICT,), files=("app.py",))
    assert merge(done, [specced(1, "kind:bug")], POLICY) == []
    assert approvals.risk(done, [specced(1, "kind:bug")], POLICY) is None


def test_a_low_risk_agent_pr_lands_with_no_approval_record_and_no_label(
    landing: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    added: list[tuple[int, str, bool]],
) -> None:
    Path("docs/agents/loop.md").write_text(LOOP)
    follow_up = issue(
        1, "kind:enhancement", "area:web", "size:S", body="The fix.\n\nFound while #5 landing.\n"
    )
    landing["pull"]["closingIssuesReferences"]["nodes"] = [follow_up]
    assert approvals.main(["check", "build", "7"]) == 0
    assert gate(capsys, "check", "merge", "7") == (0, [])
    assert added == []


@pytest.mark.parametrize(
    ("labels", "issue_labels", "files", "why"),
    [
        (("risk:high",), (), ("app.py",), "`risk:high` is on PR #7"),
        ((), ("risk:high",), ("app.py",), "`risk:high` is on #1"),
        ((), (), ("billing/a.py",), "it matches a risk rule (path `billing/**`)"),
        ((), ("size:XL",), ("app.py",), "it matches a risk rule (label `size:XL`)"),
    ],
    ids=["label-on-the-pr", "label-on-its-issue", "risk-path", "risk-label"],
)
def test_a_high_risk_merge_waits_for_the_owner_and_says_why(
    labels: tuple[str, ...],
    issue_labels: tuple[str, ...],
    files: tuple[str, ...],
    why: str,
    hub: dict[str, list[dict[str, Any]]],
) -> None:
    loop = loop_md(
        "spec:\n  auto:\n    - bug\n    - follow-up\n    - sub-issue\nrisk:\n  labels:\n    - size:L\n    - size:XL\n    - area:api\n  paths:\n    - billing/**\n"
    )
    found = approved(1, "area:web", *issue_labels)
    assert merge(pull(reviews=(VERDICT,), labels=labels, files=files), [found], loop) == [
        HELD + why
    ]
    signed = given_go(pull(reviews=(VERDICT,), labels=labels, files=files), hub)
    assert merge(signed, [found], loop) == []


def test_check_merge_adds_risk_high_when_it_infers_high_risk_and_holds_for_the_owner(
    landing: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    added: list[tuple[int, str, bool]],
    hub: dict[str, list[dict[str, Any]]],
) -> None:
    landing["pull"]["files"]["nodes"] = [{"path": ".github/ci.yml"}]
    code, lines = gate(capsys, "check", "merge", "7")
    assert (code, lines[0]) == (approvals.WAITING, GITHUB)
    assert added == [(7, "risk:high", True)]
    added.clear()
    landing["pull"]["labels"]["nodes"] = [{"name": "risk:high"}]
    gate(capsys, "check", "merge", "7")
    assert added == []  # already on the PR
    signed = given_go(
        pull(reviews=(VERDICT,), files=(".github/ci.yml",), labels=("risk:high",)), hub
    )
    signed["closingIssuesReferences"]["nodes"] = [approved(1, "area:web", "kind:bug")]
    landing["pull"] = signed
    assert gate(capsys, "check", "merge", "7")[0] == 0


@pytest.mark.parametrize(
    ("answer", "can"),
    [
        ("admin\n", True),
        ("maintain\n", True),
        ("write\n", True),
        ("read\n", False),
        ("none\n", False),
        (None, False),
    ],
)
def test_write_access_is_githubs_collaborator_permission(
    monkeypatch: pytest.MonkeyPatch, answer: str | None, can: bool
) -> None:
    asked: list[tuple[str, ...]] = []

    def gh(*args: str, data: str | None = None) -> str:
        asked.append(args)
        if answer is None:
            raise subprocess.CalledProcessError(1, "gh", stderr="gh: Not Found (HTTP 404)\n")
        return answer

    monkeypatch.setattr(approvals.github, "gh", gh)
    assert approvals.writer("worker") is can
    assert asked == [
        ("api", "repos/{owner}/{repo}/collaborators/worker/permission", "--jq", ".permission")
    ]


# --- assignees: who an issue belongs to ---


def held(
    found: dict[str, Any], *logins: str, events: tuple[dict[str, Any], ...] = ()
) -> dict[str, Any]:
    """`found` assigned to `logins`, with these timeline events."""
    return {
        **found,
        "assignees": {"nodes": [{"login": login} for login in logins]},
        "timelineItems": {"nodes": list(events)},
    }


def labelled(by: str, kind: str = "User", at: str = WRITTEN) -> dict[str, Any]:
    """`approved:spec` added by `by`."""
    return {
        "__typename": "LabeledEvent",
        "createdAt": at,
        "actor": {"__typename": kind, "login": by},
        "label": {"name": "approved:spec"},
    }


def assigned_event(login: str, at: str) -> dict[str, Any]:
    return {"__typename": "AssignedEvent", "createdAt": at, "assignee": {"login": login}}


def test_the_approver_is_who_added_approved_spec_else_the_account_running_the_loop() -> None:
    assert approvals.approver(held(approved(1), events=(labelled("ann"), labelled("bob")))) == "bob"
    assert (
        approvals.approver(held(approved(1), events=(labelled("github-actions", "Bot"),))) is None
    )
    assert approvals.approver(specced(1)) == "loop"  # a spec the policy approves: no label


@pytest.mark.parametrize(
    ("logins", "refused"),
    [((), False), (("ann",), False), (("bob",), True), (("ann", "bob"), True)],
)
def test_check_build_refuses_an_issue_assigned_to_anyone_but_its_approver(
    logins: tuple[str, ...], refused: bool
) -> None:
    found = held(approved(1, "kind:bug", "area:web"), *logins, events=(labelled("ann"),))
    line = (
        f"#1 is assigned to {', '.join('@' + login for login in logins)}, not to its approver @ann alone:"
        " someone else's issue is theirs, so the loop leaves it"
    )
    assert approvals.proofs("build", pull(), [found], LOOP) == ([line] if refused else [])
    assert approvals.proofs("merge", pull(reviews=(VERDICT,), labels=OWNED), [found], LOOP) == []


def listed(number: int, branch: str, body: str, *closing: int) -> dict[str, Any]:
    """An open PR as `gh pr list --json number,headRefName,body,closingIssuesReferences` prints it."""
    return {
        "number": number,
        "headRefName": branch,
        "body": body,
        "closingIssuesReferences": [
            {
                "number": n,
                "repository": {"name": "scratch-gate-revert", "owner": {"login": "kzarzycki"}},
            }
            for n in closing
        ],
    }


def duplicate(number: int, other: int, branch: str) -> str:
    return (
        f"#{number} is also closed by open PR #{other} (branch `{branch}`): one build per issue, so close one of"
        " the PRs, or link it with `Part of`"
    )


@pytest.mark.parametrize(
    ("mine", "theirs", "closing"),
    [
        ("Closes #1", "Fixes #1", ()),
        ("Resolves #1", "the change", (1,)),  # GitHub links it, with no line in the body
        (
            "Implements #1",
            "closes #1",
            (),
        ),  # under a `signoff` rule, Implements finishes the issue too
    ],
)
def test_check_build_refuses_an_issue_another_open_pr_closes_and_each_names_the_other(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mine: str,
    theirs: str,
    closing: tuple[int, ...],
) -> None:
    monkeypatch.setattr(
        approvals, "project", lambda: (TRACKER, add(LOOP, "signoff", "always: true\n"))
    )
    spec = held(approved(1, "kind:bug", "area:web"), events=(labelled("loop"),))
    first, second = pull(body=mine), pull(body=theirs)
    second.update(number=8, headRefName="second")
    second["closingIssuesReferences"]["nodes"] = [spec for _ in closing]
    first["closingIssuesReferences"]["nodes"] = [] if "#1" in mine else [spec]
    pulls = {7: first, 8: second}
    monkeypatch.setattr(approvals, "pull_request", lambda pr: pulls[pr])
    monkeypatch.setattr(approvals, "issue_node", lambda _n: spec)
    listing = json.dumps([listed(7, "feature", mine), listed(8, "second", theirs, *closing)])
    calls: list[tuple[str, ...]] = []

    def gh(*args: str, data: str | None = None) -> str:
        if "graphql" in args:  # the body's unlinked `#1`
            return json.dumps({"data": {"repository": {"i1": spec}}})
        assert args[:2] == ("pr", "list")
        calls.append(args)
        return listing

    monkeypatch.setattr(approvals.github, "gh", gh)
    monkeypatch.setattr(approvals, "open_pulls", OPEN_PULLS)
    # `always: true` wants `Implements`, so a closing keyword here also gets the keyword check's line
    also = lambda: [
        line for line in capsys.readouterr().out.splitlines() if " is also closed " in line
    ]
    assert approvals.main(["check", "build", "7"]) == 1
    assert also() == [duplicate(1, 8, "second")]
    assert approvals.main(["check", "build", "8"]) == 1
    assert also() == [duplicate(1, 7, "feature")]
    assert len(calls) == 2  # one list call per check


def test_check_build_passes_as_today_when_no_other_open_pr_closes_its_issue(
    monkeypatch: pytest.MonkeyPatch,
    assigned: list[tuple[Any, ...]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Another PR that only builds on the issue (`Part of`), closes another one, or is this PR itself holds nothing."""
    monkeypatch.setattr(approvals, "project", lambda: (TRACKER, LOOP))
    spec = held(approved(1, "kind:bug", "area:web"), events=(labelled("loop"),))
    mine = pull(body="Closes #1")
    mine["closingIssuesReferences"]["nodes"] = [spec]
    monkeypatch.setattr(approvals, "pull_request", lambda _pr: mine)
    listing = [
        listed(7, "feature", "Closes #1", 1),
        listed(8, "part", "Part of #1"),
        listed(9, "other", "Fixes #2", 2),
        listed(10, "signed", "Implements #1"),  # no `signoff` rule: a link like `Part of`
    ]
    calls: list[tuple[str, ...]] = []

    def gh(*args: str, data: str | None = None) -> str:
        calls.append(args)
        return json.dumps(listing)

    monkeypatch.setattr(approvals.github, "gh", gh)
    monkeypatch.setattr(approvals, "open_pulls", OPEN_PULLS)
    assert approvals.main(["check", "build", "7"]) == 0
    assert capsys.readouterr().out == ""
    assert [args[:2] for args in calls] == [("pr", "list")]
    assert (1, "loop", True) in assigned


def test_check_build_counts_only_this_repos_issue_when_another_pr_closes_the_same_number_elsewhere(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`other/project#1` is another issue than this repo's #1, in the body or in GitHub's closing references."""
    monkeypatch.setattr(approvals, "project", lambda: (TRACKER, LOOP))
    spec = held(approved(1, "kind:bug", "area:web"), events=(labelled("loop"),))
    mine = pull(body="Closes #1")
    mine["closingIssuesReferences"]["nodes"] = [spec]
    monkeypatch.setattr(approvals, "pull_request", lambda _pr: mine)
    foreign = listed(8, "foreign", "Closes other/project#1")
    foreign["closingIssuesReferences"] = [
        {"number": 1, "repository": {"name": "project", "owner": {"login": "other"}}}
    ]
    monkeypatch.setattr(approvals.github, "gh", lambda *_args, **_kw: json.dumps([foreign]))
    monkeypatch.setattr(approvals, "open_pulls", OPEN_PULLS)
    assert approvals.main(["check", "build", "7"]) == 0
    assert capsys.readouterr().out == ""


def test_check_build_refuses_when_the_open_pr_list_reaches_its_limit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A list as long as the limit may have left out the PR that closes the issue too, so the check can't pass."""
    monkeypatch.setattr(approvals, "project", lambda: (TRACKER, LOOP))
    spec = held(approved(1, "kind:bug", "area:web"), events=(labelled("loop"),))
    mine = pull(body="Closes #1")
    mine["closingIssuesReferences"]["nodes"] = [spec]
    monkeypatch.setattr(approvals, "pull_request", lambda _pr: mine)
    monkeypatch.setattr(approvals, "OPEN_LIMIT", 2, raising=False)
    everyone = [
        listed(7, "feature", "Closes #1", 1),
        listed(8, "a", "Fixes #2"),
        listed(9, "b", "Fixes #1"),
    ]
    calls: list[tuple[str, ...]] = []

    def gh(*args: str, data: str | None = None) -> str:
        calls.append(args)
        return json.dumps(everyone[: int(args[args.index("--limit") + 1])])

    monkeypatch.setattr(approvals.github, "gh", gh)
    monkeypatch.setattr(approvals, "open_pulls", OPEN_PULLS)
    assert approvals.main(["check", "build", "7"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        (
            "the duplicate-build check reads at most 2 open PRs and this repo has at least that many, so it could"
            " not see every open PR: close stale PRs, or check by hand that no other open PR closes #1"
        )
    ]
    assert len(calls) == 1


def test_check_merge_lists_no_open_prs_and_a_duplicate_does_not_hold_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The duplicate check belongs to build: by merge the build it guards has run, and the list costs a request."""
    spec = approved(1, "kind:bug", "area:web")
    mine = pull(body="Closes #1")
    mine["closingIssuesReferences"]["nodes"] = [spec]
    listings: list[None] = []
    second = {**listed(8, "second", "Fixes #1"), "closingIssuesReferences": {"nodes": []}}
    monkeypatch.setattr(approvals, "open_pulls", lambda: listings.append(None) or [second])
    assert duplicate(1, 8, "second") in approvals.proofs("build", mine, [spec], LOOP)
    assert duplicate(1, 8, "second") not in approvals.proofs("merge", mine, [spec], LOOP)
    assert len(listings) == 1  # the build check's, none at merge


def test_being_assigned_is_no_go_ahead() -> None:
    claimed = held(specced(1, "kind:enhancement"), "loop")
    assert approvals.proofs("build", pull(), [claimed], LOOP) == [
        "#1 has no `Approved: spec` record by the owner",
        "#1 lacks the `approved:spec` label",
    ]


def test_building_assigns_the_approver_only_when_no_one_is_and_the_pr_follows(
    assigned: list[tuple[Any, ...]],
) -> None:
    free = held(approved(1), events=(labelled("ann"),))
    claimed = held(approved(2), "ann", events=(labelled("ann"),))
    assert approvals.claim(pull(), [free, claimed]) == []
    assert assigned == [
        (1, "Assigned: @ann\nBy: loop\nPR: #7\n"),
        (1, "ann", True),
        (7, "ann", True),
    ]
    assigned.clear()
    assert approvals.claim(held(pull(), "ann"), [claimed]) == []
    assert assigned == []


def test_check_build_claims_only_once_every_proof_holds(
    monkeypatch: pytest.MonkeyPatch,
    assigned: list[tuple[Any, ...]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    in_project(monkeypatch)
    pr = pull()
    pr["closingIssuesReferences"]["nodes"] = [
        held(issue(1, "kind:enhancement", "area:web", "size:S"))
    ]
    monkeypatch.setattr(approvals, "pull_request", lambda _pr: pr)
    assert approvals.main(["check", "build", "7"]) == 1
    assert assigned == []
    pr["closingIssuesReferences"]["nodes"] = [
        held(specced(1, "kind:bug"))
    ]  # the policy approves a bug
    assert approvals.main(["check", "build", "7"]) == 0
    assert (1, "loop", True) in assigned
    capsys.readouterr()


def test_a_refused_assignment_is_reported_and_never_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(number: int, login: str, add: bool) -> None:
        raise subprocess.CalledProcessError(1, "gh", stderr="Validation Failed")

    monkeypatch.setattr(approvals, "assign", refuse)
    assert approvals.claim(pull(), [held(approved(1), events=(labelled("ann"),))]) == [
        "#1: could not assign @ann (Validation Failed)"
    ]


def test_a_refused_record_leaves_the_issue_unassigned_and_says_so(
    monkeypatch: pytest.MonkeyPatch, assigned: list[tuple[Any, ...]]
) -> None:
    def refuse(number: int, body: str) -> None:
        raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 403: issue is locked")

    monkeypatch.setattr(approvals, "comment", refuse)
    assert approvals.claim(pull(), [held(approved(1), events=(labelled("ann"),))]) == [
        "#1: did not assign @ann: could not record the claim (HTTP 403: issue is locked)"
    ]
    assert assigned == []  # no assignment release would read as a person's


FORK, THEM = "https://github.com/me/fork.git", "git@github.com:them/fork"
DEMO = REPOSITORY / "engineering" / "skills" / "demo" / "scripts" / "demo.sh"


@pytest.mark.parametrize(
    ("remotes", "default", "refused"),
    [
        ({"origin": FORK}, False, False),
        ({"origin": FORK, "upstream": THEM}, False, True),
        ({"origin": FORK, "upstream": THEM}, True, False),
        ({"origin": FORK, "upstream": "https://github.com/them/fork.git/"}, False, True),
        ({"origin": FORK, "upstream": "https://GITHUB.COM/them/fork"}, False, True),
        ({"origin": FORK, "upstream": "ssh://git@github.com:22/them/fork.git"}, False, True),
        ({"origin": FORK, "upstream": "git@github.com:/them/fork.git"}, False, True),
        ({"origin": FORK, "upstream": "ssh://git@github.com//them/fork.git"}, False, True),
        ({"origin": FORK, "mirror": "https://gitlab.com/me/fork.git"}, False, False),
        ({"origin": FORK, "mirror": "https://gitlab.com/github.com/them/fork.git"}, False, False),
        ({"origin": FORK, "mirror": "https://notgithub.com/them/fork.git"}, False, False),
        ({"origin": FORK, "push": "git@github.com:Me/Fork.git"}, False, False),
    ],
)
def test_one_repo_refuses_several_github_remotes_without_a_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remotes: dict[str, str],
    default: bool,
    refused: bool,
) -> None:
    """gh reads `{owner}/{repo}` as `upstream`'s when several GitHub remotes have no default: approvals.py and
    demo.sh refuse, naming them, before any gh call but demo.sh's route probe and version check."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name, url in remotes.items():
        subprocess.run(["git", "-C", str(tmp_path), "remote", "add", name, url], check=True)
    if default:
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "remote.origin.gh-resolved", "base"], check=True
        )
    monkeypatch.chdir(tmp_path)
    if refused:
        with pytest.raises(approvals.Refused, match="me/fork, them/fork.*gh repo set-default"):
            approvals.one_repo()
    else:
        approvals.one_repo()
    # demo.sh: a gh that passes the version check and exits 77 on any other call shows whether the
    # guard let it through
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "gh").write_text(
        f'#!/bin/sh\necho "$*" >> {tmp_path}/gh.log\n'
        '[ "$1" = --version ] && echo "gh version 2.103.3" && exit 0\n'
        '[ "$1 $2" = "api graphql" ] && exit 0\nexit 77\n'
    )
    (shim / "gh").chmod(0o755)
    (tmp_path / "v.mp4").write_bytes(b"x")
    shell = subprocess.run(
        ["bash", str(DEMO), "v.mp4"],
        capture_output=True,
        text=True,
        env={"PATH": f"{shim}:/usr/bin:/bin", "HOME": str(tmp_path)},
        check=False,
    )
    assert (shell.returncode, "me/fork them/fork" in shell.stderr) == (
        (1, True) if refused else (77, False)
    )
    if refused:  # the refusal comes before the route probe, so the probe never reaches gh
        log = tmp_path / "gh.log"
        assert "api graphql" not in (log.read_text() if log.exists() else "")


# --- the verifier judges risk (`verifier: true`) ---


VERIFIED = loop_md(
    "spec:\n  auto:\n    - bug\n    - follow-up\n    - sub-issue\nrisk:\n  verifier: true\n"
)


def judging(risk: str | None, commit: str = HEAD, **fields: Any) -> dict[str, Any]:
    """A satisfied verifier review whose last line is `Risk: <risk>`, or none with None."""
    review = verdict_on(commit, **fields)
    return {**review, "body": review["body"] + (f"Risk: {risk}\n" if risk else "")}


@pytest.fixture
def gating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], list[tuple[int, str]]]:
    """`check merge` in a checkout with a `risk: verifier` loop.md: the PR it reads, and every comment it posts."""
    agents = tmp_path / "docs" / "agents"
    agents.mkdir(parents=True)
    (agents / "issue-tracker.md").write_text(TRACKER)
    (agents / "loop.md").write_text(VERIFIED)
    state: dict[str, Any] = {}
    comments: list[tuple[int, str]] = []
    monkeypatch.setattr(approvals, "pull_request", lambda _n: state["pull"])
    monkeypatch.setattr(approvals, "comment", lambda number, body: comments.append((number, body)))
    monkeypatch.chdir(tmp_path)
    return state, comments


def judged_pr(*reviews: dict[str, Any], **fields: Any) -> dict[str, Any]:
    pr = pull(reviews=reviews, files=("app.py",), **fields)
    pr["closingIssuesReferences"]["nodes"] = [approved(1, "area:web", "kind:enhancement")]
    return pr


def test_risk_low_from_the_verifier_merges_without_the_owner(
    gating: tuple[dict[str, Any], list[tuple[int, str]]],
    capsys: pytest.CaptureFixture[str],
    added: list[tuple[int, str, bool]],
) -> None:
    state, comments = gating
    state["pull"] = judged_pr(judging("low — a flag on two test commands"))
    assert approvals.main(["check", "merge", "7"]) == 0
    assert capsys.readouterr().out == ""
    assert (added, comments) == ([], [])


@pytest.mark.parametrize(
    ("risk", "said"),
    [
        (
            "high — it narrows the test selection",
            "says `Risk: high — it narrows the test selection`",
        ),
        ("medium — unsure", "says `Risk: medium — unsure`"),
        (None, "has no `Risk:` line"),
    ],
    ids=["high", "another-word", "no-line"],
)
def test_risk_high_another_word_or_no_line_waits_for_the_owner_and_flags_the_pr(
    gating: tuple[dict[str, Any], list[tuple[int, str]]],
    capsys: pytest.CaptureFixture[str],
    added: list[tuple[int, str, bool]],
    risk: str | None,
    said: str,
    hub: dict[str, list[dict[str, Any]]],
) -> None:
    state, comments = gating
    state["pull"] = judged_pr(judging(risk))
    why = f"it matches a risk rule (verifier): the verifier's pass 1 on {HEAD[:7]} {said}"
    assert approvals.main(["check", "merge", "7"]) == approvals.WAITING
    assert capsys.readouterr().out.splitlines() == [HELD + why]
    assert added == [(7, "risk:high", True)]
    assert [number for number, _ in comments] == [7]
    assert comments[0][1].startswith(f"{approvals.FLAGGED}, since {why}.")
    signed = given_go(judged_pr(judging(risk), labels=("risk:high",)), hub)
    assert merge(signed, signed["closingIssuesReferences"]["nodes"], VERIFIED) == []


def test_a_risk_low_on_an_older_head_or_by_a_stranger_does_not_count(
    monkeypatch: pytest.MonkeyPatch, no_origin: list[list[str]]
) -> None:
    monkeypatch.setattr(approvals, "writer", lambda login: False)
    issues = [approved(1, "area:web", "kind:enhancement")]
    older = judged_pr(judging("low — fine", commit=OLD))
    assert (
        approvals.proofs("merge", older, issues, VERIFIED) != []
    )  # no verdict on the head lands nothing
    high = judging("high — a gate weakened")
    stranger = judging("low — fine", author="stranger", heading="Verifier (claude), pass 2")
    pr = judged_pr(high, stranger)
    assert merge(pr, issues, VERIFIED) == [
        HELD
        + f"it matches a risk rule (verifier): the verifier's pass 1 on {HEAD[:7]} says `Risk: high — a gate weakened`"
    ]
    # a mixed PR's two verifiers on one pass: both must say low
    other = judging("high — CI permissions", heading="Verifier (codex), pass 1")
    assert approvals.judged(judged_pr(judging("low — fine"), other)) == (
        f"the verifier's pass 1 on {HEAD[:7]} says `Risk: high — CI permissions`"
    )


def test_a_path_term_holds_whatever_the_verifier_says() -> None:
    loop = loop_md(
        "spec:\n  auto:\n    - bug\n    - follow-up\n    - sub-issue\nrisk:\n  verifier: true\n  paths:\n    - .github/**\n"
    )
    pr = pull(reviews=(judging("low — a workflow comment"),), files=(".github/ci.yml",))
    issues = [approved(1, "area:web", "kind:enhancement")]
    assert merge(pr, issues, loop) == [HELD + "it matches a risk rule (path `.github/**`)"]
    assert merge({**pr, "files": {"nodes": [{"path": "app.py"}]}}, issues, loop) == []


@pytest.mark.parametrize("spec", ["", "  auto: true\n"], ids=["merge", "spec-auto"])
@pytest.mark.parametrize(
    "condition",
    [
        "  paths:\n    - docs/verifier.md",
        "  paths:\n    - verifier/**",
        "  labels:\n    - area:verifier",
    ],
)
def test_the_word_verifier_in_a_path_or_label_is_no_verifier_term(
    spec: str, condition: str
) -> None:
    """A rule without the `verifier` term holds as it did before it: the verifier's `Risk:` line, high or missing,
    holds nothing."""
    loop = loop_md(f"spec:\n{spec}risk:\n{condition}\n")
    issues = [approved(1, "area:web", "kind:enhancement")]
    for review in (judging("high — a gate changed"), judging(None)):
        assert approvals.risk(pull(reviews=(review,), files=("app.py",)), issues, loop) is None


def test_under_spec_auto_the_verifier_term_holds_the_merge_not_the_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(approvals, "writer", lambda login: True)
    loop = loop_md("spec:\n  auto: true\nrisk:\n  verifier: true\n  paths:\n    - billing/**\n")
    owned = by("worker", specced(5, "kind:enhancement"))
    pr = pull(reviews=(judging("low — fine"),), files=("app.py",))
    assert approvals.proofs("build", pr, [owned], loop) == []
    assert approvals.risk(pr, [owned], loop) is None
    high = pull(reviews=(judging("high — surprises users"),), files=("app.py",))
    assert approvals.proofs("build", high, [owned], loop) == []
    assert approvals.risk(high, [owned], loop) == (
        "it matches a risk rule's `verifier` term: the verifier's pass 1 on"
        f" {HEAD[:7]} says `Risk: high — surprises users`"
    )


def test_the_gates_risk_high_comes_off_when_a_later_pass_judges_it_low_and_a_raise_stays(
    gating: tuple[dict[str, Any], list[tuple[int, str]]],
    capsys: pytest.CaptureFixture[str],
    added: list[tuple[int, str, bool]],
) -> None:
    state, _ = gating
    flagged = note(f"{approvals.FLAGGED}, since …", createdAt="2026-10-01T11:00:01Z")
    rejudged = judging("low — the lead's reason holds", heading="Verifier (claude), pass 2")
    rejudged["body"] += "Scope: full\n"
    state["pull"] = judged_pr(judging("high — x"), rejudged, labels=("risk:high",))
    state["pull"]["comments"]["nodes"].append(flagged)
    assert approvals.main(["check", "merge", "7"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "PR #7: removed the `risk:high` the gate added, since the verifier now judges the risk low"
    ]
    assert added == [(7, "risk:high", False)]
    added.clear()
    # the lead raised it again after the gate's comment: only the owner lowers it
    state["pull"] = judged_pr(judging("high — x"), rejudged, labels=("risk:high",))
    state["pull"]["comments"]["nodes"].append({**flagged, "createdAt": "2026-10-01T10:30:00Z"})
    assert approvals.main(["check", "merge", "7"]) == approvals.WAITING
    assert added == []
    # a person's label with no gate comment stays too
    state["pull"] = judged_pr(rejudged, labels=("risk:high",))
    assert approvals.main(["check", "merge", "7"]) == approvals.WAITING
    assert added == []


OLD_HEAD = "b" * 40


def later(risk: str, number: int, commit: str = HEAD, *extra: str) -> dict[str, Any]:
    """A pass after the first, on `commit`, with the given extra lines (`Logic change:`, `Scope:`)."""
    review = judging(risk, commit=commit, heading=f"Verifier (claude), pass {number}")
    return {**review, "body": review["body"] + "".join(f"{line}\n" for line in extra)}


@pytest.mark.parametrize(
    "extra",
    [
        ("Logic change: none",),
        ("Logic change: the retry count",),
        (),
        ("Logic change: none.", "Scope: partial"),
        ("Scope: fully",),
    ],
)
def test_a_later_pass_cannot_lower_the_risk_of_a_pass_that_reviewed_the_whole_change(
    gating: tuple[dict[str, Any], list[tuple[int, str]]],
    capsys: pytest.CaptureFixture[str],
    added: list[tuple[int, str, bool]],
    extra: tuple[str, ...],
) -> None:
    """#1397: pass 1 `Risk: high`, then a pass 2 `Risk: low` with no `Scope: full` stays high and asks the owner."""
    state, _ = gating
    full = judging("high — it changes landing policy", commit=OLD_HEAD)
    state["pull"] = judged_pr(full, later("low — a rebase", 2, HEAD, *extra))
    assert approvals.main(["check", "merge", "7"]) == approvals.WAITING
    assert "says `Risk: high — it changes landing policy`" in capsys.readouterr().err
    assert added == [(7, "risk:high", True)]


def test_a_chain_of_later_passes_keeps_the_risk_and_a_full_scope_pass_lowers_it(
    gating: tuple[dict[str, Any], list[tuple[int, str]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    state, _ = gating
    full = judging("high — it changes landing policy", commit=OLD_HEAD)
    middle = later("low — a rebase", 2, "c" * 40)
    chain = [
        full,
        middle,
        later("low — a merge", 3, "d" * 40, "Logic change: none"),
        later("low", 4),
    ]
    state["pull"] = judged_pr(*chain)
    assert approvals.main(["check", "merge", "7"]) == approvals.WAITING
    state["pull"] = judged_pr(*chain, later("low — whole diff re-read", 5, HEAD, "Scope: full"))
    assert approvals.main(["check", "merge", "7"]) == 0
    state["pull"] = judged_pr(
        *chain, later("high — whole diff", 5, HEAD, "Scope: full"), later("low", 6)
    )
    assert approvals.main(["check", "merge", "7"]) == approvals.WAITING


def posted_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    number: int,
    body: str,
    scope: str | None,
) -> dict[str, Any]:
    """The review `verdict` posts for a report with `body` in its criteria and `scope` as its closing line, as GitHub returns it."""
    report = tmp_path / f"r{number}.md"
    report.write_text(
        f"Verifier: claude, pass {number}\n\n## Criteria\n\n- c{body}\n\nHead: {HEAD}\n"
        f"VERDICT: 0 blocker, 0 major, 0 minor\nSATISFIED: yes\n"
        + (f"Scope: {scope}\n" if scope else "")
        + "Risk: low — whole diff\n"
    )
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(approvals, "diff_lines", lambda *_: {})
    monkeypatch.setattr(approvals, "post_review", lambda _pr, review: sent.append(review))
    assert approvals.verdict(7, report) == 0
    return {
        **verdict_on(HEAD, heading=f"Verifier (claude), pass {number}"),
        "body": sent[0]["body"],
    }


def test_the_closing_scope_line_is_posted_and_read_and_a_criteria_line_is_not(
    gating: tuple[dict[str, Any], list[tuple[int, str]]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """#1397: `verdict` posts the report's last `Scope:` line, `Scope: full` lowers a high, and the exact value counts."""
    state, _ = gating
    full = judging("high — it changes landing policy", commit=OLD_HEAD)
    state["pull"] = judged_pr(full)
    for scope, body, waits in [
        ("full", "", False),
        ("Full", "", False),
        (None, "\nScope: full", True),  # a criteria line is no scope
        ("full diff of the delta", "", True),
        (None, "", True),
    ]:
        review = posted_pass(monkeypatch, tmp_path, 2, body, scope)
        state["pull"] = judged_pr(full, review)
        assert (approvals.main(["check", "merge", "7"]) != 0) is waits, (scope, body)
    crlf = {
        **review,
        "body": review["body"].replace("\n", "\r\n"),
    }  # a review edited on the web: CRLF
    crlf = {**crlf, "body": crlf["body"].replace("- c\r\n", "- c\r\n\r\nScope: full\r\n")}
    state["pull"] = judged_pr(full, crlf)
    assert approvals.main(["check", "merge", "7"]) != 0, (
        "a criteria Scope line in a CRLF body is no scope"
    )
    report = tmp_path / "r2.md"
    assert approvals.report_facts(report)["scope"] is None
    report.write_text(report.read_text().replace("Risk:", "Scope: full\nRisk:"))
    assert approvals.report_facts(report)["scope"] == "full"


def test_a_repeated_pass_1_is_a_delta_and_a_pass_that_comes_first_is_the_whole_change(
    gating: tuple[dict[str, Any], list[tuple[int, str]]],
) -> None:
    state, _ = gating
    state["pull"] = judged_pr(
        judging("high — policy", commit=OLD_HEAD), judging("low", commit=HEAD)
    )
    assert approvals.main(["check", "merge", "7"]) == approvals.WAITING
    state["pull"] = judged_pr(later("high — policy", 2, OLD_HEAD), later("low", 3))
    assert approvals.main(["check", "merge", "7"]) == approvals.WAITING


def test_unflag_keeps_the_label_after_a_later_low_and_a_path_rule_label_is_untouched(
    gating: tuple[dict[str, Any], list[tuple[int, str]]],
    capsys: pytest.CaptureFixture[str],
    added: list[tuple[int, str, bool]],
) -> None:
    state, _ = gating
    flagged = note(f"{approvals.FLAGGED}, since …", createdAt="2026-10-01T11:00:01Z")
    high, low = judging("high — x", commit=OLD_HEAD), later("low — a rebase", 2)
    state["pull"] = judged_pr(high, low, labels=("risk:high",))
    state["pull"]["comments"]["nodes"].append(flagged)
    assert approvals.main(["check", "merge", "7"]) == approvals.WAITING
    assert added == []
    # a path rule's label has no gate comment, so a full low pass does not take it off
    state["pull"] = judged_pr(high, later("low", 2, HEAD, "Scope: full"), labels=("risk:high",))
    assert approvals.main(["check", "merge", "7"]) == approvals.WAITING
    assert added == []


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (("", ""), None),
        (
            ("Risk: low — a test fixture\n", ""),
            "has no `Risk: low|high — <reason>` line: the report is unfinished",
        ),
        (
            ("SATISFIED: no\n", ""),
            "has no `VERDICT:` or `SATISFIED:` line: the report is unfinished",
        ),
        (
            ("Verifier: claude, same-family, pass 2\n", ""),
            "has no `Verifier: <family>, pass <n>` line",
        ),
    ],
)
def test_report_reads_a_report_as_verdict_would_without_github(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    change: tuple[str, str],
    why: str | None,
) -> None:
    """The verifier runs it before replying, in a sandbox with no GitHub, so a format slip costs no extra turn."""

    def offline(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"report reached out: {args}")

    monkeypatch.setattr(approvals.github, "gh", offline)
    monkeypatch.setattr(approvals, "one_repo", offline)
    report = tmp_path / "report.md"
    report.write_text(REPORT.replace(*change) if change[0] else REPORT)
    assert approvals.main(["report", str(report)]) == (1 if why else 0)
    assert capsys.readouterr().out == (
        f"{report} {why}\n" if why else f"{report} reads as a verdict\n"
    )


def test_a_report_without_a_risk_line_posts_nothing(
    reviewing: list[tuple[Any, ...]], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "report.md"
    report.write_text(REPORT.replace("Risk: low — a test fixture\n", ""))
    assert approvals.main(["verdict", "7", str(report)]) == 1
    assert capsys.readouterr().out == (
        f"{report} has no `Risk: low|high — <reason>` line: the report is unfinished\n"
    )
    assert [call for call in reviewing if call[0] == "review"] == []


# --- a head's own recorded pass ---


def gated_head(head: str, env: dict[str, str | None] | None = None) -> None:
    """Record a green `local-ci --record` pass on `head`, as run_local_ci() writes it, of a gate that sets `env`."""
    env = {} if env is None else env
    held = approvals.tree_of(head)
    command = shlex.join([*(f"{name}={value}" for name, value in env.items()), *approvals.LOCAL_CI])
    where = approvals.recorded(held, command)
    where.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "head": head,
        "tree": held,
        "command": command,
        "exit": 0,
        "env": env,
        "time": "2026-01-01T00:00:00Z",
        "host": "host",
    }
    where.write_text(json.dumps(record))


def posts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every body comment() hands gh, from a fake host whose name and home name its owner."""
    sent: list[str] = []
    monkeypatch.setattr(approvals, "comment", COMMENT)
    monkeypatch.setattr(approvals.github, "gh", lambda *args, data=None: sent.append(data) or "")
    monkeypatch.setattr(approvals.socket, "gethostname", lambda: "owner-MacBook.local")
    monkeypatch.setenv("HOME", "/Users/owner")
    return sent


def test_a_posted_review_names_no_home_however_it_is_spelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A review goes as JSON: a non-ASCII home is replaced as GitHub decodes it, in the body and inline comments."""
    sent = posts(monkeypatch)
    monkeypatch.setenv("HOME", "/home/é")
    approvals.post_review(
        7,
        {
            "body": "cache /home/é/cache",
            "comments": [{"path": "a.py", "line": 1, "body": "at /home/é"}],
        },
    )
    review = json.loads(sent[0])
    assert (review["body"], review["comments"][0]["body"]) == ("cache ~/cache", "at ~")


def test_posted_text_names_no_account_in_a_temp_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing test's path under pytest's `pytest-of-<user>` names the account; a relative `tmp/` path stays."""
    monkeypatch.setenv("HOME", "/Users/owner")
    monkeypatch.setattr(approvals.tempfile, "gettempdir", lambda: "/var/folders/ab/T")
    text = (
        "FAILED /tmp/pytest-of-owner/pytest-12/test_x0/a.py\n"
        "/var/folders/ab/T/pytest-of-owner/pytest-current/b\n"
        "/var/folders/ab/T/x.log /tmp\n"
        "~/tmp/y tmp/loop/z.log /tmpfoo /Users/owner/c"
    )
    assert approvals.public(text) == (
        "FAILED <tmp>/test_x0/a.py\n<tmp>/b\n<tmp>/x.log <tmp>\n~/tmp/y tmp/loop/z.log /tmpfoo ~/c"
    )
    # a temp dir under home or under /tmp, and one this process doesn't know
    for tmp in ("/Users/owner/tmp", "/tmp/build"):
        monkeypatch.setattr(approvals.tempfile, "gettempdir", lambda tmp=tmp: tmp)
        assert (
            approvals.public(f"{tmp}/pytest-of-owner/pytest-3/a /Users/owner/tmpx")
            == "<tmp>/a ~/tmpx"
        )
    assert (
        approvals.public("/scratch/pytest-of-owner/pytest-3/a")
        == "/scratch/pytest-of-<user>/pytest-3/a"
    )


# --- a clean merge of main carries the head's verdict, never its local-ci record, where loop.md's carry_over_main says so ---

NO_CI = add(LOOP, "loop", "ci: false\n")
CARRY = add(NO_CI, "carry_over_main", "except:\n  - mise.toml\n")
APP = "".join(f"line {n}\n" for n in range(1, 11))


def carries(merge: str, head: str, base: str, since: str, branch: str = "main") -> str:
    return (
        f"{merge} carries the proofs of {head}: it is the clean merge (git merge-tree) of {head} and {branch}'s"
        f" {base}, whose changes since {since} touch none of the PR's files, and neither side touches a"
        " `carry_over_main.except` path"
    )


@pytest.fixture
def carrying(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., dict[str, str]]:
    """A bare `origin` whose `main` holds app.py and mise.toml, and the PR's head `feature` (`head`), which changes
    app.py's first line. `merged(files)` commits `files` on a branch off that `main` (`base`) and merges it into the
    head as git would (`merge`, the head its first parent): `resolve` writes what a conflict leaves, `extra` adds a
    file the merge alone has, `crossed` first has each side merge the other's commit (a criss-cross, so the head is
    that merge and the two have two merge bases), `onto` merges into that commit instead of the head and `main` moves
    main from that commit, `then` commits `then` on top of the merge as a PR commit after it (`merge`). The test runs
    in a clone made before any of them was pushed, so the gate must fetch."""
    origin, work, checker = (tmp_path / name for name in ("origin.git", "work", "checker"))
    sh("git", "init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    sh("git", "clone", "-q", str(origin), str(work), cwd=tmp_path)
    who = ("-c", "commit.gpgsign=false", "-c", "user.name=Lead", "-c", "user.email=l@example.com")
    git = lambda *args: sh("git", *who, *args, cwd=work)

    def write(files: dict[str, str]) -> None:
        for name, text in files.items():
            (work / name).write_text(text)
        git("add", "-A")

    write({"app.py": APP, "mise.toml": "[tools]\n"})
    git("commit", "-qm", "base")
    git("branch", "-M", "main")
    git("push", "-q", "origin", "HEAD:main")
    sh("git", "clone", "-q", str(origin), str(checker), cwd=tmp_path)
    git("switch", "-qc", "feature")
    write({"app.py": APP.replace("line 1\n", "line 1, the feature\n")})
    git("commit", "-qm", "feature")
    head = git("rev-parse", "HEAD")
    cases = itertools.count()

    def merged(
        files: dict[str, str],
        resolve: dict[str, str] | None = None,
        extra: dict[str, str] | None = None,
        crossed: bool = False,
        onto: str | None = None,
        main: str = "main",
        then: dict[str, str] | None = None,
    ) -> dict[str, str]:
        n = next(cases)
        git("switch", "-qc", f"base{n}", main)
        write(files)
        git("commit", "-qm", f"main moved {n}")
        first = onto or head
        if crossed:
            moved = git("rev-parse", "HEAD")
            git("merge", "-q", "--no-ff", "-m", f"main takes the feature {n}", head)
            git("switch", "-q", "--detach", head)
            git("merge", "-q", "--no-ff", "-m", f"the feature takes main {n}", moved)
            first = git("rev-parse", "HEAD")
            git("switch", "-q", f"base{n}")
            # main moves once more, so the merge's tree is not the head's (same_tree)
            write({"crossed.txt": "main moved again\n"})
            git("commit", "-qm", f"main moved again {n}")
        base = git("rev-parse", "HEAD")
        git("switch", "-q", "--detach", first)
        subprocess.run(
            ["git", *who, "merge", "-q", "--no-ff", "--no-commit", base],
            cwd=work,
            capture_output=True,
            check=False,
        )
        write({**(resolve or {}), **(extra or {})})
        git("commit", "-qm", f"Merge main {n}")
        if then:
            write(then)
            git("commit", "-qm", f"the feature after main {n}")
        merge = git("rev-parse", "HEAD")
        git(
            "push",
            "-q",
            "origin",
            f"{head}:refs/heads/feature",
            f"{base}:refs/heads/main{n}",
            f"{merge}:refs/heads/pr{n}",
        )
        return {"head": first, "base": base, "merge": merge, "since": git("rev-parse", "main")}

    monkeypatch.chdir(checker)
    return merged


def carried_pull(case: dict[str, str], *comments: str, risk: str = "") -> dict[str, Any]:
    """The PR whose head is the merge, with a satisfied verdict on the head it merged main into."""
    review = verdict_on(case["head"])
    review["body"] += risk
    pr = pull(reviews=(review,), comments=comments, checks=(), files=("app.py",))
    pr.update(headRefOid=case["merge"], baseRefOid=case["base"])
    return pr


@pytest.fixture
def posting(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """`local-ci <pr>` against a stubbed GitHub: `pull` is the PR, `loop` loop.md; `posted` holds each comment and
    `ran` each head the check would run on, which it never does here."""
    state: dict[str, Any] = {"posted": [], "ran": [], "loop": CARRY}

    def run_local_ci(pr: int | None, head: str | None, _passed: Any = ()) -> dict[str, Any]:
        state["ran"].append(head)
        raise approvals.Refused("the check would run")

    monkeypatch.setattr(approvals, "comment", lambda _number, body: state["posted"].append(body))
    monkeypatch.setattr(approvals, "pull_request", lambda _n: state["pull"])
    monkeypatch.setattr(approvals, "project", lambda: (None, state["loop"]))
    monkeypatch.setattr(approvals, "one_repo", lambda: None)
    monkeypatch.setattr(approvals, "run_local_ci", run_local_ci)
    return state


def test_a_clean_merge_of_main_carries_the_verdict_but_never_the_local_ci_record(
    carrying: Callable[..., dict[str, str]],
    posting: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Under `ci: false` `local-ci` reruns its own gate on the merge, as CI would: the head's record stays the head's;
    check merge reads none (#1872)."""
    case = carrying({"other.txt": "main moved\n"})
    head, merge_sha = case["head"], case["merge"]
    gated_head(head)
    posting["pull"] = carried_pull(case)
    assert approvals.main(["local-ci", "7", "--run"]) == 1
    assert (posting["ran"], posting["posted"]) == ([merge_sha], [])
    capsys.readouterr()
    on_head = f"{approvals.CHECKED}\nHead: {head}\nCommand: mise run check:all\nEnv: default\n"
    assert merge(carried_pull(case, on_head), [approved(1, "area:web")], CARRY) == []
    assert capsys.readouterr().out.splitlines() == [
        f"PR #7: {carries(merge_sha, head, case['base'], case['since'])}"
    ]


# --- the go on a head carries over merges of the base a verifier pass says change no logic (#808) ---

NO_LOGIC = "Logic change: none\n"


def go_over(
    case: dict[str, str],
    hub: dict[str, list[dict[str, Any]]],
    logic: str = NO_LOGIC,
    satisfied: str = "yes",
    on: str = "merge",
) -> dict[str, Any]:
    """A high-risk PR whose head is the case's `merge`, with the owner's go on the head it merged the base into and a
    verifier pass on `on` ending in `logic`."""
    review = verdict_on(case[on], satisfied=satisfied)
    review["body"] += logic
    pr = pull(reviews=(review,), checks=(), files=("app.py",), labels=("risk:high",))
    pr.update(headRefOid=case["head"], baseRefOid=case["base"])
    given_go(pr, hub)
    pr["headRefOid"] = case["merge"]
    return pr


def held_at(case: dict[str, str], why: str) -> list[str]:
    return [
        HELD.replace(HEAD, case["merge"]) + "`risk:high` is on PR #7",
        f"PR #7: the go on {case['head']} carries nothing to {case['merge']}: {why}",
    ]


def test_the_go_carries_over_a_merge_of_the_base_with_conflict_fixes_a_verdict_calls_no_logic_change(
    carrying: Callable[..., dict[str, str]], hub: dict[str, list[dict[str, Any]]]
) -> None:
    case = carrying(
        {"app.py": CONFLICTED}, resolve={"app.py": APP.replace("line 1\n", "line 1, both\n")}
    )
    pr = go_over(case, hub)
    assert approvals.owner_waits(pr, [], LOOP) == []
    assert approvals.whose(pr, LOOP) == [
        (
            f"PR #7: the go on {case['head']} is the owner's, carried to {case['merge']}: every commit since is a"
            " merge of main, and the verifier's pass on it says `Logic change: none`"
        )
    ]
    # a go withdrawn on the old head has nothing to carry
    withdrawn = status("owner", "The owner withdrew their go", state="failure")
    hub[case["head"]].insert(0, withdrawn)
    held = [HELD.replace(HEAD, case["merge"]) + "`risk:high` is on PR #7"]
    assert approvals.owner_waits(pr, [], LOOP) == held
    hub[case["head"]].pop(0)
    # a go withdrawn on the new head stays withdrawn, whatever the older one says
    hub[case["merge"]] = [withdrawn]
    assert approvals.owner_waits(pr, [], LOOP) == held


@pytest.mark.parametrize(
    ("given", "why"),
    [
        (
            {"then": {"app.py": APP.replace("line 2\n", "line 2, after\n")}},
            "it is no merge of two parents",
        ),
        ({"base": "main"}, "its second parent {base} is not on main"),
    ],
    ids=["a PR commit after the merge", "a merge of another branch"],
)
def test_the_go_carries_nothing_over_a_commit_other_than_a_merge_of_the_base(
    carrying: Callable[..., dict[str, str]],
    hub: dict[str, list[dict[str, Any]]],
    given: dict[str, Any],
    why: str,
) -> None:
    case = carrying({"other.txt": "main moved\n"}, then=given.get("then"))
    pr = go_over(case, hub)
    if (
        "base" in given
    ):  # the branch merged in is not the base: the base's tip stayed where the PR started
        pr["baseRefOid"] = case["since"]
    assert approvals.owner_waits(pr, [], LOOP) == held_at(case, why.format(**case))


@pytest.mark.parametrize(
    ("given", "why"),
    [
        ({"on": "head"}, "no verifier review is on {merge}"),
        (
            {"satisfied": "no"},
            "the verifier's pass 1 on {merge} is not satisfied, or has a blocker or major open",
        ),
        ({"logic": ""}, "the verifier's pass 1 on {merge} does not say `Logic change: none`"),
        (
            {"logic": "Logic change: the retry count\n"},
            "the verifier's pass 1 on {merge} does not say `Logic change: none`",
        ),
    ],
    ids=["no verdict on the new head", "an unsatisfied verdict", "no logic line", "a logic change"],
)
def test_the_go_carries_nothing_without_a_satisfied_verdict_on_the_new_head_saying_no_logic_change(
    carrying: Callable[..., dict[str, str]],
    hub: dict[str, list[dict[str, Any]]],
    given: dict[str, str],
    why: str,
) -> None:
    case = carrying(  # a hand-resolved merge, which the mechanical proof (catch_up) does not carry
        {"app.py": CONFLICTED}, resolve={"app.py": APP.replace("line 1\n", "line 1, both\n")}
    )
    assert approvals.owner_waits(go_over(case, hub, **given), [], LOOP) == held_at(
        case, why.format(**case)
    )


def test_a_clean_merge_of_the_base_carries_the_verdict_and_the_go_with_no_new_pass(
    carrying: Callable[..., dict[str, str]],
    hub: dict[str, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1314: the proof is mechanical (catch_up), so neither a verifier pass on the merge nor its `Logic change: none`
    is read, and a project without `carry_over_main` gets it too."""
    case = carrying({"other.txt": "main moved\n"})
    pr = go_over(case, hub, logic="", on="head")
    assert approvals.owner_waits(pr, [], LOOP) == []
    [line] = approvals.whose(pr, LOOP)
    assert f"carried to {case['merge']}: every commit since is a merge of main whose" in line
    assert "git show --remerge-diff` is empty" in line
    assert merge(carried_pull(case), [approved(1, "area:web")], NO_CI) == []
    assert "carried to" in capsys.readouterr().out


def test_a_project_with_carry_over_main_keeps_its_own_rule_for_the_go_as_for_the_verdict(
    carrying: Callable[..., dict[str, str]],
    hub: dict[str, list[dict[str, Any]]],
) -> None:
    """The mechanical proof (catch_up) is for a project without `carry_over_main`: with it, the go carries only by the
    verifier's pass on the new head, as the verdict carries only by the project's own merge rule (B3)."""
    case = carrying({"other.txt": "main moved\n"})
    pr = go_over(case, hub, logic="", on="head")
    assert isinstance(approvals.carried_go(pr, LOOP), tuple)
    found = approvals.carried_go(pr, CARRY)
    assert isinstance(found, str) and found.startswith("the go on") and "carries nothing" in found
    assert approvals.whose(pr, CARRY) == []


def test_a_verifier_reports_logic_change_line_is_posted_with_its_verdict(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text(
        f"Verifier: claude, pass 2 (merge of main)\n\n## Criteria\n\n- The merge keeps both sides.\n\n"
        f"Head: {HEAD}\nVERDICT: 0 blocker, 0 major, 0 minor\n"
        f"SATISFIED: yes\nRisk: low — a merge\n{NO_LOGIC}"
    )
    assert approvals.parse_report(report)[3][-1] == NO_LOGIC.strip()


def test_a_carried_verdicts_risk_line_still_holds_the_merge(
    carrying: Callable[..., dict[str, str]],
) -> None:
    case = carrying({"other.txt": "main moved\n"})
    pr = carried_pull(case, risk="Risk: high — a test fixture\n")
    assert approvals.risk(pr, [], add(CARRY, "risk", "verifier: true\n")) == (
        f"it matches a risk rule (verifier): the verifier's pass 1 on {case['head'][:7]} says"
        " `Risk: high — a test fixture`"
    )


CONFLICTED = APP.replace("line 1\n", "line 1, on main\n")
REFUSED = {
    "a resolved conflict": (
        {"app.py": CONFLICTED},
        {"resolve": {"app.py": APP.replace("line 1\n", "line 1, both\n")}},
        CARRY,
        "{head} and {base} conflict",
    ),
    "an evil merge": (
        {"other.txt": "main moved\n"},
        {"extra": {"extra.txt": "in neither parent\n"}},
        CARRY,
        "{merge}'s tree is not git merge-tree's of its parents: the merge resolved or added something",
    ),
    "an overlapping file": (
        {"app.py": APP.replace("line 10\n", "line 10, on main\n")},
        {},
        CARRY,
        "app.py changed on both sides since {since}",
    ),
    "a touched except-path": (
        {"mise.toml": "[tools]\nuv = 'latest'\n"},
        {},
        CARRY,
        "main's changes since {since} touch mise.toml, a `carry_over_main.except` path",
    ),
    "the PR's own except-path change": (
        {"other.txt": "main moved\n"},
        {},
        add(NO_CI, "carry_over_main", "except:\n  - '*.py'\n"),
        "the PR's changes since {since} touch app.py, a `carry_over_main.except` path",
    ),
    "a criss-cross merge": (
        {"other.txt": "main moved\n"},
        {"crossed": True},
        CARRY,
        "{head} and {base} have 2 merge bases",
    ),
    "a project without the line": (
        {"app.py": CONFLICTED},
        {"resolve": {"app.py": APP.replace("line 1\n", "line 1, both\n")}},
        NO_CI,
        None,
    ),
}


def test_a_submodule_bump_main_hides_with_ignore_all_still_meets_the_except_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ignore = all` in .gitmodules hides a submodule's bump from a plain git diff."""
    who = ("-c", "commit.gpgsign=false", "-c", "user.name=Lead", "-c", "user.email=l@example.com")
    git = lambda *args: sh("git", *who, *args, cwd=tmp_path)
    git("init", "-q", "-b", "main")

    def commit(text: str) -> None:
        (tmp_path / "app").write_text(text)
        git("add", "-A")
        git("commit", "-qm", text)

    commit("lib 1\n")
    lib1 = git("rev-parse", "HEAD")
    commit("lib 2\n")
    lib2 = git("rev-parse", "HEAD")
    (tmp_path / ".gitmodules").write_text(
        '[submodule "deps/lib"]\n\tpath = deps/lib\n\turl = ./lib\n\tignore = all\n'
    )
    (tmp_path / "deps" / "lib").mkdir(parents=True)
    git("update-index", "--add", "--cacheinfo", f"160000,{lib1},deps/lib")
    commit("base\n")
    git("switch", "-qc", "feature")
    commit("feature\n")
    head = git("rev-parse", "HEAD")
    git("switch", "-q", "main")
    git("update-index", "--cacheinfo", f"160000,{lib2},deps/lib")
    git("commit", "-qm", "main bumps the dependency")
    tip = git("rev-parse", "HEAD")
    monkeypatch.chdir(tmp_path)
    why = approvals.merged(head, tip, ["deps/**"], "main")
    assert isinstance(why, str) and why.endswith("touch deps/lib, a `carry_over_main.except` path")


def test_diff_relative_run_from_a_subdirectory_still_meets_the_except_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`diff.relative` makes a porcelain diff drop what lies outside the current directory."""
    who = ("-c", "commit.gpgsign=false", "-c", "user.name=Lead", "-c", "user.email=l@example.com")
    git = lambda *args: sh("git", *who, *args, cwd=tmp_path)
    git("init", "-q", "-b", "main")
    git("config", "diff.relative", "true")
    (tmp_path / "pkg").mkdir()

    def commit(files: dict[str, str]) -> str:
        for name, text in files.items():
            (tmp_path / name).write_text(text)
        git("add", "-A")
        git("commit", "-qm", "commit")
        return git("rev-parse", "HEAD")

    commit({"guard.cfg": "a\n", "pkg/feature": "a\n"})
    git("switch", "-qc", "feature")
    head = commit({"pkg/feature": "b\n"})
    git("switch", "-q", "main")
    tip = commit({"guard.cfg": "b\n"})
    monkeypatch.chdir(tmp_path / "pkg")
    why = approvals.merged(head, tip, ["guard.cfg"], "main")
    assert isinstance(why, str) and why.endswith("touch guard.cfg, a `carry_over_main.except` path")


@pytest.mark.parametrize(("files", "merging", "loop", "why"), REFUSED.values(), ids=REFUSED.keys())
def test_a_merge_that_may_have_changed_the_prs_code_carries_nothing(
    carrying: Callable[..., dict[str, str]],
    capsys: pytest.CaptureFixture[str],
    files: dict[str, str],
    merging: dict[str, Any],
    loop: str,
    why: str | None,
) -> None:
    """Each falls back to today's proofs: a verifier pass on the merge."""
    case = carrying(files, **merging)
    head, merge_sha = case["head"], case["merge"]
    assert merge(carried_pull(case), [approved(1, "area:web")], loop) == [
        (
            f"PR #7: the newest verifier review is on {head}, not on the head {merge_sha}:"
            " every commit after a verdict needs a verdict of its own"
        ),
    ]
    said = (
        []
        if why is None
        else [f"PR #7: {merge_sha} carries nothing from {head}: {why.format(**case)}"]
    )
    assert capsys.readouterr().out.splitlines() == said


def test_a_merge_of_a_commit_not_on_the_base_carries_nothing(
    carrying: Callable[..., dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    """Another branch merged in is no merge of main, however disjoint: main's tip here never reached it."""
    case = carrying({"other.txt": "not on main\n"})
    pr = carried_pull(case)
    pr["baseRefOid"] = case["since"]
    assert merge(pr, [approved(1, "area:web")], CARRY)[0].startswith(
        "PR #7: the newest verifier review is on"
    )
    assert capsys.readouterr().out.splitlines() == [
        f"PR #7: {case['merge']} carries nothing from {case['head']}: its second parent {case['base']} is not on main"
    ]


def test_two_clean_merges_of_main_carry_the_verdict_from_before_both(
    carrying: Callable[..., dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    """Main merged in twice since the verdict: each merge carries its first parent's, down to the verified head."""
    first = carrying({"other.txt": "main moved\n"})
    second = carrying({"more.txt": "main moved again\n"}, onto=first["merge"], main=first["base"])
    pr = carried_pull(first)
    pr.update(headRefOid=second["merge"], baseRefOid=second["base"])
    assert merge(pr, [approved(1, "area:web")], CARRY) == []
    assert capsys.readouterr().out.splitlines() == [
        f"PR #7: {carries(first['merge'], first['head'], first['base'], first['since'])}",
        f"PR #7: {carries(second['merge'], first['merge'], second['base'], first['base'])}",
    ]


def test_a_pr_commit_after_a_clean_merge_carries_nothing(
    carrying: Callable[..., dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    """The commit on top of the merge is the PR's own, never reviewed: the head needs its own verdict. A rebased or
    amended head is the same case, a commit of one parent where the walk looks for a merge."""
    case = carrying(
        {"other.txt": "main moved\n"}, then={"app.py": APP.replace("line 5\n", "line 5, new\n")}
    )
    assert merge(carried_pull(case), [approved(1, "area:web")], CARRY)[0].startswith(
        "PR #7: the newest verifier review is on"
    )
    assert capsys.readouterr().out.splitlines() == [
        f"PR #7: {case['merge']} carries nothing from {case['head']}: it is no merge of two parents"
    ]


def test_a_pr_commit_between_two_clean_merges_carries_nothing(
    carrying: Callable[..., dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    """A clean merge on top hides no PR commit under it: the walk meets that commit before the verified one."""
    first = carrying({"other.txt": "main moved\n"})
    rewritten = carrying(
        {"more.txt": "main moved again\n"}, then={"app.py": APP.replace("line 2\n", "line 2!\n")}
    )
    case = carrying(
        {"third.txt": "main moved a third time\n"}, onto=rewritten["merge"], main=first["base"]
    )
    pr = carried_pull(first)
    pr.update(headRefOid=case["merge"], baseRefOid=case["base"])
    assert merge(pr, [approved(1, "area:web")], CARRY)[0].startswith(
        "PR #7: the newest verifier review is on"
    )
    assert capsys.readouterr().out.splitlines() == [
        f"PR #7: {case['merge']} carries nothing from {first['head']}: {rewritten['merge']} is no merge of two parents"
    ]


# --- the delegated approver's go on a head (#748) ---

DELEGATE_ALL = add(GITHUB_RULE, "loop", "delegate: all\n")
DELEGATE_GO = f"The delegate's go on {HEAD[:12]}, under the owner's delegation"
DELEGATE_RECORD = (
    f"Approved under the owner's delegation.\nApproved: merge\nBy: delegate\nHead: {HEAD}\n"
)
LABELLED_HIGH = HELD + "`risk:high` is on PR #7"
DELEGATED_HIGH = LABELLED_HIGH.replace(
    "--by owner`; into main `approved:merge` is no go; `loop.delegate: all` in the policy file lets a delegate's go count)",
    f"--by owner`, or the delegate's: `approve merge 7 --head {HEAD} --by delegate`; into main `approved:merge` is no go)",
)
OWNER_RECORD = f"Approved by the owner.\nApproved: merge\nBy: owner\nHead: {HEAD}\n"


@pytest.fixture
def delegating(landing: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    """The landing checkout with `loop.delegate: all` and a high-risk PR into main; each comment posted lands on the
    PR, as GitHub reads it back."""
    Path("docs/agents/loop.md").write_text(DELEGATE_ALL)
    landing["pull"]["files"]["nodes"] = [{"path": ".github/ci.yml"}]
    landing["pull"]["labels"]["nodes"] = [{"name": "risk:high"}]
    posted: list[tuple[int, str]] = []

    def comment(number: int, body: str) -> None:
        posted.append((number, approvals.machine(body)))
        landing["pull"]["comments"]["nodes"].append(note(body))

    monkeypatch.setattr(approvals, "comment", comment)
    return posted


def test_with_delegate_all_the_delegates_go_into_main_posts_its_status_and_record_and_clears_check_merge(
    delegating: list[tuple[int, str]],
    hub: dict[str, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, lines = gate(capsys, "check", "merge", "7")
    assert (code, lines[0]) == (approvals.WAITING, DELEGATED_HIGH)
    assert gate(capsys, "approve", "merge", "7", "--head", HEAD, "--by", "delegate") == (
        0,
        [f"PR #7: the delegate's go is on {HEAD}, under the owner's delegation"],
    )
    assert [(s["context"], s["description"]) for s in hub[HEAD]] == [("owner", DELEGATE_GO)]
    assert delegating == [(7, DELEGATE_RECORD)]
    assert gate(capsys, "check", "merge", "7") == (
        0,
        [f"PR #7: the go on {HEAD} is the delegate's, under the owner's delegation"],
    )


def test_without_the_setting_the_delegates_go_into_main_is_refused_naming_it(
    delegating: list[tuple[int, str]],
    hub: dict[str, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    Path("docs/agents/loop.md").write_text(GITHUB_RULE)
    code, lines = gate(capsys, "approve", "merge", "7", "--head", HEAD, "--by", "delegate")
    assert code == 1 and "`loop.delegate: all`" in lines[0] and "main" in lines[0]
    assert (delegating, hub) == ([], {})
    Path("docs/agents/loop.md").write_text(add(GITHUB_RULE, "loop", "delegate: feature\n"))
    assert gate(capsys, "approve", "merge", "7", "--head", HEAD, "--by", "delegate")[0] == 1
    assert (delegating, hub) == ([], {})


def test_into_a_feature_branch_the_delegates_go_needs_no_setting(
    delegating: list[tuple[int, str]],
    hub: dict[str, list[dict[str, Any]]],
    landing: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    Path("docs/agents/loop.md").write_text(GITHUB_RULE)
    into_feature(landing["pull"])
    assert gate(capsys, "approve", "merge", "7", "--head", HEAD, "--by", "delegate")[0] == 0
    assert approvals.owner_waits(landing["pull"], [], GITHUB_RULE) == []


def test_a_head_that_moved_is_refused_for_the_delegate_as_for_the_owner(
    delegating: list[tuple[int, str]],
    hub: dict[str, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    for by in ("owner", "delegate"):
        code, lines = gate(capsys, "approve", "merge", "7", "--head", "c" * 40, "--by", by)
        assert (code, lines) == (
            1,
            [f"PR #7's head is {HEAD}, not {'c' * 40}: ask about the head it has now"],
        )
    assert (delegating, hub) == ([], {})


def test_a_delegates_status_no_longer_counts_once_the_setting_is_removed(
    delegating: list[tuple[int, str]],
    hub: dict[str, list[dict[str, Any]]],
    landing: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert gate(capsys, "approve", "merge", "7", "--head", HEAD, "--by", "delegate")[0] == 0
    assert approvals.owner_waits(landing["pull"], [], DELEGATE_ALL) == []
    assert approvals.owner_waits(landing["pull"], [], GITHUB_RULE) == [LABELLED_HIGH]
    Path("docs/agents/loop.md").write_text(GITHUB_RULE)
    code, lines = gate(capsys, "check", "merge", "7")
    assert (code, lines) == (approvals.WAITING, [LABELLED_HIGH])
    # nor is the delegate's record read as the owner's, or the owner's status with the delegate's record
    hub[HEAD] = [status("owner", f"The owner's go on {HEAD[:12]}")]
    assert approvals.owner_waits(landing["pull"], [], DELEGATE_ALL) == [DELEGATED_HIGH]


def test_the_owners_and_the_delegates_records_open_with_their_own_plain_line(
    delegating: list[tuple[int, str]],
    hub: dict[str, list[dict[str, Any]]],
    landing: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert gate(capsys, "approve", "merge", "7", "--head", HEAD, "--by", "owner") == (
        0,
        [f"PR #7: the owner's go is on {HEAD}"],
    )
    assert gate(capsys, "check", "merge", "7") == (0, [f"PR #7: the go on {HEAD} is the owner's"])
    assert gate(capsys, "approve", "merge", "7", "--head", HEAD, "--by", "delegate")[0] == 0
    assert delegating == [(7, OWNER_RECORD), (7, DELEGATE_RECORD)]
    assert [approvals.parsed(body, "merge")["by"] for _, body in delegating] == [
        "owner",
        "delegate",
    ]
    assert (
        gate(capsys, "approve", "merge", "7", "--head", HEAD, "--by", "delegate", "--withdraw")[0]
        == 0
    )
    assert delegating[-1] == (7, f"Withdrawn: merge\nBy: delegate\nHead: {HEAD}\n")
    assert hub[HEAD][0]["state"] == "failure"
    assert approvals.owner_waits(landing["pull"], [], DELEGATE_ALL) == [DELEGATED_HIGH]


def test_land_says_whose_go_it_landed_on(
    lands: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    (lands["work"] / "docs" / "agents" / "loop.md").write_text(DELEGATE_ALL)
    head = lands["open"](1, {".github/ci.yml": "on: push\n"}, labels=("risk:high",))
    pr = lands["pulls"][1]
    pr["comments"]["nodes"].append(note(DELEGATE_RECORD.replace(HEAD, head)))
    hub[head] = [status("owner", f"The delegate's go on {head[:12]}, under the owner's delegation")]
    code, lines = lands["land"]("1")
    assert code == 0, lines
    assert f"PR #1: the go on {head} is the delegate's, under the owner's delegation" in lines


def test_check_merge_names_no_go_on_a_low_risk_head_that_carries_one(
    delegating: list[tuple[int, str]],
    landing: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # a later pass that says `Risk: low` takes `risk:high` off a head that already carries a go
    landing["pull"]["files"]["nodes"] = [{"path": "app.py"}]
    landing["pull"]["labels"]["nodes"] = []
    assert gate(capsys, "approve", "merge", "7", "--head", HEAD, "--by", "owner")[0] == 0
    assert gate(capsys, "check", "merge", "7") == (0, [])


def test_land_names_no_go_on_a_low_risk_head_that_carries_one(
    lands: dict[str, Any], hub: dict[str, list[dict[str, Any]]]
) -> None:
    head = lands["open"](1, {"1.txt": "1\n"})
    lands["pulls"][1]["comments"]["nodes"].append(note(OWNER_RECORD.replace(HEAD, head)))
    hub[head] = [status("owner", f"The owner's go on {head[:12]}")]
    code, lines = lands["land"]("1")
    assert code == 0, lines
    assert not [line for line in lines if "the go on" in line], lines


def test_without_the_setting_the_delegate_still_withdraws_their_go_into_main_naming_themself(
    delegating: list[tuple[int, str]],
    hub: dict[str, list[dict[str, Any]]],
    landing: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert gate(capsys, "approve", "merge", "7", "--head", HEAD, "--by", "delegate")[0] == 0
    Path("docs/agents/loop.md").write_text(GITHUB_RULE)
    assert gate(
        capsys, "approve", "merge", "7", "--head", HEAD, "--by", "delegate", "--withdraw"
    ) == (0, [f"PR #7: the delegate's go on {HEAD} is withdrawn"])
    assert (hub[HEAD][0]["state"], hub[HEAD][0]["description"]) == (
        "failure",
        f"The delegate withdrew their go on {HEAD[:12]}",
    )
    assert delegating[-1] == (7, f"Withdrawn: merge\nBy: delegate\nHead: {HEAD}\n")


def test_delegate_is_a_choice_of_all_or_feature() -> None:
    assert approvals.settings(add(BARE, "loop", "delegate: all\n"))["delegate"] == "all"
    assert approvals.delegation(BARE) == "feature"
    with pytest.raises(approvals.Refused, match=r"`loop.delegate` must be one of all, feature"):
        approvals.settings(add(BARE, "loop", "delegate: everything\n"))


def test_a_spec_is_not_the_delegates_to_approve_without_delegate_all(
    github: list[tuple[Any, ...]], capsys: pytest.CaptureFixture[str]
) -> None:
    """The fixture project's policy sets no `loop.delegate: all` (test_approvals_delegate_spec.py has the rest)."""
    assert approvals.main(["approve", "spec", "1", "--by", "delegate"]) == 1
    assert "needs `loop.delegate: all`" in capsys.readouterr().out
    assert github == []


# --- one route for a gate pass: the record branch in origin (#798, #807) ---

PROXY = "gh: Write access to this GitHub API path is not permitted through this proxy. (HTTP 403)"


def no_status(*_args: Any) -> Any:
    raise subprocess.CalledProcessError(1, "gh", stderr=PROXY)


def real_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, real in (
        ("record_ref", RECORD_REF),
        ("push_record", PUSH_RECORD),
        ("drop_records", DROP_RECORDS),
    ):
        monkeypatch.setattr(approvals, name, real)


def pushed_from_a_cloud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[str, str]:
    """A cloud session (clone `a`, whose status write GitHub refuses) records a pass of `true` on a pushed head; the
    test then runs in host `b`, a clone with no record of it, where the status write works. Returns the head and the
    line local-ci printed."""
    head = published_on_a_with(tmp_path, monkeypatch, no_status)
    return head, capsys.readouterr().out


def published_on_a_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, post: Any, gate: tuple[str, ...] = ("true",)
) -> str:
    real_refs(monkeypatch)
    hosted = approvals.post_status
    monkeypatch.setattr(approvals, "post_status", post)
    head = published_on_a(tmp_path, monkeypatch, gate)
    monkeypatch.setattr(approvals, "post_status", hosted)
    git_in(tmp_path / "b", "fetch", "-q", "origin")
    return head


def name_of(head: str, command: str = "true") -> str:
    return f"{approvals.tree_of(head)}-{approvals.gate_key(command)}"


def branch_of(head: str, command: str = "true") -> str:
    return f"refs/heads/loop-gate/{name_of(head, command)}"


@pytest.mark.parametrize("where", ["host", "cloud"])
def test_a_pass_from_a_host_or_a_cloud_session_is_pushed_and_reused_by_another_clone_alike(
    github: list[tuple[Any, ...]],
    hub: dict[str, list[dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    where: str,
) -> None:
    """Acceptance 1: whether GitHub takes clone A's status (a host) or refuses it (a cloud session), A pushes the
    record branch, and clone B reuses it for `local-ci <pr>` through reusable() without running the gate, naming the
    branch and writing the `gate` status it holds on the head."""
    post = approvals.post_status if where == "host" else no_status
    head = published_on_a_with(tmp_path, monkeypatch, post)
    out = capsys.readouterr().out
    branch = branch_of(head)
    assert f"published in origin as the record branch {branch}" in out
    line = f"Tree {approvals.tree_of(head)} on {head[:12]}: true"
    message = git_in(tmp_path / "origin.git", "log", "-1", "--format=%B", branch)
    assert message.splitlines()[0] == line
    assert git_in(tmp_path / "origin.git", "for-each-ref", "--format=%(refname)") == (
        f"refs/heads/feature\n{branch}\nrefs/heads/main\nrefs/loop/gate/master\n"
    )
    if where == "host":
        assert [s["description"] for s in hub.pop(head)] == [line]
    else:
        assert hub.get(head) is None
        assert f"was not written: {PROXY}" in out

    def never(*_args: Any) -> Any:
        raise AssertionError("clone B ran the gate")

    monkeypatch.setattr(approvals, "run_local_ci", never)
    assert approvals.reusable(head)["ref"] == branch
    assert approvals.main(["local-ci", "7", "--run"]) == 0
    [(_, _, body)] = github
    assert f"Reused by tree from the pass recorded on {head} at 2026-" in body
    assert f"and pushed as {branch}, not run again." in body
    assert {(s["context"], s["state"], s["description"]) for s in hub[head]} == {
        ("gate", "success", line)
    }


def test_gate_record_reuses_origins_record_only_for_the_same_tree_and_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1822: the verifier reads origin's record of the gate on the head's tree instead of running it again; a head
    with another tree, or another command (another test list), has no record and is run. The gate sets a variable,
    so the record matches only with the env line the command sets. Clone B's own record file never counts, since a
    worker sharing the clone writes it."""
    gate = "GATE_TIER=full true"
    head = published_on_a_with(tmp_path, monkeypatch, no_status, ("GATE_TIER=full", "true"))
    capsys.readouterr()
    assert approvals.main(["gate-record", head]) == 0
    assert f"as {branch_of(head, gate)} at 2026-" in capsys.readouterr().out
    assert approvals.main(["gate-record", head, "--", "GATE_TIER=full", "true"]) == 0
    assert f"as {branch_of(head, gate)} at 2026-" in capsys.readouterr().out
    assert approvals.main(["gate-record", head, "--", "pytest", "tests/test_other.py"]) == 1
    assert "no record of `pytest tests/test_other.py`" in capsys.readouterr().out
    b = tmp_path / "b"
    git_in(b, "checkout", "-q", head)
    (b / "changed.txt").write_text("another tree\n")
    git_in(b, "add", "changed.txt")
    git_in(
        b,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "-m",
        "another tree",
    )
    other = git_in(b, "rev-parse", "HEAD").strip()
    approvals.recorded(approvals.tree_of(other), gate).parent.mkdir(parents=True, exist_ok=True)
    approvals.recorded(approvals.tree_of(other), gate).write_text('{"exit": 0}')
    assert approvals.main(["gate-record", other]) == 1
    assert f"no record of `{gate}` on tree {approvals.tree_of(other)}" in capsys.readouterr().out


def test_a_green_gate_status_naming_the_tree_with_no_record_branch_is_not_reused(
    hub: dict[str, list[dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Acceptance 2, against a real origin: once the record branch is gone, the `gate` status A wrote on the head,
    naming its tree and command, is no pass for B."""
    head = published_on_a_with(tmp_path, monkeypatch, approvals.post_status)
    assert [s["description"] for s in hub[head]] == [
        f"Tree {approvals.tree_of(head)} on {head[:12]}: true"
    ]
    git_in(tmp_path / "b", "push", "-q", "origin", f":{branch_of(head)}")
    assert approvals.reusable(head) is None


def forged(
    tmp_path: Path,
    head: str,
    name: str,
    *,
    tree: str,
    line: str,
    env: str,
    more: tuple[str, ...] = (),
) -> None:
    """Push to origin, under `name`, a record whose content is what the test says, as anyone who can push may;
    `more` are parents beside `head`."""
    a = tmp_path / "a"
    parents = [arg for parent in (head, *more) for arg in ("-p", parent)]
    commit = git_in(a, "commit-tree", tree, *parents, "-m", f"{line}\n\nEnv: {env}\n").strip()
    git_in(a, "push", "-q", "-f", "origin", f"{commit}:refs/heads/loop-gate/{name}")


@pytest.mark.parametrize(
    "wrong", ["none", "another tree", "another command", "another env", "another commit"]
)
def test_a_ref_whose_content_is_not_the_pass_asked_for_is_not_reused(
    github: list[tuple[Any, ...]],
    hub: dict[str, list[dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    wrong: str,
) -> None:
    """Acceptance 5 (#800's forged records): the branch's name says which tree and command it holds, and its commit
    must say the same: its tree, the command in its line, the env and the commit the line names. One field off and
    the gate runs; `none` is the forged record with nothing off, reused, so each other case fails only on its own
    field."""
    head, _ = pushed_from_a_cloud(tmp_path, monkeypatch, capsys)
    held, base = approvals.tree_of(head), git_in(tmp_path / "a", "rev-parse", "HEAD~1").strip()
    other = git_in(tmp_path / "a", "rev-parse", "HEAD~1^{tree}").strip()
    fields = {
        "tree": other if wrong == "another tree" else held,
        "line": f"Tree {held} on {(base if wrong == 'another commit' else head)[:12]}: "
        + ("true --tier" if wrong == "another command" else "true"),
        "env": '{"TIER": "light"}' if wrong == "another env" else "{}",
    }
    forged(tmp_path, head, name_of(head), **fields)
    git_in(tmp_path / "b", "fetch", "-q", "origin")
    assert (approvals.reusable(head) is None) == (wrong != "none")


@pytest.mark.parametrize(
    ("wrong", "said"),
    [
        ("another tree", "its commit holds the tree {other}, not {held}"),
        ("another env", """its message lacks the line 'Env: {{}}'"""),
        ("another command", "its description reads '{line} --tier', not '{line}'"),
        ("two parents", "its commit has 2 parents, not the one commit the pass ran on"),
    ],
)
def test_local_ci_refusing_a_found_record_branch_says_it_was_found_and_why_it_was_rejected(
    github: list[tuple[Any, ...]],
    hub: dict[str, list[dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    wrong: str,
    said: str,
) -> None:
    """#862 user story 2: a record branch origin holds under the expected name but whose content is not the pass
    asked for is named in the refusal with what was wrong, so the lead can tell the worker what to push, and the
    gate does not run."""
    head, _ = pushed_from_a_cloud(tmp_path, monkeypatch, capsys)
    held = approvals.tree_of(head)
    other = git_in(tmp_path / "a", "rev-parse", "HEAD~1^{tree}").strip()
    line = f"Tree {held} on {head[:12]}: true"
    forged(
        tmp_path,
        head,
        name_of(head),
        tree=other if wrong == "another tree" else held,
        line=f"{line} --tier" if wrong == "another command" else line,
        env='{"TIER": "light"}' if wrong == "another env" else "{}",
        more=(git_in(tmp_path / "a", "rev-parse", "HEAD~1").strip(),)
        if wrong == "two parents"
        else (),
    )
    monkeypatch.setattr(approvals, "run_local_ci", lambda *_a: pytest.fail("the gate ran"))
    assert approvals.main(["local-ci", "7"]) == 1
    out = capsys.readouterr().out
    assert (
        f"origin holds the record branch {branch_of(head)}, but it is not this pass: "
        + said.format(other=other, held=held, line=line)
    ) in out
    assert github == []


def test_a_record_branch_with_no_committer_or_unreadable_says_why_it_was_rejected(
    hub: dict[str, list[dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The rest of record_ref()'s rejections reach `why`: a commit naming no committer, whose time is unknown, and a
    branch origin lists but that can't be fetched."""
    head, _ = pushed_from_a_cloud(tmp_path, monkeypatch, capsys)
    a, held = tmp_path / "a", approvals.tree_of(head)
    body = f"tree {held}\nparent {head}\n\nTree {held} on {head[:12]}: true\n\nEnv: {{}}\n"
    loose = tmp_path / "commit"
    loose.write_text(body)
    commit = git_in(a, "hash-object", "-t", "commit", "-w", "--literally", str(loose)).strip()
    git_in(a, "push", "-q", "-f", "origin", f"{commit}:{branch_of(head)}")
    why: list[str] = []
    assert approvals.reusable(head, why=why) is None
    assert why == [
        (
            f"origin holds the record branch {branch_of(head)}, but it is not this pass: its commit names no"
            " committer, so its time is unknown"
        )
    ]
    real = approvals.git

    def no_fetch(*args: str) -> str:
        if args[0] == "fetch":
            raise subprocess.CalledProcessError(128, "git", stderr="fatal: the remote hung up\n")
        return real(*args)

    monkeypatch.setattr(approvals, "git", no_fetch)
    why.clear()
    assert approvals.reusable(head, why=why) is None
    assert why == [
        f"origin's record branch {branch_of(head)} could not be read: fatal: the remote hung up"
    ]


def test_a_ref_for_another_tree_or_command_is_not_reused(
    hub: dict[str, list[dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The record is found by the tree asked about and today's gate command: another head's tree, or a changed gate,
    looks for another name and runs the gate."""
    head, _ = pushed_from_a_cloud(tmp_path, monkeypatch, capsys)
    b = tmp_path / "b"
    assert approvals.reusable(head) is not None
    base = git_in(b, "rev-parse", "origin/main").strip()
    assert approvals.reusable(base) is None
    monkeypatch.setattr(approvals, "LOCAL_CI", ["true", "--another-tier"])
    assert approvals.reusable(head) is None


def test_a_record_on_the_prs_fork_does_not_count(
    hub: dict[str, list[dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Acceptance 5 (#800's fork ref): only origin, the base repo, is read, since a fork's owner may push any branch
    to the fork."""
    real_refs(monkeypatch)
    fork = tmp_path / "fork.git"
    origin = tmp_path / "origin.git"
    git_in(tmp_path, "init", "-q", "--bare", str(origin))
    git_in(tmp_path, "init", "-q", "--bare", str(fork))
    a, b = tmp_path / "a", tmp_path / "b"
    committed(a)
    git_in(a, "remote", "add", "origin", str(fork))  # the fork's clone
    head = git_in(a, "rev-parse", "HEAD").strip()
    git_in(a, "push", "-q", "origin", "HEAD:refs/heads/main")
    git_in(a, "push", "-q", str(origin), "HEAD:refs/heads/main")
    git_in(tmp_path, "clone", "-q", str(origin), str(b))
    git_in(b, "remote", "add", "fork", str(fork))  # the host fetches the PR's fork too
    monkeypatch.setattr(approvals, "LOCAL_CI", ["true"])
    monkeypatch.setattr(approvals, "one_repo", lambda: None)
    monkeypatch.setattr(approvals, "post_status", no_status)
    monkeypatch.chdir(a)
    assert approvals.main(["local-ci", "--record"]) == 0
    assert (
        "published in origin as the record branch refs/heads/loop-gate/" in capsys.readouterr().out
    )
    assert git_in(fork, "for-each-ref", "refs/heads/loop-gate/")
    monkeypatch.chdir(b)
    assert approvals.reusable(head) is None
    ref = branch_of(head)
    record = git_in(fork, "rev-parse", ref).strip()
    git_in(a, "push", "-q", str(origin), f"{record}:{ref}")  # the same record, in the base
    assert approvals.reusable(head)["ref"] == ref


def test_origin_refusing_refs_outside_branches_still_takes_the_record(
    hub: dict[str, list[dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A cloud session's proxy pushes only branches: the record is one, which another clone reuses."""
    origin = tmp_path / "origin.git"
    git_in(tmp_path, "init", "-q", "--bare", str(origin))
    hook = origin / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\nwhile read old new ref; do case $ref in refs/heads/*) ;; *) echo refused $ref; exit 1;; esac;"
        " done\n"
    )
    hook.chmod(0o755)
    head, out = pushed_from_a_cloud(tmp_path, monkeypatch, capsys)
    assert f"published in origin as the record branch {branch_of(head)}" in out
    assert approvals.reusable(head)["ref"] == branch_of(head)


def recorded_in_origin(b: Path, tree_of_commit: str, when: str) -> str:
    """Push from clone `b` a record of `true` on `tree_of_commit`'s tree, made at `when`; returns its branch."""
    held = approvals.tree_of(tree_of_commit)
    record = {"head": tree_of_commit, "tree": held, "command": "true", "env": {}, "time": when}
    return approvals.push_record(record)


def open_prs(*heads: str) -> Any:
    """gh answering `api --paginate .../pulls?state=open --jq <items>` for open PRs on `heads` as gh prints it: one
    line per item, a string bare and anything else as compact JSON."""

    def gh(*args: str) -> str:
        assert args[:2] == ("api", "--paginate") and "pulls?state=open" in args[2], args
        assert args[3] == "--jq", args
        selector = args[4]
        assert selector.startswith(".[]"), selector
        lines = []
        for number, sha in enumerate(heads, 1):
            item: Any = {"number": number, "head": {"sha": sha, "ref": f"pr-{number}"}}
            for key in filter(None, selector.removeprefix(".[]").split(".")):
                item = item[key]
            lines.append(item if isinstance(item, str) else json.dumps(item, separators=(",", ":")))
        return "".join(f"{line}\n" for line in lines)

    return gh


def loop_gate_branches(origin: Path) -> set[str]:
    return set(
        git_in(origin, "for-each-ref", "--format=%(refname)", "refs/heads/loop-gate/").split()
    )


def test_land_deletes_the_records_it_landed_and_old_ones_no_open_pr_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance 4: after land, the landed head's record branch is gone, fresh or not; a record over a day old whose
    tree is no open PR's head (a replaced head) is deleted; a fresh one (its PR not open yet) and an old one for an
    open PR's head stay. Another command's record of the landed tree goes too."""
    head = published_on_a_with(tmp_path, monkeypatch, approvals.post_status)
    a, b, origin = tmp_path / "a", tmp_path / "b", tmp_path / "origin.git"
    commits = {}
    for name in ("replaced", "unopened", "open"):
        (a / "app.py").write_text(f"value = {name!r}\n")
        git_in(a, "commit", "-qam", name)
        commits[name] = git_in(a, "rev-parse", "HEAD").strip()
        git_in(a, "push", "-q", "origin", f"HEAD:refs/heads/{name}")
    git_in(b, "fetch", "-q", "origin")
    old, fresh = "2026-01-01T00:00:00Z", approvals.now()
    kept = {
        recorded_in_origin(b, commits["unopened"], fresh),
        recorded_in_origin(b, commits["open"], old),
    }
    replaced = recorded_in_origin(b, commits["replaced"], old)
    landed_other = approvals.push_record(
        {
            "head": head,
            "tree": approvals.tree_of(head),
            "command": "true --x",
            "env": {},
            "time": old,
        }
    )
    monkeypatch.setattr(approvals.github, "gh", open_prs(commits["open"]))
    capsys.readouterr()
    approvals.drop_records([head])
    left = set(
        git_in(origin, "for-each-ref", "--format=%(refname)", "refs/heads/loop-gate/").split()
    )
    assert left == kept
    assert not {branch_of(head), replaced, landed_other} & left
    assert capsys.readouterr().out == ""


def test_a_refused_delete_is_said_and_without_the_open_prs_only_landed_records_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    head = published_on_a_with(tmp_path, monkeypatch, approvals.post_status)
    b, origin = tmp_path / "b", tmp_path / "origin.git"
    git_in(b, "fetch", "-q", "origin")
    other = recorded_in_origin(
        b, git_in(b, "rev-parse", "origin/main").strip(), "2026-01-01T00:00:00Z"
    )

    def unreadable(*_args: Any, **_kwargs: Any) -> Any:
        raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 403")

    monkeypatch.setattr(approvals.github, "rest_pages", unreadable)
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho deleting is refused\nexit 1\n")
    hook.chmod(0o755)
    approvals.drop_records([head])
    assert f"the record branch {branch_of(head)} was not deleted: remote: deleting is refused" in (
        capsys.readouterr().out
    )
    hook.unlink()
    approvals.drop_records([head])
    left = git_in(origin, "for-each-ref", "--format=%(refname)", "refs/heads/loop-gate/").split()
    assert left == [other]  # old, but whether an open PR needs it is unknown


def test_an_old_record_goes_while_a_pr_is_open_reading_the_open_prs_as_gh_prints_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """F1: the open PRs come through rest_pages() from gh's own output, which JSON-decodes each line; an open PR
    on the published head keeps its record, and an old record of another tree goes."""
    head = published_on_a_with(tmp_path, monkeypatch, approvals.post_status)
    b, origin = tmp_path / "b", tmp_path / "origin.git"
    stale = recorded_in_origin(
        b, git_in(b, "rev-parse", "origin/main").strip(), "2026-01-01T00:00:00Z"
    )
    monkeypatch.setattr(approvals.github, "gh", open_prs(head))
    capsys.readouterr()
    approvals.drop_records([])
    left = loop_gate_branches(origin)
    assert stale not in left
    assert left == {branch_of(head)}
    assert capsys.readouterr().out == ""


def test_an_open_head_whose_tree_is_unknown_keeps_every_record_but_the_landed_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2: an open PR's head this clone can't fetch (a fork's) has no tree here, so whether an old record is its
    head's is unknown: only the landed trees' records go, as when the open PRs are unreadable."""
    real_refs(monkeypatch)
    origin = tmp_path / "origin.git"
    git_in(tmp_path, "init", "-q", "--bare", str(origin))
    author, clone = tmp_path / "author", tmp_path / "clone"
    committed(author)
    git_in(author, "remote", "add", "origin", str(origin))
    git_in(author, "push", "-q", "origin", "HEAD:refs/heads/main")
    git_in(tmp_path, "clone", "-q", "--single-branch", "--branch", "main", str(origin), str(clone))
    for value in ("2", "3"):
        (author / "app.py").write_text(f"value = {value}\n")
        git_in(author, "commit", "-qam", f"value {value}")
    fork = git_in(author, "rev-parse", "HEAD").strip()
    landed = git_in(author, "rev-parse", "HEAD~1").strip()
    git_in(author, "push", "-q", "origin", f"{landed}:refs/heads/main")
    monkeypatch.chdir(author)
    old = "2026-01-01T00:00:00Z"
    kept = recorded_in_origin(author, fork, old)
    gone = recorded_in_origin(author, landed, old)
    monkeypatch.chdir(clone)
    git_in(clone, "fetch", "-q", "origin")
    monkeypatch.setattr(approvals.github, "gh", open_prs(fork))
    # origin has the fork's head under its record branch, so its fetch is made to have failed already this run
    monkeypatch.setattr(approvals, "TREE_FETCHED", {(os.getcwd(), fork)})
    assert approvals.tree_of(fork) is None
    approvals.drop_records([landed])
    assert loop_gate_branches(origin) == {kept}
    assert gone not in loop_gate_branches(origin)


def test_a_landing_leaves_no_record_of_the_landed_head_and_publishes_no_pass_on_the_tip(
    hub: dict[str, list[dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F3: merges() runs no gate and publishes no pass on the new tip (#1872); the record clean-up leaves origin with no
    record of the landed head, and the tip shows only the `land` status."""
    head = published_on_a_with(tmp_path, monkeypatch, approvals.post_status)
    b, origin = tmp_path / "b", tmp_path / "origin.git"
    tree = approvals.tree_of(head)
    assert branch_of(head) in loop_gate_branches(origin)
    listing = open_prs()

    def gh(*args: str) -> str:
        if args[:2] == ("pr", "merge"):
            git_in(b, "push", "-q", "origin", f"{head}:refs/heads/main")
            return ""
        return listing(*args)

    monkeypatch.setattr(approvals.github, "gh", gh)
    # the squash body is tested on its own
    monkeypatch.setattr(approvals, "pull_request", lambda _n: {"state": "MERGED"})
    monkeypatch.setattr(approvals, "comment", lambda *_a: None)
    hub.clear()
    capsys.readouterr()
    approvals.merges([({**pull(), "headRefOid": head}, tree)], "main", head, LOOP, set())
    assert loop_gate_branches(origin) == set()
    assert "published in origin" not in capsys.readouterr().out
    assert [found["context"] for found in hub[head]] == ["land"]


def test_land_deletes_the_record_branches_of_the_prs_it_merged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dropped: list[list[str]] = []
    monkeypatch.setattr(approvals, "drop_records", dropped.append)
    monkeypatch.setattr(approvals.github, "gh", lambda *_a, **_k: "")
    # the squash body is tested on its own
    monkeypatch.setattr(approvals, "pull_request", lambda _n: {"state": "MERGED"})
    monkeypatch.setattr(approvals, "git", lambda *args: TREE if args[0] == "rev-parse" else "")
    monkeypatch.setattr(approvals, "comment", lambda *_a: None)
    monkeypatch.setattr(approvals, "project", lambda: (None, LOOP))
    approvals.merges([(pull(), TREE)], "main", BASE, LOOP, set())
    assert dropped == [[HEAD]]


# --- risk.merge_paths: paths whose merge only the owner's go on the head allows (#791) ---

ORDERS = loop_md("risk:\n  merge_paths:\n    - src/orders/**\n")
ORDERS_AUTO = loop_md("spec:\n  auto: true\nrisk:\n  merge_paths:\n    - src/orders/**\n")
ORDERS_HELD = (
    f"PR #7 waits for the owner's go on its head (`approve merge 7 --head {HEAD} --by owner`; the delegate's go and"
    " `approved:merge` are no go), since it touches `src/orders/place.py`, which `risk.merge_paths` names"
    " (`src/orders/**`), so only the owner approves its merge"
)
# a satisfied pass that judges the change low, so neither label nor verifier holds it
LOW = {**VERDICT, "body": VERDICT["body"] + "Risk: low\n"}


def orders_pull(*files: str, labels: tuple[str, ...] = ()) -> dict[str, Any]:
    return pull(reviews=(LOW,), files=files or ("src/orders/place.py",), labels=labels)


@pytest.mark.parametrize("loop", [ORDERS, ORDERS_AUTO], ids=["spec-auto-off", "spec-auto-on"])
def test_merge_paths_hold_a_matching_pr_for_the_owners_go_naming_the_path(
    loop: str, hub: dict[str, list[dict[str, Any]]], no_origin: list[list[str]]
) -> None:
    """No `risk:high`, a verifier that says low, and still the owner's go on the head; it covers that head only."""
    issues = [approved(1, "area:web")]
    assert merge(orders_pull(), issues, loop) == [ORDERS_HELD]
    owned = given_go(orders_pull(), hub)
    assert merge(owned, issues, loop) == []
    # a new push: the go was on the head the owner saw, not this one
    pushed = {**owned, "headRefOid": "c" * 40}
    assert merge(pushed, issues, loop)[-1] == ORDERS_HELD.replace(HEAD, "c" * 40)


def test_merge_paths_hold_a_pr_whose_files_the_gate_cannot_read_all_of() -> None:
    unread = orders_pull("README.md")
    unread["files"]["totalCount"] = 101
    assert approvals.owner_waits(unread, [], ORDERS) == [
        ORDERS_HELD.split(", since ")[0]
        + ", since the gate can't read every file the PR changes, so `risk.merge_paths` holds until the owner"
        " judges them"
    ]


def test_a_pr_touching_no_merge_path_behaves_as_without_the_setting(
    hub: dict[str, list[dict[str, Any]]],
) -> None:
    issues = [approved(1, "area:web")]
    for files in (("web/app.ts",), ("src/orderly.py",), ("src/orders",)):
        assert merge(orders_pull(*files), issues, ORDERS) == merge(
            orders_pull(*files), issues, BARE
        )
        assert merge(orders_pull(*files), issues, ORDERS) == []
    # a high-risk one still takes the delegate's go or the label where it did before
    feature = into_feature(orders_pull("web/app.ts", labels=("risk:high", "approved:merge")))
    assert approvals.owner_waits(feature, issues, ORDERS) == []
    assert approvals.settings(BARE).get("risk") is None


def test_merge_paths_take_no_label_and_no_delegate_status_as_the_go(
    hub: dict[str, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # the default branch's policy is the one passed here, not the repo's real origin/main (#842)
    labelled = into_feature(orders_pull(labels=("risk:high", "approved:merge")))
    held = ORDERS_HELD
    assert approvals.owner_waits(labelled, [], ORDERS) == [held]
    # the delegate's status and record on the head, with `delegate: all`, into main and into a feature branch
    delegated = orders_pull()
    delegated["comments"]["nodes"].append(note(DELEGATE_RECORD))
    hub[HEAD] = [status("owner", DELEGATE_GO)]
    everyone = add(ORDERS, "loop", "delegate: all\n")
    assert approvals.owned(delegated, everyone) == "delegate"
    assert approvals.owner_waits(delegated, [], everyone) == [held]
    assert approvals.owner_waits(into_feature(delegated), [], everyone) == [held]


@pytest.fixture
def ordering(landing: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The landing checkout with `risk.merge_paths` and `delegate: all`, a low-risk PR touching a listed path; each
    comment and label lands on the PR, as GitHub reads it back."""
    Path("docs/agents/loop.md").write_text(
        add(
            add(GITHUB_RULE, "risk", "merge_paths:\n  - src/orders/**\n"), "loop", "delegate: all\n"
        )
    )
    landing["pull"]["files"]["nodes"] = [{"path": "src/orders/place.py"}]
    landing["pull"]["reviews"]["nodes"] = [LOW]
    landing["posted"] = []

    def comment(number: int, body: str) -> None:
        landing["posted"].append((number, approvals.machine(body)))
        landing["pull"]["comments"]["nodes"].append(note(body))

    def label(number: int, name: str, add: bool) -> None:
        landing["posted"].append((number, name, add))
        nodes = landing["pull"]["labels"]["nodes"]
        nodes[:] = [n for n in nodes if n["name"] != name] + [{"name": name}] * add

    monkeypatch.setattr(approvals, "comment", comment)
    monkeypatch.setattr(approvals, "label", label)
    return landing


def test_check_merge_refuses_a_delegates_go_on_a_merge_path_and_passes_the_owners_until_a_push(
    ordering: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert gate(capsys, "check", "merge", "7") == (approvals.WAITING, [ORDERS_HELD])
    assert (7, "risk:high", True) in ordering["posted"]  # the board shows why it waits
    ordering["posted"].clear()
    code, lines = gate(capsys, "approve", "merge", "7", "--head", HEAD, "--by", "delegate")
    assert (code, ordering["posted"], hub) == (1, [], {})
    assert lines == [
        (
            "PR #7: the delegate's go is no go here, since it touches `src/orders/place.py`, which"
            " `risk.merge_paths` names (`src/orders/**`), so only the owner approves its merge: it waits for the"
            " owner's"
        )
    ]
    into_feature(ordering["pull"])
    code, lines = gate(capsys, "delegate", "7")
    assert (code, ordering["posted"]) == (1, [])
    assert lines[0].startswith(
        "PR #7: `approved:merge` is no go here, since it touches `src/orders/place.py`"
    )
    ordering["pull"]["baseRefName"] = "main"
    assert gate(capsys, "approve", "merge", "7", "--head", HEAD, "--by", "owner")[0] == 0
    assert gate(capsys, "check", "merge", "7") == (0, [f"PR #7: the go on {HEAD} is the owner's"])
    ordering["pull"]["headRefOid"] = "c" * 40
    ordering["pull"]["reviews"]["nodes"] = [{**LOW, "commit": {"oid": "c" * 40}}]
    assert gate(capsys, "check", "merge", "7") == (
        approvals.WAITING,
        [ORDERS_HELD.replace(HEAD, "c" * 40)],
    )


def test_land_never_merges_a_merge_path_pr_without_the_owners_go_on_its_head(
    lands: dict[str, Any], hub: dict[str, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals, "label", lambda *args, **kwargs: None)
    loop = lands["work"] / "docs" / "agents" / "loop.md"
    loop.write_text(
        add(
            add(GITHUB_RULE, "risk", "merge_paths:\n  - src/orders/**\n"), "loop", "delegate: all\n"
        )
    )
    head = lands["open"](7, {"src/orders/place.py": "orders\n"})
    pr = lands["pulls"][7]
    pr["files"] = {"nodes": [{"path": "src/orders/place.py"}]}
    pr["reviews"]["nodes"] = [{**LOW, "commit": {"oid": head}}]
    # the delegate's go, under `delegate: all`, is no go here
    pr["comments"]["nodes"].append(note(DELEGATE_RECORD.replace(HEAD, head)))
    hub[head] = [status("owner", f"The delegate's go on {head[:12]}, under the owner's delegation")]
    code, lines = lands["land"]("7")
    assert code == approvals.WAITING
    assert ORDERS_HELD.replace(HEAD, head) in lines
    assert lands["merged"]() == []
    pr["comments"]["nodes"].append(note(OWNER_RECORD.replace(HEAD, head)))
    hub[head].insert(0, status("owner", f"The owner's go on {head[:12]}"))
    code, lines = lands["land"]("7")
    assert code == 0, lines
    assert lands["merged"]() == ["7"]


@pytest.mark.parametrize(
    ("change", "old", "new"),
    [
        ("RENAMED", "src/orders/place.py", "outside/place.py"),
        ("COPIED", "src/orders/place.py", "outside/copy.py"),
        ("RENAMED", "outside/place.py", "src/orders/place.py"),
    ],
    ids=["renamed-out", "copied-out", "renamed-in"],
)
def test_merge_paths_and_risk_paths_match_a_moved_files_old_name_as_well_as_its_new_one(
    change: str, old: str, new: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub's GraphQL names only a moved file's new path: the old one is read from the REST file list."""
    asked: list[int] = []

    def changes(number: int) -> list[dict[str, Any]]:
        asked.append(number)
        return [{"filename": new, "previous_filename": old, "status": change.lower()}]

    monkeypatch.setattr(approvals, "pr_changes", changes)
    moved = orders_pull(new)
    moved["files"]["nodes"][0]["changeType"] = change
    held = ORDERS_HELD.replace(
        "`src/orders/place.py`", f"`{new if new.startswith('src/orders/') else old}`"
    )
    assert approvals.owner_waits(moved, [], ORDERS) == [held]
    assert sorted(approvals.touched(moved)) == sorted({old, new})
    assert asked == [7]  # read once per PR
    rule = loop_md("risk:\n  paths:\n    - src/orders/**\n")
    assert approvals.risk(moved, [], rule) == "it matches a risk rule (path `src/orders/**`)"


def test_a_moved_file_whose_old_name_github_wont_give_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refused(_number: int) -> list[dict[str, Any]]:
        raise subprocess.CalledProcessError(1, ("gh", "api"), stderr="HTTP 502")

    monkeypatch.setattr(approvals, "pr_changes", refused)
    moved = orders_pull("outside/place.py")
    moved["files"]["nodes"][0]["changeType"] = "RENAMED"
    assert approvals.touched(moved) is None
    assert approvals.owner_waits(moved, [], ORDERS) == [
        ORDERS_HELD.split(", since ")[0]
        + ", since the gate can't read every file the PR changes, so `risk.merge_paths` holds until the owner"
        " judges them"
    ]
    assert approvals.risk(moved, [], GITHUB_RULE) == (
        "the gate can't read every file the PR changes, so the risk rule's `paths` hold until a person judges them"
    )


@pytest.mark.parametrize(
    "answer",
    [
        [],
        [{"filename": "outside/place.py", "status": "renamed"}],
        [{"filename": "other/new.py", "previous_filename": "other/old.py", "status": "renamed"}],
        [{"filename": "outside/b.py", "previous_filename": "b.py", "status": "renamed"}],
    ],
    ids=["empty", "no-old-name", "another-file", "partial"],
)
def test_a_file_list_that_gives_no_old_name_for_each_moved_file_holds(
    answer: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A REST answer that succeeds but lacks a moved file's old name (an entry without it, or a list that changed
    since the GraphQL read) proves nothing about where that file came from, so both rules hold."""
    monkeypatch.setattr(approvals, "pr_changes", lambda _n: answer)
    moved = orders_pull("outside/place.py", "outside/b.py")
    for node in moved["files"]["nodes"]:
        node["changeType"] = "RENAMED"
    assert approvals.touched(moved) is None
    assert approvals.owner_waits(moved, [], ORDERS)[0].endswith(
        "since the gate can't read every file the PR changes, so `risk.merge_paths` holds until the owner judges them"
    )
    assert approvals.risk(moved, [], GITHUB_RULE) == (
        "the gate can't read every file the PR changes, so the risk rule's `paths` hold until a person judges them"
    )


def test_a_deleted_file_under_a_merge_path_holds_the_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(approvals, "pr_changes", lambda _n: pytest.fail("no old name to read"))
    gone = orders_pull()
    gone["files"]["nodes"][0]["changeType"] = "DELETED"
    assert approvals.owner_waits(gone, [], ORDERS) == [ORDERS_HELD]


def test_check_build_before_the_pr_lists_a_moved_files_old_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, clone = tmp_path / "origin.git", tmp_path / "clone"
    git_in(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    committed(clone)
    git_in(clone, "branch", "-M", "main")
    (clone / "src" / "orders").mkdir(parents=True)
    (clone / "src" / "orders" / "place.py").write_text("orders\n" * 20)
    git_in(clone, "add", "-A")
    git_in(clone, "commit", "-qm", "orders")
    git_in(clone, "remote", "add", "origin", str(origin))
    git_in(clone, "push", "-q", "origin", "main")
    git_in(clone, "remote", "set-head", "origin", "main")
    git_in(clone, "checkout", "-q", "-b", "loop/1-fix")
    (clone / "outside").mkdir()
    git_in(clone, "mv", "src/orders/place.py", "outside/place.py")
    git_in(clone, "commit", "-qm", "move")
    monkeypatch.chdir(clone)
    assert sorted(approvals.paths(approvals.unopened())) == [
        "outside/place.py",
        "src/orders/place.py",
    ]


def gate_sees(tmp_path: Path, launcher: list[str], env: dict[str, str]) -> dict[str, str | None]:
    """The VIRTUAL_ENV, PATH and UV_RUN_RECURSION_DEPTH the gate command sees when the real script, started by
    `launcher` with `env` over a bare one, runs `local-ci --record` in a checkout whose gate dumps them."""
    repo, seen = tmp_path / "repo", tmp_path / "seen.json"
    (repo / "docs" / "agents").mkdir(parents=True)
    (repo / "docs" / "agents" / "loop.md").write_text(loop_md("loop:\n  gate: python3 dump.py\n"))
    (repo / "dump.py").write_text(
        "import json, os\n"
        f"keys = ('VIRTUAL_ENV', 'PATH', 'UV_RUN_RECURSION_DEPTH')\n"
        f"json.dump({{k: os.environ.get(k) for k in keys}}, open({str(seen)!r}, 'w'))\n"
    )
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "x"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=repo,
            check=True,
            capture_output=True,
        )
    script = SCRIPTS / "approvals.py"
    run = subprocess.run(
        [*launcher, str(script), "local-ci", "--record"],
        cwd=repo,
        env={
            k: v
            for k, v in os.environ.items()
            if k not in ("VIRTUAL_ENV", "UV_RUN_RECURSION_DEPTH")
        }
        | env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    return json.loads(seen.read_text())


def caller_venv(tmp_path: Path) -> Path:
    """An environment a caller activated: a folder with a pyvenv.cfg and a bin."""
    venv = tmp_path / "caller-venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (venv / "bin" / "python3").symlink_to(
        sys.executable
    )  # uv inspects an active environment's interpreter
    return venv


UV = [shutil.which("uv") or "uv", "run", "-q", "--script"]
needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")


def test_the_gate_started_in_the_scripts_own_environment_does_not_see_it(tmp_path: Path) -> None:
    """The script's entry point strips its own environment before the gate starts."""
    own_bin = os.path.join(sys.prefix, "bin")
    seen = gate_sees(
        tmp_path,
        [sys.executable],
        {"VIRTUAL_ENV": sys.prefix, "PATH": os.pathsep.join([own_bin, "/usr/bin", "/bin"])},
    )
    assert seen["VIRTUAL_ENV"] is None
    assert own_bin not in (seen["PATH"] or "").split(os.pathsep) and "/usr/bin" in seen["PATH"]


def test_a_virtual_env_the_caller_set_on_purpose_reaches_the_gate(tmp_path: Path) -> None:
    path = os.pathsep.join([os.path.join(sys.prefix, "bin"), "/usr/bin", "/bin"])
    elsewhere = str(tmp_path / "project-venv")
    seen = gate_sees(tmp_path, [sys.executable], {"VIRTUAL_ENV": elsewhere, "PATH": path})
    assert seen == {"VIRTUAL_ENV": elsewhere, "PATH": path, "UV_RUN_RECURSION_DEPTH": None}


# the cases a path heuristic can't tell apart: uv replaces the caller's VIRTUAL_ENV before the script starts, so
# neither is reconstructed, whether or not the caller activated an environment
CALLERS = {
    "activated, another directory in front of its bin": lambda venv: (
        str(venv),
        ["/usr/bin", str(venv / "bin"), "/bin"],
    ),
    "not activated, its bin first on PATH": lambda venv: (
        None,
        [str(venv / "bin"), "/usr/bin", "/bin"],
    ),
}


@needs_uv
@pytest.mark.parametrize("case", CALLERS)
def test_under_uv_run_the_gate_gets_no_virtual_env_and_the_callers_path(
    tmp_path: Path, case: str
) -> None:
    """The gate gets no VIRTUAL_ENV, never a guessed one, and the caller's PATH without the script's own bin."""
    virtual_env, entries = CALLERS[case](caller_venv(tmp_path))
    path = os.pathsep.join(entries)
    seen = gate_sees(
        tmp_path, UV, {"PATH": path} | ({"VIRTUAL_ENV": virtual_env} if virtual_env else {})
    )
    assert seen["VIRTUAL_ENV"] is None
    assert seen["PATH"] == path


def test_a_gate_nested_under_another_uv_run_sees_the_callers_environment_depth(
    tmp_path: Path,
) -> None:
    """uv counts each nested run in UV_RUN_RECURSION_DEPTH; the gate gets the count minus this script's."""
    seen = gate_sees(
        tmp_path,
        [sys.executable],
        {"VIRTUAL_ENV": sys.prefix, "UV_RUN_RECURSION_DEPTH": "2", "PATH": "/usr/bin:/bin"},
    )
    assert seen["UV_RUN_RECURSION_DEPTH"] == "1"


def test_land_only_lands_the_named_pr_and_leaves_the_other_untouched(lands: dict[str, Any]) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    other = lands["open"](8, {"8.txt": "8\n"})
    code, lines = lands["land"]("7", "--only")
    assert code == 0, lines
    assert lands["merged"]() == ["7"]
    # the other PR: no ready or merge call, no comment, still open on its own head
    assert all(args[-1] != "8" and args[2] != "8" for args in lands["calls"])
    assert [n for n, _ in lands["comments"]] == [7]
    assert (lands["pulls"][8]["state"], lands["pulls"][8]["headRefOid"]) == ("OPEN", other)
    assert lands["lock"]() == ""
    # without the flag it lands with the next
    assert lands["land"]()[0] == 0 and lands["merged"]() == ["7", "8"]


def test_land_only_with_wait_lands_the_named_pr_alone(lands: dict[str, Any]) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    lands["open"](8, {"8.txt": "8\n"})
    assert lands["land"]("7", "--only", "--wait")[0] == 0
    assert lands["merged"]() == ["7"]
    assert [n for n, _ in lands["comments"]] == [7]


def test_land_only_without_a_pr_is_refused(
    lands: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    lands["open"](7, {"7.txt": "7\n"})
    with pytest.raises(SystemExit) as exited:
        approvals.main(["land", "--only"])
    assert exited.value.code == 2
    assert "--only" in capsys.readouterr().err
    assert lands["merged"]() == [] and lands["calls"] == []


# --- without GraphQL: a cloud session's proxy refuses it, and REST answers the same reads ---

REFUSED_GRAPHQL = "gh: GitHub GraphQL is not available from Claude Code sessions (HTTP 403)"
NAME_WITH_OWNER = "kzarzycki/scratch-gate-revert"


def user(login: str, kind: str = "User") -> dict[str, Any]:
    return {"login": login, "type": kind}


def rest_comment(body: str, by: str = "loop", **fields: Any) -> dict[str, Any]:
    return {
        "node_id": f"IC_{next(IDS)}",
        "user": user(by),
        "body": body,
        "created_at": WRITTEN,
        "updated_at": WRITTEN,
        "minimized": None,
        **fields,
    }


def labeled(name: str, by: str = "octocat", at: str = LABELED) -> dict[str, Any]:
    return {"event": "labeled", "created_at": at, "actor": user(by), "label": {"name": name}}


def cloud_routes(spec: str = "the spec", record_of: str | None = None) -> dict[str, Any]:
    """PR #7 and issue #1 as GitHub's REST API returns them: #7 closes #1, whose spec the owner approved by its text
    (or by `record_of`), and #7 has a green `check`, a satisfied verifier review on its head and no open thread."""
    approved_text = record_of or "sha256 " + hashlib.sha256(spec.encode()).hexdigest()
    pages = {
        "pulls/7": {
            "node_id": "PR_7",
            "number": 7,
            "title": "Feature",
            "user": user("loop"),
            "assignees": [user("octocat")],
            "state": "open",
            "merged": False,
            "draft": False,
            "auto_merge": None,
            "body": "Closes #1\n\n## Evidence\n\nlog line\n",
            "base": {
                "ref": "main",
                "sha": "0" * 40,  # stale: the base's tip is read from its branch
                "repo": {"full_name": NAME_WITH_OWNER, "default_branch": "main"},
            },
            "head": {"ref": "feature", "sha": HEAD, "repo": {"full_name": NAME_WITH_OWNER}},
            "labels": [],
            "changed_files": 1,
            "commits": 1,
        },
        "branches/main": {"commit": {"sha": BASE}},
        "pulls/7/reviews?per_page=100": [
            {
                "user": user("loop"),
                "state": "COMMENTED",
                "body": VERDICT["body"],
                "commit_id": HEAD,
            }
        ],
        "pulls/7/files?per_page=100": [
            {"filename": "app.py", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"}
        ],
        "issues/7/comments?per_page=100": [],
        "pulls/7/ccr/review_threads": [{"resolved": True, "comment_ids": [1]}],
        "pulls/7/comments?per_page=100": [],
        "pulls/7/commits?per_page=100": [
            {
                "author": user("loop"),
                "committer": user("web-flow"),
                "commit": {"message": "the change"},
            }
        ],
        "issues/7/timeline?per_page=100": [],
        f"commits/{HEAD}/check-runs?filter=all&per_page=100": {
            "total_count": 1,
            "check_runs": [
                {
                    "name": "check",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": PUSHED,
                    "completed_at": PUSHED,
                    "check_suite": {"id": 5},
                }
            ],
        },
        f"commits/{HEAD}/check-suites?per_page=100": {
            "check_suites": [{"id": 5, "status": "completed"}]
        },
        f"actions/runs?head_sha={HEAD}&per_page=100": {
            "workflow_runs": [{"check_suite_id": 5, "name": "CI"}]
        },
        f"commits/{HEAD}/status?per_page=100": {"total_count": 0, "statuses": []},
        "issues/1": {
            "number": 1,
            "state": "open",
            "body": spec,
            "created_at": WRITTEN,
            "updated_at": LABELED,
            "labels": [
                {"name": name}
                for name in ("kind:enhancement", "area:web", "size:S", "approved:spec")
            ],
            "user": user("loop"),
            "assignees": [user("octocat")],
        },
        "issues/1/comments?per_page=100": [
            rest_comment(record("spec", "owner", spec=approved_text))
        ],
        "issues/1/timeline?per_page=100": [labeled("approved:spec")],
        "pulls?state=open&per_page=100": [
            {
                "number": 7,
                "head": {"ref": "feature"},
                "base": {"ref": "main"},
                "draft": False,
                "body": "Closes #1",
            }
        ],
        "pulls?state=closed&base=feature&per_page=100": [],
        f"compare/{BASE}...{HEAD}": {
            "files": [
                {
                    "filename": "app.py",
                    "patch": "@@ -1,2 +10,4 @@\n a\n+b\n+c\n d",
                }
            ]
        },
        "": {"permissions": {"push": True}},
    }
    return {f"repos/{{owner}}/{{repo}}/{path}".rstrip("/"): page for path, page in pages.items()}


class Cloud:
    """GitHub behind a cloud session's proxy: REST answers from `routes`, GraphQL (`gh api graphql`, and `gh pr` and
    `gh repo`, which read through it) is refused with 403, as is the collaborator permission path. Every call is kept
    in order, and every write is kept in `writes` instead of made."""

    FORBIDDEN = ("/collaborators/",)

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes, self.calls, self.writes = routes, [], []

    @staticmethod
    def graphql(args: tuple[str, ...]) -> bool:
        return "graphql" in args or args[0] in ("pr", "repo")

    def __call__(self, *args: str, data: str | None = None) -> str:
        self.calls.append(args)
        if self.graphql(args):
            raise subprocess.CalledProcessError(1, "gh", stderr=REFUSED_GRAPHQL)
        assert args[0] == "api", args
        rest = [arg for arg in args[1:] if arg != "--paginate"]
        path = rest[0]
        if data is not None or any(flag in rest for flag in ("-X", "-f", "-F", "--input")):
            self.writes.append((path, data))
            return ""
        if any(part in path for part in self.FORBIDDEN):
            raise subprocess.CalledProcessError(1, "gh", stderr="not permitted (HTTP 403)")
        if path not in self.routes:
            raise subprocess.CalledProcessError(1, "gh", stderr=f"{path}: Not Found (HTTP 404)")
        page = self.routes[path]
        jq = rest[rest.index("--jq") + 1] if "--jq" in rest else None
        if jq is None:
            return json.dumps(page)
        if jq == ".[]":
            items = page
        elif jq.endswith("[]") and jq.startswith("."):
            items = page[jq[1:-2]]
        elif jq == ".files[] | {filename, patch}":
            items = [{"filename": f["filename"], "patch": f["patch"]} for f in page["files"]]
        else:
            raise AssertionError(f"jq not faked: {jq}")
        return "".join(json.dumps(item) + "\n" for item in items)


@pytest.fixture
def cloud(monkeypatch: pytest.MonkeyPatch) -> Cloud:
    """approvals.py against Cloud, run in the fixture project, with the real REST readers the autouse fixtures fake."""
    found = Cloud(cloud_routes())
    monkeypatch.setattr(approvals.github, "gh", found)
    monkeypatch.setattr(approvals, "merged_high", MERGED_HIGH)
    monkeypatch.setattr(approvals, "open_pulls", OPEN_PULLS)
    monkeypatch.setattr(approvals, "comment", COMMENT)
    monkeypatch.setattr(approvals, "one_repo", lambda: None)
    in_project(monkeypatch)
    return found


def test_in_a_cloud_session_each_command_completes_with_no_graphql_call_past_the_refused_one(
    cloud: Cloud,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """`check build`, `check merge`, `local-ci <pr>` and `verdict` complete for a PR with an approved spec, a green
    gate and a satisfied verifier review where GraphQL answers 403: the first GraphQL call is refused, and the run
    reads REST alone from then on, so no command makes a second. Each command is its own run."""
    record = {
        "head": HEAD,
        "tree": TREE,
        "command": "mise run check:all",
        "exit": 0,
        "env": {},
        "time": WRITTEN,
        "published": True,
    }
    monkeypatch.setattr(approvals, "reusable", lambda head, passed=(), why=None: record)
    report = tmp_path / "report.md"
    report.write_text(REPORT.replace("app.py:12", "app.py:11").replace("app.py:40", "app.py:12"))
    for argv in (
        ["check", "build", "7"],
        ["check", "merge", "7"],
        ["local-ci", "7"],
        ["verdict", "7", str(report)],
    ):
        monkeypatch.setattr(approvals.github, "_rest_only", False)  # a new run
        cloud.calls.clear()
        assert approvals.main(argv) == 0, (argv, capsys.readouterr().out)
        asked = [call for call in cloud.calls if Cloud.graphql(call)]
        assert len(asked) <= 1 and (not asked or cloud.calls.index(asked[0]) == 0), argv
    out = capsys.readouterr().out
    assert "has no `Approved: spec`" not in out and "verifier" not in out.lower(), out
    posted = [path for path, _data in cloud.writes]
    assert posted == [
        "repos/{owner}/{repo}/issues/7/comments",  # local-ci's pass
        "repos/{owner}/{repo}/pulls/7/reviews",  # verdict's review
    ]


def test_without_graphql_check_merge_still_fails_a_missing_proof(
    cloud: Cloud, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """REST reads every proof GraphQL did: an unresolved thread, a red required check and a spec changed after its
    approval each still hold the merge."""
    monkeypatch.setattr(approvals, "required_checks", lambda _base: {"check"})
    pages = cloud.routes
    pages["repos/{owner}/{repo}/pulls/7/ccr/review_threads"] = [{"resolved": False}]
    runs = pages[f"repos/{{owner}}/{{repo}}/commits/{HEAD}/check-runs?filter=all&per_page=100"]
    runs["check_runs"][0]["conclusion"] = "failure"
    pages["repos/{owner}/{repo}/issues/1"]["body"] = "the spec, edited"
    assert approvals.main(["check", "merge", "7"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert (
        "PR #7: 1 review thread(s) unresolved: fix and reply, or triage, then resolve (github-tracker's github.md, Review trail)"
        in out
    )
    assert "PR #7: `check` is FAILURE on the head commit" in out
    assert (
        "#1 has no `Approved: spec` record by the owner for its current spec: the owner approves it again"
        in out
    )


def test_review_threads_a_host_cannot_read_over_rest_or_graphql_fail_closed(cloud: Cloud) -> None:
    """GitHub's own REST API has no review threads: where the proxy's route is missing and GraphQL refuses the threads
    too, the read raises rather than count none unresolved."""
    del cloud.routes["repos/{owner}/{repo}/pulls/7/ccr/review_threads"]
    with pytest.raises(subprocess.CalledProcessError) as raised:
        approvals.pull_request(7)
    assert raised.value.stderr == REFUSED_GRAPHQL
    assert "reviewThreads" in " ".join(cloud.calls[-1])


def test_where_graphql_answers_it_is_the_one_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a host the PR and its issues come from one GraphQL call each, as before: REST is never asked."""
    calls: list[tuple[str, ...]] = []

    def gh(*args: str, data: str | None = None) -> str:
        calls.append(args)
        assert "graphql" in args, args
        if "pullRequest(number: $n)" in " ".join(args):
            return json.dumps({"data": {"repository": {"pullRequest": pull()}}})
        return json.dumps({"data": {"repository": {"issue": approved(1)}}})

    monkeypatch.setattr(approvals.github, "gh", gh)
    assert approvals.pull_request(7)["headRefOid"] == HEAD
    assert approvals.issue_node(1)["number"] == 1
    assert len(calls) == 2 and approvals.github._rest_only is False


def test_another_error_from_graphql_is_no_refusal_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def gh(*args: str, data: str | None = None) -> str:
        raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 502: Bad Gateway")

    monkeypatch.setattr(approvals.github, "gh", gh)
    with pytest.raises(subprocess.CalledProcessError) as raised:
        approvals.pull_request(7)
    assert "502" in raised.value.stderr
    assert approvals.github._rest_only is False


def test_rest_reads_a_pr_in_graphqls_shape(cloud: Cloud) -> None:
    """A bot's login loses the `[bot]` REST adds, GitHub's web-flow committer is no account, a review that only
    comments leaves a request for changes standing, a check run is named by its workflow, the base is its branch's tip,
    and what REST can't say (the merge queue, GitHub's closing links) is left for the caller to refuse or read from
    the body."""
    pages = cloud.routes
    pages["repos/{owner}/{repo}/pulls/7"]["user"] = user("renovate[bot]", "Bot")
    pages["repos/{owner}/{repo}/pulls/7/reviews?per_page=100"] = [
        {"user": user("ann"), "state": "CHANGES_REQUESTED", "body": "", "commit_id": HEAD},
        {"user": user("ann"), "state": "COMMENTED", "body": "a note", "commit_id": HEAD},
        {"user": user("bob"), "state": "CHANGES_REQUESTED", "body": "", "commit_id": HEAD},
        {"user": user("bob"), "state": "APPROVED", "body": "", "commit_id": HEAD},
    ]
    found = approvals.pull_request(7)
    assert found["author"] == {"login": "renovate", "__typename": "Bot"}
    assert found["baseRefOid"] == BASE
    assert found["authored"]["nodes"][0]["commit"]["committer"] == {"user": None}
    assert [(r["author"]["login"], r["state"]) for r in found["latestReviews"]["nodes"]] == [
        ("ann", "CHANGES_REQUESTED"),
        ("bob", "APPROVED"),
    ]
    [run] = found["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"]
    assert (approvals.workflow(run), run["conclusion"], run["checkSuite"]["status"]) == (
        "CI",
        "SUCCESS",
        "COMPLETED",
    )
    assert found["closingIssuesReferences"] == {"nodes": []}
    assert [issue["number"] for issue in approvals.closing_issues(found)] == [1]
    assert "doesn't say whether it is in a merge queue" in approvals.unsignable(
        found, "main", Path("signers")
    )


def test_a_co_author_trailer_in_rest_leaves_the_bot_exemption_but_another_account_refuses_it(
    cloud: Cloud,
) -> None:
    """A `Co-authored-by` trailer is text the committer wrote, not another account's push: REST exempts the PR with
    it, and still refuses a commit authored or committed by another account."""
    loop = add(BARE, "bot", "renovate:\n  paths:\n    - app.py\n")
    pages = cloud.routes
    pages["repos/{owner}/{repo}/pulls/7"]["user"] = user("renovate[bot]", "Bot")
    commit = pages["repos/{owner}/{repo}/pulls/7/commits?per_page=100"][0]
    commit["author"] = user("renovate[bot]", "Bot")
    assert approvals.exempt(approvals.pull_request(7), loop)
    approvals.github._rest_only = False
    commit["commit"]["message"] = "bump\n\nCo-authored-by: Mallory <m@example.com>"
    assert approvals.exempt(approvals.pull_request(7), loop)
    commit["author"] = user("mallory")
    assert not approvals.exempt(approvals.pull_request(7), loop)
    commit["author"] = user("renovate[bot]", "Bot")
    commit["committer"] = user("mallory")
    assert not approvals.exempt(approvals.pull_request(7), loop)
    commit["committer"] = None
    commit["author"] = None  # an author GitHub ties to no account
    assert not approvals.exempt(approvals.pull_request(7), loop)


def test_the_account_running_this_is_a_writer_by_its_own_permissions_where_the_proxy_refuses_the_lookup(
    cloud: Cloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals.github, "viewer", lambda: "loop")
    assert approvals.writer("loop")
    assert not approvals.writer("drive-by")  # anyone else still can't be looked up, so isn't one
    cloud.routes["repos/{owner}/{repo}"] = {"permissions": {"pull": True}}
    approvals.github.forget()  # a new run: one run keeps the permission it read (#1771)
    assert not approvals.writer("loop")


# --- an approval names the spec's text ---


def rest_shaped(node: dict[str, Any]) -> dict[str, Any]:
    """`node` as REST gives it: no edit time."""
    return {key: value for key, value in node.items() if key != "lastEditedAt"} | {
        "updatedAt": WRITTEN
    }


@pytest.mark.parametrize("shape", [lambda node: node, rest_shaped], ids=["graphql", "rest"])
def test_an_approval_of_the_spec_text_holds_until_the_text_changes(
    shape: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    spec = issue(
        1, "size:S", "approved:spec", "area:web", comments=(record("spec", "owner", spec=TEXTED),)
    )
    assert approvals.proofs("build", pull(), [shape(spec)], LOOP) == []
    spec["body"] = "the spec\nand one more line"
    assert approvals.proofs("build", pull(), [shape(spec)], LOOP) == [
        "#1 has no `Approved: spec` record by the owner for its current spec: the owner approves it again"
    ]
    spec["body"] = "the spec"  # exactly the approved text again: the approval holds again
    assert approvals.proofs("build", pull(), [shape(spec)], LOOP) == []
    spec["body"] = "the spec\r\n"  # line ends as a browser may send them: the text is the same
    spec["comments"]["nodes"][0]["body"] = record(
        "spec", "owner", spec=approvals.texted({"body": "the spec\n"})
    )
    assert approvals.proofs("build", pull(), [shape(spec)], LOOP) == []


def test_a_record_of_the_last_edit_holds_where_graphql_dates_it_and_over_rest_says_how_to_renew_it() -> (
    None
):
    """Records written before texts were named carry the edit time: GraphQL still checks them, REST can't, so it
    refuses with the command that renews the approval."""
    assert approvals.proofs("build", pull(), [approved(1, "area:web")], LOOP) == []
    assert approvals.proofs("build", pull(), [rest_shaped(approved(1, "area:web"))], LOOP) == [
        (
            "#1 has no `Approved: spec` record by the owner for its current text: its record names the spec's last"
            " edit, which GitHub's REST API can't date, so the owner approves it again (`approve spec 1 --by owner`)"
            " or the check runs where GraphQL answers"
        )
    ]


def test_over_rest_the_owners_label_is_recorded_only_when_nothing_changed_the_spec_after_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """REST has no edit time, so the spec's `updatedAt`, which every edit moves, dates its last change: a label the
    owner added after it is their word; one at or before it is never taken as covering the text."""
    loop = add(LOOP, "loop", "owner: octocat\n")
    spec = rest_shaped(issue(1, "kind:enhancement", "area:web", "size:S", "approved:spec"))
    spec["timelineItems"] = {
        "nodes": [
            {
                "__typename": "LabeledEvent",
                "createdAt": LABELED,
                "actor": {"__typename": "User", "login": "octocat"},
                "label": {"name": "approved:spec"},
            }
        ]
    }
    sent: list[str] = []
    monkeypatch.setattr(approvals, "comment", lambda _n, body: sent.append(approvals.machine(body)))
    monkeypatch.setattr(approvals, "issue_node", lambda _n: spec)
    spec["updatedAt"] = LABELED
    approvals.adopted([spec], loop)
    assert sent == []
    assert capsys.readouterr().out == (
        "#1: the owner's `approved:spec` can't be dated after the spec's last edit, since GitHub's REST API gives no"
        f" edit time and the spec changed at {LABELED}, after it: the owner records it with `approve spec 1 --by"
        " owner`, or the check runs where GraphQL answers\n"
    )
    spec["updatedAt"] = PUSHED
    approvals.adopted([spec], loop)
    assert sent == [f"Approved: spec\nBy: owner\nSpec: {TEXTED}\n"]


def test_approve_over_rest_records_the_text_and_minimizes_nothing_it_cannot_tell_it_may(
    github: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    earlier = rest_shaped(
        issue(
            1,
            "kind:bug",
            comments=(note(record("spec", "owner", spec=AS_WRITTEN), viewerCanMinimize=False),),
        )
    )
    monkeypatch.setattr(approvals, "issue_node", lambda _n: earlier)
    assert approvals.main(["approve", "spec", "1", "--by", "owner"]) == 0
    assert github[0] == (
        "comment",
        1,
        f"Approved: spec\nBy: owner\nSpec: {TEXTED}\n{approvals.CHANGED['spec']}",
    )
    assert not any(call[0] == "minimize" for call in github)
