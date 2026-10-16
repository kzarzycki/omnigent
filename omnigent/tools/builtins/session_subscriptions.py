"""Tools for subscribing the calling session to other sessions' turn ends."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool

_SESSION_ID_PARAMETERS = {
    "type": "object",
    "properties": {
        "session_id": {
            "type": "string",
            "description": "Id of the session to watch, e.g. from sys_session_list.",
            "minLength": 1,
        }
    },
    "required": ["session_id"],
    "additionalProperties": False,
}


class SysSessionSubscribeTool(Tool):
    """Schema-only tool that subscribes the calling session to another session."""

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "sys_session_subscribe"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return (
            "Watch another session. From now on you get a one-line [System: ...] message "
            "whenever it ends a turn (completed, cancelled or failed) or blocks on a "
            "prompt; a prompt it is already blocked on is reported at once. Read what it "
            "did with sys_session_get_history. Subscribing twice is a no-op; the watched "
            "session is not changed. A session that already watches you cannot be "
            "watched back."
        )

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI-format schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": _SESSION_ID_PARAMETERS,
            },
        }


class SysSessionUnsubscribeTool(Tool):
    """Schema-only tool that stops the calling session watching another session."""

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "sys_session_unsubscribe"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return "Stop watching a session you subscribed to with sys_session_subscribe."

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI-format schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": _SESSION_ID_PARAMETERS,
            },
        }


class SysSessionSubscriptionsTool(Tool):
    """Schema-only tool that lists the sessions the calling session watches."""

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "sys_session_subscriptions"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return "List the sessions you watch (subscribed to with sys_session_subscribe)."

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI-format schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        }
