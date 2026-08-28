"""Inventory modeli ve migration'ı (T-201)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import Base
from app.db.session import create_db_engine
from app.models import Inventory, InventorySourceType, Project
from app.services.security.paths import InvalidPathError
from tests.support import alembic_config

EXPECTED_COLUMNS = {
    "id",
    "project_id",
    "name",
    "path",
    "source_type",
    "created_at",
    "updated_at",
}


def test_migration_creates_inventories_table(migrated_engine: Engine) -> None:
    """Şema `create_all` ile değil gerçek migration zinciriyle kurulur."""
    inspector = inspect(migrated_engine)

    assert "inventories" in inspector.get_table_names()
    columns = {column["name"] for column in inspector.get_columns("inventories")}
    assert columns == EXPECTED_COLUMNS


def test_models_and_migrations_do_not_drift(migrated_engine: Engine) -> None:
    """Migration zinciri ile ORM modelleri aynı şemayı tanımlamalıdır.

    Project için aynı kontrol `test_project_model.py` içinde de vardır; burada
    Inventory tablosu eklendikten sonra farkın hâlâ boş olduğu doğrulanır.
    """
    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        differences = compare_metadata(context, Base.metadata)

    assert differences == [], f"Model/migration farkı: {differences}"


def test_migration_round_trip_restores_the_same_schema(settings: Settings) -> None:
    """`up → down → up` şemayı aynı yere getirmelidir.

    Downgrade'in çalışmaması, ileride bir migration'ı geri almanın imkânsız
    olduğunu ancak üretimde fark etmek anlamına gelirdi.
    """
    config = alembic_config(settings.resolve_database_url())
    command.upgrade(config, "head")

    engine = create_db_engine(settings)
    try:
        before = _schema_snapshot(engine)
        command.downgrade(config, "0002_create_projects_table")

        assert "inventories" not in inspect(engine).get_table_names()
        assert "projects" in inspect(engine).get_table_names()

        command.upgrade(config, "head")
        assert _schema_snapshot(engine) == before
    finally:
        engine.dispose()


def _schema_snapshot(engine: Engine) -> dict[str, object]:
    """Karşılaştırılabilir bir şema özeti üretir."""
    inspector = inspect(engine)
    return {
        "columns": {
            (column["name"], str(column["type"]), column["nullable"])
            for column in inspector.get_columns("inventories")
        },
        "indexes": {
            (index["name"], tuple(index["column_names"]), index["unique"])
            for index in inspector.get_indexes("inventories")
        },
        "foreign_keys": {
            (fk["referred_table"], tuple(fk["constrained_columns"]))
            for fk in inspector.get_foreign_keys("inventories")
        },
    }


def test_project_id_is_nullable_and_foreign_key(migrated_engine: Engine) -> None:
    """Inventory bir project'e bağlı olabilir ama zorunlu değildir."""
    inspector = inspect(migrated_engine)
    project_column = next(
        column for column in inspector.get_columns("inventories") if column["name"] == "project_id"
    )
    foreign_keys = inspector.get_foreign_keys("inventories")

    assert project_column["nullable"] is True
    assert any(
        fk["referred_table"] == "projects" and fk["constrained_columns"] == ["project_id"]
        for fk in foreign_keys
    )


def test_path_has_no_unique_index(migrated_engine: Engine) -> None:
    """Aynı dosya birden çok kez kaydedilebilir; Project'teki unique düzeni yoktur.

    Bu bilinçli bir kapsam kararıdır: aynı inventory farklı project'lere bağlı
    veya farklı adlarla kayıtlı olabilir.
    """
    indexes = inspect(migrated_engine).get_indexes("inventories")

    assert not [ix for ix in indexes if ix["unique"]]


@pytest.mark.parametrize("source_type", [InventorySourceType.INI, InventorySourceType.YAML])
def test_inventory_is_persisted_with_defaults(
    db_session: Session, tmp_path: Path, source_type: InventorySourceType
) -> None:
    inventory = Inventory(name="Lab", path=str(tmp_path / "hosts"), source_type=source_type)
    db_session.add(inventory)
    db_session.commit()

    stored = db_session.get(Inventory, inventory.id)
    assert stored is not None
    assert stored.project_id is None
    assert stored.source_type is source_type
    assert isinstance(stored.created_at, datetime)
    assert isinstance(stored.updated_at, datetime)


def test_source_type_is_stored_as_its_value_not_member_name(
    db_session: Session, tmp_path: Path
) -> None:
    """Veritabanında `ini` yazar, `INI` değil; API sözleşmesiyle aynı hizada kalır."""
    db_session.add(
        Inventory(name="Lab", path=str(tmp_path / "hosts"), source_type=InventorySourceType.INI)
    )
    db_session.commit()

    (stored,) = db_session.execute(text("SELECT source_type FROM inventories")).one()
    assert stored == "ini"


def test_unknown_source_type_is_rejected_by_database_check(
    db_session: Session, tmp_path: Path
) -> None:
    """ORM atlansa bile geçersiz bir source_type veritabanına yazılamaz."""
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                "INSERT INTO inventories (name, path, source_type, created_at, updated_at) "
                "VALUES ('Kotu', :path, 'dynamic', :now, :now)"
            ),
            {"path": str(tmp_path / "hosts"), "now": "2026-07-29 00:00:00"},
        )
    db_session.rollback()


def test_raw_traversal_path_is_normalized_at_persistence_boundary(
    db_session: Session, tmp_path: Path
) -> None:
    """Ham `/a/c/../hosts` doğrudan modele verilir; `/a/hosts` saklanmalıdır."""
    (tmp_path / "c").mkdir()
    raw = str(tmp_path / "c" / ".." / "hosts.ini")
    canonical = str((tmp_path / "hosts.ini").resolve())
    assert ".." in raw

    db_session.add(Inventory(name="Ham yol", path=raw, source_type=InventorySourceType.INI))
    db_session.commit()
    db_session.expire_all()

    (stored_path,) = db_session.execute(text("SELECT path FROM inventories")).one()
    assert stored_path == canonical
    assert ".." not in stored_path


def test_invalid_path_is_rejected_at_persistence_boundary() -> None:
    """Relative veya boş path modele hiç girmez."""
    with pytest.raises(InvalidPathError):
        Inventory(name="Gecersiz", path="relative/hosts", source_type=InventorySourceType.INI)

    with pytest.raises(InvalidPathError):
        Inventory(name="Gecersiz", path="   ", source_type=InventorySourceType.INI)


def test_foreign_keys_are_enforced_on_the_real_application_engine(
    migrated_engine: Engine,
) -> None:
    """SQLite FK kısıtlarını varsayılan olarak uygulamaz; PRAGMA açık olmalıdır.

    Engine, uygulamanın kendi ``create_db_engine`` yolundan üretilir; bu test
    bir fixture'a özel ayarı değil gerçek çalışma yolunu ölçer.
    """
    with migrated_engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_unknown_project_id_is_rejected_by_foreign_key(db_session: Session, tmp_path: Path) -> None:
    """ORM atlansa bile olmayan bir project'e işaret eden satır yazılamaz."""
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                "INSERT INTO inventories "
                "(project_id, name, path, source_type, created_at, updated_at) "
                "VALUES (4242, 'Hayalet', :path, 'ini', :now, :now)"
            ),
            {"path": str(tmp_path / "hosts.ini"), "now": "2026-07-29 00:00:00"},
        )
    db_session.rollback()


def test_null_project_id_is_allowed_by_foreign_key(db_session: Session, tmp_path: Path) -> None:
    """FK açıkken standalone kayıt (project_id NULL) hâlâ yazılabilmelidir."""
    db_session.execute(
        text(
            "INSERT INTO inventories "
            "(project_id, name, path, source_type, created_at, updated_at) "
            "VALUES (NULL, 'Serbest', :path, 'ini', :now, :now)"
        ),
        {"path": str(tmp_path / "hosts.ini"), "now": "2026-07-29 00:00:00"},
    )
    db_session.commit()

    assert db_session.query(Inventory).count() == 1


def test_inventory_can_be_linked_to_a_project(db_session: Session, tmp_path: Path) -> None:
    project = Project(name="Web", path=str(tmp_path))
    db_session.add(project)
    db_session.commit()

    inventory = Inventory(
        name="Lab",
        path=str(tmp_path / "hosts.ini"),
        source_type=InventorySourceType.INI,
        project_id=project.id,
    )
    db_session.add(inventory)
    db_session.commit()

    stored = db_session.get(Inventory, inventory.id)
    assert stored is not None
    assert stored.project_id == project.id


def test_same_file_can_be_registered_twice(db_session: Session, tmp_path: Path) -> None:
    """T-201 kapsamında duplicate koruması yoktur; bu davranış kayıt altındadır."""
    path = str(tmp_path / "hosts.ini")

    db_session.add(Inventory(name="Bir", path=path, source_type=InventorySourceType.INI))
    db_session.add(Inventory(name="Iki", path=path, source_type=InventorySourceType.YAML))
    db_session.commit()

    assert db_session.query(Inventory).count() == 2
