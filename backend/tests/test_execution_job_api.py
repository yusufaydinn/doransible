"""Job listesi, detayı ve sonucu HTTP yüzeyinin sözleşmesi (R1-V3D2B).

Route'lar ince: sıralama, sayfalama, yetkilendirme, artifact okuma ve
ayrıştırma ``app.services.execution`` içindedir (D2A1/D2A2A/D2A2B1/D2A2B2).
Burada ölçülen yalnız HTTP sınırının kendisidir:

1. Liste: boş/dolu sayfa, en-yeni-önce sıra, ``project_id``/``status``
   filtresi, bounded ``limit`` ve keyset cursor ile ikinci sayfa.
2. Detay: mutlu yol; başka aktör ve olmayan Job aynı 404.
3. Sonuç: successful/failed mutlu yol; terminal-olmayan, kayıtsız ve bozuk
   sonuç aynı generic 503.
4. Girdi doğrulaması: biçimsiz UUID/limit/status/project_id/cursor sanitize
   edilmiş 422 ile domain'den önce düşer.
5. Aktör yalnız sunucu ayarındandır; istekten hiçbir kanaldan alınmaz.
6. Public JSON'da yasak alan yoktur ve OpenAPI yüzeyi tam olarak üç yeni GET
   operasyonudur; launch sözleşmesi değişmemiştir.
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
from app.models import (
    ExecutionMode,
    ExecutionPlanRecord,
    ExecutionPlanStatus,
    Inventory,
    InventorySourceType,
    Job,
    JobStatus,
    JobType,
    Project,
)
from app.services.execution.normalize import (
    ERROR_PLAYBOOK_FAILED,
    ERROR_RUNNER_FAILED,
    LEGACY_SCHEMA_VERSION,
    OUTCOME_FAILED,
    OUTCOME_SUCCESSFUL,
    SCHEMA_VERSION,
)
from app.services.execution.result_reader import JOBS_DIRNAME, RESULT_FILENAME
from app.services.jobs.artifacts import JobArtifactStore
from tests.support import make_settings

ACTOR = "yerel-operator"
OTHER_ACTOR = "baska-operator"
PLAYBOOK_PATH = "site.yml"


@pytest.fixture
def settings(
    tmp_path: Path, project_root: Path, inventory_root: Path, secrets_root: Path
) -> Settings:
    """``conftest.settings``'in aynısı, yalnız sabit ve bilinen bir ``local_actor``'la.

    Route yalnız ``settings.local_actor``'ı kullanır; testlerin ``ACTOR``
    sabitiyle seed ettiği Job'ların gerçekten yetkilendirilebilmesi için
    ikisinin aynı değeri taşıması gerekir.
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


# Özetin **tam** alan kümesi (bkz. test_execution_job_read.py).
SAFE_SUMMARY_FIELDS = {
    "job_id",
    "job_type",
    "status",
    "mode",
    "project_id",
    "project_name",
    "inventory_id",
    "inventory_name",
    "playbook_path",
    "return_code",
    "error_code",
    "result_truncated",
    "has_recorded_result",
    "created_at",
    "started_at",
    "finished_at",
}

SAFE_RESULT_FIELDS = {
    "schema_version",
    "job_id",
    "return_code",
    "outcome",
    "error_code",
    "recap",
    "events",
    "events_truncated",
    "result_truncated",
    "ansible_output",
    "ansible_output_truncated",
}

# Sonuç cevabının taşıdığı ham display metni. Değer bilinçle "hassas" görünür:
# testler onun cevapta **bulunduğunu** ölçer. Bu bir sızıntı değil, R1-V3J3A'nın
# açık sözleşmesidir (trusted-operator / CLI-equivalent model).
DISPLAY_OUTPUT = "ok: [web-1] => ansible_become_password=SENTINEL-DISPLAY-PW"

FORBIDDEN_FIELDS = (
    "requested_by",
    "actor",
    "execution_plan_id",
    "plan_id",
    "workspace_id",
    "manifest_digest",
    "artifact_path",
    "worker_id",
    "heartbeat_at",
    "lease_expires_at",
    "token",
    "plan_token",
    "environment",
    "argv",
    "command",
    "private_key",
    "stdout",
    "stderr",
    # Join yalnız isimleri dışarı çıkarır (R1-V3J0B2); Project/Inventory'nin
    # path ve description'ı görünmez.
    "project_path",
    "inventory_path",
    "project_description",
)


# --- Kurulum yardımcıları -----------------------------------------------------


@pytest.fixture
def records(db_session: Session, tmp_path: Path) -> tuple[Project, Inventory]:
    project = Project(name="Web", path=str(tmp_path / "proje"))
    db_session.add(project)
    db_session.commit()
    inventory = Inventory(
        name="Prod",
        path=str(tmp_path / "proje" / "hosts.ini"),
        source_type=InventorySourceType.INI,
        project_id=project.id,
    )
    db_session.add(inventory)
    db_session.commit()
    return project, inventory


def _hex64() -> str:
    return uuid.uuid4().hex * 2


def _plan(
    session: Session,
    *,
    project_id: int,
    inventory_id: int,
    requested_by: str,
    playbook_path: str,
    moment: datetime,
    mode: ExecutionMode = ExecutionMode.CHECK,
) -> str:
    plan_id = str(uuid.uuid4())
    session.add(
        ExecutionPlanRecord(
            id=plan_id,
            token_hash=_hex64(),
            project_id=project_id,
            inventory_id=inventory_id,
            playbook_path=playbook_path,
            requested_by=requested_by,
            input_fingerprint=_hex64(),
            workspace_id=str(uuid.uuid4()),
            manifest_digest=_hex64(),
            status=ExecutionPlanStatus.CLAIMED,
            mode=mode,
            created_at=moment,
            expires_at=moment + timedelta(hours=1),
            claimed_at=moment,
        )
    )
    session.flush()
    return plan_id


_DEFAULT: Any = object()


def _seed(
    session: Session,
    project: Project,
    inventory: Inventory,
    *,
    job_id: str | None = None,
    status: JobStatus = JobStatus.SUCCESSFUL,
    requested_by: str = ACTOR,
    created_at: datetime | None = None,
    playbook_path: str = PLAYBOOK_PATH,
    return_code: int | None = None,
    error_code: str | None = None,
    result_truncated: bool = False,
    artifact_path: Any = _DEFAULT,
    mode: ExecutionMode = ExecutionMode.CHECK,
) -> str:
    """Tek bir yetkilendirilmiş PLAYBOOK Job satırı (ve onu doğuran plan)."""
    moment = created_at or datetime.now(UTC)
    identifier = job_id or str(uuid.uuid4())
    plan_id = _plan(
        session,
        project_id=project.id,
        inventory_id=inventory.id,
        requested_by=requested_by,
        playbook_path=playbook_path,
        moment=moment,
        mode=mode,
    )
    resolved_artifact = (
        f"{JOBS_DIRNAME}/{identifier}/{RESULT_FILENAME}"
        if artifact_path is _DEFAULT
        else artifact_path
    )
    fields: dict[str, Any] = {
        "id": identifier,
        "job_type": JobType.PLAYBOOK,
        "status": status,
        "mode": mode,
        "execution_plan_id": plan_id,
        "project_id": project.id,
        "inventory_id": inventory.id,
        "playbook_path": playbook_path,
        "limit_pattern": None,
        "requested_by": requested_by,
        "created_at": moment,
        "return_code": return_code,
        "error_code": error_code,
        "result_truncated": result_truncated,
        "artifact_path": resolved_artifact,
    }
    if status is JobStatus.RUNNING:
        fields["started_at"] = moment
        fields["worker_id"] = str(uuid.uuid4())
        fields["heartbeat_at"] = moment
        fields["lease_expires_at"] = moment + timedelta(seconds=30)
    session.add(Job(**fields))
    session.commit()
    return identifier


def _successful_document(job_id: str, **overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "return_code": 0,
        "outcome": OUTCOME_SUCCESSFUL,
        "error_code": None,
        "recap": {
            "web-1": {
                "ok": 1,
                "changed": 0,
                "failures": 0,
                "unreachable": 0,
                "skipped": 0,
                "rescued": 0,
                "ignored": 0,
            }
        },
        "events": [
            {
                "event": "runner_on_ok",
                "host": "web-1",
                "task": "Ping",
                "changed": False,
                "failed": False,
            }
        ],
        "events_truncated": False,
        "result_truncated": False,
        "ansible_output": DISPLAY_OUTPUT,
        "ansible_output_truncated": False,
    }
    document.update(overrides)
    return document


def _legacy_document(job_id: str, **overrides: Any) -> dict[str, Any]:
    """``schema_version=1`` artifact'i: output alanlarını **hiç** taşımaz."""
    document = _successful_document(job_id)
    document["schema_version"] = LEGACY_SCHEMA_VERSION
    del document["ansible_output"]
    del document["ansible_output_truncated"]
    document.update(overrides)
    return document


def _failed_document(job_id: str, **overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "return_code": 2,
        "outcome": OUTCOME_FAILED,
        "error_code": ERROR_RUNNER_FAILED,
        "recap": {
            "web-1": {
                "ok": 0,
                "changed": 0,
                "failures": 1,
                "unreachable": 0,
                "skipped": 0,
                "rescued": 0,
                "ignored": 0,
            }
        },
        "events": [
            {
                "event": "runner_on_failed",
                "host": "web-1",
                "task": "Ping",
                "changed": False,
                "failed": True,
            }
        ],
        "events_truncated": False,
        "result_truncated": False,
        "ansible_output": DISPLAY_OUTPUT,
        "ansible_output_truncated": False,
    }
    document.update(overrides)
    return document


def _publish(app_data: Path, job_id: str, document: dict[str, Any]) -> None:
    store = JobArtifactStore(app_data)
    store.create(job_id)
    store.write_result(job_id, document)


@pytest.fixture
def app_data(settings: Settings) -> Path:
    return settings.app_data_dir


# --- Liste ----------------------------------------------------------------------


def test_an_empty_actor_sees_an_empty_page(client: TestClient) -> None:
    response = client.get("/api/jobs")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"items": [], "has_more": False, "next_cursor": None}
    assert response.headers["Cache-Control"] == "no-store"


def test_the_list_returns_newest_first(
    client: TestClient, db_session: Session, records: tuple[Project, Inventory]
) -> None:
    project, inventory = records
    base = datetime.now(UTC) - timedelta(minutes=10)
    first = _seed(db_session, project, inventory, created_at=base)
    second = _seed(db_session, project, inventory, created_at=base + timedelta(minutes=1))
    third = _seed(db_session, project, inventory, created_at=base + timedelta(minutes=2))

    response = client.get("/api/jobs")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["job_id"] for item in body["items"]] == [third, second, first]
    assert body["has_more"] is False
    assert body["next_cursor"] is None
    for item in body["items"]:
        assert set(item) == SAFE_SUMMARY_FIELDS
        # Kayıt adları join'den okunur, ID'den tahmin edilmez (R1-V3J0B2).
        assert item["project_name"] == project.name
        assert item["inventory_name"] == inventory.name


def test_project_id_and_status_filters_narrow_the_page(
    client: TestClient, db_session: Session, records: tuple[Project, Inventory], tmp_path: Path
) -> None:
    project, inventory = records
    other_project = Project(name="Diger", path=str(tmp_path / "diger"))
    db_session.add(other_project)
    db_session.commit()
    other_inventory = Inventory(
        name="DigerInv",
        path=str(tmp_path / "diger" / "hosts.ini"),
        source_type=InventorySourceType.INI,
        project_id=other_project.id,
    )
    db_session.add(other_inventory)
    db_session.commit()

    matching = _seed(
        db_session, project, inventory, status=JobStatus.FAILED, error_code="runner_failed"
    )
    _seed(db_session, project, inventory, status=JobStatus.SUCCESSFUL)
    _seed(
        db_session,
        other_project,
        other_inventory,
        status=JobStatus.FAILED,
        error_code="runner_failed",
    )

    response = client.get("/api/jobs", params={"project_id": project.id, "status": "failed"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["job_id"] for item in body["items"]] == [matching]
    assert body["items"][0]["project_name"] == project.name
    assert body["items"][0]["inventory_name"] == inventory.name


def test_the_mode_query_parameter_narrows_the_page(
    client: TestClient, db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """``mode=normal`` yalnız normal kipteki işleri döner (R1-V3J0B2)."""
    project, inventory = records
    checked = _seed(db_session, project, inventory)
    normal = _seed(
        db_session,
        project,
        inventory,
        created_at=datetime.now(UTC) - timedelta(minutes=1),
        mode=ExecutionMode.NORMAL,
    )

    response = client.get("/api/jobs", params={"mode": "normal"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["job_id"] for item in body["items"]] == [normal]
    assert [item["job_id"] for item in body["items"]] != [checked]
    assert body["items"][0]["mode"] == "normal"


def test_status_mode_and_project_query_parameters_narrow_the_page_together(
    client: TestClient, db_session: Session, records: tuple[Project, Inventory], tmp_path: Path
) -> None:
    """Üç query parametresi birlikte verildiğinde kesişim döner."""
    project, inventory = records
    other_project = Project(name="Diger", path=str(tmp_path / "diger"))
    db_session.add(other_project)
    db_session.commit()
    other_inventory = Inventory(
        name="DigerInv",
        path=str(tmp_path / "diger" / "hosts.ini"),
        source_type=InventorySourceType.INI,
        project_id=other_project.id,
    )
    db_session.add(other_inventory)
    db_session.commit()

    wanted = _seed(
        db_session,
        project,
        inventory,
        status=JobStatus.FAILED,
        error_code="runner_timeout",
        mode=ExecutionMode.NORMAL,
    )
    # Doğru project ve durum, yanlış kip.
    _seed(
        db_session,
        project,
        inventory,
        created_at=datetime.now(UTC) - timedelta(minutes=1),
        status=JobStatus.FAILED,
        error_code="runner_timeout",
    )
    # Doğru durum ve kip, yanlış project.
    _seed(
        db_session,
        other_project,
        other_inventory,
        created_at=datetime.now(UTC) - timedelta(minutes=2),
        status=JobStatus.FAILED,
        error_code="runner_timeout",
        mode=ExecutionMode.NORMAL,
    )

    response = client.get(
        "/api/jobs",
        params={"project_id": project.id, "status": "failed", "mode": "normal"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["job_id"] for item in body["items"]] == [wanted]


def test_bounded_limit_and_cursor_reach_a_second_page(
    client: TestClient, db_session: Session, records: tuple[Project, Inventory]
) -> None:
    project, inventory = records
    base = datetime.now(UTC) - timedelta(minutes=10)
    ids = [
        _seed(db_session, project, inventory, created_at=base + timedelta(minutes=index))
        for index in range(3)
    ]

    first_page = client.get("/api/jobs", params={"limit": 2})
    assert first_page.status_code == 200, first_page.text
    first_body = first_page.json()
    assert [item["job_id"] for item in first_body["items"]] == [ids[2], ids[1]]
    assert first_body["has_more"] is True
    cursor = first_body["next_cursor"]
    assert cursor is not None

    second_page = client.get(
        "/api/jobs",
        params={
            "limit": 2,
            "before_created_at": cursor["created_at"],
            "before_job_id": cursor["job_id"],
        },
    )
    assert second_page.status_code == 200, second_page.text
    second_body = second_page.json()
    assert [item["job_id"] for item in second_body["items"]] == [ids[0]]
    assert second_body["has_more"] is False
    assert second_body["next_cursor"] is None


# --- Detay ------------------------------------------------------------------------


def test_a_single_job_detail_happy_path(
    client: TestClient, db_session: Session, records: tuple[Project, Inventory]
) -> None:
    project, inventory = records
    job_id = _seed(db_session, project, inventory, status=JobStatus.SUCCESSFUL, return_code=0)

    response = client.get(f"/api/jobs/{job_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == SAFE_SUMMARY_FIELDS
    assert body["job_id"] == job_id
    assert body["status"] == "successful"
    assert body["project_id"] == project.id
    assert response.headers["Cache-Control"] == "no-store"


def test_another_actor_and_a_missing_job_get_the_same_404(
    client: TestClient, db_session: Session, records: tuple[Project, Inventory]
) -> None:
    project, inventory = records
    other_actors_job = _seed(db_session, project, inventory, requested_by=OTHER_ACTOR)
    missing = str(uuid.uuid4())

    for job_id in (other_actors_job, missing):
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 404, response.text
        error = response.json()["error"]
        assert error["code"] == "job_not_found"
        assert error["details"] == {"reason": "not_found"}


# --- Sonuç ------------------------------------------------------------------------


def test_a_successful_result_response(
    client: TestClient,
    db_session: Session,
    records: tuple[Project, Inventory],
    app_data: Path,
) -> None:
    project, inventory = records
    job_id = _seed(db_session, project, inventory, status=JobStatus.SUCCESSFUL, return_code=0)
    _publish(app_data, job_id, _successful_document(job_id))

    response = client.get(f"/api/jobs/{job_id}/result")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == SAFE_RESULT_FIELDS
    assert body["job_id"] == job_id
    assert body["outcome"] == "successful"
    assert body["return_code"] == 0
    assert body["error_code"] is None
    assert body["recap"]["web-1"]["ok"] == 1
    assert response.headers["Cache-Control"] == "no-store"


def test_a_failed_result_response(
    client: TestClient,
    db_session: Session,
    records: tuple[Project, Inventory],
    app_data: Path,
) -> None:
    """R1-V3G1B'den beri aynı zamanda bir **geriye uyumluluk** kilidi.

    Belge, sınıflandırma ayrılmadan önce yazılan kaydın şeklidir: recap'te
    ``failures=1`` bulunmasına rağmen kod ``runner_failed``'dır. Bugün aynı
    çalıştırma ``playbook_failed`` üretirdi; diskteki eski belgeler **yeniden
    sınıflandırılmaz** ve okunmaya devam eder.
    """
    project, inventory = records
    job_id = _seed(
        db_session,
        project,
        inventory,
        status=JobStatus.FAILED,
        return_code=2,
        error_code=ERROR_RUNNER_FAILED,
    )
    _publish(app_data, job_id, _failed_document(job_id))

    response = client.get(f"/api/jobs/{job_id}/result")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "failed"
    assert body["return_code"] == 2
    assert body["error_code"] == ERROR_RUNNER_FAILED


def test_the_playbook_failure_code_is_identical_on_every_read_surface(
    client: TestClient,
    db_session: Session,
    records: tuple[Project, Inventory],
    app_data: Path,
) -> None:
    """Liste, detay ve sonuç aynı kodu **aynı biçimde** döner.

    Üç yüzeyin ayrışması iki yönden zararlı olurdu: listede altyapı arızası gibi
    görünen bir bulgu ya da detayda daraltılmış (``unknown_failure``) bir kod.
    Sonuç okuma yolu ayrıca DB ile belgeyi karşılaştırır; eşitlik bozuk olsaydı
    bu istek 200 değil 503 dönerdi.
    """
    project, inventory = records
    job_id = _seed(
        db_session,
        project,
        inventory,
        status=JobStatus.FAILED,
        return_code=2,
        error_code=ERROR_PLAYBOOK_FAILED,
    )
    _publish(app_data, job_id, _failed_document(job_id, error_code=ERROR_PLAYBOOK_FAILED))

    listing = client.get("/api/jobs")
    detail = client.get(f"/api/jobs/{job_id}")
    result = client.get(f"/api/jobs/{job_id}/result")

    assert listing.status_code == 200, listing.text
    assert detail.status_code == 200, detail.text
    assert result.status_code == 200, result.text

    summary = next(item for item in listing.json()["items"] if item["job_id"] == job_id)
    assert summary["error_code"] == ERROR_PLAYBOOK_FAILED
    assert detail.json()["error_code"] == ERROR_PLAYBOOK_FAILED
    assert result.json()["error_code"] == ERROR_PLAYBOOK_FAILED

    # Yeni kod yeni bir alan ya da serbest metin getirmez.
    assert set(summary) == set(detail.json())
    assert result.json()["schema_version"] == SCHEMA_VERSION


def test_a_result_document_that_disagrees_with_the_row_is_a_generic_503(
    client: TestClient,
    db_session: Session,
    records: tuple[Project, Inventory],
    app_data: Path,
) -> None:
    """DB ↔ artifact ``error_code`` eşitliği yeni kodla da **exact** kalır.

    Satır eski kodu, belge yeni kodu taşıyorsa dosya bu Job'ın güncel kaydını
    taşımıyor demektir. Eşitlik gevşetilseydi, yeniden sınıflandırılmış bir
    belge sessizce eski bir satırın üstüne okunurdu.
    """
    project, inventory = records
    job_id = _seed(
        db_session,
        project,
        inventory,
        status=JobStatus.FAILED,
        return_code=2,
        error_code=ERROR_RUNNER_FAILED,
    )
    _publish(app_data, job_id, _failed_document(job_id, error_code=ERROR_PLAYBOOK_FAILED))

    _assert_generic_unavailable(client.get(f"/api/jobs/{job_id}/result"))


def _assert_generic_unavailable(response: Any) -> None:
    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "job_result_unavailable"
    assert error["details"] == {"reason": "unavailable"}


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.RUNNING, JobStatus.CANCELED])
def test_a_non_terminal_job_result_is_a_generic_503(
    client: TestClient,
    db_session: Session,
    records: tuple[Project, Inventory],
    status: JobStatus,
) -> None:
    project, inventory = records
    job_id = _seed(db_session, project, inventory, status=status)

    response = client.get(f"/api/jobs/{job_id}/result")

    _assert_generic_unavailable(response)


def test_a_terminal_job_without_a_recorded_result_is_a_generic_503(
    client: TestClient, db_session: Session, records: tuple[Project, Inventory]
) -> None:
    project, inventory = records
    job_id = _seed(db_session, project, inventory, status=JobStatus.SUCCESSFUL, artifact_path=None)

    response = client.get(f"/api/jobs/{job_id}/result")

    _assert_generic_unavailable(response)


def test_a_corrupt_result_document_is_a_generic_503(
    client: TestClient,
    db_session: Session,
    records: tuple[Project, Inventory],
    app_data: Path,
) -> None:
    project, inventory = records
    job_id = _seed(db_session, project, inventory, status=JobStatus.SUCCESSFUL, return_code=0)
    broken = _successful_document(job_id)
    del broken["recap"]
    _publish(app_data, job_id, broken)

    response = client.get(f"/api/jobs/{job_id}/result")

    _assert_generic_unavailable(response)


# --- Girdi doğrulaması --------------------------------------------------------------


def test_a_malformed_job_id_is_a_sanitized_422(client: TestClient) -> None:
    for path in ("/api/jobs/not-a-uuid", "/api/jobs/not-a-uuid/result"):
        response = client.get(path)
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "request_validation_error"


def test_a_syntactically_valid_non_v4_uuid_job_id_is_a_sanitized_422(
    client: TestClient,
) -> None:
    """UUID1 sözdizimsel olarak geçerlidir ama domain yalnız UUID4 kabul eder.

    Path tipi genel ``uuid.UUID`` olsaydı bu değer FastAPI doğrulamasını
    geçer, domain'de ``ValueError`` üretir ve public API'de kontrolsüz bir
    500'e dönüşürdü. ``pydantic.UUID4`` sayesinde istek domain'e hiç
    ulaşmadan 422'de düşer.
    """
    not_v4 = str(uuid.uuid1())

    response = client.get(f"/api/jobs/{not_v4}")

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "request_validation_error"
    assert not_v4 not in response.text


def test_a_syntactically_valid_non_v4_uuid_job_id_on_the_result_path_is_a_sanitized_422(
    client: TestClient,
) -> None:
    not_v4 = str(uuid.uuid1())

    response = client.get(f"/api/jobs/{not_v4}/result")

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "request_validation_error"
    assert not_v4 not in response.text


def test_a_syntactically_valid_non_v4_uuid_cursor_is_a_sanitized_422(
    client: TestClient,
) -> None:
    not_v4 = str(uuid.uuid1())

    response = client.get(
        "/api/jobs",
        params={"before_created_at": "2026-01-01T00:00:00Z", "before_job_id": not_v4},
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "request_validation_error"
    assert not_v4 not in response.text


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"project_id": 0},
        {"project_id": -1},
        {"status": "bogus-status"},
        {"mode": "diff"},
        {"before_created_at": "2026-01-01T00:00:00+03:00"},
        {"before_created_at": "2026-01-01T00:00:00Z"},
        {"before_job_id": str(uuid.uuid4())},
    ],
)
def test_invalid_query_parameters_are_a_sanitized_422(
    client: TestClient, params: dict[str, Any]
) -> None:
    response = client.get("/api/jobs", params=params)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "request_validation_error"


# --- Aktör --------------------------------------------------------------------------


def test_the_actor_cannot_be_overridden_by_the_request(
    client: TestClient, db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """``requested_by`` bir query/header alanı değildir; her istek sunucu aktörüyle çalışır."""
    project, inventory = records
    mine = _seed(db_session, project, inventory, requested_by=ACTOR)
    theirs = _seed(db_session, project, inventory, requested_by=OTHER_ACTOR)

    response = client.get(
        "/api/jobs",
        params={"requested_by": OTHER_ACTOR},
        headers={"X-Requested-By": OTHER_ACTOR},
    )
    assert response.status_code == 200, response.text
    ids = {item["job_id"] for item in response.json()["items"]}
    assert mine in ids
    assert theirs not in ids


# --- Sızdırmazlık ve yasak alanlar ----------------------------------------------------


def test_no_forbidden_field_appears_in_list_detail_or_result(
    client: TestClient,
    db_session: Session,
    records: tuple[Project, Inventory],
    app_data: Path,
    settings: Settings,
) -> None:
    """Yasaklı **alan adları** üç cevapta da hiç geçmez.

    R1-V3J3A'dan sonra ``stdout``/``stderr`` listede kalır ama anlamı dardır:
    ölçülen şey runner'ın ham alan **adlarının** yeniden üretilmemesidir. Display
    metni ayrı ve açıkça adlandırılmış ``ansible_output`` alanında durur;
    :data:`DISPLAY_OUTPUT` bu yüzden bilinçle o tokenları içermeyecek biçimde
    seçilmiştir — aksi hâlde test, sözleşmeyi değil fixture metnini ölçerdi.
    """
    project, inventory = records
    job_id = _seed(db_session, project, inventory, status=JobStatus.SUCCESSFUL, return_code=0)
    _publish(app_data, job_id, _successful_document(job_id))

    responses = [
        client.get("/api/jobs"),
        client.get(f"/api/jobs/{job_id}"),
        client.get(f"/api/jobs/{job_id}/result"),
    ]
    for response in responses:
        assert response.status_code == 200, response.text
        for forbidden in FORBIDDEN_FIELDS:
            assert forbidden not in response.text, forbidden
        assert settings.local_actor not in response.text
        assert str(settings.app_data_dir) not in response.text


# --- OpenAPI ve launch sözleşmesi ------------------------------------------------------


def test_openapi_exposes_exactly_three_job_get_operations(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()

    job_paths = {path for path in spec["paths"] if path.startswith("/api/jobs")}
    assert job_paths == {"/api/jobs", "/api/jobs/{job_id}", "/api/jobs/{job_id}/result"}
    for path in job_paths:
        assert set(spec["paths"][path]) == {"get"}


def test_openapi_locks_the_mode_query_parameter_on_the_list_operation(
    client: TestClient,
) -> None:
    """`mode`, yayımlanan API sözleşmesinde de kilitli bir query parametresidir.

    Yalnız route davranışı (bkz. `test_the_mode_query_parameter_narrows_the_page`,
    `test_invalid_query_parameters_are_a_sanitized_422`) değil, `/openapi.json`'ın
    kendisi ölçülür: parametre `query`'de durmalı, zorunlu **olmamalı** ve yalnız
    `check`/`normal` değerlerini kabul etmelidir (R1-V3J0B2-AUDIT-FIX1, bulgu 1).
    Ham bir string parametre — zorunlu olsun ya da olmasın — bu sözleşmede
    görünmez; enum dışı bir değer istemciye şemadan **önce** görünür olurdu.
    """
    spec = client.get("/openapi.json").json()
    parameters = spec["paths"]["/api/jobs"]["get"]["parameters"]

    mode_parameters = [parameter for parameter in parameters if parameter["name"] == "mode"]
    assert len(mode_parameters) == 1
    mode_parameter = mode_parameters[0]

    assert mode_parameter["in"] == "query"
    assert mode_parameter["required"] is False

    schema = mode_parameter["schema"]
    # Opsiyonellik `anyOf`'taki `null` üyesiyle şemada da açıkça durur; yalnız
    # `required: false` üzerinden çıkarılmaz.
    assert {"type": "null"} in schema["anyOf"]

    refs = [entry["$ref"] for entry in schema["anyOf"] if "$ref" in entry]
    assert len(refs) == 1
    mode_schema_name = refs[0].rsplit("/", 1)[-1]
    assert spec["components"]["schemas"][mode_schema_name]["enum"] == ["check", "normal"]


def test_no_job_mutation_endpoint_was_added(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()

    for path, operations in spec["paths"].items():
        if not path.startswith("/api/jobs"):
            continue
        for method in operations:
            assert method == "get", (path, method)


def test_the_existing_launch_contract_is_unchanged(client: TestClient) -> None:
    """Launch gövdesi bu dilimde de dardır; R1-V3H2A yalnız ``mode``'u ekler."""
    spec = client.get("/openapi.json").json()
    launch_path = "/api/projects/{project_id}/executions"

    assert set(spec["paths"][launch_path]) == {"post"}
    schemas = spec["components"]["schemas"]
    request_ref = spec["paths"][launch_path]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    request_name = request_ref.rsplit("/", 1)[-1]
    assert set(schemas[request_name]["properties"]) == {
        "plan_token",
        "mode",
        "inventory_id",
        "playbook_path",
    }


# --- R1-V3J3A: display output cevap yüzeyi -----------------------------------


def test_the_result_response_carries_the_raw_display_output(
    client: TestClient,
    db_session: Session,
    records: tuple[Project, Inventory],
    app_data: Path,
) -> None:
    """Ham Ansible çıktısı yetkili sonuç cevabında **bulunur** ve sansürlenmez.

    Bu bir sızıntı testi değil, sözleşmenin kendisidir: trusted-operator
    modelinde operatörün CLI'da göreceği çıktı UI'da da görünür. Metin
    credential veya playbook kaynak satırı içerebilir; platform bunun aksini
    iddia etmez.
    """
    project, inventory = records
    job_id = _seed(db_session, project, inventory, status=JobStatus.SUCCESSFUL, return_code=0)
    document = _successful_document(job_id)
    # Vacuous değil: sentinel gerçekten yayımlanan belgede.
    assert "SENTINEL-DISPLAY-PW" in document["ansible_output"]
    _publish(app_data, job_id, document)

    response = client.get(f"/api/jobs/{job_id}/result")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == SAFE_RESULT_FIELDS
    assert body["schema_version"] == SCHEMA_VERSION
    assert body["ansible_output"] == DISPLAY_OUTPUT
    assert body["ansible_output_truncated"] is False
    # Ham çıktı önbelleğe alınmaz.
    assert response.headers["Cache-Control"] == "no-store"


def test_a_version_one_artifact_returns_empty_output_fields(
    client: TestClient,
    db_session: Session,
    records: tuple[Project, Inventory],
    app_data: Path,
) -> None:
    """Eski artifact tek cevap şeklini bozmaz: alanlar ``null``/``false`` döner."""
    project, inventory = records
    job_id = _seed(db_session, project, inventory, status=JobStatus.SUCCESSFUL, return_code=0)
    document = _legacy_document(job_id)
    assert "ansible_output" not in document
    _publish(app_data, job_id, document)

    response = client.get(f"/api/jobs/{job_id}/result")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == SAFE_RESULT_FIELDS
    assert body["schema_version"] == LEGACY_SCHEMA_VERSION
    assert body["ansible_output"] is None
    assert body["ansible_output_truncated"] is False
    assert response.headers["Cache-Control"] == "no-store"


def test_the_raw_output_is_not_returned_to_another_actor(
    client: TestClient,
    db_session: Session,
    records: tuple[Project, Inventory],
    app_data: Path,
) -> None:
    """Ham çıktı yalnız Job'ın aktörüne döner; ayrım generic 404'te kalır.

    Yetkilendirme yeni bir kural kazanmadı: ``ansible_output`` mevcut Job/result
    yetkilendirmesinin arkasındadır ve başka bir aktör ile hiç var olmayan bir
    Job aynı cevabı alır — ayrım yapan bir cevap, ham çıktının varlığını bile
    ölçülebilir kılardı.
    """
    project, inventory = records
    theirs = _seed(
        db_session,
        project,
        inventory,
        status=JobStatus.SUCCESSFUL,
        return_code=0,
        requested_by=OTHER_ACTOR,
    )
    _publish(app_data, theirs, _successful_document(theirs))
    missing = str(uuid.uuid4())

    for job_id in (theirs, missing):
        response = client.get(f"/api/jobs/{job_id}/result")

        assert response.status_code == 404, response.text
        assert "SENTINEL-DISPLAY-PW" not in response.text
        error = response.json()["error"]
        assert error["code"] == "job_not_found"
        assert error["details"] == {"reason": "not_found"}


def test_the_raw_output_never_appears_on_the_list_or_detail_endpoints(
    client: TestClient,
    db_session: Session,
    records: tuple[Project, Inventory],
    app_data: Path,
) -> None:
    """Özet yüzeyleri ham çıktıyı taşımaz; alan adı bile geçmez."""
    project, inventory = records
    job_id = _seed(db_session, project, inventory, status=JobStatus.SUCCESSFUL, return_code=0)
    _publish(app_data, job_id, _successful_document(job_id))

    # Vacuous değil: aynı Job'ın sonuç cevabı metni gerçekten taşıyor.
    assert "SENTINEL-DISPLAY-PW" in client.get(f"/api/jobs/{job_id}/result").text

    for response in (client.get("/api/jobs"), client.get(f"/api/jobs/{job_id}")):
        assert response.status_code == 200, response.text
        assert "SENTINEL-DISPLAY-PW" not in response.text
        assert "ansible_output" not in response.text


def test_openapi_only_widens_the_existing_result_response(client: TestClient) -> None:
    """Yeni route yok; genişleyen tek şey mevcut sonuç cevabının şemasıdır."""
    spec = client.get("/openapi.json").json()

    job_paths = {path for path in spec["paths"] if path.startswith("/api/jobs")}
    assert job_paths == {"/api/jobs", "/api/jobs/{job_id}", "/api/jobs/{job_id}/result"}

    schemas = spec["components"]["schemas"]
    result_properties = schemas["PlaybookJobResultResponse"]["properties"]
    assert set(result_properties) == SAFE_RESULT_FIELDS
    assert set(schemas["PlaybookJobResultResponse"]["required"]) == SAFE_RESULT_FIELDS

    for name in ("PlaybookJobSummaryResponse", "PlaybookResultEventResponse"):
        assert "ansible_output" not in schemas[name]["properties"], name
