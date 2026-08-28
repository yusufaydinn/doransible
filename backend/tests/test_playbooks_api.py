"""Playbook keşfi API'si (T-103)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.support import link_directory

PLAYBOOK = "---\n- name: Ornek\n  hosts: all\n"
ROLE_TASKS = "---\n- name: Paket\n  ansible.builtin.apt:\n    name: nginx\n"
VARS_FILE = "---\nnginx_port: 80\n"


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def create_project(client: TestClient, project_dir: Path, name: str = "Web") -> int:
    project_dir.mkdir(parents=True, exist_ok=True)
    response = client.post("/api/projects", json={"name": name, "path": str(project_dir)})
    assert response.status_code == 201
    project_id: int = response.json()["id"]
    return project_id


def test_playbooks_are_listed(client: TestClient, project_root: Path) -> None:
    project_dir = project_root / "proje"
    project_id = create_project(client, project_dir)
    write(project_dir / "site.yml", PLAYBOOK)
    write(project_dir / "playbooks" / "web.yaml", PLAYBOOK)

    response = client.get(f"/api/projects/{project_id}/playbooks")

    assert response.status_code == 200
    body = response.json()
    assert [item["path"] for item in body["playbooks"]] == ["playbooks/web.yaml", "site.yml"]
    assert body["project_id"] == project_id
    assert body["truncated"] is False
    assert body["skipped_unreadable_files"] == 0


def test_role_internals_and_var_files_are_excluded(client: TestClient, project_root: Path) -> None:
    project_dir = project_root / "proje"
    project_id = create_project(client, project_dir)
    write(project_dir / "site.yml", PLAYBOOK)
    write(project_dir / "roles" / "nginx" / "tasks" / "main.yml", ROLE_TASKS)
    write(project_dir / "roles" / "nginx" / "handlers" / "main.yml", ROLE_TASKS)
    write(project_dir / "roles" / "nginx" / "defaults" / "main.yml", VARS_FILE)
    write(project_dir / "roles" / "nginx" / "vars" / "main.yml", VARS_FILE)
    write(project_dir / "group_vars" / "all.yml", VARS_FILE)
    write(project_dir / "host_vars" / "web1.yml", VARS_FILE)
    write(project_dir / "inventory.yml", PLAYBOOK)
    write(project_dir / "README.md", "# proje")

    response = client.get(f"/api/projects/{project_id}/playbooks")

    assert [item["path"] for item in response.json()["playbooks"]] == ["site.yml"]


def test_response_never_contains_absolute_server_paths(
    client: TestClient, project_root: Path
) -> None:
    project_dir = project_root / "proje"
    project_id = create_project(client, project_dir)
    write(project_dir / "playbooks" / "web.yml", PLAYBOOK)

    raw = client.get(f"/api/projects/{project_id}/playbooks").text

    assert str(project_dir) not in raw
    assert str(project_root) not in raw
    assert project_root.name not in raw


def test_unknown_project_returns_standard_not_found(client: TestClient) -> None:
    response = client.get("/api/projects/4242/playbooks")

    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "not_found"


def test_inactive_project_returns_conflict(client: TestClient, project_root: Path) -> None:
    project_dir = project_root / "proje"
    project_id = create_project(client, project_dir)
    write(project_dir / "site.yml", PLAYBOOK)
    client.delete(f"/api/projects/{project_id}")

    response = client.get(f"/api/projects/{project_id}/playbooks")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "project_inactive"


def test_deleted_directory_returns_explainable_error(
    client: TestClient, project_root: Path
) -> None:
    project_dir = project_root / "proje"
    project_id = create_project(client, project_dir)
    project_dir.rmdir()

    response = client.get(f"/api/projects/{project_id}/playbooks")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "project_path_unavailable"
    assert error["details"]["reason"] == "missing"


def test_path_turned_into_file_returns_error(client: TestClient, project_root: Path) -> None:
    project_dir = project_root / "proje"
    project_id = create_project(client, project_dir)
    project_dir.rmdir()
    project_dir.write_text("artik dosya", encoding="utf-8")

    response = client.get(f"/api/projects/{project_id}/playbooks")

    assert response.status_code == 409
    assert response.json()["error"]["details"]["reason"] == "not_a_directory"


def test_escaping_link_is_not_listed(
    client: TestClient, project_root: Path, tmp_path: Path
) -> None:
    project_dir = project_root / "proje"
    project_id = create_project(client, project_dir)
    write(project_dir / "site.yml", PLAYBOOK)
    outside = tmp_path / "disarida"
    write(outside / "gizli.yml", PLAYBOOK)
    link_directory(project_dir / "kacis", outside)

    body = client.get(f"/api/projects/{project_id}/playbooks").json()

    assert [item["path"] for item in body["playbooks"]] == ["site.yml"]
    assert "gizli" not in client.get(f"/api/projects/{project_id}/playbooks").text


def test_unreadable_candidate_is_counted_not_fatal(client: TestClient, project_root: Path) -> None:
    project_dir = project_root / "proje"
    project_id = create_project(client, project_dir)
    write(project_dir / "site.yml", PLAYBOOK)
    (project_dir / "ikili.yml").write_bytes(b"\xff\xfe\x00binary")

    response = client.get(f"/api/projects/{project_id}/playbooks")

    assert response.status_code == 200
    body = response.json()
    assert [item["path"] for item in body["playbooks"]] == ["site.yml"]
    assert body["skipped_unreadable_files"] == 1


def test_endpoint_ignores_free_form_path_parameters(
    client: TestClient, project_root: Path, tmp_path: Path
) -> None:
    """Endpoint serbest path/glob kabul etmez; verilen parametre sonucu değiştirmez."""
    project_dir = project_root / "proje"
    project_id = create_project(client, project_dir)
    write(project_dir / "site.yml", PLAYBOOK)
    outside = tmp_path / "disarida"
    write(outside / "gizli.yml", PLAYBOOK)

    baseline = client.get(f"/api/projects/{project_id}/playbooks").json()["playbooks"]

    for params in (
        {"path": str(outside)},
        {"path": "../.."},
        {"glob": "**/*.yml"},
        {"root": str(outside)},
        {"include_inactive": "true"},
    ):
        response = client.get(f"/api/projects/{project_id}/playbooks", params=params)
        assert response.status_code == 200
        assert response.json()["playbooks"] == baseline
        assert "gizli" not in response.text


def test_non_numeric_project_id_is_rejected(client: TestClient) -> None:
    """`project_id` int'tir; path segmentine dizin adı yazılamaz."""
    response = client.get("/api/projects/etc/playbooks")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"


def test_path_traversal_in_the_url_never_succeeds(client: TestClient) -> None:
    """Traversal denemeleri route'a hiç ulaşmaz veya doğrulamada düşer."""
    for suffix in ("..%2F..%2Fetc", "../../etc", "%2e%2e%2f%2e%2e", "1/../../../etc"):
        response = client.get(f"/api/projects/{suffix}/playbooks")

        assert response.status_code in {404, 422}, suffix
        assert "playbooks" not in response.json(), suffix


def test_response_shape_is_stable(client: TestClient, project_root: Path) -> None:
    project_dir = project_root / "proje"
    project_id = create_project(client, project_dir)
    write(project_dir / "site.yml", PLAYBOOK)

    body = client.get(f"/api/projects/{project_id}/playbooks").json()

    assert set(body) == {
        "project_id",
        "playbooks",
        "skipped_unreadable_files",
        "skipped_unreadable_directories",
        "truncated",
        "scanned_at",
    }
    assert set(body["playbooks"][0]) == {"path", "name", "size_bytes", "modified_at"}
