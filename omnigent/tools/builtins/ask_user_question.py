"""Tool that asks the user a question without holding the turn open."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool

# Prefix of a question's id; the elicitation it publishes carries the same id.
QUESTION_ID_PREFIX = "question_"


class SysAskUserQuestionTool(Tool):
    """Schema-only tool that posts a question card and returns at once."""

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "sys_ask_user_question"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return (
            "Ask the user one to four multiple-choice questions without waiting. "
            "It returns at once with a question_id; the user sees a card with your "
            "options and may also type their own answer. The answer arrives later as "
            "a user message starting with `[answer to <question_id>]`, one line per "
            "question. Do not wait or poll for it: carry on with other work, or end "
            "your turn if you cannot continue without the answer. Messages from other "
            "sessions keep reaching you while the question is open."
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
                        "questions": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 4,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "question": {
                                        "type": "string",
                                        "description": "The full question, ending with '?'.",
                                        "minLength": 1,
                                    },
                                    "header": {
                                        "type": "string",
                                        "description": "A short label shown as a chip.",
                                    },
                                    "options": {
                                        "type": "array",
                                        "minItems": 2,
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "label": {"type": "string", "minLength": 1},
                                                "description": {"type": "string"},
                                            },
                                            "required": ["label"],
                                            "additionalProperties": False,
                                        },
                                    },
                                    "multiSelect": {
                                        "type": "boolean",
                                        "description": "Allow choosing several options.",
                                    },
                                },
                                "required": ["question", "options"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["questions"],
                    "additionalProperties": False,
                },
            },
        }
