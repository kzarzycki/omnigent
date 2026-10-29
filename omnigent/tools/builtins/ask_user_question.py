"""Tool that asks the user a question without holding the turn open."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool

# Prefix of a question's id; the elicitation it publishes carries the same id.
QUESTION_ID_PREFIX = "question_"
# Prefix of a harness's parked AskUserQuestion card. Declining it answers the
# question, so the runner delivers the deny without interrupting the turn.
ASK_USER_QUESTION_ELICITATION_PREFIX = "elicit_question_"


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


def structured_ask_user_question(
    tool_input: Any,
) -> dict[str, Any] | None:
    """
    Build a structured AskUserQuestion payload for the elicitation
    params extras.

    Claude's PermissionRequest payload includes the full tool_input
    when the gated tool is AskUserQuestion. Rather than relying on
    the (truncated) ``content_preview`` JSON-string, we extract the
    questions + options here and ship them as a typed structure the
    UI consumes directly.

    The returned shape is the same one the UI's
    :file:`@/lib/askUserQuestion.ts` produces from its preview
    parser — so the front-end can treat both sources uniformly.

    :param tool_input: The ``tool_input`` field from the
        PermissionRequest payload.
    :returns: ``{"questions": [...]}`` on success, or ``None`` when
        the input doesn't carry a usable AskUserQuestion shape (no
        questions, malformed options, etc.) — caller falls back to
        the binary preview-only render.
    """
    if not isinstance(tool_input, dict):
        return None
    questions_raw = tool_input.get("questions")
    if not isinstance(questions_raw, list) or not questions_raw:
        return None
    questions: list[dict[str, Any]] = []
    for entry in questions_raw:
        if not isinstance(entry, dict):
            continue
        question_text = entry.get("question")
        if not isinstance(question_text, str) or not question_text:
            continue
        options_raw = entry.get("options")
        if not isinstance(options_raw, list):
            continue
        options: list[dict[str, Any]] = []
        for opt in options_raw:
            if isinstance(opt, dict):
                label = opt.get("label")
                if not isinstance(label, str) or not label:
                    continue
                option: dict[str, Any] = {"label": label}
                description = opt.get("description")
                if isinstance(description, str) and description:
                    option["description"] = description
                # ``preview`` is an optional richer snippet some
                # Claude builds attach to an option (rendered as a
                # <pre> below the option list when selected). Ride
                # it through verbatim so the UI can surface it.
                preview = opt.get("preview")
                if isinstance(preview, str) and preview:
                    option["preview"] = preview
                options.append(option)
            elif isinstance(opt, str) and opt:
                options.append({"label": opt})
        if not options:
            continue
        question: dict[str, Any] = {
            "question": question_text,
            "options": options,
            "multiSelect": entry.get("multiSelect") is True,
        }
        header = entry.get("header")
        if isinstance(header, str) and header:
            question["header"] = header
        questions.append(question)
    if not questions:
        return None
    return {"questions": questions}
