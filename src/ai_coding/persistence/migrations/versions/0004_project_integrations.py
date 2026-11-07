"""Project settings, encrypted secrets, deployment environments and deployments
(ADR-0043, ADR-0046, ADR-0048): integrations configured from the Web UI.

Revision ID: 0004_project_integrations
Revises: 0003_rate_limits
Create Date: 2026-11-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_project_integrations"
down_revision = "0003_rate_limits"
branch_labels = None
depends_on = None


_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _ts(name: str, *, nullable: bool = False) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _project_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE",
                                   name=f"fk_{table}_project_id_projects")  # fmt: skip


def upgrade() -> None:
    op.create_table(
        "project_settings",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("section", sa.String(40), nullable=False),
        sa.Column("data", _JSON, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(150), nullable=False),
        _ts("updated_at"),
        _project_fk("project_settings"),
        sa.PrimaryKeyConstraint("project_id", "section", name="pk_project_settings"),
    )
    op.create_table(
        "project_secrets",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("key_id", sa.String(16), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("hint", sa.String(16), nullable=False),
        sa.Column("updated_by", sa.String(150), nullable=False),
        _ts("updated_at"),
        _project_fk("project_secrets"),
        sa.PrimaryKeyConstraint("project_id", "name", name="pk_project_secrets"),
    )
    op.create_table(
        "deploy_environments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("production", sa.Boolean(), nullable=False),
        sa.Column("requires_approval", sa.Boolean(), nullable=False),
        sa.Column("require_gates", sa.Boolean(), nullable=False),
        sa.Column("config", _JSON, nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        _project_fk("deploy_environments"),
        sa.PrimaryKeyConstraint("id", name="pk_deploy_environments"),
        sa.UniqueConstraint("project_id", "name", name="uq_deploy_environments_project_name"),
    )
    op.create_table(
        "deployments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("ref", sa.String(255), nullable=False),
        sa.Column("commit", sa.String(64), nullable=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("external_url", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.String(150), nullable=False),
        _ts("created_at"),
        _ts("finished_at", nullable=True),
        _project_fk("deployments"),
        sa.ForeignKeyConstraint(["environment_id"], ["deploy_environments.id"], ondelete="CASCADE",
                                name="fk_deployments_environment_id_deploy_environments"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL",
                                name="fk_deployments_run_id_runs"),
        sa.PrimaryKeyConstraint("id", name="pk_deployments"),
    )  # fmt: skip
    op.create_index(
        "ix_deployments_environment_created", "deployments", ["environment_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_deployments_environment_created", table_name="deployments")
    op.drop_table("deployments")
    op.drop_table("deploy_environments")
    op.drop_table("project_secrets")
    op.drop_table("project_settings")
