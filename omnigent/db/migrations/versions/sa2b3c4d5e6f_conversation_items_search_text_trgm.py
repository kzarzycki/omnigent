"""Index conversation item text for session content search.

Revision ID: sa2b3c4d5e6f
Revises: rc7d1e2f3a4b
Create Date: 2026-10-22 00:00:00.000000

Session search matches items with ``search_text ILIKE '%q%'``. No index served
that predicate (the earlier trigram index covered ``LOWER(search_text)``, a
different expression, and ``gf1b2c3d4e5f`` dropped it), so a word that matches
nothing read and decompressed every item in the workspace: 7-9 s on a server
with 245k items. A ``pg_trgm`` GIN index on the raw column serves ``ILIKE``
directly; the planner takes it for rare words and keeps the correlated
early-stop probe for common ones, so the query itself is unchanged.

Postgres only, like ``d5e9f1a2b3c4``. Built ``CONCURRENTLY`` so writers keep
going during the build (about 75 s for 245k items / 800 MB on a laptop).
An operator can build the same index ahead of a deploy; this migration then
finds it valid and skips the build.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "sa2b3c4d5e6f"
down_revision: str | None = "rc7d1e2f3a4b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_conversation_items_search_text_trgm"


def upgrade() -> None:
    """Build the trigram index on ``conversation_items.search_text``. No-op off Postgres."""
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    with op.get_context().autocommit_block():
        # IF NOT EXISTS would keep an INVALID leftover of a failed concurrent
        # build, or the old LOWER(search_text) index, so drop either first.
        stale = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT 1 FROM pg_index i WHERE i.indexrelid = to_regclass(:name) "
                    "AND (NOT i.indisvalid OR pg_get_indexdef(i.indexrelid) ILIKE '%lower(%')"
                ),
                {"name": _INDEX},
            )
            .scalar()
        )
        if stale:
            op.execute(f"DROP INDEX CONCURRENTLY {_INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
            "ON conversation_items USING gin (search_text gin_trgm_ops)"
        )


def downgrade() -> None:
    """Drop the trigram index. No-op off Postgres."""
    if op.get_bind().dialect.name != "postgresql":
        return
    # Plain DROP, like the other downgrades: callers may run it inside their
    # own transaction, where CONCURRENTLY is not allowed.
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
