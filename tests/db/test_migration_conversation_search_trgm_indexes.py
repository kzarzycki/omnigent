"""Tests for the conversation-search trigram indexes migration.

Session search (``GET /v1/sessions?search_query=``) matches on
``LOWER(title) LIKE '%q%'`` OR ``LOWER(search_text) LIKE '%q%'``. The leading
wildcard makes both unindexable by a b-tree, so on Postgres the content half
sequential-scans every ``conversation_items`` row. Migration ``d5e9f1a2b3c4``
adds ``pg_trgm`` GIN indexes on those two lowercased expressions so the existing
``LIKE`` becomes index-backed (Postgres only; a no-op on SQLite, which keeps its
FTS5 table and small dev tables).

These tests assert the post-migration shape per dialect and that the chain stays
reversible. The Postgres leg (marked ``databricks``) additionally proves the
planner uses the content index for the real search predicate.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine

from omnigent.db.utils import (
    _build_alembic_config,
    _run_migrations,
    clear_engine_cache,
    get_or_create_engine,
)

_NEW_REVISION = "d5e9f1a2b3c4"
# Revision just before this one (its down_revision).
_PRE_REVISION = "f7a8b9c0d1e2"
_ITEMS_INDEX = "ix_conversation_items_search_text_trgm"
_TITLE_INDEX = "ix_conversations_title_trgm"


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> Iterator[Engine]:
    """Fresh SQLite DB with the full alembic chain applied (at head)."""
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    try:
        yield engine
    finally:
        clear_engine_cache()


def _index_names(engine: Engine, table: str) -> set[str]:
    return {i["name"] for i in sa.inspect(engine).get_indexes(table)}


def test_trgm_indexes_absent_on_sqlite(sqlite_engine: Engine) -> None:
    """
    On SQLite the migration is a no-op: neither trigram index is created.

    ``pg_trgm``/GIN are Postgres-only, and SQLite search rides its FTS5 table,
    so the migration must skip cleanly rather than emit unsupported DDL. A
    failure here means the dialect guard regressed and the chain would break on
    a fresh SQLite database.
    """
    assert _ITEMS_INDEX not in _index_names(sqlite_engine, "conversation_items")
    assert _TITLE_INDEX not in _index_names(sqlite_engine, "conversations")


def test_roundtrip_reversible_on_sqlite(tmp_path: Path) -> None:
    """
    Upgrade → downgrade → upgrade roundtrips on a throwaway SQLite DB.

    The downgrade leg is otherwise uncovered (the engine fixtures only run
    ``upgrade head``), so this proves the migration stays reversible even though
    both directions are no-ops on SQLite.
    """
    uri = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    cfg = _build_alembic_config(uri)
    engine = sa.create_engine(uri)
    try:
        for target in (_NEW_REVISION, _PRE_REVISION, _NEW_REVISION):
            with engine.begin() as conn:
                cfg.attributes["connection"] = conn
                command.upgrade(cfg, target) if target == _NEW_REVISION else command.downgrade(
                    cfg, target
                )
        # No trigram indexes on SQLite regardless of direction.
        assert _ITEMS_INDEX not in _index_names(engine, "conversation_items")
    finally:
        engine.dispose()
        clear_engine_cache()


@pytest.mark.databricks
def test_trgm_indexes_present_on_postgres(db_uri: str) -> None:
    """
    On Postgres both trigram GIN indexes exist at head: the title one over
    ``lower(title)``, and the item content one over the raw ``search_text``,
    which is what content search's ``ILIKE`` reads.

    Asserts the migrations' contract structurally — index type ``gin`` with
    ``gin_trgm_ops`` — rather than a query plan,
    which depends on table statistics and would be non-deterministic against the
    empty per-worker test DB. (Whether the planner *chooses* the index at scale
    is a property of Postgres + ``pg_trgm``, not of this DDL.)

    Runs only in the Postgres-backed ``databricks`` lane (the ``db_uri`` fixture
    yields the migrated per-worker Postgres DB there; on SQLite this test is
    deselected). Read-only — it never downgrades the shared worker DB.
    """
    engine = get_or_create_engine(db_uri)
    if engine.dialect.name != "postgresql":
        pytest.skip("trigram indexes are Postgres-only")

    with engine.connect() as conn:
        defs = dict(
            conn.execute(
                sa.text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE indexname IN (:items, :title)"
                ),
                {"items": _ITEMS_INDEX, "title": _TITLE_INDEX},
            ).fetchall()
        )
        invalid = conn.execute(
            sa.text(
                "SELECT c.relname FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname IN (:items, :title) AND NOT i.indisvalid"
            ),
            {"items": _ITEMS_INDEX, "title": _TITLE_INDEX},
        ).fetchall()
    assert set(defs) == {_TITLE_INDEX, _ITEMS_INDEX}, defs
    for definition in defs.values():
        lowered = definition.lower()
        assert "gin" in lowered and "gin_trgm_ops" in lowered, definition
    assert "lower(" in defs[_TITLE_INDEX].lower(), defs[_TITLE_INDEX]
    assert "lower(" not in defs[_ITEMS_INDEX].lower(), defs[_ITEMS_INDEX]
    # The indexes are built with CREATE INDEX CONCURRENTLY; a failed concurrent
    # build leaves an INVALID (planner-ignored) index that would still pass the
    # structural checks above, so assert both builds actually completed.
    assert not invalid, invalid


@pytest.mark.skipif(
    not os.environ.get("OMNIGENT_TEST_DB_URI", "").startswith("postgresql"),
    reason="needs PostgreSQL (OMNIGENT_TEST_DB_URI)",
)
def test_postgres_items_index_rebuild_keeps_a_valid_build_and_replaces_leftovers() -> None:
    """
    ``sa2b3c4d5e6f`` keeps a valid index an operator built ahead of a deploy,
    replaces an INVALID leftover of a failed concurrent build and the old
    ``lower(search_text)`` index, and its downgrade runs in a caller's transaction.
    """
    root = sa.make_url(os.environ["OMNIGENT_TEST_DB_URI"])
    name = f"sa2_rebuild_{os.getpid()}"
    uri = root.set(database=name).render_as_string(hide_password=False)
    admin = sa.create_engine(root, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    setup = sa.create_engine(uri, isolation_level="AUTOCOMMIT")
    index_state = (
        "SELECT i.indexrelid::int, i.indisvalid, pg_get_indexdef(i.indexrelid) "
        "FROM pg_index i WHERE i.indexrelid = to_regclass(:name)"
    )

    def rerun_migration(conn: sa.Connection) -> tuple[int, bool, str]:
        conn.execute(sa.text("UPDATE alembic_version SET version_num = 'rc7d1e2f3a4b'"))
        _run_migrations(sa.create_engine(uri), uri)
        return tuple(conn.execute(sa.text(index_state), {"name": _ITEMS_INDEX}).one())

    try:
        _run_migrations(sa.create_engine(uri), uri)
        with setup.connect() as conn:
            prebuilt, _, _ = conn.execute(sa.text(index_state), {"name": _ITEMS_INDEX}).one()
            assert rerun_migration(conn)[0] == prebuilt, "a valid prebuilt index was rebuilt"

            conn.execute(
                sa.text("UPDATE pg_index SET indisvalid = false WHERE indexrelid = :oid"),
                {"oid": prebuilt},
            )
            oid, valid, _ = rerun_migration(conn)
            assert valid and oid != prebuilt, "an INVALID leftover was kept"

            conn.execute(sa.text(f"DROP INDEX {_ITEMS_INDEX}"))
            conn.execute(
                sa.text(
                    f"CREATE INDEX {_ITEMS_INDEX} ON conversation_items "
                    "USING gin (lower(search_text) gin_trgm_ops)"
                )
            )
            _, valid, definition = rerun_migration(conn)
            assert valid and "lower(" not in definition.lower(), definition

        cfg = _build_alembic_config(uri)
        engine = sa.create_engine(uri)
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "rc7d1e2f3a4b")
        engine.dispose()
        with setup.connect() as conn:
            assert (
                conn.execute(sa.text("SELECT to_regclass(:n)"), {"n": _ITEMS_INDEX}).scalar()
                is None
            )
    finally:
        setup.dispose()
        clear_engine_cache()
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
