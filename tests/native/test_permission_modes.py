"""Ordering and launch args of permission modes a created session may request."""

from __future__ import annotations

import pytest

from omnigent.native.permission_modes import (
    APPROVAL_SKIPPING_PERMISSION_MODES,
    PERMISSION_LAUNCH_ARGS,
    PERMISSION_MODE_STRICTNESS,
    check_permission_mode_not_looser,
    launch_args_for_mode,
    mode_for_launch_args,
    permission_mode_exceeds_default,
)


def test_every_launch_mode_has_a_strictness_position() -> None:
    for harness, modes in PERMISSION_LAUNCH_ARGS.items():
        assert set(modes) == set(PERMISSION_MODE_STRICTNESS[harness])


@pytest.mark.parametrize("harness", list(PERMISSION_MODE_STRICTNESS))
def test_skipping_modes_rank_looser_than_every_asking_mode(harness: str) -> None:
    order = PERMISSION_MODE_STRICTNESS[harness]
    skipping = [i for i, mode in enumerate(order) if mode in APPROVAL_SKIPPING_PERMISSION_MODES]
    asking = [i for i, mode in enumerate(order) if mode not in APPROVAL_SKIPPING_PERMISSION_MODES]
    assert min(skipping) > max(asking)


@pytest.mark.parametrize(
    ("harness", "mode", "default"),
    [
        ("claude-native", "plan", None),
        ("claude-native", "acceptEdits", "acceptEdits"),
        ("claude-native", "default", "bypassPermissions"),
        ("claude-native", "bypassPermissions", "bypassPermissions"),
        ("codex-native", "full-access", "bypass"),
        ("acp", "auto", "bypassPermissions"),
        ("acp", "bypassPermissions", "bypassPermissions"),
    ],
)
def test_equal_or_stricter_modes_are_accepted(
    harness: str, mode: str, default: str | None
) -> None:
    check_permission_mode_not_looser(harness, mode, default)


@pytest.mark.parametrize(
    ("harness", "mode", "default", "reason"),
    [
        ("claude-native", "bypassPermissions", None, "declares no default that skips it"),
        ("claude-native", "bypassPermissions", "acceptEdits", "looser than this agent's default"),
        ("codex-native", "bypass", "full-access", "looser than this agent's default"),
        ("acp", "bypassPermissions", "auto", "looser than this agent's default"),
        ("acp", "bypassPermissions", None, "skips approval"),
    ],
)
def test_looser_modes_are_refused_with_a_reason(
    harness: str, mode: str, default: str | None, reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        check_permission_mode_not_looser(harness, mode, default)


def test_refusal_names_the_harness_choices() -> None:
    with pytest.raises(ValueError) as error:
        check_permission_mode_not_looser("codex-native", "auto", None)
    message = str(error.value)
    assert "is not a mode of 'codex-native'" in message
    assert "'approve-for-me'" in message
    assert "'full-access'" not in message


@pytest.mark.parametrize("harness", ["claude-sdk", None])
def test_harness_without_modes_is_refused(harness: str | None) -> None:
    with pytest.raises(ValueError, match="omit it"):
        check_permission_mode_not_looser(harness, "default", None)


@pytest.mark.parametrize(
    ("harness", "args", "mode"),
    [
        ("codex-native", None, "default"),
        ("codex-native", ["--dangerously-bypass-approvals-and-sandbox"], "bypass"),
        ("codex-native", PERMISSION_LAUNCH_ARGS["codex-native"]["read-only"], "read-only"),
        ("claude-native", ["--permission-mode", "plan"], "plan"),
        ("cursor-native", ["--yolo"], "yolo"),
        ("claude-sdk", ["--yolo"], None),
    ],
)
def test_launch_args_map_back_to_their_mode(harness: str, args: list[str] | None, mode) -> None:
    assert mode_for_launch_args(harness, args) == mode


def test_a_mode_with_no_table_args_launches_with_its_bypass_flag() -> None:
    assert launch_args_for_mode("codex-native", "bypass") == [
        "--dangerously-bypass-approvals-and-sandbox"
    ]
    assert launch_args_for_mode("codex-native", "default") is None


@pytest.mark.parametrize(
    ("harness", "mode", "default", "exceeds"),
    [
        ("claude-native", None, "plan", False),
        ("claude-native", "bypassPermissions", None, True),
        ("claude-native", "acceptEdits", None, False),
        ("cursor-native", "yolo", None, True),
        ("claude-native", "bypassPermissions", "acceptEdits", True),
        ("claude-native", "plan", "acceptEdits", False),
        ("codex-native", "bypass", "bypass", False),
    ],
)
def test_a_mode_exceeds_the_default_when_looser_or_skipping_without_one(
    harness: str, mode: str | None, default: str | None, exceeds: bool
) -> None:
    assert permission_mode_exceeds_default(harness, mode, default) is exceeds
