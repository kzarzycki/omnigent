"""approvals.py: spec.auto's `paths` term, a spec labelled spec:paths approved by policy while its PR stays inside them (#805)."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any

import pytest
from approvals_harness import (
    AUTO,
    BARE,
    HEAD,
    POLICY,
    TRACKER,
    VERDICT,
    approvals,
    by,
    git_in,
    lands,  # noqa: F401
    loop_md,
    merge,
    pull,
    specced,
    verdict_on,
    writers,  # noqa: F401
)

SCOPED = loop_md(
    "spec:\n  auto:\n    - bug\n    - paths:\n        - mise.toml\n        - docs/agents/**\n"
    "risk:\n  verifier: true\n  paths:\n    - billing/**\n"
)
LOW = {**VERDICT, "body": VERDICT["body"] + "Risk: low\n"}
OWNER_SPEC = [
    "#1 has no `Approved: spec` record by the owner",
    "#1 lacks the `approved:spec` label",
]


def tooling(*labels: str, author: str = "worker") -> dict[str, Any]:
    """A writer's chore spec the lead declared path-scoped, with no approval of the owner's."""
    return by(author, specced(1, "kind:chore", "spec:paths", *labels))


def owners(why: str) -> list[str]:
    """check's lines for #1 when the paths term does not approve its spec."""
    return [
        f"#1: its spec is the owner's to approve, though it carries `spec:paths`, since {why}",
        *OWNER_SPEC,
    ]


def moved(*files: tuple[str, str | None]) -> dict[str, Any]:
    """A PR with the head's verdict changing `files`, each a (path, old name) pair: a pair with an old name is a
    rename, as GraphQL gives it (the new path only); approvals.py reads the old name from the REST list."""
    pr = pull(reviews=(LOW,))
    pr["files"] = {
        "totalCount": len(files),
        "nodes": [
            {"path": path, "changeType": "RENAMED" if old else "MODIFIED"} for path, old in files
        ],
    }
    return pr


@pytest.fixture
def renames(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The PR's REST file list (pr_changes), each renamed file's `previous_filename` by its new name; `fail` makes
    GitHub refuse the list."""
    found: dict[str, Any] = {"old": {}, "fail": False}

    def changes(number: int) -> list[dict[str, Any]]:
        if found["fail"]:
            raise subprocess.CalledProcessError(1, ["gh", "api"])
        return [{"filename": new, "previous_filename": old} for new, old in found["old"].items()]

    monkeypatch.setattr(approvals, "pr_changes", changes)
    return found


def test_check_build_passes_a_spec_paths_issue_with_no_owner_record(
    writers: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Before the PR, the setting and the label approve the spec: the branch's files so far stay inside the paths."""
    spec = {1: tooling(), 2: by("worker", specced(2, "kind:chore"))}
    changed = ["docs/agents/loop.md\n"]

    def git(*args: str) -> str:
        if args[0] == "diff":
            return "".join(f"{path}\0" for path in changed[0].split())
        return "origin/main\n" if args[-1] == "origin/HEAD" else "loop/1-fix\n"

    monkeypatch.setattr(approvals, "project", lambda: (TRACKER, SCOPED))
    monkeypatch.setattr(approvals, "one_repo", lambda: None)
    monkeypatch.setattr(approvals, "issue_node", lambda n: spec[n])
    monkeypatch.setattr(approvals, "current_pr", lambda: None)
    monkeypatch.setattr(approvals, "git", git)

    def build(number: int) -> tuple[int, list[str]]:
        code = approvals.main(["check", "build", "--issue", str(number)])
        return code, capsys.readouterr().out.splitlines()

    assert build(1) == (0, ["#1: every build proof holds; no PR yet"])
    # no label, no policy: the owner approves it
    assert build(2) == (
        1,
        ["#2 has no `Approved: spec` record by the owner", "#2 lacks the `approved:spec` label"],
    )
    changed[0] = "docs/agents/loop.md app.py\n"
    assert build(1) == (1, owners("the PR changes `app.py`, outside `spec.auto`'s `paths`"))


def test_check_merge_passes_a_spec_paths_pr_inside_the_paths_and_names_each_file_outside(
    writers: list[str],
) -> None:
    inside = pull(reviews=(LOW,), files=("mise.toml", "docs/agents/loop.md"))
    assert merge(inside, [tooling()], SCOPED) == []
    outside = pull(reviews=(LOW,), files=("mise.toml", "src/app.py", "README.md"))
    assert approvals.proofs("merge", outside, [tooling()], SCOPED) == owners(
        "the PR changes `src/app.py`, `README.md`, outside `spec.auto`'s `paths`"
    )


@pytest.mark.parametrize(
    ("new", "old"),
    [("src/loop.md", "docs/agents/loop.md"), ("docs/agents/app.py", "src/app.py")],
    ids=["out-of-the-paths", "into-the-paths"],
)
def test_a_rename_across_the_paths_either_way_voids_the_policy(
    writers: list[str], renames: dict[str, Any], new: str, old: str
) -> None:
    renames["old"] = {new: old}
    outside = new if old.startswith("docs/agents/") else old
    assert approvals.proofs("merge", moved((new, old)), [tooling()], SCOPED) == owners(
        f"the PR changes `{outside}`, outside `spec.auto`'s `paths`"
    )
    # a rename that stays inside them is approved
    renames["old"] = {"docs/agents/new.md": "docs/agents/loop.md"}
    within = moved(("docs/agents/new.md", "docs/agents/loop.md"))
    assert approvals.proofs("merge", within, [tooling()], SCOPED) == []


def test_a_file_list_github_cannot_give_in_full_voids_the_policy(
    writers: list[str], renames: dict[str, Any]
) -> None:
    unreadable = "GitHub can't give every file the PR changes, an old name of a moved file included"
    crowded = pull(reviews=(LOW,), files=("mise.toml",))
    crowded["files"]["totalCount"] = 101
    assert approvals.proofs("merge", crowded, [tooling()], SCOPED) == owners(
        "the gate can't read every file the PR changes, so the risk rule's `paths` hold until a person judges them"
    )
    unruled = loop_md("spec:\n  auto:\n    - paths:\n        - mise.toml\n")
    assert approvals.proofs("merge", crowded, [tooling()], unruled) == owners(unreadable)
    # a rename whose old name GitHub won't give, or gives for another file only
    renames["fail"] = True
    rename = moved(("docs/agents/new.md", "docs/agents/loop.md"))
    assert approvals.proofs("merge", rename, [tooling()], unruled) == owners(unreadable)
    renames["fail"], renames["old"] = False, {"docs/agents/other.md": "docs/agents/loop.md"}
    rename = moved(("docs/agents/new.md", "docs/agents/loop.md"))
    assert approvals.proofs("merge", rename, [tooling()], unruled) == owners(unreadable)


def test_a_spec_paths_pr_the_risk_rule_holds_still_needs_the_owners_approval(
    writers: list[str],
) -> None:
    inside = ("docs/agents/loop.md",)
    marked = pull(reviews=(LOW,), files=inside, labels=("risk:high",))
    assert approvals.proofs("merge", marked, [tooling()], SCOPED)[:3] == owners(
        "`risk:high` is on PR #7"
    )
    assert approvals.proofs("build", pull(files=inside), [tooling("risk:high")], SCOPED) == owners(
        "`risk:high` is on #1"
    )
    ruled = loop_md(
        "spec:\n  auto:\n    - paths:\n        - docs/agents/**\nrisk:\n  paths:\n    - docs/agents/gatekeeper.md\n"
    )
    held = pull(reviews=(LOW,), files=("docs/agents/gatekeeper.md",))
    assert approvals.proofs("merge", held, [tooling()], ruled) == owners(
        "it matches a risk rule (path `docs/agents/gatekeeper.md`)"
    )
    # the verifier's `Risk: high` holds it too, once a review is read
    high = verdict_on(HEAD, "0 blocker, 0 major, 2 minor")
    high["body"] += "Risk: high\n"
    judged = pull(reviews=(high,), files=inside)
    assert approvals.proofs("merge", judged, [tooling()], SCOPED)[:3] == owners(
        "it matches a risk rule (verifier): the verifier's pass 1 on aaaaaaa says `Risk: high`"
    )
    # so does an author who can't write to the repo
    assert approvals.proofs(
        "build", pull(files=inside), [tooling(author="drive-by")], SCOPED
    ) == owners("its author @drive-by can't write to the repo")


def test_without_the_paths_term_the_label_approves_nothing_and_the_kinds_work_as_before(
    writers: list[str],
) -> None:
    inside = pull(files=("docs/agents/loop.md",))
    for policy in (POLICY, AUTO.replace("area:api", "area:none"), BARE):
        assert approvals.spec_paths(policy) == []
    assert approvals.proofs("build", inside, [tooling()], POLICY) == owners(
        "`spec.auto` names no `paths`, so `spec:paths` approves nothing"
    )
    # `spec.auto: true` approves it as it did, label or not, and the kind list its kinds
    assert approvals.proofs("build", inside, [tooling()], AUTO) == []
    assert approvals.proofs("build", inside, [by("worker", specced(1, "kind:bug"))], SCOPED) == []
    assert approvals.spec_kinds(SCOPED) == {"bug"}
    assert approvals.spec_paths(SCOPED) == ["mise.toml", "docs/agents/**"]
    assert approvals.settings(SCOPED)["spec"]["auto"] == [
        "bug",
        {"paths": ["mise.toml", "docs/agents/**"]},
    ]


def changes(pr: dict[str, Any], *files: tuple[str, str, str | None]) -> dict[str, Any]:
    """`pr` changing `files`, each (path, GraphQL changeType, old name): a RENAMED or COPIED file's old name is in
    the REST list (renames), a DELETED file's path its old one, as GitHub gives them."""
    pr["files"] = {
        "totalCount": len(files),
        "nodes": [{"path": path, "changeType": kind} for path, kind, _ in files],
    }
    return pr


INSIDE = ("docs/agents/loop.md", "MODIFIED", None)
CROSSINGS = [
    (("src/loop.md", "RENAMED", "docs/agents/loop.md"), "src/loop.md"),
    (("docs/agents/app.py", "RENAMED", "src/app.py"), "src/app.py"),
    (("src/copy.md", "COPIED", "docs/agents/loop.md"), "src/copy.md"),
    (("docs/agents/app.py", "COPIED", "src/app.py"), "src/app.py"),
    (("src/old.py", "DELETED", None), "src/old.py"),
]
CROSSING_IDS = ["rename-out", "rename-in", "copy-out", "copy-in", "deleted-outside"]


@pytest.fixture
def merges(
    writers: list[str],
    renames: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Callable[..., tuple[int, list[str]]]:
    """`check merge 7` and `check build 7` through main() under SCOPED, on a PR closing a writer's `spec:paths`
    issue with a `Risk: low` verdict on its head; `run(point, *files)` gives (exit, lines)."""
    monkeypatch.setattr(approvals, "project", lambda: (TRACKER, SCOPED))
    monkeypatch.setattr(approvals, "one_repo", lambda: None)

    def run(point: str, *files: tuple[str, str, str | None]) -> tuple[int, list[str]]:
        renames["old"] = {path: old for path, kind, old in files if kind in ("RENAMED", "COPIED")}
        pr = changes(pull(reviews=(LOW,)), *files)
        pr["closingIssuesReferences"]["nodes"] = [tooling()]
        monkeypatch.setattr(approvals, "pull_request", lambda _n: pr)
        code = approvals.main(["check", point, "7"])
        return code, capsys.readouterr().out.splitlines()

    return run


def test_check_merge_lands_a_spec_paths_pr_inside_the_paths(
    merges: Callable[..., tuple[int, list[str]]],
) -> None:
    code, lines = merges("merge", INSIDE, ("docs/agents/new.md", "RENAMED", "docs/agents/old.md"))
    assert code == 0 and not any("owner's to approve" in line for line in lines), lines


@pytest.mark.parametrize(("crossing", "outside"), CROSSINGS, ids=CROSSING_IDS)
def test_check_merge_refuses_a_spec_paths_pr_crossing_the_paths(
    merges: Callable[..., tuple[int, list[str]]],
    crossing: tuple[str, str, str | None],
    outside: str,
) -> None:
    code, lines = merges("merge", INSIDE, crossing)
    assert code == 1
    assert lines[:3] == owners(f"the PR changes `{outside}`, outside `spec.auto`'s `paths`")


def test_check_merge_refuses_a_spec_paths_pr_whose_diff_left_the_paths_after_check_build(
    merges: Callable[..., tuple[int, list[str]]],
) -> None:
    code, lines = merges("build", INSIDE)
    assert code == 0 and not any("owner's to approve" in line for line in lines), lines
    code, lines = merges("merge", INSIDE, ("src/app.py", "ADDED", None))
    assert code == 1
    assert lines[:3] == owners("the PR changes `src/app.py`, outside `spec.auto`'s `paths`")


@pytest.mark.parametrize(("crossing", "outside"), CROSSINGS, ids=CROSSING_IDS)
def test_land_lands_a_spec_paths_pr_inside_the_paths_and_refuses_one_crossing_them(
    lands: dict[str, Any],
    writers: list[str],
    renames: dict[str, Any],
    crossing: tuple[str, str, str | None],
    outside: str,
) -> None:
    (lands["work"] / "docs" / "agents" / "loop.md").write_text(SCOPED)
    git_in(lands["work"], "add", "-A")
    git_in(lands["work"], "commit", "-qm", "the paths policy")
    git_in(lands["work"], "push", "-q", "origin", "HEAD:refs/heads/main")
    git_in(lands["work"], "fetch", "-q", "origin")
    for number in (7, 8):
        lands["open"](number, {f"docs/agents/{number}.md": f"{number}\n"}, reviews=())
        pr = lands["pulls"][number]
        pr["reviews"]["nodes"] = [
            {
                **verdict_on(pr["headRefOid"]),
                "body": verdict_on(pr["headRefOid"])["body"] + "Risk: low\n",
            }
        ]
        pr["closingIssuesReferences"]["nodes"] = [
            by("worker", specced(100 + number, "kind:chore", "spec:paths"))
        ]
    changes(lands["pulls"][7], INSIDE)
    changes(lands["pulls"][8], INSIDE, crossing)
    renames["old"] = {crossing[0]: crossing[2]} if crossing[2] else {}
    code, lines = lands["land"]("8")
    assert code == 1
    assert (
        "#108: its spec is the owner's to approve, though it carries `spec:paths`, since the PR changes"
        f" `{outside}`, outside `spec.auto`'s `paths`"
    ) in "\n".join(lines), lines
    # land names #8 and refuses it, and lands every landable PR into that base: #7, inside the paths
    assert lands["merged"]() == ["7"]
    assert lands["pulls"][8]["state"] == "OPEN"


@pytest.mark.parametrize(
    "term",
    [
        "    - path:\n        - a/**\n",
        "    - paths: a/**\n",
        "    - paths: []\n",
        "    - paths:\n        - a/**\n      labels:\n        - b\n",
    ],
    ids=["misspelt", "a-string", "empty", "a-second-key"],
)
def test_a_paths_term_off_its_shape_is_refused_naming_the_key(term: str) -> None:
    with pytest.raises(approvals.Refused) as raised:
        approvals.settings(loop_md(f"spec:\n  auto:\n{term}"))
    assert str(raised.value).startswith(
        "loop.md's `spec.auto` term must be `paths`, a list of globs"
    )
