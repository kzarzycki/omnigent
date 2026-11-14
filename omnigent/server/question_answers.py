"""Answers to ``sys_ask_user_question`` cards, kept for the asking session to read back.

The answer also reaches the session as a user message, but any session that can
message it can type the same ``[answer to <id>]`` text. Only the server records an
entry here, when the session's user answers the card, so ``sys_read_answer`` is the
proof that an answer came through the card.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass

# Enough for every open question on a busy server; the oldest answer goes first.
_MAX_ANSWERS = 2048


@dataclass(frozen=True)
class QuestionAnswer:
    """
    One card answer as the server delivered it.

    :param session_id: The session that asked, e.g. ``"conv_abc123"``.
    :param action: The card verdict: ``"accept"``, ``"decline"`` or ``"cancel"``.
    :param choices: Question label to the choice. The label is the question's header,
        else its first 80 characters, never the full text, e.g. ``{"Release": "Yes"}``.
    """

    session_id: str
    action: str
    choices: dict[str, str]


# custom-lint: disable-next=workspace-scoped-cache -- ids are unique, lookups session-checked
_answers: OrderedDict[str, QuestionAnswer] = OrderedDict()
_lock = threading.Lock()


def record(question_id: str, answer: QuestionAnswer) -> None:
    """Remember *answer* for *question_id*, dropping the oldest past the cap."""
    with _lock:
        _answers[question_id] = answer
        _answers.move_to_end(question_id)
        while len(_answers) > _MAX_ANSWERS:
            _answers.popitem(last=False)


def forget(question_id: str) -> None:
    """Drop the answer for *question_id*, e.g. when its delivery failed."""
    with _lock:
        _answers.pop(question_id, None)


def lookup(question_id: str, *, session_id: str) -> QuestionAnswer | None:
    """Return the answer to *question_id* when *session_id* asked it, else ``None``."""
    with _lock:
        answer = _answers.get(question_id)
    if answer is None or answer.session_id != session_id:
        return None
    return answer


def reset_for_tests() -> None:
    """Clear every recorded answer."""
    with _lock:
        _answers.clear()
