"""A PR carrying ``loop:hold`` is parked on purpose (#2164): the supervisor raises no stall and tells no dequeue of it
for a manager to fix, and both resume once the label is off (#2316)."""

import json

import pytest
from test_omnigent_agent import (  # noqa: F401  (the fixtures apply here by name)
    no_model,
    no_sleep,
    this_repo,
    trusted_folders,
)
from test_omnigent_pr_stall import HOUR, omnigent_agent, pr, stalls
from test_omnigent_queue_rejects import GitHub, acts, comment, fleet, relays, run

HOLD = [{"name": "loop:hold"}]
RED = {
    "checks": [{"name": "qualify", "conclusion": "failure", "completed_at": "2026-01-01T00:00:00Z"}]
}


@pytest.mark.parametrize(
    "extra",
    [
        {},  # no push, verdict, go or queue step
        RED,  # a red required check
        {"mergeable": False, "mergeable_state": "dirty"},  # a conflict
    ],
)
def test_a_held_pr_is_no_stall_and_stalls_as_before_once_released(monkeypatch, extra) -> None:
    memo: dict = {}
    assert stalls(monkeypatch, [pr(12, labels=HOLD, **extra)], memo=memo) == {}
    assert list(stalls(monkeypatch, [pr(12, **extra)], memo=memo)) == [12]  # the label came off


def test_a_pr_stalled_before_its_hold_is_not_told_again_after_it(monkeypatch) -> None:
    memo: dict = {}
    assert list(stalls(monkeypatch, [pr(12)], memo=memo)) == [12]
    assert stalls(monkeypatch, [pr(12, labels=HOLD)], memo=memo) == {}
    assert memo["held"] == [12]


class HeldGitHub(GitHub):
    """Repo o/r's open PRs: #12 as a draft (no stall read), with ``loop:hold`` while ``held``."""

    held = True

    def gh(self, *args: str, timeout: float = 0) -> str:
        path = next((a for a in args if a.startswith("repos/")), "")
        if path.startswith("repos/o/r/pulls?"):
            self.calls.append(args)
            rows = [pr(12, draft=True, labels=HOLD if self.held else [])]
            out = json.dumps(rows) if "page=1" in path else "[]"
            return f'HTTP/2.0 200 OK\nEtag: "{hash(out)}"\n\n{out}'
        return super().gh(*args, timeout=timeout)


def test_the_dequeue_of_a_held_pr_is_told_to_no_one_and_one_after_release_is(
    tmp_path, monkeypatch
) -> None:
    registry, og = fleet(tmp_path)
    og.snaps["a"] = {"labels": {"omni.pr": "o/r#12", "omni.role": "lead"}}
    github_ = HeldGitHub(monkeypatch)
    github_.comments.append(comment(100, 12, jobs=(7,)))
    run(og, registry, monkeypatch, 3)
    assert relays(og, 12) == [] and not acts(og, "o/r#12")  # held: no lead, no manager

    github_.held = False  # the label comes off; the PR is dropped again
    github_.comments.append(comment(101, 12, jobs=(7,), ago=30.0))
    run(og, registry, monkeypatch, 3)
    assert [to for to, _ in relays(og, 12)] == ["a"]


def test_the_hold_label_name_is_the_one_the_queue_rule_drops() -> None:
    assert omnigent_agent.HOLD_LABEL == "loop:hold" and HOUR == 3600.0
