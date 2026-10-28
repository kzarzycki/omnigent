"""Migrations ``nn1a2b3c4d5e`` and ``oo1a2b3c4d5e`` add the session subscription tables;
``va1b2c3d4e5f`` adds their quiet period."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache


def _run(uri: str, engine: sa.Engine, action: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        getattr(command, action)(config, revision)


def test_upgrade_creates_session_subscriptions_and_downgrade_drops_it(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'subscriptions.db'}"
    engine = sa.create_engine(uri)
    try:
        _run(uri, engine, "upgrade", "nn1a2b3c4d5e")
        inspector = sa.inspect(engine)
        assert "session_subscriptions" in inspector.get_table_names()
        pk = inspector.get_pk_constraint("session_subscriptions")["constrained_columns"]
        assert pk == ["workspace_id", "subscriber_id", "target_id"]
        indexes = {
            ix["name"]: ix["column_names"] for ix in inspector.get_indexes("session_subscriptions")
        }
        assert indexes["ix_session_subscriptions_target_id"] == [
            "workspace_id",
            "target_id",
            "subscriber_id",
        ]

        _run(uri, engine, "downgrade", "mm1a2b3c4d5e")
        assert "session_subscriptions" not in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()
        clear_engine_cache()


def test_upgrade_creates_the_delivery_outbox_and_downgrade_drops_it(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'deliveries.db'}"
    engine = sa.create_engine(uri)
    try:
        _run(uri, engine, "upgrade", "oo1a2b3c4d5e")
        inspector = sa.inspect(engine)
        pk = inspector.get_pk_constraint("session_subscription_deliveries")
        assert pk["constrained_columns"] == [
            "workspace_id",
            "subscriber_id",
            "target_id",
            "event_key",
        ]
        indexes = {
            ix["name"]: ix["column_names"]
            for ix in inspector.get_indexes("session_subscription_deliveries")
        }
        assert indexes["ix_session_subscription_deliveries_state_due_at"] == [
            "state",
            "due_at",
            "workspace_id",
            "subscriber_id",
            "target_id",
            "event_key",
        ]

        _run(uri, engine, "downgrade", "nn1a2b3c4d5e")
        assert "session_subscription_deliveries" not in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()
        clear_engine_cache()


def test_the_subscription_tables_declare_no_column_defaults(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'defaults.db'}"
    engine = sa.create_engine(uri)
    try:
        _run(uri, engine, "upgrade", "oo1a2b3c4d5e")
        inspector = sa.inspect(engine)
        for table in ("session_subscriptions", "session_subscription_deliveries"):
            defaults = {
                column["name"]: column["default"]
                for column in inspector.get_columns(table)
                if column["default"] is not None
            }
            assert defaults == {}, f"{table} declares database defaults: {defaults}"
    finally:
        engine.dispose()
        clear_engine_cache()


def test_the_delivery_outbox_stores_no_notice_text(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'outbox-columns.db'}"
    engine = sa.create_engine(uri)
    try:
        _run(uri, engine, "upgrade", "oo1a2b3c4d5e")
        columns = {
            column["name"]
            for column in sa.inspect(engine).get_columns("session_subscription_deliveries")
        }
        assert columns == {
            "workspace_id",
            "subscriber_id",
            "target_id",
            "event_key",
            "state",
            "due_at",
            "attempts",
            "outcome",
        }
    finally:
        engine.dispose()
        clear_engine_cache()


def test_quiet_seconds_is_added_nullable_reruns_as_a_no_op_and_downgrades(
    tmp_path: Path,
) -> None:
    uri = f"sqlite:///{tmp_path / 'quiet.db'}"
    engine = sa.create_engine(uri)

    def quiet_column() -> dict[str, object] | None:
        columns = sa.inspect(engine).get_columns("session_subscriptions")
        return next((dict(c) for c in columns if c["name"] == "quiet_seconds"), None)

    try:
        _run(uri, engine, "upgrade", "ua1b2c3d4e5f")
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO session_subscriptions (workspace_id, subscriber_id, target_id)"
                    " VALUES (1, :a, :b)"
                ),
                {"a": b"a" * 16, "b": b"b" * 16},
            )
        _run(uri, engine, "upgrade", "va1b2c3d4e5f")
        column = quiet_column()
        assert column is not None
        assert isinstance(column["type"], sa.Integer)
        assert column["nullable"] is True
        with engine.connect() as conn:
            assert conn.execute(
                sa.text("SELECT quiet_seconds FROM session_subscriptions")
            ).all() == [(None,)]

        # A rerun over a database that already has the column changes nothing.
        _run(uri, engine, "stamp", "ua1b2c3d4e5f")
        _run(uri, engine, "upgrade", "va1b2c3d4e5f")
        assert quiet_column() is not None

        _run(uri, engine, "downgrade", "ua1b2c3d4e5f")
        assert quiet_column() is None
    finally:
        engine.dispose()
        clear_engine_cache()
