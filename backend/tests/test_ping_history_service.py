"""R1-V3J1A salt-okunur ping geçmişi servisinin sözleşmesi.

Ölçülen sınırlar:

1. Boş geçmiş, tek ölçüm ve kısmen ulaşılamayan ölçüm mutlu yolları.
2. Sıra ``finished_at DESC, id DESC``'tir ve eşit ``finished_at`` değerlerinde
   kararlıdır; ``limit`` veritabanında uygulanır.
3. Görünürlük **tamamen** ``WHERE`` içindedir: başka inventory, başka aktör,
   terminal olmayan durum, PLAYBOOK türü ve beklenenden farklı ``artifact_path``
   taşıyan satırlar Python'a hiç gelmez.
4. Eksik, symlink, aşırı büyük ve bozuk artifact aynı sabit 503'e düşer;
   sahte bir ``artifact_path`` hiçbir koşulda açılmaz.
5. DB satırı ile belge arasındaki her alan uyuşmazlığı (kimlik, inventory,
   durum, return code, zaman damgaları) aynı sabit 503'tür.
6. Belge doğrulaması katıdır: alan kümesi, şema sürümü, ``job_type``,
   ``bool``-as-``int`` sayaçlar, negatif sayaç, toplam tutarsızlığı ve
   ``hosts`` ihlalleri reddedilir.
7. Servis yalnız ``SELECT`` çalıştırır ve her yolda (dolu, boş, 404, 503)
   çıkışta açık transaction bırakmaz.
8. Çağıran hatası (aralık dışı limit, geçersiz app-data kökü) SQL ve dosya
   sisteminden önce ``ValueError`` olur.
9. Doğrudan SQL ile yazılmış **biçimsiz** bir Job kimliği de aynı generic
   503'e düşer; private bir işaret dışarı kaçmaz ve dosya sistemine hiç
   dokunulmaz (R1-V3J1AF).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import DateTime, Engine, bindparam, event, text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import NotFoundError
from app.models import Inventory, InventorySourceType, Job, JobStatus, JobType, Project
from app.services.execution.result_reader import JOBS_DIRNAME, RESULT_FILENAME
from app.services.inventories.ping_confirm import PING_JOB_TYPE, RESULT_SCHEMA_VERSION
from app.services.inventories.ping_history import (
    MAX_PING_HISTORY_LIMIT,
    MAX_PING_HOST_MESSAGE_LENGTH,
    MAX_PING_RESULT_BYTES,
    PingHistoryPage,
    PingHistoryUnavailableError,
    list_ping_runs,
)
from app.services.jobs.artifacts import JobArtifactStore

ACTOR = "yerel-operator"
OTHER_ACTOR = "baska-operator"


# --- Kurulum yardımcıları -----------------------------------------------------


@pytest.fixture
def records(db_session: Session, tmp_path: Path) -> tuple[Inventory, Inventory]:
    """Aynı project'e bağlı iki ayrı inventory.

    İkincisi süs değildir: "başka inventory'nin ölçümü görünmez" iddiası ancak
    gerçekten başka bir kayıt varken bir şey kanıtlar.
    """
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


@pytest.fixture
def app_data(settings: Settings, migrated_engine: Engine) -> Path:
    """Uygulamanın gerçek app-data kökü.

    ``migrated_engine`` SQLite için ``ensure_app_data_dirs``'i zaten çalıştırır
    ve ``jobs`` alt dizinini 0700 olarak kurar.
    """
    return settings.app_data_dir


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
    finished_at: datetime | None = None,
    artifact_path: Any = _DEFAULT,
) -> str:
    """Tek bir Job satırı yazar ve kimliğini döndürür."""
    identifier = job_id or str(uuid.uuid4())
    start = started_at or datetime.now(UTC)
    finish = finished_at or (start + timedelta(seconds=2))
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
    """Satırla **tutarlı** bir ping belgesi üretir.

    Zaman damgaları satırın kendisinden okunur: testin ayrıca bir zaman
    kurgusu yapması, DB ↔ artifact bağının gerçekten ölçüldüğünü gizlerdi.
    """
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
    """SQLite'ın tzinfo'suz döndürdüğü damgayı UTC'ye bağlar."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def publish(app_data: Path, job_id: str, payload: dict[str, Any]) -> None:
    store = JobArtifactStore(app_data)
    store.create(job_id)
    store.write_result(job_id, payload)


def call(
    session: Session,
    inventory: Inventory,
    app_data: Path,
    *,
    requested_by: str = ACTOR,
    limit: int = 10,
) -> PingHistoryPage:
    return list_ping_runs(
        session,
        inventory.id,
        requested_by=requested_by,
        app_data_dir=app_data,
        limit=limit,
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


# --- Mutlu yol -----------------------------------------------------------------


def test_empty_history_is_an_empty_list_not_an_error(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records
    # Kimlik çağrıdan **önce** okunur: `rollback` ORM örneklerini expire eder ve
    # sonradan okumak, servisin bırakmadığı bir transaction'ı testin kendisi
    # açardı.
    inventory_id = inventory.id

    page = call(db_session, inventory, app_data)

    assert not db_session.in_transaction()
    assert page == PingHistoryPage(inventory_id=inventory_id, items=())


def test_a_single_fully_reachable_measurement_is_summarised(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records
    hosts = [{"name": f"web-{index}", "status": "reachable", "message": None} for index in range(5)]
    job_id = seed(db_session, inventory, status=JobStatus.SUCCESSFUL, return_code=0)
    publish(app_data, job_id, document(job_id, inventory, session=db_session, hosts=hosts))

    page = call(db_session, inventory, app_data)

    assert page.inventory_id == inventory.id
    assert len(page.items) == 1
    item = page.items[0]
    assert item.job_id == job_id
    assert item.status == "successful"
    assert item.return_code == 0
    assert item.started_at.utcoffset() == timedelta(0)
    assert item.finished_at.utcoffset() == timedelta(0)
    assert item.summary.total == 5
    assert item.summary.reachable == 5
    assert item.summary.unreachable == 0
    assert item.summary.failed == 0
    assert item.summary.no_result == 0


def test_a_partially_unreachable_measurement_keeps_the_failed_status(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records
    hosts: list[dict[str, Any]] = [
        {"name": f"web-{index}", "status": "reachable", "message": None} for index in range(4)
    ]
    hosts.append(
        {"name": "web-4", "status": "unreachable", "message": "connect to host *** port ***"}
    )
    job_id = seed(db_session, inventory, status=JobStatus.FAILED, return_code=4)
    publish(
        app_data,
        job_id,
        document(
            job_id, inventory, session=db_session, status="failed", return_code=4, hosts=hosts
        ),
    )

    page = call(db_session, inventory, app_data)

    item = page.items[0]
    assert item.status == "failed"
    assert item.return_code == 4
    assert item.summary.total == 5
    assert item.summary.reachable == 4
    assert item.summary.unreachable == 1


def test_the_public_item_carries_no_host_names_or_messages(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    """Host adı ve mesajı belgede doğrulanır ama dışarı **taşınmaz**."""
    inventory, _ = records
    hosts = [{"name": "gizli-host", "status": "failed", "message": "gizli mesaj"}]
    job_id = seed(db_session, inventory, status=JobStatus.FAILED, return_code=2)
    publish(
        app_data,
        job_id,
        document(
            job_id, inventory, session=db_session, status="failed", return_code=2, hosts=hosts
        ),
    )

    page = call(db_session, inventory, app_data)

    rendered = repr(page)
    assert "gizli-host" not in rendered
    assert "gizli mesaj" not in rendered
    assert ACTOR not in rendered
    assert JOBS_DIRNAME not in rendered


# --- Sıra ve limit --------------------------------------------------------------


def test_measurements_are_returned_newest_first(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records
    base = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
    identifiers = []
    for index in range(3):
        start = base + timedelta(minutes=index)
        job_id = seed(
            db_session,
            inventory,
            started_at=start,
            finished_at=start + timedelta(seconds=5),
        )
        publish(app_data, job_id, document(job_id, inventory, session=db_session))
        identifiers.append(job_id)

    page = call(db_session, inventory, app_data)

    assert [item.job_id for item in page.items] == list(reversed(identifiers))


def test_equal_finish_times_are_broken_by_descending_id(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    """Aynı ``finished_at`` tek anahtarla belirsiz bir sıra üretirdi."""
    inventory, _ = records
    moment = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
    identifiers = sorted(str(uuid.uuid4()) for _ in range(3))
    for identifier in identifiers:
        seed(
            db_session,
            inventory,
            job_id=identifier,
            started_at=moment,
            finished_at=moment + timedelta(seconds=5),
        )
        publish(app_data, identifier, document(identifier, inventory, session=db_session))

    page = call(db_session, inventory, app_data)

    assert [item.job_id for item in page.items] == list(reversed(identifiers))


def test_the_limit_bounds_the_returned_rows(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records
    base = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
    for index in range(4):
        start = base + timedelta(minutes=index)
        job_id = seed(
            db_session, inventory, started_at=start, finished_at=start + timedelta(seconds=1)
        )
        publish(app_data, job_id, document(job_id, inventory, session=db_session))

    assert len(call(db_session, inventory, app_data, limit=2).items) == 2
    assert len(call(db_session, inventory, app_data, limit=MAX_PING_HISTORY_LIMIT).items) == 4


# --- Görünürlük: WHERE içinde ---------------------------------------------------


def test_another_inventorys_measurement_is_never_read(
    db_session: Session,
    records: tuple[Inventory, Inventory],
    app_data: Path,
    counted_statements: list[str],
) -> None:
    inventory, other = records
    foreign = seed(db_session, other)
    publish(app_data, foreign, document(foreign, other, session=db_session))
    counted_statements.clear()

    page = call(db_session, inventory, app_data)

    assert page.items == ()
    assert all(foreign not in statement for statement in counted_statements)


def test_another_actors_measurement_is_invisible(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records
    foreign = seed(db_session, inventory, requested_by=OTHER_ACTOR)
    publish(app_data, foreign, document(foreign, inventory, session=db_session))

    assert call(db_session, inventory, app_data).items == ()


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.RUNNING, JobStatus.CANCELED])
def test_non_terminal_ping_jobs_are_invisible(
    db_session: Session,
    records: tuple[Inventory, Inventory],
    app_data: Path,
    status: JobStatus,
) -> None:
    inventory, _ = records
    seed(db_session, inventory, status=status, return_code=None)

    assert call(db_session, inventory, app_data).items == ()


def test_playbook_jobs_are_invisible(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory, job_type=JobType.PLAYBOOK)
    publish(app_data, job_id, document(job_id, inventory, session=db_session))

    assert call(db_session, inventory, app_data).items == ()


@pytest.mark.parametrize(
    "artifact_path",
    [
        None,
        "",
        "jobs/başka/result.json",
        "/etc/passwd",
        "jobs/{job_id}/../../etc/passwd",
        "jobs/{job_id}/result.json.tmp",
    ],
)
def test_a_row_whose_artifact_path_is_not_the_expected_one_is_invisible(
    db_session: Session,
    records: tuple[Inventory, Inventory],
    app_data: Path,
    artifact_path: str | None,
) -> None:
    """Beklenenden farklı bir yol taşıyan satır elenir ve **hiç açılmaz**."""
    inventory, _ = records
    identifier = str(uuid.uuid4())
    stored = artifact_path.format(job_id=identifier) if artifact_path else artifact_path
    seed(db_session, inventory, job_id=identifier, artifact_path=stored)
    publish(app_data, identifier, document(identifier, inventory, session=db_session))

    assert call(db_session, inventory, app_data).items == ()


def test_a_fake_artifact_path_is_never_opened(
    db_session: Session,
    records: tuple[Inventory, Inventory],
    app_data: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sahte yol taşıyan tek satır varken dosya sistemine hiç dokunulmaz."""
    inventory, _ = records
    seed(db_session, inventory, artifact_path="/etc/passwd")

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Dosya sistemine dokunuldu.")

    monkeypatch.setattr(os, "open", boom)

    assert call(db_session, inventory, app_data).items == ()


# --- Artifact arızaları: tek generic 503 ----------------------------------------


def test_a_missing_artifact_is_a_generic_503(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records
    seed(db_session, inventory)

    with pytest.raises(PingHistoryUnavailableError) as caught:
        call(db_session, inventory, app_data)

    assert caught.value.status_code == 503
    assert caught.value.code == "ping_history_unavailable"
    assert caught.value.details == {"reason": "unavailable"}
    assert not db_session.in_transaction()


def test_the_error_leaks_neither_path_nor_document(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory)
    # Belge, yinelenen host adı yüzünden reddedilir; içindeki ad ve mesaj yine
    # de gerçek bir sızıntı adayıdır.
    hosts = [
        {"name": "gizli-host", "status": "reachable", "message": "gizli mesaj"},
        {"name": "gizli-host", "status": "reachable", "message": "gizli mesaj"},
    ]
    publish(app_data, job_id, document(job_id, inventory, session=db_session, hosts=hosts))

    with pytest.raises(PingHistoryUnavailableError) as caught:
        call(db_session, inventory, app_data)

    rendered = f"{caught.value.message} {caught.value.details}"
    assert "gizli-host" not in rendered
    assert "gizli mesaj" not in rendered
    assert str(app_data) not in rendered
    assert job_id not in rendered


def test_a_symlinked_artifact_is_a_generic_503(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path, tmp_path: Path
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory)
    store = JobArtifactStore(app_data)
    store.create(job_id)
    target = tmp_path / "disarida.json"
    target.write_text("{}", encoding="utf-8")
    (app_data / JOBS_DIRNAME / job_id / RESULT_FILENAME).symlink_to(target)

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


def test_an_oversized_artifact_is_a_generic_503(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory)
    store = JobArtifactStore(app_data)
    store.create(job_id)
    (app_data / JOBS_DIRNAME / job_id / RESULT_FILENAME).write_text(
        " " * (MAX_PING_RESULT_BYTES * 2 + 64), encoding="utf-8"
    )

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


def test_malformed_json_is_a_generic_503(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory)
    store = JobArtifactStore(app_data)
    store.create(job_id)
    (app_data / JOBS_DIRNAME / job_id / RESULT_FILENAME).write_text("{bozuk", encoding="utf-8")

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


def test_one_broken_artifact_fails_the_whole_page(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    """Bozuk satır **sessizce atlanmaz**: eksik bir geçmiş tam görünürdü."""
    inventory, _ = records
    base = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
    healthy = seed(db_session, inventory, started_at=base, finished_at=base + timedelta(seconds=1))
    publish(app_data, healthy, document(healthy, inventory, session=db_session))
    seed(
        db_session,
        inventory,
        started_at=base + timedelta(minutes=1),
        finished_at=base + timedelta(minutes=1, seconds=1),
    )

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


# --- DB ↔ artifact bağı ---------------------------------------------------------


def test_a_document_belonging_to_another_job_is_rejected(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory)
    payload = document(job_id, inventory, session=db_session)
    payload["job_id"] = str(uuid.uuid4())
    publish(app_data, job_id, payload)

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


def test_a_document_naming_another_inventory_is_rejected(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, other = records
    job_id = seed(db_session, inventory)
    payload = document(job_id, inventory, session=db_session)
    payload["inventory_id"] = other.id
    publish(app_data, job_id, payload)

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


def test_a_status_mismatch_is_rejected(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory, status=JobStatus.SUCCESSFUL, return_code=0)
    payload = document(job_id, inventory, session=db_session, status="failed")
    publish(app_data, job_id, payload)

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


def test_a_return_code_mismatch_is_rejected(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory, return_code=0)
    payload = document(job_id, inventory, session=db_session, return_code=1)
    publish(app_data, job_id, payload)

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


@pytest.mark.parametrize("field", ["started_at", "finished_at"])
def test_a_timestamp_mismatch_is_rejected(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path, field: str
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory)
    payload = document(job_id, inventory, session=db_session)
    shifted = datetime.fromisoformat(str(payload[field])) + timedelta(seconds=1)
    payload[field] = shifted.isoformat()
    publish(app_data, job_id, payload)

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-20T09:00:00",
        "2026-08-20T09:00:00+03:00",
        "dun",
        1755680400,
    ],
)
def test_a_non_utc_or_unparsable_timestamp_is_rejected(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path, value: Any
) -> None:
    """Naive damga sessizce UTC sayılmaz, UTC dışı offset çevrilmez."""
    inventory, _ = records
    job_id = seed(db_session, inventory)
    payload = document(job_id, inventory, session=db_session)
    payload["finished_at"] = value
    publish(app_data, job_id, payload)

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


# --- Biçimsiz DB kimliği (R1-V3J1AF) --------------------------------------------


def seed_raw_job_id(
    session: Session,
    inventory: Inventory,
    *,
    job_id: str,
    started_at: datetime,
) -> None:
    """ORM'i **atlayarak** doğrudan SQL ile görünür bir PING satırı yazar.

    ``Job.id`` üzerindeki ``@validates`` kancası yalnız ORM üzerinden yazarken
    çalışır. Testin bozuk bir kimliği gerçekten kaydedebilmesi için o kancanın
    hiç görmediği bir yol gerekir; ``Job(...)`` ile yazmaya çalışmak testi
    kendi kurgusunda düşürür ve ölçmek istediği sınıra hiç ulaştırmazdı
    (vacuous test).

    Satır görünürlük koşullarının **hepsini** sağlar: doğru inventory, doğru
    aktör, terminal durum, dolu ``started_at``/``finished_at`` ve tam olarak
    ``jobs/<kimlik>/result.json`` olan ``artifact_path``. Yani satır sorgudan
    gerçekten geçer; reddedilme yeri kimlik doğrulamasıdır.

    Zaman damgaları SQLAlchemy'nin **kendi** ``DateTime`` bind processor'ından
    geçer; test, SQLite'ın saklama biçimini elle kurgulamaz.
    """
    statement = text(
        "INSERT INTO jobs ("
        "  id, job_type, status, mode, inventory_id, project_id, requested_by,"
        "  artifact_path, return_code, result_truncated,"
        "  started_at, finished_at, created_at"
        ") VALUES ("
        "  :id, 'ping', 'successful', 'check', :inventory_id, :project_id, :requested_by,"
        "  :artifact_path, 0, 0,"
        "  :started_at, :finished_at, :created_at"
        ")"
    ).bindparams(
        bindparam("started_at", type_=DateTime(timezone=True)),
        bindparam("finished_at", type_=DateTime(timezone=True)),
        bindparam("created_at", type_=DateTime(timezone=True)),
    )
    session.execute(
        statement,
        {
            "id": job_id,
            "inventory_id": inventory.id,
            "project_id": inventory.project_id,
            "requested_by": ACTOR,
            "artifact_path": f"{JOBS_DIRNAME}/{job_id}/{RESULT_FILENAME}",
            "started_at": started_at,
            "finished_at": started_at + timedelta(seconds=2),
            "created_at": started_at,
        },
    )
    session.commit()

    # Kurgunun kendisi doğrulanır: satır gerçekten bu kimlikle yazılmış olmalı.
    stored = session.execute(text("SELECT id FROM jobs WHERE id = :id"), {"id": job_id}).scalar()
    assert stored == job_id
    session.rollback()


@pytest.mark.parametrize(
    "job_id",
    [
        # Hiç UUID olmayan bir kimlik.
        "bozuk-job-id",
        # UUID olarak ayrıştırılabilen ama canonical **olmayan** yazım.
        str(uuid.uuid4()).upper(),
        # Doğru biçim, yanlış sürüm.
        "00000000-0000-1000-8000-000000000000",
    ],
)
def test_a_malformed_db_job_id_is_the_same_generic_503(
    db_session: Session,
    records: tuple[Inventory, Inventory],
    app_data: Path,
    monkeypatch: pytest.MonkeyPatch,
    job_id: str,
) -> None:
    """Biçimsiz bir kimlik sözleşmedeki sabit 503'e düşer, private bir işarete değil.

    Doğrudan SQL veya bozuk legacy veri böyle bir satır üretebilir. Kimlik
    doğrulaması ihlal sınırının **dışında** kalsaydı, private ``_RejectedDocument``
    servisten kaçar ve çağıran ``503 ping_history_unavailable`` yerine
    yakalanmamış bir istisna görürdü.
    """
    inventory, _ = records
    seed_raw_job_id(
        db_session,
        inventory,
        job_id=job_id,
        started_at=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
    )

    # Kimlik dosya sistemine **hiç** ulaşmamalıdır: bozuk bir kimliği bir dizin
    # adı olarak aşağı katmana geçirmek, reddin yerini okuyucuya taşırdı.
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Dosya sistemine dokunuldu.")

    monkeypatch.setattr(os, "open", boom)

    with pytest.raises(PingHistoryUnavailableError) as caught:
        call(db_session, inventory, app_data)

    assert caught.value.status_code == 503
    assert caught.value.code == "ping_history_unavailable"
    assert caught.value.details == {"reason": "unavailable"}
    # Bozuk kimlik hiçbir yüzeye yazılmaz.
    rendered = f"{caught.value.message} {caught.value.details} {caught.value.args}"
    assert job_id not in rendered
    assert str(app_data) not in rendered
    assert not db_session.in_transaction()


# --- Belge doğrulaması ----------------------------------------------------------


def test_an_extra_document_field_is_rejected(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory)
    payload = document(job_id, inventory, session=db_session)
    payload["stdout"] = "ham cikti"
    publish(app_data, job_id, payload)

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


def test_a_missing_document_field_is_rejected(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory)
    payload = document(job_id, inventory, session=db_session)
    del payload["limit"]
    publish(app_data, job_id, payload)

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


@pytest.mark.parametrize("value", [RESULT_SCHEMA_VERSION + 1, "1", True, None])
def test_a_foreign_schema_version_is_rejected(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path, value: Any
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory)
    payload = document(job_id, inventory, session=db_session, schema_version=value)
    publish(app_data, job_id, payload)

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


@pytest.mark.parametrize("value", ["playbook", "PING", "", None])
def test_a_foreign_job_type_is_rejected(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path, value: Any
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory)
    payload = document(job_id, inventory, session=db_session, job_type=value)
    publish(app_data, job_id, payload)

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


def test_a_boolean_counter_is_not_accepted_as_an_integer(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    """``bool`` ``int``'in alt sınıfıdır; ``True`` sessizce ``1`` sayılamaz."""
    inventory, _ = records
    job_id = seed(db_session, inventory)
    payload = document(job_id, inventory, session=db_session)
    payload["summary"] = {
        "total": True,
        "reachable": True,
        "unreachable": 0,
        "failed": 0,
        "no_result": 0,
    }
    publish(app_data, job_id, payload)

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


def test_a_negative_counter_is_rejected(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    """Negatif bir sayaç toplamı yine tutturabilir; tek başına toplam yetmez."""
    inventory, _ = records
    job_id = seed(db_session, inventory)
    payload = document(job_id, inventory, session=db_session)
    payload["summary"] = {
        "total": 1,
        "reachable": 2,
        "unreachable": -1,
        "failed": 0,
        "no_result": 0,
    }
    publish(app_data, job_id, payload)

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


def test_an_inconsistent_total_is_rejected(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory)
    payload = document(job_id, inventory, session=db_session)
    payload["summary"] = {
        "total": 9,
        "reachable": 1,
        "unreachable": 0,
        "failed": 0,
        "no_result": 0,
    }
    publish(app_data, job_id, payload)

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


def test_a_summary_with_a_missing_field_is_rejected(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory)
    payload = document(job_id, inventory, session=db_session)
    summary = dict(payload["summary"])
    del summary["no_result"]
    payload["summary"] = summary
    publish(app_data, job_id, payload)

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


def test_a_host_count_that_disagrees_with_total_is_rejected(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory)
    payload = document(job_id, inventory, session=db_session)
    payload["summary"] = {
        "total": 2,
        "reachable": 2,
        "unreachable": 0,
        "failed": 0,
        "no_result": 0,
    }
    publish(app_data, job_id, payload)

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


def test_duplicate_host_names_are_rejected(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    """Aynı host'u iki kez taşıyan belge, başka bir host'un sonucunu gizler."""
    inventory, _ = records
    job_id = seed(db_session, inventory)
    hosts = [
        {"name": "web-1", "status": "reachable", "message": None},
        {"name": "web-1", "status": "reachable", "message": None},
    ]
    publish(app_data, job_id, document(job_id, inventory, session=db_session, hosts=hosts))

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


def test_an_unknown_host_status_is_rejected(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory)
    payload = document(job_id, inventory, session=db_session)
    payload["hosts"] = [{"name": "web-1", "status": "belki", "message": None}]
    publish(app_data, job_id, payload)

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


def test_host_status_counts_must_match_the_summary(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory)
    payload = document(job_id, inventory, session=db_session)
    payload["hosts"] = [{"name": "web-1", "status": "failed", "message": "hata"}]

    publish(app_data, job_id, payload)

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


@pytest.mark.parametrize(
    "message",
    [123, {"metin": "hayir"}, "x" * (MAX_PING_HOST_MESSAGE_LENGTH + 1)],
)
def test_an_invalid_host_message_is_rejected(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path, message: Any
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory)
    hosts = [{"name": "web-1", "status": "failed", "message": message}]
    publish(
        app_data,
        job_id,
        document(
            job_id,
            inventory,
            session=db_session,
            status="failed",
            hosts=hosts,
        ),
    )

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


def test_a_host_message_at_the_documented_ceiling_is_accepted(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    """Yazan tarafın üretebileceği en uzun mesaj hâlâ geçerlidir."""
    inventory, _ = records
    hosts = [
        {
            "name": "web-1",
            "status": "failed",
            "message": "x" * MAX_PING_HOST_MESSAGE_LENGTH,
        }
    ]
    job_id = seed(db_session, inventory, status=JobStatus.FAILED, return_code=2)
    publish(
        app_data,
        job_id,
        document(
            job_id, inventory, session=db_session, status="failed", return_code=2, hosts=hosts
        ),
    )

    page = call(db_session, inventory, app_data)

    assert page.items[0].summary.failed == 1


def test_a_non_object_document_is_rejected(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory)
    store = JobArtifactStore(app_data)
    store.create(job_id)
    (app_data / JOBS_DIRNAME / job_id / RESULT_FILENAME).write_text("[]", encoding="utf-8")

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)


# --- 404 sözleşmesi ve çağıran hatası -------------------------------------------


def test_an_unknown_inventory_keeps_the_existing_404_contract(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records

    with pytest.raises(NotFoundError) as caught:
        list_ping_runs(
            db_session,
            inventory.id + 9999,
            requested_by=ACTOR,
            app_data_dir=app_data,
            limit=10,
        )

    assert caught.value.status_code == 404
    assert caught.value.code == "not_found"
    assert not db_session.in_transaction()


@pytest.mark.parametrize("limit", [0, -1, MAX_PING_HISTORY_LIMIT + 1, True, "10"])
def test_an_out_of_range_limit_is_a_caller_error(
    db_session: Session,
    records: tuple[Inventory, Inventory],
    app_data: Path,
    counted_statements: list[str],
    limit: Any,
) -> None:
    inventory, _ = records
    counted_statements.clear()

    with pytest.raises(ValueError):
        call(db_session, inventory, app_data, limit=limit)

    assert counted_statements == []


@pytest.mark.parametrize("root", ["/app-data", Path("app-data"), Path("/srv/../app-data")])
def test_an_invalid_app_data_root_is_a_caller_error(
    db_session: Session,
    records: tuple[Inventory, Inventory],
    counted_statements: list[str],
    root: Any,
) -> None:
    inventory, _ = records
    counted_statements.clear()

    with pytest.raises(ValueError):
        list_ping_runs(db_session, inventory.id, requested_by=ACTOR, app_data_dir=root, limit=10)

    assert counted_statements == []


# --- Yalnız SELECT; açık transaction yok ----------------------------------------


def test_the_service_only_selects(
    db_session: Session,
    records: tuple[Inventory, Inventory],
    app_data: Path,
    counted_statements: list[str],
) -> None:
    inventory, _ = records
    job_id = seed(db_session, inventory)
    publish(app_data, job_id, document(job_id, inventory, session=db_session))
    counted_statements.clear()

    call(db_session, inventory, app_data)

    assert counted_statements != []
    for statement in counted_statements:
        head = " ".join(statement.split()).upper()
        assert head.startswith("SELECT"), statement
    assert not db_session.in_transaction()


def test_the_session_is_clean_after_a_503(
    db_session: Session, records: tuple[Inventory, Inventory], app_data: Path
) -> None:
    inventory, _ = records
    seed(db_session, inventory)

    with pytest.raises(PingHistoryUnavailableError):
        call(db_session, inventory, app_data)

    assert not db_session.in_transaction()
