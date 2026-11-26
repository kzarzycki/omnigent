"""approvals.py: the policy is read from the base the PR merges onto, origin/<base>, not the working tree (#1047)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import (
    approvals,
    gatekeeper_md,
    git_in,
)

OLD = gatekeeper_md("loop:\n  gate: mise run check\n")
NEW = gatekeeper_md(
    "loop:\n  gate_steps:\n"
    "    - name: lint\n      command: mise run lint\n      where: any\n"
    "    - name: unit\n      command: mise run unit\n      where: any\n"
)
UNBASED = "approvals.py: no origin/<base> ref, so the policy is read from the working tree\n"


@pytest.fixture(autouse=True)
def fresh() -> None:
    """No base found and no line printed yet this run, so a test's stderr doesn't depend on which ran before it."""
    for cached in (approvals.default_base, approvals.unbased, approvals.differs):
        # a mutation check may take a cache off: the tests below then fail on what it changes, not here
        if clear := getattr(cached, "cache_clear", None):
            clear()


def checkout(repo: Path) -> None:
    repo.mkdir()
    git_in(repo, "init", "-q", "-b", "main")
    git_in(repo, "config", "user.email", "loop@example.com")
    git_in(repo, "config", "user.name", "loop")
    git_in(repo, "config", "commit.gpgsign", "false")


def policy(repo: Path, text: str, message: str) -> str:
    agents = repo / "docs" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "gatekeeper.md").write_text(text)
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-qm", message)
    return git_in(repo, "rev-parse", "HEAD").strip()


def test_a_checkout_behind_its_base_gates_under_the_bases_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The working tree is a commit behind origin/main, whose newer commit adds `gate_steps`: the stepped plan, and
    one line naming the file and both commits."""
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    git_in(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    checkout(repo)
    git_in(repo, "remote", "add", "origin", str(origin))
    old = policy(repo, OLD, "one gate")
    new = policy(repo, NEW, "gate steps")
    git_in(repo, "push", "-q", "origin", "main")
    git_in(repo, "fetch", "-q", "origin")
    git_in(repo, "reset", "-q", "--hard", old)  # the host checkout sat behind origin/main
    monkeypatch.chdir(repo)
    assert [step["name"] for step in approvals.gate_plan()] == ["lint", "unit"]
    assert capsys.readouterr().err == (
        f"approvals.py: docs/agents/gatekeeper.md in the working tree (at {old}) differs from origin/main's"
        f" (at {new}): using origin/main's\n"
    )
    approvals.gate_plan()
    assert capsys.readouterr().err == ""  # once per run


def test_a_checkout_without_origin_reads_the_working_tree_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    checkout(repo)
    policy(repo, OLD, "one gate")
    (repo / "docs" / "agents" / "gatekeeper.md").write_text(NEW)  # uncommitted, and still read
    monkeypatch.chdir(repo)
    assert [step["name"] for step in approvals.gate_plan()] == ["lint", "unit"]
    assert capsys.readouterr().err == UNBASED
    approvals.gate_plan()
    assert capsys.readouterr().err == ""  # once per run


def test_the_base_is_the_branch_origin_head_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A clone's origin/HEAD names the default branch, read before asking origin, whose HEAD may name another."""
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    git_in(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    checkout(repo)
    git_in(repo, "remote", "add", "origin", str(origin))
    old = policy(repo, OLD, "one gate")
    git_in(repo, "push", "-q", "origin", "main")
    new = policy(repo, NEW, "gate steps")
    git_in(repo, "push", "-q", "origin", "HEAD:refs/heads/trunk")
    git_in(repo, "fetch", "-q", "origin")
    git_in(repo, "remote", "set-head", "origin", "trunk")
    git_in(repo, "reset", "-q", "--hard", old)
    monkeypatch.chdir(repo)
    assert [step["name"] for step in approvals.gate_plan()] == ["lint", "unit"]
    assert f"differs from origin/trunk's (at {new})" in capsys.readouterr().err


def gated(text: str) -> str:
    return gatekeeper_md(f"loop:\n  gate: {text}\n")


@pytest.fixture
def based(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A clone whose origin's main holds the gate `echo default` and whose branch `feature` holds `echo feature`, the
    working tree on main; `push(text)` commits a gate on main and pushes it, as another land would."""
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    git_in(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    checkout(repo)
    git_in(repo, "remote", "add", "origin", str(origin))
    policy(repo, gated("echo default"), "main's gate")
    git_in(repo, "push", "-q", "origin", "main")
    git_in(repo, "switch", "-qc", "feature")
    policy(repo, gated("echo feature"), "feature's gate")
    git_in(repo, "push", "-q", "origin", "feature")
    git_in(repo, "switch", "-q", "main")
    git_in(repo, "fetch", "-q", "origin")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(approvals, "one_repo", lambda: None)
    pull = {"number": 7, "state": "OPEN", "baseRefName": "feature", "isDraft": False}
    monkeypatch.setattr(approvals, "pull_request", lambda _n: pull)

    def push(text: str) -> None:
        clone = tmp_path / "other"
        if not clone.exists():
            git_in(tmp_path, "clone", "-q", str(origin), str(clone))
            git_in(clone, "config", "user.email", "loop@example.com")
            git_in(clone, "config", "user.name", "loop")
            git_in(clone, "config", "commit.gpgsign", "false")
        policy(clone, text, "another land's policy")
        git_in(clone, "push", "-q", "origin", "HEAD:main")

    return {"repo": repo, "pull": pull, "push": push}


def test_a_pr_into_a_feature_branch_is_gated_by_that_branchs_policy(
    based: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """check, land and local-ci <pr> each read the policy of the base the PR merges onto, the ref land builds on; a
    command with no PR reads the default branch's."""
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        approvals, "checked", lambda *_a: seen.append(("check", approvals.gate()[0])) or 0
    )
    monkeypatch.setattr(
        approvals, "pr_local_ci", lambda *_a: seen.append(("local-ci", approvals.gate()[0])) or 0
    )
    monkeypatch.setattr(
        approvals,
        "landing_here",
        lambda named, base, worst, tracker, loop, *_a: (
            seen.append(("land", approvals.settings(loop)["gate"])) or (0, False)
        ),
    )
    assert approvals.check("merge", 7) == 0
    assert approvals.local_ci(7) == 0
    assert approvals.landing(7) == (0, False)
    assert seen == [
        ("check", "echo feature"),
        ("local-ci", "echo feature"),
        ("land", "echo feature"),
    ]
    assert approvals.gate()[0] == "echo default"
    based["pull"]["baseRefName"] = "main"
    seen.clear()
    assert approvals.check("merge", 7) == 0
    assert seen == [("check", "echo default")]


def test_land_decides_under_the_policy_it_fetched_under_the_lock(
    based: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another land changes main's policy while this one waits for the lock: the batch is judged under the fetched
    one, and a verdict readied() kept under the old one is not reused."""
    based["pull"]["baseRefName"] = "main"
    monkeypatch.setattr(approvals, "in_cloud", lambda: False)
    monkeypatch.setattr(
        approvals, "readied", lambda named, *_a: [{**pull, "verdict": "old"} for pull in named]
    )
    # nothing was readied, so nothing is judged again before the batch
    monkeypatch.setattr(
        approvals, "landable", lambda pull, *_a, **_kw: pytest.fail(f"PR #{pull['number']} judged")
    )
    monkeypatch.setattr(
        approvals,
        "locked",
        lambda *_a: (
            based["push"](gatekeeper_md("loop:\n  gate: echo newer\n  owner: ann\n")) or True
        ),
    )
    monkeypatch.setattr(approvals, "unlock", lambda *_a: subprocess.CompletedProcess([], 0, "", ""))
    judged: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []
    monkeypatch.setattr(
        approvals,
        "local_queue",
        lambda named, base, tracker, loop, *_a: (
            judged.append((named, approvals.settings(loop))) or (0, False)
        ),
    )
    assert approvals.landing(7) == (0, False)
    [(named, policy)] = judged
    assert (policy["gate"], policy.get("owner")) == ("echo newer", "ann")
    assert "verdict" not in named[0]


def test_the_default_branch_is_asked_of_origin_once_per_run(
    based: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clone without origin/HEAD asks origin for its HEAD: once, however often the policy is read."""
    # git 2.48 and later make origin/HEAD on fetch, which the fixture ran: take it off whatever the version
    git_in(based["repo"], "update-ref", "--no-deref", "-d", "refs/remotes/origin/HEAD")
    assert not git_in(based["repo"], "for-each-ref", "refs/remotes/origin/HEAD").strip()
    asked: list[list[str]] = []
    real = subprocess.run

    def run(args: list[str], *rest: Any, **kw: Any) -> Any:
        if "ls-remote" in args:
            asked.append(args)
        return real(args, *rest, **kw)

    monkeypatch.setattr(approvals.subprocess, "run", run)
    for _ in range(3):
        assert approvals.gate()[0] == "echo default"
    assert len(asked) == 1


@pytest.mark.parametrize(
    "command",
    [
        lambda: approvals.go(7, "a" * 40, approvals.DELEGATE),
        lambda: approvals.delegate(7),
        lambda: approvals.brief(7, None, []),
    ],
    ids=["approve merge --by delegate", "delegate", "brief"],
)
def test_each_command_on_a_pr_reads_its_bases_policy(
    based: dict[str, Any], monkeypatch: pytest.MonkeyPatch, command: Any
) -> None:
    based["pull"].update(headRefOid="a" * 40)
    read: list[str | None] = []

    def project(**_kw: Any) -> Any:
        read.append(approvals.POLICY_BASE.get())
        raise approvals.Refused("read")

    monkeypatch.setattr(approvals, "project", project)
    with pytest.raises(approvals.Refused, match="read"):
        command()
    assert read == ["feature"]


def where(place: str) -> str:
    return gatekeeper_md(
        f"loop:\n  gate: echo {place}\nland:\n  signed_squash_to: main\n  allowed_signers: signers\n"
        f"  where: {place}\n"
    )


@pytest.mark.parametrize(
    ("main", "feature", "refused"),
    [("host", "cloud", True), ("cloud", "host", False)],
    ids=["feature excludes the host", "feature allows the host"],
)
def test_where_a_land_runs_is_its_bases_policy(
    based: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    main: str,
    feature: str,
    refused: bool,
) -> None:
    """A land of a PR into `feature` on a host is placed by feature's `land.where`, not main's."""
    repo = based["repo"]
    policy(repo, where(main), "main's place")
    git_in(repo, "push", "-q", "origin", "main")
    git_in(repo, "switch", "-q", "feature")
    policy(repo, where(feature), "feature's place")
    git_in(repo, "push", "-q", "origin", "feature")
    git_in(repo, "switch", "-q", "main")
    monkeypatch.setattr(approvals, "in_cloud", lambda: False)
    monkeypatch.setattr(approvals, "landing_here", lambda *_a: (0, False))
    if refused:
        with pytest.raises(
            approvals.Refused, match="`land.where` is `cloud`, so land does not run in a host"
        ):
            approvals.landing(7)
    else:
        assert approvals.landing(7) == (0, False)


OWNED = gatekeeper_md("loop:\n  gate: echo newer\n  owner: ann\n")


def test_a_land_reads_origins_policy_not_the_clones_last_fetch(
    based: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another land changed main's policy before this one started, and the clone's origin/main is behind it: drafts are
    readied under origin's policy."""
    based["pull"].update(baseRefName="main", isDraft=True)
    based["push"](OWNED)  # origin moved; this clone's origin/main did not
    monkeypatch.setattr(approvals, "in_cloud", lambda: False)
    seen: list[Any] = []

    def readied(named: list[dict[str, Any]], required: set[str], tracker: Any, loop: Any) -> Any:
        seen.append(approvals.settings(loop).get("owner"))
        raise approvals.Refused("readied")

    monkeypatch.setattr(approvals, "readied", readied)
    with pytest.raises(approvals.Refused, match="readied"):
        approvals.landing(7)
    assert seen == ["ann"]


@pytest.mark.parametrize(
    ("changed", "undone"),
    [(OWNED, True), (gatekeeper_md("loop:\n  gate: echo newer\n"), False)],
    ids=["now waits for the owner", "still ready to mark"],
)
def test_a_draft_readied_before_the_lock_wait_goes_back_to_draft_when_the_new_policy_would_not_ready_it(
    based: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    changed: str,
    undone: bool,
) -> None:
    based["pull"].update(baseRefName="main", isDraft=True)
    # PR 7 a draft this land readies, PR 8 ready already
    monkeypatch.setattr(
        approvals, "pull_request", lambda n: {**based["pull"], "number": n, "isDraft": n == 7}
    )
    monkeypatch.setattr(approvals, "in_cloud", lambda: False)
    monkeypatch.setattr(approvals, "required_checks", lambda _base: set())
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(approvals.github, "gh", lambda *args, **_kw: calls.append(args) or "")
    # readied() marked it ready under the policy before the wait
    monkeypatch.setattr(
        approvals,
        "readied",
        lambda named, *_a: [
            {**pull, "isDraft": False, "readied": True} if pull["isDraft"] else pull
            for pull in named
        ],
    )
    judging: list[int] = []

    def landable(pull: dict[str, Any], _tracker: Any, loop: Any, *_a: Any, **_kw: Any) -> Any:
        judging.append(pull["number"])
        assert pull["isDraft"]
        if approvals.settings(loop).get("owner"):
            return approvals.Verdict((approvals.WAITING, False), issues=[])
        return approvals.Verdict(None, draft=True, issues=[])

    monkeypatch.setattr(approvals, "landable", landable)
    monkeypatch.setattr(approvals, "locked", lambda *_a: based["push"](changed) or True)
    monkeypatch.setattr(approvals, "unlock", lambda *_a: subprocess.CompletedProcess([], 0, "", ""))
    judged: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(
        approvals, "local_queue", lambda named, *_a: judged.append(named) or (0, False)
    )
    capsys.readouterr()
    assert approvals.landing([7, 8]) == (0, False)
    assert judging == [
        7
    ]  # only the PR this land readied is judged again here; the batch judges the rest
    [[pull, ready]] = judged
    assert (ready["number"], ready["isDraft"], "readied" in ready) == (8, False, False)
    lines = capsys.readouterr().out.splitlines()
    if undone:
        assert calls == [("pr", "ready", "--undo", "7")]
        assert lines == [
            (
                "PR #7 is a draft again: it was readied under the policy the base had before this land's wait, and"
                " the base's policy now would not ready it"
            )
        ]
        assert (pull["isDraft"], pull["verdict"].exit, "readied" in pull) == (
            True,
            (approvals.WAITING, False),
            False,
        )
    else:
        assert (calls, lines) == ([], [])
        assert (pull["isDraft"], pull["readied"], "verdict" in pull) == (False, True, False)


def test_a_shallow_clone_is_deepened_for_the_base_the_command_resolved(
    based: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Under `local-ci <pr>` the base is resolved once (policy_from): deepened() does not read the PR again."""
    shallow = tmp_path / "shallow"
    git_in(
        tmp_path,
        "clone",
        "-q",
        "--depth",
        "1",
        "--single-branch",
        "-b",
        "main",
        (tmp_path / "origin.git").as_uri(),
        str(shallow),
    )
    monkeypatch.chdir(shallow)
    monkeypatch.setattr(approvals, "pull_request", lambda n: pytest.fail(f"PR #{n} read again"))
    with approvals.policy_from("feature"):
        approvals.deepened(7)
    assert git_in(shallow, "rev-parse", "--verify", "-q", "refs/remotes/origin/feature").strip()


def test_a_base_that_stops_allowing_this_place_during_the_wait_refuses_and_releases_the_lock(
    based: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = based["repo"]
    policy(repo, where("host"), "main lands on a host")
    git_in(repo, "push", "-q", "origin", "main")
    based["pull"].update(baseRefName="main")
    monkeypatch.setattr(approvals, "in_cloud", lambda: False)
    monkeypatch.setattr(approvals, "readied", lambda named, *_a: named)
    # the lock taken, its commit in `held`, as locked() leaves it
    monkeypatch.setattr(
        approvals,
        "locked",
        lambda _base, held, _pr: based["push"](where("cloud")) or held.append("c" * 40) or True,
    )
    released: list[str] = []
    monkeypatch.setattr(
        approvals,
        "unlock",
        lambda base, _tip: released.append(base) or subprocess.CompletedProcess([], 0, "", ""),
    )
    monkeypatch.setattr(approvals, "local_queue", lambda *_a: pytest.fail("local_queue"))
    with pytest.raises(
        approvals.Refused, match="`land.where` is `cloud`, so land does not run in a host"
    ):
        approvals.landing(7)
    assert released == ["main"]


def test_a_land_signs_with_the_key_the_policy_names_under_the_lock(
    based: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """In a cloud session the policy names the key's variable: one changed during the wait signs the batch."""
    keys = tmp_path / "keys"
    keys.mkdir()
    signers = []
    for name in ("first", "second"):
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(keys / name)],
            check=True,
        )
        monkeypatch.setenv(f"{name.upper()}_KEY", (keys / name).read_text())
        signers.append(f"loop@example.com {(keys / f'{name}.pub').read_text()}")
    (keys / "signers").write_text("".join(signers))

    def keyed(name: str) -> str:
        return gatekeeper_md(
            "loop:\n  gate: echo cloud\nland:\n  signed_squash_to: main\n"
            f"  allowed_signers: {keys / 'signers'}\n  where: cloud\n  signing_key_env: {name}\n"
        )

    repo = based["repo"]
    policy(repo, keyed("FIRST_KEY"), "the first key")
    git_in(repo, "push", "-q", "origin", "main")
    based["pull"].update(baseRefName="main")
    monkeypatch.setattr(approvals, "in_cloud", lambda: True)
    monkeypatch.setattr(approvals, "readied", lambda named, *_a: named)
    monkeypatch.setattr(approvals, "locked", lambda *_a: based["push"](keyed("SECOND_KEY")) or True)
    monkeypatch.setattr(approvals, "unlock", lambda *_a: subprocess.CompletedProcess([], 0, "", ""))
    used: list[str] = []

    def local_queue(*_a: Any) -> tuple[int, bool]:
        key = next(arg for arg in approvals.SIGNING if arg.startswith("user.signingkey="))
        used.append(Path(key.removeprefix("user.signingkey=")).read_text())
        return 0, False

    monkeypatch.setattr(approvals, "local_queue", local_queue)
    assert approvals.landing(7) == (0, False)
    assert used == [(keys / "second").read_text()]
    assert approvals.SIGNING == []


def test_a_failed_fetch_of_the_base_readies_no_draft_and_goes_on_to_the_lock(
    based: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    based["pull"].update(baseRefName="main", isDraft=True)
    git_in(based["repo"], "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    monkeypatch.setattr(approvals, "in_cloud", lambda: False)
    monkeypatch.setattr(approvals, "readied", lambda *_a: pytest.fail("readied"))
    monkeypatch.setattr(approvals, "locked", lambda *_a: False)  # another land holds it
    capsys.readouterr()
    assert approvals.landing(7) == (approvals.WAITING, True)
    out = capsys.readouterr().out
    said = "land could not fetch main from origin, so it readies no draft before the lock: "
    assert out.startswith(said) and out.count("\n") == 1
    assert out.removeprefix(said).strip()  # git's reason


def proof(status: str) -> str:
    return gatekeeper_md(
        f"loop:\n  gate: echo {status}\n  head_proof:\n    status: {status}\n    env: []\n"
    )


def test_the_working_tree_env_var_reads_the_working_tree_and_says_why(monkeypatch, capsys):
    root = str(approvals.toplevel())
    monkeypatch.setenv(approvals.WORKING_TREE_POLICY, "1")
    assert approvals.policy_base(root) is None
    approvals.unbased(root)
    assert (
        f"{approvals.WORKING_TREE_POLICY} is set, so the policy is read" in capsys.readouterr().err
    )
