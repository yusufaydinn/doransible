"""API'den bağımsız atomik Job yaşam döngüsü (T-204B1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models import Job, JobStatus, JobType


class ActivePingJobConflictError(AppError):
    status_code = 409
    code = "job_already_running"


def reserve_pending_ping(
    session: Session,
    *,
    inventory_id: int,
    project_id: int | None,
    limit_pattern: str | None,
    requested_by: str,
    job_id: str | None = None,
    now: datetime | None = None,
) -> Job:
    """Partial unique index güvencesiyle aktif ping rezervasyonu yapar.

    ``job_id`` verilirse o canonical UUID4 kullanılır. Orkestrasyon (T-204B2)
    kimliği rezervasyondan **önce** üretir: artifact dizini ile veritabanı
    satırının aynı kimliği taşıdığı, rezervasyon başarısız olduğunda hangi
    dizinin temizleneceği ancak böyle kesinleşir. Model doğrulaması canonical
    olmayan bir değeri reddeder.
    """
    job = Job(
        id=job_id if job_id is not None else str(uuid.uuid4()),
        job_type=JobType.PING,
        status=JobStatus.PENDING,
        inventory_id=inventory_id,
        project_id=project_id,
        limit_pattern=limit_pattern,
        requested_by=requested_by,
        created_at=now or datetime.now(UTC),
    )
    session.add(job)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        if _is_active_ping_conflict(exc):
            raise ActivePingJobConflictError("Bu inventory için etkin bir ping işi var.") from exc
        raise
    return job


def _is_active_ping_conflict(exc: IntegrityError) -> bool:
    diagnostic = getattr(exc.orig, "diag", None)
    if getattr(diagnostic, "constraint_name", None) == "uq_jobs_active_ping_inventory":
        return True
    message = str(exc.orig)
    return (
        "uq_jobs_active_ping_inventory" in message
        or "UNIQUE constraint failed: jobs.inventory_id" in message
    )


def mark_running(session: Session, job_id: str, *, now: datetime | None = None) -> bool:
    """Pending → running geçişini tek UPDATE ile yapar."""
    result = session.execute(
        update(Job)
        .where(
            Job.id == job_id,
            Job.job_type == JobType.PING,
            Job.status == JobStatus.PENDING,
        )
        .values(status=JobStatus.RUNNING, started_at=now or datetime.now(UTC))
    )
    return getattr(result, "rowcount", 0) == 1


def finish_job(
    session: Session,
    job_id: str,
    *,
    status: JobStatus,
    return_code: int | None,
    artifact_path: str | None,
    now: datetime | None = None,
) -> bool:
    """Running ping işini atomik biçimde terminal duruma geçirir."""
    if status not in {JobStatus.SUCCESSFUL, JobStatus.FAILED, JobStatus.CANCELED}:
        raise ValueError("Terminal Job durumu gerekli.")
    result = session.execute(
        update(Job)
        .where(
            Job.id == job_id,
            Job.job_type == JobType.PING,
            Job.status == JobStatus.RUNNING,
        )
        .values(
            status=status,
            return_code=return_code,
            artifact_path=artifact_path,
            finished_at=now or datetime.now(UTC),
        )
    )
    return getattr(result, "rowcount", 0) == 1


def active_ping_query(inventory_id: int) -> Select[tuple[Job]]:
    return select(Job).where(
        Job.inventory_id == inventory_id,
        Job.job_type == JobType.PING,
        Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
    )


def recover_stale_ping(
    session: Session,
    job_id: str,
    *,
    stale_seconds: float,
    now: datetime | None = None,
) -> bool:
    """Stale kararı ve değişikliği tek atomik UPDATE içinde yapar."""
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(seconds=stale_seconds)
    result = session.execute(
        update(Job)
        .where(
            Job.id == job_id,
            Job.job_type == JobType.PING,
            or_(
                (Job.status == JobStatus.PENDING) & (Job.created_at < cutoff),
                (Job.status == JobStatus.RUNNING)
                & Job.started_at.is_not(None)
                & (Job.started_at < cutoff),
            ),
        )
        .values(status=JobStatus.FAILED, finished_at=moment, return_code=None)
    )
    return getattr(result, "rowcount", 0) == 1
