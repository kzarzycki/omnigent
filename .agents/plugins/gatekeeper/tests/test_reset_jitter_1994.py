"""A land --wait that waited on a rate-limit reset resumes up to a few minutes after it, at random, so the lands that
waited on one reset do not all spend the fresh bucket in its first second; the jitter never pushes a wait whose reset
falls inside the land's ceiling past it (#1994)."""

import time

from approvals_harness import approvals

github = approvals.github


def test_a_reset_wait_spreads_over_minutes(monkeypatch) -> None:
    assert github.JITTER >= 120
    monkeypatch.setattr(github, "wait_until", None)
    monkeypatch.setattr(github.random, "randint", lambda low, high: high)
    assert github.jittered(100) == 100 + github.JITTER


def test_the_jitter_fits_the_ceilings_room(monkeypatch) -> None:
    monkeypatch.setattr(github.random, "randint", lambda low, high: high)
    monkeypatch.setattr(github, "wait_until", time.monotonic() + 100 + 50)
    assert 100 <= github.jittered(100) <= 150  # at most the 50 s left, never past the ceiling
    monkeypatch.setattr(github, "wait_until", time.monotonic() + 60)
    assert (
        github.jittered(100) == 100
    )  # a reset past the ceiling gets no jitter; the caller stops on it
