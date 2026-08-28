"""Veritabanı bağlantı katmanının temel doğrulaması."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from app.db.session import create_db_engine, create_session_factory
from tests.support import make_settings


def test_sqlite_engine_creates_database_directory(tmp_path: Path) -> None:
    settings = make_settings(app_data_dir=tmp_path / "app-data", database_url=None)

    create_db_engine(settings)

    assert (settings.app_data_dir / "database").is_dir()


def test_session_can_execute_statement(tmp_path: Path) -> None:
    settings = make_settings(
        app_data_dir=tmp_path / "app-data",
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
    )
    factory = create_session_factory(settings)

    with factory() as session:
        assert session.execute(text("SELECT 1")).scalar_one() == 1


def test_sqlite_connections_enforce_foreign_keys(tmp_path: Path) -> None:
    """SQLite FK'leri varsayılan olarak uygulamaz; her bağlantıda açılmalıdır.

    PRAGMA bağlantı başınadır: pool'dan gelen ikinci bir bağlantı da açık
    olmalıdır, aksi hâlde koruma aralıklı çalışırdı.
    """
    settings = make_settings(
        app_data_dir=tmp_path / "app-data",
        database_url=f"sqlite:///{(tmp_path / 'fk.db').as_posix()}",
    )
    engine = create_db_engine(settings)

    try:
        for _ in range(2):
            with engine.connect() as connection:
                assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    finally:
        engine.dispose()


def test_session_factory_connections_enforce_foreign_keys(tmp_path: Path) -> None:
    """Servis katmanının kullandığı session'lar da korumayı taşır."""
    settings = make_settings(
        app_data_dir=tmp_path / "app-data",
        database_url=f"sqlite:///{(tmp_path / 'fk.db').as_posix()}",
    )
    factory = create_session_factory(settings)

    with factory() as session:
        assert session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
