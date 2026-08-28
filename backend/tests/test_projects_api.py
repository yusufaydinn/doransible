"""Project CRUD API (T-102)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.support import link_directory


def test_project_can_be_created(client: TestClient, project_root: Path) -> None:
    (project_root / "web").mkdir()

    response = client.post(
        "/api/projects",
        json={"name": "Web", "path": str(project_root / "web"), "description": "Nginx"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Web"
    assert body["path"] == str((project_root / "web").resolve())
    assert body["description"] == "Nginx"
    assert body["is_active"] is True
    assert isinstance(body["id"], int)


def test_response_does_not_expose_internal_path_key(client: TestClient, project_root: Path) -> None:
    """`path_key` iç karşılaştırma detayıdır, API sözleşmesinin parçası değildir."""
    response = client.post("/api/projects", json={"name": "Web", "path": str(project_root)})

    assert response.status_code == 201
    assert set(response.json()) == {
        "id",
        "name",
        "path",
        "description",
        "is_active",
        "created_at",
        "updated_at",
    }


def test_client_cannot_set_server_owned_fields(client: TestClient, project_root: Path) -> None:
    """`id`, `is_active` ve `path_key` istemci tarafından set edilemez."""
    response = client.post(
        "/api/projects",
        json={
            "name": "Web",
            "path": str(project_root),
            "is_active": False,
            "path_key": "zorlanmis",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"


def test_projects_are_listed(client: TestClient, project_root: Path) -> None:
    (project_root / "bir").mkdir()
    (project_root / "iki").mkdir()
    client.post("/api/projects", json={"name": "Zeta", "path": str(project_root / "iki")})
    client.post("/api/projects", json={"name": "Alfa", "path": str(project_root / "bir")})

    response = client.get("/api/projects")

    assert response.status_code == 200
    assert [item["name"] for item in response.json()] == ["Alfa", "Zeta"]


def test_project_detail_is_returned(client: TestClient, project_root: Path) -> None:
    created = client.post("/api/projects", json={"name": "Web", "path": str(project_root)}).json()

    response = client.get(f"/api/projects/{created['id']}")

    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_unknown_project_returns_standard_not_found(client: TestClient) -> None:
    response = client.get("/api/projects/4242")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert response.json()["error"]["message"]


def test_delete_deactivates_without_removing_files(client: TestClient, project_root: Path) -> None:
    """DELETE fiziksel dosyaları silmez; yalnızca kaydı pasife alır."""
    project_dir = project_root / "web"
    project_dir.mkdir()
    playbook = project_dir / "site.yml"
    playbook.write_text("- hosts: all", encoding="utf-8")
    created = client.post("/api/projects", json={"name": "Web", "path": str(project_dir)}).json()

    response = client.delete(f"/api/projects/{created['id']}")

    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert project_dir.is_dir()
    assert playbook.read_text(encoding="utf-8") == "- hosts: all"
    # Kayıt hâlâ okunabilir, yalnızca varsayılan listede görünmez.
    assert client.get(f"/api/projects/{created['id']}").status_code == 200
    assert client.get("/api/projects").json() == []
    assert len(client.get("/api/projects", params={"include_inactive": True}).json()) == 1


def test_delete_is_idempotent(client: TestClient, project_root: Path) -> None:
    created = client.post("/api/projects", json={"name": "Web", "path": str(project_root)}).json()

    first = client.delete(f"/api/projects/{created['id']}")
    second = client.delete(f"/api/projects/{created['id']}")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["is_active"] is False


def test_delete_unknown_project_returns_not_found(client: TestClient) -> None:
    response = client.delete("/api/projects/4242")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_duplicate_path_returns_conflict(client: TestClient, project_root: Path) -> None:
    (project_root / "web").mkdir()
    first = client.post(
        "/api/projects", json={"name": "Bir", "path": str(project_root / "web")}
    ).json()

    response = client.post("/api/projects", json={"name": "Iki", "path": str(project_root / "web")})

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "project_already_exists"
    assert error["details"] == {"project_id": first["id"], "is_active": True}


def test_duplicate_of_inactive_project_explains_the_state(
    client: TestClient, project_root: Path
) -> None:
    created = client.post("/api/projects", json={"name": "Web", "path": str(project_root)}).json()
    client.delete(f"/api/projects/{created['id']}")

    response = client.post("/api/projects", json={"name": "Yeniden", "path": str(project_root)})

    assert response.status_code == 409
    assert "pasif" in response.json()["error"]["message"]


def test_path_outside_allowed_root_is_forbidden(client: TestClient, tmp_path: Path) -> None:
    outside = tmp_path / "disarida"
    outside.mkdir()

    response = client.post("/api/projects", json={"name": "Disarida", "path": str(outside)})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "path_not_allowed"


def test_traversal_out_of_allowed_root_is_forbidden(
    client: TestClient, project_root: Path, tmp_path: Path
) -> None:
    """`<root>/../../etc/passwd` benzeri klasik traversal denemesi."""
    (tmp_path / "disarida").mkdir()

    response = client.post(
        "/api/projects",
        json={"name": "Traversal", "path": str(project_root / ".." / "disarida")},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "path_not_allowed"


def test_symlink_escape_is_forbidden(
    client: TestClient, project_root: Path, tmp_path: Path
) -> None:
    """Root içindeki bağlantı dışarıyı gösteriyorsa API 403 döner."""
    outside = tmp_path / "gizli"
    outside.mkdir()
    link = link_directory(project_root / "gorunuste-icerde", outside)

    response = client.post("/api/projects", json={"name": "Kacis", "path": str(link)})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "path_not_allowed"


def test_error_message_does_not_leak_allowed_roots(
    client: TestClient, project_root: Path, tmp_path: Path
) -> None:
    """403 mesajı sunucudaki izinli dizinleri listelemez."""
    outside = tmp_path / "disarida"
    outside.mkdir()

    error = client.post("/api/projects", json={"name": "Disarida", "path": str(outside)}).json()[
        "error"
    ]

    assert str(project_root) not in error["message"]
    assert str(project_root) not in str(error["details"])


def test_missing_path_is_rejected(client: TestClient, project_root: Path) -> None:
    response = client.post(
        "/api/projects", json={"name": "Yok", "path": str(project_root / "hic-olmayan")}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "path_not_found"


def test_file_instead_of_directory_is_rejected(client: TestClient, project_root: Path) -> None:
    target = project_root / "site.yml"
    target.write_text("- hosts: all", encoding="utf-8")

    response = client.post("/api/projects", json={"name": "Dosya", "path": str(target)})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "path_not_a_directory"


def test_relative_path_is_rejected(client: TestClient) -> None:
    response = client.post("/api/projects", json={"name": "Relative", "path": "projeler/web"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_path"


def test_blank_name_is_rejected(client: TestClient, project_root: Path) -> None:
    response = client.post("/api/projects", json={"name": "   ", "path": str(project_root)})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"


def test_oversized_name_is_rejected(client: TestClient, project_root: Path) -> None:
    response = client.post("/api/projects", json={"name": "a" * 201, "path": str(project_root)})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"


def test_missing_body_fields_return_standard_envelope(client: TestClient) -> None:
    response = client.post("/api/projects", json={})

    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
