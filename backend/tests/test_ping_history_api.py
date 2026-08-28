"""Ping geçmişi HTTP yüzeyinin sözleşmesi (R1-V3J1A).

Route incedir: SQL, artifact okuma ve belge doğrulaması
``app.services.inventories.ping_history`` içindedir. Burada ölçülen yalnız HTTP
sınırının kendisidir:

1. Boş geçmiş, tek ölçüm, kısmen ulaşılamayan ölçüm ve en-yeni-önce sıra.
2. ``limit``: varsayılan 10, tavan 25; ``0`` ve ``26`` sanitize edilmiş 422.
3. Görünürlük: başka inventory, başka aktör, terminal olmayan PING ve PLAYBOOK
   satırları cevapta yoktur; olmayan inventory mevcut 404 sözleşmesini korur.
4. Bozuk/eksik artifact ve DB ↔ belge uyuşmazlığı generic ``503
   ping_history_unavailable``; cevap path, belge içeriği, host adı veya mesaj
   taşımaz.
5. Public JSON'ın alan kümesi **tam olarak** sözleşmedeki kümedir.
6. ``Cache-Control: no-store``.
7. OpenAPI yüzeyi tek bir yeni GET operasyonudur; ping confirm sözleşmesi
   değişmemiştir.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import Inventory, InventorySourceType, Job, JobStatus, JobType, Project
from app.services.execution.result_reader import JOBS_DIRNAME, RESULT_FILENAME
from app.services.inventories.ping_confirm import PING_JOB_TYPE, RESULT_SCHEMA_VERSION
from app.services.inventories.ping_history import (
    DEFAULT_PING_HISTORY_LIMIT,
    MAX_PING_HISTORY_LIMIT,
)
from app.services.jobs.artifacts import JobArtifactStore
from tests.support import make_settings

ACTOR = "yerel-operator"
OTHER_ACTOR = "baska-operator"

# Cevaptaki **tam** alan kümeleri.
SAFE_ITEM_FIELDS = {
    "job_id",
    "status",
    "return_code",
    "started_at",
    "finished_at",
    "summary",
}
SAFE_SUMMARY_FIELDS = {"total", "reachable", "unreachable", "failed", "no_result"}
SAFE_PAGE_FIELDS = {"inventory_id", "items"}

# Cevapta hiçbir koşulda görünmemesi gereken alan adları.
FORBIDDEN_FIELDS = (
    "requested_by",
    "actor",
    "artifact_path",
    "artifact_dir",
    "hosts",
    "host",
    "message",
    "path",
    "project_path",
    "inventory_path",
    "stdout",
    "stderr",
    "argv",
    "command",
    "environment",
    "env",
    "preview_token",
    "token",
    "snapshot",
    "limit",
    "digest",
)


@pytest.fixture
def settings(
    tmp_path: Path, project_root: Path, inventory_root: Path, secrets_root: Path
) -> Settings:
    """``conftest.settings``'in aynısı, yalnız sabit ve bilinen bir ``local_actor``'la.

    Route yalnız ``settings.local_actor``'ı kullanır; testlerin ``ACTOR``
    sabitiyle seed ettiği Job'ların görünebilmesi için ikisi aynı olmalıdır.
    """
    return make_settings(
        environment="test",
        app_data_dir=tmp_path / "app-data",
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        cors_origins=["http://localhost:5173"],
        project_root_allowlist=[project_root],
        inventory_root_allowlist=[inventory_root],
        ssh_key_root_allowlist=[secrets_root],
        local_actor=ACTOR,
    )


@pytest.fixture
def records(db_session: Session, tmp_path: Path) -> tuple[Inventory, Inventory]:
    project = Project(name="Web", path=str(tmp_path / "proje"))
    db_session.add(project)
    db_session.commit()
    first = Inventory(
        name="Prod",
        path=str(tmp_path / "proje" / "hosts.ini"),
        source_type=InventorySourceType.INI,
        project_id=project.id,
    )
    second = Inventory(
        name="Staging",
        path=str(tmp_path / "proje" / "staging.ini"),
        source_type=InventorySourceType.INI,
        project_id=project.id,
    )
    db_session.add_all([first, second])
    db_session.commit()
    return first, second


_DEFAULT: Any = object()


def seed(
    session: Session,
    inventory: Inventory,
    *,
    job_id: str | None = None,
    job_type: JobType = JobType.PING,
    status: JobStatus = JobStatus.SUCCESSFUL,
    requested_by: str = ACTOR,
    return_code: int | None = 0,
    started_at: datetime | None = None,
    artifact_path: Any = _DEFAULT,
) -> str:
    identifier = job_id or str(uuid.uuid4())
    start = started_at or datetime.now(UTC)
    finish = start + timedelta(seconds=2)
    resolved_artifact = (
        f"{JOBS_DIRNAME}/{identifier}/{RESULT_FILENAME}"
        if artifact_path is _DEFAULT
        else artifact_path
    )
    fields: dict[str, Any] = {
        "id": identifier,
        "job_type": job_type,
        "status": status,
        "inventory_id": inventory.id,
        "project_id": inventory.project_id,
        "requested_by": requested_by,
        "created_at": start,
        "started_at": start,
        "finished_at": finish if status in (JobStatus.SUCCESSFUL, JobStatus.FAILED) else None,
        "return_code": return_code,
        "artifact_path": resolved_artifact,
    }
    if job_type is JobType.PLAYBOOK:
        fields["playbook_path"] = "site.yml"
    session.add(Job(**fields))
    session.commit()
    return identifier


def document(
    job_id: str,
    inventory: Inventory,
    *,
    session: Session,
    status: str = "successful",
    return_code: int | None = 0,
    hosts: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    row = session.get(Job, job_id)
    assert row is not None
    assert row.started_at is not None and row.finished_at is not None
    entries = (
        hosts if hosts is not None else [{"name": "web-1", "status": "reachable", "message": None}]
    )
    counts = {"reachable": 0, "unreachable": 0, "failed": 0, "no_result": 0}
    for entry in entries:
        counts[str(entry["status"])] += 1
    payload: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "job_id": job_id,
        "job_type": PING_JOB_TYPE,
        "status": status,
        "inventory_id": inventory.id,
        "project_id": inventory.project_id,
        "limit": None,
        "return_code": return_code,
        "started_at": _aware(row.started_at).isoformat(),
        "finished_at": _aware(row.finished_at).isoformat(),
        "summary": {"total": len(entries), **counts},
        "hosts": entries,
    }
    payload.update(overrides)
    return payload


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def publish(settings: Settings, job_id: str, payload: dict[str, Any]) -> None:
    store = JobArtifactStore(settings.app_data_dir)
    store.create(job_id)
    store.write_result(job_id, payload)


def url(inventory: Inventory) -> str:
    return f"/api/inventories/{inventory.id}/ping-runs"


# --- Mutlu yol -----------------------------------------------------------------


def test_empty_history_is_200_with_an_empty_list(
    client: TestClient, records: tuple[Inventory, Inventory]
) -> None:
    inventory, _ = records

    response = client.get(url(inventory))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == SAFE_PAGE_FIELDS
    assert body["inventory_id"] == inventory.id
    assert body["items"] == []


def test_a_single_fully_reachable_measurement_is_returned(
    client: TestClient,
    db_session: Session,
    settings: Settings,
    records: tuple[Inventory, Inventory],
) -> None:
    inventory, _ = records
    hosts = [{"name": f"web-{index}", "status": "reachable", "message": None} for index in range(5)]
    job_id = seed(db_session, inventory, status=JobStatus.SUCCESSFUL, return_code=0)
    publish(settings, job_id, document(job_id, inventory, session=db_session, hosts=hosts))

    body = client.get(url(inventory)).json()

    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["job_id"] == job_id
    assert item["status"] == "successful"
    assert item["return_code"] == 0
    assert item["summary"] == {
        "total": 5,
        "reachable": 5,
        "unreachable": 0,
        "failed": 0,
        "no_result": 0,
    }
    # Zaman damgaları UTC olarak serileşir.
    assert item["started_at"].endswith("+00:00") or item["started_at"].endswith("Z")


def test_a_four_reachable_one_unreachable_measurement_is_returned(
    client: TestClient,
    db_session: Session,
    settings: Settings,
    records: tuple[Inventory, Inventory],
) -> None:
    inventory, _ = records
    hosts: list[dict[str, Any]] = [
        {"name": f"web-{index}", "status": "reachable", "message": None} for index in range(4)
    ]
    hosts.append({"name": "web-4", "status": "unreachable", "message": "connect to *** port ***"})
    job_id = seed(db_session, inventory, status=JobStatus.FAILED, return_code=4)
    publish(
        settings,
        job_id,
        document(
            job_id, inventory, session=db_session, status="failed", return_code=4, hosts=hosts
        ),
    )

    item = client.get(url(inventory)).json()["items"][0]

    assert item["status"] == "failed"
    assert item["return_code"] == 4
    assert item["summary"] == {
        "total": 5,
        "reachable": 4,
        "unreachable": 1,
        "failed": 0,
        "no_result": 0,
    }


def test_measurements_are_returned_newest_first(
    client: TestClient,
    db_session: Session,
    settings: Settings,
    records: tuple[Inventory, Inventory],
) -> None:
    inventory, _ = records
    base = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
    identifiers = []
    for index in range(3):
        job_id = seed(db_session, inventory, started_at=base + timedelta(minutes=index))
        publish(settings, job_id, document(job_id, inventory, session=db_session))
        identifiers.append(job_id)

    body = client.get(url(inventory)).json()

    assert [item["job_id"] for item in body["items"]] == list(reversed(identifiers))


def test_equal_finish_times_produce_a_stable_descending_id_order(
    client: TestClient,
    db_session: Session,
    settings: Settings,
    records: tuple[Inventory, Inventory],
) -> None:
    inventory, _ = records
    moment = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
    identifiers = sorted(str(uuid.uuid4()) for _ in range(3))
    for identifier in identifiers:
        seed(db_session, inventory, job_id=identifier, started_at=moment)
        publish(settings, identifier, document(identifier, inventory, session=db_session))

    first = client.get(url(inventory)).json()["items"]
    second = client.get(url(inventory)).json()["items"]

    assert [item["job_id"] for item in first] == list(reversed(identifiers))
    assert first == second


# --- Limit sözleşmesi -----------------------------------------------------------


def test_the_default_limit_is_ten(
    client: TestClient,
    db_session: Session,
    settings: Settings,
    records: tuple[Inventory, Inventory],
) -> None:
    inventory, _ = records
    base = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
    for index in range(DEFAULT_PING_HISTORY_LIMIT + 2):
        job_id = seed(db_session, inventory, started_at=base + timedelta(minutes=index))
        publish(settings, job_id, document(job_id, inventory, session=db_session))

    body = client.get(url(inventory)).json()

    assert len(body["items"]) == DEFAULT_PING_HISTORY_LIMIT


def test_an_explicit_limit_bounds_the_page(
    client: TestClient,
    db_session: Session,
    settings: Settings,
    records: tuple[Inventory, Inventory],
) -> None:
    inventory, _ = records
    base = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
    for index in range(3):
        job_id = seed(db_session, inventory, started_at=base + timedelta(minutes=index))
        publish(settings, job_id, document(job_id, inventory, session=db_session))

    body = client.get(url(inventory), params={"limit": 2}).json()

    assert len(body["items"]) == 2


@pytest.mark.parametrize("limit", [0, -1, MAX_PING_HISTORY_LIMIT + 1, "abc", 2.5])
def test_an_out_of_range_limit_is_a_sanitized_422(
    client: TestClient, records: tuple[Inventory, Inventory], limit: Any
) -> None:
    inventory, _ = records

    response = client.get(url(inventory), params={"limit": limit})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "request_validation_error"
    for detail in body["error"]["details"]:
        assert set(detail) == {"type", "loc", "msg"}


def test_the_maximum_limit_is_accepted(
    client: TestClient, records: tuple[Inventory, Inventory]
) -> None:
    inventory, _ = records

    response = client.get(url(inventory), params={"limit": MAX_PING_HISTORY_LIMIT})

    assert response.status_code == 200


# --- Görünürlük -----------------------------------------------------------------


def test_another_inventorys_measurement_is_not_listed(
    client: TestClient,
    db_session: Session,
    settings: Settings,
    records: tuple[Inventory, Inventory],
) -> None:
    inventory, other = records
    foreign = seed(db_session, other)
    publish(settings, foreign, document(foreign, other, session=db_session))

    assert client.get(url(inventory)).json()["items"] == []
    assert client.get(url(other)).json()["items"] != []


def test_another_actors_measurement_is_not_listed(
    client: TestClient,
    db_session: Session,
    settings: Settings,
    records: tuple[Inventory, Inventory],
) -> None:
    inventory, _ = records
    foreign = seed(db_session, inventory, requested_by=OTHER_ACTOR)
    publish(settings, foreign, document(foreign, inventory, session=db_session))

    assert client.get(url(inventory)).json()["items"] == []


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.RUNNING, JobStatus.CANCELED])
def test_non_terminal_ping_jobs_are_not_listed(
    client: TestClient,
    db_session: Session,
    records: tuple[Inventory, Inventory],
    status: JobStatus,
) -> None:
    inventory, _ = records
    seed(db_session, inventory, status=status, return_code=None)

    assert client.get(url(inventory)).json()["items"] == []


def test_playbook_jobs_are_not_listed(
    client: TestClient,
    db_session: Session,
    settings: Settings,
    records: tuple[Inventory, Inventory],
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory, job_type=JobType.PLAYBOOK)
    publish(settings, job_id, document(job_id, inventory, session=db_session))

    assert client.get(url(inventory)).json()["items"] == []


def test_a_row_with_an_unexpected_artifact_path_is_not_listed(
    client: TestClient,
    db_session: Session,
    settings: Settings,
    records: tuple[Inventory, Inventory],
) -> None:
    """Sahte yol taşıyan satır elenir; o yol hiçbir koşulda açılmaz."""
    inventory, _ = records
    identifier = str(uuid.uuid4())
    seed(db_session, inventory, job_id=identifier, artifact_path="/etc/passwd")
    publish(settings, identifier, document(identifier, inventory, session=db_session))

    response = client.get(url(inventory))

    assert response.status_code == 200
    assert response.json()["items"] == []


def test_an_unknown_inventory_keeps_the_existing_404_contract(
    client: TestClient, records: tuple[Inventory, Inventory]
) -> None:
    inventory, _ = records

    response = client.get(f"/api/inventories/{inventory.id + 9999}/ping-runs")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --- Hata sözleşmesi -------------------------------------------------------------


def test_a_missing_artifact_is_a_generic_503(
    client: TestClient, db_session: Session, records: tuple[Inventory, Inventory]
) -> None:
    inventory, _ = records
    seed(db_session, inventory)

    response = client.get(url(inventory))

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "ping_history_unavailable",
        "message": "Ping geçmişi şu anda okunamıyor.",
        "details": {"reason": "unavailable"},
    }


def test_malformed_json_is_a_generic_503(
    client: TestClient,
    db_session: Session,
    settings: Settings,
    records: tuple[Inventory, Inventory],
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory)
    store = JobArtifactStore(settings.app_data_dir)
    store.create(job_id)
    (settings.app_data_dir / JOBS_DIRNAME / job_id / RESULT_FILENAME).write_text(
        "{bozuk", encoding="utf-8"
    )

    assert client.get(url(inventory)).status_code == 503


@pytest.mark.parametrize("field", ["job_id", "inventory_id", "status", "return_code"])
def test_a_db_document_mismatch_is_a_generic_503(
    client: TestClient,
    db_session: Session,
    settings: Settings,
    records: tuple[Inventory, Inventory],
    field: str,
) -> None:
    inventory, other = records
    job_id = seed(db_session, inventory, status=JobStatus.SUCCESSFUL, return_code=0)
    payload = document(job_id, inventory, session=db_session)
    payload[field] = {
        "job_id": str(uuid.uuid4()),
        "inventory_id": other.id,
        "status": "failed",
        "return_code": 7,
    }[field]
    publish(settings, job_id, payload)

    response = client.get(url(inventory))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ping_history_unavailable"


def test_the_503_leaks_neither_path_nor_document_content(
    client: TestClient,
    db_session: Session,
    settings: Settings,
    records: tuple[Inventory, Inventory],
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory)
    hosts = [
        {"name": "gizli-host", "status": "reachable", "message": "gizli mesaj"},
        {"name": "gizli-host", "status": "reachable", "message": "gizli mesaj"},
    ]
    publish(settings, job_id, document(job_id, inventory, session=db_session, hosts=hosts))

    response = client.get(url(inventory))

    assert response.status_code == 503
    assert "gizli-host" not in response.text
    assert "gizli mesaj" not in response.text
    assert str(settings.app_data_dir) not in response.text
    assert settings.local_actor not in response.text
    assert job_id not in response.text


# --- Public alan kümesi ve başlıklar ---------------------------------------------


def test_the_response_carries_exactly_the_contract_fields(
    client: TestClient,
    db_session: Session,
    settings: Settings,
    records: tuple[Inventory, Inventory],
) -> None:
    inventory, _ = records
    hosts: list[dict[str, Any]] = [
        {"name": "gizli-host", "status": "failed", "message": "gizli mesaj"},
        {"name": "web-2", "status": "reachable", "message": None},
    ]
    job_id = seed(db_session, inventory, status=JobStatus.FAILED, return_code=2)
    publish(
        settings,
        job_id,
        document(
            job_id, inventory, session=db_session, status="failed", return_code=2, hosts=hosts
        ),
    )

    response = client.get(url(inventory))
    body = response.json()

    assert set(body) == SAFE_PAGE_FIELDS
    item = body["items"][0]
    assert set(item) == SAFE_ITEM_FIELDS
    assert set(item["summary"]) == SAFE_SUMMARY_FIELDS
    for field in FORBIDDEN_FIELDS:
        assert f'"{field}"' not in response.text
    assert "gizli-host" not in response.text
    assert "gizli mesaj" not in response.text
    assert settings.local_actor not in response.text
    assert str(settings.app_data_dir) not in response.text


def test_the_response_is_not_stored(
    client: TestClient, records: tuple[Inventory, Inventory]
) -> None:
    inventory, _ = records

    response = client.get(url(inventory))

    assert response.headers["Cache-Control"] == "no-store"


def test_the_error_response_is_also_not_cacheable_content(
    client: TestClient, db_session: Session, records: tuple[Inventory, Inventory]
) -> None:
    """503 gövdesi sabit ve içeriksizdir; ara katman için saklanacak bir şey yok."""
    inventory, _ = records
    seed(db_session, inventory)

    response = client.get(url(inventory))

    assert response.status_code == 503
    assert response.json()["error"]["details"] == {"reason": "unavailable"}


def test_the_actor_cannot_be_supplied_by_the_request(
    client: TestClient,
    db_session: Session,
    settings: Settings,
    records: tuple[Inventory, Inventory],
) -> None:
    """Aktör yalnız sunucu ayarındandır; query, header veya cookie ile gelmez."""
    inventory, _ = records
    foreign = seed(db_session, inventory, requested_by=OTHER_ACTOR)
    publish(settings, foreign, document(foreign, inventory, session=db_session))

    responses = [
        client.get(url(inventory), params={"requested_by": OTHER_ACTOR}),
        client.get(url(inventory), headers={"X-Actor": OTHER_ACTOR}),
    ]

    for response in responses:
        assert response.status_code == 200
        assert response.json()["items"] == []


# --- OpenAPI ---------------------------------------------------------------------


def test_openapi_exposes_exactly_one_new_ping_runs_operation(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()

    path = "/api/inventories/{inventory_id}/ping-runs"
    assert path in spec["paths"]
    assert set(spec["paths"][path]) == {"get"}


def test_openapi_locks_the_limit_query_parameter(client: TestClient) -> None:
    """``limit``, yayımlanan sözleşmede de sınırlıdır ve zorunlu değildir."""
    spec = client.get("/openapi.json").json()
    parameters = spec["paths"]["/api/inventories/{inventory_id}/ping-runs"]["get"]["parameters"]

    limits = [parameter for parameter in parameters if parameter["name"] == "limit"]
    assert len(limits) == 1
    schema = limits[0]["schema"]
    assert limits[0]["in"] == "query"
    assert limits[0]["required"] is False
    assert schema["maximum"] == MAX_PING_HISTORY_LIMIT
    assert schema["minimum"] == 1
    assert schema["default"] == DEFAULT_PING_HISTORY_LIMIT


def test_openapi_response_schema_forbids_extra_fields(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()

    item = spec["components"]["schemas"]["PingHistoryItemResponse"]
    assert item["additionalProperties"] is False
    assert set(item["properties"]) == SAFE_ITEM_FIELDS

    summary = spec["components"]["schemas"]["PingHistorySummaryResponse"]
    assert summary["additionalProperties"] is False
    assert set(summary["properties"]) == SAFE_SUMMARY_FIELDS

    page = spec["components"]["schemas"]["PingHistoryResponse"]
    assert page["additionalProperties"] is False
    assert set(page["properties"]) == SAFE_PAGE_FIELDS


def test_the_existing_ping_confirm_contract_is_unchanged(client: TestClient) -> None:
    """Yazma akışının yüzeyi bu turda değişmedi."""
    spec = client.get("/openapi.json").json()

    assert set(spec["paths"]["/api/inventories/{inventory_id}/ping"]) == {"post"}
    assert set(spec["paths"]["/api/inventories/{inventory_id}/ping/preview"]) == {"post"}
    assert set(spec["paths"]["/api/inventories/{inventory_id}/ping/preview/cancel"]) == {"post"}


def test_the_read_endpoint_rejects_writes(
    client: TestClient, records: tuple[Inventory, Inventory]
) -> None:
    inventory, _ = records

    for method in (client.post, client.put, client.patch, client.delete):
        assert method(url(inventory)).status_code == 405
