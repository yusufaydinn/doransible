"""Playbook keşfi — servis katmanı ve kayıt sonrası güvenlik yeniden doğrulaması (T-103)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models import Project
from app.services.projects import (
    ProjectInactiveError,
    ProjectPathUnavailableError,
    ScanLimits,
    create_project,
    deactivate_project,
    list_project_playbooks,
)
from app.services.security.paths import PathNotAllowedError
from tests.support import link_directory

PLAYBOOK = "---\n- name: Ornek\n  hosts: all\n"
ROLE_TASKS = "---\n- name: Paket\n  ansible.builtin.apt:\n    name: nginx\n"


@pytest.fixture
def allowed_root(project_root: Path) -> Path:
    """conftest'teki izinli project kökünün yerel adı.

    Dizini burada ayrıca oluşturmayız: ``db_session`` artık ``settings``
    üzerinden aynı fixture'a bağlı ve iki farklı fixture'ın aynı dizini
    oluşturmaya çalışması çakışma üretirdi.
    """
    return project_root


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def make_project(session: Session, root: Path, name: str = "Web") -> Project:
    project_dir = root / "proje"
    project_dir.mkdir(exist_ok=True)
    return create_project(session, name=name, path=str(project_dir), allowed_roots=[root])


def discover(session: Session, project_id: int, root: Path) -> list[str]:
    result = list_project_playbooks(session, project_id, allowed_roots=[root], limits=ScanLimits())
    return [item.path for item in result.playbooks]


def test_playbooks_are_discovered_for_an_active_project(
    db_session: Session, allowed_root: Path
) -> None:
    project = make_project(db_session, allowed_root)
    write(Path(project.path) / "site.yml", PLAYBOOK)
    write(Path(project.path) / "roles" / "nginx" / "tasks" / "main.yml", ROLE_TASKS)

    assert discover(db_session, project.id, allowed_root) == ["site.yml"]


def test_unknown_project_raises_not_found(db_session: Session, allowed_root: Path) -> None:
    with pytest.raises(NotFoundError) as exc_info:
        discover(db_session, 4242, allowed_root)

    assert exc_info.value.status_code == 404


def test_inactive_project_is_rejected(db_session: Session, allowed_root: Path) -> None:
    project = make_project(db_session, allowed_root)
    write(Path(project.path) / "site.yml", PLAYBOOK)
    deactivate_project(db_session, project.id)

    with pytest.raises(ProjectInactiveError) as exc_info:
        discover(db_session, project.id, allowed_root)

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "project_inactive"


def test_deleted_project_directory_is_reported(db_session: Session, allowed_root: Path) -> None:
    project = make_project(db_session, allowed_root)
    Path(project.path).rmdir()

    with pytest.raises(ProjectPathUnavailableError) as exc_info:
        discover(db_session, project.id, allowed_root)

    assert exc_info.value.status_code == 409
    assert exc_info.value.details == {"project_id": project.id, "reason": "missing"}


def test_project_path_turned_into_a_file_is_reported(
    db_session: Session, allowed_root: Path
) -> None:
    project = make_project(db_session, allowed_root)
    path = Path(project.path)
    path.rmdir()
    path.write_text("artik bir dosya", encoding="utf-8")

    with pytest.raises(ProjectPathUnavailableError) as exc_info:
        discover(db_session, project.id, allowed_root)

    assert exc_info.value.details == {"project_id": project.id, "reason": "not_a_directory"}


def test_allowlist_is_revalidated_after_registration(
    db_session: Session, allowed_root: Path, tmp_path: Path
) -> None:
    """Kayıttan sonra allowlist daraltılırsa keşif reddedilir."""
    project = make_project(db_session, allowed_root)
    write(Path(project.path) / "site.yml", PLAYBOOK)
    baska_kok = tmp_path / "baska-kok"
    baska_kok.mkdir()

    with pytest.raises(PathNotAllowedError) as exc_info:
        list_project_playbooks(
            db_session, project.id, allowed_roots=[baska_kok], limits=ScanLimits()
        )

    assert exc_info.value.status_code == 403


def test_project_path_replaced_by_escaping_link_is_rejected(
    db_session: Session, allowed_root: Path, tmp_path: Path
) -> None:
    """Kayıt sonrası project dizini dışarı giden bir bağlantıyla değiştirilirse
    keşif çalışmaz; veritabanındaki path'e körü körüne güvenilmez."""
    project = make_project(db_session, allowed_root)
    outside = tmp_path / "disarida"
    write(outside / "gizli.yml", PLAYBOOK)
    path = Path(project.path)
    path.rmdir()
    link_directory(path, outside)

    with pytest.raises(PathNotAllowedError):
        discover(db_session, project.id, allowed_root)


def test_escaping_link_inside_project_is_not_listed(
    db_session: Session, allowed_root: Path, tmp_path: Path
) -> None:
    project = make_project(db_session, allowed_root)
    write(Path(project.path) / "site.yml", PLAYBOOK)
    outside = tmp_path / "disarida"
    write(outside / "gizli.yml", PLAYBOOK)
    link_directory(Path(project.path) / "kacis", outside)

    found = discover(db_session, project.id, allowed_root)

    assert found == ["site.yml"]
    assert not any("gizli" in path for path in found)


def test_result_reports_project_id(db_session: Session, allowed_root: Path) -> None:
    project = make_project(db_session, allowed_root)
    write(Path(project.path) / "site.yml", PLAYBOOK)

    result = list_project_playbooks(
        db_session, project.id, allowed_roots=[allowed_root], limits=ScanLimits()
    )

    assert result.project_id == project.id
    assert result.truncated is False
    assert result.skipped_unreadable_files == 0
