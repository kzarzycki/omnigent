"""Add ``background_work_lost_at`` to conversation metadata.

Revision ID: zc4f8a1b6e02
Revises: zb7c3e5a9d21

Adds one nullable ``BigInteger`` column to ``omnigent_conversation_metadata``:
when the server dropped a session's running background-work flag because the
runner holding that work went away, in epoch seconds. The session list shows
it, and the server clears it once the session was told the work was lost.

SQLite does not enforce declared types, so there, and on Cloudflare D1, which
serves SQLite, the column gets a CHECK that it holds an integer, added in the
same ``ADD COLUMN`` so the table is not rebuilt.

Additive with no default or backfill; older servers ignore the column.
Downgrade drops it, discarding which sessions lost background work.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "zc4f8a1b6e02"
down_revision: str | None = "zb7c3e5a9d21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "omnigent_conversation_metadata"
_LOST_AT_CHECK = "ck_conversation_metadata_lost_at_integer"
# Dialects that store a value of any type in any column.
_SQLITE_FAMILY = frozenset({"sqlite", "cloudflare_d1"})


def upgrade() -> None:
    """Add ``background_work_lost_at``."""
    if op.get_bind().dialect.name in _SQLITE_FAMILY:
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN background_work_lost_at BIGINT "
            f"CONSTRAINT {_LOST_AT_CHECK} "
            "CHECK (background_work_lost_at IS NULL "
            "OR typeof(background_work_lost_at) = 'integer')"
        )
    else:
        op.add_column(_TABLE, sa.Column("background_work_lost_at", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Remove ``background_work_lost_at``."""
    with op.batch_alter_table(_TABLE) as batch_op:
        if op.get_bind().dialect.name in _SQLITE_FAMILY:
            # The rebuilt table would otherwise keep the CHECK on the dropped column.
            batch_op.drop_constraint(_LOST_AT_CHECK, type_="check")
        batch_op.drop_column("background_work_lost_at")
