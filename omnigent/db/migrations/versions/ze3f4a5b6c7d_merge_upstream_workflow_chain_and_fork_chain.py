"""Merge upstream's scheduled-task workflow chain with the fork's chain.

Revision ID: ze3f4a5b6c7d
Revises: 47d3e8fc3e25, zd2a9c7e4b18
Create Date: 2026-11-27 00:00:00.000000

Upstream's revisions after ``bf30d7cbda61`` end at ``47d3e8fc3e25``, a second
head beside the fork's ``zd2a9c7e4b18``. A merge revision, rather than
re-parenting either branch, lets a database already at ``zd2a9c7e4b18`` still
apply them.

Rollback to a binary at ``zd2a9c7e4b18``: ``alembic downgrade zd2a9c7e4b18``
removes only this merge, because Alembic keeps the other parent; the upstream
revisions stay applied.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "ze3f4a5b6c7d"
down_revision: str | Sequence[str] | None = ("47d3e8fc3e25", "zd2a9c7e4b18")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
