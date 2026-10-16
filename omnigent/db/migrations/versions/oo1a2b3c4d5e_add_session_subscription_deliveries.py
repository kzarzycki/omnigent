"""add session_subscription_deliveries table

Revision ID: oo1a2b3c4d5e
Revises: nn1a2b3c4d5e
Create Date: 2026-10-16 00:00:00.000000

Adds ``session_subscription_deliveries``: the outbox of subscription notices,
one row per ``(subscriber, target, event)``. The primary key makes each event
deliverable once per subscriber; pending rows are retried until posted. Rows
hold only the event's identity and an outcome code, never user content: the
notice text is built when it is posted.

Deployment: additive, new table only; older application code ignores it.
Apply before the application change that writes it. Roll back by downgrading
this revision (drops undelivered notices).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "oo1a2b3c4d5e"
down_revision: str | None = "nn1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``session_subscription_deliveries`` table."""
    op.create_table(
        "session_subscription_deliveries",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("subscriber_id", Uuid16(), nullable=False),
        sa.Column("target_id", Uuid16(), nullable=False),
        sa.Column("event_key", sa.String(64), nullable=False),
        sa.Column("state", sa.SmallInteger(), nullable=False),
        sa.Column("due_at", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), nullable=False),
        sa.Column("outcome", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "subscriber_id", "target_id", "event_key"),
    )
    op.create_index(
        "ix_session_subscription_deliveries_state_due_at",
        "session_subscription_deliveries",
        ["state", "due_at", "workspace_id", "subscriber_id", "target_id", "event_key"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``session_subscription_deliveries`` table."""
    op.drop_index(
        "ix_session_subscription_deliveries_state_due_at",
        table_name="session_subscription_deliveries",
    )
    op.drop_table("session_subscription_deliveries")
