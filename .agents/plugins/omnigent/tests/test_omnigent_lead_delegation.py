"""omnigent_agent.py: a lead's delegation text from the base policy, and a USER NEEDED with no card (#1551)."""

import base64
import json
import subprocess
from pathlib import Path

import pytest
import test_omnigent_agent as base
from test_omnigent_agent import (  # noqa: F401  (the fixtures apply here by name)
    Lead,
    ending,
    following,
    no_model,
    no_sleep,
    posted,
    supervised,
    told,
)

omnigent_agent = base.omnigent_agent

POLICY = "docs/agents/gatekeeper.md"


def checkout(tmp_path: Path, policy: str | None) -> Path:
    """A checkout whose `origin/main` holds ``policy`` as its gatekeeper.md (none: no policy file)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True
        )

    (work / "README.md").write_text("x\n")
    if policy is not None:
        (work / POLICY).parent.mkdir(parents=True)
        (work / POLICY).write_text(policy)
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    git("remote", "add", "origin", str(origin))
    git("push", "-q", "origin", "main")
    git("fetch", "-q", "origin")
    git("remote", "set-head", "origin", "main")
    return work


def policy_with(loop: str) -> str:
    return f"# Gatekeeper\n\n## Policy\n\n```yaml\n{loop}```\n"


@pytest.mark.parametrize(
    ("policy", "delegated"),
    [
        (policy_with("loop:\n  delegate: all\n"), True),
        (policy_with("loop:\n  delegate: feature\n"), False),
        (policy_with("loop:\n  gate: mise run check\n"), False),
        (None, False),
    ],
)
def test_the_delegation_text_follows_the_base_policy(
    tmp_path: Path, policy: str | None, delegated: bool
) -> None:
    work = checkout(tmp_path, policy)
    text = omnigent_agent.lead_delegation(None, str(work))
    assert text == (omnigent_agent.DELEGATION if delegated else "")


def test_the_text_names_the_merge_go_and_the_critical_paths() -> None:
    text = omnigent_agent.DELEGATION
    assert "approve merge <pr> --head <sha> --by delegate" in text
    assert "risk:high" in text and "risk.critical_paths" in text and "sys_ask_user_question" in text
    # the owner's card answer is recorded by the lead, never left for the owner to label by hand (#2170)
    assert (
        "approve spec <n> --by owner" in text and "answered" in text and "never run it" not in text
    )
    gk = omnigent_agent._load_gatekeeper()
    assert gk is not None  # the flags are those gatekeeper's `approve` takes
    src = Path(gk.__file__).read_text()
    assert 'choices=("owner", DELEGATE)' in src and 'approving.add_argument("--head")' in src


def test_a_top_level_lead_start_carries_the_text_only_under_delegate_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    for policy, delegated in ((policy_with("loop:\n  delegate: all\n"), True), (None, False)):
        work = checkout(tmp_path / str(delegated), policy)
        report = tmp_path / f"{delegated}.md"
        omnigent_agent.start(
            base.FakeOmnigent(),
            "claude",
            "t",
            "do X",
            report,
            None,
            0,
            top_level=True,
            workspace=str(work),
        )
        plain = omnigent_agent.LEAD_PREAMBLE.format(report=report, role="")
        if delegated:
            plain = (
                omnigent_agent.LEAD_PREAMBLE.removesuffix("\n") + omnigent_agent.DELEGATION + "\n"
            )
            plain = plain.format(report=report, role="")
        assert Path(f"{report}.brief").read_text() == "AGENT_SIGNATURE=cc/?/?\n" + plain + "do X"
        # #2170: no generated lead brief tells a lead not to record the owner's answered card, or it waits on a given label
        brief = " ".join(Path(f"{report}.brief").read_text().split()).lower()
        assert "never run it" not in brief and "don't record" not in brief
        assert ("approve spec <n> --by owner" in brief) == delegated


def test_a_missing_or_broken_policy_reader_gives_todays_wording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = checkout(tmp_path, policy_with("loop:\n  delegate: all\n"))
    monkeypatch.setattr(omnigent_agent, "_load_gatekeeper", lambda: None)
    assert omnigent_agent.lead_delegation(None, str(work)) == ""

    class Broken:
        @staticmethod
        def default_base(_: str) -> str:
            raise RuntimeError("boom")

        toplevel = default_base

    monkeypatch.setattr(omnigent_agent, "_load_gatekeeper", lambda: Broken)
    assert omnigent_agent.lead_delegation(None, str(work)) == ""


def test_a_gatekeeper_that_exits_on_import_reads_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # gatekeeper.py calls sys.exit when PyYAML is missing; the start goes on with today's wording
    (tmp_path / "gatekeeper.py").write_text("import sys\nsys.exit('needs PyYAML')\n")
    monkeypatch.setattr(omnigent_agent, "installed", lambda skill: tmp_path)
    assert omnigent_agent._load_gatekeeper() is None


BASE_FILES = {
    "docs/agents/gatekeeper.md": policy_with("loop:\n  delegate: all\n"),
    "docs/agents/loop.md": "# Loop facts\n",
}
CONFIG = '[core]\n\tbare = false\n[remote "origin"]\n\turl = https://github.com/acme/repo.git\n'


def far_host(
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, str],
    config: str | None = CONFIG,
    root: str | None = "/w",
    host_files: dict[str, str] | None = None,
) -> list[list[str]]:
    """A checkout at ``/w`` on host ``far`` whose ``.git/config`` is ``config``, and GitHub's default branch ``trunk``
    of ``acme/repo`` holding ``files``, failing as the real APIs do (``Gone``, a ``Fail`` naming the 404); the ``gh``
    calls are returned."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        omnigent_agent, "far_checkout_root", lambda *_: Path(root) if root else None
    )

    def read_on_host(_og: object, _host: str, path: str) -> str:
        if host_files is not None:
            if path in host_files:
                return host_files[path]
        elif path == "/w/.git/config" and config is not None:
            return config
        raise omnigent_agent.Gone(path)

    def gh(*args: str, **_: object) -> str:
        calls.append(list(args))
        endpoint = args[-1]
        if endpoint == "repos/acme/repo":
            return '{"default_branch": "trunk"}'
        path, _, ref = endpoint.removeprefix("repos/acme/repo/contents/").partition("?ref=")
        if ref == "trunk" and path in files:
            if "Accept: application/vnd.github.raw" in args:
                return files[path]
            return json.dumps(
                {"encoding": "base64", "content": base64.b64encode(files[path].encode()).decode()}
            )
        raise omnigent_agent.Fail(f"gh api {endpoint}: gh: Not Found (HTTP 404)")

    monkeypatch.setattr(omnigent_agent, "read_on_host", read_on_host)
    monkeypatch.setattr(omnigent_agent, "gh", gh)
    return calls


SETTINGS_LOOP = "# Loop facts\n\n## Settings\n\n```yaml\nloop:\n  delegate: all\n```\n"


@pytest.mark.parametrize(
    ("files", "delegated"),
    [
        (BASE_FILES, True),
        (
            {
                **BASE_FILES,
                "docs/agents/gatekeeper.md": policy_with("loop:\n  delegate: feature\n"),
            },
            False,
        ),
        ({"docs/agents/loop.md": "# Loop facts\n"}, False),
        ({"docs/agents/loop.md": SETTINGS_LOOP}, True),  # the deprecated source, no gatekeeper.md
        ({}, False),
    ],
)
def test_a_workspace_on_another_host_follows_the_origin_default_branch_s_policy(
    monkeypatch: pytest.MonkeyPatch, files: dict[str, str], delegated: bool
) -> None:
    calls = far_host(monkeypatch, files)
    text = omnigent_agent.lead_delegation(base.FakeOmnigent(), "/w", "far")
    assert text == (omnigent_agent.DELEGATION if delegated else "")
    assert calls[0] == ["api", "repos/acme/repo"]
    assert all(c[-1].endswith("?ref=trunk") for c in calls[1:]) and len(calls) == 3


WORKTREE = {
    "/w/.git": "gitdir: /main/.git/worktrees/w\n",
    "/main/.git/worktrees/w/commondir": "../..\n",
    "/main/.git/config": CONFIG,
}
RELATIVE_WORKTREE = {**WORKTREE, "/w/.git": "gitdir: ../main/.git/worktrees/w\n"}
SUBMODULE = {"/w/.git": "gitdir: ../.git/modules/w\n", "/.git/modules/w/config": CONFIG}


@pytest.mark.parametrize("host_files", [WORKTREE, RELATIVE_WORKTREE, SUBMODULE])
def test_a_far_worktree_or_submodule_workspace_reads_its_origin_through_its_gitdir(
    monkeypatch: pytest.MonkeyPatch, host_files: dict[str, str]
) -> None:
    far_host(monkeypatch, BASE_FILES, host_files=host_files)
    assert (
        omnigent_agent.lead_delegation(base.FakeOmnigent(), "/w", "far")
        == omnigent_agent.DELEGATION
    )


def test_the_far_policy_read_asks_for_the_raw_contents(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = far_host(monkeypatch, BASE_FILES)
    omnigent_agent.lead_delegation(base.FakeOmnigent(), "/w", "far")
    assert all("Accept: application/vnd.github.raw" in c for c in calls[1:])


@pytest.mark.parametrize(
    "config",
    [None, "[core]\n\tbare = false\n", '[remote "origin"]\n\turl = /srv/local.git\n'],
)
def test_an_unreadable_origin_on_another_host_keeps_todays_wording_with_one_stderr_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], config: str | None
) -> None:
    calls = far_host(monkeypatch, BASE_FILES, config=config)
    assert omnigent_agent.lead_delegation(base.FakeOmnigent(), "/w", "far") == ""
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "other host" in err[0] and calls == []


def test_a_failed_gh_call_keeps_todays_wording_with_one_stderr_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    far_host(monkeypatch, BASE_FILES)

    def broken(*args: str, **_: object) -> str:
        raise omnigent_agent.Fail(f"gh {' '.join(args)}: exit status 1")

    monkeypatch.setattr(omnigent_agent, "gh", broken)
    assert omnigent_agent.lead_delegation(base.FakeOmnigent(), "/w", "far") == ""
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_a_far_workspace_outside_a_checkout_keeps_todays_wording_silently(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = far_host(monkeypatch, BASE_FILES, root=None)
    assert omnigent_agent.lead_delegation(base.FakeOmnigent(), "/w", "far") == ""
    assert capsys.readouterr().err == "" and calls == []


def started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str, work: Path | None
) -> list[str]:
    """What ``start`` added to a top-level lead's preamble (``lead_delegation``'s answers) on ``host``, workspace
    ``work`` (else ``/w``); a brief for another host is written there, not here."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(omnigent_agent, "host_capacity", lambda *_a, **_k: (True, ""))
    answers: list[str] = []
    real = omnigent_agent.lead_delegation

    def record(*args: object) -> str:
        answers.append(real(*args))
        return answers[-1]

    monkeypatch.setattr(omnigent_agent, "lead_delegation", record)
    omnigent_agent.start(
        base.FakeOmnigent(), "claude", "t", "do X", tmp_path / "r.md", None, 0,
        top_level=True, workspace=str(work or "/w"), host_id=host,
    )  # fmt: skip
    return answers


def test_a_top_level_lead_started_on_another_host_carries_the_far_policy_s_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    far_host(monkeypatch, BASE_FILES)
    assert started(tmp_path, monkeypatch, "far", None) == [omnigent_agent.DELEGATION]
    far_host(monkeypatch, {})
    assert started(tmp_path / "2", monkeypatch, "far", None) == [""]


def test_a_lead_started_on_this_host_named_reads_the_local_checkout_not_the_far_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    far_host(monkeypatch, {})  # a far read would find no policy and no text
    work = checkout(tmp_path / "local", policy_with("loop:\n  delegate: all\n"))
    ran: list[str] = []
    real = omnigent_agent._load_gatekeeper

    def spy() -> object:
        ran.append("local")
        return real()

    monkeypatch.setattr(omnigent_agent, "_load_gatekeeper", spy)
    brief = started(tmp_path, monkeypatch, "h", work)  # FakeOmnigent's own host id
    assert ran == ["local"] and brief == [omnigent_agent.DELEGATION]


def test_an_unasked_line_keys_the_owner_item_as_a_plain_turn_end_does() -> None:
    last = "\nlast message: USER NEEDED: q"
    plain = omnigent_agent.owner_item(f"a t: turn ended{last}")
    asked = omnigent_agent.owner_item(f"a t: {omnigent_agent.UNASKED_LINE}{last}")
    assert plain == asked == last.lstrip("\n")


def test_the_cloud_swaps_still_apply_to_a_delegating_lead_preamble() -> None:
    lead = omnigent_agent.LEAD_PREAMBLE.removesuffix("\n") + omnigent_agent.DELEGATION + "\n"
    cloud = omnigent_agent.cloud_preamble(lead, None)
    assert "Your files stay in this cloud VM" in cloud and "--by delegate" in cloud


CARD = {"elicitation_id": "question_1", "params": {"message": "Land it?"}}


@pytest.mark.parametrize(
    ("snap", "line"),
    [({}, omnigent_agent.UNASKED_LINE), ({"pending_elicitations": [CARD]}, "")],
)
def test_user_needed_with_no_card_is_its_own_event_and_with_one_the_card_alone_tells_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, snap: dict, line: str
) -> None:
    registry = following(tmp_path)
    og = Lead(told("brief", 60), ending("USER NEEDED: land it?", 1))
    og.snap = snap
    supervised(og, registry, monkeypatch, 3, notify="cos")
    news = [p for p in posted(og) if "asked its user" not in p]  # a card is told once, by itself
    # with a card open the end is not forwarded too: the card already reaches the owner (#1529)
    assert news == (
        [f"Supervisor: a t-a: {line}\nlast message: USER NEEDED: land it?"] if line else []
    )


@pytest.mark.parametrize("decisions", [False, True])
def test_the_unasked_kind_routes_to_the_manager_to_act_on(decisions: bool) -> None:
    """A USER NEEDED with no card is its manager's question (or a card the lead forgot), never one to forward as it
    stands: the manager asks the owner on its own card or sends the lead to its card (#2170). The small model is not
    asked, standing decisions or not; a direct lead and a repeat are recorded as any turn end."""
    event = {"kind": omnigent_agent.UNASKED, "line": "a: x", "last": "USER NEEDED: q"}
    event["decisions"] = decisions

    def ask() -> tuple[str, str]:
        raise AssertionError("the small model is not asked: the route is the rule's")

    got = omnigent_agent.route(event, ask)
    assert got == omnigent_agent.route({**event, "kind": "ended"}, ask) and got[:2] == (
        "rule",
        "act",
    )
    for extra in ({"direct": True}, {"repeat": True}):
        assert omnigent_agent.route({**event, **extra}, ask)[1] == "record"


@pytest.mark.parametrize(
    ("text", "info", "carded", "expected"),
    [
        ("USER NEEDED: q", {}, False, True),
        ("USER NEEDED: q", {"pending_elicitations": [CARD]}, False, False),
        ("USER NEEDED: q", {}, True, False),
        ("DONE: shipped", {}, False, False),
        ("STILL WORKING: waiting on CI", {}, False, False),
    ],
)
def test_unasked_end_wants_a_user_needed_ending_and_no_card(
    text: str, info: dict, carded: bool, expected: bool
) -> None:
    items = [ending(text, 1)]
    assert omnigent_agent.unasked_end(None, "a", info, items, carded) is expected


def test_brief_prints_a_top_level_leads_delegation_as_start_sends_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`brief` prints the body `start` sends, so under `delegate: all` a cloud lead's child lead gets the
    delegation line through sys_session_create as well (#1509)."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    work = checkout(tmp_path, policy_with("loop:\n  delegate: all\n"))
    report = tmp_path / "r.md"
    omnigent_agent.start(
        base.FakeOmnigent(),
        "claude",
        "t",
        "do X",
        report,
        None,
        0,
        top_level=True,
        workspace=str(work),
    )
    sent = Path(f"{report}.brief").read_text().partition("\n")[2]
    monkeypatch.chdir(work)
    assert omnigent_agent.main(["brief", "do X", str(report), "--top-level"]) == 0
    printed = capsys.readouterr().out
    assert printed == sent and omnigent_agent.DELEGATION.strip() in printed
