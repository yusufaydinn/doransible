"""Project CRUD endpoint'leri (T-102).

Route'lar yalnızca istek/cevap dönüşümü yapar; path doğrulaması, allowlist
kontrolü ve duplicate kararı ``app.services.projects`` içindedir
(route/service katman ayrımı sözleşmesi).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.models import Project
from app.schemas.project import PlaybookListResponse, ProjectCreate, ProjectResponse
from app.services import projects as project_service
from app.services.projects import PlaybookScanResult, ScanLimits

router = APIRouter(prefix="/projects", tags=["projects"])

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.post(
    "",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Project kaydet",
)
def create_project(
    payload: ProjectCreate,
    session: SessionDep,
    settings: SettingsDep,
) -> Project:
    """Var olan bir Ansible dizinini kaydeder.

    Dizin kopyalanmaz veya oluşturulmaz; yalnızca kaydı tutulur.
    """
    return project_service.create_project(
        session,
        name=payload.name,
        path=payload.path,
        description=payload.description,
        allowed_roots=settings.resolve_project_root_allowlist(),
    )


@router.get("", response_model=list[ProjectResponse], summary="Project listesi")
def list_projects(
    session: SessionDep,
    include_inactive: Annotated[bool, Query(description="Pasif kayıtları da listele")] = False,
) -> list[Project]:
    """Kayıtlı project'leri döndürür."""
    return project_service.list_projects(session, include_inactive=include_inactive)


@router.get("/{project_id}", response_model=ProjectResponse, summary="Project detayı")
def get_project(project_id: int, session: SessionDep) -> Project:
    """Tek bir project kaydını döndürür."""
    return project_service.get_project(session, project_id)


@router.get(
    "/{project_id}/playbooks",
    response_model=PlaybookListResponse,
    summary="Project altındaki playbook'ları keşfet",
)
def list_project_playbooks(
    project_id: int,
    session: SessionDep,
    settings: SettingsDep,
) -> PlaybookScanResult:
    """Aktif project kökü altındaki playbook adaylarını döndürür.

    Endpoint **path veya glob parametresi almaz**: taranacak dizin yalnızca
    kayıtlı project kökünden belirlenir (GUVENLIK.md bölüm 4).
    """
    return project_service.list_project_playbooks(
        session,
        project_id,
        allowed_roots=settings.resolve_project_root_allowlist(),
        limits=ScanLimits.from_settings(settings),
    )


@router.delete("/{project_id}", response_model=ProjectResponse, summary="Project'i pasife al")
def deactivate_project(project_id: int, session: SessionDep) -> Project:
    """Project kaydını pasife alır.

    Bu endpoint **dosya silmez**. Project dizini ve içeriği diskte kalır;
    yalnızca kayıt pasif duruma geçer ve varsayılan listede görünmez.
    """
    return project_service.deactivate_project(session, project_id)
