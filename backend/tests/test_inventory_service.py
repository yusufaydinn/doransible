"""Inventory servisi (T-201).

API testleri HTTP sözleşmesini doğrular; bu dosya servis katmanının kendi
kararlarını HTTP olmadan ölçer. Böylece bir kontrolün route'a mı yoksa servise
mi ait olduğu karışmaz (route/service katman ayrımı sözleşmesi).

Servis iki ayrı sınır uygular (ADR-015): standalone inventory
``inventory_roots`` altında, project'e bağlı inventory ise project'in kendi
kökü altında olmalıdır.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models import InventorySourceType, Project
from app.services.inventories import (
    InventoryOutsideProjectError,
    create_inventory,
    get_inventory,
    list_inventories,
)
from app.services.projects.service import ProjectInactiveError
from app.services.security.paths import (
    InvalidPathError,
    PathIsNotAFileError,
    PathNotAllowedError,
    PathNotFoundError,
)
from tests.support import link_directory


def _inventory_file(root: Path, name: str = "hosts.ini") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    target = root / name
    target.write_text("[web]\nweb01\n", encoding="utf-8")
    return target


def _add_project(session: Session, path: Path, *, is_active: bool = True) -> Project:
    path.mkdir(parents=True, exist_ok=True)
    project = Project(name="Web", path=str(path))
    project.is_active = is_active
    session.add(project)
    session.commit()
    return project


# --- Standalone akışı --------------------------------------------------------


def test_standalone_inventory_is_created_and_normalized(
    db_session: Session, tmp_path: Path
) -> None:
    inventory_root = tmp_path / "envanterler"
    target = _inventory_file(inventory_root)

    inventory = create_inventory(
        db_session,
        name="  Lab  ",
        path=str(inventory_root / "." / "hosts.ini"),
        source_type=InventorySourceType.INI,
        inventory_roots=[inventory_root],
        project_roots=[tmp_path / "projeler"],
    )

    assert inventory.id is not None
    assert inventory.name == "Lab"
    assert inventory.path == str(target.resolve())
    assert inventory.project_id is None


def test_empty_inventory_allowlist_is_fail_closed(db_session: Session, tmp_path: Path) -> None:
    """Inventory root tanımlı değilse hiçbir standalone kayıt kabul edilmez."""
    target = _inventory_file(tmp_path / "envanterler")

    with pytest.raises(PathNotAllowedError):
        create_inventory(
            db_session,
            name="Lab",
            path=str(target),
            source_type=InventorySourceType.INI,
            inventory_roots=[],
            project_roots=[tmp_path],
        )


def test_project_root_does_not_widen_the_standalone_boundary(
    db_session: Session, tmp_path: Path
) -> None:
    """Project allowlist'i standalone akışını genişletmez.

    Project kökü altındaki her dosya kendiliğinden kaydedilebilir bir inventory
    değildir; standalone kayıt yalnızca inventory root'larına tabidir.
    """
    project_root = tmp_path / "projeler"
    target = _inventory_file(project_root / "web")

    with pytest.raises(PathNotAllowedError):
        create_inventory(
            db_session,
            name="Lab",
            path=str(target),
            source_type=InventorySourceType.INI,
            inventory_roots=[tmp_path / "envanterler"],
            project_roots=[project_root],
        )


def test_symlink_escape_out_of_inventory_root_is_rejected(
    db_session: Session, tmp_path: Path
) -> None:
    inventory_root = tmp_path / "envanterler"
    inventory_root.mkdir()
    outside = tmp_path / "disarida"
    _inventory_file(outside)
    link = link_directory(inventory_root / "kacis", outside)

    with pytest.raises(PathNotAllowedError):
        create_inventory(
            db_session,
            name="Kacis",
            path=str(link / "hosts.ini"),
            source_type=InventorySourceType.INI,
            inventory_roots=[inventory_root],
            project_roots=[tmp_path],
        )


def test_shared_prefix_sibling_of_inventory_root_is_rejected(
    db_session: Session, tmp_path: Path
) -> None:
    """`<root>-evil` inventory root'unun altında değildir."""
    inventory_root = tmp_path / "envanterler"
    inventory_root.mkdir()
    target = _inventory_file(tmp_path / "envanterler-evil")

    with pytest.raises(PathNotAllowedError):
        create_inventory(
            db_session,
            name="Prefix",
            path=str(target),
            source_type=InventorySourceType.INI,
            inventory_roots=[inventory_root],
            project_roots=[tmp_path],
        )


def test_relative_path_never_reaches_the_allowlist_check(
    db_session: Session, tmp_path: Path
) -> None:
    """Normalizasyon ilk adımdır; relative path allowlist'e hiç ulaşmaz."""
    with pytest.raises(InvalidPathError):
        create_inventory(
            db_session,
            name="Relative",
            path="inventories/hosts.ini",
            source_type=InventorySourceType.INI,
            inventory_roots=[tmp_path],
            project_roots=[tmp_path],
        )


def test_missing_file_is_rejected_after_allowlist(db_session: Session, tmp_path: Path) -> None:
    inventory_root = tmp_path / "envanterler"
    inventory_root.mkdir()

    with pytest.raises(PathNotFoundError):
        create_inventory(
            db_session,
            name="Yok",
            path=str(inventory_root / "yok.ini"),
            source_type=InventorySourceType.INI,
            inventory_roots=[inventory_root],
            project_roots=[tmp_path],
        )


def test_directory_is_rejected(db_session: Session, tmp_path: Path) -> None:
    inventory_root = tmp_path / "envanterler"
    (inventory_root / "alt").mkdir(parents=True)

    with pytest.raises(PathIsNotAFileError):
        create_inventory(
            db_session,
            name="Dizin",
            path=str(inventory_root / "alt"),
            source_type=InventorySourceType.INI,
            inventory_roots=[inventory_root],
            project_roots=[tmp_path],
        )


# --- Project'e bağlı akış ----------------------------------------------------


def test_project_link_is_persisted(db_session: Session, tmp_path: Path) -> None:
    project_root = tmp_path / "projeler"
    project = _add_project(db_session, project_root / "web")
    target = _inventory_file(project_root / "web" / "inventories", "prod.yml")

    inventory = create_inventory(
        db_session,
        name="Prod",
        path=str(target),
        source_type=InventorySourceType.YAML,
        project_id=project.id,
        # Inventory root'u dosyayı kapsamıyor; project akışı ona bağlı değildir.
        inventory_roots=[tmp_path / "envanterler"],
        project_roots=[project_root],
    )

    assert inventory.project_id == project.id
    assert inventory.source_type is InventorySourceType.YAML


def test_unknown_project_cannot_be_linked(db_session: Session, tmp_path: Path) -> None:
    """Path genel sınırı geçtiğinde sıradaki kontrol project kaydıdır."""
    target = _inventory_file(tmp_path / "projeler" / "web")

    with pytest.raises(NotFoundError):
        create_inventory(
            db_session,
            name="Lab",
            path=str(target),
            source_type=InventorySourceType.INI,
            project_id=4242,
            inventory_roots=[tmp_path / "envanterler"],
            project_roots=[tmp_path / "projeler"],
        )


def test_project_allowlist_is_checked_before_the_project_lookup(
    db_session: Session, tmp_path: Path
) -> None:
    """İzinli alanın dışındaki path, project sorgulanmadan reddedilir.

    Project bulunmamasına rağmen hata ``NotFoundError`` değil
    ``PathNotAllowedError``'dır; 403/404 farkı bir project id oracle'ı olamaz.
    """
    target = _inventory_file(tmp_path / "disarida")

    with pytest.raises(PathNotAllowedError):
        create_inventory(
            db_session,
            name="Disarida",
            path=str(target),
            source_type=InventorySourceType.INI,
            project_id=4242,
            inventory_roots=[tmp_path / "envanterler"],
            project_roots=[tmp_path / "projeler"],
        )


def test_project_allowlist_is_checked_before_file_existence(
    db_session: Session, tmp_path: Path
) -> None:
    """İzinsiz alanda var olmayan dosya da aynı güvenlik hatasını üretir."""
    (tmp_path / "disarida").mkdir()

    with pytest.raises(PathNotAllowedError):
        create_inventory(
            db_session,
            name="Yok",
            path=str(tmp_path / "disarida" / "yok.ini"),
            source_type=InventorySourceType.INI,
            project_id=4242,
            inventory_roots=[tmp_path / "envanterler"],
            project_roots=[tmp_path / "projeler"],
        )


def test_inactive_project_cannot_be_linked(db_session: Session, tmp_path: Path) -> None:
    project_root = tmp_path / "projeler"
    project = _add_project(db_session, project_root / "web", is_active=False)
    target = _inventory_file(project_root / "web")

    with pytest.raises(ProjectInactiveError):
        create_inventory(
            db_session,
            name="Lab",
            path=str(target),
            source_type=InventorySourceType.INI,
            project_id=project.id,
            inventory_roots=[tmp_path / "envanterler"],
            project_roots=[project_root],
        )


def test_file_outside_linked_project_is_rejected(db_session: Session, tmp_path: Path) -> None:
    project_root = tmp_path / "projeler"
    project = _add_project(db_session, project_root / "web")
    target = _inventory_file(project_root / "baska")

    with pytest.raises(InventoryOutsideProjectError) as exc_info:
        create_inventory(
            db_session,
            name="Yabanci",
            path=str(target),
            source_type=InventorySourceType.INI,
            project_id=project.id,
            inventory_roots=[tmp_path / "envanterler"],
            project_roots=[project_root],
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.details == {"project_id": project.id}


def test_inventory_root_does_not_widen_the_project_boundary(
    db_session: Session, tmp_path: Path
) -> None:
    """Inventory allowlist'i project akışını genişletmez.

    Dosya inventory root'unun içindedir ama project allowlist'inin tamamen
    dışındadır; genel sınır kontrolüne takılır ve project'e hiç bakılmaz.
    """
    project_root = tmp_path / "projeler"
    inventory_root = tmp_path / "envanterler"
    project = _add_project(db_session, project_root / "web")
    target = _inventory_file(inventory_root)

    with pytest.raises(PathNotAllowedError):
        create_inventory(
            db_session,
            name="Yabanci",
            path=str(target),
            source_type=InventorySourceType.INI,
            project_id=project.id,
            inventory_roots=[inventory_root],
            project_roots=[project_root],
        )


def test_empty_project_allowlist_is_fail_closed(db_session: Session, tmp_path: Path) -> None:
    """Project root tanımlı değilse project'e bağlı kayıt da kabul edilmez."""
    project_root = tmp_path / "projeler"
    project = _add_project(db_session, project_root / "web")
    target = _inventory_file(project_root / "web")

    with pytest.raises(PathNotAllowedError):
        create_inventory(
            db_session,
            name="Lab",
            path=str(target),
            source_type=InventorySourceType.INI,
            project_id=project.id,
            inventory_roots=[tmp_path],
            project_roots=[],
        )


def test_project_root_is_revalidated_against_the_current_allowlist(
    db_session: Session, tmp_path: Path
) -> None:
    """Kayıt anındaki allowlist kalıcı bir garanti değildir.

    Project daha geniş bir allowlist ile kaydedilmiş olabilir. Bağ kurulurken
    kök yeniden normalize edilip allowlist'e karşı tekrar doğrulanır; daralmış
    bir yapılandırmada eski kayıt üzerinden içeri girilemez.
    """
    project = _add_project(db_session, tmp_path / "eski-kok" / "web")
    target = _inventory_file(tmp_path / "eski-kok" / "web" / "inventories")
    # Daralmış allowlist inventory dosyasını kapsar ama project kökünü kapsamaz.
    narrowed = [tmp_path / "eski-kok" / "web" / "inventories"]

    with pytest.raises(PathNotAllowedError):
        create_inventory(
            db_session,
            name="Lab",
            path=str(target),
            source_type=InventorySourceType.INI,
            project_id=project.id,
            inventory_roots=[tmp_path],
            project_roots=narrowed,
        )


def test_missing_file_in_project_is_rejected_after_the_boundary(
    db_session: Session, tmp_path: Path
) -> None:
    """Project akışında da varlık kontrolü güvenlik sınırından sonradır."""
    project_root = tmp_path / "projeler"
    project = _add_project(db_session, project_root / "web")

    with pytest.raises(PathNotFoundError):
        create_inventory(
            db_session,
            name="Yok",
            path=str(project_root / "web" / "yok.ini"),
            source_type=InventorySourceType.INI,
            project_id=project.id,
            inventory_roots=[tmp_path / "envanterler"],
            project_roots=[project_root],
        )


# --- Listeleme ve detay ------------------------------------------------------


def test_list_is_sorted_and_filterable(db_session: Session, tmp_path: Path) -> None:
    project_root = tmp_path / "projeler"
    inventory_root = tmp_path / "envanterler"
    project = _add_project(db_session, project_root / "web")
    linked = _inventory_file(project_root / "web", "prod.ini")
    standalone = _inventory_file(inventory_root, "serbest.ini")
    create_inventory(
        db_session,
        name="Zeta",
        path=str(standalone),
        source_type=InventorySourceType.INI,
        inventory_roots=[inventory_root],
        project_roots=[project_root],
    )
    create_inventory(
        db_session,
        name="Alfa",
        path=str(linked),
        source_type=InventorySourceType.INI,
        project_id=project.id,
        inventory_roots=[inventory_root],
        project_roots=[project_root],
    )

    assert [item.name for item in list_inventories(db_session)] == ["Alfa", "Zeta"]
    assert [item.name for item in list_inventories(db_session, project_id=project.id)] == ["Alfa"]


def test_get_unknown_inventory_raises_not_found(db_session: Session) -> None:
    with pytest.raises(NotFoundError):
        get_inventory(db_session, 4242)
