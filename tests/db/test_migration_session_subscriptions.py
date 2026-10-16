"""Migration ``nn1a2b3c4d5e`` adds the ``session_subscriptions`` table."""

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
