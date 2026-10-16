"""add session_subscriptions table

Revision ID: nn1a2b3c4d5e
Revises: mm1a2b3c4d5e
Create Date: 2026-10-16 00:00:00.000000

Adds ``session_subscriptions``: one row per ``(subscriber, target)`` session
pair, so a session is told when another session ends a turn or blocks on a
prompt. ``last_turn_key`` / ``last_prompt_key`` hold a sha256 hex digest of the
last delivered turn / prompt; a conditional update on them claims each event
once across retries and replicas.

Deployment: additive, new table only; older application code ignores it. Roll
back by downgrading this revision (drops subscriptions).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "nn1a2b3c4d5e"
down_revision: str | None = "mm1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``session_subscriptions`` table."""
    op.create_table(
        "session_subscriptions",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("subscriber_id", Uuid16(), nullable=False),
        sa.Column("target_id", Uuid16(), nullable=False),
        sa.Column("last_turn_key", sa.String(64), nullable=True),
        sa.Column("last_prompt_key", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "subscriber_id", "target_id"),
    )
    op.create_index(
        "ix_session_subscriptions_target_id",
        "session_subscriptions",
        ["workspace_id", "target_id", "subscriber_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``session_subscriptions`` table."""
    op.drop_index("ix_session_subscriptions_target_id", table_name="session_subscriptions")
    op.drop_table("session_subscriptions")
