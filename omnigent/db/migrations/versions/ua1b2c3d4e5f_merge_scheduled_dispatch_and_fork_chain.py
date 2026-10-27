"""Merge upstream's scheduled-task dispatch fields with the fork's migration chain.

Revision ID: ua1b2c3d4e5f
Revises: c8e4f2a6b901, ta1b2c3d4e5f
Create Date: 2026-10-27 00:00:00.000000

``c8e4f2a6b901`` revises ``nf12ab34cd56``, which ``ta1b2c3d4e5f`` already
merged. A merge revision, rather than re-parenting either branch, lets a
database already at ``ta1b2c3d4e5f`` still apply ``c8e4f2a6b901`` on upgrade.

Rollback to a binary at ``ta1b2c3d4e5f``: ``alembic downgrade nf12ab34cd56``.
That undoes this merge and ``c8e4f2a6b901``, with its guards, and keeps the
fork chain. Downgrading to ``ta1b2c3d4e5f`` only removes this merge, because
Alembic keeps the other parent.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "ua1b2c3d4e5f"
down_revision: str | Sequence[str] | None = ("c8e4f2a6b901", "ta1b2c3d4e5f")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
