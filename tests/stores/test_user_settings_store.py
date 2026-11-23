"""Tests for per-user settings the server applies on a user's behalf."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import column, insert, table

from omnigent.db.db_models import workspace_scope
from omnigent.db.utils import get_or_create_engine
from omnigent.server.server_config import SESSION_TITLE_INSTRUCTIONS_MAX_CHARS
from omnigent.stores.user_settings_store import (
    SESSION_TITLE_INSTRUCTIONS_PREFERENCE_KEY,
    THINKING_SUMMARIES_PREFERENCE_KEY,
    UserSettingsStore,
    open_user_settings_store,
    session_owner_thinking_summaries,
    session_owner_title_instructions,
)

ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture()
def store(db_uri: str) -> UserSettingsStore:
    return UserSettingsStore(db_uri)


def _write_raw_value(
    db_uri: str,
    user_id: str,
    value: bytes,
    key: str = SESSION_TITLE_INSTRUCTIONS_PREFERENCE_KEY,
) -> None:
    with get_or_create_engine(db_uri).begin() as connection:
        # Untyped columns skip the value codec while letting each dialect quote ``key``.
        raw_preferences = table(
            "preferences",
            column("workspace_id"),
            column("user_id"),
            column("key"),
            column("value"),
        )
        connection.execute(
            insert(raw_preferences).values(
                workspace_id=0,
                user_id=user_id,
                key=key,
                value=value,
            )
        )


def test_save_round_trips_stripped_instructions(store: UserSettingsStore) -> None:
    saved = store.save_session_title_instructions("  Start with an emoji.  ", user_id=ALICE)

    assert saved == "Start with an emoji."
    assert store.get_session_title_instructions(user_id=ALICE) == "Start with an emoji."


def test_save_replaces_previous_instructions(store: UserSettingsStore) -> None:
    store.save_session_title_instructions("First rule.", user_id=ALICE)
    store.save_session_title_instructions("Second rule.", user_id=ALICE)

    assert store.get_session_title_instructions(user_id=ALICE) == "Second rule."


@pytest.mark.parametrize("cleared", [None, "", "   \n"])
def test_blank_save_clears_instructions(store: UserSettingsStore, cleared: str | None) -> None:
    store.save_session_title_instructions("Start with an emoji.", user_id=ALICE)

    assert store.save_session_title_instructions(cleared, user_id=ALICE) is None
    assert store.get_session_title_instructions(user_id=ALICE) is None


def test_instructions_are_isolated_per_user(store: UserSettingsStore) -> None:
    store.save_session_title_instructions("Alice rule.", user_id=ALICE)
    store.save_session_title_instructions("Local rule.", user_id=None)

    assert store.get_session_title_instructions(user_id=ALICE) == "Alice rule."
    assert store.get_session_title_instructions(user_id=BOB) is None
    assert store.get_session_title_instructions(user_id=None) == "Local rule."


def test_instructions_are_isolated_per_workspace(store: UserSettingsStore) -> None:
    with workspace_scope(101):
        store.save_session_title_instructions("Workspace one rule.", user_id=ALICE)
    with workspace_scope(102):
        assert store.get_session_title_instructions(user_id=ALICE) is None
        store.save_session_title_instructions("Workspace two rule.", user_id=ALICE)
    with workspace_scope(101):
        assert store.get_session_title_instructions(user_id=ALICE) == "Workspace one rule."
    with workspace_scope(102):
        store.save_session_title_instructions(None, user_id=ALICE)
        assert store.get_session_title_instructions(user_id=ALICE) is None
    with workspace_scope(101):
        assert store.get_session_title_instructions(user_id=ALICE) == "Workspace one rule."


@pytest.mark.parametrize(
    "character",
    [pytest.param("x", id="ascii"), pytest.param("\N{ROCKET}", id="four-byte")],
)
def test_save_accepts_limit_and_rejects_longer(store: UserSettingsStore, character: str) -> None:
    at_limit = character * SESSION_TITLE_INSTRUCTIONS_MAX_CHARS
    assert store.save_session_title_instructions(at_limit, user_id=ALICE) == at_limit
    assert store.get_session_title_instructions(user_id=ALICE) == at_limit

    with pytest.raises(ValueError, match="exceed 4000 characters"):
        store.save_session_title_instructions(at_limit + character, user_id=ALICE)
    assert store.get_session_title_instructions(user_id=ALICE) == at_limit


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(b"\x00\x07unknown codec", id="unknown-codec"),
        pytest.param(b"\x00\x01not a zstd frame", id="corrupt-zstd"),
        pytest.param(b"\xff\xfe not utf-8", id="invalid-utf8"),
        pytest.param(b"x" * (SESSION_TITLE_INSTRUCTIONS_MAX_CHARS + 1), id="over-limit"),
        pytest.param(b"   ", id="blank"),
    ],
)
def test_invalid_stored_value_falls_back_to_default(
    store: UserSettingsStore, db_uri: str, stored: bytes
) -> None:
    _write_raw_value(db_uri, ALICE, stored)

    assert store.get_session_title_instructions(user_id=ALICE) is None


def test_open_store_requires_sql_storage(db_uri: str) -> None:
    assert open_user_settings_store(object()) is None
    first = open_user_settings_store(SimpleNamespace(storage_location=db_uri))
    assert first is not None
    assert open_user_settings_store(SimpleNamespace(storage_location=db_uri)) is first


def test_owner_resolver_reads_session_owner(store: UserSettingsStore) -> None:
    store.save_session_title_instructions("Alice rule.", user_id=ALICE)
    owners = {"conv_alice": ALICE, "conv_bob": BOB}
    calls: list[tuple[str, bool]] = []

    def get_session_owner(session_id: str, *, owner_only: bool = False) -> str | None:
        calls.append((session_id, owner_only))
        return owners.get(session_id)

    resolve = session_owner_title_instructions(
        store, SimpleNamespace(get_session_owner=get_session_owner), single_user=False
    )

    assert resolve("conv_alice") == "Alice rule."
    assert resolve("conv_bob") is None
    assert resolve("conv_unowned") is None
    assert calls == [("conv_alice", True), ("conv_bob", True), ("conv_unowned", True)]


def test_single_user_resolver_reads_local_user(store: UserSettingsStore) -> None:
    store.save_session_title_instructions("Local rule.", user_id=None)

    def unexpected_owner_lookup(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("single-user mode has one owner")

    resolve = session_owner_title_instructions(
        store, SimpleNamespace(get_session_owner=unexpected_owner_lookup), single_user=True
    )

    assert resolve("conv_any") == "Local rule."


def test_thinking_summaries_default_off_and_round_trip(store: UserSettingsStore) -> None:
    assert store.get_thinking_summaries(user_id=ALICE) is False

    assert store.save_thinking_summaries(True, user_id=ALICE) is True
    assert store.get_thinking_summaries(user_id=ALICE) is True

    assert store.save_thinking_summaries(False, user_id=ALICE) is False
    assert store.get_thinking_summaries(user_id=ALICE) is False


def test_thinking_summaries_are_isolated_per_user(store: UserSettingsStore) -> None:
    store.save_thinking_summaries(True, user_id=ALICE)

    assert store.get_thinking_summaries(user_id=BOB) is False
    assert store.get_thinking_summaries(user_id=None) is False
    store.save_thinking_summaries(True, user_id=None)
    store.save_thinking_summaries(False, user_id=ALICE)
    assert store.get_thinking_summaries(user_id=None) is True
    assert store.get_thinking_summaries(user_id=ALICE) is False


def test_thinking_summaries_do_not_touch_title_instructions(store: UserSettingsStore) -> None:
    store.save_session_title_instructions("Alice rule.", user_id=ALICE)
    store.save_thinking_summaries(True, user_id=ALICE)
    store.save_thinking_summaries(False, user_id=ALICE)

    assert store.get_session_title_instructions(user_id=ALICE) == "Alice rule."


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(b"false", id="false"),
        pytest.param(b"1", id="other-text"),
        pytest.param(b"\x00\x07unknown codec", id="unknown-codec"),
    ],
)
def test_unexpected_thinking_summaries_value_reads_off(
    store: UserSettingsStore, db_uri: str, stored: bytes
) -> None:
    _write_raw_value(db_uri, ALICE, stored, key=THINKING_SUMMARIES_PREFERENCE_KEY)

    assert store.get_thinking_summaries(user_id=ALICE) is False


def _conversation(conv_id: str, root_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=conv_id, root_conversation_id=root_id or conv_id)


def test_thinking_owner_resolver_reads_session_owner(store: UserSettingsStore) -> None:
    store.save_thinking_summaries(True, user_id=ALICE)
    owners = {"conv_alice": ALICE, "conv_bob": BOB}
    calls: list[tuple[str, bool]] = []

    def get_session_owner(session_id: str, *, owner_only: bool = False) -> str | None:
        calls.append((session_id, owner_only))
        return owners.get(session_id)

    resolve = session_owner_thinking_summaries(
        store, SimpleNamespace(get_session_owner=get_session_owner), single_user=False
    )

    assert resolve(_conversation("conv_alice")) is True
    assert resolve(_conversation("conv_bob")) is False
    assert resolve(_conversation("conv_unowned")) is False
    # A sub-session without its own grant follows its root session's owner.
    assert resolve(_conversation("conv_child", root_id="conv_alice")) is True
    assert calls == [
        ("conv_alice", True),
        ("conv_bob", True),
        ("conv_unowned", True),
        ("conv_child", True),
        ("conv_alice", True),
    ]


def test_single_user_thinking_resolver_reads_local_user(store: UserSettingsStore) -> None:
    store.save_thinking_summaries(True, user_id=None)

    def unexpected_owner_lookup(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("single-user mode has one owner")

    resolve = session_owner_thinking_summaries(
        store, SimpleNamespace(get_session_owner=unexpected_owner_lookup), single_user=True
    )

    assert resolve(_conversation("conv_any")) is True
