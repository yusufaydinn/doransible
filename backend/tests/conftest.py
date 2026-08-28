"""Ortak test fixture'ları.

Test'ler gerçek ``app-data`` dizinine dokunmaz; her test izole bir tmp
dizini üzerinde çalışır.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import create_db_engine, get_session
from app.main import create_app
from tests.support import alembic_config, make_settings


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """İzin verilen tek project root'u.

    API testleri bu dizinin altına gerçek klasörler açar; dışında kalan her
    path allowlist tarafından reddedilmelidir.
    """
    root = tmp_path / "izinli-kok"
    root.mkdir()
    return root


@pytest.fixture
def inventory_root(tmp_path: Path) -> Path:
    """İzin verilen tek inventory root'u.

    Project root'undan bilinçli olarak **ayrıdır** (ADR-015 revizyonu): bir
    testin project allowlist'i üzerinden standalone inventory kaydedebilmesi
    ayrımın gerçekten uygulandığını gizlerdi.
    """
    root = tmp_path / "izinli-inventory-kok"
    root.mkdir()
    return root


@pytest.fixture
def secrets_root(tmp_path: Path) -> Path:
    """İzin verilen tek SSH private key kökü (T-204A).

    Project ve inventory köklerinden **ayrıdır**: bir inventory'nin yanında
    duran her dosyanın kendiliğinden kullanılabilir bir SSH anahtarı sayılması
    istenmez.
    """
    root = tmp_path / "izinli-secrets-kok"
    root.mkdir()
    return root


@pytest.fixture
def settings(
    tmp_path: Path, project_root: Path, inventory_root: Path, secrets_root: Path
) -> Settings:
    """İzole bir app-data dizini kullanan test ayarları."""
    return make_settings(
        environment="test",
        app_data_dir=tmp_path / "app-data",
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        cors_origins=["http://localhost:5173"],
        project_root_allowlist=[project_root],
        inventory_root_allowlist=[inventory_root],
        ssh_key_root_allowlist=[secrets_root],
    )


@pytest.fixture
def client(settings: Settings, migrated_engine: Engine) -> Iterator[TestClient]:
    """Test ayarlarıyla yapılandırılmış bir HTTP client.

    Veritabanı bağımlılığı da migration uygulanmış izole veritabanına
    yönlendirilir; testler gerçek ``app-data`` dizinine dokunmaz.
    """
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings

    def _override_session() -> Iterator[Session]:
        with Session(migrated_engine, expire_on_commit=False) as session:
            yield session

    app.dependency_overrides[get_session] = _override_session

    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def migrated_engine(settings: Settings) -> Iterator[Engine]:
    """Alembic migration'ları uygulanmış, izole bir SQLite veritabanı.

    İki şey bilinçlidir:

    - Şema ``create_all`` ile değil gerçek migration zinciriyle kurulur;
      böylece testler migration'ların çalıştığını da doğrular.
    - Engine ``create_engine`` ile elle değil uygulamanın kendi
      :func:`create_db_engine` yolundan üretilir. Aksi hâlde testler,
      ``PRAGMA foreign_keys=ON`` gibi yalnızca o yolda uygulanan davranışları
      hiç görmezdi.
    """
    command.upgrade(alembic_config(settings.resolve_database_url()), "head")

    engine = create_db_engine(settings)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_session(migrated_engine: Engine) -> Iterator[Session]:
    """Migration uygulanmış veritabanına bağlı bir ORM session'ı."""
    with Session(migrated_engine, expire_on_commit=False) as session:
        yield session
