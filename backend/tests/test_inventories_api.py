"""Inventory CRUD API (T-201).

Testler iki ayrı kökle çalışır (ADR-015):

- ``inventory_root`` — standalone inventory'lerin kabul edildiği yer
- ``project_root`` — project kayıtlarının kabul edildiği yer

İkisi bilinçli olarak ayrıdır; birini diğerinin yerine kullanan bir test,
ayrımın gerçekten uygulandığını gizlerdi.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import create_db_engine, get_session
from app.main import create_app
from tests.support import alembic_config, link_directory, make_settings

RESPONSE_FIELDS = {
    "id",
    "project_id",
    "name",
    "path",
    "source_type",
    "created_at",
    "updated_at",
}


def _write_inventory(path: Path, content: str = "[web]\nweb01\n") -> Path:
    """Test için gerçek bir inventory dosyası oluşturur."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _create_project(client: TestClient, path: Path, name: str = "Web") -> dict[str, object]:
    """API üzerinden project oluşturur ve gövdesini döndürür."""
    path.mkdir(parents=True, exist_ok=True)
    response = client.post("/api/projects", json={"name": name, "path": str(path)})
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


@pytest.fixture
def default_roots_client(tmp_path: Path) -> Iterator[TestClient]:
    """Hiçbir allowlist yapılandırılmamış, varsayılan ayarlı bir client.

    Varsayılan köklerin (``app-data/projects`` ve ``app-data/inventories``)
    gerçekten kullanılabilir olduğunu ölçmek için gereklidir; ana ``client``
    fixture'ı ikisini de açıkça geçersiz kılar.
    """
    settings = make_settings(
        environment="test",
        app_data_dir=tmp_path / "app-data",
        database_url=f"sqlite:///{(tmp_path / 'varsayilan.db').as_posix()}",
        cors_origins=["http://localhost:5173"],
    )
    command.upgrade(alembic_config(settings.resolve_database_url()), "head")
    engine: Engine = create_db_engine(settings)

    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings

    def _override_session() -> Iterator[Session]:
        with Session(engine, expire_on_commit=False) as session:
            yield session

    app.dependency_overrides[get_session] = _override_session

    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    engine.dispose()


# --- Oluşturma ---------------------------------------------------------------


def test_ini_inventory_can_be_created(client: TestClient, inventory_root: Path) -> None:
    target = _write_inventory(inventory_root / "hosts.ini")

    response = client.post(
        "/api/inventories",
        json={"name": "Lab", "path": str(target), "source_type": "ini"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Lab"
    assert body["path"] == str(target.resolve())
    assert body["source_type"] == "ini"
    assert body["project_id"] is None
    assert isinstance(body["id"], int)


def test_yaml_inventory_can_be_created(client: TestClient, inventory_root: Path) -> None:
    target = _write_inventory(inventory_root / "hosts.yml", "all:\n  hosts:\n    web01:\n")

    response = client.post(
        "/api/inventories",
        json={"name": "Lab YAML", "path": str(target), "source_type": "yaml"},
    )

    assert response.status_code == 201
    assert response.json()["source_type"] == "yaml"


def test_inventory_can_be_linked_to_project(client: TestClient, project_root: Path) -> None:
    project = _create_project(client, project_root / "web")
    target = _write_inventory(project_root / "web" / "inventories" / "prod.ini")

    response = client.post(
        "/api/inventories",
        json={
            "name": "Prod",
            "path": str(target),
            "source_type": "ini",
            "project_id": project["id"],
        },
    )

    assert response.status_code == 201
    assert response.json()["project_id"] == project["id"]


def test_standalone_inventory_is_not_linked_to_any_project(
    client: TestClient, inventory_root: Path
) -> None:
    """`project_id` verilmezse inventory yeniden kullanılabilir bir kayıt olur."""
    target = _write_inventory(inventory_root / "paylasilan.ini")

    response = client.post(
        "/api/inventories",
        json={"name": "Paylasilan", "path": str(target), "source_type": "ini"},
    )

    assert response.status_code == 201
    assert response.json()["project_id"] is None


def test_response_contains_only_the_agreed_fields(client: TestClient, inventory_root: Path) -> None:
    target = _write_inventory(inventory_root / "hosts.ini")

    response = client.post(
        "/api/inventories",
        json={"name": "Lab", "path": str(target), "source_type": "ini"},
    )

    assert set(response.json()) == RESPONSE_FIELDS


def test_stored_path_is_normalized(client: TestClient, inventory_root: Path) -> None:
    """İzinli alan içindeki `..` sonuca taşınmaz; kanonik yol saklanır."""
    target = _write_inventory(inventory_root / "hosts.ini")
    (inventory_root / "gecici").mkdir()
    noisy = inventory_root / "gecici" / ".." / "hosts.ini"

    response = client.post(
        "/api/inventories",
        json={"name": "Lab", "path": str(noisy), "source_type": "ini"},
    )

    assert response.status_code == 201
    assert response.json()["path"] == str(target.resolve())


# --- Varsayılan kökler -------------------------------------------------------


def test_default_inventory_root_accepts_standalone_inventory(
    default_roots_client: TestClient, tmp_path: Path
) -> None:
    """Varsayılan yapılandırma kullanılabilir bir inventory kökü bırakır.

    `app-data/inventories` uygulamanın kendi oluşturduğu dizindir; oraya konan
    bir dosya ek yapılandırma olmadan kaydedilebilmelidir.
    """
    target = _write_inventory(tmp_path / "app-data" / "inventories" / "hosts.ini")

    response = default_roots_client.post(
        "/api/inventories",
        json={"name": "Varsayilan", "path": str(target), "source_type": "ini"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["project_id"] is None


def test_default_project_root_is_not_a_standalone_inventory_root(
    default_roots_client: TestClient, tmp_path: Path
) -> None:
    """`app-data/projects` altındaki bir dosya kendiliğinden inventory olmaz.

    Project kökü altında duran her dosyanın kaydedilebilir sayılması, project
    allowlist'ini sessizce bir inventory allowlist'ine dönüştürürdü.
    """
    target = _write_inventory(tmp_path / "app-data" / "projects" / "web" / "hosts.ini")

    response = default_roots_client.post(
        "/api/inventories",
        json={"name": "Proje icinden", "path": str(target), "source_type": "ini"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "path_not_allowed"


# --- Listeleme ve detay ------------------------------------------------------


def test_inventories_are_listed(client: TestClient, inventory_root: Path) -> None:
    _write_inventory(inventory_root / "bir.ini")
    _write_inventory(inventory_root / "iki.ini")
    client.post(
        "/api/inventories",
        json={"name": "Zeta", "path": str(inventory_root / "iki.ini"), "source_type": "ini"},
    )
    client.post(
        "/api/inventories",
        json={"name": "Alfa", "path": str(inventory_root / "bir.ini"), "source_type": "ini"},
    )

    response = client.get("/api/inventories")

    assert response.status_code == 200
    assert [item["name"] for item in response.json()] == ["Alfa", "Zeta"]


def test_list_can_be_filtered_by_project(
    client: TestClient, project_root: Path, inventory_root: Path
) -> None:
    project = _create_project(client, project_root / "web")
    _write_inventory(project_root / "web" / "prod.ini")
    _write_inventory(inventory_root / "serbest.ini")
    client.post(
        "/api/inventories",
        json={
            "name": "Prod",
            "path": str(project_root / "web" / "prod.ini"),
            "source_type": "ini",
            "project_id": project["id"],
        },
    )
    client.post(
        "/api/inventories",
        json={
            "name": "Serbest",
            "path": str(inventory_root / "serbest.ini"),
            "source_type": "ini",
        },
    )

    filtered = client.get("/api/inventories", params={"project_id": project["id"]})

    assert filtered.status_code == 200
    assert [item["name"] for item in filtered.json()] == ["Prod"]
    assert len(client.get("/api/inventories").json()) == 2


def test_inventory_detail_is_returned(client: TestClient, inventory_root: Path) -> None:
    target = _write_inventory(inventory_root / "hosts.ini")
    created = client.post(
        "/api/inventories",
        json={"name": "Lab", "path": str(target), "source_type": "ini"},
    ).json()

    response = client.get(f"/api/inventories/{created['id']}")

    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_unknown_inventory_returns_standard_not_found(client: TestClient) -> None:
    response = client.get("/api/inventories/4242")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert response.json()["error"]["message"]


# --- Girdi doğrulaması -------------------------------------------------------


def test_invalid_source_type_is_rejected(client: TestClient, inventory_root: Path) -> None:
    """`ini` ve `yaml` dışındaki biçimler (dinamik inventory dâhil) kabul edilmez."""
    target = _write_inventory(inventory_root / "hosts.sh")

    response = client.post(
        "/api/inventories",
        json={"name": "Dinamik", "path": str(target), "source_type": "dynamic"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"


def test_client_cannot_set_server_owned_fields(client: TestClient, inventory_root: Path) -> None:
    target = _write_inventory(inventory_root / "hosts.ini")

    response = client.post(
        "/api/inventories",
        json={
            "name": "Lab",
            "path": str(target),
            "source_type": "ini",
            "id": 99,
            "created_at": "2000-01-01T00:00:00Z",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"


def test_blank_name_is_rejected(client: TestClient, inventory_root: Path) -> None:
    target = _write_inventory(inventory_root / "hosts.ini")

    response = client.post(
        "/api/inventories",
        json={"name": "   ", "path": str(target), "source_type": "ini"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"


def test_non_positive_project_id_is_rejected(client: TestClient, inventory_root: Path) -> None:
    target = _write_inventory(inventory_root / "hosts.ini")

    response = client.post(
        "/api/inventories",
        json={"name": "Lab", "path": str(target), "source_type": "ini", "project_id": 0},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"


def test_relative_path_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/inventories",
        json={"name": "Relative", "path": "inventories/hosts.ini", "source_type": "ini"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_path"


def test_missing_body_fields_return_standard_envelope(client: TestClient) -> None:
    response = client.post("/api/inventories", json={})

    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}


# --- Dosya varlığı -----------------------------------------------------------


def test_missing_file_is_rejected(client: TestClient, inventory_root: Path) -> None:
    response = client.post(
        "/api/inventories",
        json={
            "name": "Yok",
            "path": str(inventory_root / "hic-olmayan.ini"),
            "source_type": "ini",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "path_not_found"


def test_directory_instead_of_file_is_rejected(client: TestClient, inventory_root: Path) -> None:
    directory = inventory_root / "alt-dizin"
    directory.mkdir()

    response = client.post(
        "/api/inventories",
        json={"name": "Dizin", "path": str(directory), "source_type": "ini"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "path_not_a_file"


# --- Path güvenliği: standalone ----------------------------------------------


def test_path_outside_allowed_root_is_forbidden(client: TestClient, tmp_path: Path) -> None:
    target = _write_inventory(tmp_path / "disarida" / "hosts.ini")

    response = client.post(
        "/api/inventories",
        json={"name": "Disarida", "path": str(target), "source_type": "ini"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "path_not_allowed"


def test_project_root_is_not_a_standalone_inventory_root(
    client: TestClient, project_root: Path
) -> None:
    """Project allowlist'i standalone inventory akışını genişletmez."""
    target = _write_inventory(project_root / "web" / "hosts.ini")

    response = client.post(
        "/api/inventories",
        json={"name": "Proje icinden", "path": str(target), "source_type": "ini"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "path_not_allowed"


def test_shared_prefix_sibling_root_is_forbidden(
    client: TestClient, inventory_root: Path, tmp_path: Path
) -> None:
    """`<root>-evil` yolu root'un altında değildir.

    Bu, `startswith` ile yapılan bir prefix karşılaştırmasının kaçıracağı
    senaryodur; kontrol gerçek yol parçaları üzerinden yapılır.
    """
    sibling = tmp_path / f"{inventory_root.name}-evil"
    target = _write_inventory(sibling / "hosts.ini")

    response = client.post(
        "/api/inventories",
        json={"name": "Prefix", "path": str(target), "source_type": "ini"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "path_not_allowed"


def test_traversal_out_of_allowed_root_is_forbidden(
    client: TestClient, inventory_root: Path, tmp_path: Path
) -> None:
    _write_inventory(tmp_path / "disarida" / "hosts.ini")
    traversal = inventory_root / ".." / "disarida" / "hosts.ini"

    response = client.post(
        "/api/inventories",
        json={"name": "Traversal", "path": str(traversal), "source_type": "ini"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "path_not_allowed"


def test_symlink_escape_is_forbidden(
    client: TestClient, inventory_root: Path, tmp_path: Path
) -> None:
    """İzinli alanın içinde görünen bir bağlantı dışarıyı gösteriyorsa reddedilir.

    Windows'ta symlink yetkisi yoksa aynı çözümleme davranışına sahip junction
    kullanılır; test sessizce "geçti" sayılmaz.
    """
    outside = tmp_path / "gizli"
    _write_inventory(outside / "hosts.ini")
    link = link_directory(inventory_root / "gorunuste-icerde", outside)

    response = client.post(
        "/api/inventories",
        json={"name": "Kacis", "path": str(link / "hosts.ini"), "source_type": "ini"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "path_not_allowed"


# --- Path güvenliği: project bağı --------------------------------------------


def test_unknown_project_cannot_be_linked(client: TestClient, project_root: Path) -> None:
    target = _write_inventory(project_root / "web" / "hosts.ini")

    response = client.post(
        "/api/inventories",
        json={
            "name": "Lab",
            "path": str(target),
            "source_type": "ini",
            "project_id": 4242,
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_inactive_project_cannot_be_linked(client: TestClient, project_root: Path) -> None:
    project = _create_project(client, project_root / "web")
    target = _write_inventory(project_root / "web" / "hosts.ini")
    client.delete(f"/api/projects/{project['id']}")

    response = client.post(
        "/api/inventories",
        json={
            "name": "Lab",
            "path": str(target),
            "source_type": "ini",
            "project_id": project["id"],
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "project_inactive"


def test_inventory_outside_linked_project_is_forbidden(
    client: TestClient, project_root: Path
) -> None:
    """Project allowlist'inde olmak yetmez; dosya o project'in kökünde olmalıdır."""
    project = _create_project(client, project_root / "web")
    target = _write_inventory(project_root / "baska" / "hosts.ini")

    response = client.post(
        "/api/inventories",
        json={
            "name": "Yabanci",
            "path": str(target),
            "source_type": "ini",
            "project_id": project["id"],
        },
    )

    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "inventory_path_outside_project"
    assert error["details"] == {"project_id": project["id"]}


def test_inventory_root_does_not_widen_the_project_boundary(
    client: TestClient, project_root: Path, inventory_root: Path
) -> None:
    """Inventory allowlist'i project akışını genişletmez.

    Dosya inventory kökünün içindedir ama project allowlist'inin tamamen
    dışındadır; bu yüzden genel sınır kontrolüne takılır ve project'e hiç
    bakılmadan reddedilir.
    """
    project = _create_project(client, project_root / "web")
    target = _write_inventory(inventory_root / "hosts.ini")

    response = client.post(
        "/api/inventories",
        json={
            "name": "Yabanci",
            "path": str(target),
            "source_type": "ini",
            "project_id": project["id"],
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "path_not_allowed"


def test_project_boundary_rejects_shared_prefix_sibling(
    client: TestClient, project_root: Path
) -> None:
    """`<project>-evil` project kökünün altında değildir.

    Prefix tuzağının project sınırındaki karşılığıdır: `startswith` ile yapılan
    bir karşılaştırma bu yolu yanlışlıkla project içi sayardı.
    """
    project = _create_project(client, project_root / "web")
    target = _write_inventory(project_root / "web-evil" / "hosts.ini")

    response = client.post(
        "/api/inventories",
        json={
            "name": "Prefix",
            "path": str(target),
            "source_type": "ini",
            "project_id": project["id"],
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "inventory_path_outside_project"


def test_symlink_escape_out_of_project_is_forbidden(client: TestClient, project_root: Path) -> None:
    """Project içinde görünüp project dışına çıkan bağlantı da reddedilir.

    Hedef project allowlist'inin içindedir; bu yüzden reddi gerçekten project
    sınırı kontrolü üretir.
    """
    project = _create_project(client, project_root / "web")
    outside_project = project_root / "baska"
    _write_inventory(outside_project / "hosts.ini")
    link = link_directory(project_root / "web" / "baglanti", outside_project)

    response = client.post(
        "/api/inventories",
        json={
            "name": "Kacis",
            "path": str(link / "hosts.ini"),
            "source_type": "ini",
            "project_id": project["id"],
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "inventory_path_outside_project"


# --- Kontrol sırası ve bilgi sızıntısı ---------------------------------------


def test_allowlist_is_checked_before_file_existence(client: TestClient, tmp_path: Path) -> None:
    """İzinsiz alanda var olan ve olmayan dosya **ayırt edilemez** cevap üretir.

    Aksi hâlde endpoint, farklı yollar denenerek sunucudaki dosyaların
    varlığını öğrenmeye yarayan bir dosya sistemi sondası olurdu.
    """
    existing = _write_inventory(tmp_path / "disarida" / "var.ini")
    missing = tmp_path / "disarida" / "yok.ini"

    existing_response = client.post(
        "/api/inventories",
        json={"name": "Var", "path": str(existing), "source_type": "ini"},
    )
    missing_response = client.post(
        "/api/inventories",
        json={"name": "Yok", "path": str(missing), "source_type": "ini"},
    )

    assert existing_response.status_code == missing_response.status_code == 403
    assert existing_response.json() == missing_response.json()


def test_project_boundary_is_checked_before_file_existence(
    client: TestClient, project_root: Path
) -> None:
    """Project akışında da var olan ve olmayan dosya aynı cevabı üretir."""
    project = _create_project(client, project_root / "web")
    existing = _write_inventory(project_root / "baska" / "var.ini")
    missing = project_root / "baska" / "yok.ini"

    existing_response = client.post(
        "/api/inventories",
        json={
            "name": "Var",
            "path": str(existing),
            "source_type": "ini",
            "project_id": project["id"],
        },
    )
    missing_response = client.post(
        "/api/inventories",
        json={
            "name": "Yok",
            "path": str(missing),
            "source_type": "ini",
            "project_id": project["id"],
        },
    )

    assert existing_response.status_code == missing_response.status_code == 403
    assert existing_response.json() == missing_response.json()
    assert existing_response.json()["error"]["code"] == "inventory_path_outside_project"


def test_project_link_is_checked_before_file_existence(
    client: TestClient, project_root: Path
) -> None:
    """İzinli alan içinde, bilinmeyen project + var olmayan dosya: 404 döner.

    Path genel sınırı geçtiği için sıradaki kontrol project kaydıdır; dosya
    varlığına hiç bakılmaz.
    """
    response = client.post(
        "/api/inventories",
        json={
            "name": "Yok",
            "path": str(project_root / "hic-olmayan.ini"),
            "source_type": "ini",
            "project_id": 4242,
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_existing_file_inside_project_roots_with_unknown_project_returns_not_found(
    client: TestClient, project_root: Path
) -> None:
    """İzinli alan içindeki **mevcut** dosya + bilinmeyen project: yine 404.

    Genel sınır geçildiğinde project kaydının yokluğu artık gizlenecek bir
    bilgi değildir; istemci zaten izin verilen alanın içindedir.
    """
    target = _write_inventory(project_root / "web" / "hosts.ini")

    response = client.post(
        "/api/inventories",
        json={
            "name": "Var",
            "path": str(target),
            "source_type": "ini",
            "project_id": 4242,
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_project_allowlist_is_checked_before_the_project_lookup(
    client: TestClient, tmp_path: Path
) -> None:
    """İzinli alanın dışındaki path, project sorgulanmadan reddedilir.

    Aksi hâlde 403/404 farkı, izin verilmeyen bir path üzerinden project
    kaydının var olup olmadığını sızdıran bir oracle olurdu.
    """
    target = _write_inventory(tmp_path / "disarida" / "hosts.ini")

    response = client.post(
        "/api/inventories",
        json={
            "name": "Disarida",
            "path": str(target),
            "source_type": "ini",
            "project_id": 4242,
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "path_not_allowed"


def test_project_allowlist_rejection_is_identical_for_existing_and_missing_files(
    client: TestClient, tmp_path: Path
) -> None:
    """İzinsiz alanda var olan ve olmayan dosya aynı gövdeyi üretir.

    Project bağı istenmiş olması bu garantiyi değiştirmez: güvenlik sınırı
    hem project sorgusundan hem varlık kontrolünden öncedir.
    """
    existing = _write_inventory(tmp_path / "disarida" / "var.ini")
    missing = tmp_path / "disarida" / "yok.ini"

    existing_response = client.post(
        "/api/inventories",
        json={
            "name": "Var",
            "path": str(existing),
            "source_type": "ini",
            "project_id": 4242,
        },
    )
    missing_response = client.post(
        "/api/inventories",
        json={
            "name": "Yok",
            "path": str(missing),
            "source_type": "ini",
            "project_id": 4242,
        },
    )

    assert existing_response.status_code == missing_response.status_code == 403
    assert existing_response.json() == missing_response.json()
    assert existing_response.json()["error"]["code"] == "path_not_allowed"


def test_project_allowlist_rejection_does_not_depend_on_the_project_existing(
    client: TestClient, project_root: Path, tmp_path: Path
) -> None:
    """Aynı izinsiz path, project var olsa da olmasa da aynı cevabı verir."""
    project = _create_project(client, project_root / "web")
    target = _write_inventory(tmp_path / "disarida" / "hosts.ini")

    known = client.post(
        "/api/inventories",
        json={
            "name": "Bilinen",
            "path": str(target),
            "source_type": "ini",
            "project_id": project["id"],
        },
    )
    unknown = client.post(
        "/api/inventories",
        json={
            "name": "Bilinmeyen",
            "path": str(target),
            "source_type": "ini",
            "project_id": 4242,
        },
    )

    assert known.status_code == unknown.status_code == 403
    assert known.json() == unknown.json()


def test_error_response_does_not_leak_server_paths(
    client: TestClient, inventory_root: Path, project_root: Path, tmp_path: Path
) -> None:
    """403 cevabı ne izinli kökleri ne de denenen yolu geri yansıtır."""
    target = _write_inventory(tmp_path / "disarida" / "hosts.ini")

    error = client.post(
        "/api/inventories",
        json={"name": "Disarida", "path": str(target), "source_type": "ini"},
    ).json()["error"]

    rendered = f"{error['message']} {error['details']}"
    assert str(inventory_root) not in rendered
    assert str(project_root) not in rendered
    assert str(target) not in rendered


def test_project_boundary_error_does_not_leak_server_paths(
    client: TestClient, project_root: Path
) -> None:
    """Project sınırı hatası da sunucudaki yolları tekrarlamaz."""
    project = _create_project(client, project_root / "web")
    target = _write_inventory(project_root / "baska" / "hosts.ini")

    error = client.post(
        "/api/inventories",
        json={
            "name": "Yabanci",
            "path": str(target),
            "source_type": "ini",
            "project_id": project["id"],
        },
    ).json()["error"]

    rendered = f"{error['message']} {error['details']}"
    assert str(project_root) not in rendered
    assert str(target) not in rendered


def test_error_response_does_not_leak_internal_exception_details(
    client: TestClient, inventory_root: Path
) -> None:
    """Hata zarfı traceback, modül adı veya SQL parçası taşımaz."""
    error = client.post(
        "/api/inventories",
        json={
            "name": "Yok",
            "path": str(inventory_root / "hic-olmayan.ini"),
            "source_type": "ini",
        },
    ).json()["error"]

    rendered = f"{error['message']} {error['details']}"
    for leak in ("Traceback", "app.services", "sqlalchemy", "SELECT", "INSERT"):
        assert leak not in rendered
