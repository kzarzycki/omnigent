"""gatekeeper.py: a pack bump a cloud worker pushes gets the `bot.<name>` fast path. Under a `branch` section a commit
author with no GitHub account counts as the PR's author, and a bump whose `apm.yml` changes only the engineering
dependency's `ref:` line needs no spec approval (#1800)."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from approvals_harness import approvals, loop_md, pull
from test_approvals import BOTS, LOOP, UNREVIEWED, add, authored, issue

PACK_BUMP = add(
    LOOP,
    "bot",
    "pack-bump:\n  branch: claude/ready-pack-bump-\n  paths:\n    - apm.yml\n    - apm.lock.yaml\n"
    '    - "**/apm.yml"\n    - ".agents/**"\n',
)
RENOVATE = add(LOOP, "bot", "renovate:\n  branch: renovate/\n  paths:\n    - uv.lock\n")
# a consumer's `apm.yml` bump: only its engineering dependency's `ref:` line moves
REF_BUMP = (
    "@@ -5,7 +5,7 @@ targets:\n"
    " dependencies:\n"
    "   apm:\n"
    "   - git: kzarzycki/agent-skills/engineering\n"
    "-    ref: ^0.35.1\n"
    "+    ref: ^0.36.0\n"
    "   - git: someone/other-pack\n"
    "     ref: ^1.0.0\n"
    "   mcp: []"
)
FILES = ("apm.yml", "apm.lock.yaml", ".agents/skills/engineering-loop/SKILL.md")
GENERATED = ("apm.lock.yaml", ".agents/skills/engineering-loop/SKILL.md")


def cloud_bump(
    *files: str,
    patch: str = REF_BUMP,
    commits: tuple[str | None, ...] = (None,),
    branch: str = "claude/ready-pack-bump-engineering-v0.36.0",
) -> dict[str, Any]:
    """A pack bump a cloud worker pushed: the writer's PR on `branch`, its commits by a git identity GitHub ties to no
    account (None), with `patch` as the diff of each `apm.yml`."""
    files = files or GENERATED
    pr = {
        **pull(files=files),
        "author": {"__typename": "User", "login": "writer"},
        "headRefName": branch,
    }
    pr["authored"] = {"nodes": [authored(by) for by in commits]}
    pr["changes"] = [{"filename": path, "status": "modified", "patch": patch} for path in files]
    return pr


UNAPPROVED = issue(3, "kind:chore", "area:agents", "size:XS")
SPEC = [
    "#3 has no `Approved: spec` record by the owner",
    "#3 lacks the `approved:spec` label",
]


def gate(pr: dict[str, Any], loop: str = PACK_BUMP) -> list[str]:
    """What the merge lacks for `pr`, which closes an issue whose spec nobody approved."""
    return approvals.proofs("merge", pr, [UNAPPROVED], loop)


def test_a_cloud_workers_commit_on_a_pack_bump_branch_gets_the_exemption_only_for_its_paths() -> (
    None
):
    """A cloud worker commits as a git identity with no GitHub account: under a `branch` section that author counts as
    the PR's, as a committer with no account does; every other guard stays."""
    assert approvals.exempt(cloud_bump(), PACK_BUMP)
    assert approvals.proofs("merge", cloud_bump(), [], PACK_BUMP) == []
    assert (
        approvals.proofs("merge", cloud_bump("apm.lock.yaml", "app.py"), [], PACK_BUMP)
        == UNREVIEWED
    )
    assert not approvals.exempt(
        cloud_bump(commits=(None, "other")), PACK_BUMP
    )  # another person's push
    assert approvals.exempt(cloud_bump(commits=(None, "writer")), PACK_BUMP)
    forked = cloud_bump()
    forked["isCrossRepository"] = True  # a fork may name any branch
    assert not approvals.exempt(forked, PACK_BUMP)
    assert not approvals.exempt(cloud_bump(branch="claude/ready-feature"), PACK_BUMP)
    # a login section still needs the bot's own account on every commit
    bot = {**cloud_bump("uv.lock"), "author": {"__typename": "Bot", "login": "dependabot"}}
    assert not approvals.exempt(bot, BOTS)


def test_a_mechanical_pack_bump_skips_the_spec_gate() -> None:
    """The owner's rule on #1800: a bot-exempt pack bump whose diff is `apm.yml`'s engineering `ref:` line plus the
    lock and generated files needs no spec approval."""
    assert gate(cloud_bump(*FILES)) == []
    assert gate(cloud_bump(*FILES), loop=LOOP) == SPEC + UNREVIEWED  # no section: no bump
    no_newline = REF_BUMP + "\n\\ No newline at end of file"
    assert gate(cloud_bump(*FILES, patch=no_newline)) == []
    for release in (
        "0.36.0",
        "'^0.36.0'",
        '"~0.36.0"',
        "engineering-v0.36.0",
        "^0.36.0  # pinned by the bump",
    ):
        assert gate(cloud_bump(*FILES, patch=REF_BUMP.replace("^0.36.0", release))) == [], release


@pytest.mark.parametrize(
    ("why", "patch"),
    [
        (
            "a dependency added beside the ref",
            REF_BUMP.replace("   mcp: []", "+  - git: someone/new-pack\n   mcp: []"),
        ),
        (
            "another dependency's ref",
            REF_BUMP.replace(
                "-    ref: ^0.35.1\n+    ref: ^0.36.0\n   - git: someone/other-pack\n     ref: ^1.0.0",
                "     ref: ^0.35.1\n   - git: someone/other-pack\n-    ref: ^1.0.0\n+    ref: ^2.0.0",
            ),
        ),
        (
            "another dependency's ref dropped and the pack's added",
            (
                "@@ -5,8 +5,8 @@ targets:\n"
                " dependencies:\n"
                "   apm:\n"
                "   - git: someone/other-pack\n"
                "-    ref: ^1.0.0\n"
                "   - git: kzarzycki/agent-skills/engineering\n"
                "     ref: ^0.35.1\n"
                "+    ref: ^0.36.0\n"
                "   mcp: []"
            ),
        ),
        (
            "the ref line swapped for another key",
            REF_BUMP.replace("+    ref: ^0.36.0", "+    branch: main"),
        ),
        (
            "the added line before the removed one",
            REF_BUMP.replace(
                "-    ref: ^0.35.1\n+    ref: ^0.36.0", "+    ref: ^0.36.0\n-    ref: ^0.35.1"
            ),
        ),
        (
            "the two edits in two hunks",
            REF_BUMP.replace("+    ref: ^0.36.0\n", "@@ -9,1 +9,1 @@\n+    ref: ^0.36.0\n"),
        ),
        (
            "the ref past the pack item's end",
            REF_BUMP.replace(
                "   - git: kzarzycki/agent-skills/engineering\n-    ref: ^0.35.1\n+    ref: ^0.36.0\n",
                "   - git: kzarzycki/agent-skills/engineering\n   mcp: []\n-    ref: ^0.35.1\n+    ref: ^0.36.0\n",
            ),
        ),
        ("a second hunk with its own edit", REF_BUMP + "\n@@ -20,1 +20,2 @@\n x: 1\n+y: 2"),
        ("the pack moved to a branch", REF_BUMP.replace("+    ref: ^0.36.0", "+    ref: main")),
        (
            "the pack moved to a worker's branch",
            REF_BUMP.replace("+    ref: ^0.36.0", "+    ref: claude/ready-some-feature"),
        ),
        (
            "the pack moved to a commit",
            REF_BUMP.replace("+    ref: ^0.36.0", "+    ref: " + "0123abcd" * 5),
        ),
        (
            "a release with a suffix",
            REF_BUMP.replace("+    ref: ^0.36.0", "+    ref: ^0.36.0-evil"),
        ),
        ("a line that is no part of a diff", REF_BUMP + "\nextra: 1"),
        (
            "no list item in the hunk's context",
            "@@ -6,2 +6,2 @@\n-    ref: ^0.35.1\n+    ref: ^0.36.0\n     path: x",
        ),
        ("no patch", ""),
    ],
)
def test_any_other_apm_yml_change_keeps_the_spec_gate(why: str, patch: str) -> None:
    assert patch != REF_BUMP, why
    assert gate(cloud_bump(*FILES, patch=patch)) == SPEC, why


def test_a_bump_that_moves_no_pack_keeps_the_spec_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The skip is for a PR that moves the pack: an exempt PR that leaves `apm.yml` alone (an updater's lock-only PR,
    a hand edit of a generated file) still owes the spec, and so does one whose `apm.yml` patch GitHub can't give."""
    assert gate(cloud_bump(*GENERATED)) == SPEC
    assert gate(cloud_bump(".agents/skills/engineering-loop/scripts/gatekeeper.py")) == SPEC
    renovate = {
        **cloud_bump("uv.lock", branch="renovate/uv-lock"),
        "headRefName": "renovate/uv-lock",
    }
    assert approvals.exempt(renovate, RENOVATE)
    assert gate(renovate, loop=RENOVATE) == SPEC
    # a nested `apm.yml` beside the root one is checked too
    nested = cloud_bump(*FILES, "sub/apm.yml")
    nested["changes"][-1]["patch"] = REF_BUMP + "\n+extra: 1"
    assert gate(nested) == SPEC
    # GitHub refuses the REST file list that holds the patches
    unread = cloud_bump(*FILES)
    del unread["changes"]

    def refused(_number: int) -> list[dict[str, Any]]:
        raise subprocess.CalledProcessError(1, ["gh", "api"])

    monkeypatch.setattr(approvals, "pr_changes", refused)
    assert gate(unread) == SPEC
    # a login section's exempt PR keeps it too, the same pack bump included: only a `branch` section names one
    login = add(
        LOOP,
        "bot",
        'dependabot[bot]:\n  paths:\n    - apm.yml\n    - apm.lock.yaml\n    - ".agents/**"\n',
    )
    bot = {
        **cloud_bump(*FILES, commits=("dependabot[bot]",)),
        "author": {"__typename": "Bot", "login": "dependabot"},
    }
    assert approvals.exempt(bot, login)
    assert gate(bot, loop=login) == SPEC


def test_a_mechanical_pack_bump_on_a_risk_path_is_no_spec_hold_under_spec_auto_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where `apm.yml` is on the risk paths, `spec.auto: true` leaves each spec to the owner: a mechanical bump's is
    not, and a bump with any other `apm.yml` change still is."""
    monkeypatch.setattr(approvals, "writer", lambda login: True)
    loop = add(
        loop_md("spec:\n  auto: true\nrisk:\n  paths:\n    - apm.yml\n"),
        "bot",
        "pack-bump:\n  branch: claude/ready-pack-bump-\n  paths:\n    - apm.yml\n    - apm.lock.yaml\n",
    )
    ask = issue(5, "kind:chore", "size:XS")
    held = [
        "#5: its spec is the owner's to approve under `spec.auto: true`, since it matches a risk rule (path `apm.yml`)"
    ]
    spec = ["#5 has no `Approved: spec` record by the owner", "#5 lacks the `approved:spec` label"]
    bump = cloud_bump("apm.yml", "apm.lock.yaml")
    assert approvals.holds(bump, [ask], loop) == []
    assert approvals.proofs("merge", bump, [ask], loop) == []
    more = cloud_bump("apm.yml", "apm.lock.yaml", patch=REF_BUMP + "\n+extra: 1")
    assert approvals.holds(more, [ask], loop) == held
    assert approvals.proofs("merge", more, [ask], loop) == spec


NO_ISSUE = "the PR names no issue: it needs a `Closes #<issue>` line for an approved spec"


def test_a_mechanical_bump_needs_no_issue_and_any_other_pr_still_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bump the gate proves mechanical (#2040), a pack bump or a verified pin bump, names no issue and is not refused
    for it; a person's PR, and a `branch` section's PR that is no pack bump (a hook under `.agents/`), still are."""
    monkeypatch.setattr(approvals, "pin_target", lambda pr: None)
    bump = {**cloud_bump(*FILES), "body": ""}
    found, issues = approvals.gated("merge", bump, None, PACK_BUMP)
    assert issues == [] and NO_ISSUE not in found
    hook = {**cloud_bump(".agents/hooks/run.sh"), "body": ""}  # exempt paths, but no pack moved
    assert approvals.exempt(hook, PACK_BUMP)
    assert NO_ISSUE in approvals.gated("merge", hook, None, PACK_BUMP)[0]
    person = {**cloud_bump(*FILES, branch="claude/ready-feature"), "body": ""}
    assert NO_ISSUE in approvals.gated("merge", person, None, PACK_BUMP)[0]
    monkeypatch.setattr(
        approvals, "pin_target", lambda pr: "engineering-v0.36.0"
    )  # a verified pin bump
    assert NO_ISSUE not in approvals.gated("merge", person, None, PACK_BUMP)[0]


def test_a_bump_that_names_an_issue_still_has_it_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    """The waiver is for naming no issue: an issue a bump does name is read as any PR's is (its `needs-owner`)."""
    labels = [*UNAPPROVED["labels"]["nodes"], {"name": approvals.NEEDS_OWNER}]
    held = {**UNAPPROVED, "labels": {"nodes": labels}}
    monkeypatch.setattr(approvals, "adopted", lambda issues, loop: [held])
    found, _ = approvals.gated("merge", cloud_bump(*FILES), None, PACK_BUMP)
    assert f"#3 waits for the owner ({approvals.NEEDS_OWNER})" in found, found
