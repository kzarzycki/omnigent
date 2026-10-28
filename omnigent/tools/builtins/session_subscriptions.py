"""Tools for subscribing the calling session to other sessions' turn ends."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool

_SESSION_ID_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": "Id of the session to watch, e.g. from sys_session_list.",
    "minLength": 1,
}

_SESSION_ID_PARAMETERS = {
    "type": "object",
    "properties": {"session_id": _SESSION_ID_PROPERTY},
    "required": ["session_id"],
    "additionalProperties": False,
}

_SUBSCRIBE_PARAMETERS = {
    **_SESSION_ID_PARAMETERS,
    "properties": {
        "session_id": _SESSION_ID_PROPERTY,
        "quiet_seconds": {
            "type": "integer",
            "description": (
                "Optional quiet period, e.g. 300: hold each completed turn end that long "
                "and report it only once the session and its sub-agents have stopped "
                "running, so a busy session wakes you once per settled stretch. 0 or "
                "absent reports every turn end at once."
            ),
            "minimum": 0,
            "maximum": 86400,
        },
    },
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
            "did with sys_session_get_history. With quiet_seconds, a completed turn end "
            "is held and replaced by a newer one; failures, cancels, prompts and a reply "
            "with a line starting DONE, USER NEEDED or PARKED are still reported at once. "
            "Subscribing again only sets quiet_seconds; the watched session is not "
            "changed. A session that already watches you cannot be watched back."
        )

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI-format schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": _SUBSCRIBE_PARAMETERS,
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
        return (
            "List the sessions you watch (subscribed to with sys_session_subscribe), "
            "100 per page. When has_more is true, call again with after=last_id."
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
                        "after": {
                            "type": "string",
                            "description": "last_id of the previous page, to read the next one.",
                        }
                    },
                    "additionalProperties": False,
                },
            },
        }
