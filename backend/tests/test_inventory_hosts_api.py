"""`GET /api/inventories/{id}/hosts` (T-202).

Endpoint path veya komut parametresi almaz; okunacak dosya yalnızca
veritabanındaki kayıttan belirlenir. Bu testler hem mutlu yolu hem de kaydın
"dünyası" sonradan değiştiğinde üretilen güvenli hataları ölçer.

Parser olarak gerçek `ansible-inventory` yerine denetlenebilir bir stub süreci
kullanılır; gerekçe ``tests/inventory_parser_stub.py`` içindedir.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services.inventories.parser import MAX_STDERR_BYTES
from tests.support import link_directory, stub_parser_command

# Sınır aşıldıktan sonra stub'ın açık kalacağı süre. İstek bundan **önce**
# tamamlanmalıdır; tamamlanmıyorsa sınır gerçek zamanlı uygulanmıyor demektir.
HANG_SECONDS = 30

# Boyut testlerinde timeout bilinçli olarak cömerttir: hatayı üreten şeyin
# timeout değil **boyut sınırı** olduğu böyle kanıtlanır.
GENEROUS_TIMEOUT_SECONDS = 25.0

SIMPLE_OUTPUT: dict[str, Any] = {
    "_meta": {
        "hostvars": {
            "web01": {"ansible_host": "10.0.0.10"},
            "web02": {"ansible_host": "10.0.0.11"},
            "db01": {"ansible_host": "10.0.0.20"},
        }
    },
    "all": {"children": ["ungrouped", "production"]},
    "production": {"children": ["web", "db"]},
    "web": {"hosts": ["web01", "web02"]},
    "db": {"hosts": ["db01"]},
}

SECRET_OUTPUT: dict[str, Any] = {
    "_meta": {
        "hostvars": {
            "web01": {
                "ansible_host": "10.0.0.10",
                "ansible_password": "hunter2",
                "api_token": "ghp_gizli",
                "app": {"database": {"password": "p4ss"}, "port": 5432},
                "certificates": [
                    "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----"
                ],
                "vault_blob": "$ANSIBLE_VAULT;1.1;AES256\n3363346238\n",
                "header": "Authorization: Bearer sk-live-gizli",
            }
        }
    },
    "all": {"children": ["web"]},
    "web": {"hosts": ["web01"]},
}


def _write_inventory(path: Path, content: str = "[web]\nweb01\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _payload(tmp_path: Path, payload: dict[str, Any], name: str = "payload.json") -> Path:
    target = tmp_path / name
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def _use_stub(settings: Settings, behaviour: str, **options: object) -> None:
    """Etkin parser komutunu stub'a çevirir.

    ``settings`` nesnesi uygulamayla paylaşıldığı için istek anında okunur;
    test gövdesinde yapılan değişiklik sonraki isteği etkiler.
    """
    settings.ansible_inventory_command = stub_parser_command(behaviour, **options)


def _create_standalone(client: TestClient, target: Path, name: str = "Lab") -> int:
    response = client.post(
        "/api/inventories",
        json={"name": name, "path": str(target), "source_type": "ini"},
    )
    assert response.status_code == 201, response.text
    inventory_id: int = response.json()["id"]
    return inventory_id


def _create_linked(client: TestClient, target: Path, project_id: int) -> int:
    response = client.post(
        "/api/inventories",
        json={
            "name": "Prod",
            "path": str(target),
            "source_type": "ini",
            "project_id": project_id,
        },
    )
    assert response.status_code == 201, response.text
    inventory_id: int = response.json()["id"]
    return inventory_id


def _create_project(client: TestClient, path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    response = client.post("/api/projects", json={"name": "Web", "path": str(path)})
    assert response.status_code == 201, response.text
    project_id: int = response.json()["id"]
    return project_id


@pytest.fixture
def standalone_inventory(
    client: TestClient, inventory_root: Path, tmp_path: Path, settings: Settings
) -> int:
    """Stub parser'a bağlanmış, kayıtlı bir standalone inventory."""
    target = _write_inventory(inventory_root / "hosts.ini")
    inventory_id = _create_standalone(client, target)
    _use_stub(settings, "payload", payload=_payload(tmp_path, SIMPLE_OUTPUT))
    return inventory_id


# --- Mutlu yol ----------------------------------------------------------------


def test_ini_inventory_hosts_and_groups_are_returned(
    client: TestClient, standalone_inventory: int
) -> None:
    response = client.get(f"/api/inventories/{standalone_inventory}/hosts")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["inventory_id"] == standalone_inventory
    groups = {group["name"]: group["hosts"] for group in body["groups"]}
    assert groups["web"] == ["web01", "web02"]
    assert groups["db"] == ["db01"]
    assert groups["production"] == ["db01", "web01", "web02"]
    hosts = {host["name"]: host for host in body["hosts"]}
    assert hosts["web01"]["groups"] == ["all", "production", "web"]
    assert hosts["web01"]["variables"] == {"ansible_host": "10.0.0.10"}


def test_yaml_inventory_hosts_and_groups_are_returned(
    client: TestClient, inventory_root: Path, tmp_path: Path, settings: Settings
) -> None:
    target = _write_inventory(
        inventory_root / "hosts.yml",
        "all:\n  children:\n    web:\n      hosts:\n        web01:\n",
    )
    response = client.post(
        "/api/inventories",
        json={"name": "Lab YAML", "path": str(target), "source_type": "yaml"},
    )
    inventory_id = response.json()["id"]
    _use_stub(
        settings,
        "payload",
        payload=_payload(
            tmp_path,
            {
                "_meta": {"hostvars": {"web01": {"http_port": 8080}}},
                "all": {"children": ["ungrouped", "web"]},
                "web": {"hosts": ["web01"]},
            },
        ),
    )

    hosts_response = client.get(f"/api/inventories/{inventory_id}/hosts")

    assert hosts_response.status_code == 200
    body = hosts_response.json()
    assert [group["name"] for group in body["groups"]] == ["all", "ungrouped", "web"]
    assert body["hosts"][0]["name"] == "web01"
    assert body["hosts"][0]["variables"] == {"http_port": 8080}


def test_response_shape_matches_the_contract(client: TestClient, standalone_inventory: int) -> None:
    body = client.get(f"/api/inventories/{standalone_inventory}/hosts").json()

    assert set(body) == {"inventory_id", "groups", "hosts"}
    assert set(body["groups"][0]) == {"name", "hosts"}
    assert set(body["hosts"][0]) == {"name", "groups", "variables"}


def test_raw_parser_json_is_not_exposed(client: TestClient, standalone_inventory: int) -> None:
    """Ham `ansible-inventory` çıktısı cevaba sızmaz."""
    body = client.get(f"/api/inventories/{standalone_inventory}/hosts").json()

    assert "_meta" not in body
    assert "_meta" not in json.dumps(body)


def test_ordering_is_stable_across_requests(client: TestClient, standalone_inventory: int) -> None:
    first = client.get(f"/api/inventories/{standalone_inventory}/hosts").json()
    second = client.get(f"/api/inventories/{standalone_inventory}/hosts").json()

    assert first == second
    assert [group["name"] for group in first["groups"]] == sorted(
        group["name"] for group in first["groups"]
    )
    assert [host["name"] for host in first["hosts"]] == sorted(
        host["name"] for host in first["hosts"]
    )


def test_linked_inventory_can_be_parsed(
    client: TestClient, project_root: Path, tmp_path: Path, settings: Settings
) -> None:
    project_id = _create_project(client, project_root / "web")
    target = _write_inventory(project_root / "web" / "hosts.ini")
    inventory_id = _create_linked(client, target, project_id)
    _use_stub(settings, "payload", payload=_payload(tmp_path, SIMPLE_OUTPUT))

    response = client.get(f"/api/inventories/{inventory_id}/hosts")

    assert response.status_code == 200
    assert len(response.json()["hosts"]) == 3


# --- Redaction ----------------------------------------------------------------


def test_host_variables_are_redacted(
    client: TestClient, inventory_root: Path, tmp_path: Path, settings: Settings
) -> None:
    target = _write_inventory(inventory_root / "hosts.ini")
    inventory_id = _create_standalone(client, target)
    _use_stub(settings, "payload", payload=_payload(tmp_path, SECRET_OUTPUT))

    body = client.get(f"/api/inventories/{inventory_id}/hosts").json()

    variables = body["hosts"][0]["variables"]
    assert variables["ansible_host"] == "10.0.0.10"
    assert variables["ansible_password"] == "***"
    assert variables["api_token"] == "***"
    assert variables["app"] == {"database": {"password": "***"}, "port": 5432}
    assert variables["certificates"] == ["***"]
    assert variables["vault_blob"] == "***"
    assert variables["header"] == "***"


def test_no_secret_material_survives_anywhere_in_the_response(
    client: TestClient, inventory_root: Path, tmp_path: Path, settings: Settings
) -> None:
    """Cevabın tamamı taranır; secret hiçbir alanda kalmamalıdır."""
    target = _write_inventory(inventory_root / "hosts.ini")
    inventory_id = _create_standalone(client, target)
    _use_stub(settings, "payload", payload=_payload(tmp_path, SECRET_OUTPUT))

    rendered = client.get(f"/api/inventories/{inventory_id}/hosts").text

    for secret in ("hunter2", "ghp_gizli", "p4ss", "MIIE", "3363346238", "sk-live-gizli"):
        assert secret not in rendered


# --- Kayıt doğrulaması --------------------------------------------------------


def test_unknown_inventory_returns_not_found(client: TestClient) -> None:
    response = client.get("/api/inventories/4242/hosts")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_deleted_file_produces_a_safe_error(
    client: TestClient, inventory_root: Path, tmp_path: Path, settings: Settings
) -> None:
    target = _write_inventory(inventory_root / "hosts.ini")
    inventory_id = _create_standalone(client, target)
    _use_stub(settings, "payload", payload=_payload(tmp_path, SIMPLE_OUTPUT))
    target.unlink()

    response = client.get(f"/api/inventories/{inventory_id}/hosts")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "inventory_path_unavailable"
    assert error["details"] == {"inventory_id": inventory_id, "reason": "missing"}


def test_file_replaced_by_a_directory_produces_a_safe_error(
    client: TestClient, inventory_root: Path, tmp_path: Path, settings: Settings
) -> None:
    target = _write_inventory(inventory_root / "hosts.ini")
    inventory_id = _create_standalone(client, target)
    _use_stub(settings, "payload", payload=_payload(tmp_path, SIMPLE_OUTPUT))
    target.unlink()
    target.mkdir()

    response = client.get(f"/api/inventories/{inventory_id}/hosts")

    assert response.status_code == 409
    assert response.json()["error"]["details"]["reason"] == "not_a_file"


def test_allowlist_narrowed_after_registration_blocks_parsing(
    client: TestClient, inventory_root: Path, tmp_path: Path, settings: Settings
) -> None:
    """Kayıt anındaki allowlist kalıcı bir garanti değildir."""
    target = _write_inventory(inventory_root / "hosts.ini")
    inventory_id = _create_standalone(client, target)
    _use_stub(settings, "payload", payload=_payload(tmp_path, SIMPLE_OUTPUT))
    settings.inventory_root_allowlist = [tmp_path / "baska-kok"]

    response = client.get(f"/api/inventories/{inventory_id}/hosts")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "path_not_allowed"


def test_symlink_escape_after_registration_blocks_parsing(
    client: TestClient, inventory_root: Path, tmp_path: Path, settings: Settings
) -> None:
    """Kayıt sonrası dizin, kök dışını gösteren bir bağlantıyla değiştirilirse."""
    inner = inventory_root / "envanter"
    target = _write_inventory(inner / "hosts.ini")
    inventory_id = _create_standalone(client, target)
    _use_stub(settings, "payload", payload=_payload(tmp_path, SIMPLE_OUTPUT))

    outside = tmp_path / "disarida"
    _write_inventory(outside / "hosts.ini")
    target.unlink()
    inner.rmdir()
    link_directory(inner, outside)

    response = client.get(f"/api/inventories/{inventory_id}/hosts")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "path_not_allowed"


def test_inventory_that_escaped_its_project_blocks_parsing(
    client: TestClient, project_root: Path, tmp_path: Path, settings: Settings
) -> None:
    """Bağlı inventory sonradan project dışına yönlendirilirse reddedilir."""
    project_id = _create_project(client, project_root / "web")
    inner = project_root / "web" / "envanter"
    target = _write_inventory(inner / "hosts.ini")
    inventory_id = _create_linked(client, target, project_id)
    _use_stub(settings, "payload", payload=_payload(tmp_path, SIMPLE_OUTPUT))

    outside_project = project_root / "baska"
    _write_inventory(outside_project / "hosts.ini")
    target.unlink()
    inner.rmdir()
    link_directory(inner, outside_project)

    response = client.get(f"/api/inventories/{inventory_id}/hosts")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "inventory_path_outside_project"


def test_inactive_project_blocks_parsing(
    client: TestClient, project_root: Path, tmp_path: Path, settings: Settings
) -> None:
    project_id = _create_project(client, project_root / "web")
    target = _write_inventory(project_root / "web" / "hosts.ini")
    inventory_id = _create_linked(client, target, project_id)
    _use_stub(settings, "payload", payload=_payload(tmp_path, SIMPLE_OUTPUT))
    client.delete(f"/api/projects/{project_id}")

    response = client.get(f"/api/inventories/{inventory_id}/hosts")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "project_inactive"


# --- Parser arızaları ---------------------------------------------------------


def test_missing_parser_is_reported_in_the_standard_envelope(
    client: TestClient, standalone_inventory: int, settings: Settings, tmp_path: Path
) -> None:
    settings.ansible_inventory_command = [str(tmp_path / "hic-olmayan-parser")]

    response = client.get(f"/api/inventories/{standalone_inventory}/hosts")

    assert response.status_code == 503
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "inventory_parser_unavailable"
    assert "ansible-core" in body["error"]["message"]


def test_crashing_parser_is_reported_as_infrastructure_not_content(
    client: TestClient, standalone_inventory: int, settings: Settings
) -> None:
    """Çöken parser 503 üretir; kullanıcıya "dosyan bozuk" (422) denmez."""
    _use_stub(settings, "crash")

    response = client.get(f"/api/inventories/{standalone_inventory}/hosts")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "inventory_parser_unavailable"


def test_crashing_parser_does_not_leak_internal_details(
    client: TestClient, standalone_inventory: int, settings: Settings
) -> None:
    _use_stub(settings, "crash")

    rendered = client.get(f"/api/inventories/{standalone_inventory}/hosts").text

    for leak in ("Traceback", "runpy", "check_blocking_io", "AttributeError", "ansible/cli"):
        assert leak not in rendered


def test_timeout_is_reported_safely(
    client: TestClient, standalone_inventory: int, settings: Settings
) -> None:
    settings.inventory_parse_timeout_seconds = 1.0
    _use_stub(settings, "sleep", sleep_seconds=10)

    response = client.get(f"/api/inventories/{standalone_inventory}/hosts")

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "inventory_parse_timeout"


def test_oversized_output_is_reported_safely(
    client: TestClient, standalone_inventory: int, settings: Settings
) -> None:
    settings.inventory_parse_max_output_bytes = 100_000
    _use_stub(settings, "huge", size_bytes=400_000)

    response = client.get(f"/api/inventories/{standalone_inventory}/hosts")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "inventory_parse_output_too_large"


def test_oversized_output_does_not_wait_for_the_parser_to_finish(
    client: TestClient, standalone_inventory: int, settings: Settings
) -> None:
    """Endpoint, sınırı aşan parser'ın doğal bitişini **beklemez**.

    Stub sınırı aşacak kadar yazar, sonra `HANG_SECONDS` boyunca açık kalır.
    Sınır yalnızca süreç bittikten sonra ölçülseydi istek bu süre kadar asılı
    kalır ve boyut hatası yerine timeout üretirdi.
    """
    settings.inventory_parse_max_output_bytes = 100_000
    settings.inventory_parse_timeout_seconds = GENEROUS_TIMEOUT_SECONDS
    _use_stub(settings, "huge-then-hang", size_bytes=400_000, sleep_seconds=HANG_SECONDS)
    started = time.monotonic()

    response = client.get(f"/api/inventories/{standalone_inventory}/hosts")

    elapsed = time.monotonic() - started
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "inventory_parse_output_too_large"
    assert error["details"] == {"stream": "stdout"}
    assert elapsed < HANG_SECONDS, f"İstek parser'ın doğal bitişini bekledi ({elapsed:.1f}s)."


def test_stderr_flood_does_not_wait_for_the_parser_to_finish(
    client: TestClient, standalone_inventory: int, settings: Settings
) -> None:
    """stderr de gerçek bir üst sınıra tabidir; sınırsız büyüyemez.

    Hata çıktısı yalnızca gösterilirken kırpılsaydı, süreç sınırsız veri
    üretmeye devam edebilirdi. Cevap `stderr` akışını işaret etmelidir.
    """
    settings.inventory_parse_timeout_seconds = GENEROUS_TIMEOUT_SECONDS
    _use_stub(
        settings,
        "stderr-flood-then-hang",
        size_bytes=4 * MAX_STDERR_BYTES,
        sleep_seconds=HANG_SECONDS,
    )
    started = time.monotonic()

    response = client.get(f"/api/inventories/{standalone_inventory}/hosts")

    elapsed = time.monotonic() - started
    error = response.json()["error"]
    assert response.status_code == 502
    assert error["code"] == "inventory_parse_output_too_large"
    assert error["details"] == {"stream": "stderr"}
    assert elapsed < HANG_SECONDS, f"İstek parser'ın doğal bitişini bekledi ({elapsed:.1f}s)."


def test_invalid_json_is_reported_safely(
    client: TestClient, standalone_inventory: int, settings: Settings
) -> None:
    _use_stub(settings, "invalid-json")

    response = client.get(f"/api/inventories/{standalone_inventory}/hosts")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "inventory_parse_invalid_output"


def test_json_array_output_is_reported_safely(
    client: TestClient, standalone_inventory: int, settings: Settings
) -> None:
    _use_stub(settings, "json-array")

    response = client.get(f"/api/inventories/{standalone_inventory}/hosts")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "inventory_parse_invalid_output"


def test_unparseable_inventory_is_reported_as_a_content_error(
    client: TestClient, standalone_inventory: int, settings: Settings
) -> None:
    _use_stub(settings, "fail")

    response = client.get(f"/api/inventories/{standalone_inventory}/hosts")

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "inventory_parse_failed"
    assert body["error"]["details"]["parser_message"]


def test_parse_error_does_not_leak_paths_or_secrets(
    client: TestClient,
    standalone_inventory: int,
    settings: Settings,
    inventory_root: Path,
) -> None:
    _use_stub(settings, "fail")

    rendered = client.get(f"/api/inventories/{standalone_inventory}/hosts").text

    assert str(inventory_root) not in rendered
    assert "hunter2" not in rendered
    assert "Traceback" not in rendered


def test_every_error_uses_the_standard_envelope(
    client: TestClient, standalone_inventory: int, settings: Settings
) -> None:
    """Bütün parser hataları aynı zarfla döner; istemci tek yapı bekler."""
    for behaviour in ("fail", "invalid-json", "json-array"):
        _use_stub(settings, behaviour)

        body = client.get(f"/api/inventories/{standalone_inventory}/hosts").json()

        assert set(body) == {"error"}
        assert set(body["error"]) == {"code", "message", "details"}
        assert body["error"]["message"]


# --- Endpoint sözleşmesi ------------------------------------------------------


def test_endpoint_ignores_client_supplied_path_or_command(
    client: TestClient, standalone_inventory: int, tmp_path: Path
) -> None:
    """Query parametreleri okunacak dosyayı veya komutu değiştiremez."""
    outside = _write_inventory(tmp_path / "disarida" / "hosts.ini")

    response = client.get(
        f"/api/inventories/{standalone_inventory}/hosts",
        params={"path": str(outside), "command": "whoami", "inventory": str(outside)},
    )

    assert response.status_code == 200
    # Cevap hâlâ kayıtlı dosyanın içeriğidir, dışarıdan verilenin değil.
    assert {host["name"] for host in response.json()["hosts"]} == {"web01", "web02", "db01"}
