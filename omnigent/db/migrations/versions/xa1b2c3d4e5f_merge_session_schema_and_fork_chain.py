"""Merge upstream's session-mode, side-chat and family schema with the fork's chain.

Revision ID: xa1b2c3d4e5f
Revises: 1ad1db53f99d, wa1b2c3d4e5f
Create Date: 2026-11-13 00:00:00.000000

Upstream's revisions after ``b2e4f6a8c013`` end at ``1ad1db53f99d``, a second
head beside the fork's ``wa1b2c3d4e5f``. A merge revision, rather than
re-parenting either branch, lets a database already at ``wa1b2c3d4e5f`` still
apply them.

Rollback to a binary at ``wa1b2c3d4e5f``: ``alembic downgrade wa1b2c3d4e5f``
removes only this merge, because Alembic keeps the other parent; the upstream
revisions between ``b2e4f6a8c013`` and ``1ad1db53f99d`` stay applied.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "xa1b2c3d4e5f"
down_revision: str | Sequence[str] | None = ("1ad1db53f99d", "wa1b2c3d4e5f")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
