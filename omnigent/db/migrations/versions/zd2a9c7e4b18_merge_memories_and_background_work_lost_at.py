"""Merge upstream's memories table with the fork's background-work-lost column.

Revision ID: zd2a9c7e4b18
Revises: ffeec66eadb5, zc4f8a1b6e02
Create Date: 2026-11-21 00:00:00.000000

Upstream's revisions end at ``ffeec66eadb5``, a second head beside the fork's
``zc4f8a1b6e02``. A merge revision, rather than re-parenting either branch, lets
a database already at ``zc4f8a1b6e02`` still apply the upstream revisions.

Rollback to a binary at ``zc4f8a1b6e02``: ``alembic downgrade zc4f8a1b6e02``
removes only this merge, because Alembic keeps the other parent.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "zd2a9c7e4b18"
down_revision: str | Sequence[str] | None = ("ffeec66eadb5", "zc4f8a1b6e02")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
