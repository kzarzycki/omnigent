"""approvals.py: `check stalled` judges every base, not only Mergify's, and a PR it can't judge stops no other (#1746).

On a base `land` merges itself, a PR `landable` accepts that is still open was never landed; where GitHub's merge
queue holds the base, one not in that queue is listed. A PR stacked on another PR's branch, whose code differs from
what runs here, gets one `not judged` line and the PRs after it are still judged. Driven through main() against the
fixtures of test_approvals_github_queue.py.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from approvals_harness import approvals, git_in, lands, verdict_on  # noqa: F401
from test_approvals import add
from test_approvals_github_queue import queue  # noqa: F401
from test_approvals_mergify_handover import go, risky


@pytest.fixture
def direct(queue: dict[str, Any]) -> dict[str, Any]:
    """`queue` on a base whose rules require `qualify` and no merge queue, with no Mergify in the policy: `land`
    merges each PR itself."""
    queue["rules"] = approvals.github.Required({"qualify"})
    return queue


def stalled(capsys: pytest.CaptureFixture[str]) -> tuple[int, list[str]]:
    capsys.readouterr()
    code = approvals.main(["check", "stalled"])
    return code, capsys.readouterr().out.splitlines()


def test_a_landable_pr_on_a_direct_land_base_is_listed_until_it_lands(
    direct: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    head = direct["opened"](7)
    code, lines = stalled(capsys)
    assert (code, lines) == (
        1,
        [f"PR #7 has its go and a satisfied verdict at {head} and is not landed"],
    )
    assert direct["land"]("7", "--only")[0] == 0
    assert direct["merged"]() == ["7"]
    assert stalled(capsys) == (0, [])


def test_a_pr_land_would_refuse_is_not_listed(
    direct: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """A required check that is red holds the PR: `land` would not take it, so it is not stalled."""
    direct["opened"](7)
    direct["pulls"][7]["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"][
        0
    ]["conclusion"] = "FAILURE"
    assert stalled(capsys) == (0, [])


def test_a_pr_in_github_s_merge_queue_is_not_listed(
    queue: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    head = queue["opened"](7)
    code, lines = stalled(capsys)
    assert (code, lines) == (
        1,
        [f"PR #7 has its go and a satisfied verdict at {head} and is not in the merge queue"],
    )
    assert queue["land"]("7", "--only")[0] == 0 and queue["enqueued"] == [(7, head)]
    assert stalled(capsys) == (0, [])


def test_a_stacked_pr_is_skipped_with_a_line_and_the_prs_after_it_are_still_judged(
    direct: dict[str, Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#7's base is another PR's branch, whose gatekeeper differs from the one running here: it is not judged, and #8
    and #9 after it are."""
    git_in(direct["work"], "push", "-q", "origin", "origin/main:refs/heads/pr-base")
    direct["opened"](7)
    direct["pulls"][7]["baseRefName"] = "pr-base"
    eight = direct["opened"](8)
    nine = direct["opened"](9)
    stub = approvals.judge_with_base  # the harness's: every base's code is what runs here

    def judge(base: str, fetch: bool = False, rules: bool = True) -> None:
        if base == "pr-base":  # as judge_with_base words it, from base_drift's list
            raise approvals.Drifted(
                "gatekeeper/skills/gatekeeper/scripts/gatekeeper.py differs from origin/pr-base's: a PR's own copy"
                " would judge it; run from the base's code (a checkout of origin/pr-base)",
                base,
            )
        stub(base, fetch, rules)

    monkeypatch.setattr(approvals, "judge_with_base", judge)
    code, lines = stalled(capsys)
    assert code == 1
    assert lines == [
        (
            "PR #7 into pr-base is not judged here: gatekeeper/skills/gatekeeper/scripts/gatekeeper.py differs from"
            " origin/pr-base's: a PR's own copy would judge it; run from the base's code (a checkout of origin/pr-base)"
        ),
        f"PR #8 has its go and a satisfied verdict at {eight} and is not landed",
        f"PR #9 has its go and a satisfied verdict at {nine} and is not landed",
        "1 of 3 open PRs not judged here",
    ]


def test_a_base_that_can_t_be_fetched_skips_its_prs_alone(
    direct: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """A base origin no longer has: each PR into it gets its line, a skip alone exits 0."""
    direct["opened"](7)
    direct["pulls"][7]["baseRefName"] = "gone"
    code, lines = stalled(capsys)
    assert code == 0
    assert len(lines) == 2 and lines[1] == "1 of 1 open PRs not judged here"
    assert lines[0].startswith("PR #7 into gone is not judged here: could not fetch or read gone")


def test_a_refused_pr_read_skips_that_pr_alone(
    direct: dict[str, Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    direct["opened"](7)
    eight = direct["opened"](8)
    pulls = direct["pulls"]

    def read(number: int) -> dict[str, Any]:
        if number == 7:
            raise subprocess.CalledProcessError(1, "gh", stderr="gh: timed out")
        return pulls[number]

    monkeypatch.setattr(approvals, "pull_request", read)
    code, lines = stalled(capsys)
    assert (code, lines) == (
        1,
        [
            "PR #7 into main is not judged here: gh: timed out",
            f"PR #8 has its go and a satisfied verdict at {eight} and is not landed",
            "1 of 2 open PRs not judged here",
        ],
    )


def test_a_base_whose_policy_is_refused_skips_its_prs_alone(
    direct: dict[str, Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`land.queue: mergify` with `land.signed_squash_to` is refused on its base: each PR into it gets the refusal,
    and a PR into another base is still judged."""
    git_in(direct["work"], "push", "-q", "origin", "origin/main:refs/heads/signed")
    direct["opened"](7)
    direct["pulls"][7]["baseRefName"] = "signed"
    eight = direct["opened"](8)
    real = approvals.mergify

    def refused(loop: str | None) -> bool:
        if approvals.POLICY_BASE.get() == "signed":
            raise approvals.Refused(
                "signed's `land.queue: mergify` and `land.signed_squash_to`: set one of them"
            )
        return real(loop)

    monkeypatch.setattr(approvals, "mergify", refused)
    assert stalled(capsys) == (
        1,
        [
            (
                "PR #7 into signed is not judged here: signed's `land.queue: mergify` and `land.signed_squash_to`:"
                " set one of them"
            ),
            f"PR #8 has its go and a satisfied verdict at {eight} and is not landed",
            "1 of 2 open PRs not judged here",
        ],
    )


def idle_for(monkeypatch: pytest.MonkeyPatch, hours: float) -> None:
    """Every PR's head was pushed `hours` ago."""
    at = datetime.now(UTC) - timedelta(hours=hours)
    monkeypatch.setattr(approvals, "head_pushed", lambda _pull: at.strftime("%Y-%m-%dT%H:%M:%SZ"))


def test_a_verified_pr_waiting_on_its_go_past_the_idle_hours_is_listed_with_what_it_misses(
    direct: dict[str, Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A satisfied verdict and no go, in a repo without Mergify: listed once idle past the default hours, naming the
    go; inside them, not listed; once the go is on its head, it is a landable PR (`is not landed`) instead."""
    head = risky(direct, 7)
    idle_for(monkeypatch, 13)
    code, lines = stalled(capsys)
    assert (code, lines) == (
        1,
        [
            (
                f"PR #7 has a satisfied verdict at {head} and no go, gate record or new head for 13h:"
                " missing: go"
            )
        ],
    )
    idle_for(monkeypatch, approvals.STALLED_HOURS - 1)
    assert stalled(capsys) == (0, [])
    assert go(7, head) == 0
    assert stalled(capsys) == (
        1,
        [f"PR #7 has its go and a satisfied verdict at {head} and is not landed"],
    )


def test_the_idle_hours_are_the_policy_s_and_an_unsatisfied_verdict_is_not_listed(
    direct: dict[str, Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    risky(direct, 7)
    unverified = risky(direct, 8, reviews=())
    idle_for(monkeypatch, 13)
    loop = direct["work"] / "docs" / "agents" / "loop.md"
    loop.write_text(add(loop.read_text(), "loop", "stalled_after_hours: 24\n"))
    assert stalled(capsys) == (0, [])
    idle_for(monkeypatch, 25)
    code, lines = stalled(capsys)
    assert code == 1 and [line.split(" at ")[0] for line in lines] == [
        "PR #7 has a satisfied verdict"
    ]
    assert unverified not in "".join(lines)


def test_a_verified_pr_with_no_gate_record_on_its_head_is_listed_missing_the_record(
    direct: dict[str, Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`loop.head_proof` asks a gate record on the head before `land` takes it: none came, so it waits on that."""
    head = direct["opened"](7)
    loop = direct["work"] / "docs" / "agents" / "loop.md"
    loop.write_text(add(loop.read_text(), "loop", "head_proof:\n  status: gate/head\n  env: []\n"))
    idle_for(monkeypatch, 13)
    code, lines = stalled(capsys)
    assert code == 1
    assert lines == [
        f"PR #7 has a satisfied verdict at {head} and no go, gate record or new head for 13h: missing: record"
    ]


def passes_on(
    head: str, *verdicts: tuple[int, str, str], at: str | None = None
) -> tuple[dict[str, Any], ...]:
    """The verifier's reviews on `head`: each (pass, counts, SATISFIED), posted at `at` when given."""
    return tuple(
        {
            **verdict_on(head, counts, satisfied, heading=f"Verifier (claude), pass {number}"),
            **({"submittedAt": at} if at else {}),
        }
        for number, counts, satisfied in verdicts
    )


def reviewed(
    state: dict[str, Any], number: int, *verdicts: tuple[int, str, str], at: str | None = None
) -> str:
    """A PR waiting for its go whose head carries `verdicts` in place of the default satisfied one."""
    head = risky(state, number)
    state["pulls"][number]["reviews"]["nodes"] = list(passes_on(head, *verdicts, at=at))
    return head


OPEN, CLEAN = "0 blocker, 1 major, 0 minor", "0 blocker, 0 major, 0 minor"


def test_an_unsatisfied_verdict_on_the_head_is_not_listed_until_it_is_at_the_cap(
    direct: dict[str, Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A major open on pass 1 is the author's to fix, not an idle hand-back; at the cap it is the lead's call, so it
    is listed, naming the pass."""
    reviewed(direct, 7, (1, OPEN, "no"))
    idle_for(monkeypatch, 13)
    assert stalled(capsys) == (0, [])
    cap = approvals.cap(None)
    head = reviewed(direct, 8, (cap, OPEN, "no"))
    code, lines = stalled(capsys)
    assert code == 1
    assert lines == [
        (
            f"PR #8 has its pass {cap} verdict, at the cap, at {head} and no go, gate record or new head"
            " for 13h: missing: go"
        )
    ]


def test_only_the_newest_pass_on_the_head_counts(
    direct: dict[str, Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A satisfied pass 1 then an unsatisfied pass 2 on the head: the newest says fix it, so it is not listed; a
    satisfied verdict on an earlier commit only says nothing of this head."""
    reviewed(direct, 7, (1, CLEAN, "yes"), (2, OPEN, "no"))
    head = risky(direct, 8)
    direct["pulls"][8]["reviews"]["nodes"] = list(passes_on("e" * 40, (1, CLEAN, "yes")))
    idle_for(monkeypatch, 13)
    assert stalled(capsys) == (0, [])
    assert head not in "".join(stalled(capsys)[1])


def test_the_idle_hours_run_from_the_newest_verdict_when_it_came_after_the_push(
    direct: dict[str, Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A head pushed 13 h ago whose satisfied verdict was posted an hour ago has waited an hour, not 13."""
    hour_ago = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    reviewed(direct, 7, (1, CLEAN, "yes"), at=hour_ago)
    idle_for(monkeypatch, 13)
    assert stalled(capsys) == (0, [])
    long_ago = (datetime.now(UTC) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    direct["pulls"][7]["reviews"]["nodes"][0]["submittedAt"] = long_ago
    # a pass posted an hour ago on an earlier head says nothing of this one: the clock does not restart
    direct["pulls"][7]["reviews"]["nodes"].insert(
        0, *passes_on("e" * 40, (1, CLEAN, "yes"), at=hour_ago)
    )
    code, lines = stalled(capsys)
    assert code == 1 and lines[0].endswith("new head for 6h: missing: go")


def test_a_policy_naming_the_github_queue_on_a_base_without_one_judges_none_of_its_prs(
    direct: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    loop = direct["work"] / "docs" / "agents" / "loop.md"
    loop.write_text(add(loop.read_text(), "land", "queue: github\n"))
    direct["opened"](7)
    code, lines = stalled(capsys)
    assert code == 0 and lines[0].startswith("PR #7 into main is not judged here: "), lines
    assert "`land.queue: github`" in lines[0], lines


def test_a_base_whose_rules_require_github_s_queue_is_judged_by_it_under_mergify(
    queue: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The base's rules win over `land.queue: mergify`, in `check stalled` as in `land`."""
    loop = queue["work"] / "docs" / "agents" / "loop.md"
    loop.write_text(add(loop.read_text(), "land", "queue: mergify\n"))
    head = queue["opened"](7)
    assert stalled(capsys) == (
        1,
        [f"PR #7 has its go and a satisfied verdict at {head} and is not in the merge queue"],
    )
