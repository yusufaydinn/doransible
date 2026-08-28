"""`POST /api/projects/{id}/execution-plans` sözleşmesi (R1-V2).

Merkez iddia R1-V1 ile aynıdır ve bir adım ileri gider: **hazırlama da hiçbir
playbook çalıştırmaz.** `ansible-runner`/`ansible-playbook` çağrılmaz, SSH
bağlantısı kurulmaz, Job satırı ve artifact oluşmaz. Başlatılan tek alt süreç,
özgün inventory'yi okuyan parser'dır.

Buna ek olarak dondurma sözleşmesi ölçülür: plan dondurulmuş kopyadan üretilir,
kaynak sonradan değişse veya silinse bile hazırlanmış plan ayakta kalır, raw
token yalnızca bir kez döner ve veritabanına yalnızca özeti yazılır.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.core.config import Settings
from app.main import create_app
from app.services.execution import workspace as ws
from app.services.execution.store import token_digest
from tests.support import stub_parser_command

PLAYBOOK = "---\n- name: Ornek\n  hosts: all\n"
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

pytestmark = pytest.mark.skipif(
    not ws.secure_filesystem_available(),
    reason="Descriptor-relative dosya sistemi primitive'leri bu platformda yok (ADR-017).",
)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _use_stub(settings: Settings, tmp_path: Path, payload: dict[str, Any]) -> None:
    target = tmp_path / "payload.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    settings.ansible_inventory_command = stub_parser_command("payload", payload=str(target))


def _create_project(client: TestClient, path: Path, name: str = "Web") -> int:
    path.mkdir(parents=True, exist_ok=True)
    response = client.post("/api/projects", json={"name": name, "path": str(path)})
    assert response.status_code == 201, response.text
    project_id: int = response.json()["id"]
    return project_id


def _create_linked_inventory(client: TestClient, target: Path, project_id: int) -> int:
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


def _prepare(client: TestClient, project_id: int, **payload: Any) -> httpx.Response:
    """Hazırlama isteği gönderir; ``mode`` verilmezse ``check`` varsayılır (R1-V3H2A)."""
    payload.setdefault("mode", "check")
    return cast(
        httpx.Response,
        client.post(f"/api/projects/{project_id}/execution-plans", json=payload),
    )


def _preview(client: TestClient, project_id: int, **payload: Any) -> httpx.Response:
    payload.setdefault("mode", "check")
    return cast(
        httpx.Response,
        client.post(f"/api/projects/{project_id}/execution-plan", json=payload),
    )


@pytest.fixture
def project_dir(project_root: Path) -> Path:
    directory = project_root / "proje"
    directory.mkdir(parents=True, exist_ok=True)
    _write(directory / "site.yml", PLAYBOOK)
    _write(directory / "playbooks" / "web.yml", PLAYBOOK)
    _write(directory / "inventories" / "production.ini", INVENTORY_TEXT)
    return directory


@pytest.fixture
def prepare_context(
    client: TestClient, project_dir: Path, tmp_path: Path, settings: Settings
) -> tuple[int, int]:
    project_id = _create_project(client, project_dir)
    inventory_id = _create_linked_inventory(
        client, project_dir / "inventories" / "production.ini", project_id
    )
    _use_stub(settings, tmp_path, SIMPLE_OUTPUT)
    return project_id, inventory_id


def _workspaces(settings: Settings) -> list[str]:
    return ws.list_workspace_ids(settings.resolve_execution_plan_dir())


def test_prepare_returns_frozen_plan_and_single_use_token(
    client: TestClient, prepare_context: tuple[int, int], settings: Settings
) -> None:
    """Mutlu yol: dondurulmuş plan, TTL'li token ve manifest digest'i döner."""
    project_id, inventory_id = prepare_context

    response = _prepare(client, project_id, inventory_id=inventory_id, playbook_path="site.yml")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["prepared"] is True
    assert len(body["plan_token"]) == 43
    assert len(body["manifest_digest"]) == 64
    assert body["expires_at"].endswith("Z") or "+00:00" in body["expires_at"]

    plan = body["plan"]
    assert plan["executable"] is False
    assert plan["not_executable_reason"] == "execution_not_enabled"
    assert plan["mode"] == "check"
    assert plan["playbook"]["path"] == "site.yml"
    assert plan["hosts"] == ["db01", "web01", "web02"]
    assert plan["host_count"] == 3
    assert plan["inventory"]["id"] == inventory_id
    # Tam olarak bir workspace yayımlanır.
    assert len(_workspaces(settings)) == 1


def test_response_is_not_cacheable(client: TestClient, prepare_context: tuple[int, int]) -> None:
    """Tek kullanımlık sır taşıyan cevap hiçbir katmanda saklanmamalıdır."""
    project_id, inventory_id = prepare_context

    response = _prepare(client, project_id, inventory_id=inventory_id, playbook_path="site.yml")

    assert response.headers["cache-control"] == "no-store"


def test_database_stores_only_the_token_hash(
    client: TestClient,
    prepare_context: tuple[int, int],
    migrated_engine: Engine,
) -> None:
    """Raw token veritabanına yazılmaz; satırda yalnızca özeti bulunur."""
    project_id, inventory_id = prepare_context

    body = _prepare(client, project_id, inventory_id=inventory_id, playbook_path="site.yml").json()

    with migrated_engine.connect() as connection:
        rows = connection.execute(text("SELECT * FROM execution_plans")).mappings().all()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["token_hash"] == token_digest(body["plan_token"])
    assert body["plan_token"] not in str(row)
    assert row["status"] == "prepared"
    assert row["manifest_digest"] == body["manifest_digest"]


def test_prepared_plan_row_records_check_mode_explicitly(
    client: TestClient,
    prepare_context: tuple[int, int],
    migrated_engine: Engine,
) -> None:
    """Hazırlanan plan satırı kipi **kalıcı olarak** taşır (R1-V3H1B1).

    Ölçülen şey yalnızca cevaptaki ``mode`` alanı değil, veritabanına yazılan
    sütunun kendisidir. Kip planın kalıcı parçası olduğu için claim koşulu ve
    ondan doğacak Job onu buradan okur — satır kipi taşımasaydı zincirin
    başlangıcı boş kalırdı.
    """
    project_id, inventory_id = prepare_context

    body = _prepare(client, project_id, inventory_id=inventory_id, playbook_path="site.yml").json()

    assert body["plan"]["mode"] == "check"
    with migrated_engine.connect() as connection:
        stored = connection.execute(text("SELECT mode FROM execution_plans")).scalar_one()
    assert stored == "check"


def test_normal_mode_prepares_a_normal_plan_row(
    client: TestClient,
    prepare_context: tuple[int, int],
    migrated_engine: Engine,
) -> None:
    """R1-V3H2A: ``mode=normal`` isteği de kabul edilir ve satıra öyle yazılır.

    Public yüzey artık check-only değildir — seçilen kip cevaba, fingerprint'e
    ve kalıcı plan satırına aynen taşınır.
    """
    project_id, inventory_id = prepare_context

    body = _prepare(
        client, project_id, mode="normal", inventory_id=inventory_id, playbook_path="site.yml"
    ).json()

    assert body["plan"]["mode"] == "normal"
    with migrated_engine.connect() as connection:
        stored = connection.execute(text("SELECT mode FROM execution_plans")).scalar_one()
    assert stored == "normal"


def test_mode_is_a_required_field(client: TestClient, prepare_context: tuple[int, int]) -> None:
    """``mode`` verilmezse istek domain katmanına ulaşmadan 422 alır."""
    project_id, inventory_id = prepare_context

    response = client.post(
        f"/api/projects/{project_id}/execution-plans",
        json={"inventory_id": inventory_id, "playbook_path": "site.yml"},
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "request_validation_error"


@pytest.mark.parametrize(
    "mode",
    [None, "", "   ", "Check", "CHECK", "Normal", "check ", "dry-run", "diff", 123, True],
)
def test_invalid_mode_values_are_rejected(
    client: TestClient, prepare_context: tuple[int, int], mode: object
) -> None:
    """Bilinmeyen, boş, whitespace'li veya farklı case bir ``mode`` 422 alır."""
    project_id, inventory_id = prepare_context

    response = client.post(
        f"/api/projects/{project_id}/execution-plans",
        json={"mode": mode, "inventory_id": inventory_id, "playbook_path": "site.yml"},
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "request_validation_error"


def test_actor_is_bound_from_settings_and_stays_off_the_response(
    client: TestClient,
    prepare_context: tuple[int, int],
    settings: Settings,
    migrated_engine: Engine,
) -> None:
    """Aktör plana **sunucu ayarından** bağlanır ve cevaba çıkmaz (R1-V3A).

    İstemci aktörü seçebilseydi "aktör bağı" yalnızca bir alan kopyalaması
    olurdu; cevapta görünseydi de sunucu tarafı bir yapılandırma etiketi API
    yüzeyine sızardı.
    """
    project_id, inventory_id = prepare_context

    response = _prepare(client, project_id, inventory_id=inventory_id, playbook_path="site.yml")

    assert response.status_code == 201, response.text
    assert "requested_by" not in response.text
    assert settings.local_actor not in response.text
    with migrated_engine.connect() as connection:
        stored = connection.execute(text("SELECT requested_by FROM execution_plans")).scalar_one()
    assert stored == settings.local_actor


def test_request_body_cannot_choose_the_actor(
    client: TestClient, prepare_context: tuple[int, int]
) -> None:
    """``requested_by`` bir istek alanı değildir; gönderilirse istek reddedilir."""
    project_id, inventory_id = prepare_context

    response = _prepare(
        client,
        project_id,
        inventory_id=inventory_id,
        playbook_path="site.yml",
        requested_by="baska-aktor",
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"


def test_each_preparation_returns_a_different_token(
    client: TestClient, prepare_context: tuple[int, int]
) -> None:
    """Token yeniden üretilemez: her hazırlama yeni bir sır verir."""
    project_id, inventory_id = prepare_context

    first = _prepare(client, project_id, inventory_id=inventory_id, playbook_path="site.yml")
    second = _prepare(client, project_id, inventory_id=inventory_id, playbook_path="site.yml")

    assert first.json()["plan_token"] != second.json()["plan_token"]


def test_prepare_starts_no_playbook_or_runner_process(
    client: TestClient,
    prepare_context: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Başlatılan tek alt süreç parser'dır; execution süreci yoktur."""
    project_id, inventory_id = prepare_context
    launched: list[list[str]] = []
    original = subprocess.Popen

    def _capture(*args: Any, **kwargs: Any) -> Any:
        command: Any = args[0] if args else kwargs.get("args")
        launched.append([str(part) for part in (command or [])])
        return original(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _capture)

    assert (
        _prepare(
            client, project_id, inventory_id=inventory_id, playbook_path="site.yml"
        ).status_code
        == 201
    )

    assert launched, "parser süreci başlatılmalıydı"
    for command in launched:
        joined = " ".join(command)
        assert "ansible-playbook" not in joined
        assert "ansible-runner" not in joined
        assert "ansible_runner" not in joined
        assert "sshpass" not in joined
        assert not any(part == "ssh" or part.endswith("/ssh") for part in command)


def test_prepare_writes_no_job_row_or_artifact(
    client: TestClient,
    prepare_context: tuple[int, int],
    migrated_engine: Engine,
    settings: Settings,
) -> None:
    """Job, artifact ve ping preview state'i oluşmaz."""
    project_id, inventory_id = prepare_context

    assert (
        _prepare(
            client, project_id, inventory_id=inventory_id, playbook_path="site.yml"
        ).status_code
        == 201
    )

    with migrated_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM jobs")).scalar_one() == 0
    assert list((settings.app_data_dir / "jobs").iterdir()) == []
    assert list(settings.resolve_ping_preview_dir().iterdir()) == []


def test_frozen_plan_survives_source_mutation(
    client: TestClient, prepare_context: tuple[int, int], project_dir: Path, settings: Settings
) -> None:
    """Hazırlamadan sonra kaynak değişse de dondurulmuş içerik değişmez."""
    project_id, inventory_id = prepare_context
    body = _prepare(client, project_id, inventory_id=inventory_id, playbook_path="site.yml").json()
    workspace_id = _workspaces(settings)[0]
    root = settings.resolve_execution_plan_dir()

    (project_dir / "site.yml").write_text("---\n- hosts: butun\n", encoding="utf-8")
    (project_dir / "yeni.yml").write_text(PLAYBOOK, encoding="utf-8")

    manifest = ws.read_manifest(root, workspace_id)
    assert manifest["digest"] == body["manifest_digest"]
    frozen_project = ws.workspace_project_root(root, workspace_id)
    assert (frozen_project / "site.yml").read_text(encoding="utf-8") == PLAYBOOK
    assert not (frozen_project / "yeni.yml").exists()


def test_frozen_plan_survives_source_deletion(
    client: TestClient, prepare_context: tuple[int, int], project_dir: Path, settings: Settings
) -> None:
    """Kaynak silinse bile hazırlanmış planın içeriği okunabilir."""
    project_id, inventory_id = prepare_context
    _prepare(client, project_id, inventory_id=inventory_id, playbook_path="site.yml")
    workspace_id = _workspaces(settings)[0]
    root = settings.resolve_execution_plan_dir()

    for path in sorted(project_dir.rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    project_dir.rmdir()

    assert (ws.workspace_project_root(root, workspace_id) / "site.yml").exists()
    assert "web01" in ws.read_frozen_inventory(root, workspace_id)


def test_raw_inventory_file_is_not_copied(
    client: TestClient, prepare_context: tuple[int, int], settings: Settings
) -> None:
    """Dondurulan inventory, ham dosya değil normalize snapshot'tır."""
    project_id, inventory_id = prepare_context
    _prepare(client, project_id, inventory_id=inventory_id, playbook_path="site.yml")
    root = settings.resolve_execution_plan_dir()
    workspace_id = _workspaces(settings)[0]

    snapshot = ws.read_frozen_inventory(root, workspace_id)
    document = json.loads(snapshot)
    assert set(document["all"]["hosts"]) == {"db01", "web01", "web02"}
    # Ham INI metni dondurulmuş kopyada bulunmaz.
    assert INVENTORY_TEXT not in snapshot


def test_response_leaks_no_paths_or_hostvars(
    client: TestClient, prepare_context: tuple[int, int], settings: Settings, project_dir: Path
) -> None:
    """Cevapta absolute path, workspace yolu, hostvar veya manifest içeriği yok."""
    project_id, inventory_id = prepare_context

    raw = _prepare(client, project_id, inventory_id=inventory_id, playbook_path="site.yml").text

    assert str(project_dir) not in raw
    assert str(settings.resolve_execution_plan_dir()) not in raw
    assert str(settings.app_data_dir) not in raw
    assert "10.0.0.10" not in raw
    assert "ansible_host" not in raw
    assert "manifest.json" not in raw
    assert "hosts.yml" not in raw


def test_symlinked_project_tree_is_refused_without_residue(
    client: TestClient,
    prepare_context: tuple[int, int],
    project_dir: Path,
    settings: Settings,
    migrated_engine: Engine,
    tmp_path: Path,
) -> None:
    """Dondurulamayan ağaç fail-closed reddedilir ve kalıntı bırakmaz."""
    project_id, inventory_id = prepare_context
    outside = tmp_path / "disarisi"
    outside.mkdir()
    os.symlink(outside, project_dir / "kisayol", target_is_directory=True)

    response = _prepare(client, project_id, inventory_id=inventory_id, playbook_path="site.yml")

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "execution_workspace_unsafe"
    assert str(outside) not in response.text
    assert list(settings.resolve_execution_plan_dir().iterdir()) == []
    with migrated_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM execution_plans")).scalar_one() == 0


def test_undiscovered_playbook_is_refused(
    client: TestClient, prepare_context: tuple[int, int], settings: Settings
) -> None:
    """Keşifte olmayan playbook için workspace bile oluşturulmaz."""
    project_id, inventory_id = prepare_context

    response = _prepare(
        client, project_id, inventory_id=inventory_id, playbook_path="../../etc/hosts"
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "playbook_not_discovered"
    assert list(settings.resolve_execution_plan_dir().iterdir()) == []


def test_inventory_must_belong_to_the_project(
    client: TestClient,
    project_dir: Path,
    project_root: Path,
    inventory_root: Path,
    tmp_path: Path,
    settings: Settings,
) -> None:
    """Standalone inventory ile plan hazırlanamaz ve bilgi sızmaz."""
    project_id = _create_project(client, project_dir)
    standalone = _write(inventory_root / "bagimsiz.ini", INVENTORY_TEXT)
    response = client.post(
        "/api/inventories",
        json={"name": "Bagimsiz", "path": str(standalone), "source_type": "ini"},
    )
    assert response.status_code == 201
    inventory_id = response.json()["id"]
    _use_stub(settings, tmp_path, SIMPLE_OUTPUT)

    prepared = _prepare(client, project_id, inventory_id=inventory_id, playbook_path="site.yml")

    assert prepared.status_code == 409
    assert prepared.json()["error"]["code"] == "inventory_not_linked_to_project"
    assert str(standalone) not in prepared.text
    assert list(settings.resolve_execution_plan_dir().iterdir()) == []


def test_inactive_project_cannot_prepare(
    client: TestClient, prepare_context: tuple[int, int], settings: Settings
) -> None:
    """Pasif project için plan hazırlanmaz."""
    project_id, inventory_id = prepare_context
    assert client.delete(f"/api/projects/{project_id}").status_code == 200

    response = _prepare(client, project_id, inventory_id=inventory_id, playbook_path="site.yml")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "project_inactive"
    assert list(settings.resolve_execution_plan_dir().iterdir()) == []


@pytest.mark.parametrize(
    "extra",
    [
        # `mode` R1-V3H2A ile gerçek bir alan oldu; kapsam dışı kalanlar
        # (limit/tags/extra_vars/forks/plan_token) hâlâ reddedilir.
        {"limit": "web01"},
        {"tags": "deploy"},
        {"extra_vars": {"a": 1}},
        {"forks": 50},
        {"plan_token": "x" * 43},
    ],
)
def test_extra_fields_are_rejected(
    client: TestClient, prepare_context: tuple[int, int], extra: dict[str, Any]
) -> None:
    """İkinci bir kanaldan parametre geçirilemez."""
    project_id, inventory_id = prepare_context

    response = _prepare(
        client, project_id, inventory_id=inventory_id, playbook_path="site.yml", **extra
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"


def test_preview_endpoint_still_writes_no_state(
    client: TestClient,
    prepare_context: tuple[int, int],
    settings: Settings,
    migrated_engine: Engine,
) -> None:
    """R1-V1 önizlemesi state yazmaz: ne workspace ne de plan kaydı."""
    project_id, inventory_id = prepare_context

    response = _preview(client, project_id, inventory_id=inventory_id, playbook_path="site.yml")

    assert response.status_code == 200
    assert "plan_token" not in response.text
    assert response.json()["executable"] is False
    assert list(settings.resolve_execution_plan_dir().iterdir()) == []
    with migrated_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM execution_plans")).scalar_one() == 0


def test_only_the_launch_route_consumes_a_plan_token(client: TestClient) -> None:
    """Kapsam kilidi: token'ı tüketen **tek** bir public endpoint vardır (R1-V3D1).

    R1-V3A'da bu test "hiçbir yol token tüketmez" diyordu; R1-V3D1 o yolu
    bilerek açtı. Ölçülen iddia bu yüzden yok olmaz, **daralır**: token'ı kabul
    eden yol tam olarak birdir ve hazırlama/önizleme sözleşmeleri bozulmamıştır.
    İkinci bir token kapısı — ya da hazırlama gövdesine sızmış bir ``plan_token``
    alanı — testi düşürür.
    """
    spec = client.get("/openapi.json").json()

    execution_paths = {path for path in spec["paths"] if "execution" in path}
    assert execution_paths == {
        "/api/projects/{project_id}/execution-plan",
        "/api/projects/{project_id}/execution-plans",
        "/api/projects/{project_id}/executions",
    }

    # Token kabul eden tek istek şeması launch'ınkidir; hazırlama ve önizleme
    # gövdeleri onu ne ister ne kabul eder.
    token_paths = {
        (path, method)
        for path, operations in spec["paths"].items()
        for method, operation in operations.items()
        if "plan_token" in _operation_request_fields(spec, operation)
    }
    assert token_paths == {("/api/projects/{project_id}/executions", "post")}

    for path in (
        "/api/projects/{project_id}/execution-plan",
        "/api/projects/{project_id}/execution-plans",
    ):
        fields = _operation_request_fields(spec, spec["paths"][path]["post"])
        assert fields == {"mode", "inventory_id", "playbook_path"}

    # Aktör hiçbir istek gövdesinden gelmez; sunucu ayarındadır.
    assert "requested_by" not in _request_body_fields(spec)


def _operation_request_fields(spec: dict[str, Any], operation: Any) -> set[str]:
    """Tek bir operasyonun **istek gövdesi** alan adları.

    Cevap şemaları bilinçli olarak dışarıda bırakılır: ``plan_token`` hazırlama
    cevabında bir kez döner; ölçülen, onu geri **kabul eden** yolların hangileri
    olduğu.
    """
    if not isinstance(operation, dict):
        return set()
    schemas = spec.get("components", {}).get("schemas", {})
    fields: set[str] = set()
    for media in operation.get("requestBody", {}).get("content", {}).values():
        name = media.get("schema", {}).get("$ref", "").rsplit("/", 1)[-1]
        fields.update(schemas.get(name, {}).get("properties", {}))
    return fields


def _request_body_fields(spec: dict[str, Any]) -> set[str]:
    """Bütün endpoint'lerin istek gövdesi alan adlarının birleşimi."""
    fields: set[str] = set()
    for operations in spec["paths"].values():
        for operation in operations.values():
            fields.update(_operation_request_fields(spec, operation))
    return fields


def test_startup_reconciliation_collects_orphans(
    client: TestClient,
    prepare_context: tuple[int, int],
    settings: Settings,
    migrated_engine: Engine,
) -> None:
    """Açılış turu, kaydı olmayan yaşlı workspace'i toplar; geçerli planı korur."""
    project_id, inventory_id = prepare_context
    body = _prepare(client, project_id, inventory_id=inventory_id, playbook_path="site.yml").json()
    assert body["prepared"] is True
    root = settings.resolve_execution_plan_dir()
    live_workspace = _workspaces(settings)[0]

    orphan = ws.freeze_workspace(
        root,
        project_root=settings.app_data_dir / "inventories",
        inventory_snapshot='{"all": {"hosts": {"web01": {}}}}\n',
    )
    old = 1_600_000_000
    os.utime(root / orphan.workspace_id, (old, old))

    with TestClient(create_app(settings)):
        pass

    assert ws.workspace_exists(root, orphan.workspace_id) is False
    assert ws.workspace_exists(root, live_workspace) is True
    with migrated_engine.connect() as connection:
        status = connection.execute(text("SELECT status FROM execution_plans")).scalar_one()
    assert status == "prepared"
