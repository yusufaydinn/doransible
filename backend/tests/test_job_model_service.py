"""Job migration, partial index ve atomik yaşam döngüsü testleri."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import cast

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, Table, inspect, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateIndex

from app.db.base import Base
from app.models import (
    ExecutionPlanRecord,
    ExecutionPlanStatus,
    Inventory,
    InventorySourceType,
    Job,
    JobStatus,
    JobType,
    Project,
)
from app.services.jobs.service import (
    ActivePingJobConflictError,
    finish_job,
    mark_running,
    recover_stale_ping,
    reserve_pending_ping,
)


def _inventory(session: Session, tmp_path: Path) -> Inventory:
    item = Inventory(
        name="lab", path=str(tmp_path / "hosts.ini"), source_type=InventorySourceType.INI
    )
    session.add(item)
    session.commit()
    return item


def _authorized_playbook_job(session: Session, tmp_path: Path) -> Job:
    """Claim edilmiş bir plana bağlı, geçerli bir ``pending`` PLAYBOOK Job'ı.

    Etkin bir PLAYBOOK Job'ı planını taşımak **zorundadır**
    (``ck_jobs_active_playbook_is_authorized``), bu yüzden ping primitive'lerinin
    ona dokunmadığını ölçmek için önce gerçek bir plan kaydı kurulur; kısıtı
    atlatmak için üretilmiş yarım bir satır aynı şeyi ölçmezdi.
    """
    project = Project(name="lab", path=str(tmp_path / "proje"))
    session.add(project)
    session.commit()
    inventory = Inventory(
        name="lab",
        path=str(tmp_path / "hosts.ini"),
        source_type=InventorySourceType.INI,
        project_id=project.id,
    )
    session.add(inventory)
    session.commit()

    now = datetime.now(UTC)
    plan = ExecutionPlanRecord(
        id=str(uuid.uuid4()),
        token_hash="a" * 64,
        project_id=project.id,
        inventory_id=inventory.id,
        playbook_path="site.yml",
        requested_by="actor",
        input_fingerprint="b" * 64,
        workspace_id=str(uuid.uuid4()),
        manifest_digest="c" * 64,
        status=ExecutionPlanStatus.CLAIMED,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        claimed_at=now,
    )
    session.add(plan)
    session.commit()

    job = Job(
        id=str(uuid.uuid4()),
        job_type=JobType.PLAYBOOK,
        status=JobStatus.PENDING,
        execution_plan_id=plan.id,
        inventory_id=inventory.id,
        project_id=project.id,
        playbook_path="site.yml",
        requested_by="actor",
        created_at=now,
    )
    session.add(job)
    session.commit()
    return job


def test_job_migration_and_metadata_match(migrated_engine: Engine) -> None:
    inspector = inspect(migrated_engine)
    assert "jobs" in inspector.get_table_names()
    assert {column["name"] for column in inspector.get_columns("jobs")} == {
        "id",
        "job_type",
        "status",
        # R1-V3H1A: planın onayladığı çalıştırma kipi (`check`/`normal`).
        "mode",
        "inventory_id",
        "project_id",
        "playbook_path",
        # R1-V3A: Job'u yetkilendiren claim edilmiş plan (ping işlerinde NULL).
        "execution_plan_id",
        "limit_pattern",
        "requested_by",
        "artifact_path",
        "return_code",
        # R1-V3C1A: terminal sebep kodu ve normalize sonucun kırpılma işareti.
        "error_code",
        "result_truncated",
        # R1-V3C1A: worker ownership/lease üçlüsü.
        "worker_id",
        "heartbeat_at",
        "lease_expires_at",
        "started_at",
        "finished_at",
        "created_at",
    }
    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.RUNNING])
def test_database_refuses_an_unauthorized_active_playbook_job(
    db_session: Session, tmp_path: Path, status: JobStatus
) -> None:
    """Planı olmayan etkin PLAYBOOK Job'ı veritabanı seviyesinde reddedilir.

    Kural yalnız servis katmanında dursaydı, doğrudan yazılan bir satır onaysız
    ama çalıştırılabilir görünen bir iş üretirdi: `execution_plan_id` boş
    olduğunda o Job'un arkasında hiçbir dondurulmuş içerik ve hiçbir kullanıcı
    onayı yoktur.

    Satırın **tek** kusuru plan bağının yokluğudur: `running` varyantı geçerli
    bir sahiplik/kira üçlüsü taşır. Aksi hâlde satır aynı anda
    ``ck_jobs_running_playbook_has_lease``'i de ihlal ederdi ve SQLite hangi
    CHECK'i bildireceğini tablo tanımındaki sıraya göre seçtiği için test,
    ölçmek istediği invariant yerine tablo yeniden kurulumlarının ürettiği
    kısıt sırasını ölçerdi.
    """
    inventory = _inventory(db_session, tmp_path)
    now = datetime.now(UTC)
    running = status is JobStatus.RUNNING

    db_session.add(
        Job(
            id=str(uuid.uuid4()),
            job_type=JobType.PLAYBOOK,
            status=status,
            execution_plan_id=None,
            inventory_id=inventory.id,
            playbook_path="site.yml",
            requested_by="actor",
            started_at=now if running else None,
            worker_id=str(uuid.uuid4()) if running else None,
            heartbeat_at=now if running else None,
            lease_expires_at=now + timedelta(minutes=2) if running else None,
            created_at=now,
        )
    )
    with pytest.raises(IntegrityError, match="ck_jobs_active_playbook_is_authorized"):
        db_session.flush()
    db_session.rollback()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("execution_plan_id", None, id="execution_plan_id"),
        pytest.param("project_id", None, id="project_id"),
        pytest.param("playbook_path", None, id="playbook_path"),
        # `limit` ters yönde kontrol edilir: bu dilimde kapsam dışıdır ve dolu
        # bir değer, planda onaylanandan **daha geniş** bir hedef kümesi
        # anlamına gelirdi.
        pytest.param("limit_pattern", "web01", id="limit_pattern"),
    ],
)
def test_active_playbook_job_must_carry_its_binding_fields(
    db_session: Session, tmp_path: Path, field: str, value: object
) -> None:
    """Plan bağı tek başına yetmez: project, playbook ve `limit` de bağlanır."""
    job = _authorized_playbook_job(db_session, tmp_path)
    setattr(job, field, value)

    with pytest.raises(IntegrityError, match="ck_jobs_active_playbook_is_authorized"):
        db_session.flush()
    db_session.rollback()


def test_database_refuses_a_ping_job_bound_to_a_plan(db_session: Session, tmp_path: Path) -> None:
    """Ping'in kendi onay akışı vardır; bir plana bağlanamaz."""
    job = _authorized_playbook_job(db_session, tmp_path)
    plan_id = job.execution_plan_id
    db_session.delete(job)
    db_session.commit()

    db_session.add(
        Job(
            id=str(uuid.uuid4()),
            job_type=JobType.PING,
            status=JobStatus.PENDING,
            execution_plan_id=plan_id,
            inventory_id=job.inventory_id,
            requested_by="actor",
            created_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError, match="ck_jobs_ping_has_no_execution_plan"):
        db_session.flush()
    db_session.rollback()


def test_authorized_playbook_job_is_accepted(db_session: Session, tmp_path: Path) -> None:
    """Kısıt meşru işi engellemez: plana bağlı `pending` PLAYBOOK Job'ı kabul edilir."""
    job = _authorized_playbook_job(db_session, tmp_path)

    assert job.status is JobStatus.PENDING
    assert job.execution_plan_id is not None
    assert job.limit_pattern is None
    # Terminal duruma geçen Job kısıtın dışındadır; geçmiş kayıt korunur.
    job.status = JobStatus.FAILED
    job.finished_at = datetime.now(UTC)
    db_session.commit()


def test_partial_unique_index_compiles_for_sqlite_and_postgresql() -> None:
    table = cast(Table, Job.__table__)
    index = next(index for index in table.indexes if index.name == "uq_jobs_active_ping_inventory")
    sqlite_sql = str(CreateIndex(index).compile(dialect=sqlite.dialect()))
    postgres_sql = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    for sql in (sqlite_sql, postgres_sql):
        assert "UNIQUE INDEX" in sql
        assert "job_type = 'ping'" in sql
        assert "status IN ('pending', 'running')" in sql


def test_database_rejects_two_active_pings(migrated_engine: Engine, tmp_path: Path) -> None:
    with Session(migrated_engine, expire_on_commit=False) as setup:
        inventory_id = _inventory(setup, tmp_path).id
    first_flushed = Event()

    def _reserve_first() -> str:
        with Session(migrated_engine) as first:
            reserve_pending_ping(
                first,
                inventory_id=inventory_id,
                project_id=None,
                limit_pattern=None,
                requested_by="actor",
            )
            first_flushed.set()
            time.sleep(0.1)
            first.commit()
            return "reserved"

    def _reserve_second() -> str:
        assert first_flushed.wait(timeout=5)
        with Session(migrated_engine) as second:
            with pytest.raises(ActivePingJobConflictError):
                reserve_pending_ping(
                    second,
                    inventory_id=inventory_id,
                    project_id=None,
                    limit_pattern=None,
                    requested_by="actor",
                )
            assert second.execute(text("SELECT 1")).scalar_one() == 1
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(_reserve_first)
        second_future = pool.submit(_reserve_second)
        results = {first_future.result(), second_future.result()}
    assert results == {"reserved", "conflict"}


def test_reservation_accepts_an_externally_generated_job_id(
    db_session: Session, tmp_path: Path
) -> None:
    """Orkestrasyon kimliği önceden üretir: artifact dizini ve satır eşleşmeli."""
    inventory = _inventory(db_session, tmp_path)
    job_id = str(uuid.uuid4())

    job = reserve_pending_ping(
        db_session,
        job_id=job_id,
        inventory_id=inventory.id,
        project_id=None,
        limit_pattern=None,
        requested_by="actor",
    )
    db_session.commit()

    assert job.id == job_id
    assert db_session.get(Job, job_id) is not None


def test_reservation_rejects_a_non_canonical_job_id(db_session: Session, tmp_path: Path) -> None:
    inventory = _inventory(db_session, tmp_path)

    with pytest.raises(ValueError):
        reserve_pending_ping(
            db_session,
            job_id="not-a-uuid",
            inventory_id=inventory.id,
            project_id=None,
            limit_pattern=None,
            requested_by="actor",
        )


def test_active_conflict_uses_the_public_error_code() -> None:
    """Kod public sözleşmenin parçasıdır: 409 `job_already_running`."""
    assert ActivePingJobConflictError.status_code == 409
    assert ActivePingJobConflictError.code == "job_already_running"


def test_non_unique_integrity_error_is_not_misclassified(
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        with pytest.raises(IntegrityError):
            reserve_pending_ping(
                session,
                inventory_id=999_999,
                project_id=None,
                limit_pattern=None,
                requested_by="actor",
            )
        assert session.execute(text("SELECT 1")).scalar_one() == 1


def test_running_requires_started_at(db_session: Session, tmp_path: Path) -> None:
    inventory = _inventory(db_session, tmp_path)
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                "INSERT INTO jobs (id,job_type,status,inventory_id,requested_by,created_at) "
                "VALUES (:id,'ping','running',:inventory,'actor',:now)"
            ),
            {"id": str(uuid.uuid4()), "inventory": inventory.id, "now": datetime.now(UTC)},
        )
    db_session.rollback()


@pytest.mark.parametrize(
    "terminal_status",
    [JobStatus.SUCCESSFUL, JobStatus.FAILED, JobStatus.CANCELED],
)
def test_pending_cannot_finish_directly(
    db_session: Session,
    tmp_path: Path,
    terminal_status: JobStatus,
) -> None:
    inventory = _inventory(db_session, tmp_path)
    job = reserve_pending_ping(
        db_session,
        inventory_id=inventory.id,
        project_id=None,
        limit_pattern=None,
        requested_by="actor",
    )
    db_session.commit()

    assert not finish_job(
        db_session,
        job.id,
        status=terminal_status,
        return_code=0,
        artifact_path=None,
    )
    db_session.commit()
    db_session.refresh(job)
    assert job.status is JobStatus.PENDING
    assert job.finished_at is None


def test_valid_ping_transition_and_terminal_is_final(db_session: Session, tmp_path: Path) -> None:
    inventory = _inventory(db_session, tmp_path)
    job = reserve_pending_ping(
        db_session,
        inventory_id=inventory.id,
        project_id=None,
        limit_pattern=None,
        requested_by="actor",
    )
    db_session.commit()
    assert mark_running(db_session, job.id)
    assert finish_job(
        db_session,
        job.id,
        status=JobStatus.SUCCESSFUL,
        return_code=0,
        artifact_path=f"jobs/{job.id}/result.json",
    )
    db_session.commit()
    db_session.refresh(job)
    assert job.status is JobStatus.SUCCESSFUL
    assert not finish_job(
        db_session,
        job.id,
        status=JobStatus.FAILED,
        return_code=1,
        artifact_path=None,
    )


def test_ping_primitives_reject_playbook_job(db_session: Session, tmp_path: Path) -> None:
    job = _authorized_playbook_job(db_session, tmp_path)

    assert not mark_running(db_session, job.id)

    # R1-V3C1A: `running` bir PLAYBOOK satırı sahiplik/lease taşımak
    # **zorundadır** (`ck_jobs_running_playbook_has_lease`). Testin ölçtüğü şey
    # ping primitive'lerinin bu Job'a dokunmaması olduğu için geçiş, gerçek bir
    # worker'ın yazacağı alanlarla birlikte yapılır; alanları boş bırakmak
    # ölçülmek istenen davranış yerine yeni invariantı tetiklerdi.
    now = datetime.now(UTC)
    job.status = JobStatus.RUNNING
    job.started_at = now
    job.worker_id = str(uuid.uuid4())
    job.heartbeat_at = now
    job.lease_expires_at = now + timedelta(minutes=2)
    db_session.commit()
    assert not finish_job(
        db_session,
        job.id,
        status=JobStatus.SUCCESSFUL,
        return_code=0,
        artifact_path=None,
    )


@pytest.mark.parametrize("initial_status", [JobStatus.PENDING, JobStatus.RUNNING])
def test_stale_ping_recovery_is_terminal_and_cannot_be_refinished(
    db_session: Session,
    tmp_path: Path,
    initial_status: JobStatus,
) -> None:
    inventory = _inventory(db_session, tmp_path)
    old = datetime.now(UTC) - timedelta(hours=1)
    job = reserve_pending_ping(
        db_session,
        inventory_id=inventory.id,
        project_id=None,
        limit_pattern="all",
        requested_by="actor",
        now=old,
    )
    db_session.commit()
    if initial_status is JobStatus.RUNNING:
        assert mark_running(db_session, job.id, now=old)
        db_session.commit()
    assert recover_stale_ping(db_session, job.id, stale_seconds=60, now=datetime.now(UTC))
    db_session.commit()
    db_session.refresh(job)
    assert job.status is JobStatus.FAILED
    assert not recover_stale_ping(db_session, job.id, stale_seconds=60)
    assert not finish_job(
        db_session,
        job.id,
        status=JobStatus.SUCCESSFUL,
        return_code=0,
        artifact_path="jobs/x/result.json",
    )
