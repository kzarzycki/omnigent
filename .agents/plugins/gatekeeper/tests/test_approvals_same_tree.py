"""approvals.py: a verdict and a go on an earlier head hold for a head with the same tree (#1019)."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import (
    LOOP,
    approvals,
    approved,
    note,
    pull,
    record,
    status,
    verdict_on,
)

ISSUES = [approved(1, "area:web")]
AFTER = "every commit after a verdict needs a verdict of its own"


def git(where: Path, *args: str) -> str:
    who = ("-c", "commit.gpgsign=false", "-c", "user.name=Lead", "-c", "user.email=l@example.com")
    return subprocess.run(
        ["git", *who, *args], cwd=where, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def heads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A checkout with the reviewed head `reviewed`, `reworded` (the same change under another message, so the same
    tree) and `changed` (one more file changed on top of `reviewed`), and `tree`, the reviewed head's tree."""
    monkeypatch.chdir(tmp_path)
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "app.py").write_text("one\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    (tmp_path / "app.py").write_text("one, the feature\n")
    git(tmp_path, "commit", "-qam", "feature\n\nCo-authored-by: A <a@example.com>")
    reviewed = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "commit", "-q", "--amend", "-m", "feature\n\nCo-Authored-By: A <a@example.com>")
    reworded = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "switch", "-qc", "changed", reviewed)
    (tmp_path / "app.py").write_text("one, the feature, changed\n")
    git(tmp_path, "commit", "-qam", "feature, changed")
    return {
        "reviewed": reviewed,
        "reworded": reworded,
        "changed": git(tmp_path, "rev-parse", "HEAD"),
        "tree": git(tmp_path, "rev-parse", f"{reviewed}^{{tree}}"),
    }


def pr_at(
    head: str,
    reviewed: str,
    hub: dict[str, list[dict[str, Any]]],
    go: bool = True,
    threads: tuple[bool, ...] = (),
    **verdict: str,
) -> dict[str, Any]:
    """A high-risk PR now at `head`, with a verifier pass on `reviewed` and, with `go`, the owner's go there as
    `approve merge` leaves it (the record on the PR, the status on the commit)."""
    review = verdict_on(reviewed, **verdict)
    pr = pull(reviews=(review,), files=("app.py",), labels=("risk:high",), threads=threads)
    pr["headRefOid"] = reviewed
    if go:
        pr["comments"]["nodes"].append(note(record("merge", "owner", head=reviewed)))
        hub.setdefault(reviewed, []).insert(
            0, status("owner", f"The owner's go on {reviewed[:12]}")
        )
    pr["headRefOid"] = head
    return pr


def merge(pr: dict[str, Any]) -> list[str]:
    """check merge's lines: what fails, then what it waits for."""
    return approvals.proofs("merge", pr, ISSUES, LOOP) + approvals.waits(pr, ISSUES, LOOP)


def test_a_reworded_head_passes_check_merge_on_the_old_verdict_and_go(
    heads: dict[str, str], hub: dict[str, list[dict[str, Any]]], capsys: pytest.CaptureFixture[str]
) -> None:
    reviewed, reworded, tree = heads["reviewed"], heads["reworded"], heads["tree"]
    pr = pr_at(reworded, reviewed, hub)
    assert merge(pr) == []
    assert capsys.readouterr().out.splitlines() == [
        f"PR #7: verdict on {reviewed} carried: same tree {tree}"
    ]
    assert approvals.owner_waits(pr, [], LOOP) == []
    assert approvals.whose(pr, LOOP) == [
        f"PR #7: the go on {reviewed} is the owner's, carried to {reworded}: same tree {tree}"
    ]


def test_a_head_with_one_changed_file_keeps_todays_rules(
    heads: dict[str, str], hub: dict[str, list[dict[str, Any]]], capsys: pytest.CaptureFixture[str]
) -> None:
    reviewed, changed = heads["reviewed"], heads["changed"]
    pr = pr_at(changed, reviewed, hub)
    lines = merge(pr)
    assert lines[0] == (
        f"PR #7: the newest verifier review is on {reviewed}, not on the head {changed}: {AFTER}"
    )
    assert any(line.startswith("PR #7 waits for the owner's go on its head") for line in lines)
    assert approvals.whose(pr, LOOP) == []
    assert "carried" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("verdict", "threads", "why"),
    [
        (
            {"satisfied": "no"},
            (),
            "PR #7: a verifier review on {reviewed} is not satisfied: fix and verify again",
        ),
        (
            {"counts": "0 blocker, 1 major, 0 minor"},
            (),
            "PR #7: the verifier reviews on {reviewed} have 0 blocker and 1 major open: fix and verify again",
        ),
        (
            {},
            (False,),
            "PR #7: 1 review thread(s) unresolved: fix and reply, or triage, then resolve (github-tracker's github.md, Review trail)",
        ),
    ],
    ids=["an unsatisfied pass", "a major open", "an unresolved thread"],
)
def test_a_carried_verdict_counts_as_it_did_on_the_old_head(
    heads: dict[str, str],
    hub: dict[str, list[dict[str, Any]]],
    verdict: dict[str, str],
    threads: tuple[bool, ...],
    why: str,
) -> None:
    pr = pr_at(heads["reworded"], heads["reviewed"], hub, threads=threads, **verdict)
    lines = approvals.proofs("merge", pr, ISSUES, LOOP)
    assert why.format(**heads) in lines
    assert not any(AFTER in line for line in lines)


def test_a_carried_verdicts_risk_line_still_holds(
    heads: dict[str, str], hub: dict[str, list[dict[str, Any]]]
) -> None:
    pr = pr_at(heads["reworded"], heads["reviewed"], hub)
    pr["reviews"]["nodes"][0]["body"] += "Risk: high, it changes when a go holds\n"
    assert approvals.judged(pr, LOOP) == (
        f"the verifier's pass 1 on {heads['reviewed'][:7]} says `Risk: high, it changes when a go holds`"
    )


def test_a_reworded_head_without_a_go_on_the_old_one_waits_for_the_owner(
    heads: dict[str, str], hub: dict[str, list[dict[str, Any]]]
) -> None:
    pr = pr_at(heads["reworded"], heads["reviewed"], hub, go=False)
    assert [line[:42] for line in approvals.owner_waits(pr, [], LOOP)] == [
        "PR #7 waits for the owner's go on its head"
    ]


def test_commits_neither_the_clone_nor_origin_has_share_no_tree(
    heads: dict[str, str], hub: dict[str, list[dict[str, Any]]]
) -> None:
    """Two unreadable trees are no same tree: the head is held to today's rules."""
    pr = pr_at("c" * 40, "d" * 40, hub)
    assert approvals.same_tree("c" * 40, "d" * 40) is None
    assert (
        merge(pr)[0]
        == f"PR #7: the newest verifier review is on {'d' * 40}, not on the head {'c' * 40}: {AFTER}"
    )


# --- tree_of fetches only the commit it lacks, within a bound, once per run ---


@pytest.fixture
def behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A clone of `origin` made before origin got `feature` (the commit `missing`) and the unrelated branch `other`
    (`unrelated`); the test runs in the clone."""
    origin, author, clone = tmp_path / "origin.git", tmp_path / "author", tmp_path / "clone"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "init", "-q", "-b", "main", str(author))
    (author / "app.py").write_text("one\n")
    git(author, "add", "-A")
    git(author, "commit", "-qm", "base")
    git(author, "push", "-q", str(origin), "main")
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    # a clone set to fetch every tag (a fetch by commit stores no ref, so it follows none on its own)
    git(clone, "config", "remote.origin.tagOpt", "--tags")
    (author / "app.py").write_text("one, the feature\n")
    git(author, "commit", "-qam", "feature")
    missing = git(author, "rev-parse", "HEAD")
    git(author, "switch", "-q", "--orphan", "other")
    (author / "other.txt").write_text("unrelated\n")
    git(author, "add", "-A")
    git(author, "commit", "-qm", "unrelated")
    unrelated = git(author, "rev-parse", "HEAD")
    # a tag on the commit asked for and one on another, which the clone's tagOpt fetches without --no-tags
    git(author, "tag", "-a", "-m", "on the feature", "v-feature", missing)
    git(author, "tag", "v-other", unrelated)
    git(author, "push", "-q", str(origin), f"{missing}:refs/heads/feature", "other", "--tags")
    monkeypatch.chdir(clone)
    monkeypatch.setattr(approvals, "TREE_FETCHED", set())
    return {
        "missing": missing,
        "unrelated": unrelated,
        "tree": git(author, "rev-parse", f"{missing}^{{tree}}"),
        "clone": str(clone),
    }


def has(where: str, sha: str) -> bool:
    return (
        subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=where, check=False
        ).returncode
        == 0
    )


def test_a_missing_tree_fetches_only_that_commit(behind: dict[str, str]) -> None:
    assert approvals.tree_of(behind["missing"]) == behind["tree"]
    assert not has(behind["clone"], behind["unrelated"])
    assert git(Path(behind["clone"]), "tag", "-l") == ""
    assert git(Path(behind["clone"]), "branch", "-r").split() == [
        "origin/HEAD",
        "->",
        "origin/main",
        "origin/main",
    ]


def test_a_commit_origin_lacks_is_asked_for_once_per_run(
    behind: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fetches: list[list[str]] = []
    real = subprocess.Popen

    def spy(args: list[str], **kwargs: Any) -> Any:
        if "fetch" in args:
            fetches.append(args)
        return real(args, **kwargs)

    monkeypatch.setattr(approvals.subprocess, "Popen", spy)
    assert approvals.tree_of("f" * 40) is None
    assert approvals.tree_of("f" * 40) is None
    assert len(fetches) == 1 and fetches[0][-1] == "f" * 40


def test_a_fetch_that_hangs_gives_none_within_the_bound(
    behind: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transport (here an ssh that never answers) is killed with git at the bound, so nothing is left running."""
    git(Path(behind["clone"]), "remote", "set-url", "origin", "ssh://nowhere.invalid/repo.git")
    pid = Path(behind["clone"]).parent / "ssh.pid"
    monkeypatch.setenv("GIT_SSH_COMMAND", f"echo $$ > {pid}; exec sleep 60 #")
    monkeypatch.setattr(approvals, "TREE_FETCH_SECONDS", 1)
    started = time.monotonic()
    assert approvals.tree_of(behind["missing"]) is None
    assert time.monotonic() - started < 15
    assert not alive(int(pid.read_text()))


def alive(pid: int) -> bool:
    """Whether `pid` still runs, within a second for its kill to land; a zombie nobody reaped yet has stopped."""
    stat = Path(f"/proc/{pid}/stat")
    for _ in range(20):
        try:
            if stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return False
        except FileNotFoundError, ProcessLookupError:
            return False
        time.sleep(0.05)
    return True
