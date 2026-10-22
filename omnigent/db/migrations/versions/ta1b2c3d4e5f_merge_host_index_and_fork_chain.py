"""Merge upstream's conversation host index with the fork's migration chain.

Revision ID: ta1b2c3d4e5f
Revises: nf12ab34cd56, sa2b3c4d5e6f
Create Date: 2026-10-22 00:00:00.000000

Both branches start at ``mm1a2b3c4d5e``. A merge revision, rather than
re-parenting either branch, lets a database already at ``sa2b3c4d5e6f``
still apply ``nf12ab34cd56`` on upgrade.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "ta1b2c3d4e5f"
down_revision: str | Sequence[str] | None = ("nf12ab34cd56", "sa2b3c4d5e6f")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
