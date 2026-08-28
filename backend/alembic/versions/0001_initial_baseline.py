"""Initial baseline

Bu revision şema değişikliği içermez. Amacı migration zincirini başlatmak ve
``alembic_version`` tablosunu oluşturmaktır. İlk gerçek tablo (``projects``)
T-101 kapsamında bu revision'ın üzerine eklenecektir.

Revision ID: 0001_initial_baseline
Revises:
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001_initial_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
