"""A PR the merge queue dequeues reaches the lead that owns it, and the queue's rejections are counted (#2085).

The seam: ``gh`` faked to serve repo o/r's open PRs (none), its issue comments (Mergify's status comments, one per queue
entry) and each failing job's log; the Omnigent server faked as a fleet of sessions with labels, parents and an
archived flag, so the supervisor's PR poll reads and routes them as it would GitHub's and Omnigent's."""

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

import pytest
from test_omnigent_agent import Leads, ending, events_of, omnigent_agent, supervised, told

DAY = 86400.0
FIXTURE = Path(__file__).with_name(
    "mergify_queue_comments.json"
)  # copied from a real PR's Mergify comments


def iso(ago: float) -> str:
    return datetime.fromtimestamp(time.time() - ago, UTC).isoformat()


def comment(
    cid: int,
    pr: int,
    state: str = "dequeued",
    code: str = "CHECKS_FAILED",
    jobs: tuple[int, ...] = (),
    ago: float = 60.0,
    fault: bool = False,
    login: str = "mergify[bot]",
) -> dict:
    """A Mergify status comment shaped as the real one: its payload, then a ``Failing checks:`` list of ``qualify``
    runs, one per job in ``jobs``."""
    payload = {"version": 1, "state": state, "left_at": iso(ago)}
    if state == "dequeued":
        payload["dequeue_reason"] = {"code": code, "checks": ["qualify"] * bool(jobs)}
    url = "https://github.com/o/r/actions/runs/9/job/{}"
    failing = "".join(
        f"- ❌ [`qualify`]({url.format(j)}) ([job log]({url.format(j)}))\n" for j in jobs
    )
    body = (
        "<!---\nDO NOT EDIT\n-*- Mergify Payload -*-\n"
        + json.dumps(payload)
        + "\n-*- Mergify Payload End -*-\n-->\n\n### Left the queue\n\n"
        # an earlier attempt's red run, in the history above the reason: not one of the failing checks
        + f"- ❌ **Checks failed (attempt 1/2)**\n  - ❌ [`old`]({url.format(1)})\n\n"
        + ("**The failure is in this pull request.** It failed alone on top of `main`.\n\n" * fault)
        + (f"Failing checks:\n{failing}\n**To get back in the queue**\n" if jobs else "")
    )
    return {
        "id": cid,
        "issue_url": f"https://api.github.com/repos/o/r/issues/{pr}",
        "user": {"login": login},
        "updated_at": iso(ago - 1),
        "body": body,
    }


def log_of(*tests: str) -> str:
    stamp = "2026-11-22T23:46:42.6823503Z"
    return (
        "".join(f"{stamp} FAILED {t} - AssertionError: x\n" for t in tests) + f"{stamp} 1 failed\n"
    )


class GitHub:
    """``gh`` for repo o/r: ``comments`` is its issue comments (a test adds to it between polls), ``logs`` each job's
    log; ``down`` makes the next reads fail (``RateLimited`` when it is ``"limit"``). Each call is recorded."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.comments: list[dict] = []
        self.logs: dict[int, str] = {}
        self.calls: list[tuple[str, ...]] = []
        self.down = ""
        self.pulls_down = False
        self.log_limit = 0  # the next log reads refused for the rate limit
        monkeypatch.setattr(omnigent_agent, "gh", self.gh)

    def gh(self, *args: str, timeout: float = 0) -> str:
        self.calls.append(args)
        path = next((a for a in args if a.startswith("repos/")), "")
        if self.down == "limit" and "comments" in path:
            raise omnigent_agent.RateLimited("gh api: API rate limit exceeded", 0.0)
        if self.down and "comments" in path:
            raise omnigent_agent.Fail("gh api: HTTP 502")
        if self.pulls_down and "pulls" in path:
            raise omnigent_agent.Fail("gh api: HTTP 502")
        if job := re.fullmatch(r"repos/o/r/actions/jobs/(\d+)/logs", path):
            if self.log_limit:
                self.log_limit -= 1
                raise omnigent_agent.RateLimited("gh api: API rate limit exceeded", 0.0)
            return self.logs.get(int(job[1]), "no tests ran\n")
        if path.startswith("repos/o/r/pulls?"):
            out = "[]"
        elif path.startswith("repos/o/r/issues/comments?"):
            since = unquote(re.search(r"since=([^&]+)", path)[1])
            shown = [c for c in self.comments if c["updated_at"] >= since]
            out = json.dumps(shown if "page=1" in path else [])
        else:
            raise omnigent_agent.Fail(f"gh {' '.join(args)}: HTTP 404")
        return f'HTTP/2.0 200 OK\nEtag: "{hash(out)}"\n\n{out}'

    def listings(self) -> list[str]:
        return [a for c in self.calls for a in c if "issues/comments" in a]

    def log_reads(self) -> list[str]:
        return [a for c in self.calls for a in c if a.endswith("/logs")]


class Fleet(Leads):
    """Followed lead ``a`` (its labels in ``snaps``) and any other sessions, listed with their labels, parents and
    archived flag, as ``GET /v1/sessions`` lists them; an archived session takes no message until unarchived.
    ``refuse_unarchive`` makes the unarchive fail."""

    def __init__(self, **histories: list[dict]) -> None:
        super().__init__(**histories)
        self.unarchived: list[str] = []
        self.refuse_unarchive = False
        self.unlisted = 0  # the next session lists that fail

    def call(self, method: str, path: str, body: object = None) -> dict:
        assert method == "GET" and path.startswith("/v1/sessions?"), path
        if self.unlisted:
            self.unlisted -= 1
            raise omnigent_agent.Fail("GET /v1/sessions: HTTP 502")
        rows = [{"id": s, **self.snaps.get(s, {})} for s in self.histories]
        return {"data": rows}

    def unarchive(self, sid: str) -> None:
        if self.refuse_unarchive:
            raise omnigent_agent.Fail(f"PATCH /v1/sessions/{sid}: HTTP 503")
        self.unarchived.append(sid)
        self.snaps[sid].pop("archived", None)


def fleet(tmp_path: Path, **sessions: dict) -> tuple[Path, Fleet]:
    """Lead ``a`` followed in the registry, quiet on a named wait; each of ``sessions`` another session with the
    given info (labels, ``parent_session_id``, ``archived``)."""
    registry = tmp_path / "registry"
    registry.write_text(f"a={tmp_path / 'a.md'}\t{tmp_path / 'a.md.brief'}\tfollow\n")
    quiet = [told("brief", 9000), ending("STILL WORKING: waiting on CI\nWaits: CI", 8000)]
    og = Fleet(a=list(quiet), **{s: list(quiet) for s in sessions})
    og.snaps.update(sessions)
    return registry, og


def run(
    og: Fleet, registry: Path, monkeypatch: pytest.MonkeyPatch, polls: int, between=lambda p: None
) -> None:
    monkeypatch.setattr(omnigent_agent, "PR_STALL_POLL", 0.0)
    monkeypatch.setattr(omnigent_agent, "HEAD_SECONDS", 1e9)
    supervised(
        og, registry, monkeypatch, polls, between, notify="cos", console=True, stall_repos=("o/r",)
    )


def relays(og: Fleet, pr: int) -> list[tuple[str, str]]:
    return [(s, t) for s, t in og.inbox if f"o/r#{pr} left the merge queue" in t]


def acts(og: Fleet, word: str) -> list[str]:
    return [t for s, t in og.inbox if s == "cos" and word in t and "[act]" in t]


def test_queue_comment_parses() -> None:
    """The real comments: a dequeue (its last run's failing check, not the earlier attempts' runs above it, and
    Mergify's fault line) and a merge; a comment without the payload, or not from Mergify, is no entry."""
    dequeue, merged = json.loads(FIXTURE.read_text())
    entry = omnigent_agent.queue_comment(dequeue)
    assert entry is not None
    assert (entry["pr"], entry["state"], entry["code"]) == (1874, "dequeued", "CHECKS_FAILED")
    assert entry["checks"] == [
        [
            "qualify",
            "https://github.com/kzarzycki/agent-skills/actions/runs/45084791976/job/137400839102",
            "137400839102",
        ]
    ]
    assert entry["fault"].startswith("The failure is in this pull request.")
    assert entry["left_at"] == pytest.approx(
        datetime.fromisoformat("2026-11-22T23:47:29.822708+00:00").timestamp()
    )
    done = omnigent_agent.queue_comment(merged)
    assert done is not None
    assert (done["pr"], done["state"], done["code"], done["checks"], done["fault"]) == (
        1874,
        "merged",
        "",
        [],
        "",
    )
    assert omnigent_agent.queue_comment({**dequeue, "user": {"login": "someone"}}) is None
    plain = {**dequeue, "body": "Queue requested — the merge queue status continues below."}
    assert omnigent_agent.queue_comment(plain) is None


def test_failed_tests_strip_the_actions_timestamp() -> None:
    log = log_of("tests/test_a.py::test_x[p-1]", "tests/test_b.py::test_y") + log_of(
        "tests/test_a.py::test_x[p-1]"
    )
    assert omnigent_agent.failed_tests(log) == [
        "tests/test_a.py::test_x[p-1]",
        "tests/test_b.py::test_y",
    ]


def test_dequeue_relays_to_labelled_lead(tmp_path, monkeypatch) -> None:
    """Lead ``a`` carries ``omni.pr=o/r#12``; its worker ``w`` carries the same label and must not get it."""
    registry, og = fleet(
        tmp_path,
        w={"labels": {"omni.pr": "o/r#12", "omni.role": "worker"}, "parent_session_id": "a"},
    )
    og.snaps["a"] = {"labels": {"omni.pr": "o/r#12", "omni.role": "lead"}}
    gh = GitHub(monkeypatch)
    gh.comments.append(comment(100, 12, jobs=(7,), fault=True))
    gh.logs[7] = log_of("tests/test_x.py::test_one")
    run(og, registry, monkeypatch, 3)
    [(to, text)] = relays(og, 12)
    assert to == "a"
    assert "Failing check: qualify https://github.com/o/r/actions/runs/9/job/7" in text
    assert "Failing tests: tests/test_x.py::test_one" in text and "known flake" not in text
    assert "The failure is in this pull request." in text and "run `land 12` again" in text
    assert gh.log_reads() == ["repos/o/r/actions/jobs/7/logs"]  # once, however many polls
    assert len(gh.listings()) == 3  # one comments read a poll, at the PR poll
    assert not acts(og, "o/r#12")
    assert [e["action"] for e in events_of(registry) if e["kind"] == "dequeued"] == ["relay"]


def test_a_dequeue_is_relayed_once_across_a_restart(tmp_path, monkeypatch) -> None:
    registry, og = fleet(tmp_path)
    og.snaps["a"] = {"labels": {"omni.pr": "o/r#12", "omni.role": "lead"}}
    gh = GitHub(monkeypatch)
    gh.comments.append(comment(100, 12, jobs=(7,)))
    run(og, registry, monkeypatch, 2)
    run(og, registry, monkeypatch, 2)  # a restarted supervisor, its state loaded from disk
    assert len(relays(og, 12)) == 1
    assert len(gh.log_reads()) == 1
    # a new dequeue of the same PR is a new comment: a new relay
    gh.comments.append(comment(101, 12, jobs=(8,), ago=30))
    run(og, registry, monkeypatch, 2)
    assert len(relays(og, 12)) == 2


def test_dequeue_relays_to_worker_parent_lead(tmp_path, monkeypatch) -> None:
    """The PR's ``start --pr`` worker ``w`` is archived; its parent ``m`` is a plain session, and ``m``'s parent ``L``
    is the lead that started it, archived too: ``L`` is unarchived and told, never ``w``."""
    registry, og = fleet(
        tmp_path,
        w={
            "labels": {"omni.pr": "o/r#12", "omni.role": "worker"},
            "parent_session_id": "m",
            "archived": True,
        },
        m={"parent_session_id": "L"},
        L={"labels": {"omni.role": "lead"}, "archived": True},
    )
    gh = GitHub(monkeypatch)
    gh.comments.append(comment(100, 12, jobs=(7,)))
    run(og, registry, monkeypatch, 2)
    assert [s for s, _ in relays(og, 12)] == ["L"]
    assert og.unarchived == ["L"]


def test_dequeue_unarchives_lead(tmp_path, monkeypatch) -> None:
    """The archived lead is reached through ``relay``'s own unarchive, which runs before the send."""
    registry, og = fleet(
        tmp_path, L={"labels": {"omni.role": "lead", "omni.pr": "o/r#12"}, "archived": True}
    )
    gh = GitHub(monkeypatch)
    gh.comments.append(comment(100, 12, jobs=(7,)))
    order: list[str] = []
    asked: list[dict] = []
    real_relay = omnigent_agent.relay

    def relay(og_, sid, text, *a, **kw):
        asked.append({"sid": sid, **kw})
        return real_relay(og_, sid, text, *a, **kw)

    monkeypatch.setattr(omnigent_agent, "relay", relay)
    unarchive, send = og.unarchive, og.send
    og.unarchive = lambda sid: (order.append(f"unarchive {sid}"), unarchive(sid))[1]
    og.send = lambda sid, text: (order.append(f"send {sid}"), send(sid, text))[1]
    run(og, registry, monkeypatch, 2)
    assert [a for a in asked if a["sid"] == "L"] == [{"sid": "L", "unarchive": True}]
    assert order[:2] == ["unarchive L", "send L"]
    assert [s for s, _ in relays(og, 12)] == ["L"]


def test_dequeue_host_offline_retries(tmp_path, monkeypatch) -> None:
    """The lead's host is offline on the first poll: nothing is sent and no act event raised; the next poll
    delivers it, once."""
    registry, og = fleet(tmp_path, L={"labels": {"omni.role": "lead", "omni.pr": "o/r#12"}})
    og.snaps["L"].update(host_online=False)
    og.offline.add("L")
    gh = GitHub(monkeypatch)
    gh.comments.append(comment(100, 12, jobs=(7,)))

    def back(poll: int) -> None:
        if poll == 1:
            assert not relays(og, 12)
            og.snaps["L"].pop("host_online")
            og.offline.discard("L")

    run(og, registry, monkeypatch, 4, back)
    assert [s for s, _ in relays(og, 12)] == ["L"]
    assert not acts(og, "not delivered") and not acts(og, "o/r#12")
    assert [e["id"] for e in events_of(registry) if e["kind"] == "dequeued"] == [
        "dequeued:o/r:100",
        "dequeued:o/r:100:1",
    ]


def test_a_followed_lead_whose_wait_names_the_pr_owns_it(tmp_path, monkeypatch) -> None:
    registry, og = fleet(tmp_path)
    og.histories["a"][-1] = ending("STILL WORKING: waiting on the queue\nWaits: o/r#12", 8000)
    gh = GitHub(monkeypatch)
    gh.comments.append(comment(100, 12, jobs=(7,)))
    run(og, registry, monkeypatch, 2)
    assert [s for s, _ in relays(og, 12)] == ["a"]


@pytest.mark.parametrize("failure", ["no lead", "unarchive", "relay"])
def test_dequeue_without_lead_acts(tmp_path, monkeypatch, failure) -> None:
    """No lead owns the PR, or its unarchive or relay fails: one act event to the manager, never sent twice."""
    registry, og = fleet(
        tmp_path, L={"labels": {"omni.role": "lead", "omni.pr": "o/r#12"}, "archived": True}
    )
    if failure == "no lead":
        og.snaps["L"]["labels"]["omni.pr"] = "o/r#99"
    og.refuse_unarchive = failure == "unarchive"
    if failure == "relay":
        og.snaps["L"].pop("archived")
        og.offline.add("L")  # a runner that never comes back: the message is lost
        monkeypatch.setattr(omnigent_agent, "ACTION_SECONDS", 0.05)
        monkeypatch.setattr(
            omnigent_agent,
            "revive",
            lambda og, sid: (_ for _ in ()).throw(omnigent_agent.Fail("no runner came up")),
        )
    gh = GitHub(monkeypatch)
    gh.comments.append(comment(100, 12, jobs=(7,)))
    run(og, registry, monkeypatch, 4)
    woken = acts(og, "o/r#12" if failure == "no lead" else "not delivered")
    assert len(woken) == 1
    if failure == "no lead":  # the whole message, its failing check too, not only its first line
        assert "left the merge queue" in woken[0]
        assert "Failing check: qualify https://github.com/o/r/actions/runs/9/job/7" in woken[0]
    assert not [t for s, t in og.inbox if s == "L"]


def queue(*entries: tuple[int, int, str, tuple[str, ...]], ago: float = 3600) -> dict:
    """A ``queue_read`` memo of entries ``(id, pr, state, tests)``, each ``ago`` seconds old."""
    return {
        "since": iso(0),
        "entries": {
            str(cid): {
                "pr": pr,
                "state": state,
                "left_at": time.time() - ago,
                "code": "CHECKS_FAILED" if state == "dequeued" else "",
                "checks": [["qualify", "u", "1"]] if state == "dequeued" else [],
                "fault": "",
                "tests": list(tests),
                "told": True,
            }
            for cid, pr, state, tests in entries
        },
    }


def kinds(said: list[tuple[str, str, str]]) -> list[str]:
    return [k for _, k, _ in said]


def test_rejection_share_threshold() -> None:
    merged = [(i, i, "merged", ()) for i in range(1, 5)]
    memo = queue(*merged, (10, 10, "dequeued", ("t::a",)), (11, 11, "dequeued", ("t::b",)))
    [(_, kind, line)] = omnigent_agent.queue_alarms("o/r", memo)
    assert kind == "queue-rejects"
    assert (
        "rejected 2 of 6 entries" in line
        and "33%" in line
        and "#10 (t::a)" in line
        and "#11 (t::b)" in line
    )
    assert omnigent_agent.queue_alarms("o/r", memo) == []  # the next poll: none
    for i in range(20, 25):  # 2 of 11: back under 20%, which re-arms it
        memo["entries"][str(i)] = {**memo["entries"]["1"], "pr": i}
    assert omnigent_agent.queue_alarms("o/r", memo) == []
    memo["entries"]["30"] = {**memo["entries"]["10"], "pr": 30, "tests": ["t::c"]}  # 3 of 12: 25%
    assert kinds(omnigent_agent.queue_alarms("o/r", memo)) == ["queue-rejects"]
    few = queue(
        (1, 1, "merged", ()),
        (2, 2, "merged", ()),
        (3, 3, "dequeued", ("t::a",)),
        (4, 4, "dequeued", ("t::b",)),
    )
    assert omnigent_agent.queue_alarms("o/r", few) == []  # 4 entries: under the floor


def test_a_dequeue_other_than_failed_checks_is_an_entry_not_a_rejection() -> None:
    memo = queue(
        *[(i, i, "merged", ()) for i in range(1, 4)], (8, 8, "dequeued", ()), (9, 9, "dequeued", ())
    )
    for e in memo["entries"].values():
        if e["state"] == "dequeued":
            e["code"] = "PR_MANUALLY_UNQUEUED"
    counts = omnigent_agent.queue_counts(memo["entries"])
    assert (counts["entries"], counts["rejected"]) == (5, [])


def test_flake_told_apart(tmp_path, monkeypatch) -> None:
    """``t::flaky`` fails the queue runs of #12 and #13 in one poll: one queue-flake event, neither dequeue is a
    rejection, and both relays name it as a flake; #14's own test makes it a rejection."""
    registry, og = fleet(tmp_path)
    og.snaps["a"] = {"labels": {"omni.role": "lead"}}
    gh = GitHub(monkeypatch)
    gh.comments += [
        comment(100, 12, jobs=(7,)),
        comment(101, 13, jobs=(8,)),
        comment(102, 14, jobs=(9,)),
    ]
    gh.logs.update({7: log_of("t::flaky"), 8: log_of("t::flaky"), 9: log_of("t::flaky", "t::own")})
    run(og, registry, monkeypatch, 3)
    flaky = [e for e in events_of(registry) if e["kind"] == "queue-flake"]
    assert (
        len(flaky) == 1
        and "`t::flaky` failed merge-queue runs of #12, #13, #14" in flaky[0]["line"]
    )
    assert "Known-red tests" in flaky[0]["line"]
    assert sum(t.count("failed merge-queue runs of") for _, t in og.inbox) == 1
    told_ = [e["line"] for e in events_of(registry) if e["kind"] == "dequeued"]  # no lead owns them
    assert len(told_) == 3
    assert all("t::flaky (a known flake)" in t for t in told_)
    assert sum("Every failing test is a known flake" in t for t in told_) == 2
    counts = omnigent_agent.queue_counts(
        json.loads(registry.with_suffix(".state.json").read_text())["queue"]["o/r"]["entries"]
    )
    assert sorted(counts["rejected"]) == ["102"]
    assert counts["flakes"] == {"t::flaky": [12, 13, 14]}


def test_two_prs_make_a_flake_and_one_does_not() -> None:
    two = queue((1, 1, "dequeued", ("t::a",)), (2, 2, "dequeued", ("t::a",)))
    assert omnigent_agent.queue_counts(two["entries"])["flakes"] == {"t::a": [1, 2]}
    one = queue((1, 1, "dequeued", ("t::a",)), (2, 1, "dequeued", ("t::a",)))  # the same PR twice
    assert omnigent_agent.queue_counts(one["entries"])["flakes"] == {}


def test_window_prunes(monkeypatch) -> None:
    gh = GitHub(monkeypatch)
    gh.comments += [
        comment(100, 12, jobs=(7,), ago=6 * DAY),
        comment(101, 13, state="merged", ago=60),
    ]
    memo: dict = {}
    omnigent_agent.queue_read("o/r", memo)
    assert sorted(memo["entries"]) == ["100", "101"]
    assert omnigent_agent.queue_counts(memo["entries"])["entries"] == 2
    later = time.time() + 2 * DAY  # #12's entry is now 8 days old
    assert omnigent_agent.queue_counts(memo["entries"], later)["entries"] == 1
    omnigent_agent.queue_read("o/r", memo, later)
    assert sorted(memo["entries"]) == ["101"]
    # the first read backfills 7 days and no more: an entry 8 days old is never kept
    gh.comments.append(comment(102, 14, ago=8 * DAY))
    gh.comments[-1]["updated_at"] = iso(10)  # edited since (a requeue line)
    omnigent_agent.queue_read("o/r", memo)
    assert "102" not in memo["entries"]


def test_the_first_read_counts_old_dequeues_and_relays_only_new_ones(monkeypatch) -> None:
    gh = GitHub(monkeypatch)
    gh.comments += [comment(100, 12, ago=2 * DAY), comment(101, 13, ago=60)]
    memo: dict = {}
    assert omnigent_agent.queue_read("o/r", memo) == ["101"]
    assert memo["entries"]["100"]["told"] is True
    since = gh.listings()[0]
    assert unquote(re.search(r"since=([^&]+)", since)[1]) <= iso(7 * DAY - 60)


def test_a_poll_where_nothing_moved_reads_the_same_path_for_a_304(monkeypatch) -> None:
    gh = GitHub(monkeypatch)
    gh.comments.append(comment(100, 12, ago=60))
    memo: dict = {}
    omnigent_agent.queue_read("o/r", memo)
    omnigent_agent.queue_read("o/r", memo)
    omnigent_agent.queue_read("o/r", memo)
    first, second, third = gh.listings()
    assert second == third  # the since cursor rests: the page's ETag answers it
    assert (
        unquote(re.search(r"since=([^&]+)", second)[1])
        == gh.comments[0]["updated_at"]
        != unquote(re.search(r"since=([^&]+)", first)[1])
    )
    assert any("If-None-Match" in a for a in gh.calls[-1])


@pytest.mark.parametrize("down", ["limit", "fail"])
def test_a_failed_queue_read_keeps_state_and_retries_next_poll(tmp_path, monkeypatch, down) -> None:
    registry, og = fleet(tmp_path)
    og.snaps["a"] = {"labels": {"omni.pr": "o/r#12", "omni.role": "lead"}}
    gh = GitHub(monkeypatch)
    gh.comments.append(comment(100, 12, jobs=(7,)))
    gh.down = down
    monkeypatch.setattr(omnigent_agent, "RESET_JITTER", 0.0)

    def recover(poll: int) -> None:
        if poll == 2:
            gh.down = ""

    run(og, registry, monkeypatch, 4, recover)
    assert len(relays(og, 12)) == 1


def test_the_share_counts_at_its_boundaries() -> None:
    """5 entries is enough to judge; a share of exactly 20% is not over it."""
    merged = [(i, i, "merged", ()) for i in range(1, 4)]
    five = queue(*merged, (10, 10, "dequeued", ("t::a",)), (11, 11, "dequeued", ("t::b",)))
    assert kinds(omnigent_agent.queue_alarms("o/r", five)) == ["queue-rejects"]
    fifth = queue(*merged, (4, 4, "merged", ()), (10, 10, "dequeued", ("t::a",)))
    assert omnigent_agent.queue_alarms("o/r", fifth) == []


def test_falling_under_the_floor_does_not_re_arm_the_rejection_event() -> None:
    merged = [(i, i, "merged", ()) for i in range(1, 5)]
    memo = queue(*merged, (10, 10, "dequeued", ("t::a",)), (11, 11, "dequeued", ("t::b",)))
    assert kinds(omnigent_agent.queue_alarms("o/r", memo)) == ["queue-rejects"]
    kept = {c: memo["entries"].pop(c) for c in ("1", "2")}  # 2 of 4: over 20%, but under the floor
    assert omnigent_agent.queue_alarms("o/r", memo) == []
    memo["entries"].update(kept)  # 2 of 6 again: the share never dropped back, so no new event
    assert omnigent_agent.queue_alarms("o/r", memo) == []


def test_only_a_failed_checks_dequeue_reads_its_job_logs(monkeypatch) -> None:
    gh = GitHub(monkeypatch)
    gh.comments += [comment(100, 12, code="CONFLICT", jobs=(7,)), comment(101, 13, state="merged")]
    memo: dict = {}
    omnigent_agent.queue_read("o/r", memo)
    assert gh.log_reads() == [] and memo["entries"]["100"]["tests"] == []


def test_a_rate_limited_log_read_keeps_the_entry_for_the_next_read(monkeypatch) -> None:
    gh = GitHub(monkeypatch)
    gh.comments.append(comment(100, 12, jobs=(7,)))
    gh.logs[7] = log_of("t::a")
    gh.log_limit = 1
    memo: dict = {}
    with pytest.raises(omnigent_agent.RateLimited):
        omnigent_agent.queue_read("o/r", memo)
    assert "100" not in memo.get("entries", {}) and "since" not in memo
    assert omnigent_agent.queue_read("o/r", memo) == ["100"]
    assert memo["entries"]["100"]["tests"] == ["t::a"]


def test_an_unread_session_list_sends_the_dequeue_at_the_next_poll(tmp_path, monkeypatch) -> None:
    registry, og = fleet(tmp_path, L={"labels": {"omni.role": "lead", "omni.pr": "o/r#12"}})
    gh = GitHub(monkeypatch)
    gh.comments.append(comment(100, 12, jobs=(7,)))
    og.unlisted = 1
    run(og, registry, monkeypatch, 3)
    assert [s for s, _ in relays(og, 12)] == ["L"]


def test_a_failed_pr_listing_still_reads_the_queue(tmp_path, monkeypatch) -> None:
    registry, og = fleet(tmp_path, L={"labels": {"omni.role": "lead", "omni.pr": "o/r#12"}})
    gh = GitHub(monkeypatch)
    gh.comments.append(comment(100, 12, jobs=(7,)))
    gh.pulls_down = True
    run(og, registry, monkeypatch, 2)
    assert [s for s, _ in relays(og, 12)] == ["L"]


def test_a_labelled_lead_wins_over_another_leads_worker(tmp_path, monkeypatch) -> None:
    """``w`` (listed first) is a worker labelled with the PR under lead ``Y``; lead ``X`` carries the label itself."""
    registry, og = fleet(
        tmp_path,
        w={"labels": {"omni.pr": "o/r#12", "omni.role": "worker"}, "parent_session_id": "Y"},
        Y={"labels": {"omni.role": "lead"}},
        X={"labels": {"omni.role": "lead", "omni.pr": "o/r#12"}},
    )
    gh = GitHub(monkeypatch)
    gh.comments.append(comment(100, 12, jobs=(7,)))
    run(og, registry, monkeypatch, 2)
    assert [s for s, _ in relays(og, 12)] == ["X"]


def test_a_wait_on_another_pr_owns_nothing(tmp_path, monkeypatch) -> None:
    registry, og = fleet(tmp_path)
    og.histories["a"][-1] = ending("STILL WORKING: waiting on the queue\nWaits: o/r#13", 8000)
    gh = GitHub(monkeypatch)
    gh.comments.append(comment(100, 12, jobs=(7,)))
    run(og, registry, monkeypatch, 2)
    assert not [s for s, _ in relays(og, 12) if s == "a"]
    assert len(acts(og, "o/r#12")) == 1


def test_a_failed_checks_dequeue_with_no_test_found_is_a_rejection() -> None:
    memo = queue((1, 1, "merged", ()), (2, 2, "dequeued", ()))
    assert omnigent_agent.queue_counts(memo["entries"])["rejected"] == ["2"]


@pytest.mark.parametrize(
    "kind, session, action",
    [
        ("dequeued", "L", "relay"),
        ("dequeued", "", "act"),
        ("queue-rejects", "", "act"),
        ("queue-flake", "", "act"),
    ],
)
def test_queue_events_route_by_rule(kind, session, action) -> None:
    def ask():
        raise AssertionError("no model call")

    event = {"kind": kind, "session": session, "last": "", "line": "x"}
    assert omnigent_agent.route(event, ask)[:2] == ("rule", action)
