"""Loop pollers spend few GitHub REST calls and sleep through a rate limit (#1537)."""

import json
import subprocess

import pytest
from test_omnigent_agent import omnigent_agent as oa

HOUR = 3600


def test_intervals_pinned():
    assert oa.WAIT_FOR_PR_EVERY == 120.0
    assert oa.HEAD_SECONDS == 300.0


def test_rate_limit_sleeps_to_reset(monkeypatch):
    """A fake 403 whose bucket resets in 90 s makes wait-for sleep ~91 s once, not retry."""
    now = 1_000_000.0
    monkeypatch.setattr(oa.time, "time", lambda: now)

    def run(cmd, **kw):
        if cmd[:3] == ["gh", "api", "rate_limit"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"0 {int(now) + 90}\n")
        raise subprocess.CalledProcessError(1, cmd, stderr="gh: API rate limit exceeded (HTTP 403)")

    monkeypatch.setattr(oa.subprocess, "run", run)
    with pytest.raises(oa.RateLimited) as e:
        oa.gh("api", "x")
    assert e.value.wait == 91.0
    assert oa.rate_wait("HTTP 429 Retry-After: 17") == 17.0
    assert oa.rate_wait("HTTP 404 not found") is None


def test_wait_for_sleeps_instead_of_retrying(monkeypatch, capsys):
    calls, slept = [], []

    def gate(pr, end, since=None):
        calls.append(1)
        if len(calls) == 1:
            raise oa.RateLimited("403", 77.0)
        return 0, "passed"

    monkeypatch.setattr(oa, "gate_outcome", gate)
    monkeypatch.setattr(oa.time, "sleep", slept.append)
    monkeypatch.setattr(oa.random, "uniform", lambda low, high: high)  # the most jitter
    assert oa.wait_for(None, None, 5, "gate", 1000.0) == (0, "passed")
    assert slept == [77.0 + oa.RESET_JITTER] and "rate limit" in capsys.readouterr().err


def test_waiters_on_one_reset_resume_over_minutes_not_in_its_first_second():
    """Every poller that waited on one reset resuming in its first second drains the fresh bucket at once."""
    waits = [oa.RateLimited("403", 90.0).jittered() for _ in range(200)]
    assert all(90.0 <= wait <= 90.0 + oa.RESET_JITTER for wait in waits)
    assert oa.RESET_JITTER >= 120 and max(waits) - min(waits) > 60  # spread over minutes


def test_unchanged_pr_hour_is_bounded(monkeypatch):
    """One simulated hour of an unchanged PR: verdict polls read the PR only; gate polls reuse cached commits."""
    asked = []
    pr = {"head": {"sha": "a" * 40}, "updated_at": "t0"}

    def fake_gh(*args, **kw):
        path = next(a for a in args if a.startswith("repos/"))
        asked.append(path)
        if path.endswith("pulls/5"):
            return json.dumps(pr)
        if "git/commits/" in path:
            return json.dumps({"tree": {"sha": "t"}, "committer": {"date": "2026-01-01T00:00:00Z"}})
        if "matching-refs" in path or "statuses" in path:
            return "[]"
        return ""

    monkeypatch.setattr(oa, "gh", fake_gh)
    monkeypatch.setattr(oa, "COMMITS", {})
    polls = int(HOUR / oa.WAIT_FOR_PR_EVERY)
    seen: dict = {}
    for _ in range(polls):
        assert oa.verdict_outcome(5, oa.time.monotonic() + 30, None, seen) is None
    verdict_calls = len(asked)
    asked.clear()
    for _ in range(polls):
        assert oa.gate_outcome(5, oa.time.monotonic() + 30) is None
    # was 4+ calls every 30 s (480+/h); now at most 1 per poll for verdict (+ one full look), 3 for gate
    assert verdict_calls <= polls + 4
    assert len(asked) <= 3 * polls + 1


def test_head_read_uses_shared_cache(monkeypatch):
    seen = []
    monkeypatch.setattr(oa, "gh", lambda *a, **k: seen.append(a) or "b" * 40)
    oa.default_head("o/r")
    assert "--cache" in seen[0] and "300s" in seen[0]


def test_a_secondary_limit_waits_a_minute_not_to_the_core_reset():
    assert oa.rate_wait("You have exceeded a secondary rate limit (HTTP 403)") == 60.0


def test_wait_for_passes_its_memo_to_the_verdict_poll(monkeypatch):
    """Through wait_for itself: an unchanged PR costs one pulls read per poll, four polls in all."""
    asked = []
    monkeypatch.setattr(oa, "COMMITS", {})
    monkeypatch.setattr(oa, "WAIT_FOR_PR_EVERY", 0.0)
    monkeypatch.setattr(oa.time, "sleep", lambda s: None)

    def fake_gh(*args, **kw):
        path = args[2] if args[1] == "--paginate" else args[1]
        asked.append(path)
        if path.endswith("pulls/5"):
            return json.dumps({"head": {"sha": "a" * 40}, "updated_at": "t0"})
        if "git/commits/" in path:
            return json.dumps({"committer": {"date": "2026-01-01T00:00:00Z"}})
        return ""

    monkeypatch.setattr(oa, "gh", fake_gh)
    polls = []
    real = oa.verdict_outcome

    def counted(*a, **k):
        polls.append(1)
        if len(polls) == 4:
            return 0, "done"
        return real(*a, **k)

    monkeypatch.setattr(oa, "verdict_outcome", counted)
    assert oa.wait_for(None, None, 5, "verdict", 100.0) == (0, "done")
    assert (
        len(asked) <= 3 + 3
    )  # the first look reads the PR, its commit and the lists; the rest only the PR


def test_supervise_asks_no_repo_before_the_limit_resets(monkeypatch, tmp_path):
    """After one RateLimited head read, no repo is read until its reset, however many polls pass."""
    import test_omnigent_agent as t

    reads = []

    def gh(*args, timeout=0):
        reads.append(args)
        raise oa.RateLimited("403", 600.0)

    monkeypatch.setattr(oa, "HEAD_SECONDS", 0)
    monkeypatch.setattr(oa, "gh", gh)
    registry = t.following(tmp_path)
    og = t.Lead(t.told("brief", 60))
    t.in_checkout(og)
    t.supervised(og, registry, monkeypatch, 3, notify="cos", console=True)
    assert len(reads) == 1


def test_supervise_reads_the_default_branch_rarely_over_an_hour(monkeypatch, tmp_path):
    """A fake clock runs one hour of supervise polls on an unmoved branch; the head reads stay at 12 or fewer
    (one per HEAD_SECONDS). land --wait's loop honours QUEUE_POLL: test_approvals_rate_limit_1266."""
    import test_omnigent_agent as t

    now, reads = [0.0], []
    monkeypatch.setattr(oa.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(oa, "HEAD_SECONDS", 300.0)

    def gh(*args, timeout=0):
        reads.append(args)
        return "b" * 40 + "\n"

    monkeypatch.setattr(oa, "gh", gh)
    registry = t.following(tmp_path)
    og = t.Lead(t.told("brief", 60))
    t.in_checkout(og)
    step = 60.0  # a poll every minute of fake time: an hour in 60 polls

    def tick(_poll):
        now[0] += step

    t.supervised(og, registry, monkeypatch, int(HOUR / step) + 1, tick, notify="cos", console=True)
    heads = [r for r in reads if "repos/o/r/commits/HEAD" in r]
    assert 1 <= len(heads) <= 13
    # beside them each PR poll (once per PR_STALL_POLL) lists the repo's open PRs (#1819) and its queue comments (#2085)
    polls = HOUR / oa.PR_STALL_POLL + 1
    assert len([r for r in reads if any("issues/comments" in a for a in r)]) <= polls
    assert len(reads) - len(heads) <= 2 * polls


@pytest.mark.parametrize(
    ("stderr", "bucket_answer", "want"),
    [
        (
            "secondary rate limit (HTTP 403)",
            "0 9999999999",
            60.0,
        ),  # a core bucket far from reset is not used
        (
            "GraphQL: API rate limit exceeded",
            "graphql",
            91.0,
        ),  # the GraphQL bucket's reset, not core's
        (
            "API rate limit exceeded (HTTP 403)",
            "5 9999999999",
            60.0,
        ),  # calls left: the per-minute limit
    ],
)
def test_rate_wait_branches(monkeypatch, stderr, bucket_answer, want):
    now = 1_000_000.0
    monkeypatch.setattr(oa.time, "time", lambda: now)

    def run(cmd, **kw):
        jq = cmd[-1]
        if bucket_answer == "graphql":  # only the graphql bucket is empty and resets soon
            out = f"0 {int(now) + 90}" if ".graphql" in jq else "5 9999999999"
        else:
            out = bucket_answer
        return subprocess.CompletedProcess(cmd, 0, stdout=out)

    monkeypatch.setattr(oa.subprocess, "run", run)
    assert oa.rate_wait(stderr) == want


def test_supervise_holds_its_head_reads_for_the_jittered_wait(monkeypatch, tmp_path):
    """A limit that resets now (wait 0) still holds the reads for the jitter: the hold is wait + jitter, not wait."""
    import test_omnigent_agent as t

    reads = []

    def gh(*args, timeout=0):
        reads.append(args)
        raise oa.RateLimited("403", 0.0)

    monkeypatch.setattr(oa, "HEAD_SECONDS", 0)
    monkeypatch.setattr(oa, "gh", gh)
    monkeypatch.setattr(oa.random, "uniform", lambda low, high: 3600.0)
    registry = t.following(tmp_path)
    og = t.Lead(t.told("brief", 60))
    t.in_checkout(og)
    t.supervised(og, registry, monkeypatch, 3, notify="cos", console=True)
    assert len(reads) == 1


def test_supervise_holds_its_open_pr_reads_for_the_jittered_wait(monkeypatch, tmp_path):
    """The open-PR sweep's hold is wait + jitter too, so a limit that resets now still holds it."""
    import test_omnigent_agent as t

    sweeps = []

    def stalls(repo, after, seen):
        sweeps.append(repo)
        raise oa.RateLimited("403", 0.0)

    monkeypatch.setattr(oa, "HEAD_SECONDS", 3600)
    monkeypatch.setattr(oa, "PR_STALL_POLL", 0)
    monkeypatch.setattr(oa, "default_head", lambda repo: "a" * 40)
    monkeypatch.setattr(oa, "pr_stalls", stalls)
    monkeypatch.setattr(oa.random, "uniform", lambda low, high: 3600.0)
    registry = t.following(tmp_path)
    og = t.Lead(t.told("brief", 60))
    t.in_checkout(og)
    t.supervised(og, registry, monkeypatch, 3, notify="cos", console=True)
    assert len(sweeps) == 1
