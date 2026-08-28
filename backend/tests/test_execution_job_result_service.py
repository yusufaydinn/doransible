"""R1-V3D2A2B2 birleşik salt-okunur Job sonucu servisinin sözleşmesi.

Ölçülen sınırlar:

1. Successful/failed mutlu yol: gerçek DB Job'ı ve gerçek depoyla yazılmış
   sonuç birlikte doğrulanmış bir :class:`PlaybookJobResult` üretir.
2. Yetkisiz/olmayan/PING/binding-invalid Job'lar D2A1'in aynı 404 sözleşmesini
   korur ve bu yolda dosya sistemine hiç dokunulmaz.
3. Terminal olmayan durum ve kaydedilmemiş sonuç aynı sabit 503'e düşer; bu
   yollarda da dosya sistemine dokunulmaz.
4. Bozuk/eksik/başka-Job'a-ait artifact aynı sabit 503'tür.
5. DB özeti ile ayrıştırılmış belge arasındaki alan uyuşmazlıkları (durum ↔
   outcome, return_code, error_code, result_truncated, job_id) tek tek aynı
   sabit 503'e düşer.
6. Çağıran hatası (biçimsiz kimlik, aralık dışı sınır, geçersiz app-data kökü)
   SQL ve dosya sisteminden önce ``ValueError`` olur.
7. Servis yalnız ``SELECT`` çalıştırır ve her yolda (mutlu, 404, 503) çıkışta
   açık transaction bırakmaz.
"""

from __future__ import annotations

import ast
import inspect
import os
import uuid
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event
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
from app.services.execution import result_service
from app.services.execution.normalize import (
    ERROR_PLAYBOOK_FAILED,
    ERROR_RUNNER_FAILED,
    LEGACY_SCHEMA_VERSION,
    OUTCOME_FAILED,
    OUTCOME_SUCCESSFUL,
    SCHEMA_VERSION,
)
from app.services.execution.read import JobNotFoundError, PlaybookJobSummary
from app.services.execution.result import (
    MAX_ALLOWED_EVENTS,
    MIN_ALLOWED_RESULT_BYTES,
    JobResultUnavailableError,
    PlaybookJobResult,
)
from app.services.execution.result_reader import DIRECTORY_MODE, JOBS_DIRNAME, RESULT_FILENAME
from app.services.execution.result_service import get_playbook_job_result
from app.services.jobs.artifacts import JobArtifactStore

ACTOR = "yerel-operator"
OTHER_ACTOR = "baska-operator"
PLAYBOOK_PATH = "site.yml"
MAX_EVENTS = 100
MAX_RESULT_BYTES = 100_000


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
    status: ExecutionPlanStatus = ExecutionPlanStatus.CLAIMED,
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
            status=status,
            created_at=moment,
            expires_at=moment + timedelta(hours=1),
            claimed_at=moment if status is ExecutionPlanStatus.CLAIMED else None,
        )
    )
    session.flush()
    return plan_id


_DEFAULT: Any = object()


def _seed(
    session: Session,
    records: tuple[Project, Inventory],
    *,
    job_id: str | None = None,
    job_type: JobType = JobType.PLAYBOOK,
    status: JobStatus = JobStatus.SUCCESSFUL,
    requested_by: str = ACTOR,
    created_at: datetime | None = None,
    with_plan: bool = True,
    playbook_path: str | None = PLAYBOOK_PATH,
    return_code: int | None = None,
    error_code: str | None = None,
    result_truncated: bool = False,
    artifact_path: Any = _DEFAULT,
    plan_overrides: dict[str, Any] | None = None,
) -> str:
    """Tek bir Job satırı (ve gerekiyorsa onu yetkilendiren planı) yazar."""
    project, inventory = records
    moment = created_at or datetime.now(UTC)
    identifier = job_id or str(uuid.uuid4())

    plan_id: str | None = None
    if with_plan:
        plan_fields: dict[str, Any] = {
            "project_id": project.id,
            "inventory_id": inventory.id,
            "requested_by": requested_by,
            "playbook_path": PLAYBOOK_PATH if playbook_path is None else playbook_path,
            "moment": moment,
        }
        plan_fields.update(plan_overrides or {})
        plan_id = _plan(session, **plan_fields)

    resolved_artifact = (
        f"{JOBS_DIRNAME}/{identifier}/{RESULT_FILENAME}"
        if artifact_path is _DEFAULT
        else artifact_path
    )

    fields: dict[str, Any] = {
        "id": identifier,
        "job_type": job_type,
        "status": status,
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
        if job_type is JobType.PLAYBOOK:
            fields["worker_id"] = str(uuid.uuid4())
            fields["heartbeat_at"] = moment
            fields["lease_expires_at"] = moment + timedelta(seconds=30)
    session.add(Job(**fields))
    session.commit()
    return identifier


@pytest.fixture
def app_data(settings: Settings, migrated_engine: Engine) -> Path:
    """Uygulamanın gerçek app-data kökü.

    ``migrated_engine`` SQLite için :func:`ensure_app_data_dirs`'i zaten
    çalıştırır ve ``jobs`` alt dizinini 0700 olarak kurar; ayrı bir kök
    kurmak bu düzeni ikinci kez ve tutarsız biçimde inşa etmek olurdu.
    """
    return settings.app_data_dir


# Yetkili sonuç cevabının taşıdığı ham display metni. Değer bilinçle "hassas"
# görünür: testler onun **taşındığını** ölçer, temizlendiğini değil.
DISPLAY_OUTPUT = "ok: [web-1] => ansible_become_password=SENTINEL-DISPLAY-PW"


def successful_document(job_id: str, **overrides: Any) -> dict[str, Any]:
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


def failed_document(job_id: str, **overrides: Any) -> dict[str, Any]:
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


def legacy_document(job_id: str, **overrides: Any) -> dict[str, Any]:
    """``schema_version=1`` artifact'i: output alanlarını **hiç** taşımaz."""
    document = successful_document(job_id)
    document["schema_version"] = LEGACY_SCHEMA_VERSION
    del document["ansible_output"]
    del document["ansible_output_truncated"]
    document.update(overrides)
    return document


def publish(app_data: Path, job_id: str, document: dict[str, Any]) -> None:
    store = JobArtifactStore(app_data)
    store.create(job_id)
    store.write_result(job_id, document)


def call(
    session: Session,
    job_id: str,
    app_data: Path,
    *,
    requested_by: str = ACTOR,
    max_events: int = MAX_EVENTS,
    max_result_bytes: int = MAX_RESULT_BYTES,
) -> PlaybookJobResult:
    return get_playbook_job_result(
        session,
        job_id,
        requested_by=requested_by,
        app_data_dir=app_data,
        max_events=max_events,
        max_result_bytes=max_result_bytes,
    )


@pytest.fixture
def counted_statements(migrated_engine: Engine) -> Iterator[list[str]]:
    seen: list[str] = []

    def _record(_conn: Any, _cursor: Any, statement: str, *_args: Any, **_kwargs: Any) -> None:
        seen.append(statement)

    event.listen(migrated_engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(migrated_engine, "before_cursor_execute", _record)


def block_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Bu yolda dosya sistemine dokunulmamalıdır.")

    monkeypatch.setattr(os, "open", boom)
    monkeypatch.setattr(os, "stat", boom)


# --- Mutlu yollar ---------------------------------------------------------------


def test_a_successful_job_result_is_composed(
    db_session: Session, records: tuple[Project, Inventory], app_data: Path
) -> None:
    job_id = _seed(db_session, records, status=JobStatus.SUCCESSFUL, return_code=0)
    publish(app_data, job_id, successful_document(job_id))

    result = call(db_session, job_id, app_data)

    assert result.job_id == job_id
    assert result.outcome == OUTCOME_SUCCESSFUL
    assert result.return_code == 0
    assert result.error_code is None
    assert result.result_truncated is False
    assert result.recap["web-1"].ok == 1
    assert len(result.events) == 1
    assert not db_session.in_transaction()


def test_a_failed_job_result_is_composed(
    db_session: Session, records: tuple[Project, Inventory], app_data: Path
) -> None:
    job_id = _seed(
        db_session,
        records,
        status=JobStatus.FAILED,
        return_code=2,
        error_code=ERROR_RUNNER_FAILED,
    )
    publish(app_data, job_id, failed_document(job_id))

    result = call(db_session, job_id, app_data)

    assert result.job_id == job_id
    assert result.outcome == OUTCOME_FAILED
    assert result.return_code == 2
    assert result.error_code == ERROR_RUNNER_FAILED
    assert not db_session.in_transaction()


def test_a_playbook_failure_row_and_document_are_composed_together(
    db_session: Session, records: tuple[Project, Inventory], app_data: Path
) -> None:
    """Yeni kod da sıradan bir sonuçtur: satır ve belge eşitse okuma başarılıdır.

    ``playbook_failed`` için ayrı bir özel durum **yoktur**: eşitlik kuralı
    kodun kendisine bakmaz, yalnız satır ile belgenin aynı değeri taşımasına
    bakar.
    """
    job_id = _seed(
        db_session,
        records,
        status=JobStatus.FAILED,
        return_code=2,
        error_code=ERROR_PLAYBOOK_FAILED,
    )
    publish(app_data, job_id, failed_document(job_id, error_code=ERROR_PLAYBOOK_FAILED))

    result = call(db_session, job_id, app_data)

    assert result.outcome == OUTCOME_FAILED
    assert result.error_code == ERROR_PLAYBOOK_FAILED
    assert result.recap["web-1"].failures == 1


def test_a_legacy_runner_failed_pair_is_never_reclassified_on_read(
    db_session: Session, records: tuple[Project, Inventory], app_data: Path
) -> None:
    """Eski kayıtlar okundukları yerde **yeniden sınıflandırılmaz**.

    Belge, sınıflandırma ayrılmadan önce yazılmış bir çalıştırmanın şeklidir:
    recap gerçek bir host failure bildirir ama kod ``runner_failed``'dır. Okuma
    yolu bunu "aslında ``playbook_failed`` olmalıydı" diye düzeltmeye kalksaydı
    DB ↔ artifact eşitliği kendi kendini bozar ve eski her Job okunamaz olurdu.
    """
    job_id = _seed(
        db_session,
        records,
        status=JobStatus.FAILED,
        return_code=2,
        error_code=ERROR_RUNNER_FAILED,
    )
    publish(app_data, job_id, failed_document(job_id))

    result = call(db_session, job_id, app_data)

    assert result.error_code == ERROR_RUNNER_FAILED
    # Kanıt belgede duruyor ama kod korunuyor: düzeltme yapılmadı.
    assert result.recap["web-1"].failures == 1


@pytest.mark.parametrize(
    ("row_code", "document_code"),
    [
        pytest.param(ERROR_RUNNER_FAILED, ERROR_PLAYBOOK_FAILED, id="row-legacy-document-new"),
        pytest.param(ERROR_PLAYBOOK_FAILED, ERROR_RUNNER_FAILED, id="row-new-document-legacy"),
    ],
)
def test_a_classification_disagreement_between_row_and_document_is_unavailable(
    db_session: Session,
    records: tuple[Project, Inventory],
    app_data: Path,
    row_code: str,
    document_code: str,
) -> None:
    """İki kod arasındaki ayrışma **her iki yönde de** 503'tür.

    Eşitlik gevşetilseydi, yeniden sınıflandırılmış bir belge eski bir satırın
    üstüne sessizce okunur ve kullanıcı iki farklı yüzeyde iki farklı sebep
    görürdü.
    """
    job_id = _seed(
        db_session,
        records,
        status=JobStatus.FAILED,
        return_code=2,
        error_code=row_code,
    )
    publish(app_data, job_id, failed_document(job_id, error_code=document_code))

    with pytest.raises(JobResultUnavailableError):
        call(db_session, job_id, app_data)


def test_the_result_is_immutable(
    db_session: Session, records: tuple[Project, Inventory], app_data: Path
) -> None:
    job_id = _seed(db_session, records, status=JobStatus.SUCCESSFUL, return_code=0)
    publish(app_data, job_id, successful_document(job_id))

    result = call(db_session, job_id, app_data)

    with pytest.raises(TypeError):
        result.recap["web-1"] = None  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        result.events = ()  # type: ignore[misc]


# --- Yetkilendirme: aynı 404, sıfır dosya sistemi --------------------------------


@pytest.mark.parametrize(
    "invisible",
    ["missing", "other_actor", "ping", "planless"],
)
def test_invisible_jobs_produce_the_same_not_found_with_zero_filesystem(
    db_session: Session,
    records: tuple[Project, Inventory],
    app_data: Path,
    invisible: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if invisible == "missing":
        job_id = str(uuid.uuid4())
    elif invisible == "other_actor":
        job_id = _seed(db_session, records, requested_by=OTHER_ACTOR)
    elif invisible == "ping":
        job_id = _seed(
            db_session, records, job_type=JobType.PING, with_plan=False, playbook_path=None
        )
    else:
        job_id = _seed(db_session, records, with_plan=False)

    block_filesystem(monkeypatch)

    with pytest.raises(JobNotFoundError) as caught:
        call(db_session, job_id, app_data)

    assert caught.value.status_code == 404
    assert caught.value.code == "job_not_found"
    assert not db_session.in_transaction()


def test_a_binding_invalid_job_produces_the_same_not_found_with_zero_filesystem(
    db_session: Session,
    records: tuple[Project, Inventory],
    app_data: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bağı bozuk terminal satır: plan ``expired``, Job aksi hâlde sağlam."""
    job_id = _seed(
        db_session,
        records,
        plan_overrides={"status": ExecutionPlanStatus.EXPIRED},
    )

    block_filesystem(monkeypatch)

    with pytest.raises(JobNotFoundError):
        call(db_session, job_id, app_data)


# --- Terminal olmayan durum / kaydedilmemiş sonuç: sabit 503, sıfır dosya --------


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.RUNNING, JobStatus.CANCELED])
def test_a_non_terminal_job_is_unavailable_and_never_opens_a_file(
    db_session: Session,
    records: tuple[Project, Inventory],
    app_data: Path,
    status: JobStatus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `has_recorded_result` kasıtlı olarak True: kapının sebebinin durum
    # olduğu, kaydedilmiş sonucun yokluğu olmadığı ölçülür.
    job_id = _seed(db_session, records, status=status)

    block_filesystem(monkeypatch)

    with pytest.raises(JobResultUnavailableError) as caught:
        call(db_session, job_id, app_data)

    assert caught.value.status_code == 503
    assert caught.value.code == "job_result_unavailable"
    assert not db_session.in_transaction()


def test_a_terminal_job_without_a_recorded_result_is_unavailable_and_never_opens_a_file(
    db_session: Session,
    records: tuple[Project, Inventory],
    app_data: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = _seed(db_session, records, status=JobStatus.SUCCESSFUL, artifact_path=None)

    block_filesystem(monkeypatch)

    with pytest.raises(JobResultUnavailableError):
        call(db_session, job_id, app_data)


# --- Bozuk/eksik artifact: aynı sabit 503 ----------------------------------------


def test_a_missing_result_file_is_unavailable(
    db_session: Session, records: tuple[Project, Inventory], app_data: Path
) -> None:
    job_id = _seed(db_session, records, status=JobStatus.SUCCESSFUL)
    # Job dizini bile hiç oluşturulmadı; `has_recorded_result` yine de True
    # (artifact_path Job'a ait beklenen değeri taşıyor).

    with pytest.raises(JobResultUnavailableError):
        call(db_session, job_id, app_data)


def test_a_corrupt_document_is_unavailable(
    db_session: Session, records: tuple[Project, Inventory], app_data: Path
) -> None:
    job_id = _seed(db_session, records, status=JobStatus.SUCCESSFUL)
    job_dir = app_data / JOBS_DIRNAME / job_id
    job_dir.mkdir(parents=True)
    os.chmod(job_dir, DIRECTORY_MODE)
    path = job_dir / RESULT_FILENAME
    path.write_bytes(b"{bozuk json")
    os.chmod(path, 0o600)

    with pytest.raises(JobResultUnavailableError):
        call(db_session, job_id, app_data)


def test_another_jobs_document_is_unavailable(
    db_session: Session, records: tuple[Project, Inventory], app_data: Path
) -> None:
    """Belge sözdizimsel olarak geçerli ama başka bir Job'a ait."""
    job_id = _seed(db_session, records, status=JobStatus.SUCCESSFUL)
    other_id = str(uuid.uuid4())
    publish(app_data, job_id, successful_document(other_id))

    with pytest.raises(JobResultUnavailableError):
        call(db_session, job_id, app_data)


# --- DB ↔ artifact tutarlılığı ---------------------------------------------------


def test_a_status_outcome_mismatch_is_unavailable(
    db_session: Session, records: tuple[Project, Inventory], app_data: Path
) -> None:
    """DB ``failed`` diyor, belge kendi içinde tutarlı bir ``successful``."""
    job_id = _seed(db_session, records, status=JobStatus.FAILED, error_code=ERROR_RUNNER_FAILED)
    publish(app_data, job_id, successful_document(job_id))

    with pytest.raises(JobResultUnavailableError):
        call(db_session, job_id, app_data)


def test_a_return_code_mismatch_is_unavailable(
    db_session: Session, records: tuple[Project, Inventory], app_data: Path
) -> None:
    job_id = _seed(
        db_session,
        records,
        status=JobStatus.FAILED,
        return_code=99,
        error_code=ERROR_RUNNER_FAILED,
    )
    publish(app_data, job_id, failed_document(job_id, return_code=2))

    with pytest.raises(JobResultUnavailableError):
        call(db_session, job_id, app_data)


def test_an_error_code_mismatch_is_unavailable(
    db_session: Session, records: tuple[Project, Inventory], app_data: Path
) -> None:
    """DB'nin tanımadığı kod ``unknown_failure``'a daralır; belgenin bilinen kodu eşleşmez."""
    job_id = _seed(
        db_session,
        records,
        status=JobStatus.FAILED,
        return_code=2,
        error_code="serbest_metin_hata",
    )
    publish(app_data, job_id, failed_document(job_id, error_code=ERROR_RUNNER_FAILED))

    with pytest.raises(JobResultUnavailableError):
        call(db_session, job_id, app_data)


def test_a_result_truncated_mismatch_is_unavailable(
    db_session: Session, records: tuple[Project, Inventory], app_data: Path
) -> None:
    job_id = _seed(
        db_session,
        records,
        status=JobStatus.FAILED,
        return_code=2,
        error_code=ERROR_RUNNER_FAILED,
        result_truncated=False,
    )
    publish(app_data, job_id, failed_document(job_id, result_truncated=True))

    with pytest.raises(JobResultUnavailableError):
        call(db_session, job_id, app_data)


def test_a_job_id_mismatch_between_summary_and_result_is_unavailable() -> None:
    """Uçtan uca üretilemeyen tek vaka: iki nesne doğrudan kurulup karşılaştırılır.

    Normal akışta ``result.job_id`` her zaman ``expected_job_id`` ile (dolayısıyla
    ``summary.job_id`` ile) aynıdır; parser bunu zaten zorlar. Bu satır yine de
    :func:`_require_consistent_with_summary`'nin bağımsız bir savunma katmanı
    olduğunu doğrudan ölçer.
    """
    moment = datetime.now(UTC)
    summary = PlaybookJobSummary(
        job_id=str(uuid.uuid4()),
        status=JobStatus.SUCCESSFUL,
        mode=ExecutionMode.CHECK,
        project_id=1,
        project_name="Web",
        inventory_id=1,
        inventory_name="Prod",
        playbook_path=PLAYBOOK_PATH,
        return_code=0,
        error_code=None,
        result_truncated=False,
        has_recorded_result=True,
        created_at=moment,
        started_at=moment,
        finished_at=moment,
    )
    result = PlaybookJobResult(
        schema_version=SCHEMA_VERSION,
        job_id=str(uuid.uuid4()),
        return_code=0,
        outcome=OUTCOME_SUCCESSFUL,
        error_code=None,
        recap={},
        events=(),
        events_truncated=False,
        result_truncated=False,
        ansible_output=None,
        ansible_output_truncated=False,
    )

    with pytest.raises(JobResultUnavailableError):
        result_service._require_consistent_with_summary(summary, result)


# --- Çağıran hatası: SQL ve dosya sisteminden önce --------------------------------


def test_a_non_canonical_job_id_is_a_caller_error(
    db_session: Session,
    app_data: Path,
    counted_statements: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    block_filesystem(monkeypatch)

    with pytest.raises(ValueError):
        call(db_session, "GECERSIZ", app_data)

    assert counted_statements == []


def test_a_relative_app_data_root_is_a_caller_error(
    db_session: Session, counted_statements: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    block_filesystem(monkeypatch)

    with pytest.raises(ValueError):
        call(db_session, str(uuid.uuid4()), Path("app-data"))

    assert counted_statements == []


@pytest.mark.parametrize(
    "value",
    [True, 0, -1, MAX_ALLOWED_EVENTS + 1, "100"],
    ids=["bool", "zero", "negative", "over", "str"],
)
def test_an_invalid_max_events_is_a_caller_error(
    db_session: Session,
    app_data: Path,
    counted_statements: list[str],
    monkeypatch: pytest.MonkeyPatch,
    value: Any,
) -> None:
    block_filesystem(monkeypatch)

    with pytest.raises(ValueError):
        call(db_session, str(uuid.uuid4()), app_data, max_events=value)

    assert counted_statements == []


@pytest.mark.parametrize(
    "value",
    [True, MIN_ALLOWED_RESULT_BYTES - 1, 0, "100000"],
    ids=["bool", "below_floor", "zero", "str"],
)
def test_an_invalid_max_result_bytes_is_a_caller_error(
    db_session: Session,
    app_data: Path,
    counted_statements: list[str],
    monkeypatch: pytest.MonkeyPatch,
    value: Any,
) -> None:
    block_filesystem(monkeypatch)

    with pytest.raises(ValueError):
        call(db_session, str(uuid.uuid4()), app_data, max_result_bytes=value)

    assert counted_statements == []


def test_a_caller_error_is_not_the_unavailable_error(db_session: Session, app_data: Path) -> None:
    with pytest.raises(ValueError) as caught:
        call(db_session, "GECERSIZ", app_data)

    assert not isinstance(caught.value, JobResultUnavailableError)
    assert not isinstance(caught.value, JobNotFoundError)


# --- Yalnız SELECT; açık transaction yok -----------------------------------------


def test_the_service_only_selects(
    db_session: Session,
    records: tuple[Project, Inventory],
    app_data: Path,
    counted_statements: list[str],
) -> None:
    job_id = _seed(db_session, records, status=JobStatus.SUCCESSFUL, return_code=0)
    publish(app_data, job_id, successful_document(job_id))
    counted_statements.clear()

    call(db_session, job_id, app_data)

    assert counted_statements != []
    for statement in counted_statements:
        head = " ".join(statement.split()).upper()
        assert head.startswith("SELECT"), statement


# --- Kapsam kilidi -----------------------------------------------------------------


def test_the_service_imports_no_runner_worker_or_transport_layer() -> None:
    tree = ast.parse(inspect.getsource(result_service))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    for forbidden in (
        "subprocess",
        "threading",
        "ansible_runner",
        "fastapi",
        "app.api.routes.executions",
        "app.services.jobs.artifacts",
        "app.services.execution.executor",
        "app.services.execution.runner_process",
        "app.services.execution.store",
        "app.services.execution.worker",
        "app.services.execution.workspace",
    ):
        assert forbidden not in imported, forbidden


def test_read_result_document_is_not_exported_by_the_package() -> None:
    import app.services.execution as execution_package

    assert "_read_result_document" not in execution_package.__all__
    assert not hasattr(execution_package, "_read_result_document")
    assert "get_playbook_job_result" in execution_package.__all__
    assert execution_package.get_playbook_job_result is get_playbook_job_result


def test_the_job_result_endpoint_is_exactly_one_get_path(client: TestClient) -> None:
    """Kapsam kilidi: R1-V3D2B ile bağlanan sonuç yolu tam olarak birdir.

    ``GET /api/jobs/{job_id}/result`` dışında sonucu okuyan, güncelleyen veya
    silen fazladan bir yol yoktur.

    Toplam operasyon sayısının tarihçesi (path kümesi değil, **operasyon**
    sayısı): R1-V3J0C controller path browse yolunu ekleyerek 19→20, R1-V3J1
    persistent ping history ``ping-runs`` yolunu ekleyerek 20→21 yaptı. R1-V3J2
    yalnız **frontend** cursor pagination'dı ve backend'e route eklemedi;
    R1-V3J3A da eklemedi — o dilim yalnız mevcut sonuç cevabının şemasını
    genişletti.
    """
    spec = client.get("/openapi.json").json()

    assert set(spec["paths"]) == {
        "/health",
        "/api/projects",
        "/api/projects/{project_id}",
        "/api/projects/{project_id}/playbooks",
        "/api/projects/{project_id}/execution-plan",
        "/api/projects/{project_id}/execution-plans",
        "/api/projects/{project_id}/executions",
        "/api/inventories",
        "/api/inventories/{inventory_id}",
        "/api/inventories/{inventory_id}/hosts",
        "/api/inventories/{inventory_id}/ping",
        "/api/inventories/{inventory_id}/ping/preview",
        "/api/inventories/{inventory_id}/ping/preview/cancel",
        "/api/jobs",
        "/api/jobs/{job_id}",
        "/api/jobs/{job_id}/result",
        # R1-V3J0C: Project/Inventory formlarının "Gözat…" dialogu için tek,
        # salt-okunur controller path browse yolu.
        "/api/controller-paths",
        # R1-V3J1: kalıcı ping geçmişi için tek, salt-okunur liste yolu.
        "/api/inventories/{inventory_id}/ping-runs",
    }
    assert sum(len(operations) for operations in spec["paths"].values()) == 21
    assert set(spec["paths"]["/api/jobs/{job_id}/result"]) == {"get"}


def test_the_parser_docstring_reflects_the_real_floor() -> None:
    from app.services.execution.result import parse_playbook_result

    source = inspect.getsource(parse_playbook_result)
    assert "MIN_ALLOWED_RESULT_BYTES" in source
    assert "``1``\n            ile" not in source


# --- R1-V3J3A: sürüm 1/2 ve ham display çıktısı ------------------------------


def test_a_version_two_result_carries_the_raw_display_output(
    db_session: Session, records: tuple[Project, Inventory], app_data: Path
) -> None:
    """Yetkili okuma yolu ham metni **taşır**; sansürlemez.

    Bu bir gizlilik testi değil, sözleşmenin kendisidir: trusted-operator
    modelinde CLI'da görülecek çıktı UI'da da görülür.
    """
    job_id = _seed(db_session, records, status=JobStatus.SUCCESSFUL, return_code=0)
    document = successful_document(job_id)
    # Vacuous değil: sentinel gerçekten yayımlanan belgede.
    assert "SENTINEL-DISPLAY-PW" in document["ansible_output"]
    publish(app_data, job_id, document)

    result = call(db_session, job_id, app_data)

    assert result.schema_version == SCHEMA_VERSION
    assert result.ansible_output == DISPLAY_OUTPUT
    assert result.ansible_output_truncated is False


def test_a_version_one_artifact_still_reads_with_empty_output(
    db_session: Session, records: tuple[Project, Inventory], app_data: Path
) -> None:
    """Eski artifact okunmaya devam eder; migration yoktur."""
    job_id = _seed(db_session, records, status=JobStatus.SUCCESSFUL, return_code=0)
    document = legacy_document(job_id)
    assert "ansible_output" not in document
    publish(app_data, job_id, document)

    result = call(db_session, job_id, app_data)

    assert result.schema_version == LEGACY_SCHEMA_VERSION
    assert result.ansible_output is None
    assert result.ansible_output_truncated is False
    assert result.recap["web-1"].ok == 1


def test_a_mixed_version_artifact_is_a_generic_503(
    db_session: Session, records: tuple[Project, Inventory], app_data: Path
) -> None:
    """Sürüm ile alan kümesi ayrışırsa sonuç sunulamaz.

    İki yön de ölçülür: sürüm 1 + output alanı ve sürüm 2 − output alanı. İkisi
    de hiçbir writer'ın üretemeyeceği belgelerdir ve aynı sabit cevaba düşerler.
    """
    for document_factory in (
        lambda job_id: legacy_document(job_id, ansible_output=None),
        lambda job_id: {
            key: value
            for key, value in successful_document(job_id).items()
            if key != "ansible_output_truncated"
        },
    ):
        job_id = _seed(db_session, records, status=JobStatus.SUCCESSFUL, return_code=0)
        publish(app_data, job_id, document_factory(job_id))

        with pytest.raises(JobResultUnavailableError) as caught:
            call(db_session, job_id, app_data)

        assert caught.value.status_code == 503
        assert caught.value.code == "job_result_unavailable"
        assert caught.value.details == {"reason": "unavailable"}


def test_the_raw_output_never_reaches_the_error_response_or_the_repr(
    db_session: Session, records: tuple[Project, Inventory], app_data: Path
) -> None:
    """Ham metin hata cevabına ve ``repr``'e giremez; yalnız sonuç nesnesindedir."""
    job_id = _seed(db_session, records, status=JobStatus.SUCCESSFUL, return_code=0)
    publish(app_data, job_id, successful_document(job_id))

    result = call(db_session, job_id, app_data)
    assert "SENTINEL-DISPLAY-PW" in (result.ansible_output or "")
    assert "SENTINEL-DISPLAY-PW" not in repr(result)

    # Aynı belge bozulduğunda hata cevabı da metni taşımaz.
    broken = _seed(db_session, records, status=JobStatus.SUCCESSFUL, return_code=0)
    publish(app_data, broken, successful_document(broken, outcome="boom"))

    with pytest.raises(JobResultUnavailableError) as caught:
        call(db_session, broken, app_data)

    error = caught.value
    for surface in (error.message, repr(error.details), repr(error)):
        assert "SENTINEL-DISPLAY-PW" not in surface
