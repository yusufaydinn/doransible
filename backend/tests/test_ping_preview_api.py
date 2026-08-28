"""Ping preview API sözleşmesi (T-204A).

Bu testler gerçek `ansible-inventory` sürecini kullanır (Phase 1 ve Phase 1b);
yalnızca ölçülemeyen arıza yolları için stub'a düşülür. Kritik iddia şudur:
**preview hiçbir SSH bağlantısı kurmaz ve hiçbir ansible ad-hoc ping
çalıştırmaz.**
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services.jobs.preview import META_FILENAME, SNAPSHOT_FILENAME, token_digest
from tests.support import real_parser_available, stub_parser_command

pytestmark = pytest.mark.skipif(
    not real_parser_available(),
    reason="`ansible-inventory` bu platformda çalıştırılamıyor (Ansible control node desteği).",
)

INI_INVENTORY = """\
[web]
web01 ansible_host=10.0.0.10
web02 ansible_host=10.0.0.11

[db]
db01 ansible_host=10.0.0.20

[production:children]
web
db
"""


@pytest.fixture
def inventory_id(client: TestClient, inventory_root: Path) -> int:
    """Kaydedilmiş, gerçek bir standalone inventory."""
    path = inventory_root / "hosts.ini"
    path.write_text(INI_INVENTORY, encoding="utf-8")
    response = client.post(
        "/api/inventories",
        json={"name": "prod", "path": str(path), "source_type": "ini"},
    )
    assert response.status_code == 201
    return int(response.json()["id"])


def _preview(client: TestClient, inventory_id: int, **payload: Any) -> httpx.Response:
    """Preview isteği gönderir.

    ``TestClient.post`` gevşek tiplenmiş olduğu için sonuç açıkça daraltılır;
    böylece çağıran testlerde ``response.status_code`` tip denetiminden geçer.
    """
    return cast(
        httpx.Response,
        client.post(f"/api/inventories/{inventory_id}/ping/preview", json=payload),
    )


def _preview_dirs(settings: Settings) -> list[Path]:
    root = settings.resolve_ping_preview_dir()
    return sorted(root.iterdir()) if root.is_dir() else []


# --- Preview hiçbir şey çalıştırmaz -------------------------------------------


def test_preview_never_starts_an_ssh_or_adhoc_ping(
    client: TestClient, inventory_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Başlatılan tek süreç türü `ansible-inventory`'dir.

    `ansible`, `ssh`, `sshpass` veya `-m ping` içeren hiçbir çağrı olmamalıdır.
    """
    invocations: list[list[str]] = []
    real_popen = subprocess.Popen

    def _capture(args: Any, **kwargs: Any) -> Any:
        invocations.append(list(args))
        return real_popen(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _capture)

    assert _preview(client, inventory_id).status_code == 200

    assert invocations, "Phase 1 gerçek bir süreç başlatmalıydı"
    for argv in invocations:
        executable = Path(argv[0]).name
        assert executable == "ansible-inventory", f"beklenmeyen süreç: {executable}"
        assert "ping" not in argv
        assert "-m" not in argv
        assert "--module-name" not in argv
        assert not any(part in {"ssh", "sshpass"} for part in argv)


def test_preview_creates_no_job_and_no_artifact_directory(
    client: TestClient, inventory_id: int, settings: Settings
) -> None:
    """T-204A'da Job modeli ve artifact yazımı **yoktur**."""
    assert _preview(client, inventory_id).status_code == 200

    jobs_dir = settings.app_data_dir / "jobs"
    assert jobs_dir.is_dir()
    assert list(jobs_dir.iterdir()) == []


# --- Plan sözleşmesi ----------------------------------------------------------


def test_plan_reports_the_exact_host_count(client: TestClient, inventory_id: int) -> None:
    body = _preview(client, inventory_id).json()

    assert body["plan"]["host_count"] == 3
    assert body["plan"]["hosts"] == ["db01", "web01", "web02"]
    assert body["plan"]["hosts_truncated"] is False


def test_plan_exposes_only_safe_fields(client: TestClient, inventory_id: int) -> None:
    """Adres, kullanıcı, anahtar yolu ve diğer hostvar'lar cevapta yoktur."""
    response = _preview(client, inventory_id)
    rendered = response.text
    plan = response.json()["plan"]

    assert plan["operation"] == "ansible.builtin.ping"
    assert plan["connection"] == "ssh"
    assert plan["host_key_policy"] == "strict"
    assert plan["become"] is False
    assert plan["inventory"]["binding"] == "standalone"
    for leak in ("10.0.0.10", "ansible_host", "hostvars", "_meta", "ansible_user"):
        assert leak not in rendered


def test_plan_effect_text_is_honest(client: TestClient, inventory_id: int) -> None:
    """Mutlak güvence verilmez: ping uzakta modül ve süreç oluşturur."""
    effect = _preview(client, inventory_id).json()["plan"]["operation_effect"]

    assert "SSH" in effect
    assert "Hiçbir değişiklik yapılmaz" not in effect


def test_plan_does_not_leak_the_server_side_path(
    client: TestClient, inventory_id: int, inventory_root: Path
) -> None:
    """Onay için gereken bilgi hangi kaydın hedeflendiğidir, dosyanın yeri değil."""
    assert str(inventory_root) not in _preview(client, inventory_id).text


def test_host_list_is_truncated_but_the_count_stays_exact(
    client: TestClient, inventory_id: int, settings: Settings
) -> None:
    settings.ping_preview_max_listed_hosts = 2

    plan = _preview(client, inventory_id).json()["plan"]

    assert plan["host_count"] == 3
    assert plan["hosts"] == ["db01", "web01"]
    assert plan["hosts_truncated"] is True


def test_project_bound_inventory_is_described_in_the_plan(
    client: TestClient, project_root: Path
) -> None:
    project_dir = project_root / "web-altyapi"
    project_dir.mkdir()
    inventory = project_dir / "hosts.ini"
    inventory.write_text(INI_INVENTORY, encoding="utf-8")
    project_id = client.post(
        "/api/projects", json={"name": "web-altyapi", "path": str(project_dir)}
    ).json()["id"]
    inventory_id = client.post(
        "/api/inventories",
        json={
            "name": "prod",
            "path": str(inventory),
            "source_type": "ini",
            "project_id": project_id,
        },
    ).json()["id"]

    plan = _preview(client, inventory_id).json()["plan"]

    assert plan["inventory"]["binding"] == "project"
    assert plan["inventory"]["project_id"] == project_id
    assert plan["inventory"]["project_name"] == "web-altyapi"


# --- Limit --------------------------------------------------------------------


def test_limit_narrows_the_frozen_target_set(client: TestClient, inventory_id: int) -> None:
    """Limit, özgün inventory'de değil **Snapshot A üzerinde** çözülür."""
    plan = _preview(client, inventory_id, limit="production:!web02").json()["plan"]

    assert plan["limit"] == "production:!web02"
    assert plan["hosts"] == ["db01", "web01"]
    assert plan["host_count"] == 2


@pytest.mark.parametrize(
    "limit", ["", "   ", "@/etc/passwd", "web[01", "!", ":", "all::", "~^web", "/etc/hosts"]
)
def test_malformed_limits_are_rejected_before_any_process_starts(
    client: TestClient,
    inventory_id: int,
    limit: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Yasaklı desen Ansible'a **hiç ulaşmaz**."""
    invocations: list[list[str]] = []
    real_popen = subprocess.Popen

    def _capture(args: Any, **kwargs: Any) -> Any:
        invocations.append(list(args))
        return real_popen(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _capture)

    response = _preview(client, inventory_id, limit=limit)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ping_invalid_limit"
    assert invocations == []


def test_limit_matching_nothing_is_reported_as_such(client: TestClient, inventory_id: int) -> None:
    response = _preview(client, inventory_id, limit="boyle-bir-grup-yok")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ping_no_hosts_matched"


def test_limit_resolution_failure_is_not_reported_as_a_parser_problem(
    client: TestClient, inventory_id: int, settings: Settings
) -> None:
    """Snapshot A'nın ayrıştırılabilirliği Phase 1'de kanıtlanmıştır.

    Bu yüzden Phase 1b'deki her arıza limite atfedilir; kullanıcıya "inventory
    bozuk" veya "parser çöktü" denmez ve Ansible'ın metni gösterilmez.
    """
    # Stub Phase 1'i geçirir, Phase 1b'de (yalnızca `--limit` verildiğinde)
    # traceback ile çöker. Gerçek `ansible-inventory` de `--limit '!'` girdisinde
    # tam olarak böyle davranır (ölçüldü: rc=250 + traceback).
    settings.ansible_inventory_command = stub_parser_command("crash-on-limit")

    response = _preview(client, inventory_id, limit="web")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ping_invalid_limit"
    assert "Traceback" not in response.text
    assert "IndexError" not in response.text


# --- Inventory güvenliği ------------------------------------------------------


@pytest.mark.parametrize("variable", ["ansible_password", "ansible_ssh_pass"])
def test_password_inventory_is_rejected_without_naming_the_variable(
    client: TestClient, inventory_root: Path, variable: str, settings: Settings
) -> None:
    path = inventory_root / "parolali.ini"
    path.write_text(f"[web]\nweb01 ansible_host=10.0.0.10 {variable}=hunter2\n", "utf-8")
    inventory_id = client.post(
        "/api/inventories",
        json={"name": "parolali", "path": str(path), "source_type": "ini"},
    ).json()["id"]

    response = _preview(client, inventory_id)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ping_inventory_unsafe"
    assert variable not in response.text
    assert "hunter2" not in response.text
    assert response.json()["error"]["details"] is None
    # Hiçbir state yayımlanmadı.
    assert _preview_dirs(settings) == []


@pytest.mark.parametrize("variable", ["ansible_password", "ansible_ssh_pass"])
def test_password_never_reaches_any_file_on_disk(
    client: TestClient, inventory_root: Path, variable: str, settings: Settings
) -> None:
    """Parola ne snapshot'a, ne meta'ya, ne de başka bir app-data dosyasına yazılır."""
    path = inventory_root / "parolali.ini"
    path.write_text(f"[web]\nweb01 ansible_host=10.0.0.10 {variable}=hunter2\n", "utf-8")
    inventory_id = client.post(
        "/api/inventories",
        json={"name": "parolali", "path": str(path), "source_type": "ini"},
    ).json()["id"]

    _preview(client, inventory_id)

    for candidate in settings.app_data_dir.rglob("*"):
        if candidate.is_file():
            content = candidate.read_bytes()
            assert b"hunter2" not in content
            assert variable.encode() not in content


@pytest.mark.parametrize(
    "line",
    [
        "web01 ansible_connection=local",
        "web01 ansible_ssh_executable=/bin/sh",
        "web01 ansible_ssh_common_args='-o ProxyCommand=/bin/sh'",
        "web01 ansible_shell_executable=/bin/sh",
        "web01 ansible_bilinmeyen_knob=1",
        "web01 ansible_become=true",
        "web01 ansible_host=-oProxyCommand=/bin/sh",
        "web01 ansible_host=root@10.0.0.10",
    ],
)
def test_unsafe_inventories_are_rejected_fail_closed(
    client: TestClient, inventory_root: Path, line: str, settings: Settings
) -> None:
    path = inventory_root / "riskli.ini"
    path.write_text(f"[web]\n{line}\n", encoding="utf-8")
    inventory_id = client.post(
        "/api/inventories",
        json={"name": "riskli", "path": str(path), "source_type": "ini"},
    ).json()["id"]

    response = _preview(client, inventory_id)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ping_inventory_unsafe"
    assert _preview_dirs(settings) == []


def test_private_key_inside_the_allowlist_is_accepted(
    client: TestClient, inventory_root: Path, secrets_root: Path
) -> None:
    key = secrets_root / "id_ed25519"
    key.write_text("anahtar", encoding="utf-8")
    path = inventory_root / "anahtarli.ini"
    path.write_text(
        f"[web]\nweb01 ansible_host=10.0.0.10 ansible_ssh_private_key_file={key}\n",
        encoding="utf-8",
    )
    inventory_id = client.post(
        "/api/inventories",
        json={"name": "anahtarli", "path": str(path), "source_type": "ini"},
    ).json()["id"]

    response = _preview(client, inventory_id)

    assert response.status_code == 200
    # Anahtar yolu API cevabında görünmez.
    assert str(key) not in response.text


def test_private_key_outside_the_allowlist_is_rejected(
    client: TestClient, inventory_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "disarida_id_rsa"
    outside.write_text("anahtar", encoding="utf-8")
    path = inventory_root / "anahtarli.ini"
    path.write_text(
        f"[web]\nweb01 ansible_host=10.0.0.10 ansible_ssh_private_key_file={outside}\n",
        encoding="utf-8",
    )
    inventory_id = client.post(
        "/api/inventories",
        json={"name": "anahtarli", "path": str(path), "source_type": "ini"},
    ).json()["id"]

    response = _preview(client, inventory_id)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ping_inventory_unsafe"
    assert str(outside) not in response.text


def test_user_variables_are_dropped_without_an_error(
    client: TestClient, inventory_root: Path, settings: Settings
) -> None:
    path = inventory_root / "kullanici-degiskenli.ini"
    path.write_text("[web]\nweb01 ansible_host=10.0.0.10 http_port=8080\n", "utf-8")
    inventory_id = client.post(
        "/api/inventories",
        json={"name": "uv", "path": str(path), "source_type": "ini"},
    ).json()["id"]

    response = _preview(client, inventory_id)

    assert response.status_code == 200
    snapshot = (_preview_dirs(settings)[0] / SNAPSHOT_FILENAME).read_text("utf-8")
    assert "http_port" not in snapshot
    assert "ansible_host" in snapshot


# --- State ve snapshot --------------------------------------------------------


def test_published_state_has_tight_permissions(
    client: TestClient, inventory_id: int, settings: Settings
) -> None:
    _preview(client, inventory_id)

    root = settings.resolve_ping_preview_dir()
    directory = _preview_dirs(settings)[0]
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / SNAPSHOT_FILENAME).stat().st_mode) == 0o600
    assert stat.S_IMODE((directory / META_FILENAME).stat().st_mode) == 0o600


def test_state_directory_is_addressed_by_the_token_digest(
    client: TestClient, inventory_id: int, settings: Settings
) -> None:
    token = _preview(client, inventory_id).json()["preview_token"]

    assert _preview_dirs(settings)[0].name == token_digest(token)


def test_snapshot_a_is_not_kept_in_the_published_state(
    client: TestClient, inventory_id: int, settings: Settings
) -> None:
    """Grup topolojisini taşıyan Snapshot A geçici workdir'de kalır."""
    _preview(client, inventory_id, limit="web")

    names = {path.name for path in _preview_dirs(settings)[0].iterdir()}
    assert names == {META_FILENAME, SNAPSHOT_FILENAME}


def test_frozen_snapshot_survives_a_change_to_the_source_inventory(
    client: TestClient, inventory_id: int, inventory_root: Path, settings: Settings
) -> None:
    """Preview'dan sonra özgün inventory değişse de snapshot değişmez.

    T-204B Phase 2 bu dondurulmuş dosyayı kullanacaktır; TOCTOU garantisi budur.
    """
    _preview(client, inventory_id)
    snapshot_path = _preview_dirs(settings)[0] / SNAPSHOT_FILENAME
    before = snapshot_path.read_text(encoding="utf-8")

    (inventory_root / "hosts.ini").write_text(
        "[web]\nsaldirgan ansible_host=203.0.113.9\n", encoding="utf-8"
    )

    assert snapshot_path.read_text(encoding="utf-8") == before
    document = json.loads(before)
    assert set(document["all"]["hosts"]) == {"db01", "web01", "web02"}
    assert "saldirgan" not in before


def test_snapshot_digest_is_recorded_for_later_verification(
    client: TestClient, inventory_id: int, settings: Settings
) -> None:
    _preview(client, inventory_id)
    directory = _preview_dirs(settings)[0]

    meta = json.loads((directory / META_FILENAME).read_text(encoding="utf-8"))
    snapshot = (directory / SNAPSHOT_FILENAME).read_bytes()
    assert meta["snapshot_sha256"] == hashlib.sha256(snapshot).hexdigest()
    assert meta["inventory_id"] == inventory_id
    assert meta["host_count"] == 3


# --- Cancel -------------------------------------------------------------------


def test_cancel_removes_the_state(
    client: TestClient, inventory_id: int, settings: Settings
) -> None:
    token = _preview(client, inventory_id).json()["preview_token"]

    response = client.post(
        f"/api/inventories/{inventory_id}/ping/preview/cancel",
        json={"preview_token": token},
    )

    assert response.status_code == 204
    assert _preview_dirs(settings) == []


@pytest.mark.parametrize("token", ["a" * 43, "kisa", "../../etc/passwd", "b" * 43])
def test_cancel_is_idempotent_for_unknown_or_replayed_tokens(
    client: TestClient, inventory_id: int, token: str
) -> None:
    """Bilinmeyen ve kullanılmış token aynı cevabı alır; oracle üretilmez."""
    response = client.post(
        f"/api/inventories/{inventory_id}/ping/preview/cancel",
        json={"preview_token": token},
    )

    assert response.status_code == 204
    assert response.content == b""


def test_cancel_of_an_already_cancelled_token_still_returns_204(
    client: TestClient, inventory_id: int
) -> None:
    token = _preview(client, inventory_id).json()["preview_token"]
    url = f"/api/inventories/{inventory_id}/ping/preview/cancel"
    assert client.post(url, json={"preview_token": token}).status_code == 204

    assert client.post(url, json={"preview_token": token}).status_code == 204


def test_cancel_does_not_start_any_process(
    client: TestClient, inventory_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = _preview(client, inventory_id).json()["preview_token"]
    invocations: list[list[str]] = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: invocations.append(list(args)))

    client.post(
        f"/api/inventories/{inventory_id}/ping/preview/cancel",
        json={"preview_token": token},
    )

    assert invocations == []


# --- İstek doğrulaması --------------------------------------------------------


def test_client_cannot_supply_execution_parameters(client: TestClient, inventory_id: int) -> None:
    """Modül adı, host pattern'i ve komut istemciden alınmaz."""
    for payload in (
        {"module": "shell"},
        {"host_pattern": "all"},
        {"command": ["ansible"]},
        {"timeout": 1},
        {"forks": 50},
    ):
        response = _preview(client, inventory_id, **payload)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "request_validation_error"


def test_unknown_inventory_returns_not_found(client: TestClient) -> None:
    response = _preview(client, 4242)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_deleted_inventory_file_is_reported_at_use_time(
    client: TestClient, inventory_id: int, inventory_root: Path
) -> None:
    """Kayıt anındaki kontroller kalıcı garanti değildir."""
    (inventory_root / "hosts.ini").unlink()

    response = _preview(client, inventory_id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "inventory_path_unavailable"


def test_inactive_project_blocks_the_preview(client: TestClient, project_root: Path) -> None:
    project_dir = project_root / "pasif"
    project_dir.mkdir()
    inventory = project_dir / "hosts.ini"
    inventory.write_text(INI_INVENTORY, encoding="utf-8")
    project_id = client.post(
        "/api/projects", json={"name": "pasif", "path": str(project_dir)}
    ).json()["id"]
    inventory_id = client.post(
        "/api/inventories",
        json={
            "name": "prod",
            "path": str(inventory),
            "source_type": "ini",
            "project_id": project_id,
        },
    ).json()["id"]
    client.delete(f"/api/projects/{project_id}")

    response = _preview(client, inventory_id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "project_inactive"


def test_preview_store_failure_is_reported_without_filesystem_detail(
    client: TestClient, inventory_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("izin yok: /srv/gizli/dizin")

    monkeypatch.setattr(os, "rename", _fail)

    response = _preview(client, inventory_id)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "ping_preview_unavailable"
    assert "/srv/gizli" not in response.text


def test_cancel_reports_infrastructure_failure_instead_of_a_silent_204(
    client: TestClient, inventory_id: int, settings: Settings
) -> None:
    """Temizlenemeyen state ``204`` ile örtülmez.

    Denetimde ölçülen davranış: beklenmeyen bir dosya yüzünden ``rmdir``
    başarısız olduğunda endpoint ``204`` dönüyor ve claim edilmiş state diskte
    kalıyordu. Artık ``500`` döner ve durum fark edilebilir olur.
    """
    token = _preview(client, inventory_id).json()["preview_token"]
    (_preview_dirs(settings)[0] / "beklenmeyen.bin").write_text("veri", encoding="utf-8")

    response = client.post(
        f"/api/inventories/{inventory_id}/ping/preview/cancel",
        json={"preview_token": token},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "ping_preview_unavailable"
    remaining = _preview_dirs(settings)
    assert len(remaining) == 1
    assert (remaining[0] / "beklenmeyen.bin").is_file()


def test_cancel_does_not_hide_a_mismatch_cleanup_failure(
    client: TestClient, inventory_id: int, settings: Settings
) -> None:
    """Mismatch idempotenttir; onu temizleyememek ise altyapı arızasıdır."""
    token = _preview(client, inventory_id).json()["preview_token"]
    directory = _preview_dirs(settings)[0]
    (directory / "beklenmeyen.bin").write_text("veri", encoding="utf-8")
    meta_file = directory / META_FILENAME
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    meta["inventory_id"] = inventory_id + 1
    meta_file.write_text(json.dumps(meta), encoding="utf-8")

    response = client.post(
        f"/api/inventories/{inventory_id}/ping/preview/cancel",
        json={"preview_token": token},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "ping_preview_unavailable"
    assert token not in response.text
    assert "beklenmeyen.bin" not in response.text


def test_cancel_reports_a_permission_failure_as_unavailable(
    client: TestClient, inventory_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rename`'in **her** hatası 409 sayılmaz.

    Kaynak gerçekten yoksa token bilinmiyordur (idempotent ``204``); izin ve
    I/O arızaları ise altyapı hatasıdır ve gizlenmemelidir.
    """
    token = _preview(client, inventory_id).json()["preview_token"]

    def _deny(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("izin yok: /srv/gizli/dizin")

    monkeypatch.setattr(os, "rename", _deny)

    response = client.post(
        f"/api/inventories/{inventory_id}/ping/preview/cancel",
        json={"preview_token": token},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "ping_preview_unavailable"
    assert "/srv/gizli" not in response.text


def test_cancel_error_never_leaks_filesystem_or_exception_text(
    client: TestClient, inventory_id: int, settings: Settings
) -> None:
    """Hata cevabı path, token veya exception metni taşımaz."""
    token = _preview(client, inventory_id).json()["preview_token"]
    (_preview_dirs(settings)[0] / "beklenmeyen.bin").write_text("veri", encoding="utf-8")

    response = client.post(
        f"/api/inventories/{inventory_id}/ping/preview/cancel",
        json={"preview_token": token},
    )

    body = response.text
    assert token not in body
    assert str(settings.app_data_dir) not in body
    assert "beklenmeyen.bin" not in body
    assert "Traceback" not in body
    assert response.json()["error"]["details"] is None


def test_cancel_with_a_token_from_another_inventory_is_idempotent(
    client: TestClient, inventory_id: int, inventory_root: Path, settings: Settings
) -> None:
    """Başka bir inventory'nin token'ı iptal edemez; token yine tüketilir."""
    other_path = inventory_root / "diger.ini"
    other_path.write_text(INI_INVENTORY, encoding="utf-8")
    other_id = client.post(
        "/api/inventories",
        json={"name": "diger", "path": str(other_path), "source_type": "ini"},
    ).json()["id"]
    token = _preview(client, inventory_id).json()["preview_token"]

    mismatched = client.post(
        f"/api/inventories/{other_id}/ping/preview/cancel",
        json={"preview_token": token},
    )

    assert mismatched.status_code == 204
    # Token tekrar kullanılabilir bırakılmaz: state tüketilmiştir.
    assert _preview_dirs(settings) == []
    replay = client.post(
        f"/api/inventories/{inventory_id}/ping/preview/cancel",
        json={"preview_token": token},
    )
    assert replay.status_code == 204


# --- Token hata cevabında yankılanmaz ------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "GIZLI_TOKEN_" + "x" * 130,
        "GIZLI_TOKEN_bicimsiz/../../etc/passwd",
        "",
    ],
    ids=["too_long", "malformed", "empty"],
)
def test_the_token_is_never_echoed_in_a_validation_error(
    client: TestClient, inventory_id: int, token: str
) -> None:
    """Doğrulama hatası gönderilen token'ı geri yansıtmaz.

    Ölçülen ihlal: Pydantic hatasının `input` alanı token'ın tamamını
    taşıyordu ve standart handler `exc.errors()` çıktısını olduğu gibi
    döndürüyordu.
    """
    response = client.post(
        f"/api/inventories/{inventory_id}/ping/preview/cancel",
        json={"preview_token": token},
    )

    body = response.text
    assert response.status_code in {204, 422}
    assert "GIZLI_TOKEN_" not in body
    if response.status_code == 422:
        payload = response.json()
        assert payload["error"]["code"] == "request_validation_error"
        assert "input" not in json.dumps(payload)
        assert "ctx" not in json.dumps(payload)


def test_an_oversized_limit_is_never_echoed(client: TestClient, inventory_id: int) -> None:
    """Çok uzun limit değeri de cevaba girmez."""
    response = _preview(client, inventory_id, limit="GIZLI_LIMIT_" + "a" * 5000)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"
    assert "GIZLI_LIMIT_" not in response.text


def test_validation_details_keep_only_safe_fields(client: TestClient, inventory_id: int) -> None:
    """`type`, `loc` ve `msg` korunur; girdi taşıyan alanlar atılır."""
    response = _preview(client, inventory_id, limit="a" * 5000)

    details = response.json()["error"]["details"]
    assert isinstance(details, list)
    assert details
    for item in details:
        assert set(item) == {"type", "loc", "msg"}


# --- requested_by bağlaması ----------------------------------------------------


def test_meta_records_the_requesting_actor(
    client: TestClient, inventory_id: int, settings: Settings
) -> None:
    """Preview state, planı isteyen aktöre bağlanır."""
    _preview(client, inventory_id)

    meta = json.loads((_preview_dirs(settings)[0] / META_FILENAME).read_text(encoding="utf-8"))
    assert meta["requested_by"] == settings.local_actor
    assert meta["requested_by"]


def test_meta_carries_no_secret_key_path_or_hostvar(
    client: TestClient, inventory_id: int, settings: Settings
) -> None:
    """Meta yalnızca onay bağlamını taşır."""
    _preview(client, inventory_id)

    raw = (_preview_dirs(settings)[0] / META_FILENAME).read_text(encoding="utf-8")
    meta = json.loads(raw)
    assert set(meta) == {
        "schema_version",
        "created_at",
        "expires_at",
        "inventory_id",
        "requested_by",
        "limit",
        "host_count",
        "host_key_policy",
        "operation",
        "snapshot_sha256",
    }
    assert "ansible_host" not in raw
    assert "10.0.0." not in raw


def test_a_token_cannot_be_used_against_another_actor(
    client: TestClient, inventory_id: int, settings: Settings
) -> None:
    """Aktör değişirse token artık kullanılamaz."""
    token = _preview(client, inventory_id).json()["preview_token"]
    settings.local_actor = "baska-aktor"

    response = client.post(
        f"/api/inventories/{inventory_id}/ping/preview/cancel",
        json={"preview_token": token},
    )

    # İptal idempotent kalır fakat state tüketilmiştir.
    assert response.status_code == 204
    assert _preview_dirs(settings) == []


def test_expired_preview_is_swept_on_the_next_preview(
    client: TestClient, inventory_id: int, settings: Settings
) -> None:
    """Süpürme tembeldir: bir sonraki preview isteğinde toplanır."""
    settings.ping_preview_ttl_seconds = 0.001
    first = _preview(client, inventory_id)
    assert first.status_code == 200
    assert len(_preview_dirs(settings)) == 1

    settings.ping_preview_ttl_seconds = 300.0
    second = _preview(client, inventory_id)

    assert second.status_code == 200
    assert len(_preview_dirs(settings)) == 1
    assert _preview_dirs(settings)[0].name == token_digest(second.json()["preview_token"])
