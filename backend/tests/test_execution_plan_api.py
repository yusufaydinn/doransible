"""`POST /api/projects/{id}/execution-plan` sözleşmesi (R1-V1).

Bu dilimin merkez iddiası şudur: **plan üretimi hiçbir playbook çalıştırmaz.**
`ansible-runner` ve `ansible-playbook` çağrılmaz, Job satırı yazılmaz, artifact
ve plan state'i oluşturulmaz, onay token'ı dağıtılmaz. Başlatılan tek alt süreç
inventory'yi okuyan parser'dır.

Parser olarak gerçek `ansible-inventory` yerine denetlenebilir bir stub süreci
kullanılır (gerekçe ``tests/inventory_parser_stub.py`` içindedir); subprocess
katmanı taklit edilmez.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.core.config import Settings
from app.services.execution import MAX_PREVIEW_HOSTS
from tests.support import link_directory, stub_parser_command

PLAYBOOK = "---\n- name: Ornek\n  hosts: all\n"
ROLE_TASKS = "---\n- name: Paket\n  ansible.builtin.apt:\n    name: nginx\n"
INVENTORY_TEXT = "[web]\nweb01\n"

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


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _payload_file(tmp_path: Path, payload: dict[str, Any], name: str = "payload.json") -> Path:
    target = tmp_path / name
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def _use_stub(settings: Settings, behaviour: str, **options: object) -> None:
    """Etkin parser komutunu stub'a çevirir.

    ``settings`` uygulamayla paylaşıldığı için değişiklik sonraki isteği
    etkiler.
    """
    settings.ansible_inventory_command = stub_parser_command(behaviour, **options)


def _create_project(client: TestClient, path: Path, name: str = "Web") -> int:
    path.mkdir(parents=True, exist_ok=True)
    response = client.post("/api/projects", json={"name": name, "path": str(path)})
    assert response.status_code == 201, response.text
    project_id: int = response.json()["id"]
    return project_id


def _create_linked_inventory(
    client: TestClient, target: Path, project_id: int, name: str = "Prod"
) -> int:
    response = client.post(
        "/api/inventories",
        json={
            "name": name,
            "path": str(target),
            "source_type": "ini",
            "project_id": project_id,
        },
    )
    assert response.status_code == 201, response.text
    inventory_id: int = response.json()["id"]
    return inventory_id


def _create_standalone_inventory(client: TestClient, target: Path) -> int:
    response = client.post(
        "/api/inventories",
        json={"name": "Bagimsiz", "path": str(target), "source_type": "ini"},
    )
    assert response.status_code == 201, response.text
    inventory_id: int = response.json()["id"]
    return inventory_id


def _plan(client: TestClient, project_id: int, **payload: Any) -> httpx.Response:
    """Plan isteği gönderir.

    ``mode`` çağıran tarafından verilmezse ``check`` varsayılır (R1-V3H2A'dan
    beri istekte zorunlu bir alandır, ama bu dosyanın çoğu testi kip seçimini
    değil başka bir davranışı ölçer); açıkça farklı bir kip test etmek isteyen
    çağrı ``mode=...`` geçerek varsayılanı ezebilir.

    ``TestClient.post`` gevşek tiplenmiş olduğu için sonuç açıkça daraltılır.
    """
    payload.setdefault("mode", "check")
    return cast(
        httpx.Response,
        client.post(f"/api/projects/{project_id}/execution-plan", json=payload),
    )


@pytest.fixture
def project_dir(project_root: Path) -> Path:
    """Playbook ve inventory taşıyan gerçek bir project dizini."""
    directory = project_root / "proje"
    directory.mkdir(parents=True, exist_ok=True)
    _write(directory / "site.yml", PLAYBOOK)
    _write(directory / "playbooks" / "web.yml", PLAYBOOK)
    _write(directory / "inventories" / "production.ini", INVENTORY_TEXT)
    return directory


@pytest.fixture
def plan_context(
    client: TestClient, project_dir: Path, tmp_path: Path, settings: Settings
) -> tuple[int, int]:
    """Kayıtlı project ve ona bağlı inventory; parser stub'a bağlanmıştır."""
    project_id = _create_project(client, project_dir)
    inventory_id = _create_linked_inventory(
        client, project_dir / "inventories" / "production.ini", project_id
    )
    _use_stub(settings, "payload", payload=_payload_file(tmp_path, SIMPLE_OUTPUT))
    return project_id, inventory_id


# --- Plan hiçbir şey çalıştırmaz ----------------------------------------------


def test_plan_never_starts_a_playbook_or_runner_process(
    client: TestClient, plan_context: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Başlatılan tek süreç türü parser'dır; `ansible-playbook` yoktur."""
    project_id, inventory_id = plan_context
    invocations: list[list[str]] = []
    real_popen = subprocess.Popen

    def _capture(args: Any, **kwargs: Any) -> Any:
        invocations.append([str(part) for part in args])
        return real_popen(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _capture)

    response = _plan(client, project_id, inventory_id=inventory_id, playbook_path="site.yml")

    assert response.status_code == 200, response.text
    assert invocations, "inventory parser gerçek bir süreç başlatmalıydı"
    for argv in invocations:
        joined = " ".join(argv)
        assert "ansible-playbook" not in joined
        assert "ansible_runner" not in joined
        assert "ansible-runner" not in joined
        assert not any(part in {"ssh", "sshpass"} for part in argv)
        assert "--check" not in argv


def test_plan_writes_no_job_row(
    client: TestClient, plan_context: tuple[int, int], migrated_engine: Engine
) -> None:
    """Job tablosuna hiçbir satır yazılmaz."""
    project_id, inventory_id = plan_context

    assert (
        _plan(client, project_id, inventory_id=inventory_id, playbook_path="site.yml").status_code
        == 200
    )

    with migrated_engine.connect() as connection:
        job_count = connection.execute(text("SELECT COUNT(*) FROM jobs")).scalar_one()
    assert job_count == 0


def test_plan_creates_no_artifact_or_plan_state(
    client: TestClient, plan_context: tuple[int, int], settings: Settings
) -> None:
    """Artifact dizini ve ping preview state alanı boş kalır."""
    project_id, inventory_id = plan_context

    assert (
        _plan(client, project_id, inventory_id=inventory_id, playbook_path="site.yml").status_code
        == 200
    )

    jobs_dir = settings.app_data_dir / "jobs"
    assert not jobs_dir.exists() or list(jobs_dir.iterdir()) == []
    preview_dir = settings.resolve_ping_preview_dir()
    assert not preview_dir.exists() or list(preview_dir.iterdir()) == []


# --- Sözleşme ------------------------------------------------------------------


def test_plan_returns_full_contract(client: TestClient, plan_context: tuple[int, int]) -> None:
    """Mutlu yol; alanlar ve sabitler sözleşmedeki gibidir."""
    project_id, inventory_id = plan_context

    response = _plan(
        client, project_id, inventory_id=inventory_id, playbook_path="playbooks/web.yml"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["project"] == {"id": project_id, "name": "Web"}
    assert body["inventory"] == {"id": inventory_id, "name": "Prod", "binding": "project"}
    assert body["playbook"]["path"] == "playbooks/web.yml"
    # Metadata keşif descriptor'ından olduğu gibi taşınır; keşif görünen adı da
    # göreli yol olarak üretir (T-103).
    assert body["playbook"]["name"] == "playbooks/web.yml"
    assert body["playbook"]["size_bytes"] == len(PLAYBOOK.encode("utf-8"))
    assert body["mode"] == "check"
    assert body["limit"] is None
    assert body["tags"] is None
    assert body["skip_tags"] is None
    assert body["host_count"] == 3
    assert body["hosts_truncated"] is False
    assert body["connection"] == "ssh"
    assert body["host_key_policy"] == "strict"
    assert body["become"] is False
    assert body["executable"] is False
    assert body["not_executable_reason"] == "execution_not_enabled"
    assert body["generated_at"].endswith("Z") or "+00:00" in body["generated_at"]
    assert set(body) == {
        "project",
        "inventory",
        "playbook",
        "mode",
        "limit",
        "tags",
        "skip_tags",
        "host_count",
        "hosts",
        "hosts_truncated",
        "connection",
        "host_key_policy",
        "become",
        "executable",
        "not_executable_reason",
        "generated_at",
    }


def test_plan_carries_no_token_field(client: TestClient, plan_context: tuple[int, int]) -> None:
    """Cevapta onay token'ı yoktur; plan çalıştırılabilir bir onay değildir."""
    project_id, inventory_id = plan_context

    raw = _plan(client, project_id, inventory_id=inventory_id, playbook_path="site.yml").text

    for forbidden in ("token", "preview_token", "expires_at"):
        assert forbidden not in raw


def test_hosts_are_alphabetically_ordered(
    client: TestClient, plan_context: tuple[int, int]
) -> None:
    """Host adları deterministik ve alfabetiktir."""
    project_id, inventory_id = plan_context

    body = _plan(client, project_id, inventory_id=inventory_id, playbook_path="site.yml").json()

    assert body["hosts"] == ["db01", "web01", "web02"]
    assert body["hosts"] == sorted(body["hosts"])


def test_host_list_is_truncated_at_the_documented_limit(
    client: TestClient,
    project_dir: Path,
    tmp_path: Path,
    settings: Settings,
) -> None:
    """`hosts` en fazla 100 ad taşır; `host_count` kesin toplamı korur."""
    total = MAX_PREVIEW_HOSTS + 25
    names = [f"host{index:03d}" for index in range(total)]
    payload: dict[str, Any] = {
        "_meta": {"hostvars": {name: {} for name in names}},
        "all": {"children": ["web"]},
        "web": {"hosts": names},
    }
    project_id = _create_project(client, project_dir)
    inventory_id = _create_linked_inventory(
        client, project_dir / "inventories" / "production.ini", project_id
    )
    _use_stub(settings, "payload", payload=_payload_file(tmp_path, payload))

    body = _plan(client, project_id, inventory_id=inventory_id, playbook_path="site.yml").json()

    assert body["host_count"] == total
    assert len(body["hosts"]) == MAX_PREVIEW_HOSTS
    assert body["hosts"] == sorted(names)[:MAX_PREVIEW_HOSTS]
    assert body["hosts_truncated"] is True


# --- Inventory bağı ------------------------------------------------------------


def test_standalone_inventory_is_rejected(
    client: TestClient, project_dir: Path, inventory_root: Path
) -> None:
    """Project'siz inventory ile plan üretilemez (ADR-021 Karar 11)."""
    project_id = _create_project(client, project_dir)
    standalone = _write(inventory_root / "hosts.ini", INVENTORY_TEXT)
    inventory_id = _create_standalone_inventory(client, standalone)

    response = _plan(client, project_id, inventory_id=inventory_id, playbook_path="site.yml")

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "inventory_not_linked_to_project"
    assert str(standalone) not in response.text
    assert inventory_root.name not in response.text


def test_inventory_of_another_project_is_rejected_without_leaking_it(
    client: TestClient, project_root: Path
) -> None:
    """Başka project'e bağlı inventory reddedilir ve o project sızmaz."""
    first_dir = project_root / "birinci"
    other_dir = project_root / "gizli-dizin"
    first_id = _create_project(client, first_dir, name="Birinci")
    other_id = _create_project(client, other_dir, name="GIZLIPROJE")
    _write(first_dir / "site.yml", PLAYBOOK)
    other_inventory = _write(other_dir / "hosts.ini", INVENTORY_TEXT)
    inventory_id = _create_linked_inventory(client, other_inventory, other_id, name="GizliEnvanter")

    response = _plan(client, first_id, inventory_id=inventory_id, playbook_path="site.yml")

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"]["code"] == "inventory_not_linked_to_project"
    # Standalone reddi ile aynı kod ve aynı mesaj: hangi durumda olduğu
    # ayırt edilemez.
    assert "GIZLIPROJE" not in response.text
    assert "GizliEnvanter" not in response.text
    assert str(other_id) not in json.dumps(body["error"]["details"])
    assert str(other_dir) not in response.text


def test_inactive_project_is_rejected(client: TestClient, plan_context: tuple[int, int]) -> None:
    """Pasif project için plan üretilmez."""
    project_id, inventory_id = plan_context
    assert client.delete(f"/api/projects/{project_id}").status_code == 200

    response = _plan(client, project_id, inventory_id=inventory_id, playbook_path="site.yml")

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "project_inactive"


def test_unknown_project_returns_not_found(client: TestClient) -> None:
    response = _plan(client, 4242, inventory_id=1, playbook_path="site.yml")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_unknown_inventory_returns_not_found(client: TestClient, project_dir: Path) -> None:
    project_id = _create_project(client, project_dir)

    response = _plan(client, project_id, inventory_id=9999, playbook_path="site.yml")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --- Playbook girdisi ----------------------------------------------------------


def test_undiscovered_playbook_is_rejected(
    client: TestClient, plan_context: tuple[int, int]
) -> None:
    """Keşifte olmayan bir ad plan üretmez."""
    project_id, inventory_id = plan_context

    response = _plan(client, project_id, inventory_id=inventory_id, playbook_path="olmayan.yml")

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "playbook_not_discovered"


def test_role_internal_file_is_not_a_playbook(
    client: TestClient, plan_context: tuple[int, int], project_dir: Path
) -> None:
    """Keşfin dışladığı role içeriği plan girdisi olamaz."""
    project_id, inventory_id = plan_context
    _write(project_dir / "roles" / "nginx" / "tasks" / "main.yml", ROLE_TASKS)

    response = _plan(
        client,
        project_id,
        inventory_id=inventory_id,
        playbook_path="roles/nginx/tasks/main.yml",
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "playbook_not_discovered"


@pytest.mark.parametrize(
    "candidate",
    [
        "../../etc/hosts",
        "../site.yml",
        "/etc/hosts",
        "site.yml/../site.yml",
        "./site.yml",
    ],
)
def test_traversal_and_absolute_paths_are_rejected(
    client: TestClient, plan_context: tuple[int, int], candidate: str
) -> None:
    """Traversal ve absolute path aynı sebeple reddedilir: listede yok."""
    project_id, inventory_id = plan_context

    response = _plan(client, project_id, inventory_id=inventory_id, playbook_path=candidate)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "playbook_not_discovered"
    assert candidate not in response.text


def test_symlink_leaving_the_project_is_rejected(
    client: TestClient, plan_context: tuple[int, int], project_dir: Path, tmp_path: Path
) -> None:
    """Project dışını gösteren bağlantı üzerinden plan üretilemez."""
    project_id, inventory_id = plan_context
    outside = tmp_path / "disarisi"
    outside.mkdir()
    _write(outside / "evil.yml", PLAYBOOK)
    link_directory(project_dir / "kacak", outside)

    response = _plan(client, project_id, inventory_id=inventory_id, playbook_path="kacak/evil.yml")

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "playbook_not_discovered"
    assert str(outside) not in response.text


# --- İstek gövdesi -------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        # `mode` R1-V3H2A ile gerçek bir alan oldu; kapsam dışı kalanlar
        # (limit/tags/skip_tags/extra_vars/forks/timeout/check) hâlâ reddedilir.
        {"limit": "web"},
        {"tags": "deploy"},
        {"skip_tags": "slow"},
        {"extra_vars": {"a": 1}},
        {"forks": 50},
        {"timeout": 5},
        {"check": False},
    ],
)
def test_extra_execution_parameters_are_forbidden(
    client: TestClient, plan_context: tuple[int, int], extra: dict[str, Any]
) -> None:
    """İstemci çalıştırma parametresi gönderemez."""
    project_id, inventory_id = plan_context

    response = _plan(
        client,
        project_id,
        inventory_id=inventory_id,
        playbook_path="site.yml",
        **extra,
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "request_validation_error"


def test_missing_fields_are_rejected(client: TestClient, plan_context: tuple[int, int]) -> None:
    project_id, _ = plan_context

    response = _plan(client, project_id, playbook_path="site.yml")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"


# --- Mode seçimi (R1-V3H2A) -----------------------------------------------------


def test_normal_mode_is_accepted_and_shown_in_the_plan(
    client: TestClient, plan_context: tuple[int, int]
) -> None:
    """``mode=normal`` de kabul edilir ve plan cevabında aynen görünür."""
    project_id, inventory_id = plan_context

    response = client.post(
        f"/api/projects/{project_id}/execution-plan",
        json={"mode": "normal", "inventory_id": inventory_id, "playbook_path": "site.yml"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "normal"


def test_mode_is_a_required_field(client: TestClient, plan_context: tuple[int, int]) -> None:
    """``mode`` verilmezse istek domain katmanına ulaşmadan 422 alır."""
    project_id, inventory_id = plan_context

    response = client.post(
        f"/api/projects/{project_id}/execution-plan",
        json={"inventory_id": inventory_id, "playbook_path": "site.yml"},
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "request_validation_error"


@pytest.mark.parametrize(
    "mode",
    [
        None,
        "",
        "   ",
        "Check",
        "CHECK",
        "Normal",
        "check ",
        "dry-run",
        "diff",
        123,
        True,
    ],
)
def test_invalid_mode_values_are_rejected(
    client: TestClient, plan_context: tuple[int, int], mode: object
) -> None:
    """Bilinmeyen, boş, whitespace'li veya farklı case bir ``mode`` 422 alır."""
    project_id, inventory_id = plan_context

    response = client.post(
        f"/api/projects/{project_id}/execution-plan",
        json={"mode": mode, "inventory_id": inventory_id, "playbook_path": "site.yml"},
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "request_validation_error"


def test_oversized_playbook_path_does_not_echo_the_input(
    client: TestClient, plan_context: tuple[int, int]
) -> None:
    """Sınırı aşan girdi cevapta geri yansıtılmaz."""
    project_id, inventory_id = plan_context
    marker = "A" * 5000

    response = _plan(client, project_id, inventory_id=inventory_id, playbook_path=marker)

    assert response.status_code == 422
    assert marker not in response.text


# --- Sızıntı sınırları ---------------------------------------------------------


def test_response_never_contains_absolute_server_paths(
    client: TestClient, plan_context: tuple[int, int], project_dir: Path, project_root: Path
) -> None:
    project_id, inventory_id = plan_context

    raw = _plan(client, project_id, inventory_id=inventory_id, playbook_path="site.yml").text

    assert str(project_dir) not in raw
    assert str(project_root) not in raw
    assert project_root.name not in raw


def test_response_never_contains_host_variables_or_key_material(
    client: TestClient,
    project_dir: Path,
    tmp_path: Path,
    secrets_root: Path,
    settings: Settings,
) -> None:
    """Hostvar, bağlantı adresi ve private key yolu plana girmez."""
    key_path = secrets_root / "id_ed25519"
    key_path.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nGIZLIANAHTAR\n", encoding="utf-8")
    payload: dict[str, Any] = {
        "_meta": {
            "hostvars": {
                "web01": {
                    "ansible_host": "10.11.12.13",
                    "ansible_user": "gizlikullanici",
                    "ansible_port": 2222,
                    "ansible_ssh_private_key_file": str(key_path),
                }
            }
        },
        "all": {"children": ["web"]},
        "web": {"hosts": ["web01"]},
    }
    project_id = _create_project(client, project_dir)
    inventory_id = _create_linked_inventory(
        client, project_dir / "inventories" / "production.ini", project_id
    )
    _use_stub(settings, "payload", payload=_payload_file(tmp_path, payload))

    response = _plan(client, project_id, inventory_id=inventory_id, playbook_path="site.yml")

    assert response.status_code == 200, response.text
    raw = response.text
    assert response.json()["hosts"] == ["web01"]
    for secret in (
        "10.11.12.13",
        "gizlikullanici",
        "2222",
        str(key_path),
        "GIZLIANAHTAR",
        "ansible_ssh_private_key_file",
    ):
        assert secret not in raw


def test_unsafe_inventory_details_do_not_leak_values(
    client: TestClient, project_dir: Path, tmp_path: Path, settings: Settings
) -> None:
    """Reddedilen bir hostvar'ın **değeri** cevaba girmez."""
    payload: dict[str, Any] = {
        "_meta": {"hostvars": {"web01": {"ansible_password": "hunter2"}}},
        "all": {"children": ["web"]},
        "web": {"hosts": ["web01"]},
    }
    project_id = _create_project(client, project_dir)
    inventory_id = _create_linked_inventory(
        client, project_dir / "inventories" / "production.ini", project_id
    )
    _use_stub(settings, "payload", payload=_payload_file(tmp_path, payload))

    response = _plan(client, project_id, inventory_id=inventory_id, playbook_path="site.yml")

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "ping_inventory_unsafe"
    assert "hunter2" not in response.text


def test_parser_failure_details_are_sanitized(
    client: TestClient, project_dir: Path, settings: Settings
) -> None:
    """Parser stderr'ı ham hâlde dışarı verilmez."""
    project_id = _create_project(client, project_dir)
    inventory_path = project_dir / "inventories" / "production.ini"
    inventory_id = _create_linked_inventory(client, inventory_path, project_id)
    _use_stub(settings, "fail")

    response = _plan(client, project_id, inventory_id=inventory_id, playbook_path="site.yml")

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "inventory_parse_failed"
    assert "hunter2" not in response.text
    assert str(inventory_path) not in response.text


def test_parser_temporary_workspace_is_removed_on_failure(
    client: TestClient, project_dir: Path, settings: Settings
) -> None:
    """Parser'ın geçici çalışma dizini hata yolunda da kalıntı bırakmaz."""
    project_id = _create_project(client, project_dir)
    inventory_id = _create_linked_inventory(
        client, project_dir / "inventories" / "production.ini", project_id
    )
    _use_stub(settings, "fail")
    system_temp = Path(tempfile.gettempdir())
    before = set(system_temp.glob("ansibleops-inventory-*"))

    assert (
        _plan(client, project_id, inventory_id=inventory_id, playbook_path="site.yml").status_code
        == 422
    )

    assert set(system_temp.glob("ansibleops-inventory-*")) == before
