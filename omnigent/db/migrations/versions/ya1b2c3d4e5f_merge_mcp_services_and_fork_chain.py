"""Merge upstream's scheduled-task MCP services and comment anchors with the fork's chain.

Revision ID: ya1b2c3d4e5f
Revises: a7d4c2e9f610, xa1b2c3d4e5f
Create Date: 2026-11-15 00:00:00.000000

Upstream's revisions after ``1ad1db53f99d`` end at ``a7d4c2e9f610``, a second
head beside the fork's ``xa1b2c3d4e5f``. A merge revision, rather than
re-parenting either branch, lets a database already at ``xa1b2c3d4e5f`` still
apply them.

Rollback to a binary at ``xa1b2c3d4e5f``: ``alembic downgrade xa1b2c3d4e5f``
removes only this merge, because Alembic keeps the other parent; the upstream
revisions between ``1ad1db53f99d`` and ``a7d4c2e9f610`` stay applied.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "ya1b2c3d4e5f"
down_revision: str | Sequence[str] | None = ("a7d4c2e9f610", "xa1b2c3d4e5f")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
