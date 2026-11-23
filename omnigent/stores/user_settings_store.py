"""Per-user settings the server applies on the user's behalf.

Values live in the ``preferences`` table beside the session metadata, so any
deployment with a SQL conversation store has them without extra wiring.
Account deletion already removes a user's ``preferences`` rows.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import zstandard
from sqlalchemy import LargeBinary, delete, select, type_coerce
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.compression import decode
from omnigent.db.db_models import SqlPreference, current_workspace_id
from omnigent.db.preference_rows import upsert_preference_row
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.server_config import (
    SESSION_TITLE_INSTRUCTIONS_MAX_CHARS,
    normalize_session_title_instructions,
)

if TYPE_CHECKING:
    from omnigent.entities.conversation import Conversation

_logger = logging.getLogger(__name__)

SESSION_TITLE_INSTRUCTIONS_PREFERENCE_KEY = "session_title_instructions"
THINKING_SUMMARIES_PREFERENCE_KEY = "thinking_summaries"
# UTF-8 needs at most four bytes per character.
_MAX_DECODED_BYTES = SESSION_TITLE_INSTRUCTIONS_MAX_CHARS * 4
# The only stored value; turning the setting off deletes the row.
_THINKING_SUMMARIES_ON = "true"


class UserSettingsStore:
    """Read and write one user's server-applied settings.

    ``user_id=None`` is single-user mode and maps to the reserved ``local``
    namespace, matching the other per-user preference readers.
    """

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: SQLAlchemy database URI of the Omnigent DB,
            e.g. ``"sqlite:///chat.db"``.
        """
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.user_settings_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.user_settings_store", immediate=True
        )

    def _read_value(self, key: str, *, user_id: str | None, operation: str) -> str | None:
        """Return the user's decoded value for ``key``, or ``None`` when unset or unreadable."""
        preference_user_id = RESERVED_USER_LOCAL if user_id is None else user_id
        with self._session(operation) as session:
            # Decode raw bytes here so a malformed value cannot fail in the ORM result processor.
            raw = session.scalar(
                select(type_coerce(SqlPreference.value, LargeBinary)).where(
                    SqlPreference.workspace_id == current_workspace_id(),
                    SqlPreference.user_id == preference_user_id,
                    SqlPreference.key == key,
                )
            )
        if raw is None:
            return None
        try:
            return decode(raw, max_decoded_bytes=_MAX_DECODED_BYTES)
        except (ValueError, zstandard.ZstdError):
            _logger.warning("stored %s setting is unreadable — ignoring it", key)
            return None

    def _write_value(
        self, key: str, value: str | None, *, user_id: str | None, operation: str
    ) -> None:
        """Store ``value`` for ``key``, or delete the row when ``value`` is ``None``."""
        preference_user_id = RESERVED_USER_LOCAL if user_id is None else user_id

        def write(session: Session) -> None:
            require_active_account(session, user_id)
            if value is not None:
                upsert_preference_row(session, user_id=preference_user_id, key=key, value=value)
                return
            session.execute(
                delete(SqlPreference).where(
                    SqlPreference.workspace_id == current_workspace_id(),
                    SqlPreference.user_id == preference_user_id,
                    SqlPreference.key == key,
                )
            )

        run_write_transaction(self._session_immediate, operation, write)

    def get_session_title_instructions(self, *, user_id: str | None) -> str | None:
        """Return the user's saved title instructions, or ``None`` when unset or invalid."""
        value = self._read_value(
            SESSION_TITLE_INSTRUCTIONS_PREFERENCE_KEY,
            user_id=user_id,
            operation="read_session_title_instructions",
        )
        if value is None:
            return None
        return normalize_session_title_instructions(
            value, source="saved session title instructions"
        )

    def save_session_title_instructions(
        self, instructions: str | None, *, user_id: str | None
    ) -> str | None:
        """Save or clear the user's title instructions.

        :param instructions: New instructions; ``None`` or blank clears them.
        :param user_id: The authenticated user, or ``None`` in single-user mode.
        :returns: The stored, stripped instructions, or ``None`` when cleared.
        :raises ValueError: If the instructions exceed the character limit.
        """
        value = instructions.strip() if instructions else ""
        if len(value) > SESSION_TITLE_INSTRUCTIONS_MAX_CHARS:
            limit = SESSION_TITLE_INSTRUCTIONS_MAX_CHARS
            raise ValueError(f"Session title instructions exceed {limit} characters")
        self._write_value(
            SESSION_TITLE_INSTRUCTIONS_PREFERENCE_KEY,
            value or None,
            user_id=user_id,
            operation="save_session_title_instructions",
        )
        return value or None

    def get_thinking_summaries(self, *, user_id: str | None) -> bool:
        """Return whether the user asks harnesses for thinking summaries; unset is ``False``."""
        value = self._read_value(
            THINKING_SUMMARIES_PREFERENCE_KEY, user_id=user_id, operation="read_thinking_summaries"
        )
        return value == _THINKING_SUMMARIES_ON

    def save_thinking_summaries(self, enabled: bool, *, user_id: str | None) -> bool:
        """Turn the user's thinking summaries on or off; off removes the stored row.

        :param enabled: Whether sessions the user starts ask for thinking summaries.
        :param user_id: The authenticated user, or ``None`` in single-user mode.
        :returns: The saved value.
        """
        self._write_value(
            THINKING_SUMMARIES_PREFERENCE_KEY,
            _THINKING_SUMMARIES_ON if enabled else None,
            user_id=user_id,
            operation="save_thinking_summaries",
        )
        return enabled


# custom-lint: disable-next=workspace-scoped-cache -- keyed by database URL; queries scope rows
_stores: dict[str, UserSettingsStore] = {}
_stores_lock = threading.Lock()


def open_user_settings_store(conversation_store: object) -> UserSettingsStore | None:
    """Return the settings store beside the session metadata, or ``None`` without SQL.

    :param conversation_store: The server's conversation store; settings live
        in the database at its ``storage_location``.
    """
    location = getattr(conversation_store, "storage_location", None)
    if not isinstance(location, str) or not location:
        return None
    with _stores_lock:
        store = _stores.get(location)
        if store is None:
            try:
                store = UserSettingsStore(location)
            except Exception:
                _logger.warning("User settings storage is unavailable", exc_info=True)
                return None
            _stores[location] = store
        return store


def session_owner_title_instructions(
    store: UserSettingsStore,
    conversation_store: Any,
    *,
    single_user: bool,
) -> Callable[[str], str | None]:
    """Build a resolver from a session id to its owner's title instructions.

    :param store: Where the owner's settings live.
    :param conversation_store: Answers ``get_session_owner`` for the session.
    :param single_user: Whether the server runs without authentication; then
        every session belongs to the local user.
    :returns: A callable that returns ``None`` when the owner is unknown or
        has saved no instructions.
    """

    def resolve(session_id: str) -> str | None:
        if single_user:
            return store.get_session_title_instructions(user_id=None)
        owner = conversation_store.get_session_owner(session_id, owner_only=True)
        if owner is None:
            return None
        return store.get_session_title_instructions(user_id=owner)

    return resolve


def session_owner_thinking_summaries(
    store: UserSettingsStore,
    conversation_store: Any,
    *,
    single_user: bool,
) -> Callable[[Conversation], bool]:
    """Build a resolver from a session to its owner's thinking-summaries setting.

    A sub-session without its own owner grant takes its root session's owner.

    :param store: Where the owner's settings live.
    :param conversation_store: Answers ``get_session_owner`` for the session.
    :param single_user: Whether the server runs without authentication; then
        every session belongs to the local user.
    :returns: A callable that returns ``False`` when the owner is unknown or
        has not turned the setting on.
    """

    def resolve(conv: Conversation) -> bool:
        if single_user:
            return store.get_thinking_summaries(user_id=None)
        owner = conversation_store.get_session_owner(conv.id, owner_only=True)
        if owner is None and conv.root_conversation_id != conv.id:
            owner = conversation_store.get_session_owner(
                conv.root_conversation_id, owner_only=True
            )
        if owner is None:
            return False
        return store.get_thinking_summaries(user_id=owner)

    return resolve
