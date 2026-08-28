"""Controller path browse API (R1-V3J0C).

Servis testleri (`test_controller_path_browse_service.py`) allowlist/scope
kararlarını HTTP olmadan ölçer; bu dosya yalnızca HTTP sözleşmesini —
query parametreleri, response şekli, hata kodları, OpenAPI yüzeyi ve
endpoint'in yan etkisiz kaldığını — doğrular.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def test_project_scope_happy_path(client: TestClient, project_root: Path) -> None:
    (project_root / "web").mkdir()

    response = client.get(
        "/api/controller-paths", params={"scope": "project", "path": str(project_root)}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["scope"] == "project"
    assert body["current_path"] == str(project_root.resolve())
    assert body["target_kind"] == "directory"
    assert body["truncated"] is False
    assert body["entries"] == [
        {
            "name": "web",
            "path": str((project_root / "web").resolve()),
            "kind": "directory",
            "selectable": True,
        }
    ]


def test_inventory_scope_happy_path(client: TestClient, inventory_root: Path) -> None:
    (inventory_root / "hosts.ini").write_text("[web]\nweb01\n", encoding="utf-8")

    response = client.get(
        "/api/controller-paths", params={"scope": "inventory", "path": str(inventory_root)}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["target_kind"] == "file"
    assert body["entries"] == [
        {
            "name": "hosts.ini",
            "path": str((inventory_root / "hosts.ini").resolve()),
            "kind": "file",
            "selectable": True,
        }
    ]


def test_project_inventory_scope_happy_path(client: TestClient, project_root: Path) -> None:
    project_dir = project_root / "web"
    project_dir.mkdir()
    (project_dir / "hosts.ini").write_text("[web]\nweb01\n", encoding="utf-8")
    project = client.post("/api/projects", json={"name": "Web", "path": str(project_dir)}).json()

    response = client.get(
        "/api/controller-paths",
        params={"scope": "project_inventory", "project_id": project["id"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["current_path"] == str(project_dir.resolve())
    assert body["entries"] == [
        {
            "name": "hosts.ini",
            "path": str((project_dir / "hosts.ini").resolve()),
            "kind": "file",
            "selectable": True,
        }
    ]


# --- Response field seti: owner/permission/size/timestamp yok ----------------


def test_response_field_set_is_minimal(client: TestClient, project_root: Path) -> None:
    (project_root / "site.yml").write_text("- hosts: all", encoding="utf-8")

    body = client.get(
        "/api/controller-paths", params={"scope": "project", "path": str(project_root)}
    ).json()

    assert set(body) == {"scope", "current_path", "target_kind", "entries", "truncated"}
    assert set(body["entries"][0]) == {"name", "path", "kind", "selectable"}


# --- Allowlist dışı path: generic ve sanitize 403 ----------------------------


def test_allowlist_outside_path_is_generic_403(client: TestClient, tmp_path: Path) -> None:
    outside = tmp_path / "disarida"
    outside.mkdir()

    response = client.get(
        "/api/controller-paths", params={"scope": "project", "path": str(outside)}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "path_not_allowed"


def test_missing_and_existing_outside_paths_produce_identical_error(
    client: TestClient, tmp_path: Path
) -> None:
    existing = tmp_path / "gercek"
    existing.mkdir()
    missing = tmp_path / "yok"

    responses = [
        client.get("/api/controller-paths", params={"scope": "project", "path": str(candidate)})
        for candidate in (existing, missing)
    ]

    bodies = [response.json() for response in responses]
    assert all(response.status_code == 403 for response in responses)
    assert bodies[0]["error"] == bodies[1]["error"]


def test_error_message_does_not_leak_submitted_path(client: TestClient, tmp_path: Path) -> None:
    outside = tmp_path / "gizli-yol"
    outside.mkdir()

    error = client.get(
        "/api/controller-paths", params={"scope": "project", "path": str(outside)}
    ).json()["error"]

    assert str(outside) not in error["message"]
    assert str(outside) not in str(error["details"])


def test_traversal_out_of_allowed_root_is_rejected(
    client: TestClient, project_root: Path, tmp_path: Path
) -> None:
    (tmp_path / "disarida").mkdir()

    response = client.get(
        "/api/controller-paths",
        params={"scope": "project", "path": str(project_root / ".." / "disarida")},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "path_not_allowed"


# --- scope / project_id sözleşmesi -------------------------------------------


def test_missing_scope_is_rejected(client: TestClient) -> None:
    response = client.get("/api/controller-paths")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"


def test_unknown_scope_value_is_rejected(client: TestClient) -> None:
    response = client.get("/api/controller-paths", params={"scope": "bogus"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"


def test_project_inventory_without_project_id_is_rejected(client: TestClient) -> None:
    response = client.get("/api/controller-paths", params={"scope": "project_inventory"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "browse_invalid_scope"


def test_project_id_is_forbidden_outside_project_inventory_scope(client: TestClient) -> None:
    response = client.get("/api/controller-paths", params={"scope": "project", "project_id": 1})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "browse_invalid_scope"


# --- Pasif veya bulunamayan project -------------------------------------------


def test_inactive_project_returns_409(client: TestClient, project_root: Path) -> None:
    project_dir = project_root / "web"
    project_dir.mkdir()
    created = client.post("/api/projects", json={"name": "Web", "path": str(project_dir)}).json()
    client.delete(f"/api/projects/{created['id']}")

    response = client.get(
        "/api/controller-paths",
        params={"scope": "project_inventory", "project_id": created["id"]},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "project_inactive"


def test_unknown_project_returns_404(client: TestClient) -> None:
    response = client.get(
        "/api/controller-paths", params={"scope": "project_inventory", "project_id": 4242}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --- OpenAPI route sözleşmesi --------------------------------------------------


def test_openapi_exposes_a_single_read_only_route(client: TestClient) -> None:
    """Route yüzeyi kilidi: yalnızca ``GET`` ve beklenen üç query parametresi.

    Bir POST/PUT/DELETE'in bu path'e yanlışlıkla eklenmesi (yazma yeteneği
    kazandırması) bu testi kırar. Enum değerlerinin kendisi zaten fonksiyonel
    testlerle (``test_unknown_scope_value_is_rejected`` vb.) ölçülür; burada
    yalnızca yüzeyin şekli — method seti ve parametre adları — kilitlenir.
    """
    schema = client.get("/openapi.json").json()

    path_item = schema["paths"]["/api/controller-paths"]
    assert set(path_item) == {"get"}

    parameters = {param["name"]: param for param in path_item["get"]["parameters"]}
    assert parameters["scope"]["required"] is True
    assert parameters["project_id"]["required"] is False
    assert parameters["path"]["required"] is False


# --- Endpoint write/subprocess/dosya içeriği okuma yapmaz --------------------


def test_endpoint_does_not_write_to_disk(client: TestClient, project_root: Path) -> None:
    (project_root / "web").mkdir()
    before = sorted(path.name for path in project_root.rglob("*"))

    client.get("/api/controller-paths", params={"scope": "project", "path": str(project_root)})

    after = sorted(path.name for path in project_root.rglob("*"))
    assert before == after


def test_endpoint_never_spawns_a_subprocess(
    client: TestClient, project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project_root / "web").mkdir()

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("controller-paths endpoint bir subprocess başlatmamalı")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)

    response = client.get(
        "/api/controller-paths", params={"scope": "project", "path": str(project_root)}
    )

    assert response.status_code == 200
