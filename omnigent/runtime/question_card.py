"""Names shared by the question-card tool, its answer message and the prompt text."""

from __future__ import annotations

# Name of the tool that posts a question card.
ASK_USER_QUESTION_TOOL_NAME = "sys_ask_user_question"
# Start of each answer line the session gets back: ``[answer to <question id>] ...``.
ANSWER_LINE_PREFIX = "[answer to "
