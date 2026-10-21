"""Tool that keeps the calling session visible after its turn ends."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool

# Session label that stops an archive_when_done scheduled run from being
# archived when it succeeds. The value is the reason the run is worth reading.
KEEP_LABEL_KEY = "omnigent.keep"


class SysSessionKeepTool(Tool):
    """Schema-only tool that labels the calling session to stay visible."""

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "sys_session_keep"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return (
            "Mark this session as worth reading, so it stays visible instead of being "
            "archived when a scheduled run ends. Call it when you found something the "
            "user should look at; give the reason in one line. Calling it again "
            "replaces the reason."
        )

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI-format schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": "Why this session is worth reading, in one line.",
                            "minLength": 1,
                        }
                    },
                    "required": ["reason"],
                    "additionalProperties": False,
                },
            },
        }
