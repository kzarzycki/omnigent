"""approvals.py: `approve merge` and `land` where GitHub refuses the commit-status write (#1816).

A cloud session's GitHub proxy refuses `POST statuses/<sha>` with a 403 while comments, labels and the merge pass. The
go then rests on its `Approved: merge` record, which go_on reads on a head with no `owner` status when a writer wrote
it, and `land` under `land.queue: mergify` hands over with `loop:queued` and a `Queued for Mergify` record naming the
head, which the land-status workflow turns into the `land` status. Driven through main() against the fixtures of
test_approvals_mergify_queue.py, on a PR touching `.github/**`, which the fixture's risk rule makes wait for a go on its
head.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from approvals_harness import add, approvals, git_in, lands, verdict_on  # noqa: F401
from test_approvals_cloud_queue import cloud_queue  # noqa: F401
from test_approvals_github_queue import labels_of, notes_on, queue, run  # noqa: F401
from test_approvals_mergify_handover import risky
from test_approvals_mergify_queue import land_statuses, mergify  # noqa: F401

# what `gh` prints when a cloud session's proxy refuses the status write
PROXY_403 = (
    "gh: Write access to this GitHub API path is not permitted through this proxy. (HTTP 403)"
)


@pytest.fixture
def refused(
    mergify: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """`mergify` with `loop.delegate: all`, where every status write is refused as the proxy refuses it; `tried` holds
    each (head, context) attempted, and `hub` stays empty."""
    loop = mergify["work"] / "docs" / "agents" / "loop.md"
    loop.write_text(add(loop.read_text(), "loop", "delegate: all\n"))
    tried: list[tuple[str, str]] = []

    def post(head: str, context: str, description: str, state: str = "success") -> None:
        tried.append((head, context))
        raise subprocess.CalledProcessError(1, "gh", stderr=PROXY_403)

    monkeypatch.setattr(approvals, "post_status", post)
    mergify["tried"] = tried
    return mergify


def delegate(number: int, head: str, *more: str) -> int:
    return approvals.main(
        ["approve", "merge", str(number), "--head", head, "--by", "delegate", *more]
    )


def handed(state: dict[str, Any], number: int) -> list[str]:
    return notes_on(state, number, approvals.HANDED)


def push(state: dict[str, Any], number: int, name: str) -> str:
    """A commit on PR #number's head writing `name`, pushed as its new head with a satisfied verdict on it."""
    work, pull = state["work"], state["pulls"][number]
    git_in(work, "switch", "-q", "--detach", pull["headRefOid"])
    (work / name).parent.mkdir(parents=True, exist_ok=True)
    (work / name).write_text("changed\n")
    git_in(work, "add", name)
    git_in(work, "commit", "-q", "-m", f"PR {number} again")
    head = git_in(work, "rev-parse", "HEAD").strip()
    git_in(
        work,
        "push",
        "-q",
        "-f",
        "origin",
        f"{head}:refs/heads/pr{number}",
        f"{head}:refs/pull/{number}/head",
    )
    pull["headRefOid"] = head
    pull["reviews"]["nodes"].append(verdict_on(head))
    return head


def test_a_refused_owner_status_leaves_the_record_as_the_go(
    refused: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The delegate's go with the `owner` status refused: the record names the head, `check merge` reads the go from
    it, and the hand-over that follows queues the PR by label and record, with no status written anywhere."""
    head = risky(refused, 7)
    assert delegate(7, head) == 0
    out = capsys.readouterr().out
    assert f"the `owner` status on {head} was not written, GitHub refused it" in out
    assert f"PR #7: the delegate's go is on {head}" in out
    assert (head, "owner") in refused["tried"] and hub == {}
    # the go is read from the record alone
    assert approvals.main(["check", "merge", "7"]) == 0
    assert f"PR #7: the go on {head} is the delegate's" in capsys.readouterr().out
    # and the hand-over went through on the record
    assert "loop:queued" in labels_of(refused, 7)
    [record] = handed(refused, 7)
    assert approvals.fields(record)["head"] == head


def test_land_hands_over_by_record_when_the_status_is_refused(
    refused: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`land` under Mergify with the `land` status refused: `loop:queued` and one hand-over record pinned to the head;
    a second land before the workflow posts the status adds no second record."""
    head = refused["opened"](7)
    code, lines = refused["land"]("7")
    assert code == 0, lines
    assert lines[-1] == f"PR #7 queued for Mergify at {head}"
    assert (head, "land") in refused["tried"] and land_statuses(hub, head) == []
    assert "loop:queued" in labels_of(refused, 7)
    [record] = handed(refused, 7)
    assert record.splitlines()[0] == approvals.HANDED
    assert approvals.fields(record)["head"] == head
    code, lines = refused["land"]("7")
    assert code == 0, lines
    assert any(
        line.startswith(f"PR #7: its hand-over record for {head} is already on the PR")
        and "land-status workflow" in line
        for line in lines
    ), lines
    assert len(handed(refused, 7)) == 1


def test_a_record_go_on_a_stale_head_is_refused(
    refused: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """A go recorded on one head carries nothing to a head with a change of its own: `check merge` and `land` wait for
    a go on the new head, and nothing is handed over for it."""
    old = risky(refused, 7)
    assert delegate(7, old) == 0
    refused["pulls"][7]["labels"]["nodes"] = [
        each for each in refused["pulls"][7]["labels"]["nodes"] if each["name"] != "loop:queued"
    ]
    new = push(refused, 7, ".github/y.txt")
    capsys.readouterr()
    assert approvals.main(["check", "merge", "7"]) == 3
    out = capsys.readouterr().out
    assert "waits for the owner's go on its head" in out and f"--head {new}" in out
    code, lines = refused["land"]("7")
    assert code == 3, lines
    assert "loop:queued" not in labels_of(refused, 7)
    assert [approvals.fields(each)["head"] for each in handed(refused, 7)] == [old]
    # the go is refused on the old head too, since the PR's head moved
    assert delegate(7, old) == 1


def record_note(head: str, login: str, marked: bool = True) -> dict[str, Any]:
    """The delegate's merge record on `head` as a comment by `login`, with the `Status: refused` line when `marked`."""
    lines = f"Approved: merge\nBy: delegate\nHead: {head}" + (
        f"\n{approvals.UNSTATUSED}" if marked else ""
    )
    return {
        "id": f"IC_{login}_{marked}",
        "author": {"login": login},
        "body": approvals.fold(approvals.VOICED["delegate"], f"Go to merge {head[:7]}.", lines),
        "createdAt": "2026-10-01T09:00:00Z",
        "lastEditedAt": None,
        "isMinimized": False,
        "viewerCanMinimize": False,
    }


def test_a_record_by_a_stranger_is_no_go(
    refused: dict[str, Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A comment is anyone's to write on a public repo: a marked merge record by an account that can't write is no
    go, and the same record by a writer is."""
    monkeypatch.setattr(approvals, "writer", lambda login: login in {"loop", "colleague"})
    head = risky(refused, 7)
    nodes = refused["pulls"][7]["comments"]["nodes"]
    nodes.append(record_note(head, "stranger"))
    capsys.readouterr()
    assert approvals.main(["check", "merge", "7"]) == 3
    assert "waits for the owner's go on its head" in capsys.readouterr().out
    nodes.append(record_note(head, "colleague"))
    assert approvals.main(["check", "merge", "7"]) == 0
    assert f"PR #7: the go on {head} is the delegate's" in capsys.readouterr().out


def test_a_record_without_the_refused_line_is_no_go_alone(
    refused: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """A record as a host writes it beside its status, by the account running the check, is no go with no status."""
    head = risky(refused, 7)
    refused["pulls"][7]["comments"]["nodes"].append(record_note(head, "loop", marked=False))
    capsys.readouterr()
    assert approvals.main(["check", "merge", "7"]) == 3
    assert "waits for the owner's go on its head" in capsys.readouterr().out


def test_a_record_only_delegate_go_ends_with_the_delegation(
    refused: dict[str, Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Into the default branch the delegate's record-only go counts only while `loop.delegate: all` holds, as its
    status would."""
    head = risky(refused, 7)
    assert delegate(7, head) == 0
    assert approvals.main(["check", "merge", "7"]) == 0
    monkeypatch.setattr(approvals, "delegation", lambda _policy: "feature")
    capsys.readouterr()
    assert approvals.main(["check", "merge", "7"]) == 3
    assert "waits for the owner's go on its head" in capsys.readouterr().out


def test_a_refused_withdrawal_takes_back_a_go_its_status_gave(
    refused: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A go given where statuses could be written (its `owner` success on the head), withdrawn from a cloud session
    whose status write is refused: the `Withdrawn: merge` record, newer than the status, takes the go back."""
    head = risky(refused, 7)
    assert delegate(7, head) == 0
    hub[head] = [
        {
            "context": "owner",
            "state": "success",
            "description": f"The delegate's go on {head[:12]}{approvals.DELEGATED}",
            "creator": {"login": "loop"},
            "created_at": "2026-09-30T09:00:00Z",  # before the fake's comments
        }
    ]
    assert approvals.main(["check", "merge", "7"]) == 0
    assert delegate(7, head, "--withdraw") == 0
    capsys.readouterr()
    assert approvals.main(["check", "merge", "7"]) == 3
    assert "waits for the owner's go on its head" in capsys.readouterr().out


def test_a_withdrawal_record_takes_the_go_back(
    refused: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """With the statuses refused, the newer `Withdrawn: merge` record on the head withdraws the go its record gave."""
    head = risky(refused, 7)
    assert delegate(7, head) == 0
    assert approvals.main(["check", "merge", "7"]) == 0
    assert delegate(7, head, "--withdraw") == 0
    assert (head, "owner") in refused["tried"][1:]
    capsys.readouterr()
    assert approvals.main(["check", "merge", "7"]) == 3
    assert "waits for the owner's go on its head" in capsys.readouterr().out


def test_a_status_on_the_head_still_decides_alone(
    refused: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An `owner` status written where statuses could be (a host) decides over every record older than it: a failing
    one withdraws the go the record gave."""
    head = risky(refused, 7)
    assert delegate(7, head) == 0
    hub[head] = [
        {
            "context": "owner",
            "state": "failure",
            "description": f"The delegate withdrew their go on {head[:12]}",
            "creator": {"login": "loop"},
            "created_at": "2026-10-02T09:00:00Z",  # after the fake's comments
        }
    ]
    capsys.readouterr()
    assert approvals.main(["check", "merge", "7"]) == 3
    assert "waits for the owner's go on its head" in capsys.readouterr().out


def test_an_eviction_with_the_status_refused_still_leaves_the_queue(
    refused: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mergify dequeued a PR the workflow had posted `land` on: the `land` error is refused, and the eviction still
    takes both labels off and turns the PR back to draft, so it leaves the queue and the workflow posts nothing more."""
    head = refused["opened"](7)
    assert refused["land"]("7")[0] == 0
    # the land-status workflow posted `land` from the record
    hub[head] = [
        {
            "context": "land",
            "state": "success",
            "description": f"Queued for Mergify at {head}",
            "creator": {"login": "github-actions[bot]"},
        }
    ]
    refused["pulls"][7]["labels"]["nodes"].append({"name": "loop:dequeued"})
    code, lines = refused["land"]("7")
    assert code == 1, lines
    assert f"PR #7 was dequeued by Mergify at {head}: evicted" in lines
    assert f"the `land` status on {head} was not written, GitHub refused it: {PROXY_403}" in lines
    assert labels_of(refused, 7) & {"loop:queued", "loop:dequeued"} == set()
    assert refused["pulls"][7]["isDraft"] is True


def test_another_status_failure_still_stops(
    mergify: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure that is no refusal (GitHub unreachable) stops the go as before, recording nothing."""

    def down(head: str, context: str, description: str, state: str = "success") -> None:
        raise subprocess.CalledProcessError(1, "gh", stderr="gh: connection reset")

    monkeypatch.setattr(approvals, "post_status", down)
    head = risky(mergify, 7)
    assert approvals.main(["approve", "merge", "7", "--head", head, "--by", "owner"]) != 0
    assert not notes_on(mergify, 7, approvals.VOICED["owner"])


def test_a_land_after_an_eviction_at_the_same_head_hands_over_again(
    refused: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
) -> None:
    """A cloud land of a PR Mergify evicted at an unchanged head (its batch was red for a reason not its own): the
    eviction record ends the first hand-over, so the re-land posts a second record for the land-status workflow, rather
    than leave the head's `land` at the host's `error` with no record to answer (#2006)."""
    head = refused["opened"](7)
    code, lines = refused["land"]("7")
    assert code == 0, lines
    assert len(handed(refused, 7)) == 1
    refused["pulls"][7]["labels"]["nodes"].append({"name": approvals.DEQUEUED})
    hub.setdefault(head, []).insert(0, {"context": "land", "state": "success", "description": ""})
    code, lines = refused["land"]("7")
    assert code == 1, lines  # Mergify dequeued it at the head land queued: evicted
    assert notes_on(refused, 7, approvals.QUEUE_EVICTED)
    code, lines = refused["land"]("7")
    assert lines[0] == "PR #7 is ready now", (
        lines
    )  # `land <pr>` marks it ready again; CI starts on ready
    code, lines = refused["land"]("7")
    assert code == 0, lines
    assert "loop:queued" in labels_of(refused, 7)
    records = handed(refused, 7)
    assert len(records) == 2 and approvals.fields(records[-1])["head"] == head


def test_a_re_land_after_its_own_eviction_is_not_evicted_again(
    refused: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
) -> None:
    """A cloud land evicts a PR Mergify dequeued, its `land` `error` refused, so the head's newest `land` status is still
    the old success; Mergify labels `loop:dequeued` again on that draft. The re-land reads the success as older than its
    eviction, takes the stale label off and hands the head over again, rather than evicting it once more (#1882)."""
    head = refused["opened"](7)
    assert refused["land"]("7")[0] == 0
    refused["pulls"][7]["labels"]["nodes"].append({"name": approvals.DEQUEUED})
    # the land-status workflow's success for the first record, older than the eviction note (WRITTEN)
    old = {
        "context": "land",
        "state": "success",
        "description": "",
        "created_at": "2026-09-01T00:00:00Z",
    }
    hub.setdefault(head, []).insert(0, old)
    code, lines = refused["land"]("7")
    assert code == 1, lines
    assert notes_on(refused, 7, approvals.QUEUE_EVICTED)
    assert refused["land"]("7")[1][0] == "PR #7 is ready now"
    # Mergify labels every draft of a PR it held, land's own eviction too
    refused["pulls"][7]["labels"]["nodes"].append({"name": approvals.DEQUEUED})
    code, lines = refused["land"]("7")
    assert code == 0, lines
    assert lines[-1] == f"PR #7 queued for Mergify at {head}"
    assert len(notes_on(refused, 7, approvals.QUEUE_EVICTED)) == 1
    assert approvals.DEQUEUED not in labels_of(refused, 7)
    assert "loop:queued" in labels_of(refused, 7)
    assert refused["pulls"][7]["isDraft"] is False
    records = handed(refused, 7)
    assert len(records) == 2 and approvals.fields(records[-1])["head"] == head


def test_a_dequeue_after_the_re_land_still_evicts(
    refused: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
) -> None:
    """Once the workflow posts the re-land's success, newer than the eviction, a dequeue by Mergify is real again."""
    head = refused["opened"](7)
    assert refused["land"]("7")[0] == 0
    refused["pulls"][7]["labels"]["nodes"].append({"name": approvals.DEQUEUED})
    old = {
        "context": "land",
        "state": "success",
        "description": "",
        "created_at": "2026-09-01T00:00:00Z",
    }
    hub.setdefault(head, []).insert(0, old)
    assert refused["land"]("7")[0] == 1
    assert refused["land"]("7")[1][0] == "PR #7 is ready now"
    assert refused["land"]("7")[0] == 0
    new = {
        "context": "land",
        "state": "success",
        "description": "",
        "created_at": "2026-11-01T00:00:00Z",
    }
    hub[head].insert(0, new)
    refused["pulls"][7]["labels"]["nodes"].append({"name": approvals.DEQUEUED})
    code, lines = refused["land"]("7")
    assert code == 1, lines
    assert f"PR #7 was dequeued by Mergify at {head}: evicted" in lines
    assert len(notes_on(refused, 7, approvals.QUEUE_EVICTED)) == 2


def test_check_stalled_reads_a_success_older_than_the_eviction_as_not_queued(
    refused: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`check stalled` on a PR carrying `loop:queued` whose head's only `land` success predates land's eviction at that
    head: no hand-over holds it, so it is listed until a newer success comes (#1882)."""
    head = refused["opened"](7)
    assert refused["land"]("7")[0] == 0
    old = {
        "context": "land",
        "state": "success",
        "description": "",
        "created_at": "2026-09-01T00:00:00Z",
    }
    hub.setdefault(head, []).insert(0, old)
    capsys.readouterr()
    assert approvals.main(["check", "stalled"]) == 0
    approvals.comment(7, approvals.fold(approvals.QUEUE_EVICTED, "red", f"Head: {head}"))
    capsys.readouterr()
    assert approvals.main(["check", "stalled"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        f"PR #7 has its go and a satisfied verdict at {head} and is not queued"
    ]
    new = {**old, "created_at": "2026-11-01T00:00:00Z"}
    hub[head].insert(0, new)
    assert approvals.main(["check", "stalled"]) == 0


def test_a_hand_over_after_an_eviction_at_the_same_head_asks_mergify_to_queue_it_once(
    refused: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
) -> None:
    """Mergify takes back a PR it dequeued for failing checks only on `@mergifyio queue` (its lifecycle docs), and
    land's `queue` action does not ask: the hand-over after an eviction at the same head posts the command, once per
    hand-over; a first hand-over posts none (#1882)."""
    head = refused["opened"](7)
    assert refused["land"]("7")[0] == 0
    assert notes_on(refused, 7, approvals.REQUEUE) == []
    refused["pulls"][7]["labels"]["nodes"].append({"name": approvals.DEQUEUED})
    old = {
        "context": "land",
        "state": "success",
        "description": "",
        "created_at": "2026-09-01T00:00:00Z",
    }
    hub.setdefault(head, []).insert(0, old)
    assert refused["land"]("7")[0] == 1
    assert refused["land"]("7")[1][0] == "PR #7 is ready now"
    code, lines = refused["land"]("7")
    assert code == 0, lines
    assert len(notes_on(refused, 7, approvals.REQUEUE)) == 1
    # a land before the workflow's status: the same hand-over, so no second command
    assert refused["land"]("7")[0] == 0
    assert len(notes_on(refused, 7, approvals.REQUEUE)) == 1
