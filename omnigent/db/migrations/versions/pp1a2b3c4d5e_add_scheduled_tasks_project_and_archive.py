"""add project_id and archive_when_done columns to scheduled_tasks

Revision ID: pp1a2b3c4d5e
Revises: oo1a2b3c4d5e
Create Date: 2026-10-21 00:00:00.000000

Adds ``project_id`` (nullable) and ``archive_when_done`` (boolean, default
false) to ``scheduled_tasks``. A fired run is filed into ``project_id``; when
``archive_when_done`` is set, a run that ends ``succeeded`` without the
``omnigent.keep`` label is archived.

Deployment: additive; older application code ignores both columns. Roll back by
downgrading this revision.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "pp1a2b3c4d5e"
down_revision: str | None = "oo1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add project_id and archive_when_done to scheduled_tasks."""
    op.add_column("scheduled_tasks", sa.Column("project_id", Uuid16(), nullable=True))
    op.add_column(
        "scheduled_tasks",
        sa.Column("archive_when_done", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    """Remove project_id and archive_when_done from scheduled_tasks."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("archive_when_done")
        batch_op.drop_column("project_id")
