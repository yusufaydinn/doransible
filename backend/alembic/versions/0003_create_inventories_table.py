"""create inventories table

Inventory, kayıtlı bir Ansible inventory **dosyasının** kaydıdır (T-201).

`project_id` nullable'dır: inventory bir project'e bağlı olabileceği gibi
project'ten bağımsız ve yeniden kullanılabilir de olabilir (MIMARI.md bölüm 6).

`source_type` native olmayan bir Enum'dur: VARCHAR + CHECK olarak render edilir,
böylece hem SQLite hem PostgreSQL'de aynı davranır (ADR-004) ve geçersiz bir
değer uygulama katmanı atlansa bile veritabanına yazılamaz.

`path` üzerinde bilinçli olarak unique index **yoktur**: aynı inventory dosyası
farklı project'lere bağlı veya farklı adlarla birden çok kez kaydedilebilir.

Revision ID: 0003_create_inventories_table
Revises: 0002_create_projects_table
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_create_inventories_table"
down_revision: str | None = "0002_create_projects_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_TYPE = sa.Enum(
    "ini",
    "yaml",
    name="inventory_source_type",
    native_enum=False,
    length=16,
    create_constraint=True,
)


def upgrade() -> None:
    op.create_table(
        "inventories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("source_type", SOURCE_TYPE, nullable=False),
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
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_inventories_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventories")),
    )
    with op.batch_alter_table("inventories", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_inventories_path"), ["path"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_inventories_project_id"), ["project_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("inventories", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_inventories_project_id"))
        batch_op.drop_index(batch_op.f("ix_inventories_path"))

    op.drop_table("inventories")
