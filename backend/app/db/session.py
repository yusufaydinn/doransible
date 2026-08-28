"""Engine ve session yönetimi.

Servis katmanı session'ı parametre olarak alır; route'lar ``get_session``
dependency'si üzerinden session sağlar (route/service katman ayrımı sözleşmesi).
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry

from app.core.config import Settings, ensure_app_data_dirs, get_settings


def create_db_engine(settings: Settings) -> Engine:
    """Ayarlara uygun bir SQLAlchemy engine oluşturur.

    SQLite kullanıldığında üç şey yapılır:

    1. Veritabanı dosyasının dizini oluşturulur.
    2. FastAPI'nin threadpool'undan gelen erişim için ``check_same_thread``
       kapatılır.
    3. Her bağlantıda ``PRAGMA foreign_keys=ON`` çalıştırılır.

    Üçüncüsü zorunludur: SQLite foreign key kısıtlarını **varsayılan olarak
    uygulamaz** ve bağlantı başına açılması gerekir. PRAGMA olmadan
    ``inventories.project_id`` şemada FK olarak dursa da olmayan bir project'e
    işaret eden satır yazılabilir; kısıt yalnızca belge değeri taşırdı.

    PostgreSQL DSN'i verildiğinde bu özel ayarların hiçbiri uygulanmaz;
    PostgreSQL FK'leri zaten koşulsuz uygular (ADR-004 uyumluluk sınırı).
    """
    url = settings.resolve_database_url()
    connect_args: dict[str, Any] = {}
    is_sqlite = url.startswith("sqlite")
    if is_sqlite:
        ensure_app_data_dirs(settings)
        connect_args["check_same_thread"] = False

    engine = create_engine(url, connect_args=connect_args, future=True)
    if is_sqlite:
        enable_sqlite_foreign_keys(engine)
    return engine


def enable_sqlite_foreign_keys(engine: Engine) -> None:
    """Engine'in açtığı her SQLite bağlantısında FK uygulamasını açar.

    Pool bir bağlantıyı yeniden kullandığında PRAGMA korunur; bu yüzden
    dinlenen olay ``connect``'tir (gerçek DBAPI bağlantısı kurulduğu an),
    ``checkout`` değil.
    """

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(
        dbapi_connection: DBAPIConnection,
        _connection_record: ConnectionPoolEntry,
    ) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


def create_session_factory(settings: Settings) -> sessionmaker[Session]:
    """Verilen ayarlar için yeni bir session factory üretir."""
    engine = create_db_engine(settings)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    """Süreç ömrü boyunca tek bir session factory döndürür."""
    return create_session_factory(get_settings())


def get_session() -> Iterator[Session]:
    """FastAPI dependency: istek başına bir veritabanı session'ı sağlar."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
