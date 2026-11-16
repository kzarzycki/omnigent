"""Native startup carries author text and stable guidance, without turn metadata."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.runner.app import (
    ResolvedSpec,
    _native_startup_instructions_from_spec,
)
from omnigent.runner.native.orchestration import _opencode_session_instructions_from_spec
from omnigent.runtime.prompt import ASK_USER_QUESTION_INSTRUCTION, WORKSPACE_IMAGE_INSTRUCTION
from omnigent.spec.types import AgentSpec, ExecutorSpec

# Stable guidance every native startup gets after the author's text.
_STARTUP_GUIDANCE = f"{ASK_USER_QUESTION_INSTRUCTION}\n\n{WORKSPACE_IMAGE_INSTRUCTION}"


def _spec(instructions: str | None) -> AgentSpec:
    """Build a minimal agent spec carrying *instructions*."""
    return AgentSpec(
        spec_version=1,
        name="claude_code",
        instructions=instructions,
        executor=ExecutorSpec(),
    )


@pytest.mark.parametrize(
    ("instructions", "expected"),
    [
        ("Be a concise assistant.", f"Be a concise assistant.\n\n{_STARTUP_GUIDANCE}"),
        (None, _STARTUP_GUIDANCE),
        ("   \n  ", _STARTUP_GUIDANCE),
    ],
    ids=["present", "absent", "whitespace-only"],
)
def test_native_startup_instructions_from_spec(instructions: str | None, expected: str) -> None:
    """Author text is preserved and every startup receives card and image guidance."""
    assert _native_startup_instructions_from_spec(_spec(instructions)) == expected


def test_native_startup_instructions_from_spec_none_spec() -> None:
    """A missing spec still receives stable image guidance."""
    assert _native_startup_instructions_from_spec(None) == _STARTUP_GUIDANCE


@pytest.mark.parametrize("has_spec", [False, True])
def test_opencode_startup_carries_image_guidance(has_spec: bool) -> None:
    """OpenCode's session snapshot carries guidance even without authored text."""
    result = _opencode_session_instructions_from_spec(_spec(None) if has_spec else None)
    assert result is not None
    assert result.count(WORKSPACE_IMAGE_INSTRUCTION) == 1


def test_native_startup_instructions_from_spec_resolved_wrapper() -> None:
    """A ResolvedSpec wrapper unwraps to the same instructions text."""
    wrapped = ResolvedSpec(spec=_spec("Be a concise assistant."), workdir=Path("/tmp"))
    assert _native_startup_instructions_from_spec(wrapped) == (
        f"Be a concise assistant.\n\n{_STARTUP_GUIDANCE}"
    )


def test_native_startup_instructions_from_spec_never_returns_stripped_form() -> None:
    """The original resolved text is preserved verbatim, not the stripped form."""
    padded = "  Keep leading/trailing whitespace exactly.  "
    assert _native_startup_instructions_from_spec(_spec(padded)) == (
        f"{padded}\n\n{_STARTUP_GUIDANCE}"
    )
