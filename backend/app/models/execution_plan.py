"""Hazırlanmış execution plan ORM modeli (R1-V2).

Kayıt bir **onay biletidir**, bir çalıştırma değil: satırın kendisi ne bir Job
ne de bir artifact üretir; yalnızca "kullanıcı şu dondurulmuş içeriği onaya
hazırladı" bilgisini taşır. Bileti tüketip karşılığında ``pending`` bir Job
üreten yol ayrıdır (``POST /api/projects/{project_id}/executions``, R1-V3D1).

İki şey bilinçli olarak **saklanmaz**:

- **Raw token.** Sütun yalnızca SHA-256 özetini tutar; veritabanını okuyan biri
  kullanılabilir bir token elde edemez.
- **Absolute workspace path'i.** Yalnızca opaque ``workspace_id`` tutulur; kök,
  her zaman çalışma anındaki ayarlardan türetilir. Path'i satıra yazmak, kökü
  değişen bir kurulumda kaydın eski bir dizini işaret etmesine ve o dizinin
  "güvenilir" sayılmasına yol açardı.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db.base import Base
from app.models.execution_mode import ExecutionMode, execution_mode_enum


class ExecutionPlanStatus(StrEnum):
    """Hazırlanmış planın yaşam döngüsü.

    ``claimed`` tek yönlüdür: atomik claim bir kez başarılı olur ve kayıt bir
    daha ``prepared`` hâline dönemez.
    """

    PREPARED = "prepared"
    CLAIMED = "claimed"
    EXPIRED = "expired"


def _status_enum() -> Enum:
    return Enum(
        ExecutionPlanStatus,
        name="execution_plan_status",
        native_enum=False,
        length=16,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


class ExecutionPlanRecord(Base):
    """Dondurulmuş workspace'e bağlı, TTL'li tek kullanımlık plan kaydı."""

    __tablename__ = "execution_plans"
    __table_args__ = (
        CheckConstraint(
            "status <> 'claimed' OR claimed_at IS NOT NULL",
            name="ck_execution_plans_claimed_has_claimed_at",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_execution_plans_expiry_after_creation",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # Token'ın kendisi değil, yalnızca SHA-256 özeti (64 hex).
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    inventory_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("inventories.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    playbook_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    # Planı hazırlayan aktör (:attr:`Settings.local_actor`). Claim koşulunun
    # **parçasıdır**: başka bir aktör adına hazırlanmış bir plan tüketilemez.
    # API cevabına çıkmaz.
    requested_by: Mapped[str] = mapped_column(String(100), nullable=False)
    # Planın bağlı olduğu değişmez girdilerin özeti; claim anında yeniden
    # hesaplanıp karşılaştırılır.
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # Dondurulmuş workspace'in kök altındaki opaque adı. Absolute path değildir.
    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    manifest_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # Planın onayladığı execution mode. Job'a **buradan** kopyalanır (R1-V3H1B);
    # mode'u Job'a ayrıca sormak, onaylanandan başka bir mode ile çalıştırma
    # imkânı bırakırdı.
    #
    # Sunucu tarafı varsayılanı ``check``'tir. Bu bir yan etkisizlik garantisi
    # değil, **sessiz yükseltme engelidir**: mode'u belirtmeyen bir yazım —
    # eski bir istemci, elle atılmış bir INSERT, migration'ın doldurduğu legacy
    # satır — ``--check`` taşımayan argv'ye kendiliğinden geçemez.
    mode: Mapped[ExecutionMode] = mapped_column(
        execution_mode_enum(),
        nullable=False,
        default=ExecutionMode.CHECK,
        server_default=ExecutionMode.CHECK.value,
    )
    status: Mapped[ExecutionPlanStatus] = mapped_column(_status_enum(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @validates("id", "workspace_id")
    def _canonical_uuid4(self, _key: str, value: str) -> str:
        """Kimlikler uygulamanın ürettiği kanonik UUID4 olmalıdır."""
        parsed = uuid.UUID(value)
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError("Execution plan kimliği canonical UUID4 olmalıdır.")
        return value
