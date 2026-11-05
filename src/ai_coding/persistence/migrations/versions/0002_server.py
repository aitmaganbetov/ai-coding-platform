"""Self-Hosted server: credentials, API tokens, idempotency records, run job queue.

Revision ID: 0002_server
Revises: 0001_initial
Create Date: 2026-11-05
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_server"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _ts(name: str, *, nullable: bool = True) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    op.create_table(
        "user_credentials",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        _ts("updated_at", nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE",
                                name="fk_user_credentials_user_id_users"),
        sa.PrimaryKeyConstraint("user_id", name="pk_user_credentials"),
    )  # fmt: skip
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("max_role", sa.String(20), nullable=True),
        _ts("created_at", nullable=False),
        _ts("expires_at"),
        _ts("last_used_at"),
        _ts("revoked_at"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE",
                                name="fk_api_tokens_user_id_users"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE",
                                name="fk_api_tokens_project_id_projects"),
        sa.PrimaryKeyConstraint("id", name="pk_api_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_api_tokens_token_hash"),
    )  # fmt: skip
    op.create_index("ix_api_tokens_user_id", "api_tokens", ["user_id"])
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("method", sa.String(10), nullable=False),
        sa.Column("path", sa.String(500), nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response", _JSON, nullable=False),
        _ts("created_at", nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE",
                                name="fk_idempotency_records_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_idempotency_records"),
        sa.UniqueConstraint("user_id", "key", name="uq_idempotency_user_key"),
    )  # fmt: skip
    op.create_table(
        "run_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(200), nullable=True),
        _ts("lease_expires_at"),
        _ts("available_at", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        _ts("created_at", nullable=False),
        _ts("finished_at"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE",
                                name="fk_run_jobs_run_id_runs"),
        sa.PrimaryKeyConstraint("id", name="pk_run_jobs"),
    )  # fmt: skip
    op.create_index("ix_run_jobs_run_id", "run_jobs", ["run_id"])
    op.create_index("ix_run_jobs_state_available", "run_jobs", ["state", "available_at"])


def downgrade() -> None:
    op.drop_index("ix_run_jobs_state_available", table_name="run_jobs")
    op.drop_index("ix_run_jobs_run_id", table_name="run_jobs")
    op.drop_table("run_jobs")
    op.drop_table("idempotency_records")
    op.drop_index("ix_api_tokens_user_id", table_name="api_tokens")
    op.drop_table("api_tokens")
    op.drop_table("user_credentials")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("is_admin")
