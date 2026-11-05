"""Shared rate-limit counters for several API processes (S9, B-807).

Revision ID: 0003_rate_limits
Revises: 0002_server
Create Date: 2026-11-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_rate_limits"
down_revision = "0002_server"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_counters",
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("minute", sa.BigInteger(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("key", "minute", name="pk_rate_limit_counters"),
    )


def downgrade() -> None:
    op.drop_table("rate_limit_counters")
