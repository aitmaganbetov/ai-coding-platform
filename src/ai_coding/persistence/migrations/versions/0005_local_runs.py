"""Runs of the ``ai-coding`` CLI on developers' machines reported to the server
(ADR-0047): their summaries only.

Revision ID: 0005_local_runs
Revises: 0004_project_integrations
Create Date: 2026-11-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_local_runs"
down_revision = "0004_project_integrations"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _ts(name: str, *, nullable: bool = False) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "local_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("reported_by", sa.String(150), nullable=False),
        sa.Column("machine", sa.String(255), nullable=False),
        sa.Column("cli_version", sa.String(40), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("goal", sa.String(200), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("verdict", sa.String(10), nullable=True),
        sa.Column("branch", sa.String(255), nullable=True),
        sa.Column("commit", sa.String(64), nullable=True),
        sa.Column("gates", _JSON, nullable=False),
        sa.Column("findings", _JSON, nullable=False),
        sa.Column("tasks", _JSON, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        _ts("started_at", nullable=True),
        _ts("finished_at", nullable=True),
        _ts("reported_at"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE",
                                name="fk_local_runs_project_id_projects"),
        sa.PrimaryKeyConstraint("id", name="pk_local_runs"),
        sa.UniqueConstraint("project_id", "run_id", name="uq_local_runs_project_run"),
    )  # fmt: skip
    op.create_index("ix_local_runs_project_reported", "local_runs", ["project_id", "reported_at"])


def downgrade() -> None:
    op.drop_index("ix_local_runs_project_reported", table_name="local_runs")
    op.drop_table("local_runs")
