"""Merge upstream's completed-state and pull-request schema with the fork's chain.

Revision ID: wa1b2c3d4e5f
Revises: b2e4f6a8c013, va1b2c3d4e5f
Create Date: 2026-10-30 00:00:00.000000

Upstream's revisions after ``c8e4f2a6b901`` end at ``b2e4f6a8c013``, a second
head beside the fork's ``va1b2c3d4e5f``. A merge revision, rather than
re-parenting either branch, lets a database already at ``va1b2c3d4e5f`` still
apply them.

Rollback to a binary at ``va1b2c3d4e5f``: ``alembic downgrade va1b2c3d4e5f``
removes only this merge, because Alembic keeps the other parent; the upstream
revisions between ``c8e4f2a6b901`` and ``b2e4f6a8c013`` stay applied.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "wa1b2c3d4e5f"
down_revision: str | Sequence[str] | None = ("b2e4f6a8c013", "va1b2c3d4e5f")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
