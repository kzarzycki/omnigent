"""approvals.py: the policy's `loop.short_seconds`, the host short lane's bound, which mutate.py reads (#978)."""

from __future__ import annotations

import pytest
from approvals_harness import (
    approvals,
    gatekeeper_md,
)


def test_short_seconds_is_a_whole_number_of_the_loop_section() -> None:
    policy = approvals.Policy(gatekeeper_md("loop:\n  short_seconds: 30\n"))
    assert approvals.settings(policy) == {"short_seconds": 30}


def test_short_seconds_that_is_not_a_whole_number_is_refused_naming_the_key() -> None:
    policy = approvals.Policy(gatekeeper_md("loop:\n  short_seconds: fast\n"))
    with pytest.raises(approvals.Refused, match="short_seconds"):
        approvals.settings(policy)
