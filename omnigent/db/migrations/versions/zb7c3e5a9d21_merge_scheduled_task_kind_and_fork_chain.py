"""Merge upstream's inbox items, task shares and scheduled-task kind with the fork's chain.

Revision ID: zb7c3e5a9d21
Revises: 27d16059fab9, ya1b2c3d4e5f
Create Date: 2026-11-16 00:00:00.000000

Upstream's revisions after ``a7d4c2e9f610`` end at ``27d16059fab9``, a second
head beside the fork's ``ya1b2c3d4e5f``. A merge revision, rather than
re-parenting either branch, lets a database already at ``ya1b2c3d4e5f`` still
apply them.

Rollback to a binary at ``ya1b2c3d4e5f``: ``alembic downgrade ya1b2c3d4e5f``
removes only this merge, because Alembic keeps the other parent; the upstream
revisions between ``a7d4c2e9f610`` and ``27d16059fab9`` stay applied.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "zb7c3e5a9d21"
down_revision: str | Sequence[str] | None = ("27d16059fab9", "ya1b2c3d4e5f")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
