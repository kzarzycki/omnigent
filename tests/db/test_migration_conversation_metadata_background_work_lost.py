"""Tests for the conversation metadata ``background_work_lost_at`` migration (zc4f8a1b6e02).

The migration and the ORM model add one nullable ``BigInteger`` with no
database default; existing rows read ``NULL`` and downgrading keeps them. On
SQLite and Cloudflare D1 a CHECK keeps the column an integer.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from omnigent.db.db_models import OmnigentBase, SqlConversationMetadata
from omnigent.db.utils import clear_engine_cache

# Registers the D1-on-SQLite dialect and shares its row helpers.
from tests.db.test_migration_conversation_metadata_sidebar_live_state import (
    _SESSION_ID,
    _TABLE,
    _columns,
    _insert,
    _migrate,
)

_PREVIOUS = "zb7c3e5a9d21"
_REVISION = "zc4f8a1b6e02"


@pytest.fixture(params=["sqlite", "cloudflare_d1+local_sqlite"])
def database(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[tuple[str, sa.Engine]]:
    """One SQLite file under the dialect tested, migrated to the previous revision."""
    path = tmp_path / "metadata.db"
    base_uri = f"sqlite:///{path}"
    base = sa.create_engine(base_uri)
    uri = f"{request.param}:///{path}"
    engine = sa.create_engine(uri)
    try:
        _migrate(base_uri, base, "upgrade", _PREVIOUS)
        yield uri, engine
    finally:
        base.dispose()
        engine.dispose()
        clear_engine_cache()


def _assert_holds_only_integers(engine: sa.Engine) -> None:
    for row_id, value in enumerate((None, 1_793_300_000), start=1):
        _insert(engine, row_id, "background_work_lost_at", value)
    for row_id, value in enumerate(("a" * 16_384, b"\x00" * 16_384), start=50):
        with pytest.raises(sa.exc.IntegrityError):
            _insert(engine, row_id, "background_work_lost_at", value)


def test_upgrade_adds_the_column_and_keeps_rows(database: tuple[str, sa.Engine]) -> None:
    uri, engine = database
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                f"INSERT INTO {_TABLE} (workspace_id, id, kind, git_branch) "
                "VALUES (0, :id, 1, 'main')"
            ),
            {"id": _SESSION_ID},
        )

    _migrate(uri, engine, "upgrade", _REVISION)

    column = _columns(engine)["background_work_lost_at"]
    assert isinstance(column["type"], sa.BigInteger)
    assert column["nullable"] is True and column["default"] is None
    with engine.connect() as conn:
        row = conn.execute(sa.text(f"SELECT background_work_lost_at FROM {_TABLE}")).one()
    assert tuple(row) == (None,)
    _assert_holds_only_integers(engine)


def test_model_built_schema_bounds_the_column(database: tuple[str, sa.Engine]) -> None:
    _, engine = database
    with engine.begin() as conn:
        conn.execute(sa.text(f"DROP TABLE {_TABLE}"))
    OmnigentBase.metadata.create_all(engine, tables=[SqlConversationMetadata.__table__])

    _assert_holds_only_integers(engine)


def test_downgrade_drops_the_column_and_keeps_rows(database: tuple[str, sa.Engine]) -> None:
    uri, engine = database
    _migrate(uri, engine, "upgrade", _REVISION)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                f"INSERT INTO {_TABLE} (workspace_id, id, kind, git_branch, "
                "background_work_lost_at) VALUES (0, :id, 1, 'main', 1793300000)"
            ),
            {"id": _SESSION_ID},
        )

    _migrate(uri, engine, "downgrade", _PREVIOUS)

    assert "background_work_lost_at" not in _columns(engine)
    with engine.connect() as conn:
        assert conn.execute(sa.text(f"SELECT git_branch FROM {_TABLE}")).scalar_one() == "main"
