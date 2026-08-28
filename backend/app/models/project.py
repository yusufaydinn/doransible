"""Project ORM modeli.

Project, bir Ansible dosya ağacının kökünü temsil eder. Uygulama project
dizinini kendi alanına kopyalamaz; yalnızca kaydeder ve path güvenliğini
uygular (MIMARI.md bölüm 5).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, String, Text, event, func, true
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, Mapper, mapped_column, validates

from app.db.base import Base
from app.services.security.paths import normalize_filesystem_path, path_comparison_key

NAME_MAX_LENGTH = 200
PATH_MAX_LENGTH = 1024
DESCRIPTION_MAX_LENGTH = 2000


def _utcnow() -> datetime:
    """Timezone bilgisi taşıyan geçerli UTC zamanı."""
    return datetime.now(UTC)


class Project(Base):
    """Kayıtlı bir Ansible project kökü.

    ``path`` her zaman normalize edilmiş (absolute, ``..`` çözülmüş) hâlde
    saklanır. Bu, persistence sınırında ``@validates`` ile **zorunlu**
    kılınmıştır: ``Project(path=...)`` veya sonradan yapılan bir atama ham
    yolu olduğu gibi saklayamaz, normalizasyondan geçmek zorundadır.

    ``path_key``, ``path``'ten **türetilen** karşılaştırma anahtarıdır ve
    unique index onun üzerindedir. Windows'ta case-insensitive dosya sistemi
    nedeniyle ``C:\\Projeler`` ile ``c:\\projeler`` aynı dizindir; anahtar
    bunu tek kayda indirger.

    ``path_key`` bağımsız olarak atanamaz: dışarıdan verilen değer yok
    sayılır ve her flush öncesinde ``path`` üzerinden yeniden türetilir.
    Böylece duplicate koruması zorlanmış bir anahtarla aşılamaz.
    """

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), nullable=False)
    path: Mapped[str] = mapped_column(
        String(PATH_MAX_LENGTH),
        nullable=False,
        index=True,
    )
    path_key: Mapped[str] = mapped_column(
        String(PATH_MAX_LENGTH),
        nullable=False,
        unique=True,
        index=True,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        # `true()` dialect'e göre render edilir: SQLite'ta 1, PostgreSQL'de true.
        server_default=true(),
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

        Hem ``Project(path=...)`` hem sonraki atamalar bu doğrulayıcıdan
        geçer; ham veya traversal içeren bir yol normalize edilmeden
        saklanamaz.

        Raises:
            InvalidPathError: Path normalize edilemezse.
        """
        return str(normalize_filesystem_path(value))

    @validates("path_key")
    def _ignore_external_path_key(self, _key: str, value: str) -> str:
        """``path_key`` türetilmiş alandır; dışarıdan verilen değeri yok sayar.

        ``path`` zaten atanmışsa anahtar ondan yeniden türetilir. ``path``
        henüz yoksa (örneğin ``Project(path_key=..., path=...)`` çağrısında
        anahtar önce geldiyse) değer geçici olarak kabul edilir; kesin
        türetme her hâlükârda flush öncesinde yapılır.
        """
        current_path = self.path
        if current_path is None:
            return value
        return path_comparison_key(current_path)

    def __repr__(self) -> str:
        return f"Project(id={self.id!r}, name={self.name!r})"


@event.listens_for(Project, "before_insert")
@event.listens_for(Project, "before_update")
def _enforce_derived_path_key(
    _mapper: Mapper[Project],
    _connection: Connection,
    target: Project,
) -> None:
    """Her INSERT/UPDATE öncesinde ``path_key``'i ``path``'ten yeniden türetir.

    Bu, duplicate korumasının tek güvence noktasıdır: çağıran taraf
    ``path_key``'i nasıl zorlarsa zorlasın, veritabanına yazılan değer
    daima normalize edilmiş ``path``'ten hesaplanır.
    """
    target.path_key = path_comparison_key(target.path)
