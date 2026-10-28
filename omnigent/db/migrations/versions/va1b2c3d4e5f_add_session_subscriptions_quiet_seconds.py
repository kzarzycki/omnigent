"""add quiet_seconds to session_subscriptions

Revision ID: va1b2c3d4e5f
Revises: ua1b2c3d4e5f
Create Date: 2026-10-27 00:00:00.000000

Adds a nullable ``quiet_seconds`` column to ``session_subscriptions``: how long
a completed turn end's notice is held before it is posted. ``NULL`` keeps every
existing subscription posting at once, as before.

Deployment: additive, nullable column; older application code ignores it.
Apply before the application change that writes it. Rerunning it is a no-op.
Roll back by downgrading this revision (drops every subscription's quiet period).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "va1b2c3d4e5f"
down_revision: str | None = "ua1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "session_subscriptions"
_COLUMN = "quiet_seconds"


def _has_column() -> bool:
    if context.is_offline_mode():
        return False
    return _COLUMN in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def upgrade() -> None:
    """Add ``session_subscriptions.quiet_seconds`` unless it is already there."""
    if not _has_column():
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop ``session_subscriptions.quiet_seconds``."""
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column(_COLUMN)
