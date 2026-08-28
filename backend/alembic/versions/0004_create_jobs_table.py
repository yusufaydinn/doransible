"""create jobs table

Revision ID: 0004_create_jobs_table
Revises: 0003_create_inventories_table
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_create_jobs_table"
down_revision: str | None = "0003_create_inventories_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_TYPE = sa.Enum(
    "ping", "playbook", name="job_type", native_enum=False, length=16, create_constraint=True
)
JOB_STATUS = sa.Enum(
    "pending",
    "running",
    "successful",
    "failed",
    "canceled",
    name="job_status",
    native_enum=False,
    length=16,
    create_constraint=True,
)


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("job_type", JOB_TYPE, nullable=False),
        sa.Column("status", JOB_STATUS, nullable=False),
        sa.Column("inventory_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("playbook_path", sa.String(1024), nullable=True),
        sa.Column("limit_pattern", sa.String(256), nullable=True),
        sa.Column("requested_by", sa.String(100), nullable=False),
        sa.Column("artifact_path", sa.String(1024), nullable=True),
        sa.Column("return_code", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status <> 'running' OR started_at IS NOT NULL",
            name=op.f("ck_jobs_running_has_started_at"),
        ),
        sa.ForeignKeyConstraint(
            ["inventory_id"],
            ["inventories.id"],
            ondelete="RESTRICT",
            name=op.f("fk_jobs_inventory_id_inventories"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="RESTRICT",
            name=op.f("fk_jobs_project_id_projects"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index(op.f("ix_jobs_inventory_id"), "jobs", ["inventory_id"])
    op.create_index(op.f("ix_jobs_project_id"), "jobs", ["project_id"])
    op.create_index(op.f("ix_jobs_created_at"), "jobs", ["created_at"])
    predicate = sa.text("job_type = 'ping' AND status IN ('pending', 'running')")
    op.create_index(
        "uq_jobs_active_ping_inventory",
        "jobs",
        ["inventory_id"],
        unique=True,
        sqlite_where=predicate,
        postgresql_where=predicate,
    )


def downgrade() -> None:
    op.drop_index("uq_jobs_active_ping_inventory", table_name="jobs")
    op.drop_index(op.f("ix_jobs_created_at"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_project_id"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_inventory_id"), table_name="jobs")
    op.drop_table("jobs")
