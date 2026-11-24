"""omnigent_agent.py: a --pr worker is kept by default and held until its PR closes (#943)."""

import json
from pathlib import Path

import pytest
import test_omnigent_agent as base  # the faked server and gh
from test_omnigent_agent import no_model, no_sleep  # noqa: F401  (the fixtures apply here by name)

omnigent_agent = base.omnigent_agent
LINK = "https://github.com/o/r/pull/7#issuecomment-42"


class Hub:
    """gh faked: this checkout is ``o/r``; ``pr view N --repo X`` answers ``states[(X, N)]``."""

    def __init__(self, states: dict[tuple[str, int], str]) -> None:
        self.states, self.asked, self.deleted = states, [], []
        self.comments = {"42": base.on_pr(7, "done")}

    def __call__(self, *args: str) -> str:
        if args[:2] == ("repo", "view"):
            return "o/r\n"
        if args[:2] == ("pr", "view"):
            repo, number = args[args.index("--repo") + 1], int(args[2])
            self.asked.append((repo, number))
            if (repo, number) not in self.states:
                raise omnigent_agent.Fail(f"gh pr view {number}: not found")
            return json.dumps({"state": self.states[(repo, number)]})
        if args[:3] == ("api", "-X", "DELETE"):
            self.deleted.append(args[3])
            return ""
        return json.dumps(self.comments[args[1].rsplit("/", 1)[1]])


def hub(monkeypatch: pytest.MonkeyPatch, **states: str) -> Hub:
    """States by ``<owner>__<name>_<n>`` keyword, e.g. ``o__r_7="OPEN"``."""
    found = {}
    for key, state in states.items():
        slug, _, number = key.rpartition("_")
        found[(slug.replace("__", "/"), int(number))] = state
    fake = Hub(found)
    monkeypatch.setattr(omnigent_agent, "gh", fake)
    return fake


def worker(og: base.FakeOmnigent, tmp_path: Path, pr: int = 7, role: str | None = None) -> str:
    sid, _ = omnigent_agent.start(
        og, "claude-cloud", f"t{pr}", "do X", tmp_path / "r", role, 0, pr=pr
    )
    return sid


@pytest.fixture
def og(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> base.FakeOmnigent:
    fake = base.FakeOmnigent(reply=LINK)
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: fake)
    monkeypatch.chdir(tmp_path)
    return fake


def listed(og: base.FakeOmnigent) -> None:
    """The server's session list carries each live session's labels."""
    call = og.call
    og.call = lambda m, p, b=None: (  # type: ignore[method-assign]
        {"data": [{"id": s, "labels": v} for s, v in og.labels.items() if s not in og.archived]}
        if p.startswith("/v1/sessions?kind=any&limit=1000")
        else call(m, p, b)
    )


def test_wait_with_pr_makes_no_archive_call_and_no_keep_makes_one(
    og: base.FakeOmnigent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub(monkeypatch)
    for title, flags, archived in (("a", [], []), ("b", ["--no-keep"], ["s1"])):
        sid = worker(og, tmp_path, 7)
        assert omnigent_agent.main(["wait", sid, f"{title}.md", "--pr", "7", *flags]) == 0
        assert og.archived == archived
    assert og.labels["s0"] == {"omni.pr": "o/r#7"}


def test_without_pr_the_report_finishes_the_session_unless_keep(
    og: base.FakeOmnigent, tmp_path: Path
) -> None:
    for flags, archived in (([], ["s0"]), (["--keep"], ["s0"])):
        sid, _ = omnigent_agent.start(
            og, "claude", f"n{len(archived)}", "do X", tmp_path / "r", None, 0
        )
        (tmp_path / "r.md").write_text("ok")
        assert omnigent_agent.main(["wait", sid, "r.md", *flags]) == 0
        assert og.archived == archived
    assert og.labels == {}  # no PR, no label


def test_a_verifier_with_pr_ends_with_its_verdict_and_gets_no_label(
    og: base.FakeOmnigent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = hub(monkeypatch)
    fake.comments["42"] = base.on_pr(7, "VERDICT: 0 blocker, 0 major\nSATISFIED: yes\n")
    sid = worker(og, tmp_path, 7, role=str(tmp_path / "verifier.md"))
    assert og.labels == {}
    assert omnigent_agent.main(["wait", sid, "v.md", "--pr", "7", "--verdict"]) == 0
    assert og.archived == [sid]


def test_a_failed_label_fails_the_start_and_archives_the_session(
    og: base.FakeOmnigent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub(monkeypatch)

    def refuse(sid: str, labels: dict[str, str]) -> None:
        raise omnigent_agent.Fail("PATCH: HTTP 503")

    og.label = refuse  # type: ignore[method-assign]
    with pytest.raises(omnigent_agent.Fail, match="not labelled with PR #7: PATCH: HTTP 503"):
        worker(og, tmp_path)
    assert og.archived == ["s0"]


def test_reap_holds_an_open_pr_worker_and_finishes_it_when_the_pr_is_done(
    og: base.FakeOmnigent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    states = {("o/r", 7): "OPEN"}
    monkeypatch.setattr(omnigent_agent, "gh", Hub(states))
    sid = worker(og, tmp_path)
    lines, failed = omnigent_agent.reap(og, [sid])
    assert not failed and og.archived == []
    assert "kept (o/r#7 is open)" in lines[0]
    for state in ("MERGED", "CLOSED"):
        og.archived.clear()
        states[("o/r", 7)] = state
        assert omnigent_agent.reap(og, [sid])[0][0].endswith("archived (idle)")
        assert og.archived == [sid]


def test_a_verdict_does_not_release_a_worker(
    og: base.FakeOmnigent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub(monkeypatch, o__r_7="OPEN")
    sid = worker(og, tmp_path)
    assert "is open" in omnigent_agent.reap(og, [sid])[0][0] and og.archived == []


def test_a_pr_that_cannot_be_read_holds_the_worker(
    og: base.FakeOmnigent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub(monkeypatch)  # no PR known: gh fails
    sid = worker(og, tmp_path)
    lines, _ = omnigent_agent.reap(og, [sid])
    assert og.archived == [] and "o/r#7 not read" in lines[0]


def test_a_label_naming_no_pr_holds_the_worker(og: base.FakeOmnigent) -> None:
    og.labels["s0"] = {"omni.pr": "7"}
    og.sessions["s0"] = []
    lines, _ = omnigent_agent.reap(og, ["s0"])
    assert og.archived == [] and "names no PR" in lines[0]


def test_force_respects_the_hold_of_a_stuck_worker_with_an_open_pr(
    og: base.FakeOmnigent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    states = {("o/r", 7): "OPEN"}
    monkeypatch.setattr(omnigent_agent, "gh", Hub(states))
    sid = worker(og, tmp_path)
    og.final = "running"
    og.items = lambda s: [{"created_at": 1}]  # type: ignore[method-assign]
    lines, failed = omnigent_agent.reap(og, [sid], force=True)
    assert not failed and og.archived == [] and og.interrupted == []
    assert "kept (o/r#7 is open)" in lines[0]
    states[("o/r", 7)] = "CLOSED"
    assert "archived (stuck)" in omnigent_agent.reap(og, [sid], force=True)[0][0]
    assert og.archived == [sid]


def test_the_hold_looks_the_pr_up_in_the_labels_repository(
    og: base.FakeOmnigent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same number in two repositories: this checkout's #7 is merged, the other's is open."""
    fake = hub(monkeypatch, o__r_7="MERGED", x__y_7="OPEN")
    mine = worker(og, tmp_path, 7)
    other = worker(og, tmp_path, 8)
    og.labels[other] = {"omni.pr": "x/y#7"}
    listed(og)
    lines, _ = omnigent_agent.reap(og, [], merged=True, pr=7)
    assert og.archived == [mine]
    assert ("x/y", 7) in fake.asked and "kept (x/y#7 is open)" in "\n".join(lines)
    assert all(repo in ("o/r", "x/y") for repo, _ in fake.asked)


def test_reap_merged_archives_workers_of_done_prs_and_leaves_open_ones(
    og: base.FakeOmnigent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub(monkeypatch, o__r_7="MERGED", o__r_8="CLOSED", o__r_9="OPEN")
    done, closed, open_ = (worker(og, tmp_path, n) for n in (7, 8, 9))
    listed(og)
    lines, failed = omnigent_agent.reap(og, [], merged=True)
    assert not failed and og.archived == [done, closed]
    assert any(line.startswith(open_) and "kept (o/r#9 is open)" in line for line in lines)
    assert omnigent_agent.main(["reap", "--merged", "--pr", "9"]) == 0
    assert og.archived == [done, closed]


def test_reap_merged_with_a_pr_takes_only_that_prs_workers(
    og: base.FakeOmnigent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub(monkeypatch, o__r_7="MERGED", o__r_8="MERGED")
    first, second = worker(og, tmp_path, 7), worker(og, tmp_path, 8)
    listed(og)
    omnigent_agent.reap(og, [], merged=True, pr=8)
    assert og.archived == [second] and first not in og.archived


def test_reap_without_a_target_or_merged_is_refused(og: base.FakeOmnigent) -> None:
    assert omnigent_agent.main(["reap"]) != 0


def test_the_reap_command_passes_merged_and_pr_on(
    og: base.FakeOmnigent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub(monkeypatch, o__r_7="MERGED", o__r_8="MERGED")
    first, second = worker(og, tmp_path, 7), worker(og, tmp_path, 8)
    listed(og)
    assert omnigent_agent.main(["reap", "--merged", "--pr", "8"]) == 0
    assert og.archived == [second] and first not in og.archived
    assert omnigent_agent.main(["reap", "--merged"]) == 0
    assert og.archived == [second, first]


def test_reap_merged_takes_neither_an_unlabelled_nor_an_archived_session(
    og: base.FakeOmnigent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub(monkeypatch, o__r_7="MERGED", o__r_8="MERGED")
    live, gone = worker(og, tmp_path, 7), worker(og, tmp_path, 8)
    og.sessions["plain"] = []
    og.archived.append(gone)
    rows = [
        {"id": live, "labels": {"omni.pr": "o/r#7"}},
        {"id": gone, "labels": {"omni.pr": "o/r#8"}, "archived": True},
        {"id": "plain"},
        {"id": "other", "labels": {"omni.role": "worker"}},
    ]
    call = og.call
    og.call = lambda m, p, b=None: (  # type: ignore[method-assign]
        {"data": rows} if p.startswith("/v1/sessions?kind=any&limit=1000") else call(m, p, b)
    )
    lines, _ = omnigent_agent.reap(og, [], merged=True)
    assert og.archived == [gone, live] and [line.split()[0] for line in lines] == [live]


def where(monkeypatch: pytest.MonkeyPatch, checkouts: dict[Path, str]) -> None:
    """gh faked: ``repo view`` names the repository of the checkout it runs in, and fails elsewhere."""

    def fake(*args: str, cwd: Path | None = None) -> str:
        if args[:2] == ("repo", "view") and (cwd or Path.cwd()) in checkouts:
            return checkouts[cwd or Path.cwd()] + "\n"
        raise omnigent_agent.Fail("gh repo view: fatal: not a git repository")

    monkeypatch.setattr(omnigent_agent, "gh", fake)


def test_a_named_repository_labels_the_worker_from_outside_a_checkout(
    og: base.FakeOmnigent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    where(monkeypatch, {})
    sid, _ = omnigent_agent.start(
        og, "claude-cloud", "t", "do X", tmp_path / "r", None, 0, pr=omnigent_agent.pr_ref("a/b#7")
    )
    assert og.labels[sid] == {"omni.pr": "a/b#7"}


def test_a_bare_pr_takes_its_repository_from_a_checkout_workspace(
    og: base.FakeOmnigent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)
    where(monkeypatch, {ws: "c/d"})
    sid, _ = omnigent_agent.start(
        og, "claude-cloud", "t", "do X", tmp_path / "r", None, 0, workspace=str(ws), pr=7
    )
    assert og.labels[sid] == {"omni.pr": "c/d#7"}


def test_an_unresolvable_repository_is_refused_before_any_session_exists(
    og: base.FakeOmnigent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    where(monkeypatch, {})
    with pytest.raises(omnigent_agent.Fail, match=r"--pr owner/repo#7"):
        omnigent_agent.start(og, "claude-cloud", "t", "do X", tmp_path / "r", None, 0, pr=7)
    assert not og.created
    assert not og.archived


def test_a_worker_of_a_named_repository_is_waited_on_from_outside_a_checkout(
    og: base.FakeOmnigent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og.reply = "https://github.com/a/b/pull/7#issuecomment-42"
    calls: list[tuple[str, ...]] = []

    def fake(*args: str, **kw: object) -> str:
        calls.append(args)
        if args[0] == "api":
            return json.dumps(base.on_pr(7, "done", "a/b"))
        raise omnigent_agent.Fail("gh repo view: fatal: not a git repository")

    monkeypatch.setattr(omnigent_agent, "gh", fake)
    sid, _ = omnigent_agent.start(
        og, "claude-cloud", "t", "do X", tmp_path / "r", None, 0, pr=omnigent_agent.pr_ref("a/b#7")
    )
    assert omnigent_agent.main(["wait", sid, str(tmp_path / "r.md"), "--pr", "a/b#7"]) == 0
    assert (tmp_path / "r.md").read_text() == "done"
    assert ("api", "repos/a/b/issues/comments/42") in calls


def test_reap_merged_with_a_named_pr_takes_only_that_repositorys_workers(
    og: base.FakeOmnigent,
) -> None:
    og.call = lambda *a, **k: {  # type: ignore[method-assign]
        "data": [
            {"id": "s1", "labels": {"omni.pr": "a/b#7"}},
            {"id": "s2", "labels": {"omni.pr": "c/d#7"}},
        ]
    }
    assert omnigent_agent.pr_workers(og, omnigent_agent.pr_ref("a/b#7")) == ["s1"]
    assert omnigent_agent.pr_workers(og, omnigent_agent.pr_ref("7")) == ["s1", "s2"]
