"""Create-time readiness reads a host's per-agent ACP key, not the shared ``acp`` key."""

import pytest

from omnigent.harness_availability import harness_launch_availability

_OTHER_AGENT_ONLY = {"acp": True, "acp:other": True}


def test_acp_agent_missing_from_per_agent_report_is_unavailable() -> None:
    available, reason = harness_launch_availability("acp:claude-cloud", _OTHER_AGENT_ONLY)

    assert available is False
    assert reason == "unconfigured"


@pytest.mark.parametrize(
    ("readiness", "expected"),
    [
        ({**_OTHER_AGENT_ONLY, "acp:claude-cloud": True}, (True, None)),
        ({"acp": True}, (True, None)),
        ({"acp": False}, (False, "unconfigured")),
    ],
)
def test_acp_agent_ready_or_legacy_report(
    readiness: dict[str, object], expected: tuple[bool | None, str | None]
) -> None:
    assert harness_launch_availability("acp:claude-cloud", readiness) == expected
