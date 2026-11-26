"""approvals.py: a verifier's newest review in a pass replaces its earlier ones there (#1257)."""

from __future__ import annotations

from typing import Any

import pytest
from approvals_harness import (
    HEAD,
    LOOP,
    approvals,
    approved,
    pull,
    verdict_on,
)

UNSATISFIED = f"PR #7: a verifier review on {HEAD} is not satisfied: fix and verify again"


def review(family: str, satisfied: str) -> dict[str, Any]:
    counts = "0 blocker, 0 major, 0 minor"
    return verdict_on(HEAD, counts, satisfied=satisfied, heading=f"Verifier ({family}), pass 2")


def verified(*reviews: dict[str, Any]) -> list[str]:
    return approvals.proofs("merge", pull(reviews=reviews), [approved(1, "area:web")], LOOP)


@pytest.mark.parametrize(
    ("reviews", "lines"),
    [
        # one family: the verdict written after its question was answered stands, whichever effort it names
        (
            [
                review("cc/opus-5.5/med, same-family", "no"),
                review("cc/opus-5.5/?, same-family", "yes"),
            ],
            [],
        ),
        ([review("claude", "no"), review("cc/sonnet/?", "yes")], []),
        ([review("claude", "yes"), review("claude", "no")], [UNSATISFIED]),
        # two families: each one's newest counts, and both must be satisfied
        (
            [review("claude", "no"), review("codex/terra-6.1/med", "yes"), review("claude", "yes")],
            [],
        ),
        (
            [review("claude", "yes"), review("codex/terra-6.1/med", "yes"), review("codex", "no")],
            [UNSATISFIED],
        ),
    ],
)
def test_each_familys_newest_review_in_the_pass_counts(
    reviews: list[dict[str, Any]], lines: list[str]
) -> None:
    assert verified(*reviews) == lines
