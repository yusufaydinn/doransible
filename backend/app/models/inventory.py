"""Inventory ORM modeli (T-201).

Inventory, Ansible'ın hedef host ve gruplarını tanımlayan bir **dosyanın**
kaydıdır. Project gibi burada da dosya kopyalanmaz; yalnızca kaydı tutulur ve
path güvenliği uygulanır (MIMARI.md bölüm 5-6).

Dosyanın **içeriği** bu görevde okunmaz. Host/grup çıkarımı ve secret
maskeleme T-202'nin kapsamındadır; bu model yalnızca güvenli metadata tutar.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db.base import Base
from app.services.security.paths import normalize_filesystem_path

NAME_MAX_LENGTH = 200
PATH_MAX_LENGTH = 1024
SOURCE_TYPE_MAX_LENGTH = 16


class InventorySourceType(StrEnum):
    """Desteklenen inventory dosya biçimleri.

    MVP 1'de yalnızca dosya tabanlı INI ve YAML inventory'ler desteklenir
    (veri modeli sözleşmesi). Dinamik inventory script'leri bilinçli olarak kapsam
    dışıdır: çalıştırılabilir bir dosyayı inventory olarak kabul etmek,
    ürünün "arbitrary shell execution yok" ilkesini delerdi.
    """

    INI = "ini"
    YAML = "yaml"


# Değerler (``ini``/``yaml``) saklanır, üye adları (``INI``) değil. Bu, API
# sözleşmesiyle veritabanı içeriğini aynı hizada tutar. ``create_constraint``
# ile CHECK üretilir; geçersiz bir source_type ORM'i atlasa bile veritabanına
# yazılamaz.
SOURCE_TYPE_COLUMN_TYPE = Enum(
    InventorySourceType,
    name="inventory_source_type",
    native_enum=False,
    length=SOURCE_TYPE_MAX_LENGTH,
    create_constraint=True,
    validate_strings=True,
    values_callable=lambda enum_class: [member.value for member in enum_class],
)


def _utcnow() -> datetime:
    """Timezone bilgisi taşıyan geçerli UTC zamanı."""
    return datetime.now(UTC)


class Inventory(Base):
    """Kayıtlı bir Ansible inventory dosyası.

    ``project_id`` **nullable**'dır: bir inventory tek bir project'e bağlı
    olabileceği gibi (MIMARI.md bölüm 6) project'ten bağımsız ve yeniden
    kullanılabilir de olabilir.

    ``path`` her zaman normalize edilmiş (absolute, ``..`` ve symlink
    çözülmüş) hâlde saklanır. Project'te olduğu gibi bu, persistence
    sınırında ``@validates`` ile zorunlu kılınmıştır; ham bir yol modele
    girip veritabanına ulaşamaz.

    Aynı dosyanın birden fazla kez kaydedilmesi **engellenmez**: aynı
    inventory farklı project'lere bağlı veya farklı adlarla kayıtlı olabilir.
    Bu yüzden Project'teki ``path_key`` + unique index düzeni burada bilinçli
    olarak kullanılmaz (T-201 kapsam kararı).
    """

    __tablename__ = "inventories"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(
        Integer,
        # Project kayıtları soft delete edilir, fiziksel olarak silinmez;
        # RESTRICT bu beklentiyi şemada da yazılı hâle getirir.
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), nullable=False)
    path: Mapped[str] = mapped_column(String(PATH_MAX_LENGTH), nullable=False, index=True)
    source_type: Mapped[InventorySourceType] = mapped_column(
        SOURCE_TYPE_COLUMN_TYPE,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )

    @validates("path")
    def _normalize_path(self, _key: str, value: str) -> str:
        """Persistence sınırında path normalizasyonunu zorunlu kılar.

        Raises:
            InvalidPathError: Path normalize edilemezse.
        """
        return str(normalize_filesystem_path(value))

    def __repr__(self) -> str:
        return f"Inventory(id={self.id!r}, name={self.name!r})"
