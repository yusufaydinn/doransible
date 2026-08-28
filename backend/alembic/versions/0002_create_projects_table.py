"""create projects table

Project, kayıtlı bir Ansible dosya ağacının kökünü temsil eder (T-101).

`path` görüntülenen kanonik yoldur ve arama için indekslidir. Unique index
`path_key` üzerindedir: bu sütun `os.path.normcase` ile türetilir ve
Windows'un case-insensitive dosya sistemi semantiğini yansıtır, böylece
`C:\\Projeler` ile `c:\\projeler` tek kayıt olur.

Server default'lar dialect'e göre render edilen SQLAlchemy construct'larıyla
verilir; böylece aynı migration PostgreSQL'de de çalışır (ADR-004).

Revision ID: 0002_create_projects_table
Revises: 0001_initial_baseline
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_create_projects_table"
down_revision: str | None = "0001_initial_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("path_key", sa.String(length=1024), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
    )
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_projects_path"), ["path"], unique=False)
        batch_op.create_index(batch_op.f("ix_projects_path_key"), ["path_key"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_projects_path_key"))
        batch_op.drop_index(batch_op.f("ix_projects_path"))

    op.drop_table("projects")
