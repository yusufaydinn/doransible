"""Job listesi, detayı ve sonucu endpoint'leri (R1-V3D2B).

Route'lar yalnızca istek/cevap dönüşümü yapar; sıralama, sayfalama,
yetkilendirme, artifact okuma ve ayrıştırma tamamen ``app.services.execution``
içindedir (route/service katman ayrımı sözleşmesi). Aktör hiçbir endpoint'te istekten (body, path,
header veya query) alınmaz; yalnız ``settings.local_actor`` kullanılır.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from fastapi.exceptions import RequestValidationError
from pydantic import UUID4
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.models import ExecutionMode, JobStatus
from app.schemas.execution import UtcDatetime
from app.schemas.job import (
    PlaybookJobListResponse,
    PlaybookJobResultResponse,
    PlaybookJobSummaryResponse,
)
from app.services.execution import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    get_playbook_job,
    get_playbook_job_result,
    list_playbook_jobs,
)

router = APIRouter(prefix="/jobs", tags=["jobs"])

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get(
    "",
    response_model=PlaybookJobListResponse,
    summary="Yetkilendirilmiş PLAYBOOK Job'larını sayfala",
)
def list_jobs(
    session: SessionDep,
    settings: SettingsDep,
    response: Response,
    project_id: Annotated[
        int | None, Query(ge=1, description="Yalnızca bu project'e ait işler")
    ] = None,
    status: Annotated[JobStatus | None, Query(description="Yalnızca bu durumdaki işler")] = None,
    mode: Annotated[ExecutionMode | None, Query(description="Yalnızca bu kipteki işler")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT,
    before_created_at: Annotated[
        UtcDatetime | None, Query(description="Cursor'ın zaman bileşeni")
    ] = None,
    before_job_id: Annotated[UUID4 | None, Query(description="Cursor'ın kimlik bileşeni")] = None,
) -> PlaybookJobListResponse:
    """Aktörün Job'larını en yeni önce, keyset sayfalama ile döner.

    ``before_created_at`` ve ``before_job_id`` yalnız **birlikte** verilebilir;
    yarım bir cursor domain katmanına hiç ulaşmadan sanitize edilmiş bir
    ``request_validation_error`` alır.
    """
    if (before_created_at is None) != (before_job_id is None):
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("query", "before_created_at", "before_job_id"),
                    "msg": "Cursor alanları birlikte verilmeli ya da hiç verilmemelidir.",
                }
            ]
        )
    response.headers["Cache-Control"] = "no-store"
    page = list_playbook_jobs(
        session,
        requested_by=settings.local_actor,
        project_id=project_id,
        status=status,
        mode=mode,
        limit=limit,
        before_created_at=before_created_at,
        before_job_id=None if before_job_id is None else str(before_job_id),
    )
    return PlaybookJobListResponse.model_validate(page)


@router.get(
    "/{job_id}",
    response_model=PlaybookJobSummaryResponse,
    summary="Tek bir yetkilendirilmiş Job'ın özeti",
)
def get_job(
    job_id: UUID4,
    session: SessionDep,
    settings: SettingsDep,
    response: Response,
) -> PlaybookJobSummaryResponse:
    """Aktöre bağlı tek bir Job'ı okur; görünmeyen her Job aynı 404'ü verir."""
    response.headers["Cache-Control"] = "no-store"
    summary = get_playbook_job(session, str(job_id), requested_by=settings.local_actor)
    return PlaybookJobSummaryResponse.model_validate(summary)


@router.get(
    "/{job_id}/result",
    response_model=PlaybookJobResultResponse,
    summary="Bir Job'ın doğrulanmış çalıştırma sonucu",
)
def get_job_result(
    job_id: UUID4,
    session: SessionDep,
    settings: SettingsDep,
    response: Response,
) -> PlaybookJobResultResponse:
    """Terminal ve kayıtlı bir sonucu okur; aksi hâl aynı generic 503'ü verir."""
    response.headers["Cache-Control"] = "no-store"
    result = get_playbook_job_result(
        session,
        str(job_id),
        requested_by=settings.local_actor,
        app_data_dir=settings.app_data_dir,
        max_events=settings.playbook_runner_max_events,
        max_result_bytes=settings.playbook_runner_max_result_bytes,
    )
    return PlaybookJobResultResponse.model_validate(result)
