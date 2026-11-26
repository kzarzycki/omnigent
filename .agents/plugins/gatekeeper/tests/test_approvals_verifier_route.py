"""approvals.py: a satisfied verdict from a fallback verifier (Gemini through agy, or same-family) lands a high-risk change; a Codex mid/small tier does not (#1115)."""

from __future__ import annotations

import pytest
from approvals_harness import (
    HEAD,
    add,
    approvals,
    approved,
    lands,  # noqa: F401
    loop_md,
    pull,
    verdict_on,
    writers,  # noqa: F401
)

OWNED = ("approved:merge",)


def gate(family: str, risk: str, monkeypatch: pytest.MonkeyPatch, policy: str = "") -> list[str]:
    monkeypatch.setattr(approvals, "writer", lambda _login: False)
    judged = add(loop_md("spec:\n  auto: true\n"), "risk", f"verifier: true\n{policy}")
    heading = f"Verifier ({family}), pass 1"
    review = verdict_on(HEAD, "0 blocker, 0 major, 0 minor", heading=heading) | {
        "body": f"{heading} on {HEAD[:7]}.\n\nVERDICT: 0 blocker, 0 major, 0 minor\nSATISFIED: yes\nRisk: {risk}\n"
    }
    return approvals.proofs(
        "merge",
        pull(reviews=[review], labels=OWNED),
        [approved(1, "kind:bug", "area:web")],
        judged,
    )


@pytest.mark.parametrize(
    "family",
    ["agy/gemini-4-hydrogen-medium/?", "agy", "gemini-4", "claude, same-family"],
)
@pytest.mark.parametrize("risk", ["low — a fixture", "high — CI permissions"])
def test_a_satisfied_fallback_verdict_lands_at_any_risk(
    monkeypatch: pytest.MonkeyPatch, family: str, risk: str
) -> None:
    assert gate(family, risk, monkeypatch) == []


@pytest.mark.parametrize("tier", ["mid", "small"])
def test_a_lower_codex_tier_is_still_refused_for_high_risk(
    monkeypatch: pytest.MonkeyPatch, tier: str
) -> None:
    assert gate(f"codex/terra-6.1/med {tier}", "low — a fixture", monkeypatch) == []
    assert (
        f"lower tier ({tier})"
        in gate(f"codex/terra-6.1/med {tier}", "high — CI permissions", monkeypatch)[0]
    )


def test_the_dated_override_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(approvals.Refused, match="lower_tier_until"):
        gate("codex/terra-6.1/med", "low", monkeypatch, "lower_tier_until: 2099-01-01\n")


def test_a_codex_family_naming_no_fallback_is_the_default_tier() -> None:
    def said(family: str) -> list[tuple[str, int, dict[str, str]]]:
        return [(HEAD, 1, {"Family": family})]

    assert approvals.lower(said("codex/terra-6.1/med")) is None
    assert approvals.lower(said("agy/gemini-4-argon-high/?")) is None
    assert approvals.lower(said("claude, same-family")) is None
    assert approvals.lower(said("codex/terra-6.1/med mid")) == "mid"
