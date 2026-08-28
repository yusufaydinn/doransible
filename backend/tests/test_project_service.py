"""Project servisi (T-102).

Buradaki testler HTTP katmanı olmadan çalışır: iş mantığının route'ta değil
servis katmanında olduğunun da kanıtıdır.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models import Project
from app.services.projects import (
    ProjectAlreadyExistsError,
    create_project,
    deactivate_project,
    get_project,
    list_projects,
)
from app.services.security.paths import (
    InvalidPathError,
    PathIsNotADirectoryError,
    PathNotAllowedError,
    PathNotFoundError,
)
from tests.support import link_directory


@pytest.fixture
def allowed_root(project_root: Path) -> Path:
    """conftest'teki izinli project kökünün yerel adı.

    Dizini burada ayrıca oluşturmayız: ``db_session`` artık ``settings``
    üzerinden aynı fixture'a bağlı ve iki farklı fixture'ın aynı dizini
    oluşturmaya çalışması çakışma üretirdi.
    """
    return project_root


def test_project_is_created_with_canonical_path(db_session: Session, allowed_root: Path) -> None:
    (allowed_root / "web").mkdir()

    project = create_project(
        db_session,
        name="  Web sunuculari  ",
        path=str(allowed_root / "web"),
        description="Nginx",
        allowed_roots=[allowed_root],
    )

    assert project.id is not None
    assert project.name == "Web sunuculari"
    assert project.path == str((allowed_root / "web").resolve())
    assert project.is_active is True


def test_raw_traversal_inside_root_is_normalized(db_session: Session, allowed_root: Path) -> None:
    """`root/c/../web` root içinde kalır; kanonik hâliyle saklanmalıdır."""
    (allowed_root / "web").mkdir()
    (allowed_root / "c").mkdir()

    project = create_project(
        db_session,
        name="Web",
        path=str(allowed_root / "c" / ".." / "web"),
        allowed_roots=[allowed_root],
    )

    assert project.path == str((allowed_root / "web").resolve())
    assert ".." not in project.path


def test_path_outside_allowed_root_is_rejected(
    db_session: Session, allowed_root: Path, tmp_path: Path
) -> None:
    """İzin verilen root'un dışındaki mevcut bir dizin bile reddedilir."""
    outside = tmp_path / "disarida"
    outside.mkdir()

    with pytest.raises(PathNotAllowedError) as exc_info:
        create_project(db_session, name="Disarida", path=str(outside), allowed_roots=[allowed_root])

    assert exc_info.value.status_code == 403
    assert db_session.query(Project).count() == 0


def test_traversal_out_of_allowed_root_is_rejected(
    db_session: Session, allowed_root: Path, tmp_path: Path
) -> None:
    """`root/../disarida` normalizasyondan sonra root dışına düşer."""
    outside = tmp_path / "disarida"
    outside.mkdir()
    traversal = allowed_root / ".." / "disarida"

    with pytest.raises(PathNotAllowedError):
        create_project(
            db_session, name="Traversal", path=str(traversal), allowed_roots=[allowed_root]
        )


def test_sibling_directory_with_shared_prefix_is_rejected(
    db_session: Session, tmp_path: Path
) -> None:
    """`/x/ansible` root'u `/x/ansible-evil` yolunu kapsamamalıdır.

    String prefix karşılaştırması bu senaryoda yanlış biçimde izin verirdi.
    """
    root = tmp_path / "ansible"
    evil = tmp_path / "ansible-evil"
    root.mkdir()
    evil.mkdir()

    with pytest.raises(PathNotAllowedError):
        create_project(db_session, name="Evil", path=str(evil), allowed_roots=[root])


def test_symlink_escaping_allowed_root_is_rejected(
    db_session: Session, allowed_root: Path, tmp_path: Path
) -> None:
    """Root içindeki bir bağlantı dışarıyı gösteriyorsa kayıt reddedilir.

    GUVENLIK.md bölüm 15'teki "symlink escape" senaryosu. Bağlantı root'un
    altında olduğu için yüzeysel bir kontrol izin verirdi; `resolve()`
    sonrası gerçek hedef root dışındadır.
    """
    outside = tmp_path / "gizli"
    outside.mkdir()
    (outside / "id_rsa").write_text("gizli-anahtar", encoding="utf-8")
    link = link_directory(allowed_root / "gorunuste-icerde", outside)

    with pytest.raises(PathNotAllowedError):
        create_project(db_session, name="Kacis", path=str(link), allowed_roots=[allowed_root])

    assert db_session.query(Project).count() == 0


def test_symlink_staying_inside_allowed_root_is_accepted(
    db_session: Session, allowed_root: Path
) -> None:
    """Bağlantı root içinde kalıyorsa kabul edilir ve hedefe çözülür.

    Kontrolün "her bağlantıyı reddet" gibi kaba bir kural olmadığını gösterir.
    """
    target = allowed_root / "gercek"
    target.mkdir()
    link = link_directory(allowed_root / "kisayol", target)

    project = create_project(
        db_session, name="Kisayol", path=str(link), allowed_roots=[allowed_root]
    )

    assert project.path == str(target.resolve())


def test_missing_path_is_rejected(db_session: Session, allowed_root: Path) -> None:
    with pytest.raises(PathNotFoundError) as exc_info:
        create_project(
            db_session,
            name="Yok",
            path=str(allowed_root / "hic-olmayan"),
            allowed_roots=[allowed_root],
        )

    assert exc_info.value.status_code == 422


def test_file_path_is_rejected(db_session: Session, allowed_root: Path) -> None:
    target = allowed_root / "playbook.yml"
    target.write_text("- hosts: all", encoding="utf-8")

    with pytest.raises(PathIsNotADirectoryError):
        create_project(db_session, name="Dosya", path=str(target), allowed_roots=[allowed_root])


def test_allowlist_is_checked_before_existence(
    db_session: Session, allowed_root: Path, tmp_path: Path
) -> None:
    """Root dışındaki var olmayan path de 403 döner, 404/422 değil.

    Aksi hâlde endpoint dosya sistemi sondasına dönüşür: saldırgan cevaba
    bakarak izinsiz bir dizinin var olup olmadığını öğrenirdi.
    """
    with pytest.raises(PathNotAllowedError):
        create_project(
            db_session,
            name="Sonda",
            path=str(tmp_path / "kesinlikle-yok"),
            allowed_roots=[allowed_root],
        )


def test_empty_allowlist_rejects_everything(db_session: Session, allowed_root: Path) -> None:
    """Boş allowlist "her şey serbest" değil, fail-closed anlamına gelir."""
    with pytest.raises(PathNotAllowedError):
        create_project(db_session, name="Bos", path=str(allowed_root), allowed_roots=[])


def test_relative_path_is_rejected(db_session: Session, allowed_root: Path) -> None:
    with pytest.raises(InvalidPathError):
        create_project(
            db_session, name="Relative", path="projeler/web", allowed_roots=[allowed_root]
        )


def test_multiple_allowed_roots_are_supported(db_session: Session, tmp_path: Path) -> None:
    first = tmp_path / "kok-bir"
    second = tmp_path / "kok-iki"
    first.mkdir()
    second.mkdir()
    (second / "web").mkdir()

    project = create_project(
        db_session, name="Web", path=str(second / "web"), allowed_roots=[first, second]
    )

    assert project.path == str((second / "web").resolve())


def test_allowed_root_itself_can_be_registered(db_session: Session, allowed_root: Path) -> None:
    project = create_project(
        db_session, name="Kok", path=str(allowed_root), allowed_roots=[allowed_root]
    )

    assert project.path == str(allowed_root.resolve())


def test_duplicate_path_raises_conflict(db_session: Session, allowed_root: Path) -> None:
    (allowed_root / "web").mkdir()
    create_project(
        db_session, name="Bir", path=str(allowed_root / "web"), allowed_roots=[allowed_root]
    )

    with pytest.raises(ProjectAlreadyExistsError) as exc_info:
        create_project(
            db_session, name="Iki", path=str(allowed_root / "web"), allowed_roots=[allowed_root]
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.details == {"project_id": 1, "is_active": True}
    assert db_session.query(Project).count() == 1


def test_duplicate_is_detected_across_traversal_spellings(
    db_session: Session, allowed_root: Path
) -> None:
    (allowed_root / "web").mkdir()
    (allowed_root / "c").mkdir()
    create_project(
        db_session, name="Bir", path=str(allowed_root / "web"), allowed_roots=[allowed_root]
    )

    with pytest.raises(ProjectAlreadyExistsError):
        create_project(
            db_session,
            name="Iki",
            path=str(allowed_root / "c" / ".." / "web"),
            allowed_roots=[allowed_root],
        )


def test_integrity_error_is_translated_to_conflict(
    db_session: Session, allowed_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ön kontrolü atlatan eşzamanlı kayıt da 409 üretmelidir.

    Unique index son savunmadır; onun ürettiği IntegrityError kullanıcıya
    500 olarak değil anlaşılır bir çakışma olarak dönmelidir.
    """
    (allowed_root / "web").mkdir()
    create_project(
        db_session, name="Bir", path=str(allowed_root / "web"), allowed_roots=[allowed_root]
    )

    from app.services.projects import service as project_service

    real_find = project_service.find_project_by_path
    calls: list[int] = []

    def _blind_first_call(session: Session, normalized_path: Path) -> Project | None:
        calls.append(1)
        # İlk çağrı (ön kontrol) kaydı görmez; commit sırasında unique index yakalar.
        if len(calls) == 1:
            return None
        return real_find(session, normalized_path)

    monkeypatch.setattr(project_service, "find_project_by_path", _blind_first_call)

    with pytest.raises(ProjectAlreadyExistsError):
        project_service.create_project(
            db_session, name="Iki", path=str(allowed_root / "web"), allowed_roots=[allowed_root]
        )

    db_session.rollback()
    assert db_session.query(Project).count() == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows case-insensitive dosya sistemi")
def test_windows_case_variant_is_duplicate(db_session: Session, allowed_root: Path) -> None:
    (allowed_root / "Web").mkdir()
    create_project(
        db_session, name="Bir", path=str(allowed_root / "Web"), allowed_roots=[allowed_root]
    )

    with pytest.raises(ProjectAlreadyExistsError):
        create_project(
            db_session,
            name="Iki",
            path=str(allowed_root / "Web").upper(),
            allowed_roots=[allowed_root],
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows case-insensitive allowlist")
def test_allowlist_comparison_is_case_insensitive_on_windows(
    db_session: Session, allowed_root: Path
) -> None:
    """Root farklı casing ile yapılandırılsa bile path kabul edilmelidir."""
    (allowed_root / "web").mkdir()

    project = create_project(
        db_session,
        name="Web",
        path=str(allowed_root / "web"),
        allowed_roots=[Path(str(allowed_root).upper())],
    )

    assert os.path.normcase(project.path) == os.path.normcase(str((allowed_root / "web").resolve()))


def test_list_hides_inactive_projects_by_default(db_session: Session, allowed_root: Path) -> None:
    (allowed_root / "bir").mkdir()
    (allowed_root / "iki").mkdir()
    first = create_project(
        db_session, name="Bir", path=str(allowed_root / "bir"), allowed_roots=[allowed_root]
    )
    create_project(
        db_session, name="Iki", path=str(allowed_root / "iki"), allowed_roots=[allowed_root]
    )

    deactivate_project(db_session, first.id)

    assert [p.name for p in list_projects(db_session)] == ["Iki"]
    assert [p.name for p in list_projects(db_session, include_inactive=True)] == ["Bir", "Iki"]


def test_get_project_raises_not_found(db_session: Session) -> None:
    with pytest.raises(NotFoundError) as exc_info:
        get_project(db_session, 4242)

    assert exc_info.value.status_code == 404


def test_deactivate_does_not_touch_the_filesystem(db_session: Session, allowed_root: Path) -> None:
    """DELETE davranışı: kayıt pasifleşir, project dosyaları diskte kalır."""
    project_dir = allowed_root / "web"
    project_dir.mkdir()
    playbook = project_dir / "site.yml"
    playbook.write_text("- hosts: all", encoding="utf-8")
    project = create_project(
        db_session, name="Web", path=str(project_dir), allowed_roots=[allowed_root]
    )

    deactivated = deactivate_project(db_session, project.id)

    assert deactivated.is_active is False
    assert db_session.get(Project, project.id) is not None
    assert project_dir.is_dir()
    assert playbook.read_text(encoding="utf-8") == "- hosts: all"


def test_deactivate_is_idempotent(db_session: Session, allowed_root: Path) -> None:
    project = create_project(
        db_session, name="Web", path=str(allowed_root), allowed_roots=[allowed_root]
    )

    deactivate_project(db_session, project.id)
    first_updated_at = project.updated_at
    again = deactivate_project(db_session, project.id)

    assert again.is_active is False
    assert again.updated_at == first_updated_at


def test_deactivate_raises_not_found(db_session: Session) -> None:
    with pytest.raises(NotFoundError):
        deactivate_project(db_session, 4242)


def test_inactive_project_still_blocks_reregistration(
    db_session: Session, allowed_root: Path
) -> None:
    """Pasif kayıt path'i serbest bırakmaz; kullanıcı bunu anlayabilmelidir."""
    project = create_project(
        db_session, name="Web", path=str(allowed_root), allowed_roots=[allowed_root]
    )
    deactivate_project(db_session, project.id)

    with pytest.raises(ProjectAlreadyExistsError) as exc_info:
        create_project(
            db_session, name="Yeniden", path=str(allowed_root), allowed_roots=[allowed_root]
        )

    assert exc_info.value.details == {"project_id": project.id, "is_active": False}
    assert "pasif" in exc_info.value.message
