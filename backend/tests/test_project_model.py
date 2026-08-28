"""Project modeli ve migration'ı (T-101)."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models import Project
from app.services.security.paths import InvalidPathError, normalize_filesystem_path

EXPECTED_COLUMNS = {
    "id",
    "name",
    "path",
    "path_key",
    "description",
    "is_active",
    "created_at",
    "updated_at",
}


def test_migration_creates_projects_table(migrated_engine: Engine) -> None:
    inspector = inspect(migrated_engine)

    assert "projects" in inspector.get_table_names()
    columns = {column["name"] for column in inspector.get_columns("projects")}
    assert columns == EXPECTED_COLUMNS


def test_path_key_has_unique_index(migrated_engine: Engine) -> None:
    """Duplicate koruması veritabanı seviyesinde `path_key` üzerindedir."""
    indexes = inspect(migrated_engine).get_indexes("projects")
    key_indexes = [ix for ix in indexes if ix["column_names"] == ["path_key"]]

    assert key_indexes, "path_key sütununda index bulunamadı"
    assert all(ix["unique"] for ix in key_indexes)


def test_models_and_migrations_do_not_drift(migrated_engine: Engine) -> None:
    """Migration zinciri ile ORM modelleri aynı şemayı tanımlamalıdır."""
    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        differences = compare_metadata(context, Base.metadata)

    assert differences == [], f"Model/migration farkı: {differences}"


def test_project_is_persisted_with_defaults(db_session: Session, tmp_path: Path) -> None:
    project = Project(name="Web sunuculari", path=str(tmp_path))
    db_session.add(project)
    db_session.commit()

    stored = db_session.get(Project, project.id)
    assert stored is not None
    assert stored.name == "Web sunuculari"
    assert stored.description is None
    assert stored.is_active is True
    assert isinstance(stored.created_at, datetime)
    assert isinstance(stored.updated_at, datetime)


def test_duplicate_path_is_rejected(db_session: Session, tmp_path: Path) -> None:
    """Aynı dizin iki kez kaydedilemez (T-101 kabul kriteri)."""
    path = str(normalize_filesystem_path(str(tmp_path)))
    db_session.add(Project(name="Birinci", path=path))
    db_session.commit()

    db_session.add(Project(name="Ikinci", path=path))

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_raw_traversal_path_is_normalized_at_persistence_boundary(
    db_session: Session, tmp_path: Path
) -> None:
    """Ham `/a/c/../b` doğrudan modele verilir; veritabanında `/a/b` saklanmalıdır.

    Bu, T-101'in çekirdek garantisidir: normalizasyon çağıranın nezaketine
    bırakılmaz, persistence sınırında zorunludur.
    """
    (tmp_path / "b").mkdir()
    (tmp_path / "c").mkdir()
    raw = str(tmp_path / "c" / ".." / "b")
    canonical = str((tmp_path / "b").resolve())
    assert ".." in raw

    db_session.add(Project(name="Ham yol", path=raw))
    db_session.commit()
    db_session.expire_all()

    stored_path, stored_key = db_session.execute(text("SELECT path, path_key FROM projects")).one()

    assert stored_path == canonical
    assert ".." not in stored_path
    assert stored_key == os.path.normcase(canonical)


def test_assignment_after_construction_is_also_normalized(
    db_session: Session, tmp_path: Path
) -> None:
    """Sonradan yapılan atama da sınırdan geçer."""
    (tmp_path / "bir").mkdir()
    (tmp_path / "iki").mkdir()
    project = Project(name="Proje", path=str(tmp_path / "bir"))
    db_session.add(project)
    db_session.commit()

    project.path = str(tmp_path / "bir" / ".." / "iki")
    db_session.commit()
    db_session.expire_all()

    stored_path, stored_key = db_session.execute(text("SELECT path, path_key FROM projects")).one()
    assert stored_path == str((tmp_path / "iki").resolve())
    assert stored_key == os.path.normcase(stored_path)


def test_invalid_path_is_rejected_at_persistence_boundary(tmp_path: Path) -> None:
    """Relative veya boş path modele hiç girmez."""
    with pytest.raises(InvalidPathError):
        Project(name="Gecersiz", path="relative/path")

    with pytest.raises(InvalidPathError):
        Project(name="Gecersiz", path="   ")


def test_forced_path_key_in_constructor_cannot_bypass_duplicate_protection(
    db_session: Session, tmp_path: Path
) -> None:
    """Constructor yolu: farklı zorlanmış path_key duplicate korumasını aşamaz."""
    canonical = str(normalize_filesystem_path(str(tmp_path)))

    db_session.add(Project(name="Bir", path=canonical, path_key="zorlanmis-1"))
    db_session.commit()

    db_session.add(Project(name="Iki", path=canonical, path_key="zorlanmis-2"))

    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    stored = db_session.execute(text("SELECT name, path_key FROM projects")).all()
    assert stored == [("Bir", os.path.normcase(canonical))]


def test_forced_path_key_before_path_in_constructor_is_also_overridden(
    db_session: Session, tmp_path: Path
) -> None:
    """Kwarg sırası path_key'i önce koysa bile flush öncesi türetme kazanır."""
    canonical = str(normalize_filesystem_path(str(tmp_path)))

    first = Project(path_key="zorlanmis-1", name="Bir", path=canonical)
    db_session.add(first)
    db_session.commit()

    second = Project(path_key="zorlanmis-2", name="Iki", path=canonical)
    db_session.add(second)

    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    stored = db_session.execute(text("SELECT path_key FROM projects")).all()
    assert stored == [(os.path.normcase(canonical),)]


def test_forced_path_key_after_construction_cannot_bypass_duplicate_protection(
    db_session: Session, tmp_path: Path
) -> None:
    """Sonradan atama yolu: path_key elle değiştirilse de ikinci kayıt saklanamaz."""
    canonical = str(normalize_filesystem_path(str(tmp_path)))

    first = Project(name="Bir", path=canonical)
    first.path_key = "zorlanmis-1"
    db_session.add(first)
    db_session.commit()

    second = Project(name="Iki", path=canonical)
    second.path_key = "zorlanmis-2"
    db_session.add(second)

    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    stored = db_session.execute(text("SELECT name, path_key FROM projects")).all()
    assert stored == [("Bir", os.path.normcase(canonical))]


def test_forcing_path_key_on_existing_row_is_reverted_on_update(
    db_session: Session, tmp_path: Path
) -> None:
    """Kayıtlı bir satırda path_key zorlanırsa UPDATE sırasında geri türetilir."""
    canonical = str(normalize_filesystem_path(str(tmp_path)))
    project = Project(name="Proje", path=canonical)
    db_session.add(project)
    db_session.commit()

    project.path_key = "zorlanmis"
    project.description = "guncelleme tetikleyici"
    db_session.commit()
    db_session.expire_all()

    (stored_key,) = db_session.execute(text("SELECT path_key FROM projects")).one()
    assert stored_key == os.path.normcase(canonical)


def test_paths_differing_only_by_traversal_collide_after_normalization(
    db_session: Session, tmp_path: Path
) -> None:
    """`/a/b` ve `/a/c/../b` aynı dizindir; ham hâlleriyle bile çakışmalıdır."""
    (tmp_path / "b").mkdir()
    (tmp_path / "c").mkdir()

    db_session.add(Project(name="Birinci", path=str(tmp_path / "b")))
    db_session.commit()
    db_session.add(Project(name="Ikinci", path=str(tmp_path / "c" / ".." / "b")))

    with pytest.raises(IntegrityError):
        db_session.commit()


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows dosya sistemi case-insensitive'dir; POSIX'te farklı casing farklı dizindir.",
)
def test_windows_case_variants_are_treated_as_duplicate(
    db_session: Session, tmp_path: Path
) -> None:
    """Windows'ta aynı dizin farklı casing ile iki kez kaydedilemez."""
    target = tmp_path / "Projeler"
    target.mkdir()

    db_session.add(Project(name="Birinci", path=str(target)))
    db_session.commit()
    db_session.add(Project(name="Ikinci", path=str(target).upper()))

    with pytest.raises(IntegrityError):
        db_session.commit()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX'te farklı casing gerçekten farklı dizindir.",
)
def test_posix_case_variants_are_distinct_projects(db_session: Session, tmp_path: Path) -> None:
    """POSIX'te `/a/Proje` ve `/a/proje` ayrı dizinlerdir, ikisi de kaydedilebilir."""
    upper = tmp_path / "Proje"
    lower = tmp_path / "proje"
    upper.mkdir()
    lower.mkdir()

    db_session.add(Project(name="Buyuk", path=str(upper)))
    db_session.add(Project(name="Kucuk", path=str(lower)))
    db_session.commit()

    assert db_session.query(Project).count() == 2


def test_different_paths_are_allowed(db_session: Session, tmp_path: Path) -> None:
    (tmp_path / "bir").mkdir()
    (tmp_path / "iki").mkdir()

    db_session.add(Project(name="Bir", path=str(tmp_path / "bir")))
    db_session.add(Project(name="Iki", path=str(tmp_path / "iki")))
    db_session.commit()

    assert db_session.query(Project).count() == 2


def test_project_can_be_deactivated(db_session: Session, tmp_path: Path) -> None:
    project = Project(name="Pasif", path=str(tmp_path))
    db_session.add(project)
    db_session.commit()

    project.is_active = False
    db_session.commit()

    stored = db_session.get(Project, project.id)
    assert stored is not None
    assert stored.is_active is False
