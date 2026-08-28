"""create execution plans table

Revision ID: 0005_create_execution_plans_table
Revises: 0004_create_jobs_table
Create Date: 2026-08-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_create_execution_plans_table"
down_revision: str | None = "0004_create_jobs_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXECUTION_PLAN_STATUS = sa.Enum(
    "prepared",
    "claimed",
    "expired",
    name="execution_plan_status",
    native_enum=False,
    length=16,
    create_constraint=True,
)


def upgrade() -> None:
    op.create_table(
        "execution_plans",
        sa.Column("id", sa.String(36), nullable=False),
        # Raw token **saklanmaz**; sütun yalnızca SHA-256 özetini taşır.
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("inventory_id", sa.Integer(), nullable=False),
        sa.Column("playbook_path", sa.String(1024), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        # Dondurulmuş workspace'in opaque adı; absolute path değildir.
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column("status", EXECUTION_PLAN_STATUS, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status <> 'claimed' OR claimed_at IS NOT NULL",
            name=op.f("ck_execution_plans_claimed_has_claimed_at"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_execution_plans_expiry_after_creation"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="RESTRICT",
            name=op.f("fk_execution_plans_project_id_projects"),
        ),
        sa.ForeignKeyConstraint(
            ["inventory_id"],
            ["inventories.id"],
            ondelete="RESTRICT",
            name=op.f("fk_execution_plans_inventory_id_inventories"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_plans")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_execution_plans_token_hash")),
        sa.UniqueConstraint("workspace_id", name=op.f("uq_execution_plans_workspace_id")),
    )
    op.create_index(op.f("ix_execution_plans_project_id"), "execution_plans", ["project_id"])
    op.create_index(op.f("ix_execution_plans_inventory_id"), "execution_plans", ["inventory_id"])
    op.create_index(op.f("ix_execution_plans_expires_at"), "execution_plans", ["expires_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_execution_plans_expires_at"), table_name="execution_plans")
    op.drop_index(op.f("ix_execution_plans_inventory_id"), table_name="execution_plans")
    op.drop_index(op.f("ix_execution_plans_project_id"), table_name="execution_plans")
    op.drop_table("execution_plans")
